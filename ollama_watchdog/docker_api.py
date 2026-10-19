"""Минимальный клиент Docker Engine API: только чтение логов контейнера.

Работает через unix-сокет (`unix:///var/run/docker.sock`) или TCP
(`tcp://socket-proxy:2375`) — так же, как переменная DOCKER_HOST у docker CLI.
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse


class DockerApiError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class DockerTimestamp:
    """Метка времени docker logs с наносекундной точностью (RFC 3339 Nano)."""

    seconds: int
    nanos: int

    @classmethod
    def parse(cls, text: str) -> DockerTimestamp | None:
        if not text.endswith("Z") or "T" not in text:
            return None
        base, _, fraction = text[:-1].partition(".")
        try:
            moment = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        nanos = int(fraction[:9].ljust(9, "0")) if fraction.isdigit() else 0
        return cls(int(moment.timestamp()), nanos)

    @classmethod
    def of_line(cls, line: str) -> DockerTimestamp | None:
        prefix, _, _ = line.partition(" ")
        return cls.parse(prefix)

    def as_query(self) -> str:
        return f"{self.seconds}.{self.nanos:09d}"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float | None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class DockerClient:
    _STREAM_HEADER = struct.Struct(">BxxxL")

    def __init__(self, docker_host: str = "unix:///var/run/docker.sock", timeout: float = 10.0) -> None:
        self._url = urlparse(docker_host)
        if self._url.scheme not in ("unix", "tcp", "http"):
            raise DockerApiError(f"неподдерживаемый DOCKER_HOST: {docker_host}")
        self._timeout = timeout

    def inspect_container(self, name: str) -> dict:
        response = self._request(f"/containers/{quote(name)}/json", timeout=self._timeout)
        return json.loads(response.read())

    def stream_logs(
        self,
        container: str,
        since: DockerTimestamp | None = None,
        follow: bool = True,
        has_tty: bool = False,
        read_timeout: float | None = 120.0,
    ) -> Iterator[str]:
        """Строки лога с префиксом времени docker (`--timestamps`)."""
        query = {"stdout": 1, "stderr": 1, "timestamps": 1, "follow": int(follow)}
        if since is not None:
            query["since"] = since.as_query()
        response = self._request(f"/containers/{quote(container)}/logs?{urlencode(query)}", timeout=read_timeout)
        if has_tty:
            for raw in iter(response.readline, b""):
                yield raw.decode("utf-8", errors="replace")
        else:
            yield from self._demultiplex(response)

    def _demultiplex(self, response: http.client.HTTPResponse) -> Iterator[str]:
        """Без TTY docker склеивает stdout и stderr в кадры: 8 байт заголовка + данные."""
        buffers: dict[int, bytes] = {}
        while True:
            header = response.read(self._STREAM_HEADER.size)
            if len(header) < self._STREAM_HEADER.size:
                break
            stream, size = self._STREAM_HEADER.unpack(header)
            data = buffers.get(stream, b"") + response.read(size)
            *complete, rest = data.split(b"\n")
            buffers[stream] = rest
            for raw in complete:
                yield raw.decode("utf-8", errors="replace")
        for rest in buffers.values():
            if rest:
                yield rest.decode("utf-8", errors="replace")

    def _request(self, path: str, timeout: float | None) -> http.client.HTTPResponse:
        connection = self._connect(timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
        except OSError as error:
            connection.close()
            raise DockerApiError(f"Docker API недоступен ({self._url.geturl()}): {error}") from error
        if response.status != 200:
            body = response.read().decode("utf-8", errors="replace")
            connection.close()
            try:
                message = json.loads(body).get("message", body)
            except ValueError:
                message = body
            raise DockerApiError(f"Docker API ответил {response.status}: {message.strip()}")
        return response

    def _connect(self, timeout: float | None) -> http.client.HTTPConnection:
        if self._url.scheme == "unix":
            return _UnixHTTPConnection(self._url.path, timeout)
        return http.client.HTTPConnection(self._url.hostname, self._url.port or 2375, timeout=timeout)
