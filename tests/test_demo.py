"""Saved synthetic samples and scripted checks stay separate from real evidence."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shutil
import wave

import pytest
from fastapi.testclient import TestClient

from backend.audio import validate_audio
from backend.config import Settings
from backend.main import create_app
from backend.verification import validate_evaluation, validate_requirements
from test_prototype import PrototypeRig


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'dataset' / 'demo_scenarios_v1.json'
ASSETS = ROOT / 'backend' / 'demo_assets'
BASE = '/api/prototype/demos'
VARIANTS = {'correct', 'incorrect', 'corrected', 'still_incorrect'}
REAL_TABLES = ('ec_tasks', 'ec_consents', 'ec_recordings', 'ec_attempts', 'ec_acknowledgments', 'ec_approvals')


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


class ForbiddenLiveProvider:
    configured = False
    model = None

    def __init__(self):
        self.calls = []

    async def transcribe(self, *args):
        self.calls.append('transcribe')
        raise AssertionError('A saved demonstration must not transcribe live audio')

    async def extract_requirements(self, *args):
        self.calls.append('extract')
        raise AssertionError('A saved demonstration must not use live extraction')

    async def evaluate(self, *args):
        self.calls.append('evaluate')
        raise AssertionError('A saved demonstration must not call the live provider')


class DemoRig(PrototypeRig):
    def runs(self):
        response = self.client.get(BASE)
        assert response.status_code == 200, response.text
        return response.json()

    def run(self, run_id):
        response = self.client.get(BASE + '/' + run_id)
        assert response.status_code == 200, response.text
        return response.json()

    def action(self, run_id, action, **fields):
        current = self.run(run_id)
        return self.command(f'{BASE}/{run_id}/{action}', {'expected_version': current['version'], **fields})

    def choose(self, run_id, variant):
        response = self.action(run_id, 'attempt', variant=variant)
        assert response.status_code == 200, response.text
        return response.json()

    def ack(self, run_id):
        current = self.run(run_id)
        response = self.action(run_id, 'acknowledge', attempt_id=current['latest_attempt']['id'])
        assert response.status_code == 200, response.text
        return response.json()

    def confirm(self, run_id):
        current = self.run(run_id)
        return self.action(run_id, 'approve', attempt_id=current['latest_attempt']['id'], approved=True)

    def assert_no_real_evidence(self):
        with self.app.state.store.transaction() as connection:
            assert connection.execute('SELECT COUNT(*) FROM ec_users').fetchone()[0] == 3
            assert {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in REAL_TABLES} == {table: 0 for table in REAL_TABLES}
        assert self.provider.calls == []


@pytest.fixture
def demo(tmp_path):
    with ExitStack() as stack:
        settings = Settings(mode='prototype', db_path=tmp_path / 'demo.db', audio_dir=tmp_path / 'private', release_enabled=True)
        provider = ForbiddenLiveProvider()
        yield DemoRig(create_app(settings=settings, provider=provider), provider, stack)


@pytest.mark.parametrize('scenario_index', range(7))
def test_every_curated_scenario_has_complete_strict_expected_results(scenario_index):
    document = catalog()
    assert document['source'] == 'curated_expected_result'
    assert document['evidence_kind'] == 'scripted_demo_fixture'
    assert document['language'] == 'en' and len(document['scenarios']) == 7
    assert len({item['id'] for item in document['scenarios']}) == 7
    scenario = document['scenarios'][scenario_index]
    requirements = validate_requirements(scenario['instruction']['requirements'])
    assert 3 <= len(requirements) <= 4
    assert set(scenario['variants']) == VARIANTS
    assert len({item['transcript'] for item in scenario['variants'].values()}) == 4
    for key, example in scenario['variants'].items():
        checked = validate_evaluation(requirements, example['transcript'], example['evaluation'])
        assert checked['passed'] == example['expected_pass'] == (key in {'correct', 'corrected'})
        assert checked['decision'] == example['expected_decision']
        if key == 'incorrect':
            assert checked['decision'] == 'discrepancy'
        assert example['audio_asset']['path'] == f"audio/{scenario['id']}/{key}.wav"


def test_all_35_canonical_assets_are_valid_unique_wavs_bound_to_their_scripts():
    manifest = json.loads((ASSETS / 'manifest.json').read_text(encoding='utf-8'))
    expected = {}
    for scenario in catalog()['scenarios']:
        expected[scenario['instruction']['audio_asset']['path']] = scenario['instruction']
        expected.update({item['audio_asset']['path']: item for item in scenario['variants'].values()})
    assert len(expected) == 35 and set(manifest['files']) == set(expected)
    hashes = set()
    for relative, example in expected.items():
        metadata = manifest['files'][relative]
        path = ASSETS / relative
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert validate_audio(data, 'audio/wav') == digest == metadata['sha256'] == example['audio_asset']['sha256']
        assert metadata['bytes'] == len(data) > 0 and metadata['mime'] == 'audio/wav'
        assert metadata['transcript_sha256'] == hashlib.sha256(example['transcript'].encode('utf-8')).hexdigest()
        assert example['audio_asset']['origin'] == 'synthetic_tts'
        assert example['audio_asset']['generation_status'] == 'generated'
        with wave.open(str(path), 'rb') as recording:
            assert 1 <= recording.getnchannels() <= 2
            assert 0.1 <= recording.getnframes() / recording.getframerate() <= 120
        hashes.add(digest)
    assert len(hashes) == 35


def test_seven_preloaded_runs_seed_idempotently_without_accounts_or_real_consent(demo):
    first = demo.runs()
    assert len(first) == 7
    demo.app.state.demo_service.seed()
    demo.app.state.demo_service.seed()
    second = demo.runs()
    assert {item['id'] for item in first} == {item['id'] for item in second}
    for run in second:
        assert run['kind'] == 'prerecorded_demo' and run['source'] == 'scripted_demo'
        assert run['status'] == 'awaiting_worker_example'
        assert {choice['id'] for choice in run['available_variants']} == {'correct', 'incorrect'}
        assert run['latest_attempt'] is None and not run['pm_ok'] and not run['supervisor_ok']
        assert run['release_eligible'] is False and run['real_release_enabled'] is False
        assert run['provider_used'] is False
        assert run['history'][0]['action'] == 'demo_preloaded'
        assert run['history'][0]['detail']['human_instruction_approval'] is False
    demo.assert_no_real_evidence()


@pytest.mark.parametrize('scenario_index', range(7))
def test_repeated_incorrect_demo_requires_two_corrections_and_two_own_role_approvals(demo, scenario_index):
    run_id = demo.runs()[scenario_index]['id']
    admin_id, supervisor_id = demo.profile('admin')['id'], demo.profile('supervisor')['id']
    demo.switch('supervisor')
    wrong = demo.choose(run_id, 'incorrect')
    old_attempt = wrong['latest_attempt']['id']
    assert wrong['status'] == 'awaiting_supervisor_ack'
    demo.switch('manager')
    assert demo.action(run_id, 'acknowledge', attempt_id=old_attempt).status_code == 403
    demo.switch('supervisor')
    assert demo.action(run_id, 'attempt', variant='corrected').status_code == 409
    corrected_stage = demo.ack(run_id)
    assert corrected_stage['status'] == 'awaiting_corrected_example'
    assert demo.confirm(run_id).status_code == 409
    still_wrong = demo.choose(run_id, 'still_incorrect')
    assert still_wrong['latest_attempt']['id'] != old_attempt
    assert still_wrong['latest_attempt']['verification']['passed'] is False
    assert demo.action(run_id, 'attempt', variant='corrected').status_code == 409
    demo.ack(run_id)
    passing = demo.choose(run_id, 'corrected')
    assert passing['latest_attempt']['verification']['passed'] is True
    assert passing['latest_attempt']['source'] == 'scripted_demo'
    assert passing['latest_attempt']['synthetic_audio'] is True
    supervisor_confirmation = demo.confirm(run_id)
    assert supervisor_confirmation.status_code == 200, supervisor_confirmation.text
    assert supervisor_confirmation.json()['supervisor_ok'] and not supervisor_confirmation.json()['pm_ok']
    assert demo.confirm(run_id).status_code == 409
    demo.switch('manager')
    final = demo.confirm(run_id)
    assert final.status_code == 200, final.text
    complete = final.json()
    assert complete['status'] == 'demo_complete' and complete['pm_ok'] and complete['supervisor_ok']
    assert complete['release_eligible'] is False and complete['real_release_enabled'] is False
    acknowledgments = [row for row in complete['history'] if row['action'] == 'demo_correction_acknowledged']
    assert len(acknowledgments) == 2
    assert all(row['actor_id'] == supervisor_id and row['principal_id'] == admin_id for row in acknowledgments)
    assert len({row['detail']['ack_id'] for row in acknowledgments}) == 2
    demo.assert_no_real_evidence()


@pytest.mark.parametrize('variant', ['corrected', 'still_incorrect'])
def test_correction_samples_cannot_be_selected_before_a_supervisor_ack(demo, variant):
    run = demo.runs()[0]
    demo.switch('supervisor')
    assert demo.action(run['id'], 'attempt', variant=variant).status_code == 409
    assert demo.run(run['id'])['latest_attempt'] is None


@pytest.mark.parametrize('action,fields', [
    ('attempt', {'variant': 'correct'}), ('restart', {}),
    ('acknowledge', {'attempt_id': 'no-demo-attempt'}),
    ('approve', {'attempt_id': 'no-demo-attempt', 'approved': True}),
])
def test_base_admin_remains_read_only_for_all_practice_mutations(demo, action, fields):
    run_id = demo.runs()[0]['id']
    assert demo.action(run_id, action, **fields).status_code == 403
    assert demo.run(run_id)['version'] == 1


@pytest.mark.parametrize('scenario_index', range(7))
def test_each_initial_correct_sample_still_requires_both_own_role_confirmations(demo, scenario_index):
    run_id = demo.runs()[scenario_index]['id']
    demo.switch('supervisor')
    passing = demo.choose(run_id, 'correct')
    assert passing['status'] == 'awaiting_approvals' and passing['latest_attempt']['verification']['passed']
    assert not passing['pm_ok'] and not passing['supervisor_ok']
    assert demo.confirm(run_id).status_code == 200
    assert demo.run(run_id)['status'] == 'awaiting_approvals'
    demo.switch('manager')
    complete = demo.confirm(run_id)
    assert complete.status_code == 200, complete.text
    assert complete.json()['status'] == 'demo_complete'
    assert complete.json()['real_release_enabled'] is False and complete.json()['release_eligible'] is False
    demo.assert_no_real_evidence()


@pytest.mark.parametrize('action', ['approve', 'restart'])
def test_base_admin_cannot_change_even_a_current_passing_demo(demo, action):
    run_id = demo.runs()[0]['id']
    demo.switch('supervisor')
    passed = demo.choose(run_id, 'correct')
    demo.switch()
    fields = {'attempt_id': passed['latest_attempt']['id'], 'approved': True} if action == 'approve' else {}
    assert demo.action(run_id, action, **fields).status_code == 403
    untouched = demo.run(run_id)
    assert untouched['version'] == passed['version'] and untouched['history'] == passed['history']
    assert untouched['latest_attempt']['id'] == passed['latest_attempt']['id']
    assert not untouched['pm_ok'] and not untouched['supervisor_ok']


def test_project_manager_cannot_choose_the_workers_sample(demo):
    run_id = demo.runs()[0]['id']
    demo.switch('manager')
    assert demo.action(run_id, 'attempt', variant='correct').status_code == 403
    assert demo.run(run_id)['latest_attempt'] is None


def test_demo_actions_reject_stale_csrf_and_stale_run_versions(demo):
    run = demo.runs()[0]
    demo.switch('supervisor')
    previous_csrf = demo.identity()['csrf_token']
    demo.switch('manager')
    demo.switch('supervisor')
    stale = demo.client.post(f"{BASE}/{run['id']}/attempt", json={'expected_version': 1, 'variant': 'correct'},
                             headers={'X-CSRF-Token': previous_csrf})
    assert stale.status_code == 403
    selected = demo.choose(run['id'], 'incorrect')
    assert selected['version'] > run['version']
    assert demo.command(f"{BASE}/{run['id']}/restart", {'expected_version': run['version']}).status_code == 409
    assert demo.run(run['id'])['latest_attempt']['id'] == selected['latest_attempt']['id']


def test_restarting_a_demo_keeps_history_and_restart_does_not_reseed_it(demo):
    run_id = demo.runs()[0]['id']
    demo.switch('supervisor')
    selected = demo.choose(run_id, 'incorrect')
    demo.ack(run_id)
    demo.choose(run_id, 'still_incorrect')
    previous = demo.run(run_id)
    restarted = demo.action(run_id, 'restart')
    assert restarted.status_code == 200, restarted.text
    fresh = restarted.json()
    assert fresh['id'] == run_id and fresh['generation'] > previous['generation']
    assert fresh['status'] == 'awaiting_worker_example' and fresh['latest_attempt'] is None
    assert len(fresh['history']) > len(previous['history'])
    assert {row['id'] for row in previous['history']} <= {row['id'] for row in fresh['history']}
    assert demo.action(run_id, 'approve', attempt_id=selected['latest_attempt']['id'], approved=True).status_code == 409
    new_app = create_app(settings=demo.app.state.settings, provider=ForbiddenLiveProvider())
    cookie = '; '.join(f'{name}={demo.client.cookies.get(name)}' for name in ('ec_session', 'ec_device'))
    with TestClient(new_app, base_url=demo.app.state.settings.app_origin) as client:
        restored = client.get(f'{BASE}/{run_id}', headers={'Cookie': cookie})
        assert restored.status_code == 200, restored.text
        assert restored.json()['version'] == fresh['version']
        assert restored.json()['history'] == fresh['history']
        assert len(client.get(BASE, headers={'Cookie': cookie}).json()) == 7
    demo.assert_no_real_evidence()


def test_uncertain_scripted_understanding_remains_blocked_after_a_correction(demo):
    run = next(item for item in demo.runs() if item['scenario_id'] == 'demo-07-uncertain-quantity')
    demo.switch('supervisor')
    demo.choose(run['id'], 'incorrect')
    demo.ack(run['id'])
    uncertain = demo.choose(run['id'], 'still_incorrect')
    verification = uncertain['latest_attempt']['verification']
    assert verification['decision'] == 'review_required' and verification['passed'] is False
    assert uncertain['status'] == 'awaiting_supervisor_ack'
    assert demo.confirm(run['id']).status_code == 409


@pytest.mark.parametrize('mode', ['development', 'production'])
def test_demo_endpoints_are_absent_outside_prototype_mode(tmp_path, mode):
    settings = Settings(mode=mode, db_path=tmp_path / 'outside.db', audio_dir=tmp_path / 'private', app_origin='https://testserver')
    app = create_app(settings=settings, provider=ForbiddenLiveProvider())
    with TestClient(app, base_url=settings.app_origin, headers={'Origin': settings.app_origin}) as client:
        assert client.get(BASE).status_code == 404
        assert client.get(BASE + '/demo_unknown').status_code == 404
        assert client.get(BASE + '/demo_unknown/audio/correct').status_code == 404
        assert client.post(BASE + '/demo_unknown/attempt', json={'expected_version': 1, 'variant': 'correct'}).status_code == 404


def test_demo_attempts_cannot_be_used_at_real_task_approval_recording_or_release_routes(demo):
    run_id = demo.runs()[0]['id']
    demo.switch('supervisor')
    selected = demo.choose(run_id, 'correct')
    demo_attempt = selected['latest_attempt']['id']
    demo.switch('manager')
    assert demo.command(f'/api/tasks/{run_id}/release', {'expected_version': 1, 'instruction_revision': 1, 'attempt_id': demo_attempt}).status_code == 404
    real = demo.create()
    forged = demo.command(f"/api/tasks/{real['id']}/approve", {
        'expected_version': real['version'], 'instruction_revision': real['instruction_revision'],
        'attempt_id': demo_attempt, 'approved': True,
    })
    assert forged.status_code == 409
    assert demo.task(real['id'])['status'] == 'draft'
    disclosure = demo.client.get('/api/health').json()['recording_disclosure']
    assert demo.command(f'/api/tasks/{run_id}/recording-tickets', {
        'expected_version': 1, 'purpose': 'instruction', 'consent_accepted': True,
        'consent_version': disclosure['consent_version'], 'consent_policy_id': disclosure['policy_id'],
    }).status_code == 404
    with demo.app.state.store.transaction() as connection:
        for table in REAL_TABLES[1:]:
            assert connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    assert demo.provider.calls == []


def test_sample_audio_requires_authentication_and_a_known_allowlisted_variant(demo):
    run = demo.runs()[0]
    audio_url = run['instruction']['audio_url']
    with TestClient(demo.app, base_url=demo.app.state.settings.app_origin) as anonymous:
        assert anonymous.get(audio_url).status_code == 401
    for username in ('manager', 'supervisor'):
        demo.switch(username)
        response = demo.client.get(audio_url)
        assert response.status_code == 200 and response.content[:4] == b'RIFF'
        assert response.headers['cache-control'] == 'no-store'
        assert demo.client.get(f"{BASE}/{run['id']}/audio/manifest.json").status_code == 404
        assert demo.client.get(f"{BASE}/{run['id']}/audio/%2E%2E%2Fmanifest.json").status_code == 404
    supervisor_id = demo.profile('supervisor')['id']
    with demo.app.state.store.transaction() as connection:
        connection.execute('UPDATE ec_users SET site=? WHERE id=?', ('another-site', supervisor_id))
    assert demo.client.get(audio_url).status_code in (404, 409)


def temporary_asset_root(demo, tmp_path, run):
    root = tmp_path / 'isolated-demo-assets'
    root.mkdir()
    manifest = json.loads((ASSETS / 'manifest.json').read_text(encoding='utf-8'))
    for key in ('instruction', 'correct'):
        relative = f"audio/{run['scenario_id']}/{key}.wav"
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSETS / relative, destination)
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    service = demo.app.state.demo_service
    service.assets_dir = root
    service.manifest_path = root / 'manifest.json'
    return root, manifest


@pytest.mark.parametrize('problem', ['missing_instruction', 'missing_response', 'tampered_response', 'missing_manifest_entry'])
def test_unavailable_sample_assets_block_selection_without_saving_a_decision(demo, tmp_path, problem):
    run = demo.runs()[0]
    demo.switch('supervisor')
    root, manifest = temporary_asset_root(demo, tmp_path, run)
    relative = f"audio/{run['scenario_id']}/correct.wav"
    if problem == 'missing_instruction':
        (root / f"audio/{run['scenario_id']}/instruction.wav").unlink()
    elif problem == 'missing_response':
        (root / relative).unlink()
    elif problem == 'tampered_response':
        path = root / relative
        path.write_bytes(path.read_bytes() + b'tampered')
    else:
        del manifest['files'][relative]
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    response = demo.action(run['id'], 'attempt', variant='correct')
    assert response.status_code == 503, response.text
    current = demo.run(run['id'])
    assert current['version'] == run['version'] and current['latest_attempt'] is None
    assert current['history'] == run['history']
    demo.assert_no_real_evidence()


def test_missing_instruction_after_a_pass_blocks_final_demo_approvals(demo, tmp_path):
    run = demo.runs()[0]
    demo.switch('supervisor')
    root, _ = temporary_asset_root(demo, tmp_path, run)
    passed = demo.choose(run['id'], 'correct')
    (root / f"audio/{run['scenario_id']}/instruction.wav").unlink()
    response = demo.confirm(run['id'])
    assert response.status_code == 503, response.text
    current = demo.run(run['id'])
    assert current['version'] == passed['version'] and not current['supervisor_ok'] and not current['pm_ok']


def test_client_cannot_supply_its_own_scripted_verification_result(demo):
    run = demo.runs()[0]
    demo.switch('supervisor')
    response = demo.command(f"{BASE}/{run['id']}/attempt", {
        'expected_version': run['version'], 'variant': 'incorrect',
        'verification': {'passed': True, 'decision': 'passed'},
    })
    assert response.status_code == 422
    assert demo.run(run['id'])['latest_attempt'] is None
