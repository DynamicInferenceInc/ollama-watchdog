"""Понятные имена для IP-адресов клиентов."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class InvalidAlias(ValueError):
    pass


class ClientAliases:
    """Имена из настроек (только чтение) плюс заданные в дашборде, которые сохраняются в файл."""

    MAX_LENGTH = 64

    def __init__(self, fixed: dict[str, str] | None = None, file: Path | None = None) -> None:
        self._fixed = dict(fixed or {})
        self._file = file
        self._lock = threading.Lock()
        self._custom = self._load()

    @classmethod
    def parse(cls, spec: str) -> dict[str, str]:
        """`192.168.1.10=Ноутбук, 10.0.0.5=Open WebUI` → {ip: имя}."""
        aliases = {}
        for entry in filter(None, (part.strip() for part in spec.split(","))):
            ip, separator, name = entry.partition("=")
            if not separator:
                raise InvalidAlias(f"ожидается ip=имя, получено «{entry}»")
            aliases[cls._normalize_ip(ip)] = cls._normalize_name(name)
        return aliases

    def snapshot(self) -> dict:
        with self._lock:
            return {"aliases": {**self._custom, **self._fixed}, "fixed": sorted(self._fixed)}

    def set(self, ip: str, name: str) -> dict:
        ip = self._normalize_ip(ip)
        if ip in self._fixed:
            raise InvalidAlias(f"имя для {ip} задано в WATCHDOG_CLIENT_ALIASES и меняется только там")
        name = name.strip()
        with self._lock:
            if name:
                self._custom[ip] = self._normalize_name(name)
            else:
                self._custom.pop(ip, None)
            self._save()
        return self.snapshot()

    @staticmethod
    def _normalize_ip(ip: str) -> str:
        try:
            return str(ipaddress.ip_address(ip.strip()))
        except ValueError as error:
            raise InvalidAlias(f"«{ip.strip()}» — не IP-адрес") from error

    @classmethod
    def _normalize_name(cls, name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise InvalidAlias("имя не может быть пустым")
        if len(name) > cls.MAX_LENGTH:
            raise InvalidAlias(f"имя длиннее {cls.MAX_LENGTH} символов")
        return name

    def _load(self) -> dict[str, str]:
        if self._file is None or not self._file.exists():
            return {}
        try:
            stored = json.loads(self._file.read_text(encoding="utf-8"))
            return {self._normalize_ip(ip): self._normalize_name(name) for ip, name in stored.items()}
        except (OSError, ValueError, AttributeError) as error:
            log.warning("не удалось прочитать %s: %s", self._file, error)
            return {}

    def _save(self) -> None:
        if self._file is None:
            return
        temporary = self._file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._custom, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._file)
