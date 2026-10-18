"""Isolated prerecorded practice runs; never live AI or permission to start work."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .audio import InvalidAudio, validate_audio
from .config import ROOT
from .store import encode, timestamp, uid
from .verification import InvalidProviderResponse, ProviderUnavailable, validate_evaluation, validate_requirements

VARIANTS = ('correct','incorrect','corrected','still_incorrect')
LABELS = {'correct':'Correct response','incorrect':'Incorrect response','corrected':'Corrected response','still_incorrect':'Still incorrect response'}

class DemoBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: StrictInt = Field(ge=1)

class DemoAttempt(DemoBody):
    variant: Literal['correct','incorrect','corrected','still_incorrect']

class DemoAck(DemoBody):
    attempt_id: str = Field(min_length=1,max_length=80)
    correction: Optional[str] = Field(default=None,min_length=3,max_length=6000)

class DemoApprove(DemoBody):
    attempt_id: str = Field(min_length=1,max_length=80)
    approved: StrictBool = True

class DemoLiveCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    variant: Literal['correct','incorrect','corrected','still_incorrect']

LIVE_CHECK_SECONDS = 75
LIVE_DISCLOSURE = ('Live Gemini check of a synthetic sample voice. Practice only: it is not worker evidence, '
                   'does not change this demo and cannot release work.')

class DemoService:
    def __init__(self,settings,store):
        self.settings,self.store = settings,store
        self.catalog_path = ROOT / 'dataset' / 'demo_scenarios_v1.json'
        self.assets_dir = ROOT / 'backend' / 'demo_assets'
        self.manifest_path = self.assets_dir / 'manifest.json'
        self.catalog = {}
        self.catalog_id = None
        if settings.mode == 'prototype':
            try:
                self.seed()
            except HTTPException:
                raise RuntimeError('Prototype mode requires dataset/demo_scenarios_v1.json and backend/demo_assets. '
                                   'Package the practice catalog or choose another ECHOCHECK_MODE.') from None

    def load_catalog(self):
        try:
            document = json.loads(self.catalog_path.read_text(encoding='utf-8'))
            scenarios = document['scenarios']
            if document['schema_version'] != 1 or document['evidence_kind'] != 'scripted_demo_fixture' or not isinstance(document['catalog_id'],str) or len(scenarios) != 7:
                raise ValueError
            catalog = {}
            for scenario in scenarios:
                sid = scenario['id']
                if not re.fullmatch(r'demo-[a-z0-9-]{1,90}',sid) or sid in catalog or not scenario['title'].strip() or not scenario['worker_reference'].strip():
                    raise ValueError
                requirements = validate_requirements(scenario['instruction']['requirements'])
                if not isinstance(scenario['instruction']['transcript'],str) or not scenario['instruction']['transcript'].strip() or set(scenario['variants']) != set(VARIANTS):
                    raise ValueError
                for key in VARIANTS:
                    example = scenario['variants'][key]
                    result = validate_evaluation(requirements,example['transcript'],example['evaluation'])
                    if type(example['expected_pass']) is not bool or result['passed'] != example['expected_pass'] or result['decision'] != example['expected_decision']:
                        raise ValueError
                catalog[sid] = scenario
            self.catalog,self.catalog_id = catalog,document['catalog_id']
        except (OSError,ValueError,TypeError,KeyError,InvalidProviderResponse):
            raise HTTPException(503,'The practice examples are unavailable. No live task was changed.') from None

    def event(self,c,run,actor,action,detail=None):
        c.execute('INSERT INTO ec_demo_events(run_id,version,action,actor_id,principal_id,detail,created) VALUES(?,?,?,?,?,?,?)',
                  (run['id'],run['version'],action,actor['id'] if actor else None,actor.get('principal_id',actor['id']) if actor else None,encode(detail or {}),time.time()))

    def seed(self):
        if self.settings.mode != 'prototype':
            return
        self.load_catalog()
        with self.store.transaction() as c:
            profiles = {row['persona']:dict(row) for row in c.execute('SELECT u.id,u.role,u.site,u.active,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id')}
            manager,supervisor = profiles.get('project_manager'),profiles.get('supervisor')
            if not manager or not supervisor or not manager['active'] or not supervisor['active'] or manager['site'] != supervisor['site'] or manager['role'] != 'supervisor' or supervisor['role'] != 'worker':
                return
            for scenario in self.catalog.values():
                if c.execute('SELECT 1 FROM ec_demo_runs WHERE scenario_id=? AND site=?',(scenario['id'],manager['site'])).fetchone():
                    continue
                now,run_id = time.time(),'demo_'+uid()
                c.execute('INSERT INTO ec_demo_runs(id,scenario_id,catalog_id,scenario_json,project_manager_id,supervisor_id,site,version,generation,status,created,updated) VALUES(?,?,?,?,?,?,?,1,1,?,?,?)',
                          (run_id,scenario['id'],self.catalog_id,encode(scenario),manager['id'],supervisor['id'],manager['site'],'awaiting_worker_example',now,now))
                self.event(c,{'id':run_id,'version':1},None,'demo_preloaded',{'scenario_id':scenario['id'],'source':'scripted_demo','human_instruction_approval':False})

    def assigned(self,c,run_id,actor):
        run = c.execute('SELECT * FROM ec_demo_runs WHERE id=?',(run_id,)).fetchone()
        if not run or actor['site'] != run['site'] or (actor['id'] not in (run['project_manager_id'],run['supervisor_id']) and not actor.get('is_admin_base')):
            raise HTTPException(404,'Practice example not found.')
        for person,persona,role in ((run['project_manager_id'],'project_manager','supervisor'),(run['supervisor_id'],'supervisor','worker')):
            participant = c.execute('SELECT u.*,p.persona FROM ec_users u JOIN ec_demo_profiles p ON p.user_id=u.id WHERE u.id=?',(person,)).fetchone()
            if not participant or not participant['active'] or participant['persona'] != persona or participant['role'] != role or participant['site'] != run['site']:
                raise HTTPException(409,'Practice participants changed. Choose an available prototype account.')
        return dict(run)

    def expected(self,run,expected):
        if run['version'] != expected:
            raise HTTPException(409,'This practice run changed. Refresh before continuing.')

    def change(self,c,run,actor,action,values,detail=None):
        values = dict(values)
        values.update(version=run['version']+1,updated=time.time())
        c.execute('UPDATE ec_demo_runs SET '+','.join(f'{key}=?' for key in values)+' WHERE id=? AND version=?',(*values.values(),run['id'],run['version']))
        if c.execute('SELECT changes()').fetchone()[0] != 1:
            raise HTTPException(409,'This practice run changed. Refresh before continuing.')
        self.event(c,{'id':run['id'],'version':values['version']},actor,action,detail)

    def scenario(self,run):
        return json.loads(run['scenario_json'])

    def audio_url(self,run,key):
        return '/api/prototype/demos/'+run['id']+'/audio/'+key

    def fixture_audio(self,run,key):
        scenario = self.scenario(run)
        if key not in ('instruction',)+VARIANTS:
            raise HTTPException(404,'Sample audio not found.')
        relative = 'audio/'+scenario['id']+'/'+key+'.wav'
        transcript = scenario['instruction']['transcript'] if key=='instruction' else scenario['variants'][key]['transcript']
        try:
            root = self.assets_dir.resolve()
            path = (root / relative).resolve()
            path.relative_to(root)
            manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            metadata = manifest['files'][relative]
            if manifest['schema_version'] != 1 or metadata['mime'] != 'audio/wav' or type(metadata['bytes']) is not int or not 0 < metadata['bytes'] <= 8*1024*1024 or not re.fullmatch(r'[a-f0-9]{64}',metadata['sha256']) or metadata['transcript_sha256'] != hashlib.sha256(transcript.encode('utf-8')).hexdigest():
                raise ValueError
            if path.stat().st_size != metadata['bytes']:
                raise ValueError
            data = path.read_bytes()
            if len(data) != metadata['bytes'] or validate_audio(data,'audio/wav') != metadata['sha256']:
                raise ValueError
        except (OSError,ValueError,TypeError,KeyError,InvalidAudio):
            raise HTTPException(503,'This prerecorded sample audio is unavailable. No practice decision was saved.') from None
        return path,relative,metadata['sha256']

    def result(self,c,run):
        scenario = self.scenario(run)
        stage = ('correct','incorrect') if run['status']=='awaiting_worker_example' else ('corrected','still_incorrect') if run['status']=='awaiting_corrected_example' else ()
        latest = None
        if run['latest_attempt_id']:
            row = c.execute('SELECT * FROM ec_demo_attempts WHERE id=? AND run_id=?',(run['latest_attempt_id'],run['id'])).fetchone()
            if row:
                latest = {'id':row['id'],'variant':row['variant'],'transcript':row['transcript'],'verification':json.loads(row['verification']),
                          'audio_url':self.audio_url(run,row['variant']),'ack_id':row['ack_id'],'created':timestamp(row['created']),
                          'source':'scripted_demo','synthetic_audio':True}
        history = [{'id':row['id'],'action':row['action'],'actor_id':row['actor_id'],'principal_id':row['principal_id'],'version':row['version'],
                    'detail':json.loads(row['detail']),'created':timestamp(row['created'])} for row in c.execute('SELECT * FROM ec_demo_events WHERE run_id=? ORDER BY id',(run['id'],))]
        live_checks = [self.live_view(scenario,dict(row)) for row in c.execute("SELECT * FROM ec_demo_live_checks WHERE run_id=? AND status='completed' ORDER BY created DESC LIMIT 4",(run['id'],))]
        return {'id':run['id'],'scenario_id':run['scenario_id'],'catalog_id':run['catalog_id'],'kind':'prerecorded_demo','source':'scripted_demo',
                'title':scenario['title'],'status':run['status'],'version':run['version'],'generation':run['generation'],
                'project_manager_id':run['project_manager_id'],'supervisor_id':run['supervisor_id'],'worker_reference':scenario['worker_reference'],
                'instruction':{'text':scenario['instruction']['transcript'],'transcript':scenario['instruction']['transcript'],
                               'requirements':scenario['instruction']['requirements'],'audio_url':self.audio_url(run,'instruction')},
                'available_variants':[{'id':key,'label':LABELS[key],'audio_url':self.audio_url(run,key),'transcript':scenario['variants'][key]['transcript']} for key in stage],
                'latest_attempt':latest,'correction_suggestion':scenario['correction_suggestion'],'correction_text':run['correction_text'],
                'pm_ok':bool(run['pm_ok']),'supervisor_ok':bool(run['supervisor_ok']),'real_release_enabled':False,'release_eligible':False,
                'provider_used':False,'synthetic_audio':True,'history':history,'live_checks':live_checks,'created':timestamp(run['created']),'updated':timestamp(run['updated']),
                'disclosure':'Prerecorded synthetic sample voices and scripted comparisons. This practice run is separate from live tasks and cannot release real work.'}

    def live_view(self,scenario,row):
        result = json.loads(row['result']) if row['result'] else {}
        return {'id':row['id'],'variant':row['variant'],'label':LABELS.get(row['variant'],row['variant']),'source':'live_gemini',
                'model':row['model'],'transcript':result.get('transcript',''),'verification':result.get('verification'),
                'decision':row['decision'],'expected_decision':row['expected_decision'],
                'agrees_with_prepared':row['decision'] == row['expected_decision'],'latency_ms':row['latency_ms'],
                'audio_url':'/api/prototype/demos/'+row['run_id']+'/audio/'+row['variant'],'created':timestamp(row['created']),
                'creates_evidence':False,'real_release_enabled':False,'disclosure':LIVE_DISCLOSURE}

    def current_attempt(self,c,run,attempt_id):
        if run['latest_attempt_id'] != attempt_id:
            raise HTTPException(409,'Choose the current practice attempt.')
        attempt = c.execute('SELECT * FROM ec_demo_attempts WHERE id=? AND run_id=? AND generation=?',(attempt_id,run['id'],run['generation'])).fetchone()
        if not attempt or attempt['ack_id'] != run['ack_id']:
            raise HTTPException(409,'This attempt belongs to an earlier practice stage.')
        return attempt


def demo_router(service,session,provider=None):
    router = APIRouter(prefix='/api/prototype/demos')

    def enabled():
        if service.settings.mode != 'prototype':
            raise HTTPException(404,'Not found.')

    def result(c,run_id):
        return service.result(c,dict(c.execute('SELECT * FROM ec_demo_runs WHERE id=?',(run_id,)).fetchone()))

    @router.get('')
    def list_runs(request:Request):
        enabled()
        # Seeding is idempotent and never rewrites an existing run or history.
        with service.store.transaction() as c:
            session(request,c)
        service.seed()
        with service.store.transaction() as c:
            actor = session(request,c)
            rows = c.execute('SELECT * FROM ec_demo_runs WHERE site=? ORDER BY scenario_id',(actor['site'],)).fetchall()
            return [service.result(c,service.assigned(c,row['id'],actor)) for row in rows if actor.get('is_admin_base') or actor['id'] in (row['project_manager_id'],row['supervisor_id'])]

    @router.get('/{run_id}')
    def get_run(run_id:str,request:Request):
        enabled()
        with service.store.transaction() as c:
            actor = session(request,c)
            return service.result(c,service.assigned(c,run_id,actor))

    @router.post('/{run_id}/attempt')
    def attempt(run_id:str,request:Request,body:DemoAttempt):
        enabled()
        with service.store.transaction() as c:
            actor = session(request,c,True)
            run = service.assigned(c,run_id,actor)
            service.expected(run,body.expected_version)
            if actor['id'] != run['supervisor_id']:
                raise HTTPException(403,'Only the assigned Supervisor can choose a worker example.')
            allowed = ('correct','incorrect') if run['status']=='awaiting_worker_example' else ('corrected','still_incorrect') if run['status']=='awaiting_corrected_example' else ()
            if body.variant not in allowed:
                raise HTTPException(409,'Acknowledge the discrepancy before choosing a fresh corrected example, or restart this practice run.')
            if run['status']=='awaiting_corrected_example':
                ack = c.execute('SELECT * FROM ec_demo_acks WHERE id=? AND run_id=? AND generation=?',(run['ack_id'],run_id,run['generation'])).fetchone()
                if not ack or ack['attempt_id'] != run['latest_attempt_id'] or ack['actor_id'] != run['supervisor_id']:
                    raise HTTPException(409,'The Supervisor must acknowledge the current discrepancy before a fresh example.')
            scenario = service.scenario(run)
            path,asset_path,asset_sha = service.fixture_audio(run,body.variant)
            instruction_path,instruction_asset,instruction_sha = service.fixture_audio(run,'instruction')
            example = scenario['variants'][body.variant]
            verification = validate_evaluation(scenario['instruction']['requirements'],example['transcript'],example['evaluation'])
            verification.update(source='scripted_demo',provider_used=False,real_release_enabled=False,instruction_asset_sha256=instruction_sha)
            attempt_id,now = 'demo_attempt_'+uid(),time.time()
            c.execute('INSERT INTO ec_demo_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(attempt_id,run_id,run['generation'],body.variant,example['transcript'],encode(verification),run['ack_id'],asset_path,asset_sha,actor['id'],actor.get('principal_id',actor['id']),now))
            service.change(c,run,actor,'demo_response_selected',{'latest_attempt_id':attempt_id,'pm_ok':0,'supervisor_ok':0,'status':'awaiting_approvals' if verification['passed'] else 'awaiting_supervisor_ack'},
                           {'attempt_id':attempt_id,'variant':body.variant,'passed':verification['passed'],'ack_id':run['ack_id'],'source':'scripted_demo'})
            return result(c,run_id)

    @router.post('/{run_id}/acknowledge')
    def acknowledge(run_id:str,request:Request,body:DemoAck):
        enabled()
        with service.store.transaction() as c:
            actor = session(request,c,True)
            run = service.assigned(c,run_id,actor)
            service.expected(run,body.expected_version)
            if actor['id'] != run['supervisor_id']:
                raise HTTPException(403,'Only the assigned Supervisor can acknowledge the correction.')
            current = service.current_attempt(c,run,body.attempt_id)
            if run['status'] != 'awaiting_supervisor_ack' or json.loads(current['verification'])['passed']:
                raise HTTPException(409,'Acknowledge the discrepancy in the current example.')
            correction = (body.correction or service.scenario(run)['correction_suggestion']).strip()
            if len(correction)<3:
                raise HTTPException(422,'Describe the correction explained in this practice run.')
            ack_id,now = 'demo_ack_'+uid(),time.time()
            c.execute('INSERT INTO ec_demo_acks VALUES(?,?,?,?,?,?,?,?)',(ack_id,run_id,run['generation'],body.attempt_id,actor['id'],actor.get('principal_id',actor['id']),correction,now))
            service.change(c,run,actor,'demo_correction_acknowledged',{'ack_id':ack_id,'correction_text':correction,'pm_ok':0,'supervisor_ok':0,'status':'awaiting_corrected_example'}, {'attempt_id':body.attempt_id,'ack_id':ack_id})
            return result(c,run_id)

    @router.post('/{run_id}/approve')
    def approve(run_id:str,request:Request,body:DemoApprove):
        enabled()
        if body.approved is not True:
            raise HTTPException(422,'Confirmation must be explicit.')
        with service.store.transaction() as c:
            actor = session(request,c,True)
            run = service.assigned(c,run_id,actor)
            service.expected(run,body.expected_version)
            if actor['id'] not in (run['project_manager_id'],run['supervisor_id']):
                raise HTTPException(403,'Choose the Project manager or Supervisor perspective to confirm this practice attempt.')
            current = service.current_attempt(c,run,body.attempt_id)
            if run['status'] != 'awaiting_approvals' or json.loads(current['verification'])['passed'] is not True:
                raise HTTPException(409,'A current passing practice example is required.')
            audio_path,relative,audio_sha = service.fixture_audio(run,current['variant'])
            instruction_path,instruction_relative,instruction_sha = service.fixture_audio(run,'instruction')
            if audio_sha != current['asset_sha256'] or instruction_sha != json.loads(current['verification']).get('instruction_asset_sha256'):
                raise HTTPException(409,'The prerecorded sample changed. Restart this practice run before confirming.')
            role = 'project_manager' if actor['id']==run['project_manager_id'] else 'supervisor'
            already = run['pm_ok'] if role=='project_manager' else run['supervisor_ok']
            if already:
                raise HTTPException(409,'You already confirmed this practice attempt.')
            c.execute('INSERT INTO ec_demo_approvals VALUES(?,?,?,?,?,?,?,?,?)',('demo_approval_'+uid(),run_id,run['generation'],body.attempt_id,actor['id'],actor.get('principal_id',actor['id']),role,1,time.time()))
            values = {'pm_ok':1} if role=='project_manager' else {'supervisor_ok':1}
            if (run['pm_ok'] or role=='project_manager') and (run['supervisor_ok'] or role=='supervisor'):
                values['status'] = 'demo_complete'
            service.change(c,run,actor,'demo_confirmation_saved',values,{'attempt_id':body.attempt_id,'role':role,'real_release_enabled':False})
            return result(c,run_id)

    @router.post('/{run_id}/restart')
    def restart(run_id:str,request:Request,body:DemoBody):
        enabled()
        with service.store.transaction() as c:
            actor = session(request,c,True)
            run = service.assigned(c,run_id,actor)
            service.expected(run,body.expected_version)
            if actor['id'] not in (run['project_manager_id'],run['supervisor_id']):
                raise HTTPException(403,'Choose an assigned perspective before restarting this practice run.')
            service.change(c,run,actor,'demo_restarted',{'generation':run['generation']+1,'status':'awaiting_worker_example','latest_attempt_id':None,'ack_id':None,'pm_ok':0,'supervisor_ok':0,'correction_text':None}, {'previous_attempt_id':run['latest_attempt_id'],'previous_generation':run['generation']})
            return result(c,run_id)

    @router.post('/{run_id}/live-check')
    async def live_check(run_id:str,request:Request,body:DemoLiveCheck):
        """Send one synthetic sample to the live provider. Never touches demo state or live evidence."""
        enabled()
        if provider is None or not provider.configured:
            raise HTTPException(503,'Live Gemini processing is not configured on this server. The prepared demo comparison remains available.')
        with service.store.transaction() as c:
            actor = session(request,c,True,allow_admin=True)
            run = service.assigned(c,run_id,actor)
            limit = service.store.enforce_live_budget(c,actor['session_id'])
            if limit:
                raise HTTPException(429,limit)
            path,asset_path,asset_sha = service.fixture_audio(run,body.variant)
            scenario = service.scenario(run)
            expected = scenario['variants'][body.variant]['expected_decision']
            check_id,now = 'demo_live_'+uid(),time.time()
            c.execute('INSERT INTO ec_demo_live_checks(id,run_id,variant,actor_id,principal_id,session_id,status,model,expected_decision,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (check_id,run_id,body.variant,actor['id'],actor.get('principal_id',actor['id']),actor['session_id'],'started',getattr(provider,'model',None),expected,now))
            service.event(c,run,actor,'demo_live_check_started',{'check_id':check_id,'variant':body.variant,'source':'live_gemini','creates_evidence':False})
        data = path.read_bytes()
        requirements = scenario['instruction']['requirements']
        started = time.monotonic()

        def finish(status,detail,values=None):
            with service.store.transaction() as c:
                values = dict(values or {})
                c.execute('UPDATE ec_demo_live_checks SET status=?,decision=?,result=?,latency_ms=?,finished=? WHERE id=?',
                          (status,values.get('decision'),values.get('result'),values.get('latency_ms'),time.time(),check_id))
                current = dict(c.execute('SELECT * FROM ec_demo_runs WHERE id=?',(run_id,)).fetchone())
                service.event(c,current,actor,'demo_live_check_'+status,detail)
                return dict(c.execute('SELECT * FROM ec_demo_live_checks WHERE id=?',(check_id,)).fetchone())

        try:
            transcript = await asyncio.wait_for(provider.transcribe(data,'audio/wav'),timeout=LIVE_CHECK_SECONDS)
            remaining = max(1.0,LIVE_CHECK_SECONDS-(time.monotonic()-started))
            result = await asyncio.wait_for(provider.evaluate(requirements,transcript),timeout=remaining)
            if not isinstance(result,dict):
                raise InvalidProviderResponse('The live comparison was incomplete.')
            verification = validate_evaluation(requirements,transcript,{k:v for k,v in result.items() if k not in ('passed','decision')})
        except asyncio.CancelledError:
            finish('failed',{'check_id':check_id,'reason':'cancelled'})
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc,(InvalidProviderResponse,ProviderUnavailable)) else 'The live Gemini check failed. Try again later; the prepared comparison remains available.'
            finish('failed',{'check_id':check_id,'variant':body.variant,'reason':message})
            raise HTTPException(503,message) from None
        latency = int((time.monotonic()-started)*1000)
        stored = finish('completed',{'check_id':check_id,'variant':body.variant,'decision':verification['decision'],'expected_decision':expected,
                                     'agrees_with_prepared':verification['decision']==expected,'latency_ms':latency,'source':'live_gemini'},
                        {'decision':verification['decision'],'latency_ms':latency,
                         'result':encode({'transcript':transcript,'verification':verification,'asset_sha256':asset_sha})})
        return service.live_view(scenario,stored)

    @router.get('/{run_id}/audio/{key}')
    def audio(run_id:str,key:str,request:Request):
        enabled()
        with service.store.transaction() as c:
            actor = session(request,c)
            run = service.assigned(c,run_id,actor)
            path,asset_path,sha = service.fixture_audio(run,key)
        return FileResponse(path,media_type='audio/wav',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'inline'})

    return router
