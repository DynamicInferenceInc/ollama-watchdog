import io
import struct
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ollama_watchdog.docker_api import DockerClient, DockerTimestamp
from ollama_watchdog.ingest import AnalysisStore, CollectorState, LogArchive, LogCollector, LogFeed
from ollama_watchdog.web.service import BadRequest, PeriodQuery


def docker_line(stamp: str, text: str) -> str:
    return f"{stamp} {text}\n"


def gin(clock: str, path: str = "/api/embed") -> str:
    return f'[GIN] 2026/10/15 - {clock} | 200 |  50ms |  10.0.0.1 | POST     "{path}"'


class DockerTimestampTest(unittest.TestCase):
    def test_parses_nanoseconds_and_orders(self):
        early = DockerTimestamp.parse("2026-10-15T09:29:19.000000123Z")
        late = DockerTimestamp.parse("2026-10-15T09:29:19.5Z")
        self.assertEqual(early.nanos, 123)
        self.assertEqual(late.nanos, 500_000_000)
        self.assertLess(early, late)
        self.assertEqual(late.as_query(), f"{late.seconds}.500000000")

    def test_rejects_lines_without_timestamp(self):
        self.assertIsNone(DockerTimestamp.of_line('[GIN] 2026/10/15 - 09:29:19 | 200 | ...'))


class DockerDemultiplexTest(unittest.TestCase):
    def test_joins_frames_into_lines_per_stream(self):
        def frame(stream: int, data: bytes) -> bytes:
            return struct.pack(">BxxxL", stream, len(data)) + data

        payload = frame(2, b"first line\nsecond ") + frame(1, b"stdout line\n") + frame(2, b"part\n")
        lines = list(DockerClient()._demultiplex(io.BytesIO(payload)))
        self.assertEqual(lines, ["first line", "stdout line", "second part"])


class FakeFeed(LogFeed):
    """Отдаёт заранее заданные «сеансы» логов, как docker при переподключениях."""

    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.requested_since = []
        self.exhausted = threading.Event()

    @property
    def description(self):
        return "fake"

    @property
    def follows(self):
        return True

    def lines(self, since):
        self.requested_since.append(since)
        if not self._sessions:
            self.exhausted.set()
            raise OSError("больше данных нет")
        return iter(self._sessions.pop(0))


class LogCollectorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.archive_path = Path(self.directory.name) / "ollama.log"

    def tearDown(self):
        self.directory.cleanup()

    def _run(self, sessions):
        store = AnalysisStore(timezone.utc)
        feed = FakeFeed(sessions)
        collector = LogCollector(store, feed, LogArchive(self.archive_path), retention=None)
        collector.RECONNECT_DELAYS = (0.05,)
        collector.start()
        self.assertTrue(feed.exhausted.wait(timeout=5))
        for _ in range(50):
            if collector.status.state is CollectorState.RECONNECTING:
                break
            threading.Event().wait(0.01)
        return store, feed, collector

    def test_skips_duplicates_after_reconnect_and_archives_useful_lines(self):
        first = [
            docker_line("2026-10-15T10:00:01.1Z", gin("10:00:01")),
            docker_line("2026-10-15T10:00:02.1Z", "llama_model_loader: - kv 1: noisy line"),
        ]
        second = [
            docker_line("2026-10-15T10:00:01.1Z", gin("10:00:01")),
            docker_line("2026-10-15T10:00:03.1Z", gin("10:00:03")),
        ]
        store, feed, collector = self._run([first, second])

        with store.read() as result:
            self.assertEqual(len(result.requests), 2)
        self.assertIsNone(feed.requested_since[0])
        self.assertEqual(feed.requested_since[1], DockerTimestamp.parse("2026-10-15T10:00:02.1Z"))
        archived = self.archive_path.read_text().splitlines()
        self.assertEqual(len(archived), 2)
        self.assertTrue(all("[GIN]" in line for line in archived))
        self.assertEqual(collector.status.state, CollectorState.RECONNECTING)

    def test_restores_history_from_archive_on_restart(self):
        self.archive_path.write_text(docker_line("2026-10-15T10:00:01.1Z", gin("10:00:01")))
        store, feed, collector = self._run([[docker_line("2026-10-15T10:00:05.1Z", gin("10:00:05"))]])

        with store.read() as result:
            self.assertEqual(len(result.requests), 2)
        self.assertEqual(feed.requested_since[0], DockerTimestamp.parse("2026-10-15T10:00:01.1Z"))
        self.assertEqual(collector.status.archived_lines_replayed, 1)


class PeriodQueryTest(unittest.TestCase):
    def test_parses_supported_periods(self):
        self.assertEqual(PeriodQuery.from_params({}).kind, "today")
        self.assertEqual(PeriodQuery.from_params({"period": "day", "date": "2026-10-15"}).day, date(2026, 10, 15))
        self.assertEqual(PeriodQuery.from_params({"period": "hours", "hours": "48"}).hours, 48)

    def test_rejects_invalid_input(self):
        for params in ({"period": "week"}, {"period": "day", "date": "15.10"}, {"period": "hours", "hours": "-1"}):
            with self.subTest(params=params), self.assertRaises(BadRequest):
                PeriodQuery.from_params(params)

    def test_today_compares_with_same_time_yesterday(self):
        now = datetime(2026, 10, 19, 15, 30, tzinfo=timezone.utc)
        query = PeriodQuery.from_params({"period": "today"})
        baseline = query.baseline(query.resolve(None, now), now)
        self.assertEqual(baseline.start, datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertEqual(baseline.end, datetime(2026, 10, 18, 15, 30, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
