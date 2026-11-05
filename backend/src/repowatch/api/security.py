"""回环地址、固定 Host／Origin、一次引导及浏览器会话。"""

import ipaddress
import secrets
import time
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

from repowatch.application.credentials import SecretValue
from repowatch.security import DEFAULT_REDACTOR

COOKIE_NAME = "repowatch_session"
CSRF_HEADER = "X-RepoWatch-Session"


class LocalSessions:
    def __init__(self, origin="http://127.0.0.1:8765", *, extra_origins=(), clock=time.time):
        self.origins = frozenset((origin, *extra_origins))
        for value in self.origins:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("控制面来源必须是显式回环 HTTP 地址")
        self.host = urlsplit(origin).netloc
        self.clock, self.sessions = clock, {}
        self.bootstrap_token = secrets.token_urlsafe(32)
        DEFAULT_REDACTOR.register(SecretValue(self.bootstrap_token))
        self.bootstrap_used = False
        self.bootstrap_expires_at = self.clock() + 600

    def is_local(self, request):
        try:
            peer = ipaddress.ip_address(request.client.host)
        except (ValueError, AttributeError):
            return False
        return peer.is_loopback and request.headers.get("host") == self.host

    def open(self, request):
        supplied = request.headers.get("Authorization", "")
        if (
            self.bootstrap_used
            or self.clock() >= self.bootstrap_expires_at
            or not secrets.compare_digest(supplied, "Bearer " + self.bootstrap_token)
        ):
            raise PermissionError("控制面引导凭证无效或已使用")
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.bootstrap_used = True
        self.sessions[session_id] = {"csrf": csrf, "expires_at": self.clock() + 8 * 3600}
        DEFAULT_REDACTOR.register(SecretValue(session_id))
        DEFAULT_REDACTOR.register(SecretValue(csrf))
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="strict",
            max_age=8 * 3600,
            path="/api/",
        )
        return response

    def authenticated(self, request):
        identifier = request.cookies.get(COOKIE_NAME, "")
        current = self.sessions.get(identifier)
        return current if current and self.clock() < current["expires_at"] else None

    async def guard(self, request, call_next):
        if not self.is_local(request):
            return JSONResponse({"error": "loopback_or_host_rejected"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/v1/health":
            origin = request.headers.get("origin")
            if origin is not None and origin not in self.origins:
                return JSONResponse({"error": "origin_rejected"}, status_code=403)
            if request.method == "OPTIONS":
                return await call_next(request)
            mutation = request.method not in {"GET", "HEAD"}
            if mutation and origin not in self.origins:
                return JSONResponse({"error": "origin_required"}, status_code=403)
            if path != "/api/v1/session/bootstrap":
                session = self.authenticated(request)
                if session is None:
                    return JSONResponse({"error": "session_required"}, status_code=401)
                if mutation and not secrets.compare_digest(
                    request.headers.get(CSRF_HEADER, ""),
                    session["csrf"],
                ):
                    return JSONResponse({"error": "session_header_required"}, status_code=403)
            if mutation:
                raw = await request.body()
                if len(raw) > 2 * 1024 * 1024:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
