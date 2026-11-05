from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from repowatch import __version__
from repowatch.api.routes import routes
from repowatch.api.security import COOKIE_NAME, LocalSessions
from repowatch.api.static_files import FrontendFiles
from repowatch.application.credentials import CredentialError
from repowatch.application.providers import ProviderSaveError
from repowatch.application.storage import ConflictError, StorageError
from repowatch.security import install_log_redaction


def create_app(services=None, *, sessions: LocalSessions | None = None, static_dir=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if services is not None:
                services.close()

    app = FastAPI(title="RepoWatch", version=__version__, lifespan=lifespan)

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "stage": "p1" if services else "p0", "version": __version__}

    if services is None:
        return app
    install_log_redaction()
    sessions = sessions or LocalSessions()
    app.state.sessions, app.state.services = sessions, services
    app.middleware("http")(sessions.guard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(sessions.origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-RepoWatch-Session"],
    )

    @app.post("/api/v1/session/bootstrap")
    def bootstrap(request: Request):
        return sessions.open(request)

    @app.get("/api/v1/session")
    def session(request: Request):
        return {"csrf": sessions.authenticated(request)["csrf"]}

    @app.post("/api/v1/session/logout")
    def logout(request: Request):
        sessions.sessions.pop(request.cookies.get(COOKIE_NAME), None)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(COOKIE_NAME, path="/api/")
        return response

    def handler(code, status):
        async def respond(_request, _error):
            return JSONResponse({"error": code}, status_code=status)

        return respond

    for errors, code, status in (
        ((ConflictError,), "revision_or_operation_conflict", 409),
        ((PermissionError,), "operation_not_permitted", 403),
        ((LookupError,), "record_not_found", 404),
        ((RequestValidationError, ValueError, TypeError, KeyError), "invalid_request", 422),
        ((CredentialError, ProviderSaveError), "credential_or_provider_unavailable", 503),
        ((StorageError,), "storage_unavailable", 503),
        ((Exception,), "internal_error", 500),
    ):
        for error in errors:
            app.add_exception_handler(error, handler(code, status))
    app.include_router(routes(services))
    if static_dir is not None:
        app.mount("/", FrontendFiles(directory=static_dir, html=True), name="frontend")

    return app
