"""End-to-end policy tests with isolated storage and an explicit fake provider.

Fake speech and evaluation never substitute for real provider or device QA.
No network request or real credential is used by these tests.
"""
from __future__ import annotations

import asyncio
import io
import math
import struct
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app
from backend.verification import validate_evaluation


INTENDED = 'Pack 12 cartons with 24 bottles each. Inspect every carton. Deliver to bay 7.'
ALIGNED = 'I will pack 12 cartons with 24 bottles each, inspect every carton and deliver to bay 7.'
MISMATCHED = 'I will pack 24 cartons with 12 bottles each and deliver to bay 9. Do not inspect cartons.'
REQUIREMENTS = [
    {'id': 'r1', 'text': 'Pack 12 cartons with 24 bottles each.'},
    {'id': 'r2', 'text': 'Inspect every carton.'},
    {'id': 'r3', 'text': 'Deliver to bay 7.'},
]


def audio_sample(sequence):
    """A valid, uniquely encoded two-second WAV for the fake transcription service."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        frames = (int(2500 * math.sin(2 * math.pi * (200 + sequence) * i / 8000)) for i in range(16000))
        audio.writeframes(b''.join(struct.pack('<h', frame) for frame in frames))
    return buffer.getvalue()


class FakeProvider:
    configured = True

    def __init__(self):
        self.transcripts = {}
        self.sequence = 0
        self.error = None
        self.started = None
        self.proceed = None

    def speech(self, transcript):
        self.sequence += 1
        payload = audio_sample(self.sequence)
        self.transcripts[payload] = transcript
        return payload

    async def transcribe(self, audio, mime_type):
        assert mime_type in ('audio/wav', 'audio/x-wav', 'audio/wave')
        if self.started is not None:
            self.started.set()
            if not await asyncio.to_thread(self.proceed.wait, 5):
                raise RuntimeError('Controlled test pause timed out')
        if self.error:
            raise self.error
        return self.transcripts[audio]

    async def extract_requirements(self, instruction):
        return {'requirements': REQUIREMENTS, 'complete': True, 'uncertainties': []}

    async def evaluate(self, requirements, transcript):
        if self.error:
            raise self.error
        matched = transcript == ALIGNED
        payload = {
            'requirements': [{'id': r['id'], 'status': 'matched' if matched else 'contradiction',
                              'evidence': transcript} for r in requirements],
            'complete': True, 'extra_actions': [], 'uncertainties': [],
            'summary': 'All reviewed requirements matched.' if matched else 'Retelling contradicts the instruction.',
        }
        return validate_evaluation(requirements, transcript, payload)


class Rig:
    def __init__(self, app, provider, supervisor, worker, outsider, same_device):
        self.app, self.provider = app, provider
        self.supervisor, self.worker, self.outsider, self.same_device = supervisor, worker, outsider, same_device
        self.worker_id = worker.get('/api/me').json()['user']['id']

    def command(self, client, path, body):
        me = client.get('/api/me')
        assert me.status_code == 200, me.text
        return client.post(path, json=body, headers={'X-CSRF-Token': me.json()['csrf_token']})

    def task(self, task_id, client=None):
        response = (client or self.supervisor).get('/api/tasks/' + task_id)
        assert response.status_code == 200, response.text
        return response.json()

    def create(self):
        response = self.command(self.supervisor, '/api/tasks', {'title': 'Packing and dispatch', 'worker_id': self.worker_id})
        assert response.status_code in (200, 201), response.text
        return response.json()

    def ticket(self, task_id, client, purpose):
        task = self.task(task_id, client)
        disclosure = client.get('/api/health').json()['recording_disclosure']
        response = self.command(client, f'/api/tasks/{task_id}/recording-tickets', {
            'expected_version': task['version'], 'purpose': purpose,
            'consent_version': self.app.state.settings.consent_version, 'consent_accepted': True,
            'consent_policy_id': disclosure['policy_id'],
        })
        return response

    def upload(self, task_id, client, ticket, payload):
        me = client.get('/api/me').json()
        return client.post(f"/api/tasks/{task_id}/recordings/{ticket['ticket_id']}", content=payload,
                           headers={'Content-Type': 'audio/wav', 'X-CSRF-Token': me['csrf_token'],
                                    'X-Expected-Version': str(ticket['version'])})

    def record(self, task_id, client, purpose, transcript):
        payload = self.provider.speech(transcript)
        ticket = self.ticket(task_id, client, purpose)
        assert ticket.status_code in (200, 201), ticket.text
        uploaded = self.upload(task_id, client, ticket.json(), payload)
        assert uploaded.status_code in (200, 201), uploaded.text
        return self.task(task_id, client), payload

    def publish(self, task_id):
        self.record(task_id, self.supervisor, 'instruction', INTENDED)
        task = self.task(task_id)
        response = self.command(self.supervisor, f'/api/tasks/{task_id}/publish', {
            'expected_version': task['version'], 'instruction': INTENDED,
            'requirements': REQUIREMENTS, 'reviewed_complete': True,
        })
        assert response.status_code in (200, 201), response.text
        return self.task(task_id)

    def verified(self):
        task = self.create()
        self.publish(task['id'])
        return self.record(task['id'], self.worker, 'retelling', ALIGNED)[0]

    @staticmethod
    def evidence(task):
        return {'expected_version': task['version'], 'instruction_revision': task['instruction_revision'],
                'attempt_id': task['latest_attempt']['id'] if task['latest_attempt'] else 'superseded-attempt'}

    def approve_both(self, task_id):
        for client in (self.worker, self.supervisor):
            task = self.task(task_id, client)
            response = self.command(client, f'/api/tasks/{task_id}/approve', {**self.evidence(task), 'approved': True})
            assert response.status_code in (200, 201), response.text
        return self.task(task_id)

    def release(self, task_id):
        return self.command(self.supervisor, f'/api/tasks/{task_id}/release', self.evidence(self.task(task_id)))


@pytest.fixture
def rig_factory(tmp_path):
    stack = ExitStack()
    created = []

    def factory(production=True):
        number = len(created)
        provider = FakeProvider()
        settings = Settings(db_path=tmp_path / f'{number}.db', audio_dir=tmp_path / f'audio-{number}',
                            app_origin='https://testserver', mode='production' if production else 'development',
                            release_enabled=production, secure_cookies=True)
        app = create_app(settings=settings, provider=provider)
        clients = [stack.enter_context(TestClient(app, base_url='https://testserver',
                                                  headers={'Origin': 'https://testserver'},
                                                  raise_server_exceptions=False)) for _ in range(4)]
        for username, role, site in [('supervisor', 'supervisor', 'plant-a'), ('worker', 'worker', 'plant-a'),
                                     ('outsider', 'worker', 'plant-b')]:
            app.state.store.create_user(username, 'Review-only-password-927!', role, site, username.title())
        for client, username in zip(clients[:3], ['supervisor', 'worker', 'outsider']):
            response = client.post('/api/login', json={'username': username, 'password': 'Review-only-password-927!'})
            assert response.status_code == 200, response.text
        result = Rig(app, provider, *clients)
        created.append(result)
        return result

    yield factory
    stack.close()


@pytest.fixture
def rig(rig_factory):
    return rig_factory()


def test_unauthenticated_task_access_and_mutations_are_denied(rig):
    task = rig.create()
    for path in ('/api/tasks', '/api/tasks/' + task['id']):
        assert rig.same_device.get(path).status_code == 401
    assert rig.same_device.post('/api/tasks', json={'title': 'Anonymous', 'worker_id': rig.worker_id}).status_code == 401


def test_csrf_and_cross_origin_mutations_are_denied(rig):
    body = {'title': 'Packing task', 'worker_id': rig.worker_id}
    assert rig.supervisor.post('/api/tasks', json=body).status_code == 403
    csrf = rig.supervisor.get('/api/me').json()['csrf_token']
    response = rig.supervisor.post('/api/tasks', json=body,
                                  headers={'Origin': 'https://untrusted.example', 'X-CSRF-Token': csrf})
    assert response.status_code == 403


def test_worker_cannot_create_or_read_unassigned_tasks(rig):
    assert rig.command(rig.worker, '/api/tasks', {'title': 'Forged task', 'worker_id': rig.worker_id}).status_code == 403
    task = rig.create()
    assert rig.outsider.get('/api/tasks/' + task['id']).status_code in (403, 404)
    assert rig.outsider.get('/api/tasks').json() == []


def test_cross_site_assignment_is_denied(rig):
    outsider_id = rig.outsider.get('/api/me').json()['user']['id']
    response = rig.command(rig.supervisor, '/api/tasks', {'title': 'Cross site', 'worker_id': outsider_id})
    assert response.status_code in (403, 404, 422)


def test_worker_cannot_record_supervisor_instruction(rig):
    task = rig.create()
    assert rig.ticket(task['id'], rig.worker, 'instruction').status_code == 403


def test_explicit_current_consent_is_required_before_capture(rig):
    task = rig.create()
    for consent in (False, True):
        response = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/recording-tickets", {
            'expected_version': task['version'], 'purpose': 'instruction',
            'consent_version': 'outdated-notice', 'consent_accepted': consent,
        })
        assert response.status_code in (409, 422, 400)
    assert rig.task(task['id'])['status'] == 'draft'


def test_same_browser_cannot_satisfy_second_device_requirement(rig):
    task = rig.create()
    rig.publish(task['id'])
    # Logging out/in changes identity while retaining the server-issued device cookie.
    rig.command(rig.supervisor, '/api/logout', {})
    login = rig.supervisor.post('/api/login', json={'username': 'worker', 'password': 'Review-only-password-927!'})
    assert login.status_code == 200
    response = rig.ticket(task['id'], rig.supervisor, 'retelling')
    assert response.status_code in (403, 409)


def test_release_requires_exact_verified_recording_and_both_people(rig):
    task = rig.verified()
    assert task['status'] == 'awaiting_approvals'
    assert rig.release(task['id']).status_code == 409
    body = {**rig.evidence(task), 'approved': True}
    assert rig.command(rig.worker, f"/api/tasks/{task['id']}/approve", body).status_code == 200
    assert rig.release(task['id']).status_code == 409
    task = rig.task(task['id'])
    assert rig.command(rig.supervisor, f"/api/tasks/{task['id']}/approve", {
        **rig.evidence(task), 'approved': True}).status_code == 200
    assert rig.task(task['id'])['status'] == 'awaiting_approvals'
    response = rig.release(task['id'])
    assert response.status_code == 200, response.text
    assert rig.task(task['id'])['status'] == 'released'


def test_development_mode_cannot_release_even_after_mocked_success(rig_factory):
    rig = rig_factory(production=False)
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code in (403, 409)
    assert rig.task(task['id'])['status'] != 'released'


def test_corrections_require_supervisor_ack_and_fresh_voice(rig):
    task = rig.create()
    rig.publish(task['id'])
    task, old_audio = rig.record(task['id'], rig.worker, 'retelling', MISMATCHED)
    assert task['status'] == 'awaiting_supervisor_ack'
    assert rig.ticket(task['id'], rig.worker, 'retelling').status_code == 409
    body = {'expected_version': task['version'], 'attempt_id': task['latest_attempt']['id'],
            'correction': 'Use 12 cartons with 24 bottles each; inspect every carton and deliver to bay 7.'}
    assert rig.command(rig.worker, f"/api/tasks/{task['id']}/acknowledge", body).status_code == 403
    assert rig.command(rig.supervisor, f"/api/tasks/{task['id']}/acknowledge", body).status_code == 200
    assert rig.task(task['id'])['status'] == 'awaiting_worker_rerecording'
    ticket = rig.ticket(task['id'], rig.worker, 'retelling')
    assert ticket.status_code in (200, 201), ticket.text
    replay = rig.upload(task['id'], rig.worker, ticket.json(), old_audio)
    assert replay.status_code in (409, 422), replay.text
    assert rig.task(task['id'])['status'] != 'awaiting_approvals'
    task, _ = rig.record(task['id'], rig.worker, 'retelling', ALIGNED)
    assert task['status'] == 'awaiting_approvals'
    assert not task['worker_ok'] and not task['supervisor_ok']


def test_stale_approval_cannot_apply_to_new_instruction(rig):
    task = rig.verified()
    stale = {**rig.evidence(task), 'approved': True}
    revision = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/revise", {'expected_version': task['version']})
    assert revision.status_code == 200, revision.text
    assert rig.command(rig.worker, f"/api/tasks/{task['id']}/approve", stale).status_code == 409
    current = rig.task(task['id'])
    assert current['status'] == 'draft'
    assert current['instruction_revision'] == task['instruction_revision'] + 1
    assert not current['worker_ok'] and not current['supervisor_ok']


def test_revision_cannot_publish_using_previous_recording(rig):
    task = rig.verified()
    assert rig.command(rig.supervisor, f"/api/tasks/{task['id']}/revise", {'expected_version': task['version']}).status_code == 200
    current = rig.task(task['id'])
    response = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/publish", {
        'expected_version': current['version'], 'instruction': INTENDED,
        'requirements': REQUIREMENTS, 'reviewed_complete': True,
    })
    assert response.status_code == 409


def test_recording_start_immediately_revokes_existing_release(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    ticket = rig.ticket(task['id'], rig.worker, 'retelling')
    assert ticket.status_code in (200, 201), ticket.text
    current = rig.task(task['id'])
    assert current['status'] != 'released'
    assert not current['worker_ok'] and not current['supervisor_ok']
    assert rig.release(task['id']).status_code == 409


def test_provider_failure_is_persisted_and_blocks_old_approval(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    ticket = rig.ticket(task['id'], rig.worker, 'retelling')
    assert ticket.status_code in (200, 201), ticket.text
    rig.provider.error = RuntimeError('private provider diagnostic containing SECRET-TEST-MARKER')
    response = rig.upload(task['id'], rig.worker, ticket.json(), rig.provider.speech(MISMATCHED))
    assert response.status_code in (409, 422, 502, 503), response.text
    assert 'SECRET-TEST-MARKER' not in response.text
    current = rig.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert not current['worker_ok'] and not current['supervisor_ok']
    assert rig.release(task['id']).status_code == 409


def test_cancelled_recording_cannot_be_uploaded_or_released(rig):
    task = rig.create()
    rig.publish(task['id'])
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    cancelled = rig.command(rig.worker, f"/api/recording-tickets/{ticket['ticket_id']}/cancel", {
        'expected_version': ticket['version'],
    })
    assert cancelled.status_code == 200, cancelled.text
    response = rig.upload(task['id'], rig.worker, ticket, rig.provider.speech(ALIGNED))
    assert response.status_code == 409
    assert rig.task(task['id'])['status'] != 'released'


def test_late_analysis_cannot_overwrite_a_revision(rig):
    task = rig.create()
    rig.publish(task['id'])
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    rig.provider.started, rig.provider.proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(rig.upload, task['id'], rig.worker, ticket, rig.provider.speech(ALIGNED))
        try:
            assert rig.provider.started.wait(5)
            current = rig.task(task['id'])
            response = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/revise", {'expected_version': current['version']})
            assert response.status_code == 200, response.text
        finally:
            rig.provider.proceed.set()
        response = pending.result(timeout=5)
    assert response.status_code == 409, response.text
    assert rig.task(task['id'])['status'] == 'draft'


def test_worker_cannot_forge_supervisor_approval(rig):
    task = rig.verified()
    response = rig.command(rig.worker, f"/api/tasks/{task['id']}/approve", {
        **rig.evidence(task), 'approved': True, 'role': 'supervisor',
    })
    assert response.status_code == 422
    assert not rig.task(task['id'])['supervisor_ok']


def test_withdrawal_revokes_release(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    response = rig.command(rig.worker, f"/api/tasks/{task['id']}/withdraw", rig.evidence(rig.task(task['id'])))
    assert response.status_code == 200, response.text
    assert rig.task(task['id'])['status'] != 'released'
    assert rig.release(task['id']).status_code == 409


def test_legacy_text_only_and_role_confirmation_routes_are_removed(rig):
    task = rig.create()
    for path, body in [('teachback', {'text': ALIGNED}), ('confirm', {'role': 'supervisor', 'approved': True})]:
        assert rig.command(rig.worker, f"/api/tasks/{task['id']}/{path}", body).status_code == 404


def test_same_ticket_cannot_be_uploaded_twice(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    payload = rig.provider.speech(INTENDED)
    assert rig.upload(task['id'], rig.supervisor, ticket, payload).status_code == 200
    assert rig.upload(task['id'], rig.supervisor, ticket, payload).status_code == 409


def test_old_consent_ticket_cannot_send_audio_after_notice_changes(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    rig.app.state.settings.consent_version = 'new-consent-notice'
    response = rig.upload(task['id'], rig.supervisor, ticket, rig.provider.speech(INTENDED))
    assert response.status_code == 409, response.text
    current = rig.task(task['id'])
    assert current['status'] == 'draft'
    assert current['instruction_recording_id'] is None


def test_numeric_consent_does_not_create_a_recording_ticket(rig):
    task = rig.create()
    health = rig.supervisor.get('/api/health').json()
    response = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/recording-tickets", {
        'expected_version': task['version'], 'purpose': 'instruction',
        'consent_version': health['consent_version'], 'consent_accepted': 1,
        'consent_policy_id': health['recording_disclosure']['policy_id'],
    })
    assert response.status_code == 422
    assert rig.task(task['id'])['status'] == 'draft'


def test_retention_policy_change_invalidates_an_open_capture_ticket(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    rig.app.state.settings.recording_retention_seconds += 3600
    response = rig.upload(task['id'], rig.supervisor, ticket, rig.provider.speech(INTENDED))
    assert response.status_code == 409, response.text
    assert rig.task(task['id'])['instruction_recording_id'] is None


def test_missing_recording_revokes_released_status(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    with rig.app.state.store.transaction() as connection:
        record = connection.execute('SELECT path FROM ec_recordings WHERE id=?',
                                    (task['instruction_recording_id'],)).fetchone()
    (rig.app.state.settings.audio_dir / record['path']).unlink()
    current = rig.task(task['id'])
    assert current['status'] != 'released'
    assert current['release_eligible'] is False
    assert not current['worker_ok'] and not current['supervisor_ok']


def test_expired_recording_cannot_be_played_or_keep_release(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    recording_id = task['latest_attempt']['recording_id']
    with rig.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_recordings SET retained_until=0 WHERE id=?', (recording_id,))
    assert rig.worker.get('/api/recordings/' + recording_id).status_code == 410
    assert rig.task(task['id'])['status'] != 'released'
    assert rig.app.state.store.purge_expired() == 1


def test_audio_is_private_and_authorized_only(rig):
    task = rig.verified()
    recording_id = task['instruction_recording_id']
    response = rig.worker.get('/api/recordings/' + recording_id)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert rig.outsider.get('/api/recordings/' + recording_id).status_code == 404
    assert rig.same_device.get('/api/recordings/' + recording_id).status_code == 401
    with rig.app.state.store.transaction() as connection:
        record = connection.execute('SELECT path FROM ec_recordings WHERE id=?', (recording_id,)).fetchone()
    assert (rig.app.state.settings.audio_dir / record['path']).stat().st_mode & 0o777 == 0o600
    assert rig.app.state.settings.audio_dir.stat().st_mode & 0o777 == 0o700


def test_logout_during_analysis_is_retryable_without_stale_release(rig):
    task = rig.create()
    rig.publish(task['id'])
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    rig.provider.started, rig.provider.proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(rig.upload, task['id'], rig.worker, ticket, rig.provider.speech(ALIGNED))
        try:
            assert rig.provider.started.wait(5)
            assert rig.command(rig.worker, '/api/logout', {}).status_code == 200
        finally:
            rig.provider.proceed.set()
        response = pending.result(timeout=5)
    assert response.status_code in (401, 409), response.text
    assert rig.worker.post('/api/login', json={'username': 'worker', 'password': 'Review-only-password-927!'}).status_code == 200
    current = rig.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert current['release_eligible'] is False
    new_ticket = rig.ticket(task['id'], rig.worker, 'retelling')
    assert new_ticket.status_code in (200, 201), new_ticket.text


def test_relogin_cannot_upload_a_ticket_bound_to_old_session(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    assert rig.command(rig.supervisor, '/api/logout', {}).status_code == 200
    assert rig.supervisor.post('/api/login', json={'username': 'supervisor', 'password': 'Review-only-password-927!'}).status_code == 200
    upload = rig.upload(task['id'], rig.supervisor, ticket, rig.provider.speech(INTENDED))
    assert upload.status_code in (401, 404, 409)
    cancel = rig.command(rig.supervisor, f"/api/recording-tickets/{ticket['ticket_id']}/cancel", {
        'expected_version': rig.task(task['id'])['version'],
    })
    assert cancel.status_code == 200, cancel.text
    assert rig.task(task['id'])['status'] == 'draft'


def test_cancelled_recording_does_not_persist_uploaded_audio(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    rig.command(rig.supervisor, f"/api/recording-tickets/{ticket['ticket_id']}/cancel", {
        'expected_version': ticket['version'],
    })
    assert rig.upload(task['id'], rig.supervisor, ticket, rig.provider.speech(INTENDED)).status_code == 409
    with rig.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_recordings').fetchone()[0] == 0
    assert not list(rig.app.state.settings.audio_dir.iterdir())


def test_invalid_audio_is_blocked_without_a_provider_pass(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    response = rig.upload(task['id'], rig.supervisor, ticket, b'not an audio recording')
    assert response.status_code == 422
    assert rig.task(task['id'])['status'] == 'draft'
    assert rig.task(task['id'])['instruction_recording_id'] is None


def test_repeated_discrepancy_requires_another_acknowledgment(rig):
    task = rig.create()
    rig.publish(task['id'])
    for _ in range(2):
        task, _ = rig.record(task['id'], rig.worker, 'retelling', MISMATCHED)
        assert task['status'] == 'awaiting_supervisor_ack'
        assert rig.ticket(task['id'], rig.worker, 'retelling').status_code == 409
        acknowledged = rig.command(rig.supervisor, f"/api/tasks/{task['id']}/acknowledge", {
            'expected_version': task['version'], 'attempt_id': task['latest_attempt']['id'],
            'correction': 'Explain the correct quantities, every inspection and bay 7.',
        })
        assert acknowledged.status_code == 200, acknowledged.text
    current, _ = rig.record(task['id'], rig.worker, 'retelling', ALIGNED)
    assert current['status'] == 'awaiting_approvals'
    with rig.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_acknowledgments WHERE task_id=?', (task['id'],)).fetchone()[0] == 2
        assert connection.execute('SELECT COUNT(*) FROM ec_attempts WHERE task_id=?', (task['id'],)).fetchone()[0] == 3


def test_concurrent_approvals_reject_stale_state_then_allow_fresh_review(rig):
    task = rig.verified()
    body = {**rig.evidence(task), 'approved': True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(rig.command, rig.worker, f"/api/tasks/{task['id']}/approve", body)
        second = pool.submit(rig.command, rig.supervisor, f"/api/tasks/{task['id']}/approve", body)
        responses = [first.result(timeout=5), second.result(timeout=5)]
    assert sorted(response.status_code for response in responses) == [200, 409]
    current = rig.task(task['id'])
    assert current['worker_ok'] != current['supervisor_ok']
    assert current['release_eligible'] is False
    missing_client = rig.worker if not current['worker_ok'] else rig.supervisor
    retried = rig.command(missing_client, f"/api/tasks/{task['id']}/approve", {
        **rig.evidence(current), 'approved': True,
    })
    assert retried.status_code == 200, retried.text
    assert rig.release(task['id']).status_code == 200


def test_abandoned_processing_lease_expires_to_a_retryable_blocked_state(rig):
    task = rig.create()
    rig.publish(task['id'])
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    # Model a process dying after accepting an upload: no provider result can arrive.
    with rig.app.state.store.transaction() as connection:
        connection.execute("UPDATE ec_recording_tickets SET status='consuming',operation_expires=0 WHERE id=?",
                           (ticket['ticket_id'],))
        connection.execute("UPDATE ec_tasks SET status='verification_pending' WHERE id=?", (task['id'],))
    current = rig.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert current['active_ticket'] is None
    assert current['release_eligible'] is False
    new_ticket = rig.ticket(task['id'], rig.worker, 'retelling')
    assert new_ticket.status_code in (200, 201), new_ticket.text


def test_legacy_simulated_approval_data_is_not_automatically_imported(rig):
    with rig.app.state.store.transaction() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, instruction TEXT)')
        connection.execute('INSERT INTO tasks VALUES(?,?,?)', ('legacy-approved', 'confirmed', 'An old instruction'))
    assert rig.supervisor.get('/api/tasks').json() == []
    assert rig.worker.get('/api/tasks/legacy-approved').status_code == 404
    with rig.app.state.store.transaction() as connection:
        assert connection.execute('SELECT status FROM tasks WHERE id=?', ('legacy-approved',)).fetchone()[0] == 'confirmed'


def test_oversized_recording_never_reaches_storage_or_verification(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    oversized = b'RIFF' + b'\0' * (8 * 1024 * 1024)
    response = rig.upload(task['id'], rig.supervisor, ticket, oversized)
    assert response.status_code in (413, 422), response.text
    current = rig.task(task['id'])
    assert current['status'] == 'draft'
    assert current['instruction_recording_id'] is None
    with rig.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_recordings').fetchone()[0] == 0
    assert not list(rig.app.state.settings.audio_dir.iterdir())


def test_expired_session_cannot_access_task_or_recording(rig):
    task = rig.verified()
    recording_id = task['instruction_recording_id']
    with rig.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_sessions SET expires=0 WHERE user_id=?', (rig.worker_id,))
    assert rig.worker.get('/api/me').status_code == 401
    assert rig.worker.get('/api/tasks/' + task['id']).status_code == 401
    assert rig.worker.get('/api/recordings/' + recording_id).status_code == 401


def test_recording_ticket_cannot_be_used_by_another_assigned_person(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    response = rig.upload(task['id'], rig.worker, ticket, rig.provider.speech(INTENDED))
    assert response.status_code in (403, 404), response.text
    assert rig.task(task['id'])['instruction_recording_id'] is None


def test_worker_cannot_release_even_after_both_approvals(rig):
    task = rig.verified()
    task = rig.approve_both(task['id'])
    response = rig.command(rig.worker, f"/api/tasks/{task['id']}/release", rig.evidence(task))
    assert response.status_code == 403
    assert rig.task(task['id'])['status'] == 'awaiting_approvals'


def test_concurrent_release_and_revision_cannot_mix_evidence(rig):
    task = rig.verified()
    task = rig.approve_both(task['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        release = pool.submit(rig.command, rig.supervisor, f"/api/tasks/{task['id']}/release", rig.evidence(task))
        revision = pool.submit(rig.command, rig.supervisor, f"/api/tasks/{task['id']}/revise", {'expected_version': task['version']})
        responses = [release.result(timeout=5), revision.result(timeout=5)]
    assert sorted(response.status_code for response in responses) == [200, 409]
    current = rig.task(task['id'])
    if current['status'] == 'released':
        assert current['instruction_revision'] == task['instruction_revision']
        assert current['latest_attempt']['id'] == task['latest_attempt']['id']
        # A subsequently reviewed revision still revokes the successful old release.
        assert rig.command(rig.supervisor, f"/api/tasks/{task['id']}/revise", {'expected_version': current['version']}).status_code == 200
        current = rig.task(task['id'])
    assert current['status'] == 'draft'
    assert current['instruction_revision'] == task['instruction_revision'] + 1
    assert not current['worker_ok'] and not current['supervisor_ok']


def test_provider_supplied_pass_flag_cannot_override_contradictions(rig):
    task = rig.create()
    rig.publish(task['id'])

    async def forged_pass(requirements, transcript):
        return {'requirements': [{'id': r['id'], 'status': 'contradiction', 'evidence': transcript} for r in requirements],
                'complete': True, 'extra_actions': [], 'uncertainties': [], 'summary': 'Contradictions found.',
                'passed': True, 'decision': 'passed'}

    rig.provider.evaluate = forged_pass
    current, _ = rig.record(task['id'], rig.worker, 'retelling', MISMATCHED)
    assert current['status'] == 'awaiting_supervisor_ack'
    assert current['latest_attempt']['verification']['passed'] is False
    assert rig.release(task['id']).status_code == 409


def test_incomplete_provider_coverage_is_a_persisted_failure(rig):
    task = rig.create()
    rig.publish(task['id'])

    async def omitted_requirement(requirements, transcript):
        return {'requirements': [{'id': requirements[0]['id'], 'status': 'matched', 'evidence': transcript}],
                'complete': True, 'extra_actions': [], 'uncertainties': [], 'summary': 'Incomplete response.'}

    rig.provider.evaluate = omitted_requirement
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    response = rig.upload(task['id'], rig.worker, ticket, rig.provider.speech(ALIGNED))
    assert response.status_code == 503, response.text
    current = rig.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert current['release_eligible'] is False
    assert not current['worker_ok'] and not current['supervisor_ok']


def test_revoked_participant_access_invalidates_existing_release(rig):
    task = rig.verified()
    rig.approve_both(task['id'])
    assert rig.release(task['id']).status_code == 200
    with rig.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_users SET active=0 WHERE id=?', (rig.worker_id,))
    assert rig.worker.get('/api/me').status_code == 401
    assert rig.worker.get('/api/recordings/' + task['instruction_recording_id']).status_code == 401
    # The supervisor list also revokes a previously released visual status.
    current = rig.supervisor.get('/api/tasks').json()[0]
    assert current['status'] != 'released'
    assert current['release_eligible'] is False


def test_repeated_bad_passwords_are_rate_limited_without_account_details(rig):
    statuses = []
    for _ in range(11):
        response = rig.same_device.post('/api/login', json={'username': 'missing-account', 'password': 'Not-a-real-password-927!'})
        statuses.append(response.status_code)
        assert 'password_hash' not in response.text and 'missing-account' not in response.text
    assert statuses[:10] == [401] * 10
    assert statuses[-1] == 429
    assert response.headers['retry-after'] == '900'
