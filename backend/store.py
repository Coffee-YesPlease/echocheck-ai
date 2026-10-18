"""SQLite persistence; all safety decisions run inside BEGIN IMMEDIATE."""
from __future__ import annotations
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import time
import uuid
from .auth import hash_password


def uid():
    return uuid.uuid4().hex

def timestamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS ec_users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('supervisor','worker')), site TEXT NOT NULL, display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_devices (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_sessions (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL REFERENCES ec_users(id), device_id TEXT NOT NULL REFERENCES ec_devices(id), csrf_token TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_demo_profiles (user_id TEXT PRIMARY KEY REFERENCES ec_users(id), persona TEXT NOT NULL UNIQUE CHECK(persona IN ('admin','project_manager','supervisor')));
CREATE TABLE IF NOT EXISTS ec_demo_views (session_id TEXT PRIMARY KEY REFERENCES ec_sessions(id) ON DELETE CASCADE, target_user_id TEXT REFERENCES ec_users(id));
CREATE TABLE IF NOT EXISTS ec_login_attempts (key TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ec_login_attempts_key ON ec_login_attempts(key,created);
CREATE TABLE IF NOT EXISTS ec_tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, supervisor_id TEXT NOT NULL REFERENCES ec_users(id), worker_id TEXT NOT NULL REFERENCES ec_users(id), site TEXT NOT NULL, version INTEGER NOT NULL, instruction_revision INTEGER NOT NULL, status TEXT NOT NULL, instruction TEXT NOT NULL DEFAULT '', requirements TEXT NOT NULL DEFAULT '[]', extraction TEXT, instruction_recording_id TEXT, latest_attempt_id TEXT, worker_ok INTEGER NOT NULL DEFAULT 0, supervisor_ok INTEGER NOT NULL DEFAULT 0, correction TEXT, ack_id TEXT, active_ticket_id TEXT, evidence_version INTEGER, failure_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_instruction_versions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES ec_tasks(id), revision INTEGER NOT NULL, instruction TEXT NOT NULL, requirements TEXT NOT NULL, recording_id TEXT NOT NULL, actor_id TEXT NOT NULL, reviewed_complete INTEGER NOT NULL, created REAL NOT NULL, UNIQUE(task_id,revision));
CREATE TABLE IF NOT EXISTS ec_recording_tickets (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES ec_tasks(id), actor_id TEXT NOT NULL, session_id TEXT NOT NULL, device_id TEXT NOT NULL, purpose TEXT NOT NULL, revision INTEGER NOT NULL, version INTEGER NOT NULL, ack_id TEXT, status TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL, operation_expires REAL);
CREATE TABLE IF NOT EXISTS ec_consents (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL, device_id TEXT NOT NULL, session_id TEXT NOT NULL, consent_version TEXT NOT NULL, accepted REAL NOT NULL, policy_id TEXT, policy_json TEXT);
CREATE TABLE IF NOT EXISTS ec_recordings (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ticket_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL, device_id TEXT NOT NULL, purpose TEXT NOT NULL, revision INTEGER NOT NULL, ack_id TEXT, path TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, retained_until REAL NOT NULL, created REAL NOT NULL, UNIQUE(task_id,sha256));
CREATE TABLE IF NOT EXISTS ec_transcripts (id TEXT PRIMARY KEY, recording_id TEXT NOT NULL UNIQUE, transcript TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_attempts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, recording_id TEXT, ack_id TEXT, transcript TEXT NOT NULL, status TEXT NOT NULL, verification TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_acknowledgments (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, attempt_id TEXT NOT NULL, actor_id TEXT NOT NULL, correction TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, attempt_id TEXT NOT NULL, evidence_version INTEGER NOT NULL, actor_id TEXT NOT NULL, role TEXT NOT NULL, approved INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, actor_id TEXT, action TEXT NOT NULL, version INTEGER, detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_capture_speakers (ticket_id TEXT PRIMARY KEY REFERENCES ec_recording_tickets(id), operator_id TEXT NOT NULL REFERENCES ec_users(id), speaker_kind TEXT NOT NULL CHECK(speaker_kind IN ('project_manager','shop_floor_worker')), worker_reference TEXT, worker_consent_confirmed INTEGER NOT NULL CHECK(worker_consent_confirmed IN (0,1)), created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_task_workers (task_id TEXT PRIMARY KEY REFERENCES ec_tasks(id), worker_reference TEXT NOT NULL, assigned_by TEXT NOT NULL REFERENCES ec_users(id), created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_demo_runs (id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, catalog_id TEXT NOT NULL, scenario_json TEXT NOT NULL, project_manager_id TEXT NOT NULL REFERENCES ec_users(id), supervisor_id TEXT NOT NULL REFERENCES ec_users(id), site TEXT NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL, latest_attempt_id TEXT, ack_id TEXT, pm_ok INTEGER NOT NULL DEFAULT 0, supervisor_ok INTEGER NOT NULL DEFAULT 0, correction_text TEXT, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(scenario_id,site));
CREATE TABLE IF NOT EXISTS ec_demo_attempts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ec_demo_runs(id), generation INTEGER NOT NULL, variant TEXT NOT NULL, transcript TEXT NOT NULL, verification TEXT NOT NULL, ack_id TEXT, asset_path TEXT NOT NULL, asset_sha256 TEXT NOT NULL, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_demo_acks (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ec_demo_runs(id), generation INTEGER NOT NULL, attempt_id TEXT NOT NULL, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL, correction TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_demo_approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ec_demo_runs(id), generation INTEGER NOT NULL, attempt_id TEXT NOT NULL, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('project_manager','supervisor')), approved INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_demo_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES ec_demo_runs(id), version INTEGER NOT NULL, action TEXT NOT NULL, actor_id TEXT, principal_id TEXT, detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ec_purges (recording_id TEXT PRIMARY KEY, purged REAL NOT NULL);
'''

class Store:
    def __init__(self, settings):
        self.settings = settings
        settings.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.audio_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(settings.audio_dir, 0o700)
        with closing(self.connect()) as c:
            c.executescript(SCHEMA)
            # Additive v0.2 hardening only; the legacy tasks table is never modified.
            for table, column, definition in (
                ('ec_tasks','failure_reason','TEXT'),
                ('ec_recording_tickets','operation_expires','REAL'),
                ('ec_consents','policy_id','TEXT'),
                ('ec_consents','policy_json','TEXT'),
            ):
                if column not in {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        os.chmod(settings.db_path, 0o600)

    def connect(self):
        c = sqlite3.connect(str(self.settings.db_path), timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=15000')
        return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def create_user(self, username, password, role, site, display_name):
        username = str(username).strip().lower()
        site = str(site).strip()
        display_name = str(display_name).strip()
        if not re.fullmatch(r'[a-z0-9][a-z0-9._@+-]{2,79}', username):
            raise ValueError('Username must contain 3 to 80 simple letters, numbers, or ._@+-')
        if role not in ('supervisor','worker') or not site or len(site) > 100 or not display_name or len(display_name) > 100:
            raise ValueError('Valid role, site, and display name are required')
        password_hash = hash_password(password)
        user_id = uid()
        with self.transaction() as c:
            c.execute('INSERT INTO ec_users VALUES (?,?,?,?,?,?,1,?)', (user_id,username,password_hash,role,site,display_name,time.time()))
            self.audit(c, None, user_id, 'user_provisioned', None, {'role':role,'site':site})
        return {'id':user_id,'username':username,'role':role,'site':site,'display_name':display_name}

    def audit(self, c, task_id, actor_id, action, version=None, detail=None):
        c.execute('INSERT INTO ec_audit(task_id,actor_id,action,version,detail,created) VALUES(?,?,?,?,?,?)', (task_id,actor_id,action,version,encode(detail or {}),time.time()))

    def purge_expired(self):
        count = 0
        with self.transaction() as c:
            rows = c.execute('SELECT r.* FROM ec_recordings r LEFT JOIN ec_purges p ON p.recording_id=r.id WHERE r.retained_until<=? AND p.recording_id IS NULL',(time.time(),)).fetchall()
            for row in rows:
                path = self.settings.audio_dir / row['path']
                if path.exists():
                    path.unlink()
                c.execute('INSERT INTO ec_purges VALUES(?,?)',(row['id'],time.time()))
                self.audit(c,row['task_id'],None,'recording_purged',None,{'recording_id':row['id']})
                count += 1
            # A process can stop after a private file is written and before the
            # database insertion commits. Keep a grace period beyond the entire
            # processing lease so live saves are never swept.
            tracked = {row['path'] for row in c.execute('SELECT path FROM ec_recordings')}
            orphan_cutoff = time.time() - self.settings.processing_seconds - 60
            for path in self.settings.audio_dir.glob('*.audio'):
                if path.name in tracked or not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= orphan_cutoff:
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
                self.audit(c,None,None,'orphan_recording_purged',None,{'filename':path.name})
                count += 1
        return count
