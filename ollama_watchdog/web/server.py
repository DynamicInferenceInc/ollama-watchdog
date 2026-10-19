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

from .aliases import ClientAliases, InvalidAlias
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
    def __init__(self, service: DashboardService, aliases: ClientAliases) -> None:
        self._get: dict[str, Callable[[dict[str, str]], object]] = {
            "/api/report": lambda params: service.report(PeriodQuery.from_params(params)),
            "/api/days": lambda params: service.days(),
            "/api/status": lambda params: service.status(),
            "/api/aliases": lambda params: aliases.snapshot(),
            "/api/health": lambda params: {"ok": True},
        }
        self._post: dict[str, Callable[[dict], object]] = {
            "/api/aliases": lambda body: aliases.set(str(body.get("ip", "")), str(body.get("name", ""))),
        }

    def handles(self, method: str, path: str) -> bool:
        return path in self._routes(method)

    def dispatch(self, method: str, path: str, data: dict) -> object:
        return self._routes(method)[path](data)

    def _routes(self, method: str) -> dict[str, Callable[[dict], object]]:
        return self._post if method == "POST" else self._get


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], router: ApiRouter, auth: BasicAuth | None) -> None:
        super().__init__(address, DashboardRequestHandler)
        self.router = router
        self.auth = auth


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "ollama-watchdog"
    MAX_BODY_BYTES = 4096

    def do_GET(self) -> None:  # noqa: N802 — имя задано http.server
        url = urlparse(self.path)
        if url.path != "/api/health" and not self._authorized():
            return
        if self.server.router.handles("GET", url.path):
            self._serve_api("GET", url.path, dict(parse_qsl(url.query)))
        else:
            self._serve_static(url.path)

    def do_POST(self) -> None:  # noqa: N802 — имя задано http.server
        url = urlparse(self.path)
        if not self._authorized():
            return
        if not self.server.router.handles("POST", url.path):
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        body = self._read_json()
        if body is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "ожидается JSON-объект (Content-Type: application/json)"})
            return
        self._serve_api("POST", url.path, body)

    def _read_json(self) -> dict | None:
        """Только application/json: такой запрос с чужого сайта браузер не отправит без CORS."""
        if self.headers.get_content_type() != "application/json":
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= self.MAX_BODY_BYTES:
                return None
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _authorized(self) -> bool:
        auth = self.server.auth
        if auth is None or auth.allows(self.headers.get("Authorization")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="ollama-watchdog", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _serve_api(self, method: str, path: str, data: dict) -> None:
        try:
            payload = self.server.router.dispatch(method, path, data)
            status = HTTPStatus.OK
        except (BadRequest, InvalidAlias) as error:
            payload, status = {"error": str(error)}, HTTPStatus.BAD_REQUEST
        except Exception:
            log.exception("ошибка обработки %s %s", method, path)
            payload, status = {"error": "внутренняя ошибка сервера"}, HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(status, payload)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
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
