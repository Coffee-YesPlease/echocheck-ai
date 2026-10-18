"""Live Gemini checks of prerecorded samples: practice only, bounded, never evidence."""
from __future__ import annotations

from contextlib import ExitStack

import pytest

from backend.config import Settings
from backend.main import create_app
from test_demo import BASE, REAL_TABLES, DemoRig, ForbiddenLiveProvider, catalog
from test_prototype import PrototypeRig
from test_workflow import FakeProvider

SCENARIOS = {scenario['id']: scenario for scenario in catalog()['scenarios']}


class ScriptedLiveProvider:
    """Answers like a live model would for the sample that was sent."""
    configured = True
    model = 'gemini-test-live'

    def __init__(self):
        self.calls, self.error, self.next = [], None, None

    async def transcribe(self, audio, mime_type):
        self.calls.append(('transcribe', mime_type, len(audio)))
        if self.error:
            raise self.error
        return self.next['transcript']

    async def extract_requirements(self, *args):
        raise AssertionError('Live sample checks never extract requirements')

    async def evaluate(self, requirements, transcript):
        self.calls.append(('evaluate', len(requirements)))
        return dict(self.next['evaluation'])


def rig(tmp_path, provider, **settings):
    stack = ExitStack()
    app = create_app(settings=Settings(mode='prototype', db_path=tmp_path / 'live.db', audio_dir=tmp_path / 'audio', **settings), provider=provider)
    return stack, DemoRig(app, provider, stack)


def prepare(demo, variant):
    run = demo.runs()[0]
    demo.provider.next = SCENARIOS[run['scenario_id']]['variants'][variant]
    return run


def live_check(demo, run, variant, client=None):
    return demo.command(f"{BASE}/{run['id']}/live-check", {'variant': variant}, client=client)


@pytest.mark.parametrize('variant', ['correct', 'incorrect', 'corrected', 'still_incorrect'])
def test_live_check_reports_gemini_result_without_touching_demo_or_live_evidence(tmp_path, variant):
    stack, demo = rig(tmp_path, ScriptedLiveProvider())
    with stack:
        run = prepare(demo, variant)
        response = live_check(demo, run, variant)
        assert response.status_code == 200, response.text
        result = response.json()
        expected = SCENARIOS[run['scenario_id']]['variants'][variant]['expected_decision']
        assert result['source'] == 'live_gemini' and result['model'] == 'gemini-test-live'
        assert result['decision'] == expected and result['agrees_with_prepared'] is True
        assert result['creates_evidence'] is False and result['real_release_enabled'] is False
        assert result['transcript'] and isinstance(result['latency_ms'], int)
        assert [call[0] for call in demo.provider.calls] == ['transcribe', 'evaluate']
        assert demo.provider.calls[0][1] == 'audio/wav'
        after = demo.run(run['id'])
        assert (after['version'], after['status'], after['latest_attempt']) == (run['version'], run['status'], None)
        assert after['live_checks'][0]['id'] == result['id']
        assert [event['action'] for event in after['history']][-2:] == ['demo_live_check_started', 'demo_live_check_completed']
        with demo.app.state.store.transaction() as c:
            assert {table: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in REAL_TABLES} == {table: 0 for table in REAL_TABLES}
            assert c.execute('SELECT COUNT(*) FROM ec_demo_attempts').fetchone()[0] == 0


def test_model_pass_flag_cannot_override_server_decision(tmp_path):
    stack, demo = rig(tmp_path, ScriptedLiveProvider())
    with stack:
        run = prepare(demo, 'incorrect')
        demo.provider.next = dict(demo.provider.next, evaluation=dict(demo.provider.next['evaluation'], passed=True, decision='passed'))
        result = live_check(demo, run, 'incorrect').json()
        assert result['decision'] != 'passed' and result['verification']['passed'] is False


def test_live_check_needs_a_configured_provider(tmp_path):
    stack, demo = rig(tmp_path, ForbiddenLiveProvider())
    with stack:
        run = demo.runs()[0]
        response = live_check(demo, run, 'correct')
        assert response.status_code == 503 and 'not configured' in response.json()['detail']
        assert demo.provider.calls == []
        with demo.app.state.store.transaction() as c:
            assert c.execute('SELECT COUNT(*) FROM ec_demo_live_checks').fetchone()[0] == 0


def test_live_check_requires_csrf_and_an_assigned_variant(tmp_path):
    stack, demo = rig(tmp_path, ScriptedLiveProvider())
    with stack:
        run = demo.runs()[0]
        assert demo.client.post(f"{BASE}/{run['id']}/live-check", json={'variant': 'correct'}).status_code == 403
        assert live_check(demo, run, 'instruction').status_code == 422
        assert demo.command(f"{BASE}/{run['id']}/live-check", {'variant': 'correct', 'passed': True}).status_code == 422
        assert demo.provider.calls == []


def test_provider_failure_is_recorded_without_leaking_details(tmp_path):
    stack, demo = rig(tmp_path, ScriptedLiveProvider())
    with stack:
        run = prepare(demo, 'correct')
        demo.provider.error = RuntimeError('upstream body with secret-token-123')
        response = live_check(demo, run, 'correct')
        assert response.status_code == 503 and 'secret' not in response.text
        with demo.app.state.store.transaction() as c:
            assert c.execute('SELECT status FROM ec_demo_live_checks').fetchone()['status'] == 'failed'
        assert demo.run(run['id'])['live_checks'] == []


def test_session_and_daily_limits_bound_live_checks(tmp_path):
    stack, demo = rig(tmp_path, ScriptedLiveProvider(), live_session_hourly_limit=2, live_daily_limit=3)
    with stack:
        run = prepare(demo, 'correct')
        assert live_check(demo, run, 'correct').status_code == 200
        demo.provider.error = RuntimeError('provider down')
        assert live_check(demo, run, 'correct').status_code == 503  # failures still count
        limited = live_check(demo, run, 'correct')
        assert limited.status_code == 429 and 'hourly' in limited.json()['detail']
        other = demo.client_for('supervisor')
        demo.provider.error = None
        assert live_check(demo, run, 'correct', client=other).status_code == 200
        daily = live_check(demo, run, 'correct', client=demo.client_for('manager'))
        assert daily.status_code == 429 and 'daily' in daily.json()['detail']


def test_recording_uploads_share_the_live_budget_but_cancelled_tickets_do_not(tmp_path):
    with ExitStack() as stack:
        settings = Settings(mode='prototype', db_path=tmp_path / 'p.db', audio_dir=tmp_path / 'a', live_session_hourly_limit=1)
        provider = FakeProvider()
        prototype = PrototypeRig(create_app(settings=settings, provider=provider), provider, stack)
        task = prototype.create()
        disclosure = prototype.client.get('/api/health').json()['recording_disclosure']

        def request_ticket(version):
            return prototype.command(f"/api/tasks/{task['id']}/recording-tickets", {
                'expected_version': version, 'purpose': 'instruction', 'consent_accepted': True,
                'consent_version': disclosure['consent_version'], 'consent_policy_id': disclosure['policy_id']})

        # Issuing and cancelling makes no provider call, so a loop of it cannot drain the shared allowance.
        for _ in range(3):
            current = prototype.task(task['id'])
            assert request_ticket(current['version']).status_code == 200
            current = prototype.task(task['id'])
            cancel = prototype.command(f"/api/recording-tickets/{current['active_ticket']['id']}/cancel", {'expected_version': current['version']})
            assert cancel.status_code == 200, cancel.text
        prototype.record(task['id'], 'instruction', 'Pack exactly twelve cartons.')
        assert request_ticket(prototype.task(task['id'])['version']).status_code == 429


def test_live_check_route_is_absent_outside_prototype(tmp_path):
    app = create_app(settings=Settings(mode='production', app_origin='https://testserver', db_path=tmp_path / 'x.db', audio_dir=tmp_path / 'a'), provider=ScriptedLiveProvider())
    from fastapi.testclient import TestClient
    with TestClient(app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        assert client.post(BASE + '/demo_x/live-check', json={'variant': 'correct'}).status_code == 404


def test_tickets_issued_in_advance_cannot_exceed_the_budget_at_upload(tmp_path):
    with ExitStack() as stack:
        settings = Settings(mode='prototype', db_path=tmp_path / 'p.db', audio_dir=tmp_path / 'a', live_session_hourly_limit=1)
        provider = FakeProvider()
        prototype = PrototypeRig(create_app(settings=settings, provider=provider), provider, stack)
        first, second = prototype.create(), prototype.create()
        first_ticket, second_ticket = prototype.ticket(first['id'], 'instruction'), prototype.ticket(second['id'], 'instruction')
        assert prototype.upload(first['id'], first_ticket, provider.speech('Pack exactly twelve cartons.')).status_code == 200
        before = provider.sequence
        limited = prototype.upload(second['id'], second_ticket, provider.speech('Pack exactly ten cartons.'))
        assert limited.status_code == 429 and 'hourly' in limited.json()['detail']
        assert prototype.task(second['id'])['active_ticket']['status'] == 'issued'  # nothing was consumed or sent
