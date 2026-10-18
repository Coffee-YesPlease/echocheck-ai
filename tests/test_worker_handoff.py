"""The worker speaks; the assigned Supervisor operates the recorder and corrects.

All speech and comparison responses use the fake provider. These tests establish
workflow and evidence boundaries, not voice identity, liveness, or model accuracy.
"""
from __future__ import annotations

import json
import hashlib

import pytest

from test_prototype import prototype
from test_workflow import ALIGNED, INTENDED, MISMATCHED, REQUIREMENTS
from backend.audio import MAX_AUDIO_BYTES
from backend.store import encode


def known_v03_policy(rig):
    """Exact historical policy fixture; current notices must never be guessed."""
    settings = rig.app.state.settings
    days = settings.recording_retention_seconds / 86400
    notice = (
        'I agree to record my voice for this task and send the recording to Google Gemini '
        'for transcription and instruction verification. The assigned Project manager and Supervisor '
        f'may play this recording. Audio access expires after {days:g} days. '
        'Your administrator deletes expired audio under the site retention policy. '
        'Transcripts, consent, approvals, and audit records are retained. Recording is optional; '
        'without recorded verification, EchoCheck cannot release this task to work.'
    )
    policy = {'notice': notice, 'provider': 'Google Gemini', 'model': getattr(rig.provider, 'model', None),
              'retention_seconds': settings.recording_retention_seconds, 'max_seconds': 120,
              'max_bytes': MAX_AUDIO_BYTES, 'consent_version': settings.consent_version, 'mode': settings.mode}
    policy['policy_id'] = hashlib.sha256(encode(policy).encode('utf-8')).hexdigest()
    return policy


def replace_ticket_policy(rig, ticket_id, policy):
    with rig.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_consents SET policy_id=?,policy_json=? WHERE ticket_id=?',
                           (policy['policy_id'], encode(policy), ticket_id))


def instruction_ticket_id(rig, task_id):
    with rig.app.state.store.transaction() as connection:
        return connection.execute("SELECT t.id FROM ec_recording_tickets t JOIN ec_tasks x ON x.instruction_recording_id=(SELECT id FROM ec_recordings WHERE ticket_id=t.id) WHERE x.id=?", (task_id,)).fetchone()[0]


def publish_instruction(rig, task_id):
    rig.switch('manager')
    recorded = rig.record(task_id, 'instruction', INTENDED)
    response = rig.command(f'/api/tasks/{task_id}/publish', {
        'expected_version': recorded['version'], 'instruction': INTENDED,
        'requirements': REQUIREMENTS, 'reviewed_complete': True,
    })
    assert response.status_code == 200, response.text
    return response.json()


def ready_for_worker(rig):
    task = rig.create()
    publish_instruction(rig, task['id'])
    rig.switch('supervisor')
    return rig.task(task['id'])


def request_retelling(rig, task_id, fields=None):
    task = rig.task(task_id)
    disclosure = rig.client.get('/api/health').json()['recording_disclosure']
    body = {
        'expected_version': task['version'], 'purpose': 'retelling',
        'consent_version': disclosure['consent_version'], 'consent_accepted': True,
        'consent_policy_id': disclosure['policy_id'],
    }
    body.update(fields if fields is not None else {'worker_reference': 'Worker 17', 'worker_consent_confirmed': True})
    return rig.command(f'/api/tasks/{task_id}/recording-tickets', body)


def retell(rig, task_id, transcript):
    ticket = request_retelling(rig, task_id)
    assert ticket.status_code == 200, ticket.text
    payload = rig.provider.speech(transcript)
    response = rig.upload(task_id, ticket.json(), payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def acknowledge(rig, task):
    return rig.command(f"/api/tasks/{task['id']}/acknowledge", {
        'expected_version': task['version'], 'attempt_id': task['latest_attempt']['id'],
        'correction': 'Use 12 cartons with 24 bottles each, inspect every carton and deliver to bay 7.',
    })


def test_supervisor_corrects_same_worker_on_own_device_and_requires_fresh_voice(prototype):
    task = ready_for_worker(prototype)
    operator_id = prototype.profile('supervisor')['id']
    admin_id = prototype.profile('admin')['id']
    recorder_device = prototype.identity()['device_id']
    failed, old_audio = retell(prototype, task['id'], MISMATCHED)
    assert failed['status'] == 'awaiting_supervisor_ack'
    assert failed['worker_reference'] == 'Worker 17'
    assert failed['latest_attempt']['speaker'] == {
        'kind': 'shop_floor_worker', 'reference': 'Worker 17',
        'operator_id': operator_id, 'worker_consent_confirmed': True,
    }
    assert request_retelling(prototype, task['id']).status_code == 409

    prototype.switch('manager')
    assert acknowledge(prototype, prototype.task(task['id'])).status_code == 403
    prototype.switch('supervisor')
    correction = acknowledge(prototype, prototype.task(task['id']))
    assert correction.status_code == 200, correction.text
    corrected = correction.json()
    assert corrected['status'] == 'awaiting_worker_rerecording'
    assert not corrected['worker_ok'] and not corrected['supervisor_ok']
    assert corrected['latest_attempt']['id'] == failed['latest_attempt']['id']
    different = request_retelling(prototype, task['id'], {'worker_reference': 'Worker 18', 'worker_consent_confirmed': True})
    assert different.status_code == 409

    replay_ticket = request_retelling(prototype, task['id'])
    assert replay_ticket.status_code == 200, replay_ticket.text
    replay = prototype.upload(task['id'], replay_ticket.json(), old_audio)
    assert replay.status_code in (409, 422), replay.text
    assert prototype.task(task['id'])['status'] != 'awaiting_approvals'
    passing, _ = retell(prototype, task['id'], ALIGNED)
    assert passing['status'] == 'awaiting_approvals'
    assert passing['latest_attempt']['id'] != failed['latest_attempt']['id']
    assert passing['latest_attempt']['speaker']['reference'] == 'Worker 17'
    assert prototype.identity()['device_id'] == recorder_device
    for username in ('supervisor', 'manager'):
        prototype.switch(username)
        current = prototype.task(task['id'])
        response = prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(current), 'approved': True})
        assert response.status_code == 200, response.text
    final = prototype.task(task['id'])
    assert final['worker_ok'] and final['supervisor_ok']
    assert prototype.command(f"/api/tasks/{task['id']}/release", prototype.evidence(final)).status_code == 409
    with prototype.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_users').fetchone()[0] == 3
        recordings = connection.execute("SELECT DISTINCT r.device_id FROM ec_recordings r WHERE r.task_id=? AND purpose='retelling'", (task['id'],)).fetchall()
        assert {row['device_id'] for row in recordings} == {recorder_device}
        acknowledgment = connection.execute('SELECT actor_id FROM ec_acknowledgments WHERE task_id=?', (task['id'],)).fetchone()
        assert acknowledgment['actor_id'] == operator_id
        audit = connection.execute("SELECT actor_id,detail FROM ec_audit WHERE task_id=? AND action='discrepancies_acknowledged'", (task['id'],)).fetchone()
        assert audit['actor_id'] == operator_id and json.loads(audit['detail'])['principal_admin_id'] == admin_id


@pytest.mark.parametrize('fields', [
    {'worker_reference': 'Worker 17'},
    {'worker_reference': 'Worker 17', 'worker_consent_confirmed': False},
    {'worker_reference': 'Worker 17', 'worker_consent_confirmed': 1},
    {'worker_consent_confirmed': True},
    {'worker_reference': '', 'worker_consent_confirmed': True},
    {'worker_reference': '  \t ', 'worker_consent_confirmed': True},
])
def test_worker_reference_and_explicit_consent_are_required_before_capture(prototype, fields):
    task = ready_for_worker(prototype)
    calls = []
    async def unexpected_provider_call(*args):
        calls.append(args)
        raise AssertionError('Invalid capture cannot reach the provider')
    prototype.provider.transcribe = unexpected_provider_call
    response = request_retelling(prototype, task['id'], fields)
    assert response.status_code == 422, response.text
    current = prototype.task(task['id'])
    assert current['status'] == 'awaiting_worker_recording' and current['active_ticket'] is None
    assert current['worker_reference'] is None and calls == []
    with prototype.app.state.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ec_recording_tickets WHERE task_id=? AND purpose='retelling'", (task['id'],)).fetchone()[0] == 0


def test_unassigned_supervisor_cannot_access_or_record_another_supervisors_worker(prototype):
    other = prototype.app.state.store.create_user('other-supervisor', 'Isolated-test-password-123!', 'worker', 'plant-a', 'Other Supervisor')
    prototype.switch('manager')
    created = prototype.command('/api/tasks', {'title': 'Another Supervisor task', 'worker_id': other['id']})
    assert created.status_code == 200, created.text
    task = publish_instruction(prototype, created.json()['id'])
    prototype.switch('supervisor')
    assert prototype.client.get('/api/tasks/' + task['id']).status_code == 404
    disclosure = prototype.client.get('/api/health').json()['recording_disclosure']
    response = prototype.command(f"/api/tasks/{task['id']}/recording-tickets", {
        'expected_version': task['version'], 'purpose': 'retelling',
        'consent_version': disclosure['consent_version'], 'consent_policy_id': disclosure['policy_id'],
        'consent_accepted': True, 'worker_reference': 'Worker 17', 'worker_consent_confirmed': True,
    })
    assert response.status_code == 404


def test_legacy_unidentified_audio_cannot_be_approved_or_relabelled_as_worker_voice(prototype):
    task = ready_for_worker(prototype)
    passed, old_audio = retell(prototype, task['id'], ALIGNED)
    old_recording_id = passed['latest_attempt']['recording_id']
    with prototype.app.state.store.transaction() as connection:
        ticket_id = connection.execute('SELECT ticket_id FROM ec_recordings WHERE id=?', (old_recording_id,)).fetchone()[0]
        connection.execute('DELETE FROM ec_capture_speakers WHERE ticket_id=?', (ticket_id,))
        connection.execute('DELETE FROM ec_task_workers WHERE task_id=?', (task['id'],))
    legacy = prototype.task(task['id'])
    assert legacy['latest_attempt']['speaker'] is None
    assert prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(legacy), 'approved': True}).status_code == 409
    new_ticket = request_retelling(prototype, task['id'])
    assert new_ticket.status_code == 200, new_ticket.text
    replay = prototype.upload(task['id'], new_ticket.json(), old_audio)
    assert replay.status_code in (409, 422), replay.text
    with prototype.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_capture_speakers WHERE ticket_id=?', (ticket_id,)).fetchone()[0] == 0
    fresh, _ = retell(prototype, task['id'], ALIGNED)
    assert fresh['latest_attempt']['recording_id'] != old_recording_id
    assert fresh['latest_attempt']['speaker']['reference'] == 'Worker 17'


def test_supervisor_cannot_edit_or_rerecord_project_manager_intent(prototype):
    task = ready_for_worker(prototype)
    assert prototype.command(f"/api/tasks/{task['id']}/revise", {'expected_version': task['version']}).status_code == 403
    assert prototype.command(f"/api/tasks/{task['id']}/publish", {
        'expected_version': task['version'], 'instruction': 'Ignore the original requirement.',
        'requirements': [{'id': 'other', 'text': 'Do something else.'}], 'reviewed_complete': True,
    }).status_code == 403
    disclosure = prototype.client.get('/api/health').json()['recording_disclosure']
    response = prototype.command(f"/api/tasks/{task['id']}/recording-tickets", {
        'expected_version': task['version'], 'purpose': 'instruction',
        'consent_version': disclosure['consent_version'], 'consent_policy_id': disclosure['policy_id'],
        'consent_accepted': True,
    })
    assert response.status_code == 403
    current = prototype.task(task['id'])
    assert current['instruction'] == INTENDED and current['requirements'] == REQUIREMENTS


def test_voice_provider_failure_preserves_worker_identity_and_keeps_work_blocked(prototype):
    task = ready_for_worker(prototype)
    ticket = request_retelling(prototype, task['id']).json()
    prototype.provider.error = RuntimeError('Private diagnostic not for display')
    response = prototype.upload(task['id'], ticket, prototype.provider.speech(ALIGNED))
    assert response.status_code == 503 and 'Private diagnostic' not in response.text
    current = prototype.task(task['id'])
    assert current['status'] == 'verification_failed' and current['worker_reference'] == 'Worker 17'
    assert not current['worker_ok'] and not current['supervisor_ok']
    assert current['latest_attempt']['speaker']['operator_id'] == prototype.profile('supervisor')['id']
    assert request_retelling(prototype, task['id'], {'worker_reference': 'Worker 18', 'worker_consent_confirmed': True}).status_code == 409


def test_acknowledgment_alone_cannot_approve_or_pass_an_old_retelling(prototype):
    task = ready_for_worker(prototype)
    failed, _ = retell(prototype, task['id'], MISMATCHED)
    response = acknowledge(prototype, failed)
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert corrected['latest_attempt']['verification']['passed'] is False
    assert prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(corrected), 'approved': True}).status_code == 409
    prototype.switch('manager')
    current = prototype.task(task['id'])
    assert prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(current), 'approved': True}).status_code == 409
    assert prototype.task(task['id'])['status'] == 'awaiting_worker_rerecording'


def test_stored_worker_reference_is_canonical_and_cannot_change_by_cancelling_or_revising(prototype):
    task = ready_for_worker(prototype)
    # An unused capture can correct a name typo; an actually stored worker
    # recording binds this task, including later revisions, to that worker.
    unused = request_retelling(prototype, task['id'], {'worker_reference': 'Unused name', 'worker_consent_confirmed': True}).json()
    current = prototype.task(task['id'])
    assert prototype.command(f"/api/recording-tickets/{unused['ticket_id']}/cancel", {'expected_version': current['version']}).status_code == 200
    stored, _ = retell(prototype, task['id'], ALIGNED)
    assert stored['worker_reference'] == 'Worker 17' and stored['worker_reference_locked'] is True
    first = request_retelling(prototype, task['id']).json()
    current = prototype.task(task['id'])
    assert prototype.command(f"/api/recording-tickets/{first['ticket_id']}/cancel", {'expected_version': current['version']}).status_code == 200
    same = request_retelling(prototype, task['id'], {'worker_reference': '  worker   17  ', 'worker_consent_confirmed': True})
    assert same.status_code == 200, same.text
    current = prototype.task(task['id'])
    assert current['worker_reference'] == 'Worker 17'
    assert prototype.command(f"/api/recording-tickets/{same.json()['ticket_id']}/cancel", {'expected_version': current['version']}).status_code == 200
    prototype.switch('manager')
    current = prototype.task(task['id'])
    assert prototype.command(f"/api/tasks/{task['id']}/revise", {'expected_version': current['version']}).status_code == 200
    publish_instruction(prototype, task['id'])
    prototype.switch('supervisor')
    different = request_retelling(prototype, task['id'], {'worker_reference': 'Worker 18', 'worker_consent_confirmed': True})
    assert different.status_code == 409
    assert prototype.task(task['id'])['worker_reference'] == 'Worker 17'


def test_known_v03_pm_instruction_keeps_intent_but_requires_fresh_named_worker_audio(prototype):
    task = ready_for_worker(prototype)
    old, _ = retell(prototype, task['id'], ALIGNED)
    original_instruction_id = old['instruction_recording_id']
    replace_ticket_policy(prototype, instruction_ticket_id(prototype, task['id']), known_v03_policy(prototype))
    with prototype.app.state.store.transaction() as connection:
        connection.execute('DELETE FROM ec_capture_speakers WHERE ticket_id IN (SELECT id FROM ec_recording_tickets WHERE task_id=?)', (task['id'],))
        connection.execute('DELETE FROM ec_task_workers WHERE task_id=?', (task['id'],))
    legacy = prototype.task(task['id'])
    assert legacy['status'] == 'verification_failed' and legacy['latest_attempt']['speaker'] is None
    assert not legacy['worker_ok'] and not legacy['supervisor_ok']
    fresh, _ = retell(prototype, task['id'], ALIGNED)
    assert fresh['status'] == 'awaiting_approvals'
    assert fresh['instruction_recording_id'] == original_instruction_id
    assert fresh['instruction_revision'] == old['instruction_revision']
    assert fresh['latest_attempt']['recording_id'] != old['latest_attempt']['recording_id']
    assert fresh['latest_attempt']['speaker']['kind'] == 'shop_floor_worker'


def test_legacy_pm_policy_rejects_model_drift_and_never_authorizes_worker_consent(prototype):
    task = ready_for_worker(prototype)
    historical = known_v03_policy(prototype)
    replace_ticket_policy(prototype, instruction_ticket_id(prototype, task['id']), historical)
    prototype.provider.model = 'another-processing-model'
    assert request_retelling(prototype, task['id']).status_code == 409
    prototype.provider.model = historical['model']
    capture = request_retelling(prototype, task['id'])
    assert capture.status_code == 200, capture.text
    replace_ticket_policy(prototype, capture.json()['ticket_id'], historical)
    calls = []
    async def unexpected_provider_call(*args):
        calls.append(args)
        raise AssertionError('Legacy own-voice consent does not authorize worker audio processing')
    prototype.provider.transcribe = unexpected_provider_call
    response = prototype.upload(task['id'], capture.json(), prototype.provider.speech(ALIGNED))
    assert response.status_code == 409, response.text
    assert calls == []
    with prototype.app.state.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ec_recordings WHERE task_id=? AND purpose='retelling'", (task['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('audio', [b'', b'not a valid audio recording'])
def test_empty_or_invalid_first_upload_does_not_freeze_a_worker_name(prototype, audio):
    task = ready_for_worker(prototype)
    first = request_retelling(prototype, task['id'], {'worker_reference': 'Name to correct', 'worker_consent_confirmed': True})
    assert first.status_code == 200, first.text
    failed = prototype.upload(task['id'], first.json(), audio)
    assert failed.status_code == 422, failed.text
    current = prototype.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert current['worker_reference'] is None and current['worker_reference_locked'] is False
    replacement = request_retelling(prototype, task['id'])
    assert replacement.status_code == 200, replacement.text
    accepted = prototype.upload(task['id'], replacement.json(), prototype.provider.speech(ALIGNED))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['worker_reference'] == 'Worker 17'
    assert accepted.json()['worker_reference_locked'] is True


def test_unlabelled_legacy_mismatch_recovers_without_an_acknowledgment_deadlock(prototype):
    task = ready_for_worker(prototype)
    mismatch, _ = retell(prototype, task['id'], MISMATCHED)
    assert mismatch['status'] == 'awaiting_supervisor_ack'
    previous_attempt = mismatch['latest_attempt']['id']
    with prototype.app.state.store.transaction() as connection:
        connection.execute("DELETE FROM ec_capture_speakers WHERE ticket_id IN (SELECT id FROM ec_recording_tickets WHERE task_id=? AND purpose='retelling')", (task['id'],))
        connection.execute('DELETE FROM ec_task_workers WHERE task_id=?', (task['id'],))
    recovered = prototype.task(task['id'])
    assert recovered['status'] == 'verification_failed'
    assert recovered['latest_attempt']['id'] == previous_attempt
    assert recovered['latest_attempt']['speaker'] is None
    assert not recovered['worker_ok'] and not recovered['supervisor_ok']
    fresh, _ = retell(prototype, task['id'], ALIGNED)
    assert fresh['status'] == 'awaiting_approvals'
    assert fresh['latest_attempt']['id'] != previous_attempt
    assert fresh['latest_attempt']['speaker']['reference'] == 'Worker 17'
