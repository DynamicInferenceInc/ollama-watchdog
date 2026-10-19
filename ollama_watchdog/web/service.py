"""Логика дашборда: какие данные отдаёт API."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from ..analyzer import AnalysisResult
from ..ingest import AnalysisStore, LogCollector
from ..ollama_api import OllamaClient
from ..statistics import Report, ReportBuilder, ReportPeriod
from .serialization import JsonConverter


class BadRequest(ValueError):
    pass


@dataclass(frozen=True)
class PeriodQuery:
    """Период из query-параметров: ?period=today|yesterday|day|hours|all&date=…&hours=…"""

    kind: str
    day: date | None = None
    hours: int | None = None

    MAX_HOURS = 24 * 366

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> PeriodQuery:
        kind = params.get("period", "today")
        if kind == "day":
            try:
                return cls(kind, day=date.fromisoformat(params.get("date", "")))
            except ValueError as error:
                raise BadRequest("параметр date должен быть в формате YYYY-MM-DD") from error
        if kind == "hours":
            hours = params.get("hours", "24")
            if not hours.isdigit() or not 1 <= int(hours) <= cls.MAX_HOURS:
                raise BadRequest(f"параметр hours должен быть числом от 1 до {cls.MAX_HOURS}")
            return cls(kind, hours=int(hours))
        if kind in ("today", "yesterday", "all"):
            return cls(kind)
        raise BadRequest(f"неизвестный период: {kind}")

    def resolve(self, result: AnalysisResult, now: datetime) -> ReportPeriod:
        tz = now.tzinfo
        if self.kind == "today":
            return ReportPeriod.for_day(now.date(), tz)
        if self.kind == "yesterday":
            return ReportPeriod.for_day(now.date() - timedelta(days=1), tz)
        if self.kind == "day":
            return ReportPeriod.for_day(self.day, tz)
        if self.kind == "hours":
            return ReportPeriod.last_hours(self.hours, now)
        return ReportPeriod.everything(result, now)

    def baseline(self, period: ReportPeriod, now: datetime) -> ReportPeriod | None:
        """С чем сравнивать: сегодня — со вчера к этому же часу, иначе — с предыдущим таким же периодом."""
        if not period.comparable:
            return None
        if self.kind == "today":
            return ReportPeriod(period.start - timedelta(days=1), now - timedelta(days=1), "вчера к этому времени")
        return period.previous()

    @property
    def comparison_label(self) -> str | None:
        if self.kind == "hours":
            span = f"{self.hours // 24} дн" if self.hours % 24 == 0 else f"{self.hours} ч"
            return f"по сравнению с предыдущими {span}"
        return {
            "today": "по сравнению со вчерашним днём к этому же времени",
            "yesterday": "по сравнению с позавчера",
            "day": "по сравнению с предыдущим днём",
        }.get(self.kind)

    @property
    def cache_key(self) -> tuple:
        return (self.kind, self.day, self.hours)


class DashboardService:
    CACHE_SECONDS = 15

    def __init__(
        self,
        store: AnalysisStore,
        collector: LogCollector,
        ollama: OllamaClient | None,
        display_timezone: tzinfo,
    ) -> None:
        self._store = store
        self._collector = collector
        self._ollama = ollama
        self._tz = display_timezone
        self._builder = ReportBuilder(display_timezone)
        self._json = JsonConverter(display_timezone)
        self._cache: dict[tuple, tuple[int, float, dict]] = {}
        self._cache_lock = threading.Lock()

    def report(self, query: PeriodQuery) -> dict:
        version = self._store.version
        with self._cache_lock:
            cached = self._cache.get(query.cache_key)
            if cached and cached[0] == version and time.monotonic() - cached[1] < self.CACHE_SECONDS:
                return cached[2]

        now = datetime.now(self._tz)
        with self._store.read() as result:
            period = query.resolve(result, now)
            report = self._builder.build(result, period)
            baseline = query.baseline(period, now)
            if baseline is not None and not self._history_covers(result, baseline):
                baseline = None
            previous = self._builder.build(result, baseline) if baseline else None

        payload = self._json.convert(report)
        payload["query"] = {"period": query.kind, "date": self._json.convert(query.day), "hours": query.hours}
        payload["previous"] = self._comparison(previous)
        payload["comparison_label"] = query.comparison_label if previous else None
        payload["generated_at"] = self._json.convert(now)
        with self._cache_lock:
            self._cache[query.cache_key] = (version, time.monotonic(), payload)
        return payload

    def days(self) -> list[dict]:
        with self._store.read() as result:
            counts = Counter(
                r.timestamp.astimezone(self._tz).date() for r in result.requests if r.is_inference
            )
        return [{"date": day.isoformat(), "requests": counts[day]} for day in sorted(counts, reverse=True)]

    def status(self) -> dict:
        return {
            "collector": self._json.convert(self._collector.status),
            "ollama": self._json.convert(self._ollama.status()) if self._ollama else None,
            "ollama_url": self._ollama.base_url if self._ollama else None,
            "timezone": str(self._tz),
            "now": self._json.convert(datetime.now(self._tz)),
        }

    @staticmethod
    def _history_covers(result: AnalysisResult, period: ReportPeriod) -> bool:
        """Сравнивать с прошлым периодом имеет смысл, только если логи за него у нас есть."""
        earliest = min(
            [*(r.timestamp for r in result.requests[:1]), *result.server_starts[:1]],
            default=None,
        )
        return earliest is not None and earliest <= period.start

    @staticmethod
    def _comparison(previous: Report | None) -> dict | None:
        if previous is None:
            return None
        return {
            "inference_requests": previous.inference_requests,
            "inference_errors": previous.inference_errors,
            "active_clients": previous.active_clients,
            "median_latency": previous.latency.median.total_seconds(),
            "generated_tokens": previous.generation.generated_tokens,
            "model_loads": sum(load.loads for load in previous.loads),
        }
