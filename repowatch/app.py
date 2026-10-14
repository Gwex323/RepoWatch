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

from .analysis_model import AnalysisModel
from .execution import Execution
from .github_accounts import GitHubAccounts, GitHubError
from .memory import Memory
from .repository_analysis import Analyses
from .repository_source import ExecutionError
from .store import SDK_VERSION, Conflict, Missing, Store
from .workflow import ROLES, Worker

ROOT = Path(__file__).resolve().parent.parent


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordBody(StrictBody):
    password: str = Field(min_length=10, max_length=128)


class RepositoryBody(StrictBody):
    url: str = Field(max_length=300)


class ObservationBody(StrictBody):
    total: int = Field(ge=0, le=10000, strict=True)
    page: int = Field(ge=1, le=1000, strict=True)
    size: int = Field(ge=1, le=100, strict=True)


class ConfirmationBody(StrictBody):
    decision: Literal["confirmed", "not_reproduced"]
    revision: str = Field(min_length=64, max_length=64)
    personally_reproduced: bool = False
    note: str = Field(default="", max_length=2000)


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


class GitHubRepositoryBody(GitHubVersionBody):
    repo_id: str = Field(min_length=1, max_length=100)


class ExecutionConfigBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    language: Literal["python", "go", "typescript"]
    install: str = Field(max_length=4000)
    test: str = Field(max_length=4000)
    start: str = Field(max_length=4000)
    port: int = Field(ge=1024, le=65535, strict=True)
    install_network: bool = False


class AnalysisModelBody(StrictBody):
    version: int = Field(ge=0, strict=True)
    enabled: bool
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    api: Literal["responses", "chat_completions"]
    reasoning_effort: Literal["auto", "default", "low", "medium", "high", "max"] = "auto"
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1024)


class FindingFeedbackBody(StrictBody):
    version: int = Field(ge=1, strict=True)
    decision: Literal["confirmed", "not_reproduced"]
    personally_reproduced: bool = False
    note: str = Field(default="", max_length=2000)


def create_app(
    db_path: Path | None = None, *, start_worker=True, github_client=None, credential_vault=None,
    execution_runtime=None, repository_source=None, execution_diagnoser=None, analysis_engine=None,
) -> FastAPI:
    # Windows registry MIME mappings may label .js as text/plain.
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    store = Store(db_path or Path(os.getenv("REPOWATCH_DB", str(ROOT / "data/repowatch.db"))))
    memory = Memory(store)
    github = GitHubAccounts(store, client=github_client, vault=credential_vault)
    execution = Execution(store, github, runtime=execution_runtime, source=repository_source,
                          diagnoser=execution_diagnoser)
    analysis_model = AnalysisModel(store, github)
    analyses = Analyses(execution, analysis_engine or analysis_model)
    login_attempts: dict[str, deque] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        worker = asyncio.create_task(Worker(store).run()) if start_worker else None
        executor = asyncio.create_task(execution.run()) if start_worker else None
        diagnostics = asyncio.create_task(execution.diagnoses.run()) if start_worker else None
        analyst = asyncio.create_task(analyses.run()) if start_worker else None
        yield
        execution.closing.set()
        if analyst:
            await analyst
        if diagnostics:
            await diagnostics
        if executor:
            await executor
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(title="RepoWatch", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.execution = execution
    app.state.analyses = analyses
    app.state.analysis_model = analysis_model
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
        if request.url.path.startswith("/api/analysis-model"):
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

    @app.get("/api/system")
    async def system():
        return {
            "phase": 3,
            "sdk_version": SDK_VERSION,
            "roles": ROLES,
            "model_mode": "deterministic_fixture",
            "external_model_calls": (execution.diagnoses.engine.status()["enabled"]
                                     or analyses.engine.status()["enabled"]),
            "repository_analysis": analyses.engine.status(),
            "execution_diagnosis": execution.diagnoses.engine.status(),
            "github_publication": False,
            "github_account_binding": True,
            "external_repository_execution": True,
            "existing_issue_intake": False,
            "reproduction_policy": {
                "discovered": "human", "existing_issue": "automatic", "merge": "owner_only",
            },
            "schedule": {"frequency": "daily", "enabled": False},
            "tracing_export": False,
            "storage": "SQLite",
            "runtime": "Python",
            "platform": platform.system(),
            "max_turns": 24,
            "max_attempts": 3,
            "memory": {"enabled": True, "scope": "repository_and_exact_revision",
                       "retrieval": "role_categories", "semantic_search": False},
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

    @app.get("/api/analysis-notifications")
    def analysis_notifications():
        with store.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT a.repo_id,r.name AS repository,a.status,"
                "(SELECT count(*) FROM analysis_findings f WHERE f.analysis_id=a.id "
                "AND f.status='waiting_reproduction') AS waiting "
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

    @app.get("/api/execution/readiness")
    def execution_readiness():
        return {**execution.runtime.readiness(),
                "diagnosis_model": execution.diagnoses.engine.status()}

    @app.get("/api/repositories/{repo_id}/environments")
    def environments(repo_id: str):
        return execution.list(repo_id)

    @app.post("/api/repositories/{repo_id}/environments", status_code=202)
    def download_repository(repo_id: str, body: StrictBody):
        return execution.create(repo_id)

    @app.get("/api/environments/{env_id}")
    def environment(env_id: str):
        return execution.get(env_id)

    @app.post("/api/environments/{env_id}/configuration")
    def configure_environment(env_id: str, body: ExecutionConfigBody):
        config = body.model_dump()
        version = config.pop("version")
        return execution.configure(env_id, version, config)

    @app.get("/api/environments/{env_id}/analyses")
    def environment_analyses(env_id: str):
        return analyses.list(env_id)

    @app.post("/api/environments/{env_id}/analyses", status_code=202)
    def start_analysis(env_id: str, body: GitHubVersionBody):
        return analyses.get(analyses.enqueue(env_id, body.version))

    @app.post("/api/analyses/{analysis_id}/retry")
    def retry_analysis(analysis_id: str, body: GitHubVersionBody):
        return analyses.retry(analysis_id, body.version)

    @app.post("/api/analyses/{analysis_id}/findings/{finding_id}/feedback")
    def finding_feedback(analysis_id: str, finding_id: str, body: FindingFeedbackBody):
        return analyses.feedback(analysis_id, finding_id, **body.model_dump())

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

    @app.post("/api/repositories/{repo_id}/runs")
    async def start_run(repo_id: str, body: StrictBody | None = None):
        return {"run_id": store.start_run(repo_id)}

    @app.post("/api/samples/existing-issue/runs")
    async def start_issue_sample(body: StrictBody | None = None):
        return {"run_id": store.start_issue_sample()}

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

    @app.get("/api/runs/{run_id}/memories")
    async def memory_reads(run_id: str):
        return memory.reads(run_id)

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        return store.details(run_id)

    @app.post("/api/runs/{run_id}/experiment")
    async def observe(run_id: str, body: ObservationBody):
        return store.observe(run_id, body.total, body.page, body.size)

    @app.post("/api/runs/{run_id}/confirmation")
    async def confirm(run_id: str, body: ConfirmationBody):
        if body.decision == "confirmed" and not body.personally_reproduced:
            raise HTTPException(422, "Personal reproduction must be explicitly confirmed.")
        changed = store.confirm(run_id, body.decision, body.revision, body.note.strip())
        return {"accepted": True, "changed": changed, "run": store.run(run_id)}

    @app.post("/api/runs/{run_id}/retry")
    async def retry(run_id: str):
        store.retry(run_id)
        return {"queued": True}

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
