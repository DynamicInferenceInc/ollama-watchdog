"""Агрегация результатов анализа в отчёт за выбранный период."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .analyzer import AnalysisResult
from .domain import Attribution, HttpRequest, Incident

UNKNOWN_MODEL = "не определена"


@dataclass(frozen=True)
class ReportPeriod:
    start: datetime
    end: datetime
    title: str
    comparable: bool = True

    @classmethod
    def for_day(cls, day: date, tz: tzinfo) -> ReportPeriod:
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        return cls(start, start + timedelta(days=1), f"за {day:%d.%m.%Y}")

    @classmethod
    def last_hours(cls, hours: int, now: datetime) -> ReportPeriod:
        return cls(now - timedelta(hours=hours), now, f"за последние {hours} ч")

    @classmethod
    def everything(cls, result: AnalysisResult, now: datetime) -> ReportPeriod:
        moments = [*(r.timestamp for r in result.requests[:1]), *result.server_starts[:1]]
        first = min(moments, default=now)
        return cls(first, now + timedelta(seconds=1), "за всё время", comparable=False)

    @property
    def length(self) -> timedelta:
        return self.end - self.start

    def previous(self) -> ReportPeriod:
        return ReportPeriod(self.start - self.length, self.start, "предыдущий период")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass(frozen=True)
class LatencyStats:
    average: timedelta
    median: timedelta
    p95: timedelta
    maximum: timedelta

    @classmethod
    def of(cls, durations: Iterable[timedelta]) -> LatencyStats:
        ordered = sorted(durations)
        if not ordered:
            zero = timedelta()
            return cls(zero, zero, zero, zero)
        return cls(
            average=sum(ordered, timedelta()) / len(ordered),
            median=cls._percentile(ordered, 0.50),
            p95=cls._percentile(ordered, 0.95),
            maximum=ordered[-1],
        )

    @staticmethod
    def _percentile(ordered: list[timedelta], fraction: float) -> timedelta:
        index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
        return ordered[index]


@dataclass(frozen=True)
class ModelUsage:
    model: str
    requests: int
    errors: int
    exact: int
    clients: int
    endpoints: dict[str, int]
    latency: LatencyStats
    last_request: datetime


@dataclass(frozen=True)
class EndpointUsage:
    endpoint: str
    is_inference: bool
    requests: int
    errors: int
    latency: LatencyStats


@dataclass(frozen=True)
class ClientUsage:
    client_ip: str
    inference_requests: int
    service_requests: int
    errors: int
    favourite_model: str | None
    last_seen: datetime


@dataclass(frozen=True)
class ModelLoadSummary:
    model: str
    loads: int
    average_startup_seconds: float | None
    last_loaded: datetime


@dataclass(frozen=True)
class GenerationSummary:
    tasks: int
    prompt_tokens: int
    generated_tokens: int
    average_prompt_speed: float  # токенов/с
    average_generation_speed: float  # токенов/с


@dataclass(frozen=True)
class AttributionCoverage:
    exact: int
    approximate: int
    unknown: int


@dataclass(frozen=True)
class DailySummary:
    day: date
    inference_requests: int
    errors: int
    top_model: str | None


@dataclass(frozen=True)
class TimelineBucket:
    start: datetime
    requests: int
    errors: int
    by_model: dict[str, int]


@dataclass(frozen=True)
class RequestRecord:
    timestamp: datetime
    status: int
    endpoint: str
    client_ip: str
    model: str
    attribution: Attribution
    duration: timedelta


@dataclass
class Report:
    period: ReportPeriod
    timezone: tzinfo
    first_event: datetime | None
    last_event: datetime | None
    total_requests: int
    inference_requests: int
    service_requests: int
    inference_errors: int
    active_clients: int
    latency: LatencyStats
    models: list[ModelUsage]
    endpoints: list[EndpointUsage]
    clients: list[ClientUsage]
    hourly: list[int]
    timeline_step: str  # "hour" или "day"
    timeline: list[TimelineBucket]
    daily: list[DailySummary]
    loads: list[ModelLoadSummary]
    generation: GenerationSummary
    coverage: AttributionCoverage
    failed_requests: list[RequestRecord]
    recent_requests: list[RequestRecord]
    incidents: list[Incident]
    server_starts: list[datetime]
    debug_logging_detected: bool


class ReportBuilder:
    HOURLY_TIMELINE_LIMIT = timedelta(days=8)
    RECENT_REQUESTS = 100

    def __init__(self, display_timezone: tzinfo) -> None:
        self._tz = display_timezone

    def build(self, result: AnalysisResult, period: ReportPeriod) -> Report:
        requests = [r for r in result.requests if period.contains(r.timestamp)]
        inference = [r for r in requests if r.is_inference]
        timestamps = [r.timestamp for r in requests]
        timeline_step = "hour" if period.length <= self.HOURLY_TIMELINE_LIMIT else "day"

        return Report(
            period=period,
            timezone=self._tz,
            first_event=min(timestamps, default=None),
            last_event=max(timestamps, default=None),
            total_requests=len(requests),
            inference_requests=len(inference),
            service_requests=len(requests) - len(inference),
            inference_errors=sum(r.is_error for r in inference),
            active_clients=len({r.client_ip for r in inference}),
            latency=LatencyStats.of(r.duration for r in inference),
            models=self._models(inference),
            endpoints=self._endpoints(requests),
            clients=self._clients(requests),
            hourly=self._hourly(inference),
            timeline_step=timeline_step,
            timeline=self._timeline(inference, period, timeline_step),
            daily=self._daily(inference),
            loads=self._loads(result, period),
            generation=self._generation(result, period),
            coverage=self._coverage(inference),
            failed_requests=[self._record(r) for r in inference if r.is_error],
            recent_requests=[self._record(r) for r in inference[-self.RECENT_REQUESTS:]][::-1],
            incidents=[i for i in result.incidents if period.contains(i.timestamp)],
            server_starts=[s for s in result.server_starts if period.contains(s)],
            debug_logging_detected=result.debug_logging_detected,
        )

    @staticmethod
    def _model_of(request: HttpRequest) -> str:
        return request.model or UNKNOWN_MODEL

    def _record(self, request: HttpRequest) -> RequestRecord:
        return RequestRecord(
            timestamp=request.timestamp,
            status=request.status,
            endpoint=request.endpoint,
            client_ip=request.client_ip,
            model=self._model_of(request),
            attribution=request.attribution,
            duration=request.duration,
        )

    def _models(self, inference: list[HttpRequest]) -> list[ModelUsage]:
        groups: dict[str, list[HttpRequest]] = defaultdict(list)
        for request in inference:
            groups[self._model_of(request)].append(request)
        usages = [
            ModelUsage(
                model=model,
                requests=len(items),
                errors=sum(r.is_error for r in items),
                exact=sum(r.attribution is Attribution.EXACT for r in items),
                clients=len({r.client_ip for r in items}),
                endpoints=dict(Counter(r.path for r in items).most_common()),
                latency=LatencyStats.of(r.duration for r in items),
                last_request=items[-1].timestamp,
            )
            for model, items in groups.items()
        ]
        return sorted(usages, key=lambda u: (u.model == UNKNOWN_MODEL, -u.requests))

    @staticmethod
    def _endpoints(requests: list[HttpRequest]) -> list[EndpointUsage]:
        groups: dict[str, list[HttpRequest]] = defaultdict(list)
        for request in requests:
            groups[request.endpoint].append(request)
        usages = [
            EndpointUsage(
                endpoint=endpoint,
                is_inference=items[0].is_inference,
                requests=len(items),
                errors=sum(r.is_error for r in items),
                latency=LatencyStats.of(r.duration for r in items),
            )
            for endpoint, items in groups.items()
        ]
        return sorted(usages, key=lambda u: (not u.is_inference, -u.requests))

    def _clients(self, requests: list[HttpRequest]) -> list[ClientUsage]:
        groups: dict[str, list[HttpRequest]] = defaultdict(list)
        for request in requests:
            groups[request.client_ip].append(request)
        usages = []
        for client_ip, items in groups.items():
            inference = [r for r in items if r.is_inference]
            known_models = Counter(r.model for r in inference if r.model)
            usages.append(
                ClientUsage(
                    client_ip=client_ip,
                    inference_requests=len(inference),
                    service_requests=len(items) - len(inference),
                    errors=sum(r.is_error for r in items),
                    favourite_model=known_models.most_common(1)[0][0] if known_models else None,
                    last_seen=items[-1].timestamp,
                )
            )
        return sorted(usages, key=lambda u: (-u.inference_requests, -u.service_requests))

    def _hourly(self, inference: list[HttpRequest]) -> list[int]:
        hours = [0] * 24
        for request in inference:
            hours[request.timestamp.astimezone(self._tz).hour] += 1
        return hours

    def _bucket_start(self, moment: datetime, step: str) -> datetime:
        local = moment.astimezone(self._tz).replace(minute=0, second=0, microsecond=0)
        return local.replace(hour=0) if step == "day" else local

    def _timeline(self, inference: list[HttpRequest], period: ReportPeriod, step: str) -> list[TimelineBucket]:
        delta = timedelta(days=1) if step == "day" else timedelta(hours=1)
        keys: list[datetime] = []
        cursor = self._bucket_start(period.start, step)
        while cursor < period.end:
            keys.append(cursor)
            cursor = self._bucket_start(cursor + delta, step)

        requests: Counter[datetime] = Counter()
        errors: Counter[datetime] = Counter()
        by_model: dict[datetime, Counter[str]] = defaultdict(Counter)
        for request in inference:
            key = self._bucket_start(request.timestamp, step)
            requests[key] += 1
            errors[key] += request.is_error
            by_model[key][self._model_of(request)] += 1

        return [
            TimelineBucket(start=key, requests=requests[key], errors=errors[key], by_model=dict(by_model[key]))
            for key in keys
        ]

    def _daily(self, inference: list[HttpRequest]) -> list[DailySummary]:
        groups: dict[date, list[HttpRequest]] = defaultdict(list)
        for request in inference:
            groups[request.timestamp.astimezone(self._tz).date()].append(request)
        summaries = []
        for day in sorted(groups):
            items = groups[day]
            known_models = Counter(r.model for r in items if r.model)
            summaries.append(
                DailySummary(
                    day=day,
                    inference_requests=len(items),
                    errors=sum(r.is_error for r in items),
                    top_model=known_models.most_common(1)[0][0] if known_models else None,
                )
            )
        return summaries

    @staticmethod
    def _loads(result: AnalysisResult, period: ReportPeriod) -> list[ModelLoadSummary]:
        groups: dict[str, list] = defaultdict(list)
        for load in result.loads:
            if period.contains(load.timestamp):
                groups[load.model].append(load)
        summaries = []
        for model, loads in groups.items():
            known = [l.startup_seconds for l in loads if l.startup_seconds is not None]
            summaries.append(
                ModelLoadSummary(
                    model=model,
                    loads=len(loads),
                    average_startup_seconds=sum(known) / len(known) if known else None,
                    last_loaded=loads[-1].timestamp,
                )
            )
        return sorted(summaries, key=lambda s: -s.loads)

    @staticmethod
    def _generation(result: AnalysisResult, period: ReportPeriod) -> GenerationSummary:
        timings = [t for t in result.timings if period.contains(t.timestamp)]
        prompt_tokens = sum(t.prompt_tokens for t in timings)
        generated_tokens = sum(t.generated_tokens for t in timings)
        prompt_seconds = sum(t.prompt_ms for t in timings) / 1000
        generation_seconds = sum(t.generation_ms for t in timings) / 1000
        return GenerationSummary(
            tasks=len(timings),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            average_prompt_speed=prompt_tokens / prompt_seconds if prompt_seconds else 0.0,
            average_generation_speed=generated_tokens / generation_seconds if generation_seconds else 0.0,
        )

    @staticmethod
    def _coverage(inference: list[HttpRequest]) -> AttributionCoverage:
        counts = Counter(r.attribution for r in inference)
        return AttributionCoverage(
            exact=counts[Attribution.EXACT],
            approximate=counts[Attribution.APPROXIMATE],
            unknown=counts[Attribution.UNKNOWN],
        )
