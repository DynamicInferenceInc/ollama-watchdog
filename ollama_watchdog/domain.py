"""Доменные сущности: HTTP-запросы, загрузки моделей, инциденты, тайминги генерации."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class EndpointKind(Enum):
    GENERATION = "generation"
    EMBEDDING = "embedding"
    SERVICE = "service"

    @classmethod
    def from_path(cls, path: str) -> EndpointKind:
        if path in _GENERATION_PATHS:
            return cls.GENERATION
        if path in _EMBEDDING_PATHS:
            return cls.EMBEDDING
        return cls.SERVICE


_GENERATION_PATHS = frozenset({
    "/api/chat",
    "/api/generate",
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/messages",
})

_EMBEDDING_PATHS = frozenset({
    "/api/embed",
    "/api/embeddings",
    "/v1/embeddings",
})


class Attribution(Enum):
    """Насколько уверенно запрос привязан к модели."""

    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class HttpRequest:
    timestamp: datetime
    status: int
    duration: timedelta
    client_ip: str
    method: str
    path: str
    model: str | None = None
    attribution: Attribution = Attribution.UNKNOWN

    @property
    def kind(self) -> EndpointKind:
        return EndpointKind.from_path(self.path)

    @property
    def is_inference(self) -> bool:
        return self.kind is not EndpointKind.SERVICE

    @property
    def is_error(self) -> bool:
        return self.status >= 400

    @property
    def endpoint(self) -> str:
        return f"{self.method} {self.path}"

    def attribute_to(self, model: str, attribution: Attribution) -> None:
        self.model = model
        self.attribution = attribution


@dataclass(frozen=True)
class ModelLoad:
    timestamp: datetime
    model: str
    startup_seconds: float | None


@dataclass(frozen=True)
class Incident:
    """Предупреждение или ошибка самого сервера ollama."""

    timestamp: datetime
    level: str
    message: str
    details: str


@dataclass(frozen=True, slots=True)
class GenerationTiming:
    """Статистика одной задачи llama-server (prompt + генерация)."""

    timestamp: datetime
    prompt_tokens: int
    prompt_ms: float
    generated_tokens: int
    generation_ms: float
