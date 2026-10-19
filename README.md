# ollama-watchdog

Дашборд со статистикой запросов к моделям ollama, построенный по логам её контейнера:
сколько запросов пришло к каждой модели, ошибки, время ответа, клиенты, активность по часам,
загрузки моделей в память, токены и что сейчас загружено в GPU.

## Развёртывание на машине с ollama

```bash
git clone … ollama-watchdog && cd ollama-watchdog   # или скопируйте папку
cp .env.example .env                                # поправьте OLLAMA_CONTAINER и при желании пароль
docker compose up -d --build
```

Дашборд: `http://<сервер>:8080`.

Что поднимается:

* `watchdog` — сам дашборд. Читает `docker logs -f` контейнера ollama в реальном времени и
  складывает полезные строки в volume `watchdog-data`, поэтому история переживает
  пересоздание контейнера ollama и перезапуск watchdog. Хранится `WATCHDOG_RETENTION_DAYS` дней.
* `docker-proxy` — [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy):
  watchdog получает доступ к Docker API только на чтение контейнеров и логов, а не весь `docker.sock`.

### Настройки (`.env`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `OLLAMA_CONTAINER` | `ollama` | имя контейнера ollama из `docker ps` |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | API ollama для блока «Сейчас в памяти»; пусто — выключить |
| `TZ` | `Europe/Moscow` | часовой пояс дашборда |
| `OLLAMA_SERVER_TZ` | `UTC` | часовой пояс внутри контейнера ollama |
| `WATCHDOG_PUBLISH` | `8080` | порт на хосте |
| `WATCHDOG_USERNAME` / `WATCHDOG_PASSWORD` | пусто | Basic Auth; задайте, если порт виден не только вам |

Если порт ollama не опубликован на хосте, а она живёт в своей compose-сети, подключите
watchdog к этой сети и укажите `OLLAMA_URL=http://ollama:11434`:

```yaml
# docker-compose.override.yml
services:
  watchdog:
    networks: [ollama_default]
networks:
  ollama_default:
    external: true
```

### Точная статистика по моделям

В обычном логе ollama у HTTP-запроса нет имени модели, поэтому по умолчанию модель
определяется по косвенным признакам (помечено «≈»). Чтобы привязка была точной, добавьте
контейнеру ollama переменную `OLLAMA_DEBUG=1` — watchdog подхватит это автоматически.

## Без Docker

```bash
python3 -m ollama_watchdog.web ../ollama-logs/*.txt --tz Europe/Moscow   # дашборд по файлам
python3 -m ollama_watchdog ../ollama-logs/*.txt --date 2026-10-15        # отчёт в терминал
python3 -m unittest discover -s tests -t .                                # тесты
```
