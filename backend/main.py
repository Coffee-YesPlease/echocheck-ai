"""Authenticated, versioned voice verification. AI results never release work alone."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool

from .auth import DUMMY_PASSWORD_HASH, token, token_hash, verify_password
from .audio import ALLOWED_MIME, MAX_AUDIO_BYTES, InvalidAudio, save_private_audio, validate_audio
from .config import ROOT, Settings
from .gemini_adapter import GeminiProvider, InvalidProviderResponse, validate_evaluation, validate_requirements
from .verification import validate_extraction
from .store import Store, encode, timestamp, uid
from .prototype import bootstrap_prototype, DISPLAY_ROLES

SESSION_COOKIE = 'ec_session'
DEVICE_COOKIE = 'ec_device'

class ConsentChanged(InvalidAudio):
    pass

class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Login(Body):
    username: str = Field(min_length=1,max_length=80)
    password: str = Field(min_length=1,max_length=1024)

class ViewAs(Body):
    user_id: Optional[str] = Field(default=None,max_length=80)

class NewTask(Body):
    title: str = Field(min_length=3,max_length=120)
    worker_id: str = Field(min_length=1,max_length=80)

class Versioned(Body):
    expected_version: StrictInt = Field(ge=1)

class TicketBody(Versioned):
    purpose: Literal['instruction','retelling']
    consent_version: str = Field(min_length=1,max_length=100)
    consent_accepted: StrictBool
    consent_policy_id: str = Field(min_length=64,max_length=64)
    worker_reference: Optional[str] = Field(default=None,max_length=120)
    worker_consent_confirmed: Optional[StrictBool] = None

class Publish(Versioned):
    instruction: str = Field(min_length=8,max_length=12000)
    requirements: list = Field(min_length=1,max_length=100)
    reviewed_complete: StrictBool

class Acknowledge(Versioned):
    attempt_id: str = Field(min_length=1,max_length=80)
    correction: str = Field(min_length=3,max_length=6000)

class EvidenceAction(Versioned):
    attempt_id: str = Field(min_length=1,max_length=80)
    instruction_revision: StrictInt = Field(ge=1)

class Approve(EvidenceAction):
    approved: StrictBool = True


def create_app(settings=None, provider=None, mirror_client_factory=None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    prototype_profiles = bootstrap_prototype(store) if settings.mode == 'prototype' else []
    persona_by_id = {profile['id']:profile['persona'] for profile in prototype_profiles}
    provider = provider if provider is not None else GeminiProvider()
    app = FastAPI(title='EchoCheck AI',version='0.6.0', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.provider = provider

    @app.middleware('http')
    async def safety_headers(request, call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            if not settings.origin_allowed(request.headers.get('origin')):
                response = JSONResponse({'detail':'Open EchoCheck from its configured address before continuing.'},status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Permissions-Policy'] = 'microphone=(self), camera=(), geolocation=()'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        if settings.secure_cookies:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        mirror = getattr(app.state,'mirror',None)
        if mirror is not None and request.method == 'POST' and request.url.path.startswith('/api/'):
            # Copy committed history before the request ends; Cloud Run may throttle CPU afterwards.
            try:
                await asyncio.wait_for(asyncio.to_thread(mirror.flush),timeout=4)
            except asyncio.TimeoutError:
                pass
        return response

    def session(request, c, mutation=False, allow_admin=False):
        raw = request.cookies.get(SESSION_COOKIE, '')
        if not raw:
            raise HTTPException(401,'Sign in to continue.')
        row = c.execute('SELECT s.id AS session_id,s.csrf_token,s.device_id,s.expires,u.id,u.username,u.role,u.site,u.display_name,u.active FROM ec_sessions s JOIN ec_users u ON u.id=s.user_id WHERE s.token_hash=?',(token_hash(raw),)).fetchone()
        if not row or not row['active'] or row['expires'] <= time.time():
            raise HTTPException(401,'Your session has expired. Sign in again.')
        # Both opaque cookies are server-issued; session cannot be transplanted to another browser identity.
        device = request.cookies.get(DEVICE_COOKIE,'')
        device_row = c.execute('SELECT id FROM ec_devices WHERE token_hash=?',(token_hash(device),)).fetchone() if device else None
        if not device_row or device_row['id'] != row['device_id']:
            raise HTTPException(401,'Sign in again on this device.')
        if mutation and not hmac.compare_digest(request.headers.get('x-csrf-token',''),row['csrf_token']):
            raise HTTPException(403,'Refresh your session before making this change.')
        actor = dict(row)
        profile = c.execute('SELECT persona FROM ec_demo_profiles WHERE user_id=?',(actor['id'],)).fetchone()
        if (profile and settings.mode != 'prototype') or (settings.mode == 'prototype' and not profile):
            raise HTTPException(401,'This account is not available in the current application mode.')
        actor.update(principal_id=actor['id'],principal_site=actor['site'],principal_persona=profile['persona'] if profile else None,
                     persona=profile['persona'] if profile else None,is_admin_base=False,principal_admin_id=None)
        if settings.mode == 'prototype' and actor['principal_persona'] == 'admin':
            actor['principal_admin_id'] = actor['principal_id']
            selected = c.execute('SELECT u.*,p.persona FROM ec_demo_views v JOIN ec_users u ON u.id=v.target_user_id JOIN ec_demo_profiles p ON p.user_id=u.id WHERE v.session_id=? AND u.active=1 AND u.site=?',(actor['session_id'],actor['site'])).fetchone()
            if selected:
                for key in ('id','username','role','site','display_name','active','persona'):
                    actor[key] = selected[key]
            actor['is_admin_base'] = actor['id'] == actor['principal_id']
            if mutation and actor['is_admin_base'] and not allow_admin:
                raise HTTPException(403,'Choose the Project manager or Supervisor perspective to try this action.')
        return actor

    def user_view(actor):
        result = {k:actor[k] for k in ('id','username','role','site','display_name')}
        if settings.mode == 'prototype':
            persona = actor.get('persona') if isinstance(actor,dict) else None
            persona = persona or persona_by_id.get(actor['id'])
            result.update(workflow_role=actor['role'],prototype=True)
            if persona:
                result.update(role=persona,display_role=DISPLAY_ROLES[persona])
        return result

    def principal_admin(actor):
        return settings.mode == 'prototype' and actor.get('principal_persona') == 'admin'

    def demo_users(c):
        return [user_view(dict(row)) for row in c.execute("SELECT u.id,u.username,u.display_name,u.role,u.site,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id WHERE u.active=1 ORDER BY CASE p.persona WHEN 'admin' THEN 0 WHEN 'project_manager' THEN 1 ELSE 2 END")]

    def identity_view(c,actor):
        result = {'user':user_view(actor),'csrf_token':actor['csrf_token'],'device_id':actor['device_id']}
        if settings.mode == 'prototype':
            admin = c.execute('SELECT u.*,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id WHERE u.id=?',(actor['principal_id'],)).fetchone() if principal_admin(actor) else None
            result.update(prototype=True,admin_user=user_view(dict(admin)) if admin else None,view_as_users=demo_users(c) if admin else [])
        return result

    def audit_details(actor,detail=None):
        result = dict(detail or {})
        if actor and actor.get('principal_admin_id'):
            result.update(principal_admin_id=actor['principal_admin_id'],effective_user_id=actor['id'],prototype=True)
        return result

    def assigned(c, task_id, actor):
        row = c.execute('SELECT * FROM ec_tasks WHERE id=?',(task_id,)).fetchone()
        if not row or actor['site'] != row['site'] or (actor['id'] not in (row['supervisor_id'],row['worker_id']) and not actor.get('is_admin_base')):
            raise HTTPException(404,'Task not found.')
        # Recheck current membership and active role on every decision.
        supervisor = c.execute('SELECT active,role,site FROM ec_users WHERE id=?',(row['supervisor_id'],)).fetchone()
        worker = c.execute('SELECT active,role,site FROM ec_users WHERE id=?',(row['worker_id'],)).fetchone()
        if not supervisor or not worker or not supervisor['active'] or not worker['active'] or supervisor['role'] != 'supervisor' or worker['role'] != 'worker' or supervisor['site'] != row['site'] or worker['site'] != row['site']:
            raise HTTPException(409,'Participant access changed. Contact your site administrator.')
        return dict(row)

    def role(actor, required):
        if actor['role'] != required:
            label = {'supervisor':'Project manager','worker':'Supervisor'}[required] if settings.mode == 'prototype' else required
            raise HTTPException(403,f'Only the assigned {label} can do this.')

    def version(task, expected):
        if task['version'] != expected:
            raise HTTPException(409,'This task changed on another device. Refresh before continuing.')

    def change(c, task, actor, action, values, detail=None):
        values = dict(values)
        values.update(version=task['version']+1,updated=time.time())
        assignments = ','.join(f'{key}=?' for key in values)
        c.execute(f'UPDATE ec_tasks SET {assignments} WHERE id=? AND version=?', (*values.values(),task['id'],task['version']))
        if c.execute('SELECT changes()').fetchone()[0] != 1:
            raise HTTPException(409,'This task changed. Refresh before continuing.')
        store.audit(c,task['id'],actor['id'] if actor else None,action,values['version'],audit_details(actor,detail))
        return values['version']

    def blocked_values(status='verification_failed'):
        return {'status':status,'worker_ok':0,'supervisor_ok':0,'evidence_version':None,'latest_attempt_id':None}

    def disclosure():
        days = settings.recording_retention_seconds / 86400
        if settings.mode == 'prototype':
            notice = (
                'Project manager: I agree to record my instructions. Supervisor: I operate this device '
                'while the named shop-floor worker explains their understanding. Before recording, '
                'I confirm the worker has agreed to have their voice recorded and sent to Google Gemini '
                'for transcription and comparison with the task instructions. The worker does not need '
                'a device or login. The Project manager and Supervisor may play the recording. '
            )
        else:
            notice = (
                'I agree to record my voice for this task and send the recording to Google Gemini '
                'for transcription and instruction verification. The assigned supervisor and worker '
                'may play this recording. '
            )
        notice += (
            f'Audio access expires after {days:g} days. '
            'Your administrator deletes expired audio under the site retention policy. '
            'Transcripts, consent, approvals, and audit records are retained. Recording is optional; '
            'without recorded verification, EchoCheck cannot release this task to work.'
        )
        policy = {'notice':notice,'provider':'Google Gemini','model':getattr(provider,'model',None),
                  'retention_seconds':settings.recording_retention_seconds,'max_seconds':120,
                  'max_bytes':MAX_AUDIO_BYTES,'consent_version':settings.consent_version,'mode':settings.mode}
        policy['policy_id'] = hashlib.sha256(encode(policy).encode('utf-8')).hexdigest()
        return policy

    def speaker_view(c,ticket_id):
        row = c.execute('SELECT * FROM ec_capture_speakers WHERE ticket_id=?',(ticket_id,)).fetchone()
        if not row:
            return None
        return {'kind':row['speaker_kind'],'reference':row['worker_reference'],
                'operator_id':row['operator_id'],'worker_consent_confirmed':bool(row['worker_consent_confirmed'])}

    def current_worker_speaker(c,task,ticket,require_frozen=True):
        if not task or not ticket:
            raise HTTPException(409,'Current named worker recording evidence is required.')
        speaker = speaker_view(c,ticket['id'])
        worker = c.execute('SELECT * FROM ec_task_workers WHERE task_id=?',(task['id'],)).fetchone()
        if not speaker or speaker['kind'] != 'shop_floor_worker' or speaker['operator_id'] != task['worker_id'] or speaker['operator_id'] != ticket['actor_id'] or speaker['worker_consent_confirmed'] is not True or not speaker['reference'] or (require_frozen and not worker) or (worker and speaker['reference'] != worker['worker_reference']):
            raise HTTPException(409,'The Supervisor must record the named worker with their consent before this task can be approved.')
        return speaker

    def legacy_instruction_policy():
        # v0.3 already obtained the issuer's own-voice consent for the same
        # processing and retention. That scope remains valid for instructions
        # only; it cannot attest a shop-floor worker's voice or consent.
        policy = disclosure()
        days = settings.recording_retention_seconds / 86400
        policy['notice'] = (
            'I agree to record my voice for this task and send the recording to Google Gemini '
            'for transcription and instruction verification. The assigned Project manager and Supervisor '
            f'may play this recording. Audio access expires after {days:g} days. '
            'Your administrator deletes expired audio under the site retention policy. '
            'Transcripts, consent, approvals, and audit records are retained. Recording is optional; '
            'without recorded verification, EchoCheck cannot release this task to work.'
        )
        policy.pop('policy_id')
        policy['policy_id'] = hashlib.sha256(encode(policy).encode('utf-8')).hexdigest()
        return policy

    def consent_current(c,ticket):
        consent = c.execute('SELECT * FROM ec_consents WHERE ticket_id=?',(ticket['id'],)).fetchone()
        policy = disclosure()
        matching_policy = consent and consent['policy_id'] == policy['policy_id'] and consent['policy_json'] == encode(policy)
        if consent and not matching_policy and settings.mode == 'prototype' and ticket['purpose'] == 'instruction':
            legacy = legacy_instruction_policy()
            matching_policy = consent['policy_id'] == legacy['policy_id'] and consent['policy_json'] == encode(legacy)
        if not consent or consent['consent_version'] != settings.consent_version or not matching_policy or consent['actor_id'] != ticket['actor_id'] or consent['device_id'] != ticket['device_id'] or consent['session_id'] != ticket['session_id']:
            raise ConsentChanged('Recording consent changed. Review the current notice and record again.')
        if settings.mode == 'prototype' and ticket['purpose'] == 'retelling':
            task = c.execute('SELECT * FROM ec_tasks WHERE id=?',(ticket['task_id'],)).fetchone()
            try:
                current_worker_speaker(c,task,ticket,require_frozen=False)
            except HTTPException:
                raise ConsentChanged('The Supervisor must confirm the named worker and their consent before recording.') from None
        return consent

    def expire_active(c,task):
        if not task['active_ticket_id']:
            return task
        ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(task['active_ticket_id'],)).fetchone()
        if not ticket:
            expired = True
        elif ticket['status'] == 'issued':
            expired = ticket['expires'] <= time.time()
        elif ticket['status'] == 'consuming':
            expired = ticket['operation_expires'] is None or ticket['operation_expires'] <= time.time()
        else:
            expired = True
        if expired:
            if ticket and ticket['status'] in ('issued','consuming'):
                c.execute("UPDATE ec_recording_tickets SET status='expired' WHERE id=?",(ticket['id'],))
            values = blocked_values('draft' if ticket and ticket['purpose'] == 'instruction' else 'verification_failed')
            values.update(active_ticket_id=None,failure_reason='Recording or processing expired. Review consent and make a fresh recording.')
            change(c,task,None,'recording_expired',values,{'ticket_id':task['active_ticket_id']})
            return dict(c.execute('SELECT * FROM ec_tasks WHERE id=?',(task['id'],)).fetchone())
        return task

    def cancel_session_operations(c,session_id,actor):
        rows = c.execute("SELECT t.* FROM ec_tasks t JOIN ec_recording_tickets r ON r.id=t.active_ticket_id WHERE r.session_id=? AND r.status IN ('issued','consuming')",(session_id,)).fetchall()
        for row in rows:
            task = dict(row)
            ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(task['active_ticket_id'],)).fetchone()
            c.execute("UPDATE ec_recording_tickets SET status='cancelled' WHERE id=?",(ticket['id'],))
            values = blocked_values('draft' if ticket['purpose'] == 'instruction' else 'verification_failed')
            values.update(active_ticket_id=None,failure_reason='Session ended during recording. Review consent and make a fresh recording.')
            audit_actor = {'id':ticket['actor_id']}
            principal = c.execute('SELECT s.user_id FROM ec_sessions s JOIN ec_demo_profiles p ON p.user_id=s.user_id WHERE s.id=? AND p.persona=?',(session_id,'admin')).fetchone() if settings.mode == 'prototype' else None
            if principal:
                audit_actor['principal_admin_id'] = principal['user_id']
            change(c,task,audit_actor,'recording_session_ended',values,{'ticket_id':ticket['id']})

    def task_view(c, task):
        task = expire_active(c,task)
        if settings.mode == 'prototype' and task['latest_attempt_id'] and not task['active_ticket_id'] and task['status'] in ('awaiting_supervisor_ack','awaiting_worker_rerecording','awaiting_approvals','released'):
            old_attempt = c.execute('SELECT recording_id FROM ec_attempts WHERE id=?',(task['latest_attempt_id'],)).fetchone()
            old_capture = c.execute('SELECT ticket_id FROM ec_recordings WHERE id=?',(old_attempt['recording_id'],)).fetchone() if old_attempt else None
            old_speaker = speaker_view(c,old_capture['ticket_id']) if old_capture else None
            if not old_speaker or old_speaker['kind'] != 'shop_floor_worker' or old_speaker['worker_consent_confirmed'] is not True:
                values = {'status':'verification_failed','worker_ok':0,'supervisor_ok':0,'evidence_version':None,'ack_id':None,'correction':None,
                          'failure_reason':'Earlier recording did not identify the shop-floor worker. The Supervisor must capture the named worker with their consent.'}
                change(c,task,None,'named_worker_recording_required',values,{'legacy_attempt_id':task['latest_attempt_id']})
                task = dict(c.execute('SELECT * FROM ec_tasks WHERE id=?',(task['id'],)).fetchone())
        if task['status'] == 'released':
            try:
                check_release(c,task)
            except HTTPException:
                values = blocked_values('verification_failed')
                values.update(failure_reason='Release evidence is no longer current. Verify again before work.')
                change(c,task,None,'release_invalidated',values)
                task = dict(c.execute('SELECT * FROM ec_tasks WHERE id=?',(task['id'],)).fetchone())
        result = {key:task[key] for key in ('id','title','supervisor_id','worker_id','site','version','instruction_revision','status','instruction','instruction_recording_id','latest_attempt_id','correction','failure_reason')}
        if settings.mode == 'prototype':
            named_worker = c.execute('SELECT worker_reference FROM ec_task_workers WHERE task_id=?',(task['id'],)).fetchone()
            result['worker_reference'] = named_worker['worker_reference'] if named_worker else None
            result['worker_reference_locked'] = bool(named_worker)
            if not named_worker and task['active_ticket_id']:
                pending_speaker = speaker_view(c,task['active_ticket_id'])
                if pending_speaker and pending_speaker['kind'] == 'shop_floor_worker':
                    result['worker_reference'] = pending_speaker['reference']
            original = c.execute('SELECT ticket_id FROM ec_recordings WHERE id=?',(task['instruction_recording_id'],)).fetchone()
            result['instruction_speaker'] = speaker_view(c,original['ticket_id']) if original else None
        result['requirements'] = json.loads(task['requirements'])
        result['extraction'] = json.loads(task['extraction']) if task['extraction'] else None
        result['worker_ok'] = bool(task['worker_ok'])
        result['supervisor_ok'] = bool(task['supervisor_ok'])
        result['created'] = timestamp(task['created'])
        result['updated'] = timestamp(task['updated'])
        result['release_eligible'] = False
        result['latest_attempt'] = None
        result['active_ticket'] = None
        if task['active_ticket_id']:
            active = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(task['active_ticket_id'],)).fetchone()
            if active:
                result['active_ticket'] = {'id':active['id'],'purpose':active['purpose'],'status':active['status'],
                                           'actor_id':active['actor_id'],'expires_at':timestamp(active['operation_expires'] if active['status']=='consuming' else active['expires'])}

        if task['latest_attempt_id']:
            attempt = c.execute('SELECT * FROM ec_attempts WHERE id=?',(task['latest_attempt_id'],)).fetchone()
            if attempt:
                result['latest_attempt'] = {k:attempt[k] for k in ('id','revision','recording_id','ack_id','transcript','status')}
                result['latest_attempt']['verification'] = json.loads(attempt['verification']) if attempt['verification'] else None
                if settings.mode == 'prototype':
                    capture = c.execute('SELECT ticket_id FROM ec_recordings WHERE id=?',(attempt['recording_id'],)).fetchone()
                    result['latest_attempt']['speaker'] = speaker_view(c,capture['ticket_id']) if capture else None
        if settings.mode == 'prototype':
            identified = result['latest_attempt'].get('speaker') if result['latest_attempt'] else None
            result['worker_recording_required'] = not bool(identified and identified['kind']=='shop_floor_worker' and identified['worker_consent_confirmed'] is True and identified['operator_id']==task['worker_id'] and result['worker_reference_locked'] and identified['reference']==result['worker_reference'])
        result['release_environment_ready'] = bool(settings.release_environment_ready and provider.configured)
        # Deliberately show eligibility only when all release checks currently hold.
        if task['status'] in ('awaiting_approvals','released'):
            try:
                check_release(c,task)
                result['release_eligible'] = True
            except HTTPException:
                pass
        return result

    def task_result(c, task_id):
        return task_view(c,dict(c.execute('SELECT * FROM ec_tasks WHERE id=?',(task_id,)).fetchone()))

    def evidence(c, task, body=None):
        if body and (body.attempt_id != task['latest_attempt_id'] or body.instruction_revision != task['instruction_revision']):
            raise HTTPException(409,'The requested approval refers to superseded instructions or a superseded recording.')
        if task['status'] not in ('awaiting_approvals','released') or not task['evidence_version'] or task['active_ticket_id']:
            raise HTTPException(409,'A current, passing worker recording is required.')
        attempt = c.execute('SELECT * FROM ec_attempts WHERE id=? AND task_id=? AND revision=?',(task['latest_attempt_id'],task['id'],task['instruction_revision'])).fetchone()
        if not attempt or attempt['status'] != 'passed' or not attempt['verification']:
            raise HTTPException(409,'A current, passing worker recording is required.')
        if settings.mode == 'prototype':
            capture = c.execute('SELECT ticket_id FROM ec_recordings WHERE id=?',(attempt['recording_id'],)).fetchone()
            if not capture:
                raise HTTPException(409,'Record the named worker with their consent before approving this task.')
            capture_ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(capture['ticket_id'],)).fetchone()
            current_worker_speaker(c,task,capture_ticket)
        result = json.loads(attempt['verification'])
        if result.get('passed') is not True:
            raise HTTPException(409,'Verification has not passed.')
        published = c.execute('SELECT * FROM ec_instruction_versions WHERE task_id=? AND revision=?',(task['id'],task['instruction_revision'])).fetchone()
        if not published or published['recording_id'] != task['instruction_recording_id'] or published['instruction'] != task['instruction'] or published['requirements'] != task['requirements']:
            raise HTTPException(409,'The current instruction version must be reviewed and published.')
        if attempt['ack_id'] != task['ack_id']:
            raise HTTPException(409,'Record again after the latest supervisor correction.')
        recordings = []
        for recording_id, purpose, actor_id in ((task['instruction_recording_id'],'instruction',task['supervisor_id']),(attempt['recording_id'],'retelling',task['worker_id'])):
            record = c.execute('SELECT r.*,t.status AS ticket_status,t.created AS ticket_created,t.ack_id AS ticket_ack,c.consent_version,c.accepted,c.actor_id AS consent_actor,c.device_id AS consent_device FROM ec_recordings r JOIN ec_recording_tickets t ON t.id=r.ticket_id JOIN ec_consents c ON c.ticket_id=t.id WHERE r.id=? AND r.task_id=?',(recording_id,task['id'])).fetchone()
            if not record or record['purpose'] != purpose or record['actor_id'] != actor_id or record['revision'] != task['instruction_revision'] or record['ticket_status'] != 'used' or record['consent_version'] != settings.consent_version or record['consent_actor'] != actor_id or record['consent_device'] != record['device_id']:
                raise HTTPException(409,'Current, consented recording evidence is required.')
            if record['retained_until'] <= time.time() or not (settings.audio_dir / record['path']).is_file():
                raise HTTPException(409,'Recording evidence expired or is unavailable. Record and verify again.')
            ticket_row = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(record['ticket_id'],)).fetchone()
            try:
                consent_current(c,ticket_row)
            except InvalidAudio:
                raise HTTPException(409,'Current recording consent is required. Review consent and record again.') from None
            recordings.append(record)
        if settings.mode != 'prototype' and recordings[0]['device_id'] == recordings[1]['device_id']:
            raise HTTPException(409,'Worker retelling must use a second device.')
        if task['ack_id']:
            ack = c.execute('SELECT * FROM ec_acknowledgments WHERE id=? AND task_id=? AND revision=?',(task['ack_id'],task['id'],task['instruction_revision'])).fetchone()
            if not ack or recordings[1]['ticket_created'] <= ack['created'] or recordings[1]['ticket_ack'] != ack['id']:
                raise HTTPException(409,'A fresh worker recording after supervisor acknowledgment is required.')
        return attempt

    def check_release(c,task):
        if not settings.release_environment_ready or not provider.configured:
            raise HTTPException(409,'Release is disabled until the production provider, HTTPS, and release settings are configured.')
        evidence(c,task)
        if task['supervisor_id'] == task['worker_id'] or not task['worker_ok'] or not task['supervisor_ok']:
            raise HTTPException(409,'Both assigned participants must approve the current passing verification.')
        for participant, required_role in ((task['supervisor_id'],'supervisor'),(task['worker_id'],'worker')):
            current_user = c.execute('SELECT * FROM ec_users WHERE id=?',(participant,)).fetchone()
            if not current_user or not current_user['active'] or current_user['role'] != required_role or current_user['site'] != task['site']:
                raise HTTPException(409,'Both assigned participants must retain active access to this site.')
            approval = c.execute('SELECT * FROM ec_approvals WHERE task_id=? AND actor_id=? ORDER BY rowid DESC LIMIT 1',(task['id'],participant)).fetchone()
            if not approval or not approval['approved'] or approval['role'] != required_role or approval['revision'] != task['instruction_revision'] or approval['attempt_id'] != task['latest_attempt_id'] or approval['evidence_version'] != task['evidence_version']:
                raise HTTPException(409,'Current approvals from both assigned participants are required.')

    @app.get('/api/health')
    def health():
        mirror = getattr(app.state,'mirror',None)
        return {'ok':True,'mode':settings.mode,'provider_configured':bool(provider.configured),'provider_model':getattr(provider,'model',None) if provider.configured else None,
                'firestore_mirror':mirror.status() if mirror else {'enabled':False},'release_enabled':bool(settings.release_environment_ready and provider.configured),'consent_version':settings.consent_version,'recording_retention_seconds':settings.recording_retention_seconds,'recording_disclosure':disclosure(),'prototype':settings.mode=='prototype'}

    @app.post('/api/login')
    def login(request: Request,body: Login):
        username = body.username.strip().lower()
        ip = request.client.host if request.client else 'unknown'
        # Per-account and per-origin-address limits, stored across worker processes.
        account_key = 'user:' + token_hash(username)
        ip_key = 'ip:' + token_hash(ip)
        limited = False
        with store.transaction() as c:
            now = time.time()
            c.execute('DELETE FROM ec_login_attempts WHERE created<?',(now-900,))
            account_count = c.execute('SELECT COUNT(*) FROM ec_login_attempts WHERE key=?',(account_key,)).fetchone()[0]
            ip_count = c.execute('SELECT COUNT(*) FROM ec_login_attempts WHERE key=?',(ip_key,)).fetchone()[0]
            limited = settings.mode != 'prototype' and (account_count >= 10 or ip_count >= 50)
            if not limited:
                c.executemany('INSERT INTO ec_login_attempts VALUES (?,?)',[(account_key,now),(ip_key,now)])
            row = c.execute('SELECT * FROM ec_users WHERE username=?',(username,)).fetchone()
        if limited:
            raise HTTPException(429,'Too many sign-in attempts. Try again in 15 minutes.',headers={'Retry-After':'900'})
        valid = verify_password(body.password,row['password_hash'] if row else DUMMY_PASSWORD_HASH)
        with store.transaction() as c:
            demo_account = c.execute('SELECT persona FROM ec_demo_profiles WHERE user_id=?',(row['id'],)).fetchone() if row else None
        if not row or not valid or not row['active'] or (demo_account and settings.mode != 'prototype') or (settings.mode == 'prototype' and not demo_account):
            raise HTTPException(401,'Username or password is incorrect.')
        raw_session,raw_device,csrf = token(),request.cookies.get(DEVICE_COOKIE,''),token()
        with store.transaction() as c:
            # The first transaction was intentionally short; recheck participant availability after hashing.
            current = c.execute('SELECT * FROM ec_users WHERE id=? AND active=1',(row['id'],)).fetchone()
            if not current or current['password_hash'] != row['password_hash']:
                raise HTTPException(401,'Username or password is incorrect.')
            device = c.execute('SELECT * FROM ec_devices WHERE token_hash=?',(token_hash(raw_device),)).fetchone() if raw_device else None
            if not device:
                raw_device,device_id = token(),uid()
                c.execute('INSERT INTO ec_devices VALUES(?,?,?)',(device_id,token_hash(raw_device),time.time()))
            else:
                device_id = device['id']
            previous = request.cookies.get(SESSION_COOKIE,'')
            if previous:
                old_session = c.execute('SELECT id FROM ec_sessions WHERE token_hash=?',(token_hash(previous),)).fetchone()
                if old_session:
                    cancel_session_operations(c,old_session['id'],dict(row))
                c.execute('DELETE FROM ec_sessions WHERE token_hash=?',(token_hash(previous),))
            c.execute('DELETE FROM ec_sessions WHERE expires<=?',(time.time(),))
            session_id = uid()
            c.execute('INSERT INTO ec_sessions VALUES(?,?,?,?,?,?,?)',(session_id,token_hash(raw_session),row['id'],device_id,csrf,time.time()+settings.session_seconds,time.time()))
            c.execute('DELETE FROM ec_login_attempts WHERE key=?',(account_key,))
            store.audit(c,None,row['id'],'login',None,{'device_id':device_id})
            login_actor = dict(row)
            login_actor.update(session_id=session_id,csrf_token=csrf,device_id=device_id,principal_id=row['id'],principal_persona=demo_account['persona'] if demo_account else None,persona=demo_account['persona'] if demo_account else None)
            login_payload = identity_view(c,login_actor)
        response = JSONResponse(login_payload)
        response.set_cookie(SESSION_COOKIE,raw_session,max_age=settings.session_seconds,httponly=True,secure=settings.secure_cookies,samesite='strict',path='/')
        response.set_cookie(DEVICE_COOKIE,raw_device,max_age=31536000,httponly=True,secure=settings.secure_cookies,samesite='strict',path='/')
        return response

    @app.get('/api/me')
    def me(request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            return identity_view(c,actor)

    @app.post('/api/logout')
    def logout(request: Request):
        with store.transaction() as c:
            actor = session(request,c,True,allow_admin=True)
            cancel_session_operations(c,actor['session_id'],actor)
            c.execute('DELETE FROM ec_sessions WHERE id=?',(actor['session_id'],))
            store.audit(c,None,actor['id'],'logout')
        response = JSONResponse({'ok':True})
        response.delete_cookie(SESSION_COOKIE,path='/',secure=settings.secure_cookies,httponly=True,samesite='strict')
        return response

    @app.get('/api/prototype/users')
    def prototype_users(request: Request):
        if settings.mode != 'prototype':
            raise HTTPException(404,'Not found.')
        with store.transaction() as c:
            actor = session(request,c)
            if not principal_admin(actor):
                raise HTTPException(403,'Only the prototype Admin can switch perspectives.')
            return demo_users(c)

    @app.post('/api/prototype/view-as')
    def prototype_view_as(request: Request,body: ViewAs):
        if settings.mode != 'prototype':
            raise HTTPException(404,'Not found.')
        with store.transaction() as c:
            actor = session(request,c,True,allow_admin=True)
            if not principal_admin(actor):
                raise HTTPException(403,'Only the prototype Admin can switch perspectives.')
            target = None
            if body.user_id:
                target = c.execute('SELECT u.*,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id WHERE u.id=? AND u.active=1 AND u.site=?',(body.user_id,actor['principal_site'])).fetchone()
                if not target:
                    raise HTTPException(422,'Choose one of the three prototype perspectives.')
            cancel_session_operations(c,actor['session_id'],actor)
            selected_id = target['id'] if target and target['id'] != actor['principal_id'] else None
            c.execute('INSERT OR REPLACE INTO ec_demo_views VALUES(?,?)',(actor['session_id'],selected_id))
            csrf = token()
            c.execute('UPDATE ec_sessions SET csrf_token=? WHERE id=?',(csrf,actor['session_id']))
            store.audit(c,None,actor['principal_id'],'prototype_perspective_changed',None,{'principal_admin_id':actor['principal_id'],'target_user_id':selected_id})
            updated = session(request,c)
            return identity_view(c,updated)

    @app.get('/api/people')
    def people(request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            role(actor,'supervisor')
            return [user_view(dict(r)) for r in c.execute("SELECT u.id,u.username,u.role,u.site,u.display_name,p.persona FROM ec_users u LEFT JOIN ec_demo_profiles p ON p.user_id=u.id WHERE u.site=? AND u.role='worker' AND u.active=1 ORDER BY u.display_name",(actor['site'],)).fetchall()]

    @app.post('/api/tasks')
    def new_task(request: Request,body: NewTask):
        with store.transaction() as c:
            actor = session(request,c,True)
            role(actor,'supervisor')
            worker = c.execute("SELECT * FROM ec_users WHERE id=? AND site=? AND role='worker' AND active=1",(body.worker_id,actor['site'])).fetchone()
            if not worker or worker['id'] == actor['id']:
                raise HTTPException(422,'Choose an active Supervisor assigned to your site.' if settings.mode == 'prototype' else 'Choose an active worker assigned to your site.')
            title = body.title.strip()
            if len(title) < 3:
                raise HTTPException(422,'Enter a descriptive task title.')
            task_id,now = uid(),time.time()
            c.execute('INSERT INTO ec_tasks(id,title,supervisor_id,worker_id,site,version,instruction_revision,status,created,updated) VALUES(?,?,?,?,?,1,1,?,?,?)',(task_id,title,actor['id'],worker['id'],actor['site'],'draft',now,now))
            store.audit(c,task_id,actor['id'],'task_created',1,audit_details(actor,{'worker_id':worker['id']}))
            return task_result(c,task_id)

    @app.get('/api/tasks')
    def list_tasks(request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            if actor.get('is_admin_base'):
                rows = c.execute('SELECT * FROM ec_tasks WHERE site=? ORDER BY created DESC',(actor['site'],)).fetchall()
            else:
                rows = c.execute('SELECT * FROM ec_tasks WHERE site=? AND (supervisor_id=? OR worker_id=?) ORDER BY created DESC',(actor['site'],actor['id'],actor['id'])).fetchall()
            return [task_view(c,dict(row)) for row in rows]

    @app.get('/api/tasks/{task_id}')
    def get_task(task_id: str,request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            return task_view(c,assigned(c,task_id,actor))

    @app.post('/api/tasks/{task_id}/recording-tickets')
    def recording_ticket(task_id: str,request: Request,body: TicketBody):
        with store.transaction() as c:
            actor = session(request,c,True)
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if body.consent_accepted is not True:
                raise HTTPException(422,'Recording requires explicit consent.')
            if body.consent_version != settings.consent_version or body.consent_policy_id != disclosure()['policy_id']:
                raise HTTPException(409,'Review the current recording consent before recording.')
            if not provider.configured:
                raise HTTPException(503,'Voice processing is not configured. Work remains blocked.')
            limit = store.enforce_live_budget(c,actor['session_id'])
            if limit:
                raise HTTPException(429,limit)
            task = expire_active(c,task)
            if task['active_ticket_id']:
                raise HTTPException(409,'Finish or cancel the current recording before starting another.')
            worker_reference = None
            if body.purpose == 'instruction':
                role(actor,'supervisor')
                if task['status'] not in ('draft','recording_instruction'):
                    raise HTTPException(409,'Revise the task before changing its instruction recording.')
                target_status = 'recording_instruction'
            else:
                role(actor,'worker')
                if settings.mode == 'prototype':
                    worker_reference = ' '.join((body.worker_reference or '').split())
                    if not worker_reference or body.worker_consent_confirmed is not True:
                        raise HTTPException(422,'Identify the worker and confirm they agreed to this voice recording before capture.')
                    frozen = c.execute('SELECT * FROM ec_task_workers WHERE task_id=?',(task_id,)).fetchone()
                    if frozen and frozen['worker_reference'].casefold() != worker_reference.casefold():
                        raise HTTPException(409,'Record the same worker again after corrections. Create a new task for a different worker.')
                    if frozen:
                        worker_reference = frozen['worker_reference']
                allowed = ('awaiting_worker_recording','awaiting_worker_rerecording','awaiting_approvals','released','verification_failed','recording_worker')
                if task['status'] not in allowed:
                    raise HTTPException(409,'The supervisor must review discrepancies before another worker recording.')
                published = c.execute('SELECT id FROM ec_instruction_versions WHERE task_id=? AND revision=?',(task_id,task['instruction_revision'])).fetchone()
                original = c.execute('SELECT * FROM ec_recordings WHERE id=?',(task['instruction_recording_id'],)).fetchone()
                if not published or not original:
                    raise HTTPException(409,'The Project manager must publish the recorded instruction first.' if settings.mode == 'prototype' else 'The supervisor must publish the recorded instruction first.')
                try:
                    consent_current(c,c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(original['ticket_id'],)).fetchone())
                except InvalidAudio:
                    raise HTTPException(409,'Instruction consent changed. Ask the Project manager to revise and record again.' if settings.mode == 'prototype' else 'Instruction consent changed. Ask the supervisor to revise and record again.') from None
                if settings.mode != 'prototype' and original['device_id'] == actor['device_id']:
                    raise HTTPException(409,'Use a second device for the worker retelling.')
                if original['retained_until'] <= time.time() or not (settings.audio_dir / original['path']).is_file():
                    raise HTTPException(409,'The instruction recording expired. Ask the Project manager to revise and record it again.' if settings.mode == 'prototype' else 'The instruction recording expired. Ask the supervisor to revise and record it again.')
                target_status = 'recording_worker'
            ticket_id,now = uid(),time.time()
            new_version = task['version']+1
            c.execute('INSERT INTO ec_recording_tickets(id,task_id,actor_id,session_id,device_id,purpose,revision,version,ack_id,status,expires,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ticket_id,task_id,actor['id'],actor['session_id'],actor['device_id'],body.purpose,task['instruction_revision'],new_version,task['ack_id'],'issued',now+settings.ticket_seconds,now))
            if settings.mode == 'prototype':
                c.execute('INSERT INTO ec_capture_speakers VALUES(?,?,?,?,?,?)',(ticket_id,actor['id'],'project_manager' if body.purpose=='instruction' else 'shop_floor_worker',worker_reference,1 if body.purpose=='retelling' else 0,now))
            c.execute('INSERT INTO ec_consents VALUES(?,?,?,?,?,?,?,?,?)',(uid(),ticket_id,actor['id'],actor['device_id'],actor['session_id'],settings.consent_version,now,disclosure()['policy_id'],encode(disclosure())))
            values = blocked_values(target_status)
            values.update(active_ticket_id=ticket_id,failure_reason=None)
            if body.purpose == 'instruction':
                values.update(instruction='',requirements='[]',extraction=None,instruction_recording_id=None)
            change(c,task,actor,'recording_consent_accepted',values,{'ticket_id':ticket_id,'purpose':body.purpose,'consent_version':settings.consent_version,**({'operator_id':actor['id'],'speaker_kind':'project_manager' if body.purpose=='instruction' else 'shop_floor_worker','worker_reference':worker_reference,'worker_consent_confirmed':body.purpose=='retelling'} if settings.mode=='prototype' else {})})
            return {'ticket_id':ticket_id,'version':new_version,'expires_at':timestamp(now+settings.ticket_seconds)}

    @app.post('/api/recording-tickets/{ticket_id}/cancel')
    def cancel_recording(ticket_id: str,request: Request,body: Versioned):
        with store.transaction() as c:
            actor = session(request,c,True)
            ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(ticket_id,)).fetchone()
            if not ticket or ticket['actor_id'] != actor['id'] or ticket['device_id'] != actor['device_id']:
                raise HTTPException(404,'Recording not found.')
            task = assigned(c,ticket['task_id'],actor)
            version(task,body.expected_version)
            if ticket['status'] == 'cancelled':
                return task_result(c,task['id'])
            if task['active_ticket_id'] != ticket_id or ticket['status'] not in ('issued','consuming'):
                raise HTTPException(409,'This recording is no longer active.')
            c.execute("UPDATE ec_recording_tickets SET status='cancelled' WHERE id=?",(ticket_id,))
            values = blocked_values('draft' if ticket['purpose'] == 'instruction' else 'verification_failed')
            values.update(active_ticket_id=None,failure_reason='Recording cancelled. A fresh recording is required.')
            change(c,task,actor,'recording_cancelled',values,{'ticket_id':ticket_id})
            return task_result(c,task['id'])

    def current_operation(c,task_id,ticket_id,operation_version,allow_expired=False):
        task = c.execute('SELECT * FROM ec_tasks WHERE id=?',(task_id,)).fetchone()
        ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(ticket_id,)).fetchone()
        if not task or not ticket or task['version'] != operation_version or task['active_ticket_id'] != ticket_id or ticket['status'] != 'consuming' or ticket['revision'] != task['instruction_revision'] or ticket['ack_id'] != task['ack_id']:
            raise HTTPException(409,'This recording was superseded. Refresh the task before continuing.')
        if not allow_expired and (ticket['operation_expires'] is None or ticket['operation_expires'] <= time.time()):
            raise InvalidAudio('Voice processing timed out. Review consent and record again.')
        return dict(task),dict(ticket)

    def save_failure(task_id,ticket_id,operation_version,actor,message,recording_id=None,transcript=''):
        with store.transaction() as c:
            task,ticket = current_operation(c,task_id,ticket_id,operation_version,allow_expired=True)
            c.execute("UPDATE ec_recording_tickets SET status='failed' WHERE id=?",(ticket_id,))
            values = blocked_values('draft' if ticket['purpose'] == 'instruction' else 'verification_failed')
            if ticket['purpose'] == 'retelling':
                attempt_id = uid()
                c.execute('INSERT INTO ec_attempts VALUES(?,?,?,?,?,?,?,?,?)',(attempt_id,task_id,task['instruction_revision'],recording_id,ticket['ack_id'],transcript,'failed',None,time.time()))
                values['latest_attempt_id'] = attempt_id
            values.update(active_ticket_id=None,failure_reason=message)
            change(c,task,actor,'recording_failed',values,{'ticket_id':ticket_id,'reason':message})

    def processing_budget(task_id,ticket_id,operation_version,request):
        with store.transaction() as c:
            task,ticket = current_operation(c,task_id,ticket_id,operation_version)
            consent_current(c,ticket)
            actor = session(request,c,True)
            assigned(c,task_id,actor)
            return max(0.01,ticket['operation_expires']-time.time())

    @app.post('/api/tasks/{task_id}/recordings/{ticket_id}')
    async def upload_recording(task_id: str,ticket_id: str,request: Request):
        try:
            expected_version = int(request.headers.get('x-expected-version',''))
        except ValueError:
            raise HTTPException(422,'X-Expected-Version is required for a recording upload.')
        with store.transaction() as c:
            actor = session(request,c,True)
            task = assigned(c,task_id,actor)
            version(task,expected_version)
            ticket = c.execute('SELECT * FROM ec_recording_tickets WHERE id=? AND task_id=?',(ticket_id,task_id)).fetchone()
            if not ticket or ticket['actor_id'] != actor['id'] or ticket['session_id'] != actor['session_id'] or ticket['device_id'] != actor['device_id']:
                raise HTTPException(404,'Recording ticket not found.')
            if ticket['status'] != 'issued' or task['active_ticket_id'] != ticket_id or ticket['version'] != expected_version or ticket['revision'] != task['instruction_revision'] or ticket['ack_id'] != task['ack_id']:
                raise HTTPException(409,'This recording ticket is no longer current. Record again.')
            if ticket['expires'] <= time.time():
                # Persist the failure below without exposing an old release state.
                expired = True
            else:
                expired = False
            c.execute("UPDATE ec_recording_tickets SET status='consuming',operation_expires=? WHERE id=?",(time.time()+settings.processing_seconds,ticket_id))
            values = blocked_values('transcribing_instruction' if ticket['purpose'] == 'instruction' else 'verification_pending')
            values.update(active_ticket_id=ticket_id,failure_reason=None)
            operation_version = change(c,task,actor,'recording_upload_started',values,{'ticket_id':ticket_id})
            ticket = dict(ticket)
            snapshot = dict(task)
        recording_id = None
        transcript = ''
        filename = None
        inserted = False
        try:
            with store.transaction() as c:
                consent_current(c,ticket)
            if expired:
                raise InvalidAudio('Recording consent expired before upload. Review consent and record again.')
            mime = request.headers.get('content-type','').split(';',1)[0].strip().lower()
            if mime not in ALLOWED_MIME:
                raise InvalidAudio('Use WAV, WebM, Ogg, or MP4 audio.')
            if mime in ('audio/x-wav','audio/wave'):
                mime = 'audio/wav'
            content_length = request.headers.get('content-length')
            if content_length:
                try:
                    if int(content_length) > MAX_AUDIO_BYTES:
                        raise InvalidAudio('Recording must be no larger than 8 MiB.')
                except ValueError:
                    raise InvalidAudio('Recording size is invalid.') from None
            async def read_bounded_body():
                chunks,size = [],0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise InvalidAudio('Recording must be no larger than 8 MiB.')
                    chunks.append(chunk)
                return b''.join(chunks)
            data = await asyncio.wait_for(read_bounded_body(),timeout=settings.processing_seconds)
            digest = await asyncio.to_thread(validate_audio,data,mime)
            # Shield the save job so request cancellation cannot discard the filename
            # while its worker thread continues writing private audio.
            save_job = asyncio.create_task(asyncio.to_thread(save_private_audio,settings.audio_dir,data))
            try:
                filename = await asyncio.shield(save_job)
            except asyncio.CancelledError:
                def remove_cancelled_save(job):
                    try:
                        orphan = job.result()
                        (settings.audio_dir / orphan).unlink(missing_ok=True)
                    except BaseException:
                        # Process interruption is covered by the age-graced admin sweep.
                        pass
                save_job.add_done_callback(remove_cancelled_save)
                raise
            recording_id,now = uid(),time.time()
            with store.transaction() as c:
                current,active_ticket = current_operation(c,task_id,ticket_id,operation_version)
                if c.execute('SELECT id FROM ec_recordings WHERE task_id=? AND sha256=?',(task_id,digest)).fetchone():
                    raise InvalidAudio('This exact recording has already been used for this task. Record a fresh explanation.')
                if settings.mode == 'prototype' and ticket['purpose'] == 'retelling':
                    speaker = current_worker_speaker(c,current,active_ticket,require_frozen=False)
                    c.execute('INSERT OR IGNORE INTO ec_task_workers VALUES(?,?,?,?)',(task_id,speaker['reference'],actor['id'],now))
                c.execute('INSERT INTO ec_recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(recording_id,task_id,ticket_id,actor['id'],actor['device_id'],ticket['purpose'],ticket['revision'],ticket['ack_id'],filename,mime,digest,len(data),now+settings.recording_retention_seconds,now))
                store.audit(c,task_id,actor['id'],'recording_stored',operation_version,audit_details(actor,{'recording_id':recording_id,'ticket_id':ticket_id}))
            inserted = True
            with store.transaction() as c:
                current_operation(c,task_id,ticket_id,operation_version)
                consent_current(c,ticket)
                session(request,c,True)
            if not provider.configured:
                raise RuntimeError('provider unavailable')
            budget = processing_budget(task_id,ticket_id,operation_version,request)
            transcript = await asyncio.wait_for(provider.transcribe(data,mime),timeout=budget)
            if not isinstance(transcript,str) or not 2 <= len(transcript.strip()) <= 12000:
                raise InvalidProviderResponse('Transcription was incomplete.')
            transcript = transcript.strip()
            if ticket['purpose'] == 'instruction':
                budget = processing_budget(task_id,ticket_id,operation_version,request)
                extraction = await asyncio.wait_for(provider.extract_requirements(transcript),timeout=budget)
                extraction = validate_extraction(extraction)
                requirements = extraction['requirements']
                with store.transaction() as c:
                    current,active_ticket = current_operation(c,task_id,ticket_id,operation_version)
                    # Logouts/role changes invalidate in-flight responses too.
                    consent_current(c,active_ticket)
                    fresh_actor = session(request,c,True)
                    assigned(c,task_id,fresh_actor)
                    c.execute('INSERT INTO ec_transcripts VALUES(?,?,?,?)',(uid(),recording_id,transcript,time.time()))
                    c.execute("UPDATE ec_recording_tickets SET status='used' WHERE id=?",(ticket_id,))
                    values = blocked_values('draft')
                    values.update(instruction=transcript,requirements=encode(requirements),extraction=encode(extraction),instruction_recording_id=recording_id,active_ticket_id=None,failure_reason=None)
                    change(c,current,fresh_actor,'instruction_transcribed',values,{'recording_id':recording_id})
                    return task_result(c,task_id)
            requirements = json.loads(snapshot['requirements'])
            budget = processing_budget(task_id,ticket_id,operation_version,request)
            result = await asyncio.wait_for(provider.evaluate(requirements,transcript),timeout=budget)
            if not isinstance(result,dict):
                raise InvalidProviderResponse('Verification was incomplete.')
            validated = validate_evaluation(requirements,transcript,{k:v for k,v in result.items() if k not in ('passed','decision')})
            with store.transaction() as c:
                current,active_ticket = current_operation(c,task_id,ticket_id,operation_version)
                consent_current(c,active_ticket)
                fresh_actor = session(request,c,True)
                assigned(c,task_id,fresh_actor)
                attempt_id,now = uid(),time.time()
                c.execute('INSERT INTO ec_transcripts VALUES(?,?,?,?)',(uid(),recording_id,transcript,now))
                c.execute('INSERT INTO ec_attempts VALUES(?,?,?,?,?,?,?,?,?)',(attempt_id,task_id,ticket['revision'],recording_id,ticket['ack_id'],transcript,'passed' if validated['passed'] else 'discrepancies',encode(validated),now))
                c.execute("UPDATE ec_recording_tickets SET status='used' WHERE id=?",(ticket_id,))
                values = blocked_values('awaiting_approvals' if validated['passed'] else 'awaiting_supervisor_ack')
                values.update(latest_attempt_id=attempt_id,active_ticket_id=None,evidence_version=current['version']+1 if validated['passed'] else None,failure_reason=None)
                change(c,current,fresh_actor,'verification_completed',values,{'attempt_id':attempt_id,'passed':validated['passed']})
                return task_result(c,task_id)
        except HTTPException as exc:
            # A late response must never overwrite a newer operation or revision.
            # Session expiry still records a blocking failure if this operation remains current.
            if exc.status_code in (401,403):
                try:
                    save_failure(task_id,ticket_id,operation_version,actor,'Session expired during voice processing. Sign in and record again.',recording_id,transcript)
                except HTTPException:
                    pass
            raise
        except BaseException as exc:
            message = str(exc) if isinstance(exc,InvalidAudio) else 'Voice processing failed. Work remains blocked. Try a fresh recording when the service is available.'
            try:
                save_failure(task_id,ticket_id,operation_version,actor,message,recording_id,transcript)
            except HTTPException as conflict:
                if isinstance(exc,asyncio.CancelledError):
                    raise
                raise conflict
            if isinstance(exc,asyncio.CancelledError):
                raise
            raise HTTPException(409 if isinstance(exc,ConsentChanged) else 422 if isinstance(exc,InvalidAudio) else 503,message) from None
        finally:
            if filename and not inserted:
                (settings.audio_dir / filename).unlink(missing_ok=True)

    @app.post('/api/tasks/{task_id}/publish')
    def publish(task_id: str,request: Request,body: Publish):
        try:
            requirements = validate_requirements(body.requirements)
        except InvalidProviderResponse:
            raise HTTPException(422,'Review every requirement and give each one a unique identifier and clear text.') from None
        if body.reviewed_complete is not True:
            raise HTTPException(422,'The supervisor must review the complete instruction and every requirement.')
        instruction = body.instruction.strip()
        if len(instruction) < 8:
            raise HTTPException(422,'Review the complete instruction before publishing.')
        with store.transaction() as c:
            actor = session(request,c,True)
            role(actor,'supervisor')
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if task['status'] != 'draft' or task['active_ticket_id'] or not task['instruction_recording_id']:
                raise HTTPException(409,'Record and review the instruction before publishing.')
            record = c.execute('SELECT r.*,t.status AS ticket_status,c.consent_version FROM ec_recordings r JOIN ec_recording_tickets t ON t.id=r.ticket_id JOIN ec_consents c ON c.ticket_id=t.id WHERE r.id=?',(task['instruction_recording_id'],)).fetchone()
            if not record or record['ticket_status'] != 'used' or record['actor_id'] != actor['id'] or record['revision'] != task['instruction_revision'] or record['consent_version'] != settings.consent_version or record['retained_until'] <= time.time() or not (settings.audio_dir / record['path']).is_file():
                raise HTTPException(409,'A current, consented supervisor recording is required.')
            try:
                consent_current(c,c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(record['ticket_id'],)).fetchone())
            except InvalidAudio:
                raise HTTPException(409,'Instruction consent changed. Review consent and record again.') from None
            c.execute('INSERT INTO ec_instruction_versions VALUES(?,?,?,?,?,?,?,?,?)',(uid(),task_id,task['instruction_revision'],instruction,encode(requirements),task['instruction_recording_id'],actor['id'],1,time.time()))
            values = blocked_values('awaiting_worker_recording')
            values.update(instruction=instruction,requirements=encode(requirements),correction=None,ack_id=None,failure_reason=None)
            change(c,task,actor,'instruction_published',values,{'revision':task['instruction_revision'],'recording_id':task['instruction_recording_id'],'reviewed_complete':True})
            return task_result(c,task_id)

    @app.post('/api/tasks/{task_id}/revise')
    def revise(task_id: str,request: Request,body: Versioned):
        with store.transaction() as c:
            actor = session(request,c,True)
            role(actor,'supervisor')
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if task['active_ticket_id']:
                c.execute("UPDATE ec_recording_tickets SET status='superseded' WHERE id=? AND status IN ('issued','consuming')",(task['active_ticket_id'],))
            values = blocked_values('draft')
            values.update(instruction_revision=task['instruction_revision']+1,instruction='',requirements='[]',extraction=None,instruction_recording_id=None,correction=None,ack_id=None,active_ticket_id=None,failure_reason=None)
            change(c,task,actor,'instruction_revision_started',values)
            return task_result(c,task_id)

    @app.post('/api/tasks/{task_id}/acknowledge')
    def acknowledge(task_id: str,request: Request,body: Acknowledge):
        correction = body.correction.strip()
        if len(correction) < 3:
            raise HTTPException(422,'Describe the correction explained to the worker.')
        with store.transaction() as c:
            actor = session(request,c,True)
            role(actor,'worker' if settings.mode == 'prototype' else 'supervisor')
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if task['status'] != 'awaiting_supervisor_ack' or body.attempt_id != task['latest_attempt_id']:
                raise HTTPException(409,'Acknowledge the discrepancies in the current worker recording.')
            if settings.mode == 'prototype':
                attempt = c.execute('SELECT recording_id FROM ec_attempts WHERE id=?',(body.attempt_id,)).fetchone()
                capture = c.execute('SELECT ticket_id FROM ec_recordings WHERE id=?',(attempt['recording_id'],)).fetchone() if attempt else None
                if not capture:
                    raise HTTPException(409,'A named worker recording is required before acknowledging corrections.')
                current_worker_speaker(c,task,c.execute('SELECT * FROM ec_recording_tickets WHERE id=?',(capture['ticket_id'],)).fetchone())
            ack_id = uid()
            c.execute('INSERT INTO ec_acknowledgments VALUES(?,?,?,?,?,?,?)',(ack_id,task_id,task['instruction_revision'],body.attempt_id,actor['id'],correction,time.time()))
            values = {'status':'awaiting_worker_rerecording','worker_ok':0,'supervisor_ok':0,'evidence_version':None,'ack_id':ack_id,'correction':correction,'failure_reason':None}
            change(c,task,actor,'discrepancies_acknowledged',values,{'attempt_id':body.attempt_id,'ack_id':ack_id})
            return task_result(c,task_id)

    @app.post('/api/tasks/{task_id}/approve')
    def approve(task_id: str,request: Request,body: Approve):
        if body.approved is not True:
            raise HTTPException(422,'Use withdrawal to revoke an approval.')
        with store.transaction() as c:
            actor = session(request,c,True)
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            evidence(c,task,body)
            c.execute('INSERT INTO ec_approvals VALUES(?,?,?,?,?,?,?,?,?)',(uid(),task_id,task['instruction_revision'],task['latest_attempt_id'],task['evidence_version'],actor['id'],actor['role'],1,time.time()))
            change(c,task,actor,'verification_approved',{actor['role']+'_ok':1}, {'attempt_id':task['latest_attempt_id'],'revision':task['instruction_revision']})
            return task_result(c,task_id)

    @app.post('/api/tasks/{task_id}/withdraw')
    def withdraw(task_id: str,request: Request,body: EvidenceAction):
        with store.transaction() as c:
            actor = session(request,c,True)
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if body.attempt_id != task['latest_attempt_id'] or body.instruction_revision != task['instruction_revision'] or task['status'] not in ('awaiting_approvals','released') or task['evidence_version'] is None:
                raise HTTPException(409,'This approval refers to superseded evidence.')
            c.execute('INSERT INTO ec_approvals VALUES(?,?,?,?,?,?,?,?,?)',(uid(),task_id,task['instruction_revision'],task['latest_attempt_id'],task['evidence_version'],actor['id'],actor['role'],0,time.time()))
            change(c,task,actor,'verification_approval_withdrawn',{'status':'awaiting_approvals',actor['role']+'_ok':0},{'attempt_id':task['latest_attempt_id']})
            return task_result(c,task_id)

    @app.post('/api/tasks/{task_id}/release')
    def release(task_id: str,request: Request,body: EvidenceAction):
        with store.transaction() as c:
            actor = session(request,c,True)
            role(actor,'supervisor')
            task = assigned(c,task_id,actor)
            version(task,body.expected_version)
            if body.attempt_id != task['latest_attempt_id'] or body.instruction_revision != task['instruction_revision']:
                raise HTTPException(409,'This release refers to superseded evidence.')
            if task['status'] != 'awaiting_approvals':
                raise HTTPException(409,'Complete current verification and approvals before releasing work.')
            check_release(c,task)
            change(c,task,actor,'work_released',{'status':'released'},{'attempt_id':task['latest_attempt_id'],'revision':task['instruction_revision']})
            return task_result(c,task_id)

    @app.get('/api/recordings/{recording_id}')
    def recording(recording_id: str,request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            row = c.execute('SELECT * FROM ec_recordings WHERE id=?',(recording_id,)).fetchone()
            if not row:
                raise HTTPException(404,'Recording not found.')
            assigned(c,row['task_id'],actor)
            path = settings.audio_dir / row['path']
            if row['retained_until'] <= time.time() or not path.is_file():
                raise HTTPException(410,'Recording expired or is no longer available.')
            store.audit(c,row['task_id'],actor['id'],'recording_played',None,audit_details(actor,{'recording_id':recording_id}))
            return FileResponse(path,media_type=row['mime'],headers={'Cache-Control':'no-store','Content-Disposition':'inline','X-Content-Type-Options':'nosniff'})

    @app.get('/api/tasks/{task_id}/history')
    def history(task_id: str,request: Request):
        with store.transaction() as c:
            actor = session(request,c)
            assigned(c,task_id,actor)
            audits = c.execute('SELECT * FROM ec_audit WHERE task_id=? ORDER BY id',(task_id,)).fetchall()
            return [{'id':r['id'],'actor_id':r['actor_id'],'action':r['action'],'version':r['version'],'detail':json.loads(r['detail']),'created':timestamp(r['created'])} for r in audits]

    from .demo import DemoService, demo_router
    demo_service = DemoService(settings,store)
    app.state.demo_service = demo_service
    app.include_router(demo_router(demo_service,session,provider=provider))

    app.state.mirror = None
    if settings.firestore_mirror:
        from .firestore_mirror import FirestoreMirror
        app.state.mirror = FirestoreMirror(store,client_factory=mirror_client_factory,project=settings.firestore_project)
        app.state.mirror.start()

    frontend = ROOT / 'frontend'
    app.mount('/assets',StaticFiles(directory=frontend),name='assets')

    @app.get('/')
    def index():
        return FileResponse(frontend / 'index.html',headers={'Cache-Control':'no-store'})

    return app

app = create_app()
