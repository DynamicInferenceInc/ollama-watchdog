"""Непрерывный сбор логов ollama для веб-дашборда.

`LogCollector` в фоне читает источник (`docker logs -f` через Docker API или
файлы), кормит анализатор и складывает полезные строки в `LogArchive`, чтобы
история не терялась при пересоздании контейнера ollama или самого watchdog.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path

from .analyzer import AnalysisResult, OllamaLogAnalyzer
from .docker_api import DockerApiError, DockerClient, DockerTimestamp
from .parsing import RunnerLineParser
from .sources import FileLogSource

log = logging.getLogger(__name__)


class LogArchive:
    """Файл с «полезными» строками лога (без многословного вывода llama-server)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = None

    _MARKERS = ("print_timing:", *RunnerLineParser.MARKERS)

    @classmethod
    def is_worth_keeping(cls, line: str) -> bool:
        _, _, message = line.partition(" ")
        return message.startswith(("[GIN]", "time=")) or any(marker in message for marker in cls._MARKERS)

    def replay(self) -> Iterator[str]:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8", errors="replace") as handle:
            yield from handle

    def append(self, line: str) -> None:
        if self._writer is None:
            self._writer = self._path.open("a", encoding="utf-8")
        self._writer.write(line if line.endswith("\n") else line + "\n")
        self._writer.flush()

    def drop_older_than(self, cutoff: DockerTimestamp) -> None:
        if not self._path.exists():
            return
        temporary = self._path.with_suffix(".tmp")
        with self._path.open(encoding="utf-8", errors="replace") as source, temporary.open("w", encoding="utf-8") as target:
            for line in source:
                stamp = DockerTimestamp.of_line(line)
                if stamp is None or stamp >= cutoff:
                    target.write(line)
        os.replace(temporary, self._path)


class LogFeed(ABC):
    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def follows(self) -> bool:
        """True — источник бесконечный, после обрыва к нему нужно переподключаться."""

    @abstractmethod
    def lines(self, since: DockerTimestamp | None) -> Iterator[str]: ...


class DockerLogFeed(LogFeed):
    def __init__(self, client: DockerClient, container: str) -> None:
        self._client = client
        self._container = container

    @property
    def description(self) -> str:
        return f"docker logs -f {self._container}"

    @property
    def follows(self) -> bool:
        return True

    def lines(self, since: DockerTimestamp | None) -> Iterator[str]:
        info = self._client.inspect_container(self._container)
        running = info.get("State", {}).get("Running", False)
        has_tty = info.get("Config", {}).get("Tty", False)
        yield from self._client.stream_logs(self._container, since=since, follow=running, has_tty=has_tty)
        if not running:
            raise DockerApiError(f"контейнер «{self._container}» не запущен")


class FileLogFeed(LogFeed):
    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = list(paths)

    @property
    def description(self) -> str:
        return f"файлы: {', '.join(p.name for p in self._paths)}"

    @property
    def follows(self) -> bool:
        return False

    def lines(self, since: DockerTimestamp | None) -> Iterator[str]:
        return FileLogSource(self._paths).lines()


class CollectorState(Enum):
    STARTING = "starting"
    REPLAYING = "replaying"
    STREAMING = "streaming"
    RECONNECTING = "reconnecting"
    FINISHED = "finished"


@dataclass(frozen=True)
class CollectorStatus:
    source: str
    state: CollectorState
    lines_processed: int = 0
    archived_lines_replayed: int = 0
    last_log_at: datetime | None = None
    last_error: str | None = None
    connected_since: datetime | None = None


class AnalysisStore:
    """Потокобезопасная обёртка над анализатором: пишет сборщик, читает веб-сервер."""

    def __init__(self, server_timezone: tzinfo) -> None:
        self._analyzer = OllamaLogAnalyzer(server_timezone)
        self._lock = threading.RLock()
        self._version = 0

    def feed(self, line: str) -> None:
        with self._lock:
            self._analyzer.feed(line)
            self._version += 1

    def prune(self, before: datetime) -> None:
        with self._lock:
            self._analyzer.result.prune(before)
            self._version += 1

    @property
    def version(self) -> int:
        return self._version

    def read(self):
        """Контекст-менеджер: `with store.read() as result:` — данные не меняются, пока он открыт."""
        return _LockedResult(self._lock, self._analyzer)


class _LockedResult:
    def __init__(self, lock: threading.RLock, analyzer: OllamaLogAnalyzer) -> None:
        self._lock = lock
        self._analyzer = analyzer

    def __enter__(self) -> AnalysisResult:
        self._lock.acquire()
        return self._analyzer.result

    def __exit__(self, *exc_info) -> None:
        self._lock.release()


class LogCollector(threading.Thread):
    RECONNECT_DELAYS = (1, 2, 5, 10, 30)
    PRUNE_EVERY = timedelta(hours=1)

    def __init__(
        self,
        store: AnalysisStore,
        feed: LogFeed,
        archive: LogArchive | None,
        retention: timedelta | None,
    ) -> None:
        super().__init__(name="log-collector", daemon=True)
        self._store = store
        self._feed = feed
        self._archive = archive
        self._retention = retention
        self._last_stamp: DockerTimestamp | None = None
        self._last_prune = datetime.now(timezone.utc)
        self._status_lock = threading.Lock()
        self._status = CollectorStatus(source=feed.description, state=CollectorState.STARTING)

    @property
    def status(self) -> CollectorStatus:
        with self._status_lock:
            return self._status

    def run(self) -> None:
        self._replay_archive()
        if not self._feed.follows:
            self._consume(self._feed.lines(None), archive=False)
            self._update(state=CollectorState.FINISHED)
            return
        self._follow_forever()

    def _replay_archive(self) -> None:
        if self._archive is None:
            return
        self._update(state=CollectorState.REPLAYING)
        if self._retention is not None:
            self._archive.drop_older_than(self._stamp_of(datetime.now(timezone.utc) - self._retention))
        replayed = self._consume(self._archive.replay(), archive=False)
        self._update(archived_lines_replayed=replayed)
        log.info("из архива восстановлено строк: %d", replayed)

    def _follow_forever(self) -> None:
        failures = 0
        while True:
            try:
                lines = self._feed.lines(self._last_stamp)
                self._update(state=CollectorState.STREAMING, connected_since=datetime.now(timezone.utc), last_error=None)
                self._consume(lines, archive=True)
                failures = 0
            except TimeoutError:
                # В логе долго не было строк: соединение могло тихо умереть, переподключаемся.
                failures = 0
                continue
            except (DockerApiError, OSError) as error:
                failures += 1
                log.warning("ошибка чтения логов: %s", error)
                self._update(state=CollectorState.RECONNECTING, last_error=str(error))
            time.sleep(self.RECONNECT_DELAYS[min(failures, len(self.RECONNECT_DELAYS) - 1)])

    def _consume(self, lines: Iterator[str], archive: bool) -> int:
        count = 0
        for line in lines:
            stamp = DockerTimestamp.of_line(line)
            if stamp is not None:
                if self._last_stamp is not None and stamp <= self._last_stamp:
                    continue
                self._last_stamp = stamp
            self._store.feed(line)
            if archive and self._archive is not None and LogArchive.is_worth_keeping(line):
                self._archive.append(line)
            count += 1
            self._report_progress()
        return count

    def _report_progress(self) -> None:
        last_log_at = self._datetime_of(self._last_stamp)
        with self._status_lock:
            self._status = replace(
                self._status,
                lines_processed=self._status.lines_processed + 1,
                last_log_at=last_log_at or self._status.last_log_at,
            )
        self._prune_if_due()

    def _prune_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if self._retention is None or now - self._last_prune < self.PRUNE_EVERY:
            return
        self._last_prune = now
        self._store.prune(now - self._retention)

    def _update(self, **changes) -> None:
        with self._status_lock:
            self._status = replace(self._status, **changes)

    @staticmethod
    def _stamp_of(moment: datetime) -> DockerTimestamp:
        return DockerTimestamp(int(moment.timestamp()), 0)

    @staticmethod
    def _datetime_of(stamp: DockerTimestamp | None) -> datetime | None:
        if stamp is None:
            return None
        return datetime.fromtimestamp(stamp.seconds, timezone.utc)
