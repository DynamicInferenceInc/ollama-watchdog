"""HTTP-сервер дашборда: JSON API + статические файлы фронтенда."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from .service import BadRequest, DashboardService, PeriodQuery

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True)
class BasicAuth:
    username: str
    password: str

    def allows(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            username, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, self.username) & hmac.compare_digest(password, self.password)


class ApiRouter:
    def __init__(self, service: DashboardService) -> None:
        self._routes: dict[str, Callable[[dict[str, str]], object]] = {
            "/api/report": lambda params: service.report(PeriodQuery.from_params(params)),
            "/api/days": lambda params: service.days(),
            "/api/status": lambda params: service.status(),
            "/api/health": lambda params: {"ok": True},
        }

    def handles(self, path: str) -> bool:
        return path in self._routes

    def dispatch(self, path: str, params: dict[str, str]) -> object:
        return self._routes[path](params)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], router: ApiRouter, auth: BasicAuth | None) -> None:
        super().__init__(address, DashboardRequestHandler)
        self.router = router
        self.auth = auth


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "ollama-watchdog"

    def do_GET(self) -> None:  # noqa: N802 — имя задано http.server
        url = urlparse(self.path)
        if url.path != "/api/health" and not self._authorized():
            return
        if self.server.router.handles(url.path):
            self._serve_api(url.path, dict(parse_qsl(url.query)))
        else:
            self._serve_static(url.path)

    def _authorized(self) -> bool:
        auth = self.server.auth
        if auth is None or auth.allows(self.headers.get("Authorization")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="ollama-watchdog", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _serve_api(self, path: str, params: dict[str, str]) -> None:
        try:
            payload = self.server.router.dispatch(path, params)
            status = HTTPStatus.OK
        except BadRequest as error:
            payload, status = {"error": str(error)}, HTTPStatus.BAD_REQUEST
        except Exception:
            log.exception("ошибка обработки %s", path)
            payload, status = {"error": "внутренняя ошибка сервера"}, HTTPStatus.INTERNAL_SERVER_ERROR
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", cache="no-store")

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (STATIC_DIR / relative).resolve()
        if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, file.read_bytes(), content_type, cache="no-cache")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, cache: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s - %s", self.address_string(), format % args)
