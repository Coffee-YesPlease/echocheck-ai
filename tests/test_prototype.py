"""Explicit prototype access, perspective switching, and isolation from production.

Speech is supplied by the existing fake provider. No credential or network call
in these tests verifies the accuracy of a real model or captures a real voice.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.auth import hash_password, token_hash
from backend.config import Settings
from backend.main import create_app
from backend.prototype import bootstrap_prototype
from backend.store import Store
from test_workflow import ALIGNED, INTENDED, REQUIREMENTS, FakeProvider


CREDENTIALS = {
    'admin': ('admin123', 'admin', 'supervisor'),
    'manager': ('manager123', 'project_manager', 'supervisor'),
    'supervisor': ('super123', 'supervisor', 'worker'),
}


class PrototypeRig:
    def __init__(self, app, provider, stack):
        self.app, self.provider, self.stack = app, provider, stack
        self.client = self.client_for('admin')

    def client_for(self, username, password=None):
        client = self.stack.enter_context(TestClient(
            self.app, base_url=self.app.state.settings.app_origin,
            headers={'Origin': self.app.state.settings.app_origin}, raise_server_exceptions=False,
        ))
        response = client.post('/api/login', json={
            'username': username, 'password': password or CREDENTIALS[username][0],
        })
        assert response.status_code == 200, response.text
        return client

    def profile(self, username):
        with self.app.state.store.transaction() as connection:
            return dict(connection.execute('SELECT * FROM ec_users WHERE username=?', (username,)).fetchone())

    def identity(self, client=None):
        response = (client or self.client).get('/api/me')
        assert response.status_code == 200, response.text
        return response.json()

    def command(self, path, body, client=None):
        client = client or self.client
        identity = self.identity(client)
        return client.post(path, json=body, headers={'X-CSRF-Token': identity['csrf_token']})

    def switch(self, username=None):
        response = self.command('/api/prototype/view-as', {
            'user_id': self.profile(username)['id'] if username else None,
        })
        assert response.status_code == 200, response.text
        return self.identity()

    def create(self):
        self.switch('manager')
        response = self.command('/api/tasks', {
            'title': 'Prototype role handoff', 'worker_id': self.profile('supervisor')['id'],
        })
        assert response.status_code == 200, response.text
        return response.json()

    def task(self, task_id):
        response = self.client.get('/api/tasks/' + task_id)
        assert response.status_code == 200, response.text
        return response.json()

    def ticket(self, task_id, purpose):
        task = self.task(task_id)
        disclosure = self.client.get('/api/health').json()['recording_disclosure']
        body = {
            'expected_version': task['version'], 'purpose': purpose,
            'consent_version': disclosure['consent_version'], 'consent_accepted': True,
            'consent_policy_id': disclosure['policy_id'],
        }
        if purpose == 'retelling':
            body.update(worker_reference='Worker 17', worker_consent_confirmed=True)
        response = self.command(f'/api/tasks/{task_id}/recording-tickets', body)
        assert response.status_code == 200, response.text
        return response.json()

    def upload(self, task_id, ticket, payload):
        identity = self.identity()
        return self.client.post(f"/api/tasks/{task_id}/recordings/{ticket['ticket_id']}", content=payload, headers={
            'Content-Type': 'audio/wav', 'X-CSRF-Token': identity['csrf_token'],
            'X-Expected-Version': str(ticket['version']),
        })

    def record(self, task_id, purpose, transcript):
        ticket = self.ticket(task_id, purpose)
        response = self.upload(task_id, ticket, self.provider.speech(transcript))
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def evidence(task):
        return {'expected_version': task['version'], 'instruction_revision': task['instruction_revision'],
                'attempt_id': task['latest_attempt']['id']}


@pytest.fixture
def prototype(tmp_path):
    with ExitStack() as stack:
        settings = Settings(mode='prototype', db_path=tmp_path / 'prototype.db',
                            audio_dir=tmp_path / 'private-audio', release_enabled=True)
        provider = FakeProvider()
        yield PrototypeRig(create_app(settings=settings, provider=provider), provider, stack)


def test_exactly_three_seeded_logins_have_requested_passwords_and_public_roles(prototype):
    listed = prototype.client.get('/api/prototype/users')
    assert listed.status_code == 200
    assert {user['username'] for user in listed.json()} == set(CREDENTIALS)
    for username, (password, persona, workflow_role) in CREDENTIALS.items():
        client = prototype.client_for(username, password)
        identity = prototype.identity(client)
        assert identity['prototype'] is True
        assert identity['user']['username'] == username
        assert identity['user']['role'] == persona
        assert identity['user']['workflow_role'] == workflow_role
        if username == 'admin':
            assert identity['admin_user']['username'] == 'admin'
            assert {user['username'] for user in identity['view_as_users']} == set(CREDENTIALS)
        else:
            assert identity['admin_user'] is None and identity['view_as_users'] == []
    with prototype.app.state.store.transaction() as connection:
        assert {row['username'] for row in connection.execute('SELECT username FROM ec_users')} == set(CREDENTIALS)
        assert connection.execute('SELECT COUNT(*) FROM ec_demo_profiles').fetchone()[0] == 3


def test_bootstrap_and_application_restart_preserve_custom_passwords_and_existing_sessions(prototype):
    manager_id = prototype.profile('manager')['id']
    custom_hash = hash_password('Custom-prototype-pass-123!', prototype=True)
    with prototype.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_users SET password_hash=? WHERE id=?', (custom_hash, manager_id))
        ids_before = {row['username']: row['id'] for row in connection.execute('SELECT id,username FROM ec_users')}
    bootstrap_prototype(prototype.app.state.store)
    restarted = create_app(settings=prototype.app.state.settings, provider=FakeProvider())
    assert prototype.identity()['user']['username'] == 'admin'
    with restarted.state.store.transaction() as connection:
        assert {row['username']: row['id'] for row in connection.execute('SELECT id,username FROM ec_users')} == ids_before
        assert connection.execute('SELECT password_hash FROM ec_users WHERE id=?', (manager_id,)).fetchone()[0] == custom_hash
    with TestClient(restarted, headers={'Origin': restarted.state.settings.app_origin}) as client:
        assert client.post('/api/login', json={'username': 'manager', 'password': 'manager123'}).status_code == 401
        assert client.post('/api/login', json={'username': 'manager', 'password': 'Custom-prototype-pass-123!'}).status_code == 200


def test_explicit_reset_replaces_legacy_supervisor_password_role_and_revokes_session(tmp_path):
    settings = Settings(mode='development', db_path=tmp_path / 'legacy.db', audio_dir=tmp_path / 'audio')
    store = Store(settings)
    previous = store.create_user('supervisor', 'Previous-supervisor-password-123!', 'supervisor', 'plant-a', 'Supervisor')
    old_app = create_app(settings=settings, provider=FakeProvider())
    with TestClient(old_app, base_url=settings.app_origin, headers={'Origin': settings.app_origin}) as old_client:
        assert old_client.post('/api/login', json={'username': 'supervisor', 'password': 'Previous-supervisor-password-123!'}).status_code == 200
        prototype_settings = Settings(mode='prototype', db_path=settings.db_path, audio_dir=settings.audio_dir)
        prototype_store = Store(prototype_settings)
        bootstrap_prototype(prototype_store, reset=True)
        assert old_client.get('/api/me').status_code == 401
        with prototype_store.transaction() as connection:
            replacement = connection.execute('SELECT * FROM ec_users WHERE username=?', ('supervisor',)).fetchone()
            assert replacement['id'] == previous['id'] and replacement['role'] == 'worker'
            assert connection.execute('SELECT COUNT(*) FROM ec_sessions WHERE user_id=?', (previous['id'],)).fetchone()[0] == 0
        new_app = create_app(settings=prototype_settings, provider=FakeProvider())
        with TestClient(new_app, base_url=settings.app_origin, headers={'Origin': settings.app_origin}) as client:
            assert client.post('/api/login', json={'username': 'supervisor', 'password': 'Previous-supervisor-password-123!'}).status_code == 401
            assert client.post('/api/login', json={'username': 'supervisor', 'password': 'super123'}).status_code == 200


def test_admin_base_view_is_read_only_and_can_return_without_logging_out(prototype):
    response = prototype.command('/api/tasks', {'title': 'Forbidden admin task', 'worker_id': prototype.profile('supervisor')['id']})
    assert response.status_code == 403
    task = prototype.create()
    prototype.switch('supervisor')
    visible = prototype.client.get('/api/tasks').json()
    assert task['id'] in {row['id'] for row in visible}
    assert prototype.task(task['id'])['supervisor_id'] == prototype.profile('manager')['id']
    identity = prototype.switch()
    assert identity['user']['role'] == 'admin'
    assert prototype.task(task['id'])['id'] == task['id']
    assert prototype.command(f"/api/tasks/{task['id']}/revise", {'expected_version': task['version']}).status_code == 403


@pytest.mark.parametrize('username', ['manager', 'supervisor'])
def test_non_admin_cannot_switch_or_list_prototype_perspectives(prototype, username):
    client = prototype.client_for(username)
    assert client.get('/api/prototype/users').status_code == 403
    response = prototype.command('/api/prototype/view-as', {'user_id': prototype.profile('admin')['id']}, client)
    assert response.status_code == 403
    assert prototype.identity(client)['user']['username'] == username


@pytest.mark.parametrize('target_kind', ['unknown', 'non_demo', 'foreign_site', 'inactive'])
def test_admin_switch_rejects_unknown_non_demo_foreign_and_inactive_targets(prototype, target_kind):
    if target_kind == 'unknown':
        target_id = 'not-a-prototype-user'
    elif target_kind == 'non_demo':
        target_id = prototype.app.state.store.create_user('unlisted', 'Non-demo-password-123!', 'worker', 'plant-a', 'Unlisted')['id']
    else:
        target_id = prototype.profile('supervisor')['id']
        with prototype.app.state.store.transaction() as connection:
            if target_kind == 'foreign_site':
                connection.execute('UPDATE ec_users SET site=? WHERE id=?', ('foreign-plant', target_id))
            else:
                connection.execute('UPDATE ec_users SET active=0 WHERE id=?', (target_id,))
    response = prototype.command('/api/prototype/view-as', {'user_id': target_id})
    assert response.status_code in (403, 404, 422), response.text
    assert prototype.identity()['user']['username'] == 'admin'


def test_switch_rotates_csrf_but_keeps_underlying_admin_session_principal(prototype):
    before = prototype.identity()
    raw_session = prototype.client.cookies.get('ec_session')
    switched = prototype.switch('manager')
    assert switched['csrf_token'] != before['csrf_token']
    assert switched['user']['username'] == 'manager' and switched['admin_user']['username'] == 'admin'
    assert prototype.client.get('/api/prototype/users').status_code == 200
    assert prototype.client.cookies.get('ec_session') == raw_session
    body = {'title': 'Current perspective', 'worker_id': prototype.profile('supervisor')['id']}
    stale = prototype.client.post('/api/tasks', json=body, headers={'X-CSRF-Token': before['csrf_token']})
    assert stale.status_code == 403
    assert prototype.command('/api/tasks', body).status_code == 200
    admin_id, manager_id = prototype.profile('admin')['id'], prototype.profile('manager')['id']
    with prototype.app.state.store.transaction() as connection:
        session = connection.execute('SELECT * FROM ec_sessions WHERE token_hash=?', (token_hash(raw_session),)).fetchone()
        assert session['user_id'] == admin_id
        selected = connection.execute('SELECT target_user_id FROM ec_demo_views WHERE session_id=?', (session['id'],)).fetchone()
        assert selected['target_user_id'] == manager_id


def test_switch_cancels_inflight_capture_and_late_response_cannot_publish(prototype):
    task = prototype.create()
    ticket = prototype.ticket(task['id'], 'instruction')
    prototype.provider.started, prototype.provider.proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(prototype.upload, task['id'], ticket, prototype.provider.speech(INTENDED))
        try:
            assert prototype.provider.started.wait(5)
            prototype.switch('supervisor')
        finally:
            prototype.provider.proceed.set()
        response = pending.result(timeout=5)
    assert response.status_code in (403, 409), response.text
    current = prototype.task(task['id'])
    assert current['status'] == 'draft' and not current['instruction']
    assert current['active_ticket'] is None and current['latest_attempt'] is None
    prototype.switch('manager')
    prototype.provider.started = None
    replacement = prototype.ticket(task['id'], 'instruction')
    assert replacement['ticket_id'] != ticket['ticket_id']


def test_admin_can_try_full_voice_workflow_in_one_browser_but_prototype_never_releases(prototype):
    raw_session = prototype.client.cookies.get('ec_session')
    task = prototype.create()
    current = prototype.record(task['id'], 'instruction', INTENDED)
    published = prototype.command(f"/api/tasks/{task['id']}/publish", {
        'expected_version': current['version'], 'instruction': INTENDED,
        'requirements': REQUIREMENTS, 'reviewed_complete': True,
    })
    assert published.status_code == 200, published.text
    prototype.switch('supervisor')
    current = prototype.record(task['id'], 'retelling', ALIGNED)
    assert current['status'] == 'awaiting_approvals'
    assert prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(current), 'approved': True}).status_code == 200
    prototype.switch('manager')
    current = prototype.task(task['id'])
    assert prototype.command(f"/api/tasks/{task['id']}/approve", {**prototype.evidence(current), 'approved': True}).status_code == 200
    current = prototype.task(task['id'])
    assert current['supervisor_ok'] and current['worker_ok']
    assert current['release_eligible'] is False and current['release_environment_ready'] is False
    released = prototype.command(f"/api/tasks/{task['id']}/release", prototype.evidence(current))
    assert released.status_code == 409, released.text
    assert prototype.task(task['id'])['status'] == 'awaiting_approvals'
    assert prototype.client.cookies.get('ec_session') == raw_session
    participant_ids = {prototype.profile('manager')['id'], prototype.profile('supervisor')['id']}
    admin_id = prototype.profile('admin')['id']
    with prototype.app.state.store.transaction() as connection:
        actors = connection.execute("SELECT actor_id,detail FROM ec_audit WHERE task_id=? AND action='verification_approved'", (task['id'],)).fetchall()
        assert {row['actor_id'] for row in actors} == participant_ids
        assert all(json.loads(row['detail'])['principal_admin_id'] == admin_id for row in actors)


def test_production_rejects_demo_credentials_existing_demo_session_and_switch_routes(prototype):
    settings = prototype.app.state.settings
    production = create_app(settings=Settings(
        mode='production', db_path=settings.db_path, audio_dir=settings.audio_dir,
        app_origin='https://testserver', release_enabled=True,
    ), provider=FakeProvider())
    with TestClient(production, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        for username, (password, _, _) in CREDENTIALS.items():
            response = client.post('/api/login', json={'username': username, 'password': password})
            assert response.status_code == 401
        # Send the existing demo cookies explicitly so a host change does not
        # turn this into an accidental missing-cookie test.
        demo_cookie = '; '.join(f'{name}={prototype.client.cookies.get(name)}' for name in ('ec_session', 'ec_device'))
        assert client.get('/api/me', headers={'Cookie': demo_cookie}).status_code == 401
        assert client.get('/api/prototype/users').status_code == 404
        assert client.post('/api/prototype/view-as', json={'user_id': None}).status_code == 404
    with pytest.raises(ValueError):
        bootstrap_prototype(production.state.store)
    with pytest.raises(ValueError):
        production.state.store.create_user('short', 'simple', 'worker', 'plant-a', 'Short')


@pytest.mark.parametrize('mode', ['prototype', 'development'])
def test_loopback_origin_alias_works_only_on_same_port_in_local_modes(tmp_path, mode):
    settings = Settings(mode=mode, app_origin='http://localhost:8000', db_path=tmp_path / 'origin.db', audio_dir=tmp_path / 'audio')
    app = create_app(settings=settings, provider=FakeProvider())
    username, password = 'admin', 'admin123'
    if mode == 'development':
        username, password = 'local-user', 'Local-user-password-123!'
        app.state.store.create_user(username, password, 'supervisor', 'plant-a', 'Local')
    with TestClient(app, base_url='http://127.0.0.1:8000', headers={'Origin': 'http://127.0.0.1:8000'}) as client:
        assert client.post('/api/login', json={'username': username, 'password': password}).status_code == 200
        for origin in ('http://127.0.0.1:8001', 'https://localhost:8000', 'http://foreign.example:8000', 'null'):
            result = client.post('/api/login', json={'username': username, 'password': password}, headers={'Origin': origin})
            assert result.status_code == 403, (origin, result.text)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        assert client.post('/api/login', json={'username': username, 'password': password}).status_code == 403


def test_production_origin_requires_exact_configured_origin(tmp_path):
    settings = Settings(mode='production', app_origin='https://localhost:8000', db_path=tmp_path / 'prod.db', audio_dir=tmp_path / 'audio')
    app = create_app(settings=settings, provider=FakeProvider())
    app.state.store.create_user('real-user', 'Production-user-password-123!', 'supervisor', 'plant-a', 'Real')
    with TestClient(app, base_url=settings.app_origin, headers={'Origin': settings.app_origin}) as client:
        body = {'username': 'real-user', 'password': 'Production-user-password-123!'}
        assert client.post('/api/login', json=body).status_code == 200
        for origin in ('https://127.0.0.1:8000', 'https://localhost:8001', 'https://foreign.example:8000', 'null'):
            assert client.post('/api/login', json=body, headers={'Origin': origin}).status_code == 403
    with TestClient(app, base_url=settings.app_origin) as client:
        assert client.post('/api/login', json=body).status_code == 403
