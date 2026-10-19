"""Откуда читать логи: файлы, stdin или `docker logs` (локально или по SSH)."""

from __future__ import annotations

import gzip
import re
import shlex
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from pathlib import Path


class LogSourceError(RuntimeError):
    pass


class LogSource(ABC):
    @abstractmethod
    def lines(self) -> Iterator[str]: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    def supports_rereading(self) -> bool:
        return True


class FileLogSource(LogSource):
    _TIMESTAMP = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})(?:T| - )(\d{2}:\d{2}:\d{2})")

    def __init__(self, paths: Sequence[Path]) -> None:
        missing = [p for p in paths if not p.is_file()]
        if missing:
            raise LogSourceError(f"файл не найден: {', '.join(map(str, missing))}")
        self._paths = sorted(paths, key=self._first_timestamp)

    def lines(self) -> Iterator[str]:
        for path in self._paths:
            with self._open(path) as handle:
                yield from handle

    @property
    def description(self) -> str:
        return ", ".join(str(p) for p in self._paths)

    @staticmethod
    def _open(path: Path):
        if path.suffix == ".gz":
            return gzip.open(path, "rt", encoding="utf-8", errors="replace")
        return path.open(encoding="utf-8", errors="replace")

    @classmethod
    def _first_timestamp(cls, path: Path) -> str:
        """Файлы обрабатываются в хронологическом порядке — по первой дате в файле."""
        with cls._open(path) as handle:
            for line in handle:
                match = cls._TIMESTAMP.search(line)
                if match:
                    return "{}-{}-{} {}".format(*match.groups())
        return ""


class StdinLogSource(LogSource):
    def lines(self) -> Iterator[str]:
        yield from sys.stdin

    @property
    def description(self) -> str:
        return "stdin"

    @property
    def supports_rereading(self) -> bool:
        return False


class DockerLogSource(LogSource):
    def __init__(self, container: str, ssh_host: str | None = None, since: str | None = None) -> None:
        self._container = container
        self._ssh_host = ssh_host
        self._since = since

    def lines(self) -> Iterator[str]:
        try:
            process = subprocess.Popen(
                self._command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # ollama пишет лог в stderr
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as error:
            raise LogSourceError(f"не найдена команда: {error.filename}") from error

        assert process.stdout is not None
        last_line = ""
        for line in process.stdout:
            last_line = line
            yield line
        if process.wait() != 0:
            raise LogSourceError(
                f"`{shlex.join(self._command())}` завершилась с кодом {process.returncode}: {last_line.strip()}"
            )

    @property
    def description(self) -> str:
        where = f"{self._ssh_host}:" if self._ssh_host else ""
        return f"docker logs {where}{self._container}"

    def _command(self) -> list[str]:
        docker = ["docker", "logs"]
        if self._since:
            docker += ["--since", self._since]
        docker.append(self._container)
        if self._ssh_host:
            return ["ssh", self._ssh_host, shlex.join(docker)]
        return docker
