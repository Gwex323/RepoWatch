"""Exercise SDK output recovery and durable acceptance scheduling without a provider."""

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from agents import OpenAIChatCompletionsModel
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from repowatch.acceptance import Acceptance
from repowatch.acceptance_failures import failure_code, failure_details
from repowatch.acceptance_model import run_acceptance
from repowatch.acceptance_store import initialize, progress
from repowatch.analysis_model import ObservedModel, OutputTruncated, observe_response
from repowatch.store import Conflict
from repowatch.task_scopes import digest


@pytest.mark.parametrize("always_truncated", [False, True])
def test_acceptance_recovers_truncation_once_on_the_actual_sdk_wire(always_truncated):
    requests, events, observation = [], [], {}
    verdict = {"met": False, "behavioral_failure": False, "rationale": "Missing live service.",
               "corrected_files": []}

    def record(*event):
        events.append(event)

    async def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        truncated = always_truncated or len(requests) == 1
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "length" if truncated else "stop",
                         "message": {"role": "assistant", "content": None if truncated
                                     else json.dumps(verdict)}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 12000 if truncated else 30,
                      "total_tokens": 12020 if truncated else 50},
        })

    async def observe(response):
        await observe_response(response, observation, record)

    async def run():
        async with AsyncOpenAI(
            api_key="fixture-secret", base_url="https://fixture.example/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),
                                          event_hooks={"response": [observe]}),
        ) as client:
            model = ObservedModel(OpenAIChatCompletionsModel("fixture", client), observation)
            return await run_acceptance(model, {"phase": "judge", "requirement": "unchanged"},
                                        record, reasoning_effort="high")

    if always_truncated:
        with pytest.raises(Conflict, match="acceptance_output_truncated"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == verdict
    assert len(requests) == 2
    assert [request["max_tokens"] for request in requests] == [12000, 16000]
    assert [request["reasoning_effort"] for request in requests] == ["high", "low"]
    assert all(request["model"] == "fixture" for request in requests)
    assert requests[0]["messages"][-1] == requests[1]["messages"][-1]
    assert sum(kind == "output_retry" for kind, _ in events) == 1
    assert sum(kind == "request_started" for kind, _ in events) == 2
    assert "fixture-secret" not in json.dumps(events)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    database = tmp_path / "acceptance.db"

    @contextmanager
    def connection():
        db = sqlite3.connect(database)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with connection() as db:
        db.executescript("""
            CREATE TABLE repository_analyses(id TEXT,version INTEGER,env_id TEXT);
            INSERT INTO repository_analyses VALUES('analysis',7,'environment');
            CREATE TABLE task_scopes(id TEXT,version INTEGER);
            INSERT INTO task_scopes VALUES('scope',3);
            CREATE TABLE analysis_task_scopes(analysis_id TEXT,scope_id TEXT);
            INSERT INTO analysis_task_scopes VALUES('analysis','scope');
            CREATE TABLE task_scope_events(scope_id,version,kind,actor,value,created);
        """)
        initialize(db)
        db.execute(
            "INSERT INTO scope_acceptance_runs "
            "(id,scope_id,version,analysis_id,basis,basis_hash,state,owner,attempt,deadline,"
            "model_identity,model_calls,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('run', 'scope', 3, 'analysis', '{}', 'basis', 'running', 'worker', 1,
             2000, digest({}), 1, 1000, 1000),
        )
    runtime = SimpleNamespace(readiness=lambda: {"ready": True})
    analyses = SimpleNamespace(store=SimpleNamespace(connection=connection),
                               execution=SimpleNamespace(runtime=runtime))
    engine = SimpleNamespace(status=lambda: {"enabled": True, "role_models": {}})
    worker = Acceptance(analyses, None, engine)
    worker.owner = 'worker'
    monkeypatch.setattr(worker, '_authorized', lambda *_: True)
    monkeypatch.setattr(worker, '_busy', lambda *_: False)
    monkeypatch.setattr(worker, '_basis', lambda *_: {})
    monkeypatch.setattr(worker, '_repairs', lambda *_: [])
    monkeypatch.setattr('repowatch.acceptance.time.time', lambda: 1100)
    return worker, connection


def test_transient_failure_preserves_budget_and_saved_result(controller, monkeypatch):
    worker, connection = controller
    with connection() as db:
        db.execute(
            "INSERT INTO scope_acceptance_checks "
            "(id,run_id,item_key,kind,state,attempt,specification,result,created,updated) "
            "VALUES('check','run','item','integration','running',1,'{}','saved result',1000,1000)"
        )
    error = APIConnectionError(request=httpx.Request('POST', 'https://fixture.example'))
    worker._stop({'id': 'run', 'analysis_id': 'analysis'}, error)
    with connection() as db:
        first = dict(db.execute("SELECT * FROM scope_acceptance_runs").fetchone())
    assert first['state'] == 'retry_wait'
    assert first['retry_after'] == 1110
    assert first['deadline'] == 2000 and first['model_calls'] == 1 and first['attempt'] == 1
    assert worker.claim() is None
    monkeypatch.setattr('repowatch.acceptance.time.time', lambda: 1111)
    resumed = worker.claim()
    assert resumed['attempt'] == 2 and resumed['model_calls'] == 1 and resumed['deadline'] == 2000
    worker._stop(resumed, error)
    with connection() as db:
        final = db.execute("SELECT * FROM scope_acceptance_runs").fetchone()
        assert final['state'] == 'failed'
        assert db.execute("SELECT result FROM scope_acceptance_checks").fetchone()[0] == (
            'saved result'
        )
        assert db.execute("SELECT COUNT(*) FROM scope_acceptance_failures").fetchone()[0] == 2
    assert worker.claim() is None


@pytest.mark.parametrize('field,value', [('attempt', 2), ('model_calls', 12), ('deadline', 1101)])
def test_transient_error_cannot_bypass_persisted_limits(controller, field, value):
    worker, connection = controller
    with connection() as db:
        db.execute(f'UPDATE scope_acceptance_runs SET {field}=?', (value,))
    worker._stop({'id': 'run', 'analysis_id': 'analysis'}, TimeoutError('secret body'))
    with connection() as db:
        assert db.execute('SELECT state FROM scope_acceptance_runs').fetchone()[0] == 'failed'


def test_owner_continuation_reopens_expired_time_window_without_resetting_counts(controller):
    worker, connection = controller
    with connection() as db:
        db.execute("UPDATE scope_acceptance_runs SET state='failed',deadline=1050,model_calls=5,"
                   "error='acceptance_execution_failed'")
    assert worker.retry('analysis', 7) == {'id': 'run', 'state': 'queued'}
    with connection() as db:
        row = db.execute('SELECT * FROM scope_acceptance_runs').fetchone()
        assert row['deadline'] == 2900 and row['model_calls'] == 5 and row['attempt'] == 1
        event = json.loads(db.execute('SELECT value FROM task_scope_events').fetchone()[0])
        assert event['previous_deadline'] == 1050 and event['previous_error']
    assert worker.claim()['attempt'] == 2
    worker._stop({'id': 'run', 'analysis_id': 'analysis'}, OutputTruncated('secret body'))
    with pytest.raises(Conflict, match='acceptance_model_budget'):
        worker.retry('analysis', 7)


@pytest.mark.parametrize('gate,code', [
    ('version', 'analysis_changed'), ('authorization', 'acceptance_basis_changed'),
    ('basis', 'acceptance_basis_changed'), ('busy', 'analysis_busy'),
    ('model', 'acceptance_model_changed'),
])
def test_continuation_checks_current_authorization(controller, monkeypatch, gate, code):
    worker, connection = controller
    with connection() as db:
        db.execute("UPDATE scope_acceptance_runs SET state='failed'")
    if gate == 'authorization':
        monkeypatch.setattr(worker, '_authorized', lambda *_: False)
    elif gate == 'basis':
        monkeypatch.setattr(worker, '_basis', lambda *_: {'changed': True})
    elif gate == 'busy':
        monkeypatch.setattr(worker, '_busy', lambda *_: True)
    elif gate == 'model':
        monkeypatch.setattr(worker.engine, 'status', lambda: {
            'enabled': True, 'role_models': {'x': 1},
        })
    with pytest.raises(Conflict, match=code):
        worker.retry('analysis', 6 if gate == 'version' else 7)
    with connection() as db:
        assert db.execute('SELECT state FROM scope_acceptance_runs').fetchone()[0] == 'failed'


def test_failure_history_excludes_messages_urls_and_unknown_exception_class_names(controller):
    worker, connection = controller
    secret = 'sk-test-password-provider-response'
    request = httpx.Request('POST', 'https://fixture.example/' + secret)
    error = APIStatusError(
        secret, response=httpx.Response(401, request=request), body={'key': secret},
    )
    worker._stop({'id': 'run', 'analysis_id': 'analysis'}, error)
    with connection() as db:
        snapshot = progress(db, {'id': 'scope', 'version': 3})
        assert snapshot['error'] == 'acceptance_authentication_failed'
        assert snapshot['failure']['details']['http_statuses'] == [401]
        assert secret not in json.dumps(snapshot)
        events = [tuple(row) for row in db.execute('SELECT * FROM task_scope_events')]
        assert secret not in json.dumps(events)
    assert failure_code(OutputTruncated(secret)) == 'acceptance_output_truncated'
    assert secret not in json.dumps(failure_details(type(secret, (Exception,), {})(secret)))


def test_migration_keeps_historical_run_and_does_not_requeue_unknown_failure(controller):
    worker, connection = controller
    with connection() as db:
        db.execute(
            "UPDATE scope_acceptance_runs SET state='failed',error='acceptance_execution_failed'"
        )
        db.execute('DROP TABLE scope_acceptance_failures')
        db.execute('ALTER TABLE scope_acceptance_runs DROP COLUMN retry_after')
        initialize(db)
        initialize(db)
        assert db.execute('SELECT COUNT(*) FROM scope_acceptance_runs').fetchone()[0] == 1
    assert worker.claim() is None


def test_acceptance_retry_api_requires_owner_session_and_csrf(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from repowatch.app import create_app

    app = create_app(tmp_path / 'api.db', start_worker=False)
    called = []
    monkeypatch.setattr(app.state.acceptance, 'retry', lambda *args: called.append(args) or {
        'id': 'run', 'state': 'queued',
    })
    with TestClient(app, base_url='http://127.0.0.1') as client:
        path = '/api/analyses/analysis/acceptance/retry'
        assert client.post(path, json={'version': 7}).status_code == 401
        login = client.post('/api/setup', json={'password': 'owner-test-password'})
        assert client.post(path, json={'version': 7}).status_code == 403
        response = client.post(path, json={'version': 7}, headers={
            'X-CSRF-Token': login.json()['csrf'],
        })
        assert response.status_code == 202 and response.json()['state'] == 'queued'
        assert called == [('analysis', 7)]


def test_every_retry_spends_the_same_request_budget_and_metadata_is_allowlisted(controller):
    worker, connection = controller
    worker._event({'id': 'run', 'analysis_id': 'analysis'}, 'provider_response', json.dumps({
        'output_tokens': 12000, 'reasoning_tokens': 11990, 'truncated': True,
        'finish_reason': ['untrusted'], 'body': 'secret model output', 'api_key': 'secret key',
    }))
    with connection() as db:
        recorded = json.loads(db.execute('SELECT value FROM task_scope_events').fetchone()[0])
        assert recorded['truncated'] is True and recorded['output_tokens'] == 12000
        assert 'secret' not in json.dumps(recorded) and 'finish_reason' not in recorded
        db.execute('UPDATE scope_acceptance_runs SET model_calls=12')
    with pytest.raises(Conflict, match='acceptance_model_budget'):
        worker._event({'id': 'run', 'analysis_id': 'analysis'}, 'request_started', 'role')
    with connection() as db:
        assert db.execute('SELECT model_calls FROM scope_acceptance_runs').fetchone()[0] == 12
