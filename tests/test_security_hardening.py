"""Abuse and malformed-input hardening for the public prototype.

Covers findings from the security review: requests that used to end in server
errors, sign-in floods, oversized bodies, storage and task growth, demo history
growth, and personal identifiers copied to the Firestore mirror.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import threading

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.config import Settings
from backend.main import create_app
from test_demo import BASE, DemoRig, ForbiddenLiveProvider
from test_firestore_mirror import FakeFirestore, collection
from test_prototype import PrototypeRig
from test_workflow import FakeProvider


def make_rig(tmp_path, stack, **settings):
    provider = FakeProvider()
    config = Settings(mode='prototype', db_path=tmp_path / 'p.db', audio_dir=tmp_path / 'a', **settings)
    return PrototypeRig(create_app(settings=config, provider=provider), provider, stack)


def raw_command(rig, path, body):
    """Send JSON with escapes intact (\\ud800) so lone surrogates reach the server."""
    token = rig.identity()['csrf_token']
    return rig.client.post(path, content=json.dumps(body), headers={'X-CSRF-Token': token, 'Content-Type': 'application/json'})


def anonymous(rig, stack):
    origin = rig.app.state.settings.app_origin
    return stack.enter_context(TestClient(rig.app, base_url=origin, headers={'Origin': origin}, raise_server_exceptions=False))


@pytest.fixture
def rig(tmp_path):
    with ExitStack() as stack:
        yield make_rig(tmp_path, stack), stack


def test_unencodable_text_is_rejected_cleanly_instead_of_a_server_error(rig):
    prototype, stack = rig
    client = anonymous(prototype, stack)
    login = client.post('/api/login', content=json.dumps({'username': '\ud800', 'password': 'x'}), headers={'Content-Type': 'application/json'})
    assert login.status_code == 422
    prototype.switch('manager')
    worker = prototype.profile('supervisor')['id']
    assert raw_command(prototype, '/api/tasks', {'title': 'Bad \ud800 title', 'worker_id': worker}).status_code == 422
    task = prototype.create()
    response = raw_command(prototype, f"/api/tasks/{task['id']}/acknowledge", {'expected_version': task['version'], 'attempt_id': 'x', 'correction': 'Fix \udfff it'})
    assert response.status_code == 422
    prototype.switch('supervisor')
    run = prototype.client.get(BASE).json()[0]
    response = raw_command(prototype, f"{BASE}/{run['id']}/acknowledge", {'expected_version': run['version'], 'attempt_id': 'x', 'correction': 'Fix \udfff it'})
    assert response.status_code == 422


def test_non_ascii_csrf_header_is_refused_not_a_server_error(rig):
    prototype, _ = rig
    prototype.switch('manager')
    response = prototype.client.post('/api/tasks', content=json.dumps({'title': 'abcd', 'worker_id': prototype.profile('supervisor')['id']}),
                                     headers={'X-CSRF-Token': 'caf\xe9'.encode('latin-1'), 'Content-Type': 'application/json'})
    assert response.status_code == 403


def test_oversized_json_bodies_are_refused_before_parsing(rig):
    prototype, stack = rig
    client = anonymous(prototype, stack)
    body = json.dumps({'username': 'a' * (600 * 1024), 'password': 'x'})
    response = client.post('/api/login', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert response.headers['x-content-type-options'] == 'nosniff'
    # Normal requests are unaffected.
    assert client.post('/api/login', json={'username': 'admin', 'password': 'admin123'}).status_code == 200


def test_preset_sign_in_skips_rehashing_and_floods_wait_at_one_hash(rig, monkeypatch):
    prototype, stack = rig
    calls = []
    original = main_module.verify_password

    def counting(password, encoded):
        calls.append(password)
        return original(password, encoded)

    monkeypatch.setattr(main_module, 'verify_password', counting)
    client = anonymous(prototype, stack)
    for _ in range(3):
        assert client.post('/api/login', json={'username': 'manager', 'password': 'manager123'}).status_code == 200
    assert calls.count('manager123') <= 1
    assert client.post('/api/login', json={'username': 'manager', 'password': 'wrong-guess'}).status_code == 401

    release = threading.Event()

    def slow(password, encoded):
        release.wait(5)
        return False

    monkeypatch.setattr(main_module, 'verify_password', slow)
    monkeypatch.setattr(main_module, 'PASSWORD_GATE_SECONDS', 0.2)
    results = []
    first = threading.Thread(target=lambda: results.append(client.post('/api/login', json={'username': 'admin', 'password': 'guess-1'}).status_code))
    first.start()
    try:
        second = anonymous(prototype, stack).post('/api/login', json={'username': 'admin', 'password': 'guess-2'})
        assert second.status_code == 429
    finally:
        release.set()
        first.join(10)
    assert results == [401]


def test_recording_storage_cap_refuses_new_tickets_once_full(tmp_path):
    with ExitStack() as stack:
        prototype = make_rig(tmp_path, stack, stored_audio_limit_bytes=main_module.MAX_AUDIO_BYTES + 1)
        task = prototype.create()
        prototype.record(task['id'], 'instruction', 'Pack exactly twelve cartons.')
        current = prototype.task(task['id'])
        disclosure = prototype.client.get('/api/health').json()['recording_disclosure']
        response = prototype.command(f"/api/tasks/{task['id']}/recording-tickets", {
            'expected_version': current['version'], 'purpose': 'instruction', 'consent_accepted': True,
            'consent_version': disclosure['consent_version'], 'consent_policy_id': disclosure['policy_id']})
        assert response.status_code == 503 and 'storage' in response.json()['detail']


def test_prototype_task_count_is_capped(tmp_path):
    with ExitStack() as stack:
        prototype = make_rig(tmp_path, stack, prototype_task_limit=2)
        prototype.create()
        prototype.create()
        response = prototype.command('/api/tasks', {'title': 'One too many', 'worker_id': prototype.profile('supervisor')['id']})
        assert response.status_code == 429


def test_demo_history_returns_only_the_newest_events(tmp_path):
    with ExitStack() as stack:
        config = Settings(mode='prototype', db_path=tmp_path / 'd.db', audio_dir=tmp_path / 'a')
        app = create_app(settings=config, provider=ForbiddenLiveProvider())
        demo = DemoRig(app, app.state.provider, stack)
        demo.switch('supervisor')
        run = demo.runs()[0]
        for _ in range(70):
            assert demo.action(run['id'], 'restart').status_code == 200
        history = demo.run(run['id'])['history']
        assert len(history) == 60
        assert history[-1]['action'] == 'demo_restarted'
        assert [event['id'] for event in history] == sorted(event['id'] for event in history)


def test_firestore_receives_a_pseudonym_instead_of_the_worker_reference(tmp_path):
    firestore = FakeFirestore()
    with ExitStack() as stack:
        provider = FakeProvider()
        config = Settings(mode='prototype', db_path=tmp_path / 'm.db', audio_dir=tmp_path / 'a', firestore_mirror=True)
        app = create_app(settings=config, provider=provider, mirror_client_factory=lambda: firestore)
        stack.callback(app.state.mirror.stop)
        prototype = PrototypeRig(app, provider, stack)
        task = prototype.create()
        prototype.record(task['id'], 'instruction', 'Pack exactly twelve cartons.')
        app.state.mirror.flush()
        with app.state.store.transaction() as c:
            c.execute("INSERT INTO ec_audit(task_id,actor_id,action,version,detail,created) VALUES(?,?,?,?,?,?)",
                      (task['id'], None, 'probe', None, json.dumps({'worker_reference': 'Worker 17'}), 0))
        app.state.mirror.flush()
        mirrored = [doc['detail'] for doc in collection(firestore, 'echocheck_audit_events').values() if doc['action'] == 'probe']
        assert mirrored and mirrored[0]['worker_reference'].startswith('sha256:')
        assert 'Worker 17' not in json.dumps(mirrored)


def test_request_path_flush_never_waits_for_a_running_flush(tmp_path):
    firestore = FakeFirestore()
    with ExitStack() as stack:
        config = Settings(mode='prototype', db_path=tmp_path / 'm.db', audio_dir=tmp_path / 'a', firestore_mirror=True)
        app = create_app(settings=config, provider=ForbiddenLiveProvider(), mirror_client_factory=lambda: firestore)
        stack.callback(app.state.mirror.stop)
        mirror = app.state.mirror
        assert mirror._lock.acquire(timeout=5)
        try:
            assert mirror.flush(False) == 0
        finally:
            mirror._lock.release()
