"""Запуск веб-дашборда: настройки из переменных окружения или аргументов."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..docker_api import DockerClient
from ..ingest import AnalysisStore, DockerLogFeed, FileLogFeed, LogArchive, LogCollector, LogFeed
from ..ollama_api import OllamaClient
from .aliases import ClientAliases, InvalidAlias
from .server import ApiRouter, BasicAuth, DashboardServer
from .service import DashboardService

log = logging.getLogger("ollama_watchdog")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    log_files: list[Path]
    container: str
    docker_host: str
    ollama_url: str | None
    timezone: str
    server_timezone: str
    data_dir: Path | None
    retention_days: int | None
    username: str | None
    password: str | None
    client_aliases: dict[str, str]

    @classmethod
    def from_environment(cls, argv: list[str] | None = None) -> Settings:
        env = os.environ
        parser = argparse.ArgumentParser(
            prog="ollama-watchdog-web",
            description="Веб-дашборд статистики ollama. Все параметры можно задать и переменными окружения.",
        )
        parser.add_argument("files", nargs="*", type=Path, help="читать файлы логов вместо docker")
        parser.add_argument("--host", default=env.get("WATCHDOG_HOST", "0.0.0.0"))
        parser.add_argument("--port", type=int, default=int(env.get("WATCHDOG_PORT", "8080")))
        parser.add_argument("--container", default=env.get("OLLAMA_CONTAINER", "ollama"), help="имя контейнера ollama")
        parser.add_argument("--docker-host", default=env.get("DOCKER_HOST", "unix:///var/run/docker.sock"))
        parser.add_argument("--ollama-url", default=env.get("OLLAMA_URL", "http://localhost:11434"),
                            help="адрес API ollama для live-статуса; пустая строка — отключить")
        parser.add_argument("--tz", default=env.get("TZ", "UTC"), help="часовой пояс дашборда")
        parser.add_argument("--server-tz", default=env.get("OLLAMA_SERVER_TZ", "UTC"), help="часовой пояс контейнера ollama")
        parser.add_argument("--data-dir", type=Path, default=env.get("WATCHDOG_DATA_DIR") or None,
                            help="куда сохранять архив логов (без него история живёт только в памяти)")
        parser.add_argument("--retention-days", type=int, default=int(env.get("WATCHDOG_RETENTION_DAYS", "90")),
                            help="сколько дней хранить историю; 0 — бессрочно")
        parser.add_argument("--client-aliases", default=env.get("WATCHDOG_CLIENT_ALIASES", ""),
                            help="имена клиентов: «192.168.1.10=Ноутбук, 10.0.0.5=Open WebUI»")
        args = parser.parse_args(argv)
        try:
            client_aliases = ClientAliases.parse(args.client_aliases)
        except InvalidAlias as error:
            parser.error(f"WATCHDOG_CLIENT_ALIASES: {error}")

        return cls(
            host=args.host,
            port=args.port,
            log_files=args.files or [Path(p) for p in env.get("WATCHDOG_LOG_FILES", "").split(":") if p],
            container=args.container,
            docker_host=args.docker_host,
            ollama_url=args.ollama_url or None,
            timezone=args.tz,
            server_timezone=args.server_tz,
            data_dir=args.data_dir,
            retention_days=args.retention_days or None,
            username=env.get("WATCHDOG_USERNAME") or None,
            password=env.get("WATCHDOG_PASSWORD") or None,
            client_aliases=client_aliases,
        )


class WatchdogWebApp:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        display_timezone = ZoneInfo(settings.timezone)
        self._store = AnalysisStore(ZoneInfo(settings.server_timezone))
        self._collector = LogCollector(
            store=self._store,
            feed=self._feed(),
            archive=self._archive(),
            retention=timedelta(days=settings.retention_days) if settings.retention_days else None,
        )
        ollama = OllamaClient(settings.ollama_url) if settings.ollama_url else None
        service = DashboardService(self._store, self._collector, ollama, display_timezone)
        aliases = ClientAliases(settings.client_aliases, self._aliases_file())
        self._server = DashboardServer((settings.host, settings.port), ApiRouter(service, aliases), self._auth())

    def run(self) -> None:
        self._collector.start()
        log.info("дашборд: http://%s:%d  (источник: %s)", self._settings.host, self._settings.port,
                 self._collector.status.source)
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._server.server_close()

    def _feed(self) -> LogFeed:
        if self._settings.log_files:
            return FileLogFeed(self._settings.log_files)
        return DockerLogFeed(DockerClient(self._settings.docker_host), self._settings.container)

    def _archive(self) -> LogArchive | None:
        if self._settings.data_dir is None or self._settings.log_files:
            return None
        return LogArchive(self._settings.data_dir / f"{self._settings.container}.log")

    def _aliases_file(self) -> Path | None:
        if self._settings.data_dir is None:
            return None
        self._settings.data_dir.mkdir(parents=True, exist_ok=True)
        return self._settings.data_dir / "client-aliases.json"

    def _auth(self) -> BasicAuth | None:
        if self._settings.username and self._settings.password:
            return BasicAuth(self._settings.username, self._settings.password)
        return None


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("WATCHDOG_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    WatchdogWebApp(Settings.from_environment(argv)).run()
