"""Превращение доменных объектов в JSON-совместимые структуры."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, tzinfo
from enum import Enum


class JsonConverter:
    """Даты — ISO 8601 в часовом поясе дашборда, длительности — секунды."""

    def __init__(self, display_timezone: tzinfo) -> None:
        self._tz = display_timezone

    def convert(self, value: object) -> object:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: self.convert(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, dict):
            return {str(self.convert(k)): self.convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.convert(item) for item in value]
        if isinstance(value, datetime):
            return value.astimezone(self._tz).isoformat(timespec="seconds")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, timedelta):
            return round(value.total_seconds(), 6)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, tzinfo):
            return str(value)
        return value
