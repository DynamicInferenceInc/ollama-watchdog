"""Человекочитаемый вывод отчёта: консоль и JSON."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, tzinfo
from enum import Enum

from .statistics import UNKNOWN_MODEL, Report


class Style:
    _CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def __getattr__(self, name: str) -> Callable[[str], str]:
        code = self._CODES.get(name)
        if code is None:
            raise AttributeError(name)
        return lambda text: f"\033[{code}m{text}\033[0m" if self._enabled else text


class Format:
    @staticmethod
    def number(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    @staticmethod
    def percent(part: int, total: int) -> str:
        return f"{part / total:.1%}" if total else "—"

    @staticmethod
    def duration(value: timedelta) -> str:
        seconds = value.total_seconds()
        if seconds < 0.001:
            return "<1 мс"
        if seconds < 1:
            return f"{seconds * 1000:.0f} мс"
        if seconds < 60:
            return f"{seconds:.1f} с"
        minutes, secs = divmod(round(seconds), 60)
        if minutes < 60:
            return f"{minutes} мин {secs} с"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} ч {minutes} мин"

    @staticmethod
    def moment(value: datetime, tz: tzinfo, with_date: bool = True) -> str:
        local = value.astimezone(tz)
        return local.strftime("%d.%m %H:%M:%S" if with_date else "%H:%M:%S")


class Table:
    def __init__(self, headers: Sequence[str], align: str) -> None:
        if len(headers) != len(align):
            raise ValueError("у каждой колонки должно быть выравнивание")
        self._headers = list(headers)
        self._align = align
        self._rows: list[tuple[list[str], Callable[[str], str] | None]] = []

    def add_row(self, *cells: object, style: Callable[[str], str] | None = None) -> None:
        self._rows.append(([str(c) for c in cells], style))

    def render(self, style: Style, indent: str = "  ") -> list[str]:
        widths = [len(h) for h in self._headers]
        for cells, _ in self._rows:
            widths = [max(w, len(c)) for w, c in zip(widths, cells)]

        def line(cells: Sequence[str]) -> str:
            padded = (
                c.ljust(w) if a == "<" else c.rjust(w)
                for c, w, a in zip(cells, widths, self._align)
            )
            return indent + "  ".join(padded).rstrip()

        output = [style.bold(line(self._headers)), style.dim(indent + "─" * (sum(widths) + 2 * (len(widths) - 1)))]
        for cells, row_style in self._rows:
            text = line(cells)
            output.append(row_style(text) if row_style else text)
        return output


class ConsoleReportRenderer:
    BAR_WIDTH = 40

    def __init__(self, use_color: bool, top: int = 10) -> None:
        self._style = Style(use_color)
        self._top = top

    def render(self, report: Report) -> str:
        sections = [
            self._header(report),
            self._summary(report),
            self._models(report),
            self._hourly(report),
            self._daily(report),
            self._endpoints(report),
            self._clients(report),
            self._loads(report),
            self._generation(report),
            self._failures(report),
            self._incidents(report),
            self._hints(report),
        ]
        return "\n\n".join("\n".join(lines) for lines in sections if lines) + "\n"

    def _title(self, text: str) -> str:
        return self._style.cyan(self._style.bold(f"▌ {text}"))

    def _header(self, report: Report) -> list[str]:
        title = f"OLLAMA WATCHDOG — статистика {report.period.title}"
        lines = [self._style.bold(title), self._style.dim("═" * len(title))]
        if report.first_event and report.last_event:
            lines.append(
                f"Данные в логе: {Format.moment(report.first_event, report.timezone)}"
                f" — {Format.moment(report.last_event, report.timezone)}  (часовой пояс: {report.timezone})"
            )
        else:
            lines.append(self._style.yellow("За выбранный период запросов в логе нет."))
        return lines

    def _summary(self, report: Report) -> list[str]:
        if not report.total_requests:
            return []
        errors = report.inference_errors
        error_text = f"{Format.number(errors)} ({Format.percent(errors, report.inference_requests)})"
        lines = [
            self._title("Сводка"),
            f"  Запросов к моделям:    {self._style.bold(Format.number(report.inference_requests))}",
            f"  Из них с ошибкой:      {self._style.red(error_text) if errors else error_text}",
            f"  Служебных запросов:    {Format.number(report.service_requests)}  (/api/ps, /api/tags, HEAD / …)",
            f"  Загрузок моделей:      {Format.number(sum(l.loads for l in report.loads))}",
        ]
        if report.server_starts:
            starts = ", ".join(Format.moment(s, report.timezone) for s in report.server_starts)
            lines.append(f"  Перезапуски ollama:    {self._style.yellow(starts)}")
        return lines

    def _models(self, report: Report) -> list[str]:
        if not report.models:
            return []
        table = Table(
            ["Модель", "Запросов", "Доля", "Ошибок", "Клиентов", "Среднее", "p95", "Макс", "Эндпоинты"],
            "<>>>>>>><",
        )
        for usage in report.models:
            marker = "" if usage.model == UNKNOWN_MODEL or usage.exact == usage.requests else " ≈"
            endpoints = ", ".join(f"{path} {count}" for path, count in usage.endpoints.items())
            table.add_row(
                usage.model + marker,
                Format.number(usage.requests),
                Format.percent(usage.requests, report.inference_requests),
                Format.number(usage.errors),
                usage.clients,
                Format.duration(usage.latency.average),
                Format.duration(usage.latency.p95),
                Format.duration(usage.latency.maximum),
                endpoints,
                style=self._style.dim if usage.model == UNKNOWN_MODEL else None,
            )
        coverage = report.coverage
        total = report.inference_requests
        legend = (
            f"  Привязка к модели: точно {Format.percent(coverage.exact, total)}, "
            f"приблизительно {Format.percent(coverage.approximate, total)}, "
            f"не определена {Format.percent(coverage.unknown, total)}.  «≈» — есть приблизительные."
        )
        return [self._title("Запросы по моделям"), *table.render(self._style), self._style.dim(legend)]

    def _hourly(self, report: Report) -> list[str]:
        peak = max(report.hourly, default=0)
        if not peak:
            return []
        active_hours = [hour for hour, count in enumerate(report.hourly) if count]
        lines = [self._title("Активность по часам (запросы к моделям)")]
        for hour in range(active_hours[0], active_hours[-1] + 1):
            count = report.hourly[hour]
            bar = "█" * max(1, round(count / peak * self.BAR_WIDTH)) if count else ""
            lines.append(f"  {hour:02d}:00  {self._style.green(bar.ljust(self.BAR_WIDTH))}  {Format.number(count)}")
        return lines

    def _daily(self, report: Report) -> list[str]:
        if len(report.daily) < 2:
            return []
        table = Table(["День", "Запросов", "Ошибок", "Самая популярная модель"], "<>><")
        for day in report.daily:
            table.add_row(f"{day.day:%d.%m.%Y}", Format.number(day.inference_requests), day.errors, day.top_model or "—")
        return [self._title("По дням"), *table.render(self._style)]

    def _endpoints(self, report: Report) -> list[str]:
        if not report.endpoints:
            return []
        table = Table(["Эндпоинт", "Тип", "Запросов", "Ошибок", "Среднее", "p95", "Макс"], "<<>>>>>")
        for usage in report.endpoints:
            table.add_row(
                usage.endpoint,
                "модель" if usage.is_inference else "служебный",
                Format.number(usage.requests),
                Format.number(usage.errors),
                Format.duration(usage.latency.average),
                Format.duration(usage.latency.p95),
                Format.duration(usage.latency.maximum),
                style=None if usage.is_inference else self._style.dim,
            )
        return [self._title("Эндпоинты"), *table.render(self._style)]

    def _clients(self, report: Report) -> list[str]:
        if not report.clients:
            return []
        table = Table(["Клиент (IP)", "К моделям", "Служебных", "Ошибок", "Чаще всего"], "<>>><")
        for usage in report.clients[: self._top]:
            table.add_row(
                usage.client_ip,
                Format.number(usage.inference_requests),
                Format.number(usage.service_requests),
                Format.number(usage.errors),
                usage.favourite_model or "—",
            )
        return [self._title("Клиенты"), *table.render(self._style)]

    def _loads(self, report: Report) -> list[str]:
        if not report.loads:
            return []
        table = Table(["Модель", "Загрузок", "Среднее время старта"], "<>>")
        for load in report.loads:
            startup = f"{load.average_startup_seconds:.1f} с" if load.average_startup_seconds else "—"
            table.add_row(load.model, load.loads, startup)
        return [self._title("Загрузки моделей в память"), *table.render(self._style)]

    def _generation(self, report: Report) -> list[str]:
        generation = report.generation
        if not generation.tasks:
            return []
        return [
            self._title("Токены (по всем моделям генерации)"),
            f"  Задач генерации:       {Format.number(generation.tasks)}",
            f"  Токенов на входе:      {Format.number(generation.prompt_tokens)}"
            f"  (в среднем {generation.average_prompt_speed:.0f} ток/с)",
            f"  Токенов сгенерировано: {Format.number(generation.generated_tokens)}"
            f"  (в среднем {generation.average_generation_speed:.1f} ток/с)",
        ]

    def _failures(self, report: Report) -> list[str]:
        if not report.failed_requests:
            return []
        table = Table(["Время", "Код", "Эндпоинт", "Модель", "Клиент", "Длительность"], "<><<<>")
        for failure in report.failed_requests[-self._top:]:
            table.add_row(
                Format.moment(failure.timestamp, report.timezone),
                failure.status,
                failure.endpoint,
                failure.model,
                failure.client_ip,
                Format.duration(failure.duration),
                style=self._style.red,
            )
        title = f"Неудачные запросы (последние {min(self._top, len(report.failed_requests))} из {len(report.failed_requests)})"
        return [self._title(title), *table.render(self._style)]

    def _incidents(self, report: Report) -> list[str]:
        if not report.incidents:
            return []
        grouped = Counter((i.level, i.message) for i in report.incidents)
        last_seen = {(i.level, i.message): i.timestamp for i in report.incidents}
        table = Table(["Уровень", "Раз", "Последний", "Сообщение"], "<>><")
        for (level, message), count in grouped.most_common(self._top):
            table.add_row(
                level,
                count,
                Format.moment(last_seen[(level, message)], report.timezone),
                message[:90],
                style=self._style.red if level == "ERROR" else self._style.yellow,
            )
        return [self._title("Предупреждения и ошибки сервера"), *table.render(self._style)]

    def _hints(self, report: Report) -> list[str]:
        if report.debug_logging_detected or not report.inference_requests:
            return []
        if report.coverage.exact == report.inference_requests:
            return []
        return [
            self._style.yellow(
                "Подсказка: в обычных логах ollama нет имени модели у запроса, поэтому часть запросов\n"
                "привязана приблизительно или не определена. Для точной статистики запустите контейнер\n"
                "с переменной окружения OLLAMA_DEBUG=1."
            )
        ]


class JsonReportRenderer:
    def render(self, report: Report) -> str:
        return json.dumps(asdict(report), ensure_ascii=False, indent=2, default=self._default)

    @staticmethod
    def _default(value: object) -> object:
        if isinstance(value, (datetime,)):
            return value.isoformat()
        if isinstance(value, timedelta):
            return value.total_seconds()
        if isinstance(value, Enum):
            return value.value
        return str(value)
