"""Живое состояние ollama через её HTTP API: версия и модели в памяти."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .attribution import ModelNames


@dataclass(frozen=True)
class LoadedModel:
    name: str
    size_bytes: int
    vram_bytes: int
    context_length: int | None
    parameter_size: str | None
    quantization: str | None
    expires_at: datetime | None  # None — модель держится в памяти бессрочно


@dataclass(frozen=True)
class OllamaStatus:
    reachable: bool
    checked_at: datetime
    version: str | None = None
    installed_models: int | None = None
    loaded_models: list[LoadedModel] = field(default_factory=list)
    error: str | None = None


class OllamaClient:
    FOREVER = timedelta(days=365 * 10)

    def __init__(self, base_url: str, timeout: float = 3.0, cache_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._cached: OllamaStatus | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    def status(self) -> OllamaStatus:
        with self._lock:
            if self._cached is None or time.monotonic() - self._cached_at > self._cache_seconds:
                self._cached = self._fetch_status()
                self._cached_at = time.monotonic()
            return self._cached

    def _fetch_status(self) -> OllamaStatus:
        now = datetime.now(timezone.utc)
        try:
            version = self._get("/api/version").get("version")
            running = self._get("/api/ps").get("models", [])
            installed = self._get("/api/tags").get("models", [])
        except (urllib.error.URLError, OSError, ValueError) as error:
            return OllamaStatus(reachable=False, checked_at=now, error=str(getattr(error, "reason", error)))
        return OllamaStatus(
            reachable=True,
            checked_at=now,
            version=version,
            installed_models=len(installed),
            loaded_models=[self._loaded_model(item, now) for item in running],
        )

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self._base_url + path, timeout=self._timeout) as response:
            return json.load(response)

    def _loaded_model(self, item: dict, now: datetime) -> LoadedModel:
        details = item.get("details") or {}
        return LoadedModel(
            name=ModelNames.short(item.get("name") or item.get("model", "?")),
            size_bytes=int(item.get("size") or 0),
            vram_bytes=int(item.get("size_vram") or 0),
            context_length=item.get("context_length"),
            parameter_size=details.get("parameter_size"),
            quantization=details.get("quantization_level"),
            expires_at=self._expiry(item.get("expires_at"), now),
        )

    def _expiry(self, value: str | None, now: datetime) -> datetime | None:
        if not value:
            return None
        try:
            moment = datetime.fromisoformat(self._trim_nanoseconds(value))
        except ValueError:
            return None
        return None if moment - now > self.FOREVER else moment

    @staticmethod
    def _trim_nanoseconds(value: str) -> str:
        """Go отдаёт до 9 знаков после запятой; Python понимает не больше 6."""
        if "." not in value:
            return value
        head, _, tail = value.partition(".")
        digits = len(tail) - len(tail.lstrip("0123456789"))
        return f"{head}.{tail[:min(digits, 6)]}{tail[digits:]}"
