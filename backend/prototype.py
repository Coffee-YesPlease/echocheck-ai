"""Explicit local prototype profiles. Never enabled in production mode."""
from __future__ import annotations
import time
from .auth import hash_password
from .store import uid

DEMO_SITE = 'plant-a'
PROFILES = (
    {'username':'admin','password':'admin123','persona':'admin','role':'supervisor','display_name':'Admin','display_role':'Admin'},
    {'username':'manager','password':'manager123','persona':'project_manager','role':'supervisor','display_name':'Project manager','display_role':'Project manager'},
    {'username':'supervisor','password':'super123','persona':'supervisor','role':'worker','display_name':'Supervisor','display_role':'Supervisor'},
)
DISPLAY_ROLES = {profile['persona']:profile['display_role'] for profile in PROFILES}

def bootstrap_prototype(store, reset=False):
    """Create exactly three demo profiles; reset passwords only when explicitly requested."""
    if store.settings.mode != 'prototype':
        raise ValueError('Prototype accounts can only be provisioned in prototype mode')
    with store.transaction() as c:
        for profile in PROFILES:
            row = c.execute('SELECT * FROM ec_users WHERE username=?',(profile['username'],)).fetchone()
            if row and not c.execute('SELECT 1 FROM ec_demo_profiles WHERE user_id=?',(row['id'],)).fetchone() and not reset:
                # An existing named account needs the explicit reset command first.
                continue
            if row:
                user_id = row['id']
                if reset:
                    has_tasks = c.execute('SELECT 1 FROM ec_tasks WHERE supervisor_id=? OR worker_id=? LIMIT 1',(user_id,user_id)).fetchone()
                    if has_tasks and row['role'] != profile['role']:
                        raise ValueError('Cannot remap an existing participant with tasks; use a fresh prototype database')
                    c.execute('UPDATE ec_users SET password_hash=?,role=?,site=?,display_name=?,active=1 WHERE id=?',(hash_password(profile['password'],prototype=True),profile['role'],DEMO_SITE,profile['display_name'],user_id))
                    sessions = c.execute('SELECT id FROM ec_sessions WHERE user_id=?',(user_id,)).fetchall()
                    for session in sessions:
                        c.execute('DELETE FROM ec_demo_views WHERE session_id=?',(session['id'],))
                    c.execute('DELETE FROM ec_sessions WHERE user_id=?',(user_id,))
                    from .auth import token_hash
                    c.execute('DELETE FROM ec_login_attempts WHERE key=?',('user:'+token_hash(profile['username']),))
            else:
                user_id = uid()
                c.execute('INSERT INTO ec_users VALUES(?,?,?,?,?,?,1,?)',(user_id,profile['username'],hash_password(profile['password'],prototype=True),profile['role'],DEMO_SITE,profile['display_name'],time.time()))
            c.execute('INSERT OR REPLACE INTO ec_demo_profiles VALUES(?,?)',(user_id,profile['persona']))
            if reset or not row:
                store.audit(c,None,user_id,'prototype_account_reset' if row else 'prototype_account_created',None,{'persona':profile['persona']})
        return [dict(row) for row in c.execute('SELECT u.id,u.username,u.display_name,u.role,u.site,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id ORDER BY u.username')]
