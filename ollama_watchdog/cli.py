"""Командная строка ollama-watchdog."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analyzer import AnalysisResult, OllamaLogAnalyzer
from .rendering import ConsoleReportRenderer, JsonReportRenderer
from .sources import DockerLogSource, FileLogSource, LogSource, LogSourceError, StdinLogSource
from .statistics import ReportBuilder, ReportPeriod

EXAMPLES = """\
примеры:
  %(prog)s ../ollama-logs/*.txt                      последний день из файлов
  %(prog)s --docker ollama --today                   локальный контейнер, сегодня
  %(prog)s --ssh user@gpu-box --docker ollama --yesterday
  %(prog)s --ssh user@gpu-box --docker ollama --last-hours 6 --watch 60
  ssh gpu-box 'docker logs ollama 2>&1' | %(prog)s --date 2026-10-15
"""


@dataclass(frozen=True)
class PeriodSelector:
    """Какой период показать; «последний день в логе» вычисляется по данным."""

    day: date | None = None
    relative_day: int | None = None
    last_hours: int | None = None
    everything: bool = False

    def resolve(self, result: AnalysisResult, tz: tzinfo) -> ReportPeriod:
        if self.everything:
            return ReportPeriod.everything(result, datetime.now(tz))
        if self.last_hours is not None:
            return ReportPeriod.last_hours(self.last_hours, datetime.now(tz))
        if self.day is not None:
            return ReportPeriod.for_day(self.day, tz)
        if self.relative_day is not None:
            return ReportPeriod.for_day(datetime.now(tz).date() - timedelta(days=self.relative_day), tz)
        return ReportPeriod.for_day(self._last_day_in(result, tz), tz)

    @staticmethod
    def _last_day_in(result: AnalysisResult, tz: tzinfo) -> date:
        if not result.requests:
            return datetime.now(tz).date()
        return max(r.timestamp for r in result.requests).astimezone(tz).date()


class WatchdogApp:
    def __init__(
        self,
        source: LogSource,
        period: PeriodSelector,
        renderer: ConsoleReportRenderer | JsonReportRenderer,
        display_timezone: tzinfo,
        server_timezone: tzinfo,
    ) -> None:
        self._source = source
        self._period = period
        self._renderer = renderer
        self._display_timezone = display_timezone
        self._server_timezone = server_timezone

    def report_once(self) -> str:
        result = OllamaLogAnalyzer(self._server_timezone).analyze(self._source.lines())
        period = self._period.resolve(result, self._display_timezone)
        report = ReportBuilder(self._display_timezone).build(result, period)
        return self._renderer.render(report)

    def watch(self, interval_seconds: int) -> None:
        if not self._source.supports_rereading:
            raise LogSourceError("режим --watch не работает с чтением из stdin")
        while True:
            output = self.report_once()
            updated = datetime.now(self._display_timezone).strftime("%H:%M:%S")
            sys.stdout.write("\033[2J\033[H" + output)
            sys.stdout.write(f"\nОбновлено в {updated}, следующее обновление через {interval_seconds} с (Ctrl+C — выход)\n")
            sys.stdout.flush()
            time.sleep(interval_seconds)


class ArgumentsParser:
    def __init__(self) -> None:
        self._parser = self._build()

    def parse(self, argv: list[str] | None) -> argparse.Namespace:
        return self._parser.parse_args(argv)

    def error(self, message: str) -> None:
        self._parser.error(message)

    @staticmethod
    def _build() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="ollama-watchdog",
            description="Статистика запросов к моделям ollama по её логам.",
            epilog=EXAMPLES,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("files", nargs="*", type=Path, help="файлы логов (можно .gz); без них — stdin")

        source = parser.add_argument_group("источник логов")
        source.add_argument("--docker", metavar="CONTAINER", help="читать `docker logs CONTAINER`")
        source.add_argument("--ssh", metavar="HOST", help="выполнить docker logs на удалённой машине через ssh")
        source.add_argument("--since", metavar="SINCE", help="передать в docker logs --since (например 48h)")

        period = parser.add_argument_group("период").add_mutually_exclusive_group()
        period.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="конкретный день")
        period.add_argument("--today", action="store_true", help="сегодня")
        period.add_argument("--yesterday", action="store_true", help="вчера")
        period.add_argument("--last-hours", type=int, metavar="N", help="последние N часов")
        period.add_argument("--all", action="store_true", help="весь лог целиком")

        output = parser.add_argument_group("вывод")
        output.add_argument("--tz", help="часовой пояс отчёта, например Europe/Moscow (по умолчанию системный)")
        output.add_argument("--server-tz", default="UTC", help="часовой пояс контейнера ollama (по умолчанию UTC)")
        output.add_argument("--top", type=int, default=10, help="сколько строк в списках клиентов и ошибок")
        output.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
        output.add_argument("--no-color", action="store_true", help="без цветов")
        output.add_argument("--watch", type=int, metavar="SECONDS", help="обновлять отчёт каждые N секунд")
        return parser


def _timezone(name: str | None) -> tzinfo:
    if name is None:
        return datetime.now().astimezone().tzinfo or timezone.utc
    return ZoneInfo(name)


def _source_from(args: argparse.Namespace, arguments: ArgumentsParser) -> LogSource:
    if args.docker:
        if args.files:
            arguments.error("укажите либо файлы, либо --docker")
        return DockerLogSource(args.docker, ssh_host=args.ssh, since=args.since)
    if args.ssh or args.since:
        arguments.error("--ssh и --since работают только вместе с --docker")
    if args.files:
        return FileLogSource(args.files)
    if sys.stdin.isatty():
        arguments.error("укажите файлы логов, --docker или передайте лог через stdin")
    return StdinLogSource()


def main(argv: list[str] | None = None) -> int:
    arguments = ArgumentsParser()
    args = arguments.parse(argv)

    try:
        display_timezone = _timezone(args.tz)
        server_timezone = _timezone(args.server_tz)
    except ZoneInfoNotFoundError as error:
        arguments.error(f"неизвестный часовой пояс: {error}")

    period = PeriodSelector(
        day=args.date,
        relative_day=0 if args.today else 1 if args.yesterday else None,
        last_hours=args.last_hours,
        everything=args.all,
    )
    if args.json:
        renderer = JsonReportRenderer()
    else:
        renderer = ConsoleReportRenderer(use_color=sys.stdout.isatty() and not args.no_color, top=args.top)

    try:
        app = WatchdogApp(_source_from(args, arguments), period, renderer, display_timezone, server_timezone)
        if args.watch:
            app.watch(args.watch)
        else:
            sys.stdout.write(app.report_once())
    except LogSourceError as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
