import tempfile
import unittest
from pathlib import Path

from ollama_watchdog.web.aliases import ClientAliases, InvalidAlias


class ClientAliasesTest(unittest.TestCase):
    def test_parse_settings(self):
        parsed = ClientAliases.parse(" 192.168.1.10 = Ноутбук ,10.0.0.5=Open  WebUI,")
        self.assertEqual(parsed, {"192.168.1.10": "Ноутбук", "10.0.0.5": "Open WebUI"})
        with self.assertRaises(InvalidAlias):
            ClientAliases.parse("192.168.1.10")
        with self.assertRaises(InvalidAlias):
            ClientAliases.parse("host.local=Ноутбук")

    def test_custom_aliases_persist_and_fixed_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "aliases.json"
            aliases = ClientAliases({"10.0.0.5": "Open WebUI"}, file)
            aliases.set("192.168.1.10", "  Ноутбук ")
            with self.assertRaises(InvalidAlias):
                aliases.set("10.0.0.5", "другое")

            reloaded = ClientAliases({"10.0.0.5": "Open WebUI"}, file)
            self.assertEqual(
                reloaded.snapshot(),
                {"aliases": {"192.168.1.10": "Ноутбук", "10.0.0.5": "Open WebUI"}, "fixed": ["10.0.0.5"]},
            )
            reloaded.set("192.168.1.10", "")
            self.assertEqual(ClientAliases(file=file).snapshot()["aliases"], {})

    def test_rejects_too_long_name(self):
        with self.assertRaises(InvalidAlias):
            ClientAliases().set("127.0.0.1", "x" * (ClientAliases.MAX_LENGTH + 1))


if __name__ == "__main__":
    unittest.main()
