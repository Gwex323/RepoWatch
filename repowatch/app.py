import asyncio
import mimetypes
import os
import platform
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .acceptance import Acceptance
from .analysis_model import AnalysisModel
from .diagnosis import AnalysisModelDiagnoser
from .execution import Execution
from .github_accounts import GitHubAccounts, GitHubError
from .github_issues import GitHubIssues
from .memory import Memory
from .memory_evaluation import MemoryEvaluations
from .model_providers import ModelProviders
from .provider_probe import ProviderProbe
from .repairs import REPAIR_MAX_ATTEMPTS, Repairs
from .repository_analysis import MAX_TURNS, Analyses
from .repository_source import ExecutionError
from .roles import ROLES
from .schedules import RepositorySchedules
from .scope_models import ScopeRequirement
from .store import SDK_VERSION, Conflict, Missing, Store
from .task_scopes import TaskScopes

ROOT = Path(__file__).resolve().parent.parent


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordBody(StrictBody):
    password: str = Field(min_length=10, max_length=128)


class RepositoryBody(StrictBody):
    url: str = Field(max_length=300)


class MemoryBody(StrictBody):
    category: Literal["architecture", "setup", "testing"]
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=4000)
    revision: str = Field(min_length=1, max_length=128)


class MemoryEditBody(MemoryBody):
    version: int = Field(ge=1, strict=True)


class MemoryVersionBody(StrictBody):
    version: int = Field(ge=1, strict=True)


class GitHubConnectBody(StrictBody):
    username: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    token: SecretStr = Field(min_length=20, max_length=512)


class GitHubVersionBody(StrictBody):
    version: int = Field(ge=1, le=2**63 - 1, strict=True)


class AcceptanceRetryBody(GitHubVersionBody):
    additional_attempt: bool = Field(default=False, strict=True)
    run_id: str | None = Field(default=None, min_length=1, max_length=80)


class GitHubRepositoryBody(GitHubVersionBody):
    repo_id: str = Field(min_length=1, max_length=100)


class IssueAnalysisBody(StrictBody):
    env_id: str | None = Field(default=None, min_length=1, max_length=100)


class DownloadBody(StrictBody):
    auto_run: bool = Field(default=True, strict=True)


class ExecutionConfigBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    language: Literal["python", "go", "typescript"]
    install: str = Field(max_length=4000)
    test: str = Field(max_length=4000)
    start: str = Field(max_length=4000)
    port: int = Field(ge=1024, le=65535, strict=True)
    install_network: bool = False
    run_mode: Literal["service", "test_only"] = "service"
    continue_pipeline: bool = Field(default=False, strict=True)
    desktop_offline_confirmed: bool = Field(default=False, strict=True)


class AnalysisModelBody(StrictBody):
    version: int = Field(ge=0, strict=True)
    enabled: bool
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["responses", "chat_completions"]
    reasoning_effort: Literal["auto", "default", "low", "medium", "high", "max"] = "auto"
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1024)


class AgentModelBody(StrictBody):
    version: int = Field(ge=0, strict=True)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["responses", "chat_completions"]
    reasoning_effort: Literal["auto", "default", "low", "medium", "high", "max"] = "auto"
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1024)


class ModelEnabledBody(StrictBody):
    version: int = Field(ge=0, strict=True)
    enabled: bool = Field(strict=True)


class ProviderModelBody(StrictBody):
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: Literal["auto", "default", "low", "medium", "high", "max"] = "auto"


class ProviderBody(StrictBody):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["responses", "chat_completions"] = "chat_completions"
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1024)
    models: list[ProviderModelBody] = Field(min_length=1, max_length=40)


class ProviderSaveBody(ProviderBody):
    version: int = Field(ge=0, strict=True)


class ProviderImportBody(StrictBody):
    version: int = Field(ge=0, strict=True)
    providers: list[ProviderBody] = Field(min_length=1, max_length=25)


class ProviderVersionBody(StrictBody):
    version: int = Field(ge=0, strict=True)


class ProviderProbeBody(ProviderVersionBody):
    provider_id: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["responses", "chat_completions"] = "chat_completions"
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1024)
    model: str = Field(default="", max_length=200)


class ProviderModelChoice(StrictBody):
    provider_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class DirectModelChoice(StrictBody):
    direct: Literal[True]


class RoleSelectionBody(ProviderVersionBody):
    choice: ProviderModelChoice | DirectModelChoice | None


class DefaultSelectionBody(ProviderVersionBody):
    choice: ProviderModelChoice | None


class AgentSelectionsBody(ProviderVersionBody):
    enabled: bool = Field(strict=True)
    routes: dict[str, ProviderModelChoice | DirectModelChoice | None] = Field(max_length=6)


class FindingFeedbackBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    decision: Literal["confirmed", "not_reproduced"]
    personally_reproduced: bool = False
    note: str = Field(default="", max_length=2000)


class FindingDispositionBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    decision: Literal["not_reproduced", "resolved"]
    note: str = Field(default="", max_length=2000)


class RepairSettingsBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    enabled: bool = Field(strict=True)


class ScopeRequirementsBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    items: list[ScopeRequirement] = Field(min_length=1, max_length=32)


class ScopeObservationBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    kind: Literal["regression", "integration", "client"]
    passed: bool = Field(strict=True)
    personally_verified: bool = Field(strict=True)
    note: str = Field(min_length=1, max_length=2000)
    observation: str = Field(min_length=1, max_length=6000)
    repair_id: str | None = Field(default=None, max_length=100)
    repair_patch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ScheduleBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    enabled: bool = Field(strict=True)
    local_time: str = Field(min_length=5, max_length=5)


def create_app(
    db_path: Path | None = None, *, start_worker=True, github_client=None, credential_vault=None,
    execution_runtime=None, repository_source=None, execution_diagnoser=None, analysis_engine=None,
    repair_engine=None, repair_publisher=None, provider_probe_transport=None,
) -> FastAPI:
    # Windows registry MIME mappings may label .js as text/plain.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    store = Store(db_path or Path(os.getenv("REPOWATCH_DB", str(ROOT / "data/repowatch.db"))))
    memory = Memory(store)
    github = GitHubAccounts(store, client=github_client, vault=credential_vault)
    issues = GitHubIssues(store, github)
    task_scopes = TaskScopes(store)
    analysis_model = AnalysisModel(store, github)
    model_providers = ModelProviders(store, github)
    provider_probe = ProviderProbe(model_providers, transport=provider_probe_transport)
    execution = Execution(store, github, runtime=execution_runtime, source=repository_source,
                          diagnoser=execution_diagnoser or AnalysisModelDiagnoser(analysis_model))
    repairs = Repairs(
        execution, repair_engine or analysis_model.for_flow("repair"), github,
        publisher=repair_publisher,
    )
    analyses = Analyses(execution, analysis_engine or analysis_model, repairs)
    memory_evaluations = MemoryEvaluations(analyses, analysis_engine or analysis_model)
    schedules = RepositorySchedules(store, execution)
    repairs.analyses = analyses
    acceptance = Acceptance(analyses, repairs, analysis_model.for_flow("acceptance"))
    login_attempts: dict[str, deque] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        executor = asyncio.create_task(execution.run()) if start_worker else None
        diagnostics = asyncio.create_task(execution.diagnoses.run()) if start_worker else None
        analyst = asyncio.create_task(analyses.run()) if start_worker else None
        evaluation_worker = asyncio.create_task(
            memory_evaluations.run(),
        ) if start_worker else None
        repair_worker = asyncio.create_task(repairs.run()) if start_worker else None
        acceptance_worker = asyncio.create_task(acceptance.run()) if start_worker else None
        issue_worker = asyncio.create_task(issues.run()) if start_worker else None
        schedule_worker = asyncio.create_task(schedules.run()) if start_worker else None
        yield
        execution.closing.set()
        acceptance.closing.set()
        if acceptance_worker:
            await acceptance_worker
        if analyst:
            await analyst
        if evaluation_worker:
            memory_evaluations.closing.set()
            evaluation_worker.cancel()
            with suppress(asyncio.CancelledError):
                await evaluation_worker
        repairs.closing.set()
        if repair_worker:
            await repair_worker
        if diagnostics:
            await diagnostics
        if executor:
            await executor
        if issue_worker:
            issue_worker.cancel()
            with suppress(asyncio.CancelledError):
                await issue_worker
        if schedule_worker:
            schedules.closing.set()
            schedule_worker.cancel()
            with suppress(asyncio.CancelledError):
                await schedule_worker

    app = FastAPI(title="RepoWatch", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.execution = execution
    app.state.analyses = analyses
    app.state.analysis_model = analysis_model
    app.state.model_providers = model_providers
    app.state.memory_evaluations = memory_evaluations
    app.state.repairs = repairs
    app.state.acceptance = acceptance
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Cross-origin requests are not allowed."}, 403)
        public = path in ("/api/session", "/api/setup", "/api/login")
        session = store.session(request.cookies.get("rw_session", ""))
        if path.startswith("/api/") and not public:
            if not session:
                return JSONResponse({"detail": "Owner login is required."}, 401)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), session["csrf"]
            ):
                return JSONResponse({"detail": "Invalid confirmation session."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(Missing)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(GitHubError)
    async def github_error_handler(request, exc):
        return JSONResponse({"detail": exc.code}, exc.status)

    @app.exception_handler(ExecutionError)
    async def execution_error_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, 503)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Pydantic's default error payload can echo rejected secret input.
        if request.url.path.startswith("/api/github/"):
            return JSONResponse({"detail": "github_invalid_input"}, 422)
        if request.url.path.startswith(("/api/analysis-model", "/api/agent-models",
                                        "/api/model-providers")):
            return JSONResponse({"detail": "model_invalid_input"}, 422)
        return await request_validation_exception_handler(request, exc)

    @app.get("/api/session")
    async def session(request: Request):
        current = store.session(request.cookies.get("rw_session", ""))
        return {
            "setup_required": store.setting("password") is None,
            "authenticated": bool(current),
            "csrf": current["csrf"] if current else None,
        }

    def session_response(response: Response):
        token, csrf = store.create_session()
        response.set_cookie("rw_session", token, httponly=True, samesite="strict", max_age=43200)
        return {"authenticated": True, "csrf": csrf, "setup_required": False}

    @app.post("/api/setup")
    async def setup(body: PasswordBody, response: Response):
        store.setup_password(body.password)
        return session_response(response)

    @app.post("/api/login")
    async def login(body: PasswordBody, request: Request, response: Response):
        key = request.client.host if request.client else "local"
        attempts = login_attempts[key]
        now = time.monotonic()
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many login attempts. Please wait one minute.")
        attempts.append(now)
        if not store.check_password(body.password):
            raise HTTPException(401, "The owner password is incorrect.")
        attempts.clear()
        return session_response(response)

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        store.logout(request.cookies.get("rw_session", ""))
        response.delete_cookie("rw_session")
        return {"ok": True}

    @app.get("/api/dashboard")
    async def dashboard():
        return store.dashboard()

    @app.get("/api/schedules")
    def schedule_overview():
        return schedules.list()

    @app.get("/api/system")
    async def system():
        return {
            "phase": 3,
            "sdk_version": SDK_VERSION,
            "roles": ROLES,
            "agent_models": analysis_model.configuration(),
            "model_mode": "configured_provider",
            "external_model_calls": (execution.diagnoses.engine.status()["enabled"]
                                     or analyses.engine.status()["enabled"]
                                     or repairs.settings()["model_enabled"]),
            "repository_analysis": analyses.engine.status(),
            "execution_diagnosis": execution.diagnoses.engine.status(),
            "github_publication": repairs.settings()["enabled"],
            "automatic_repair": repairs.settings(),
            "github_account_binding": True,
            "external_repository_execution": True,
            "existing_issue_intake": True,
            "reproduction_policy": {
                "discovered": "human", "existing_issue": "human", "merge": "owner_only",
            },
            "schedule": schedules.summary(),
            "tracing_export": False,
            "storage": "SQLite",
            "runtime": "Python",
            "platform": platform.system(),
            "max_turns": MAX_TURNS,
            "max_attempts": REPAIR_MAX_ATTEMPTS,
            "memory": {"enabled": True, "scope": "repository_and_exact_revision",
                       "retrieval": "literal_relevance_and_recency", "semantic_search": False},
        }

    @app.get("/api/github/accounts")
    def github_accounts():
        return github.list()

    @app.get("/api/analysis-model")
    def analysis_model_status():
        return analysis_model.status()

    @app.post("/api/analysis-model")
    def configure_analysis_model(body: AnalysisModelBody):
        values = body.model_dump(exclude={"api_key"})
        return analysis_model.save(**values, api_key=body.api_key.get_secret_value())

    @app.get("/api/agent-models")
    def agent_models():
        return analysis_model.configuration()

    @app.post("/api/agent-models")
    def configure_agent_selections(body: AgentSelectionsBody):
        return model_providers.configure(**body.model_dump())

    @app.post("/api/agent-models/settings")
    def enable_agent_models(body: ModelEnabledBody):
        return analysis_model.enable(**body.model_dump())

    @app.post("/api/agent-models/{role}")
    def configure_agent_model(role: str, body: AgentModelBody):
        values = body.model_dump(exclude={"api_key"})
        return analysis_model.save_role(role, **values, api_key=body.api_key.get_secret_value())

    @app.post("/api/agent-models/{role}/selection")
    def select_agent_model(role: str, body: RoleSelectionBody):
        if role == "default":
            raise Conflict("model_agent_invalid")
        return model_providers.select(role=role, **body.model_dump())

    @app.get("/api/model-providers")
    def provider_catalog():
        return model_providers.state()

    def provider_values(body):
        return {**body.model_dump(exclude={"api_key", "version"}),
                "api_key": body.api_key.get_secret_value()}

    @app.post("/api/model-providers")
    def create_provider(body: ProviderSaveBody):
        return model_providers.save(version=body.version, **provider_values(body))

    @app.post("/api/model-providers/import")
    def import_providers(body: ProviderImportBody):
        return model_providers.import_providers(
            version=body.version, providers=[provider_values(p) for p in body.providers],
        )

    @app.post("/api/model-providers/default")
    def select_default_model(body: DefaultSelectionBody):
        return model_providers.select(role="default", **body.model_dump())

    def probe_values(body):
        return {**body.model_dump(exclude={"api_key"}),
                "api_key": body.api_key.get_secret_value()}

    @app.post("/api/model-providers/discover")
    async def discover_provider_models(body: ProviderProbeBody):
        return await provider_probe.discover(**probe_values(body))

    @app.post("/api/model-providers/test-connection")
    async def test_provider_connection(body: ProviderProbeBody):
        return await provider_probe.test_connection(**probe_values(body))

    @app.post("/api/model-providers/{provider_id}/delete")
    def delete_provider(provider_id: str, body: ProviderVersionBody):
        return model_providers.delete(provider_id, body.version)

    @app.post("/api/model-providers/{provider_id}")
    def update_provider(provider_id: str, body: ProviderSaveBody):
        return model_providers.save(version=body.version, provider_id=provider_id,
                                    **provider_values(body))

    @app.get("/api/repair-settings")
    def repair_settings():
        return repairs.settings()

    @app.post("/api/repair-settings")
    def configure_repairs(body: RepairSettingsBody):
        return repairs.configure(body.version, body.enabled)

    @app.get("/api/analysis-notifications")
    def analysis_notifications():
        enabled = repairs.settings()["enabled"]
        with store.connection() as db:
            return [{**dict(row), "repairs_enabled": enabled} for row in db.execute(
                "SELECT a.repo_id,r.name AS repository,a.status,"
                "(SELECT count(*) FROM analysis_findings f WHERE f.analysis_id=a.id "
                "AND f.status='waiting_reproduction') AS waiting,"
                "(SELECT count(*) FROM repair_tasks t WHERE t.analysis_id=a.id "
                "AND t.status='queued') AS repair_queued,"
                "(SELECT count(*) FROM repair_tasks t WHERE t.analysis_id=a.id "
                "AND t.status='running') AS repair_running,"
                "(SELECT count(*) FROM repair_tasks t WHERE t.analysis_id=a.id "
                "AND t.status IN ('failed','interrupted')) AS repair_failed,"
                "(SELECT count(*) FROM repair_tasks t WHERE t.analysis_id=a.id "
                "AND t.status='pr_created') AS repair_published "
                "FROM repository_analyses a JOIN repositories r ON r.id=a.repo_id "
                "WHERE a.id=(SELECT id FROM repository_analyses WHERE repo_id=a.repo_id "
                "ORDER BY created DESC LIMIT 1) "
                "AND a.status IN ('waiting_model','running','completed','failed','interrupted')"
            )]

    @app.post("/api/github/accounts")
    def github_connect(body: GitHubConnectBody):
        return github.connect(body.username, body.token.get_secret_value())

    @app.post("/api/github/accounts/{account_id}/verify")
    def github_verify(account_id: int, body: GitHubVersionBody):
        return github.verify(account_id, body.version)

    @app.post("/api/github/accounts/{account_id}/activate")
    def github_activate(account_id: int, body: GitHubVersionBody):
        return github.verify(account_id, body.version, activate=True)

    @app.post("/api/github/accounts/{account_id}/disconnect")
    def github_disconnect(account_id: int, body: GitHubVersionBody):
        return github.disconnect(account_id, body.version)

    @app.post("/api/github/accounts/{account_id}/repository-access")
    def github_repository_access(account_id: int, body: GitHubRepositoryBody):
        return github.check_repository(account_id, body.version, body.repo_id)

    @app.get("/api/repositories/{repo_id}/issues")
    def repository_issues(repo_id: str):
        return issues.list(repo_id)

    @app.post("/api/repositories/{repo_id}/issues/sync", status_code=202)
    def sync_repository_issues(repo_id: str):
        return issues.sync(repo_id)

    @app.post("/api/repositories/{repo_id}/issues/{issue_id}/analysis", status_code=202)
    def analyze_repository_issue(
        repo_id: str, issue_id: str, body: IssueAnalysisBody | None = None
    ):
        issue = issues.get(issue_id)
        if issue["repo_id"] != repo_id:
            raise Conflict("issue_repository_mismatch")
        environments = [
            item for item in execution.list(repo_id)
            if item["state"] != "deleted" and item["revision"]
        ]
        if body and body.env_id:
            environment = next(
                (item for item in environments if item["id"] == body.env_id), None
            )
            if not environment:
                raise Missing("Environment not found.")
        else:
            environment = environments[0] if environments else None
        if not environment:
            raise Conflict("environment_required")
        context = {key: issue[key] for key in (
            "id", "repo_id", "number", "title", "body", "state", "author", "url", "updated_at"
        )}
        analysis_id = analyses.enqueue(
            environment["id"], environment["version"], manual=True, issue=context
        )
        return analyses.get(analysis_id)

    @app.get("/api/execution/readiness")
    def execution_readiness():
        return {**execution.runtime.readiness(),
                "diagnosis_model": execution.diagnoses.engine.status()}

    @app.get("/api/repositories/{repo_id}/environments")
    def environments(repo_id: str):
        return execution.list(repo_id)

    @app.get("/api/repositories/{repo_id}/schedule")
    def repository_schedule(repo_id: str):
        return schedules.get(repo_id)

    @app.get("/api/repositories/{repo_id}/schedule/history")
    def repository_schedule_history(repo_id: str, limit: int = Query(10, ge=1, le=50)):
        return schedules.history(repo_id, limit=limit)

    @app.post("/api/repositories/{repo_id}/schedule")
    def configure_repository_schedule(repo_id: str, body: ScheduleBody):
        return schedules.configure(
            repo_id, enabled=body.enabled, local_time=body.local_time,
            version=body.version,
        )

    @app.post("/api/repositories/{repo_id}/environments", status_code=202)
    def download_repository(repo_id: str, body: DownloadBody):
        return execution.create(repo_id, auto_run=body.auto_run)

    @app.get("/api/environments/{env_id}")
    def environment(env_id: str):
        return execution.get(env_id)

    @app.post("/api/environments/{env_id}/configuration/detect")
    def detect_environment_configuration(env_id: str, body: GitHubVersionBody):
        return execution.detect_configuration(env_id, body.version)

    @app.post("/api/environments/{env_id}/configuration")
    def configure_environment(env_id: str, body: ExecutionConfigBody):
        config = body.model_dump()
        version = config.pop("version")
        continue_pipeline = config.pop("continue_pipeline")
        confirmed = config.pop("desktop_offline_confirmed")
        return execution.configure(env_id, version, config, continue_pipeline=continue_pipeline,
                                   desktop_offline_confirmed=confirmed)

    @app.post("/api/environments/{env_id}/pipeline", status_code=202)
    def continue_environment_pipeline(env_id: str, body: GitHubVersionBody):
        return execution.continue_pipeline(env_id, body.version)

    @app.get("/api/environments/{env_id}/analyses")
    def environment_analyses(env_id: str):
        return analyses.list(env_id)

    @app.get("/api/environments/{env_id}/repairs")
    def environment_repairs(env_id: str):
        return repairs.list(env_id)

    @app.get("/api/repairs/{repair_id}")
    def repair_detail(repair_id: str):
        return repairs.get(repair_id)

    @app.post("/api/repairs/{repair_id}/retry")
    def retry_repair(repair_id: str, body: GitHubVersionBody):
        return repairs.retry(repair_id, body.version)

    @app.post("/api/environments/{env_id}/analyses", status_code=202)
    def start_analysis(env_id: str, body: GitHubVersionBody):
        return analyses.get(analyses.enqueue(env_id, body.version, manual=True))

    @app.post("/api/analyses/{analysis_id}/retry")
    def retry_analysis(analysis_id: str, body: GitHubVersionBody):
        return analyses.retry(analysis_id, body.version)

    @app.get("/api/analyses/{analysis_id}/scope")
    def analysis_scope(analysis_id: str):
        return task_scopes.get(analysis_id)

    @app.post("/api/analyses/{analysis_id}/scope/requirements")
    def add_scope_requirements(analysis_id: str, body: ScopeRequirementsBody):
        return task_scopes.add_owner_requirements(
            analysis_id, body.version, [item.model_dump() for item in body.items],
        )

    @app.post("/api/analyses/{analysis_id}/scope-review", status_code=202)
    def reassess_task_scope(analysis_id: str, body: GitHubVersionBody):
        return analyses.reassess_scope(analysis_id, body.version)

    @app.post("/api/analyses/{analysis_id}/acceptance/retry", status_code=202)
    def retry_automatic_acceptance(analysis_id: str, body: AcceptanceRetryBody):
        return acceptance.retry(
            analysis_id, body.version, additional_attempt=body.additional_attempt,
            run_id=body.run_id,
        )

    @app.post("/api/task-scopes/{scope_id}/items/{key}/observations")
    def scope_observation(scope_id: str, key: str, body: ScopeObservationBody):
        return task_scopes.observe(scope_id, key, **body.model_dump())

    @app.post("/api/analyses/{analysis_id}/memory-evaluation", status_code=202)
    def create_memory_evaluation(analysis_id: str):
        return memory_evaluations.enqueue(analysis_id)

    @app.get("/api/analyses/{analysis_id}/memory-evaluation")
    def memory_evaluation(analysis_id: str):
        return memory_evaluations.get_by_analysis(analysis_id)

    @app.post("/api/analyses/{analysis_id}/findings/{finding_id}/feedback")
    def finding_feedback(analysis_id: str, finding_id: str, body: FindingFeedbackBody):
        return analyses.feedback(analysis_id, finding_id, **body.model_dump())

    @app.post("/api/analyses/{analysis_id}/findings/{finding_id}/disposition")
    def finding_disposition(analysis_id: str, finding_id: str, body: FindingDispositionBody):
        return analyses.disposition(analysis_id, finding_id, **body.model_dump())

    @app.post("/api/environments/{env_id}/{action}", status_code=202)
    def environment_action(
        env_id: str, action: Literal["setup", "test", "start", "stop", "delete"],
        body: GitHubVersionBody,
    ):
        return execution.action(env_id, body.version, action)

    @app.post("/api/environments/{env_id}/diagnoses/{job_id}/apply", status_code=202)
    def apply_environment_diagnosis(env_id: str, job_id: str, body: GitHubVersionBody):
        return execution.apply_diagnosis(env_id, job_id, body.version)

    @app.post("/api/repositories", status_code=201)
    async def add_repository(body: RepositoryBody):
        parts = urlsplit(body.url.strip())
        if parts.scheme != "https" or parts.netloc != "github.com" or parts.query or parts.fragment:
            raise HTTPException(422, "Provide an HTTPS github.com repository URL.")
        path = parts.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", path):
            raise HTTPException(422, "Provide a repository URL, not a branch or file URL.")
        if path.split("/")[-1] in (".", ".."):
            raise HTTPException(422, "Invalid repository name.")
        return store.add_repository(path[1:], "https://github.com" + path)

    @app.get("/api/repositories/{repo_id}/memories")
    async def memories(
        repo_id: str,
        revision: str = Query(default="", max_length=128),
        query: str = Query(default="", max_length=200),
        category: Literal[
            "", "architecture", "setup", "testing", "bug", "fix", "investigation"
        ] = "",
        status: Literal["active", "archived"] = "active",
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
    ):
        return memory.list(repo_id, revision=revision, query=query, category=category,
                           status=status, limit=limit, offset=offset)

    @app.post("/api/repositories/{repo_id}/memories", status_code=201)
    async def add_memory(repo_id: str, body: MemoryBody):
        return memory.create(repo_id, **body.model_dump())

    @app.post("/api/repositories/{repo_id}/memories/{memory_id}")
    async def edit_memory(repo_id: str, memory_id: str, body: MemoryEditBody):
        return memory.update(repo_id, memory_id, **body.model_dump())

    @app.post("/api/repositories/{repo_id}/memories/{memory_id}/archive")
    async def archive_memory(repo_id: str, memory_id: str, body: MemoryVersionBody):
        return memory.archive(repo_id, memory_id, version=body.version)

    @app.post("/api/repositories/{repo_id}/memories/{memory_id}/restore")
    async def restore_memory(repo_id: str, memory_id: str, body: MemoryVersionBody):
        return memory.restore(repo_id, memory_id, version=body.version)

    @app.get("/api/repositories/{repo_id}/memories/{memory_id}/history")
    async def memory_history(repo_id: str, memory_id: str):
        return memory.history(repo_id, memory_id)

    @app.get("/api/repositories/{repo_id}/memory-metrics")
    async def memory_metrics(
        repo_id: str,
        revision: str = Query(default="", max_length=128),
    ):
        return memory.metrics(repo_id, revision=revision)

    @app.get("/api/repositories/{repo_id}/memory-aging")
    async def memory_aging(
        repo_id: str,
        revision: str = Query(default="", max_length=128),
        days: int = Query(default=90, ge=1, le=3650),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return memory.aging(repo_id, revision=revision, days=days, limit=limit)

    assets = ROOT / "dist/assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    async def index():
        file = ROOT / "dist/index.html"
        if not file.exists():
            raise HTTPException(503, "Build the WebUI with npm run build first.")
        return FileResponse(file)

    return app
