"""Привязка HTTP-запросов к моделям.

В access-логе ollama нет имени модели, поэтому модель определяется косвенно:

* `DebugRequestMatcher` — точный способ. При OLLAMA_DEBUG=1 планировщик после
  каждого инференс-запроса пишет `context for request finished` с именем модели.
* `HeuristicAttributor` — запасной способ для обычных INFO-логов: запрос,
  вызвавший загрузку модели, и случай, когда подходящая модель загружена одна.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import Attribution, EndpointKind, HttpRequest


class ModelNames:
    _REGISTRY_PREFIXES = ("registry.ollama.ai/library/", "registry.ollama.ai/")

    @classmethod
    def short(cls, full_name: str) -> str:
        for prefix in cls._REGISTRY_PREFIXES:
            if full_name.startswith(prefix):
                return full_name[len(prefix):]
        return full_name

    @staticmethod
    def blob_of(model_path: str) -> str:
        return model_path.rsplit("/", 1)[-1]


@dataclass
class Runner:
    """Процесс llama-server, обслуживающий одну модель."""

    blob: str
    is_embedding: bool
    name: str | None = None
    last_used: datetime | None = None

    @property
    def serves(self) -> EndpointKind:
        return EndpointKind.EMBEDDING if self.is_embedding else EndpointKind.GENERATION


class RunnerRegistry:
    """Какие модели сейчас загружены в ollama (насколько это видно из логов)."""

    def __init__(self) -> None:
        self._runners: dict[str, Runner] = {}
        self._starting: Runner | None = None
        self._names_by_blob: dict[str, str] = {}
        self.last_loaded: Runner | None = None

    def on_runner_starting(self, blob: str, is_embedding: bool) -> None:
        self._starting = Runner(blob=blob, is_embedding=is_embedding)

    def on_model_loaded(self, full_name: str, timestamp: datetime) -> Runner | None:
        runner = self._starting
        if runner is None:
            return None
        self._starting = None
        runner.name = ModelNames.short(full_name)
        runner.last_used = timestamp
        self._runners[runner.blob] = runner
        self._names_by_blob[runner.blob] = runner.name
        self.last_loaded = runner
        return runner

    def on_loaded_count(self, count: int) -> None:
        """Ollama сообщает число загруженных моделей; лишние считаем выгруженными (LRU)."""
        while len(self._runners) > max(count, 1):
            evicted = min(
                (r for r in self._runners.values() if r is not self.last_loaded),
                key=lambda r: r.last_used or datetime.min,
            )
            del self._runners[evicted.blob]

    def on_server_restart(self) -> None:
        self._runners.clear()
        self._starting = None
        self.last_loaded = None

    def name_for(self, model_path: str) -> str | None:
        return self._names_by_blob.get(ModelNames.blob_of(model_path))

    def find(self, model_path: str) -> Runner | None:
        return self._runners.get(ModelNames.blob_of(model_path))

    def candidates_for(self, kind: EndpointKind) -> list[Runner]:
        return [r for r in self._runners.values() if r.serves is kind]


class HeuristicAttributor:
    def __init__(self, registry: RunnerRegistry) -> None:
        self._registry = registry
        self._awaiting_trigger: dict[EndpointKind, Runner] = {}

    def on_model_loaded(self, runner: Runner) -> None:
        self._awaiting_trigger[runner.serves] = runner

    def on_server_restart(self) -> None:
        self._awaiting_trigger.clear()

    def attribute(self, request: HttpRequest) -> None:
        runner = self._awaiting_trigger.pop(request.kind, None)
        if runner is None:
            candidates = self._registry.candidates_for(request.kind)
            if len(candidates) == 1:
                runner = candidates[0]
        if runner is not None and runner.name is not None:
            request.attribute_to(runner.name, Attribution.APPROXIMATE)
            runner.last_used = request.timestamp


@dataclass(frozen=True)
class FinishedRequest:
    timestamp: datetime
    model: str
    kind: EndpointKind | None


class DebugRequestMatcher:
    """Сопоставляет строки `[GIN]` со строками `context for request finished`.

    GIN пишет строку сразу по завершении обработчика, а планировщик — сразу после
    неё, поэтому ближайший по времени незакрытый запрос и есть нужный.
    """

    MATCH_WINDOW = timedelta(seconds=5)

    def __init__(self) -> None:
        self._open_requests: deque[HttpRequest] = deque()
        self._unmatched_finishes: deque[FinishedRequest] = deque()
        self.enabled = False

    def on_request(self, request: HttpRequest) -> None:
        self._drop_stale_requests(request.timestamp)
        self._drop_stale_finishes(request.timestamp)
        finish = self._take_finish_for(request)
        if finish is not None:
            request.attribute_to(finish.model, Attribution.EXACT)
        else:
            self._open_requests.append(request)

    def on_request_finished(self, finish: FinishedRequest) -> None:
        self.enabled = True
        self._drop_stale_requests(finish.timestamp)
        request = self._take_request_for(finish)
        if request is not None:
            request.attribute_to(finish.model, Attribution.EXACT)
        else:
            self._unmatched_finishes.append(finish)

    def _take_request_for(self, finish: FinishedRequest) -> HttpRequest | None:
        for request in reversed(self._open_requests):
            if finish.kind is None or request.kind is finish.kind:
                self._open_requests.remove(request)
                return request
        return None

    def _take_finish_for(self, request: HttpRequest) -> FinishedRequest | None:
        for finish in self._unmatched_finishes:
            if finish.kind is None or finish.kind is request.kind:
                self._unmatched_finishes.remove(finish)
                return finish
        return None

    def _drop_stale_requests(self, now: datetime) -> None:
        while self._open_requests and self._open_requests[0].timestamp < now - self.MATCH_WINDOW:
            self._open_requests.popleft()

    def _drop_stale_finishes(self, now: datetime) -> None:
        while self._unmatched_finishes and self._unmatched_finishes[0].timestamp < now - self.MATCH_WINDOW:
            self._unmatched_finishes.popleft()
