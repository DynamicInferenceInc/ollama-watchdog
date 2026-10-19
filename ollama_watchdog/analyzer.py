"""Проход по логу: превращает поток строк в список событий с привязкой к моделям."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .attribution import (
    DebugRequestMatcher,
    FinishedRequest,
    HeuristicAttributor,
    ModelNames,
    RunnerRegistry,
)
from .domain import GenerationTiming, HttpRequest, Incident, ModelLoad
from .parsing import (
    GinLineParser,
    LineNormalizer,
    SlogLineParser,
    SlogRecord,
    TimingLineParser,
    TimingRecord,
)


@dataclass
class AnalysisResult:
    requests: list[HttpRequest] = field(default_factory=list)
    loads: list[ModelLoad] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    timings: list[GenerationTiming] = field(default_factory=list)
    server_starts: list[datetime] = field(default_factory=list)
    debug_logging_detected: bool = False

    def prune(self, before: datetime) -> None:
        """Удаляет события старше `before`, чтобы долгоживущий сервис не рос в памяти бесконечно."""
        self.requests = [r for r in self.requests if r.timestamp >= before]
        self.loads = [l for l in self.loads if l.timestamp >= before]
        self.incidents = [i for i in self.incidents if i.timestamp >= before]
        self.timings = [t for t in self.timings if t.timestamp >= before]
        self.server_starts = [s for s in self.server_starts if s >= before]


class OllamaLogAnalyzer:
    def __init__(self, server_timezone: timezone = timezone.utc) -> None:
        self._normalizer = LineNormalizer()
        self._gin_parser = GinLineParser(server_timezone)
        self._slog_parser = SlogLineParser()
        self._timing_parser = TimingLineParser()

        self._registry = RunnerRegistry()
        self._heuristics = HeuristicAttributor(self._registry)
        self._debug_matcher = DebugRequestMatcher()

        self._result = AnalysisResult()
        self._last_timestamp: datetime | None = None
        self._last_startup_seconds: float | None = None
        self._pending_prompt: TimingRecord | None = None

        self._slog_handlers: dict[str, Callable[[SlogRecord], None]] = {
            "starting llama-server": self._on_runner_starting,
            "template selection": self._on_model_loaded,
            "loaded runners": self._on_loaded_runners,
            "context for request finished": self._on_request_finished,
        }

    @property
    def result(self) -> AnalysisResult:
        self._result.debug_logging_detected = self._debug_matcher.enabled
        return self._result

    def analyze(self, lines: Iterable[str]) -> AnalysisResult:
        for line in lines:
            self.feed(line)
        return self.result

    def feed(self, raw_line: str) -> None:
        line = self._normalizer.normalize(raw_line)
        if line.startswith("[GIN]"):
            request = self._gin_parser.parse(line)
            if request is not None:
                self._on_http_request(request)
        elif line.startswith("time="):
            record = self._slog_parser.parse(line)
            if record is not None:
                self._on_slog_record(record)
        else:
            timing = self._timing_parser.parse(line)
            if timing is not None:
                self._on_timing(timing)

    def _on_http_request(self, request: HttpRequest) -> None:
        self._last_timestamp = request.timestamp
        self._result.requests.append(request)
        if request.is_inference:
            self._heuristics.attribute(request)
            self._debug_matcher.on_request(request)

    def _on_slog_record(self, record: SlogRecord) -> None:
        self._last_timestamp = record.timestamp
        if record.message.startswith("Listening on"):
            self._on_server_restart(record)
        elif record.message.startswith("llama-server started in"):
            self._last_startup_seconds = self._parse_startup_seconds(record.message)

        handler = self._slog_handlers.get(record.message)
        if handler is not None:
            handler(record)

        if record.level in ("WARN", "ERROR"):
            self._result.incidents.append(self._incident_from(record))

    def _on_server_restart(self, record: SlogRecord) -> None:
        self._registry.on_server_restart()
        self._heuristics.on_server_restart()
        self._result.server_starts.append(record.timestamp)

    def _on_runner_starting(self, record: SlogRecord) -> None:
        command = record.get("cmd", "")
        model_path = self._argument_after(command, "--model") or ""
        self._registry.on_runner_starting(
            blob=model_path.rsplit("/", 1)[-1],
            is_embedding="--embedding" in command.split(),
        )

    def _on_model_loaded(self, record: SlogRecord) -> None:
        runner = self._registry.on_model_loaded(record.get("model", "unknown"), record.timestamp)
        if runner is None or runner.name is None:
            return
        self._heuristics.on_model_loaded(runner)
        self._result.loads.append(
            ModelLoad(record.timestamp, runner.name, self._last_startup_seconds)
        )
        self._last_startup_seconds = None

    def _on_loaded_runners(self, record: SlogRecord) -> None:
        count = record.get("count")
        if count and count.isdigit():
            self._registry.on_loaded_count(int(count))

    def _on_request_finished(self, record: SlogRecord) -> None:
        model_path = record.get("runner.model")
        if model_path:
            runner = self._registry.find(model_path)
            name = record.get("runner.name") or self._registry.name_for(model_path)
        else:
            # Запрос, который сам вызвал загрузку модели: ollama пишет строку без runner.
            runner = self._registry.last_loaded
            name = runner.name if runner else None
        if name is None:
            return
        if runner is not None:
            runner.last_used = record.timestamp
        self._debug_matcher.on_request_finished(
            FinishedRequest(
                timestamp=record.timestamp,
                model=ModelNames.short(name),
                kind=runner.serves if runner else None,
            )
        )

    def _on_timing(self, timing: TimingRecord) -> None:
        if timing.stage == "prompt":
            self._pending_prompt = timing
            return
        if self._last_timestamp is None:
            return
        prompt = self._pending_prompt
        self._pending_prompt = None
        self._result.timings.append(
            GenerationTiming(
                timestamp=self._last_timestamp,
                prompt_tokens=prompt.tokens if prompt else 0,
                prompt_ms=prompt.milliseconds if prompt else 0.0,
                generated_tokens=timing.tokens,
                generation_ms=timing.milliseconds,
            )
        )

    @staticmethod
    def _incident_from(record: SlogRecord) -> Incident:
        details = ", ".join(f"{key}={value}" for key, value in record.attrs.items())
        return Incident(record.timestamp, record.level, record.message, details)

    @staticmethod
    def _parse_startup_seconds(message: str) -> float | None:
        try:
            return float(message.split()[3])
        except (IndexError, ValueError):
            return None

    @staticmethod
    def _argument_after(command: str, flag: str) -> str | None:
        parts = command.split()
        if flag in parts:
            index = parts.index(flag)
            if index + 1 < len(parts):
                return parts[index + 1]
        return None
