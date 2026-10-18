"""Process interruption and expired-session recovery, without provider network calls."""
from concurrent.futures import ThreadPoolExecutor
import threading

from test_workflow import ALIGNED, INTENDED, rig, rig_factory


def test_expired_consuming_operation_recovers_and_late_result_cannot_replace_new_ticket(rig):
    task = rig.create()
    rig.publish(task['id'])
    old_ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    rig.provider.started, rig.provider.proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(rig.upload, task['id'], rig.worker, old_ticket, rig.provider.speech(ALIGNED))
        try:
            assert rig.provider.started.wait(5)
            with rig.app.state.store.transaction() as connection:
                connection.execute('UPDATE ec_recording_tickets SET operation_expires=0 WHERE id=?', (old_ticket['ticket_id'],))
            recovered = rig.task(task['id'])
            assert recovered['status'] == 'verification_failed'
            assert recovered['active_ticket'] is None
            assert not recovered['worker_ok'] and not recovered['supervisor_ok']
            replacement = rig.ticket(task['id'], rig.worker, 'retelling')
            assert replacement.status_code == 200, replacement.text
            replacement_id = replacement.json()['ticket_id']
            replacement_version = replacement.json()['version']
        finally:
            rig.provider.proceed.set()
        response = pending.result(timeout=5)
    assert response.status_code == 409, response.text
    current = rig.task(task['id'])
    assert current['status'] == 'recording_worker'
    assert current['version'] == replacement_version
    assert current['active_ticket']['id'] == replacement_id
    assert current['latest_attempt'] is None


def test_expired_issued_ticket_is_exposed_then_recoverable(rig):
    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    current = rig.task(task['id'])
    assert current['active_ticket']['id'] == ticket['ticket_id']
    assert current['active_ticket']['purpose'] == 'instruction'
    assert current['active_ticket']['status'] == 'issued'
    with rig.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_recording_tickets SET expires=0 WHERE id=?', (ticket['ticket_id'],))
    recovered = rig.task(task['id'])
    assert recovered['status'] == 'draft'
    assert recovered['active_ticket'] is None
    assert recovered['version'] > current['version']
    assert rig.ticket(task['id'], rig.supervisor, 'instruction').status_code == 200


def test_session_expiry_during_provider_call_persists_retryable_failure(rig):
    task = rig.create()
    rig.publish(task['id'])
    ticket = rig.ticket(task['id'], rig.worker, 'retelling').json()
    rig.provider.started, rig.provider.proceed = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(rig.upload, task['id'], rig.worker, ticket, rig.provider.speech(ALIGNED))
        try:
            assert rig.provider.started.wait(5)
            with rig.app.state.store.transaction() as connection:
                connection.execute('UPDATE ec_sessions SET expires=0 WHERE user_id=?', (rig.worker_id,))
        finally:
            rig.provider.proceed.set()
        response = pending.result(timeout=5)
    assert response.status_code == 401, response.text
    current = rig.task(task['id'])
    assert current['status'] == 'verification_failed'
    assert current['active_ticket'] is None
    assert not current['worker_ok'] and not current['supervisor_ok']
    assert rig.worker.post('/api/login', json={'username':'worker','password':'Review-only-password-927!'}).status_code == 200
    assert rig.ticket(task['id'], rig.worker, 'retelling').status_code == 200


def test_cancel_during_private_save_removes_background_thread_audio(rig, monkeypatch):
    """Cancellation after a disk write must not lose ownership of its filename."""
    import asyncio
    import httpx
    import backend.main as main_module

    task = rig.create()
    ticket = rig.ticket(task['id'], rig.supervisor, 'instruction').json()
    payload = rig.provider.speech(INTENDED)
    csrf = rig.supervisor.get('/api/me').json()['csrf_token']
    saved, release_save, returned = threading.Event(), threading.Event(), threading.Event()
    original_save = main_module.save_private_audio

    def delayed_save(directory, data):
        filename = original_save(directory, data)
        saved.set()
        if not release_save.wait(5):
            raise RuntimeError('Controlled file-save pause timed out')
        returned.set()
        return filename

    monkeypatch.setattr(main_module, 'save_private_audio', delayed_save)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rig.app),
                                     base_url='https://testserver', cookies=rig.supervisor.cookies,
                                     headers={'Origin':'https://testserver'}) as client:
            pending = asyncio.create_task(client.post(
                f"/api/tasks/{task['id']}/recordings/{ticket['ticket_id']}", content=payload,
                headers={'Content-Type':'audio/wav','X-CSRF-Token':csrf,
                         'X-Expected-Version':str(ticket['version'])}))
            try:
                assert await asyncio.to_thread(saved.wait, 5)
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
            finally:
                release_save.set()
            assert await asyncio.to_thread(returned.wait, 5)
            # Await the thread's completion callback, which owns cancellation cleanup.
            for _ in range(100):
                if not list(rig.app.state.settings.audio_dir.iterdir()):
                    break
                await asyncio.sleep(0.01)
            assert not list(rig.app.state.settings.audio_dir.iterdir())

    asyncio.run(scenario())
    current = rig.task(task['id'])
    assert current['status'] == 'draft'
    assert current['release_eligible'] is False
    assert current['active_ticket'] is None
    with rig.app.state.store.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ec_recordings').fetchone()[0] == 0
    assert rig.app.state.store.purge_expired() == 0


def test_purge_removes_only_old_untracked_audio(rig):
    """Crash leftovers expire, while a live save and retained evidence are preserved."""
    import os
    import time

    task = rig.create()
    rig.record(task['id'], rig.supervisor, 'instruction', INTENDED)
    directory = rig.app.state.settings.audio_dir
    old_orphan = directory / 'old-orphan.audio'
    fresh_orphan = directory / 'fresh-in-progress.audio'
    old_orphan.write_bytes(b'crash-leftover')
    fresh_orphan.write_bytes(b'current-save')
    cutoff_age = rig.app.state.settings.processing_seconds + 120
    os.utime(old_orphan, (time.time()-cutoff_age, time.time()-cutoff_age))
    with rig.app.state.store.transaction() as connection:
        tracked = connection.execute('SELECT path FROM ec_recordings').fetchone()['path']
    # Even a very old mtime does not purge a tracked, unexpired recording.
    os.utime(directory / tracked, (time.time()-cutoff_age, time.time()-cutoff_age))
    assert rig.app.state.store.purge_expired() == 1
    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    assert (directory / tracked).exists()
    with rig.app.state.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ec_audit WHERE action='orphan_recording_purged'").fetchone()[0] == 1
