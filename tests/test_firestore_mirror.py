"""Firestore mirror: copies committed history, idempotent, and never blocks workflow."""
from __future__ import annotations

from contextlib import ExitStack
import json

import pytest

from backend.config import Settings
from backend.main import create_app
from test_demo import DemoRig, ForbiddenLiveProvider, catalog


class FakeFirestore:
    def __init__(self):
        self.documents, self.fail, self.commits = {}, False, 0

    def collection(self, name):
        return FakeCollection(self, name)

    def batch(self):
        return FakeBatch(self)


class FakeCollection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, document_id):
        return (self.name, document_id)


class FakeBatch:
    def __init__(self, db):
        self.db, self.pending = db, []

    def set(self, reference, document):
        self.pending.append((reference, document))

    def commit(self):
        if self.db.fail:
            raise ConnectionError('firestore unavailable')
        self.db.commits += 1
        for reference, document in self.pending:
            self.db.documents[reference] = document


@pytest.fixture
def mirrored(tmp_path):
    firestore = FakeFirestore()
    with ExitStack() as stack:
        settings = Settings(mode='prototype', db_path=tmp_path / 'm.db', audio_dir=tmp_path / 'a', firestore_mirror=True)
        app = create_app(settings=settings, provider=ForbiddenLiveProvider(), mirror_client_factory=lambda: firestore)
        stack.callback(app.state.mirror.stop)
        yield DemoRig(app, app.state.provider, stack), firestore


def collection(firestore, name):
    return {key[1]: value for key, value in firestore.documents.items() if key[0] == name}


def test_committed_demo_history_reaches_firestore_after_each_action(mirrored):
    demo, firestore = mirrored
    demo.app.state.mirror.flush()
    assert len(collection(firestore, 'echocheck_demo_runs')) == 7
    preloaded = [doc for doc in collection(firestore, 'echocheck_demo_events').values() if doc['action'] == 'demo_preloaded']
    assert len(preloaded) == 7
    assert any(doc['action'] == 'login' for doc in collection(firestore, 'echocheck_audit_events').values())
    demo.switch('supervisor')
    run = demo.runs()[0]
    demo.choose(run['id'], 'incorrect')  # the POST itself flushes before responding
    events = collection(firestore, 'echocheck_demo_events').values()
    assert any(doc['action'] == 'demo_response_selected' and doc['run_id'] == run['id'] for doc in events)
    assert collection(firestore, 'echocheck_demo_runs')[run['id']]['status'] == 'awaiting_supervisor_ack'
    assert demo.client.get('/api/health').json()['firestore_mirror']['healthy'] is True


def test_outage_never_blocks_workflow_and_retries_without_duplicates(mirrored):
    demo, firestore = mirrored
    demo.app.state.mirror.flush()
    firestore.fail = True
    demo.switch('supervisor')
    run = demo.runs()[0]
    demo.choose(run['id'], 'correct')
    assert demo.client.get('/api/health').json()['firestore_mirror']['healthy'] is False
    before = dict(firestore.documents)
    firestore.fail = False
    demo.app.state.mirror.flush()
    demo.app.state.mirror.flush()
    with demo.app.state.store.transaction() as c:
        audit_rows = c.execute('SELECT COUNT(*) FROM ec_audit').fetchone()[0]
        demo_rows = c.execute('SELECT COUNT(*) FROM ec_demo_events').fetchone()[0]
    assert len(collection(firestore, 'echocheck_audit_events')) == audit_rows
    assert len(collection(firestore, 'echocheck_demo_events')) == demo_rows
    assert set(before) <= set(firestore.documents)


def test_mirror_carries_no_worker_words_or_audio(mirrored):
    demo, firestore = mirrored
    demo.switch('supervisor')
    for run in demo.runs()[:3]:
        demo.choose(run['id'], 'incorrect')
    demo.app.state.mirror.flush()
    dumped = json.dumps([doc for doc in firestore.documents.values()], default=str)
    for scenario in catalog()['scenarios']:
        for variant in scenario['variants'].values():
            assert variant['transcript'] not in dumped
    assert 'transcript' not in dumped and '.wav' not in dumped


def test_mirror_is_off_unless_enabled(tmp_path):
    app = create_app(settings=Settings(mode='prototype', db_path=tmp_path / 'o.db', audio_dir=tmp_path / 'a'), provider=ForbiddenLiveProvider())
    assert app.state.mirror is None
