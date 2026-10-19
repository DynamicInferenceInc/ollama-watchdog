import unittest
from datetime import date, timezone

from ollama_watchdog.analyzer import OllamaLogAnalyzer
from ollama_watchdog.statistics import ReportBuilder, ReportPeriod
from tests.test_analyzer import CHAT_BLOB, OTHER_CHAT_BLOB, model_loaded, request, server_started


class UnattributedRequestsTest(unittest.TestCase):
    def test_unknown_model_is_kept_out_of_model_statistics(self):
        lines = [
            server_started("10:00:00"),
            *model_loaded("10:00:01", CHAT_BLOB, "qwen3:4b"),
            request("10:00:02", "/api/chat"),
            *model_loaded("10:00:03", OTHER_CHAT_BLOB, "qwen3.8:27b", runners=2),
            request("10:00:04", "/api/chat"),
            request("10:00:05", "/api/chat", status=499),
        ]
        result = OllamaLogAnalyzer().analyze(lines)
        report = ReportBuilder(timezone.utc).build(result, ReportPeriod.for_day(date(2026, 10, 15), timezone.utc))

        self.assertEqual(report.inference_requests, 3)
        self.assertEqual([m.model for m in report.models], ["qwen3:4b", "qwen3.8:27b"])
        self.assertEqual(report.coverage.unknown, 1)
        self.assertEqual([(r.status, r.endpoint) for r in report.unattributed_requests], [(499, "POST /api/chat")])
        bucket = next(b for b in report.timeline if b.requests)
        self.assertEqual(bucket.requests, 3)
        self.assertEqual(bucket.by_model, {"qwen3:4b": 1, "qwen3.8:27b": 1})


if __name__ == "__main__":
    unittest.main()
