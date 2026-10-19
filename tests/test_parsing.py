import unittest
from datetime import datetime, timedelta, timezone

from ollama_watchdog.parsing import (
    GinLineParser,
    GoDurationParser,
    LineNormalizer,
    SlogLineParser,
    TimingLineParser,
)


class GoDurationParserTest(unittest.TestCase):
    def test_parses_all_units(self):
        cases = {
            "33.585µs": timedelta(microseconds=33.585),
            "3.428497852s": timedelta(seconds=3.428497852),
            "12.5ms": timedelta(milliseconds=12.5),
            "1m3s": timedelta(minutes=1, seconds=3),
            "1h2m0s": timedelta(hours=1, minutes=2),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(GoDurationParser.parse(text).total_seconds(), expected.total_seconds())

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            GoDurationParser.parse("soon")


class GinLineParserTest(unittest.TestCase):
    def test_parses_request(self):
        line = '[GIN] 2026/10/15 - 12:37:57 | 500 |          1m3s |  192.168.15.230 | POST     "/api/chat"'
        request = GinLineParser().parse(line)
        self.assertEqual(request.timestamp, datetime(2026, 10, 15, 12, 37, 57, tzinfo=timezone.utc))
        self.assertEqual(request.status, 500)
        self.assertEqual(request.duration, timedelta(seconds=63))
        self.assertEqual(request.client_ip, "192.168.15.230")
        self.assertEqual(request.endpoint, "POST /api/chat")
        self.assertTrue(request.is_inference)
        self.assertTrue(request.is_error)

    def test_ignores_other_lines(self):
        self.assertIsNone(GinLineParser().parse("srv  update_slots: all slots are idle"))


class SlogLineParserTest(unittest.TestCase):
    def test_parses_quoted_and_bare_attributes(self):
        line = (
            'time=2026-10-15T12:37:56.593Z level=INFO source=images.go:381 msg="template selection" '
            'model=registry.ollama.ai/library/qwen3:4b selected=go_template renderer="" '
            'chat_template="[tools thinking completion]"'
        )
        record = SlogLineParser().parse(line)
        self.assertEqual(record.message, "template selection")
        self.assertEqual(record.level, "INFO")
        self.assertEqual(record.get("model"), "registry.ollama.ai/library/qwen3:4b")
        self.assertEqual(record.get("renderer"), "")
        self.assertEqual(record.get("chat_template"), "[tools thinking completion]")
        self.assertEqual(record.timestamp.tzinfo, timezone.utc)


class TimingLineParserTest(unittest.TestCase):
    def test_distinguishes_prompt_and_generation(self):
        parser = TimingLineParser()
        prompt = parser.parse(
            "slot print_timing: id  0 | task 0 | prompt eval time =     137.44 ms /   339 tokens "
            "(    0.41 ms per token,  2466.46 tokens per second)"
        )
        generation = parser.parse(
            "slot print_timing: id  0 | task 0 |        eval time =     675.74 ms /    16 tokens "
            "(   45.05 ms per token,    22.20 tokens per second)"
        )
        self.assertEqual((prompt.stage, prompt.tokens), ("prompt", 339))
        self.assertEqual((generation.stage, generation.tokens), ("generation", 16))
        self.assertIsNone(parser.parse("slot print_timing: id  0 | task 0 |       total time =  813.18 ms /   355 tokens"))


class LineNormalizerTest(unittest.TestCase):
    def test_strips_docker_timestamp(self):
        line = "2026-10-15T09:29:19.123456789Z [GIN] 2026/10/15 - 09:29:19 | 200 | 1ms | 1.1.1.1 | GET \"/\"\n"
        self.assertTrue(LineNormalizer().normalize(line).startswith("[GIN]"))


if __name__ == "__main__":
    unittest.main()
