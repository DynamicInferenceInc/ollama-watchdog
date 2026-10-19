"""Разбор отдельных строк лога ollama.

В логе контейнера перемешаны три формата:
  * access-лог HTTP-сервера:  [GIN] 2026/10/15 - 12:37:57 | 200 | 3.42s | 1.2.3.4 | POST "/api/chat"
  * структурный лог ollama:    time=2026-10-15T12:37:56.593Z level=INFO source=... msg="..." key=value
  * вывод llama-server:        slot print_timing: id  0 | task 0 | eval time = 675.74 ms / 16 tokens ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .domain import EndpointKind, HttpRequest


class GoDurationParser:
    """Разбирает длительности в формате Go: 33.585µs, 3.42s, 1m3s, 1h2m."""

    _UNIT_SECONDS = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "μs": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }
    _PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")

    @classmethod
    def parse(cls, text: str) -> timedelta:
        parts = cls._PART.findall(text)
        if not parts:
            raise ValueError(f"не удалось разобрать длительность: {text!r}")
        seconds = sum(float(value) * cls._UNIT_SECONDS[unit] for value, unit in parts)
        return timedelta(seconds=seconds)


class GinLineParser:
    _PATTERN = re.compile(
        r"^\[GIN\] (?P<date>\d{4}/\d{2}/\d{2}) - (?P<time>\d{2}:\d{2}:\d{2})"
        r" \|\s*(?P<status>\d{3})\s*"
        r"\|\s*(?P<duration>\S+)\s*"
        r"\|\s*(?P<ip>\S+)\s*"
        r"\|\s*(?P<method>[A-Z]+)\s+\"(?P<path>[^\"]*)\""
    )

    def __init__(self, server_timezone: timezone = timezone.utc) -> None:
        self._server_timezone = server_timezone

    def parse(self, line: str) -> HttpRequest | None:
        match = self._PATTERN.match(line)
        if match is None:
            return None
        timestamp = datetime.strptime(
            f"{match['date']} {match['time']}", "%Y/%m/%d %H:%M:%S"
        ).replace(tzinfo=self._server_timezone)
        return HttpRequest(
            timestamp=timestamp,
            status=int(match["status"]),
            duration=GoDurationParser.parse(match["duration"]),
            client_ip=match["ip"],
            method=match["method"],
            path=match["path"].split("?", 1)[0],
        )


@dataclass(frozen=True)
class SlogRecord:
    timestamp: datetime
    level: str
    source: str
    message: str
    attrs: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.attrs.get(key, default)


class SlogLineParser:
    _QUOTED_OR_BARE = r'"(?:[^"\\]|\\.)*"|\S*'
    _HEADER = re.compile(
        rf"^time=(?P<time>\S+) level=(?P<level>\S+) source=(?P<source>\S+)"
        rf" msg=(?P<msg>{_QUOTED_OR_BARE})(?P<rest>.*)$"
    )
    _ATTR = re.compile(rf"(?P<key>[\w.\-]+)=(?P<value>{_QUOTED_OR_BARE})")

    def parse(self, line: str) -> SlogRecord | None:
        match = self._HEADER.match(line)
        if match is None:
            return None
        try:
            timestamp = datetime.fromisoformat(match["time"])
        except ValueError:
            return None
        attrs = {
            attr["key"]: self._unquote(attr["value"])
            for attr in self._ATTR.finditer(match["rest"])
        }
        return SlogRecord(
            timestamp=timestamp,
            level=match["level"],
            source=match["source"],
            message=self._unquote(match["msg"]),
            attrs=attrs,
        )

    @staticmethod
    def _unquote(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] == '"':
            return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return value


@dataclass(frozen=True)
class TimingRecord:
    """Строка `print_timing` из llama-server: этап, время и число токенов."""

    stage: str  # "prompt" или "generation"
    milliseconds: float
    tokens: int


class TimingLineParser:
    _PATTERN = re.compile(
        r"print_timing:.*\| +(?P<stage>prompt eval|eval) time = +(?P<ms>[\d.]+) ms / +(?P<tokens>\d+) tokens"
    )

    def parse(self, line: str) -> TimingRecord | None:
        match = self._PATTERN.search(line)
        if match is None:
            return None
        stage = "prompt" if match["stage"] == "prompt eval" else "generation"
        return TimingRecord(stage=stage, milliseconds=float(match["ms"]), tokens=int(match["tokens"]))


@dataclass(frozen=True)
class SlotContextRecord:
    """llama-server при старте сообщает размер контекста слота — по нему узнаётся модель."""

    context_size: int


@dataclass(frozen=True)
class TaskStartedRecord:
    task_id: int
    context_size: int
    kind: EndpointKind

    @staticmethod
    def kind_from_keep(n_keep: int) -> EndpointKind:
        """Ollama запускает генеративные модели с `--keep 4`; у эмбеддингов llama-server пишет n_keep = 0."""
        return EndpointKind.EMBEDDING if n_keep == 0 else EndpointKind.GENERATION


@dataclass(frozen=True)
class TaskReleasedRecord:
    task_id: int


RunnerRecord = SlotContextRecord | TaskStartedRecord | TaskReleasedRecord


class RunnerLineParser:
    """Строки llama-server о жизненном цикле задач (одна задача — один запрос к модели)."""

    _SLOT_CONTEXT = re.compile(r"load_model: initializing, n_slots = \d+, n_ctx_slot = (?P<ctx>\d+)")
    _TASK_STARTED = re.compile(
        r"\| task (?P<task>\d+) \| new prompt, n_ctx_slot = (?P<ctx>\d+), n_keep = (?P<keep>\d+)"
    )
    _TASK_RELEASED = re.compile(r"release: +id +\d+ \| task (?P<task>\d+) \| stop processing")

    MARKERS = ("load_model: initializing", "new prompt, n_ctx_slot", "stop processing")

    def parse(self, line: str) -> RunnerRecord | None:
        if match := self._TASK_STARTED.search(line):
            return TaskStartedRecord(
                int(match["task"]), int(match["ctx"]), TaskStartedRecord.kind_from_keep(int(match["keep"]))
            )
        if match := self._TASK_RELEASED.search(line):
            return TaskReleasedRecord(int(match["task"]))
        if match := self._SLOT_CONTEXT.search(line):
            return SlotContextRecord(int(match["ctx"]))
        return None


class LineNormalizer:
    """Убирает префикс `docker logs --timestamps` и переводы строк."""

    _DOCKER_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2}) ")

    def normalize(self, line: str) -> str:
        line = line.rstrip("\r\n")
        return self._DOCKER_TIMESTAMP.sub("", line, count=1)
