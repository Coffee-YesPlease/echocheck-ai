"""Durable Firestore copy of committed workflow history, written through the Firebase Admin SDK.

SQLite stays the transactional source of truth for every safety decision. This
mirror only reads rows that are already committed and copies them to Firestore
so history survives Cloud Run instance replacement. It never blocks, rolls back
or authorizes a workflow step, and it copies no audio or transcripts.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import threading
import time

from .store import uid
import hashlib

log = logging.getLogger('echocheck.mirror')

# Append-only tables with increasing integer ids, mirrored as event documents.
EVENT_TABLES = (
    ('ec_audit', 'echocheck_audit_events'),
    ('ec_demo_events', 'echocheck_demo_events'),
)
BATCH_ROWS = 100  # two tables plus their snapshots stay under Firestore's 500-write batch limit
# Free-text identifiers typed for a worker stay in SQLite; Firestore only gets a stable pseudonym.
PSEUDONYMISED_KEYS = ('worker_reference',)


def _redact(detail):
    if not isinstance(detail, dict):
        return detail
    redacted = dict(detail)
    for key in PSEUDONYMISED_KEYS:
        if isinstance(redacted.get(key), str):
            redacted[key] = 'sha256:' + hashlib.sha256(redacted[key].casefold().encode('utf-8')).hexdigest()[:16]
    return redacted


def firebase_client(project):
    """Firestore client from the Firebase Admin SDK using Application Default Credentials."""
    import firebase_admin
    from firebase_admin import firestore
    options = {'projectId': project} if project else None
    try:
        app = firebase_admin.get_app('echocheck')
    except ValueError:
        app = firebase_admin.initialize_app(options=options, name='echocheck')
    return firestore.client(app)


def _when(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class FirestoreMirror:
    def __init__(self, store, client_factory=None, project=''):
        self.store = store
        self.project = project
        self._client_factory = client_factory or (lambda: firebase_client(project))
        self._client = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_success = None
        self.healthy = None
        self.database_id = self._database_id()

    def _database_id(self):
        # Row ids restart in a fresh database, so documents are namespaced by database.
        with self.store.transaction() as c:
            row = c.execute("SELECT value FROM ec_mirror_state WHERE key='database_id'").fetchone()
            if row:
                return row['value']
            value = uid()
            c.execute("INSERT INTO ec_mirror_state VALUES('database_id',?)", (value,))
            return value

    def _cursor(self, c, table):
        row = c.execute('SELECT value FROM ec_mirror_state WHERE key=?', ('cursor:' + table,)).fetchone()
        return int(row['value']) if row else 0

    def _pending(self):
        with closing(self.store.connect()) as c:
            writes, cursors, tasks, runs = [], {}, set(), set()
            for table, collection in EVENT_TABLES:
                rows = c.execute(f'SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT ?',
                                 (self._cursor(c, table), BATCH_ROWS)).fetchall()
                for row in rows:
                    row = dict(row)
                    document = {'database_id': self.database_id, 'source_table': table, 'row_id': row['id'],
                                'action': row['action'], 'version': row['version'], 'actor_id': row['actor_id'],
                                'detail': _redact(json.loads(row['detail'] or '{}')), 'created': _when(row['created']),
                                'mode': self.store.settings.mode}
                    if table == 'ec_audit':
                        document['task_id'] = row['task_id']
                        if row['task_id']:
                            tasks.add(row['task_id'])
                    else:
                        document['run_id'] = row['run_id']
                        document['principal_id'] = row['principal_id']
                        runs.add(row['run_id'])
                    writes.append((collection, f"{self.database_id}-{row['id']}", document))
                    cursors[table] = row['id']
            for task_id in tasks:
                task = c.execute('SELECT id,title,site,status,version,instruction_revision,worker_ok,supervisor_ok,created,updated FROM ec_tasks WHERE id=?', (task_id,)).fetchone()
                if task:
                    task = dict(task)
                    writes.append(('echocheck_tasks', task_id, {
                        'database_id': self.database_id, 'title': task['title'], 'site': task['site'],
                        'status': task['status'], 'version': task['version'],
                        'instruction_revision': task['instruction_revision'],
                        'project_manager_confirmed': bool(task['supervisor_ok']),
                        'supervisor_confirmed': bool(task['worker_ok']),
                        'created': _when(task['created']), 'updated': _when(task['updated'])}))
            for run_id in runs:
                run = c.execute('SELECT id,scenario_id,site,status,version,generation,pm_ok,supervisor_ok,updated FROM ec_demo_runs WHERE id=?', (run_id,)).fetchone()
                if run:
                    run = dict(run)
                    writes.append(('echocheck_demo_runs', run_id, {
                        'database_id': self.database_id, 'scenario_id': run['scenario_id'], 'site': run['site'],
                        'status': run['status'], 'version': run['version'], 'generation': run['generation'],
                        'project_manager_confirmed': bool(run['pm_ok']), 'supervisor_confirmed': bool(run['supervisor_ok']),
                        'source': 'scripted_demo', 'updated': _when(run['updated'])}))
            return writes, cursors

    def flush(self, wait=True):
        """Copy newly committed rows. Returns the number of documents written; never raises."""
        if not (self._lock.acquire(timeout=10) if wait else self._lock.acquire(blocking=False)):
            return 0
        try:
            total = 0
            for _ in range(20):
                writes, cursors = self._pending()
                if not writes:
                    break
                if self._client is None:
                    self._client = self._client_factory()
                batch = self._client.batch()
                for collection, document_id, document in writes:
                    batch.set(self._client.collection(collection).document(document_id), document)
                batch.commit()
                # Advance only after Firestore accepted the batch; deterministic ids make retries idempotent.
                with self.store.transaction() as c:
                    for table, last_id in cursors.items():
                        c.execute('INSERT OR REPLACE INTO ec_mirror_state VALUES(?,?)', ('cursor:' + table, str(last_id)))
                total += len(writes)
            self.healthy, self.last_success = True, time.time()
            return total
        except Exception as exc:
            self.healthy = False
            log.warning('Firestore mirror flush failed: %s', type(exc).__name__)
            return 0
        finally:
            self._lock.release()

    def start(self, interval=15):
        def loop():
            # First flush runs in the background so startup never waits on Firestore.
            while True:
                self.flush()
                if self._stop.wait(interval):
                    break
        threading.Thread(target=loop, name='firestore-mirror', daemon=True).start()

    def stop(self):
        self._stop.set()

    def status(self):
        return {'enabled': True, 'healthy': self.healthy,
                'last_success': _when(self.last_success).isoformat() if self.last_success else None}
