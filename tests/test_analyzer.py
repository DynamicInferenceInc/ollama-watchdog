import unittest

from ollama_watchdog.analyzer import OllamaLogAnalyzer
from ollama_watchdog.domain import Attribution

EMBED_BLOB = "/root/.ollama/models/blobs/sha256-embed"
CHAT_BLOB = "/root/.ollama/models/blobs/sha256-chat"
OTHER_CHAT_BLOB = "/root/.ollama/models/blobs/sha256-other"


def server_started(clock: str) -> str:
    return f'time=2026-10-15T{clock}.000Z level=INFO source=routes.go:2008 msg="Listening on [::]:11434 (version 0.32.15)"'


def model_loaded(clock: str, blob: str, name: str, embedding: bool = False, runners: int = 1) -> list[str]:
    flags = " --embedding" if embedding else ""
    return [
        f'time=2026-10-15T{clock}.000Z level=INFO source=llama_server.go:433 msg="starting llama-server" '
        f'cmd="/usr/lib/ollama/llama-server --model {blob} --port 1{flags} -ngl 99"',
        f'time=2026-10-15T{clock}.100Z level=INFO source=llama_server.go:1362 msg="llama-server started in 2.29 seconds"',
        f'time=2026-10-15T{clock}.200Z level=INFO source=images.go:381 msg="template selection" '
        f"model=registry.ollama.ai/library/{name} selected=go_template",
        f'time=2026-10-15T{clock}.300Z level=INFO source=sched.go:728 msg="loaded runners" count={runners}',
    ]


def request(clock: str, path: str, status: int = 200) -> str:
    return f'[GIN] 2026/10/15 - {clock} | {status} |  1.5s |  10.0.0.1 | POST     "{path}"'


def finished(clock: str, blob: str | None = None, name: str | None = None) -> str:
    line = f'time=2026-10-15T{clock}.500Z level=DEBUG source=sched.go:538 msg="context for request finished"'
    if blob:
        line += f" runner.name=registry.ollama.ai/library/{name} runner.pid=42 runner.model={blob}"
    return line


class HeuristicAttributionTest(unittest.TestCase):
    def test_load_trigger_and_single_candidate(self):
        lines = [
            server_started("10:00:00"),
            *model_loaded("10:00:01", EMBED_BLOB, "qwen3-embedding:8b", embedding=True),
            request("10:00:02", "/api/embed"),
            request("10:00:03", "/api/embed"),
            *model_loaded("10:00:04", CHAT_BLOB, "qwen3:4b", runners=2),
            request("10:00:05", "/api/chat"),
            request("10:00:06", "/api/embed"),
            request("10:00:07", "/api/chat"),
        ]
        result = OllamaLogAnalyzer().analyze(lines)
        models = [r.model for r in result.requests]
        self.assertEqual(models, ["qwen3-embedding:8b", "qwen3-embedding:8b", "qwen3:4b", "qwen3-embedding:8b", "qwen3:4b"])
        self.assertTrue(all(r.attribution is Attribution.APPROXIMATE for r in result.requests))
        self.assertFalse(result.debug_logging_detected)

    def test_ambiguous_chat_stays_unknown(self):
        lines = [
            server_started("10:00:00"),
            *model_loaded("10:00:01", CHAT_BLOB, "qwen3:4b"),
            request("10:00:02", "/api/chat"),
            *model_loaded("10:00:03", OTHER_CHAT_BLOB, "qwen3.8:27b", runners=2),
            request("10:00:04", "/api/chat"),
            request("10:00:05", "/api/chat"),
        ]
        result = OllamaLogAnalyzer().analyze(lines)
        self.assertEqual([r.model for r in result.requests], ["qwen3:4b", "qwen3.8:27b", None])

    def test_eviction_reduces_candidates(self):
        lines = [
            server_started("10:00:00"),
            *model_loaded("10:00:01", CHAT_BLOB, "qwen3:4b"),
            request("10:00:02", "/api/chat"),
            *model_loaded("10:00:03", OTHER_CHAT_BLOB, "qwen3.8:27b", runners=1),
            request("10:00:04", "/api/chat"),
            request("10:00:05", "/api/chat"),
        ]
        result = OllamaLogAnalyzer().analyze(lines)
        self.assertEqual([r.model for r in result.requests], ["qwen3:4b", "qwen3.8:27b", "qwen3.8:27b"])


class DebugAttributionTest(unittest.TestCase):
    def test_exact_attribution_from_debug_lines(self):
        lines = [
            server_started("10:00:00"),
            *model_loaded("10:00:01", CHAT_BLOB, "qwen3:4b"),
            request("10:00:02", "/api/chat"),
            finished("10:00:02"),
            *model_loaded("10:00:03", OTHER_CHAT_BLOB, "qwen3.8:27b", runners=2),
            request("10:00:04", "/api/chat"),
            finished("10:00:04"),
            request("10:00:05", "/api/chat"),
            finished("10:00:05", CHAT_BLOB, "qwen3:4b"),
            request("10:00:06", "/api/chat", status=500),
            request("10:00:07", "/api/chat"),
            finished("10:00:07", OTHER_CHAT_BLOB, "qwen3.8:27b"),
        ]
        result = OllamaLogAnalyzer().analyze(lines)
        self.assertTrue(result.debug_logging_detected)
        attributed = [(r.model, r.attribution) for r in result.requests]
        self.assertEqual(
            attributed,
            [
                ("qwen3:4b", Attribution.EXACT),
                ("qwen3.8:27b", Attribution.EXACT),
                ("qwen3:4b", Attribution.EXACT),
                (None, Attribution.UNKNOWN),
                ("qwen3.8:27b", Attribution.EXACT),
            ],
        )


if __name__ == "__main__":
    unittest.main()
