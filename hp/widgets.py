"""Состояние карточек: ping и виджеты (portainer, adguard, npm, …), с кэшем.

Страница по-прежнему рендерится сервером целиком. Клиент лишь раз в
``system.poll`` секунд спрашивает ``/api/state`` и обновляет уже нарисованные
карточки; кэш не даёт этому поллингу превратиться в поллинг чужих API.

Тип виджета — небольшой класс:

* ``FIELDS``   — пары (ключ, подпись), порядок по умолчанию;
* ``REQUIRED`` / ``OPTIONAL`` — ключи секции ``[widget:…]``;
* ``fetch(options)`` возвращает dict «ключ → значение»;
* ``tone(key, value)`` выбирает цвет значения: good / bad / warn / idle;
* ``show(key, value)`` превращает число в текст (проценты, часы, «1 / 8»).

Ключ ``fields = ["total", "enabled"]`` в секции виджета задаёт, какие поля
показывать и в каком порядке (не больше ``MAX_FIELDS``).
"""

from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import struct
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

PING_OK_MS = 350          # быстрее — зелёный, медленнее — жёлтый
PING_TTL = 5.0
WIDGET_TTL = 10.0
DEFAULT_TIMEOUT = 4.0
MAX_WORKERS = 8
MAX_FIELDS = 4            # сколько полей влезает в карточку
MC_DEFAULT_PORT = 25565


class WidgetFailure(Exception):
    """Причина, которую не стыдно показать прямо в карточке."""


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


# --------------------------------------------------------------------------- #
# общие походы в чужие API
# --------------------------------------------------------------------------- #

def _request(url: str, headers: dict | None = None, data: bytes | None = None,
             timeout: float = DEFAULT_TIMEOUT, insecure: bool = True, name: str = "Сервис") -> bytes:
    """Один GET/POST: любая беда — WidgetFailure с человеческой причиной."""
    request = Request(url, headers=headers or {}, data=data)
    context = ssl._create_unverified_context() if insecure else None
    host = urlsplit(url).netloc or url
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.read(4_000_000)
    except HTTPError as error:
        raise WidgetFailure(f"{name} ответил {error.code}") from error
    except URLError as error:
        raise WidgetFailure(f"нет связи с {host}: {error.reason}") from error
    except (socket.timeout, TimeoutError) as error:
        raise WidgetFailure(f"{name} не ответил вовремя") from error


def _json(raw: bytes, name: str = "Сервис"):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as error:
        raise WidgetFailure(f"{name}: ответ не JSON") from error


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return "Basic " + token


def _duration(ms) -> str:
    """597532149 мс → «166 ч»: в карточку лезет только короткая запись."""
    try:
        seconds = float(ms) / 1000
    except (TypeError, ValueError):
        return "—"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f} ч"
    if seconds >= 60:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds:.0f} с"


def _number(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class Spec:
    """Общее у всех типов: по умолчанию значение как есть и нейтральный цвет."""

    TITLE = ""
    FIELDS: tuple = ()
    REQUIRED: tuple = ()
    OPTIONAL: tuple = ()

    def fetch(self, options: dict) -> dict:
        raise NotImplementedError

    def tone(self, key: str, value) -> str:
        return "idle"

    def show(self, key: str, value):
        return value


# --------------------------------------------------------------------------- #
# Portainer
# --------------------------------------------------------------------------- #

class Portainer(Spec):
    """Счётчики контейнеров: запущено / остановлено / всего."""

    TITLE = "Portainer"
    FIELDS = (("running", "запущено"), ("stopped", "остановлено"), ("total", "всего"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("env", "timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        endpoint = str(options.get("env", 1)).strip() or "1"
        url = f"{base}/api/endpoints/{endpoint}/docker/containers/json?all=1"
        raw = _request(url, {
            "X-API-Key": str(options.get("key", "")),
            "Accept": "application/json",
            "User-Agent": "homepage-dashboard",
        }, timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
            insecure=not _truthy(options.get("verify_tls", False)), name="Portainer")
        payload = _json(raw, "Portainer — включён ли API?")
        containers = payload if isinstance(payload, list) else payload.get("containers") or []
        running = sum(1 for item in containers if str(item.get("State", "")).lower() == "running")
        total = len(containers)
        return {"running": running, "stopped": total - running, "total": total}

    def tone(self, key: str, value) -> str:
        if key == "running":
            return "good" if value else "idle"
        if key == "stopped":
            return "bad" if value else "idle"
        return "idle"


# --------------------------------------------------------------------------- #
# AdGuard Home
# --------------------------------------------------------------------------- #

ADGUARD_REPLACED = ("num_replaced_safebrowsing", "num_replaced_safesearch", "num_replaced_parental")


class AdGuard(Spec):
    """Запросы DNS: сколько всего, сколько заблокировано, сколько подменено."""

    TITLE = "AdGuard Home"
    FIELDS = (("queries", "запросы"), ("blocked", "блок"), ("filtered", "фильтр"), ("latency", "мс"))
    REQUIRED = ("url",)
    OPTIONAL = ("username", "password", "timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        headers = {"Accept": "application/json"}
        if options.get("username"):
            headers["Authorization"] = _basic(str(options.get("username")), str(options.get("password", "")))
        raw = _request(f"{base}/control/stats", headers,
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)), name="AdGuard")
        stats = _json(raw, "AdGuard")
        if not isinstance(stats, dict):
            raise WidgetFailure("AdGuard: неожиданный ответ /control/stats")
        return {
            "queries": _number(stats.get("num_dns_queries")),
            "blocked": _number(stats.get("num_blocked_filtering")),
            "filtered": sum(_number(stats.get(key)) for key in ADGUARD_REPLACED),
            "latency": int(round(float(stats.get("avg_processing_time") or 0) * 1000)),
        }

    def tone(self, key: str, value) -> str:
        if key == "blocked":
            return "warn" if value else "idle"
        if key == "latency":
            return "good" if value is not None and value <= 20 else "warn"
        return "idle"

    def show(self, key: str, value):
        return f"{value} мс" if key == "latency" and value is not None else value


# --------------------------------------------------------------------------- #
# Nginx Proxy Manager
# --------------------------------------------------------------------------- #

_NPM_TOKENS: dict[str, tuple[str, float]] = {}
NPM_TOKEN_TTL = 3600.0


class Npm(Spec):
    """Прокси-хосты: включено / выключено / всего."""

    TITLE = "Nginx Proxy Manager"
    FIELDS = (("enabled", "включено"), ("disabled", "выключено"), ("total", "всего"))
    REQUIRED = ("url", "username", "password")
    OPTIONAL = ("timeout", "verify_tls")

    def _token(self, base: str, user: str, password: str, timeout: float, insecure: bool) -> str:
        key = f"{base}|{user}"
        cached = _NPM_TOKENS.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        body = json.dumps({"identity": user, "secret": password}).encode("utf-8")
        raw = _request(f"{base}/api/tokens", {"Content-Type": "application/json",
                                              "Accept": "application/json"}, data=body,
                       timeout=timeout, insecure=insecure, name="NPM")
        token = (_json(raw, "NPM") or {}).get("token")
        if not token:
            raise WidgetFailure("NPM не выдал токен — проверь логин и пароль")
        _NPM_TOKENS[key] = (token, time.monotonic() + NPM_TOKEN_TTL)
        return token

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        insecure = not _truthy(options.get("verify_tls", False))
        token = self._token(base, str(options.get("username", "")),
                            str(options.get("password", "")), timeout, insecure)
        raw = _request(f"{base}/api/nginx/proxy-hosts",
                       {"Authorization": "Bearer " + token, "Accept": "application/json"},
                       timeout=timeout, insecure=insecure, name="NPM")
        hosts = _json(raw, "NPM")
        if not isinstance(hosts, list):
            raise WidgetFailure("NPM: неожиданный ответ /api/nginx/proxy-hosts")
        enabled = sum(1 for host in hosts if host.get("enabled"))
        return {"enabled": enabled, "disabled": len(hosts) - enabled, "total": len(hosts)}

    def tone(self, key: str, value) -> str:
        if key == "enabled":
            return "good" if value else "idle"
        if key == "disabled":
            return "warn" if value else "idle"
        return "idle"


# --------------------------------------------------------------------------- #
# Minecraft (UDP, современный протокол статуса)
# --------------------------------------------------------------------------- #

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _mc_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _varint(len(raw)) + raw


def _take_varint(buffer: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if pos >= len(buffer):
            break
        byte = buffer[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
    raise WidgetFailure("Minecraft: битый пакет")


def _read_exactly(conn, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise WidgetFailure("Minecraft закрыл соединение")
        data += chunk
    return data


def minecraft_status(target: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Спросить сервер по протоколу статуса: online, players, version."""
    host, port = split_target(target, fallback_port=MC_DEFAULT_PORT)
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            body = _varint(0) + _varint(765) + _mc_string(host) + struct.pack(">H", port) + _varint(1)
            conn.sendall(_varint(len(body)) + body + b"\x01\x00")
            raw = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if len(raw) >= 6 and _packet_complete(raw):
                    break
    except (OSError, ValueError) as error:
        raise WidgetFailure(f"Minecraft не ответил: {error}") from error
    # тело пакета: varint длина, varint id, varint длина JSON, JSON
    length, pos = _take_varint(raw, 0)
    packet = raw[pos:pos + length]
    _packet_id, pos = _take_varint(packet, 0)
    text_length, pos = _take_varint(packet, pos)
    text = packet[pos:pos + text_length].decode("utf-8", "replace")
    try:
        status = json.loads(text or "{}")
    except ValueError as error:
        raise WidgetFailure("Minecraft: не разобрал ответ") from error
    players = status.get("players") or {}
    version = (status.get("version") or {}).get("name")
    return {"status": "Up", "players": f"{players.get('online', '?')} / {players.get('max', '?')}",
            "version": version or "—"}


def _packet_complete(raw: bytes) -> bool:
    """Хватит ли уже пришедшего: длина пакета, потом сам пакет."""
    length, pos = _take_varint(raw, 0)
    return len(raw) - pos >= length


class Minecraft(Spec):
    """Жив ли сервер: статус, игроки, версия."""

    TITLE = "Minecraft"
    FIELDS = (("status", "статус"), ("players", "игроки"), ("version", "версия"))
    REQUIRED = ("url",)
    OPTIONAL = ("timeout",)

    def fetch(self, options: dict) -> dict:
        return minecraft_status(str(options.get("url", "")),
                                timeout=float(options.get("timeout", DEFAULT_TIMEOUT)))

    def tone(self, key: str, value) -> str:
        if key == "status":
            return "good" if value == "Up" else "bad"
        return "idle"


# --------------------------------------------------------------------------- #
# changedetection.io
# --------------------------------------------------------------------------- #

class ChangeDetectionIo(Spec):
    """Наблюдения за страницами: сколько изменилось и сколько всего."""

    TITLE = "changedetection.io"
    FIELDS = (("diffsDetected", "изменения"), ("totalObserved", "наблюдения"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        raw = _request(f"{base}/api/v1/watch", {"x-api-key": str(options.get("key", "")),
                                                 "Accept": "application/json"},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)), name="changedetection.io")
        watches = _json(raw, "changedetection.io")
        if not isinstance(watches, dict):
            raise WidgetFailure("changedetection.io: неожиданный ответ /api/v1/watch")
        diffs = sum(1 for watch in watches.values()
                    if _number(watch.get("last_changed")) > 0 and not watch.get("viewed"))
        return {"diffsDetected": diffs, "totalObserved": len(watches)}

    def tone(self, key: str, value) -> str:
        if key == "diffsDetected":
            return "warn" if value else "good"
        return "idle"


# --------------------------------------------------------------------------- #
# Uptime Kuma
# --------------------------------------------------------------------------- #

def _hours_since(stamp) -> int | None:
    """«2026-11-09T04:12:20.000Z» → сколько часов назад. Не разобрал — None."""
    if not stamp:
        return None
    text = str(stamp).replace("Z", "").split(".")[0]
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            moment = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return int(max(0.0, (datetime.now(timezone.utc) - moment).total_seconds()) // 3600)
    return None


class UptimeKuma(Spec):
    """Страница статуса: живые мониторы, лежащие, аптайм и текущая авария."""

    TITLE = "Uptime Kuma"
    FIELDS = (("up", "жив"), ("down", "лежит"), ("uptime", "аптайм"), ("incident", "авария"))
    REQUIRED = ("url", "slug")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        slug = quote(str(options.get("slug", "")), safe="")
        timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        insecure = not _truthy(options.get("verify_tls", False))
        page = _json(_request(f"{base}/api/status-page/{slug}", {"Accept": "application/json"},
                              timeout=timeout, insecure=insecure, name="Uptime Kuma"), "Uptime Kuma")
        beats = _json(_request(f"{base}/api/status-page/heartbeat/{slug}", {"Accept": "application/json"},
                               timeout=timeout, insecure=insecure, name="Uptime Kuma"), "Uptime Kuma")
        up = down = 0
        for history in (beats.get("heartbeatList") or {}).values():
            last = history[-1] if history else None
            if (last or {}).get("status") == 1:
                up += 1
            else:
                down += 1
        uptimes = [float(value) for value in (beats.get("uptimeList") or {}).values()
                   if isinstance(value, (int, float))]
        uptime = (sum(uptimes) / len(uptimes) * 100) if uptimes else 0.0
        incidents = page.get("incidents") or []
        started = incidents[0].get("createdDate") if incidents else (page.get("incident") or {}).get("createdDate")
        return {"up": up, "down": down, "uptime": round(uptime, 1), "incident": _hours_since(started)}

    def tone(self, key: str, value) -> str:
        if key == "up":
            return "good" if value else "idle"
        if key == "down":
            return "bad" if value else "good"
        if key == "uptime":
            return "good" if value >= 99 else ("warn" if value >= 90 else "bad")
        if key == "incident":
            return "bad" if value is not None else "idle"
        return "idle"

    def show(self, key: str, value):
        if key == "uptime" and value is not None:
            return f"{value:.1f}%"
        if key == "incident" and value is not None:
            return f"{value} ч"
        return value


# --------------------------------------------------------------------------- #
# Your Spotify
# --------------------------------------------------------------------------- #

SPOTIFY_INTERVALS = {"day": 1, "week": 7, "month": 30, "year": 365}


def spotify_start(interval: str) -> str:
    """С какого момента считаем: interval — day / week / month / year / all."""
    name = (interval or "week").strip().lower()
    if name == "all":
        return "2006-04-23T00:00:00.000Z"
    days = SPOTIFY_INTERVALS.get(name, 7)
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class YourSpotify(Spec):
    """Что слушал: треки, время и разные артисты за интервал."""

    TITLE = "Your Spotify"
    FIELDS = (("songs", "треки"), ("time", "время"), ("artists", "артисты"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("interval", "timeout", "verify_tls")

    def _page(self, base: str, endpoint: str, options: dict) -> list:
        target = (f"{base}/spotify/{endpoint}?token={quote(str(options.get('key', '')), safe='')}"
                  f"&timeSplit=all&start={quote(spotify_start(str(options.get('interval', 'week'))), safe='')}")
        raw = _request(target, {"Accept": "application/json"},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)), name="Your Spotify")
        payload = _json(raw, "Your Spotify")
        return payload if isinstance(payload, list) else []

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        songs = self._page(base, "songs_per", options)
        spent = self._page(base, "time_per", options)
        artists = self._page(base, "different_artists_per", options)
        return {
            "songs": _number((songs[0] if songs else {}).get("count")),
            "time": _number((spent[0] if spent else {}).get("count")),
            "artists": len(((artists[0] if artists else {}).get("artists") or [])),
        }

    def show(self, key: str, value):
        return _duration(value) if key == "time" else value


# --------------------------------------------------------------------------- #
# Watchtower
# --------------------------------------------------------------------------- #

WATCHTOWER_METRIC = re.compile(r"^watchtower_([a-z_]+)\s+([0-9.eE+-]+)$")


class Watchtower(Spec):
    """Последнее сканирование: сколько контейнеров проверено, обновлено, сломалось."""

    TITLE = "Watchtower"
    FIELDS = (("containers_scanned", "проверено"), ("containers_updated", "обновлено"),
              ("containers_failed", "ошибки"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        raw = _request(f"{base}/v1/metrics", {"Authorization": "Bearer " + str(options.get("key", ""))},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)), name="Watchtower")
        metrics: dict[str, float] = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            match = WATCHTOWER_METRIC.match(line.strip())
            if match:
                metrics[match.group(1)] = float(match.group(2))   # без префикса watchtower_
        if not metrics:
            raise WidgetFailure("Watchtower: в /v1/metrics нет watchtower_* — включён ли API?")
        return {key: int(metrics.get(key, 0)) for key, _label in self.FIELDS}

    def tone(self, key: str, value) -> str:
        if key == "containers_updated":
            return "good" if value else "idle"
        if key == "containers_failed":
            return "bad" if value else "idle"
        return "idle"


REGISTRY = {type(spec).__name__.lower(): spec for spec in (
    Portainer(), AdGuard(), Npm(), Minecraft(), ChangeDetectionIo(), UptimeKuma(), YourSpotify(), Watchtower(),
)}


def type_spec(kind: str | None):
    return REGISTRY.get((kind or "").strip().lower())


def labels_for(spec) -> dict:
    return {key: label for key, label in spec.FIELDS}


def field_keys(widget, spec=None) -> list[str]:
    """Порядок полей: из конфига (fields = […]) или как объявлено в типе."""
    spec = spec or type_spec(widget.type)
    if spec is None:
        return []
    known = [key for key, _label in spec.FIELDS]
    raw = widget.options.get("fields")
    if raw in (None, ""):
        return known[:MAX_FIELDS]
    items = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
    keys: list[str] = []
    for item in items:
        key = str(item).strip().strip("\"'")
        if key in known and key not in keys:
            keys.append(key)
    return (keys or known)[:MAX_FIELDS]


def fields_for(widget) -> list[dict]:
    """Поля для скелета карточки — их видно ещё до первого ответа."""
    spec = type_spec(widget.type)
    if spec is None:
        return []
    labels = labels_for(spec)
    return [{"key": key, "label": labels[key]} for key in field_keys(widget, spec)]


def title_for(widget) -> str:
    spec = type_spec(widget.type)
    return spec.TITLE if spec else (widget.type or "виджет")


def validate(widget) -> list[str]:
    """Проверка секции ``[widget:…]`` на этапе загрузки конфига."""
    problems: list[str] = []
    spec = type_spec(widget.type)
    if spec is None:
        hint = repr(widget.type) if widget.type else "не указан type"
        problems.append(f"виджет {widget.name!r}: {hint} — известны {', '.join(sorted(REGISTRY))}")
        return problems
    missing = [key for key in spec.REQUIRED if not widget.options.get(key)]
    if missing:
        problems.append(f"виджет {widget.name!r} ({widget.type}): не заданы {', '.join(missing)}")
    extra = [key for key in widget.options if key not in spec.REQUIRED + spec.OPTIONAL + ("type", "fields")]
    if extra:
        problems.append(f"виджет {widget.name!r} ({widget.type}): лишние ключи {', '.join(sorted(extra))}")
    problems += _field_problems(widget, spec)
    return problems


def _field_problems(widget, spec) -> list[str]:
    """Понятная ошибка, если в fields опечатка или полей больше, чем влезает."""
    raw = widget.options.get("fields")
    if raw in (None, ""):
        return []
    known = [key for key, _label in spec.FIELDS]
    items = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
    requested = [str(item).strip().strip("\"'") for item in items if str(item).strip()]
    problems: list[str] = []
    unknown = [key for key in requested if key not in known]
    if unknown:
        problems.append(f"виджет {widget.name!r} ({widget.type}): неизвестные поля "
                        f"{', '.join(unknown)} — известны {', '.join(known)}")
    valid = [key for key in dict.fromkeys(requested) if key in known]
    if len(valid) > MAX_FIELDS:
        problems.append(f"виджет {widget.name!r} ({widget.type}): полей больше {MAX_FIELDS}, "
                        f"будут видны {', '.join(valid[:MAX_FIELDS])}")
    return problems


def run_widget(widget) -> dict:
    """Один поход в чужой API. Никогда не бросает: ошибка — это тоже состояние."""
    spec = type_spec(widget.type)
    stamp = time.time()
    if spec is None:
        return {"ok": False, "type": widget.type, "error": f"неизвестный тип {widget.type!r}", "at": stamp}
    missing = [key for key in spec.REQUIRED if not widget.options.get(key)]
    if missing:
        return {"ok": False, "type": widget.type, "fields": [], "at": stamp,
                "error": "не хватает " + ", ".join(missing)}
    try:
        values = spec.fetch(widget.options)
    except WidgetFailure as error:
        return {"ok": False, "type": widget.type, "at": stamp, "error": str(error)}
    except Exception as error:  # noqa: BLE001 — виджет не имеет права ронять страницу
        return {"ok": False, "type": widget.type, "at": stamp, "error": f"{type(error).__name__}: {error}"}
    if not isinstance(values, dict):
        return {"ok": False, "type": widget.type, "at": stamp, "error": "виджет вернул не словарь"}
    labels = labels_for(spec)
    fields = []
    for key in field_keys(widget, spec):
        value = values.get(key)
        fields.append({"key": key, "label": labels[key], "value": spec.show(key, value),
                       "tone": spec.tone(key, value)})
    return {"ok": True, "type": widget.type, "title": spec.TITLE, "fields": fields, "at": stamp}


# --------------------------------------------------------------------------- #
# ping
# --------------------------------------------------------------------------- #

def split_target(target: str, fallback_port: int | None = None) -> tuple[str, int]:
    """``host``, ``host:port`` и ``http(s)://host:port/путь`` → (host, port)."""
    text = (target or "").strip()
    if not text:
        raise ValueError("пустой адрес")
    if "://" not in text:
        text = "//" + text
    parts = urlsplit(text)
    host = parts.hostname
    if not host:
        raise ValueError(f"не разобрал адрес {target!r}")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"не разобрал порт в {target!r}") from error
    if port is None:
        if parts.scheme == "https":
            port = 443
        elif parts.scheme == "http":
            port = 80
        else:
            port = fallback_port or 80
    return host, port


def ping(target: str, timeout: float = 2.0) -> dict:
    """TCP-connect вместо ICMP: работает без root и внутри контейнера."""
    try:
        host, port = split_target(target)
    except ValueError as error:
        return {"state": "down", "ms": None, "error": str(error)}
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = int((time.perf_counter() - started) * 1000)
    except (OSError, ValueError) as error:
        return {"state": "down", "ms": None, "target": f"{host}:{port}", "error": str(error) or "не отвечает"}
    return {"state": "ok" if ms <= PING_OK_MS else "slow", "ms": ms, "target": f"{host}:{port}"}


PING_WORDS = {"ok": "Up", "slow": "Slow", "down": "Down"}


def ping_title(state) -> str:
    """Подсказка к точке пинга без адреса: клиенту незачем знать, куда мы стучимся.

    По-английски и без хоста: «Ping: Up · 1 ms». Те же слова в static/js/app.js."""
    word = PING_WORDS.get((state or {}).get("state") or "unknown", "Unknown")
    ms = (state or {}).get("ms")
    return "Ping: " + word + (f" · {ms} ms" if ms is not None else "")


def wire_ping(state: dict) -> dict:
    """Только состояние и задержка — ни адреса, ни текста ошибки с хостом."""
    return {"state": (state or {}).get("state") or "unknown", "ms": (state or {}).get("ms")}


# --------------------------------------------------------------------------- #
# кэш и снимок
# --------------------------------------------------------------------------- #

Entry = namedtuple("Entry", "value expires at")
_CACHE: dict[str, Entry] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _NPM_TOKENS.clear()


def _peek(key: str) -> Entry | None:
    with _LOCK:
        return _CACHE.get(key)


def _cached(key: str, producer, ttl: float) -> Entry:
    now = time.monotonic()
    entry = _peek(key)
    if entry is not None and entry.expires > now:
        return entry
    entry = Entry(value=producer(), expires=time.monotonic() + ttl, at=time.time())
    with _LOCK:
        _CACHE[key] = entry
    return entry


def peek_widget(widget) -> dict | None:
    entry = _peek(f"widget|{widget.name.casefold()}")
    return entry.value if entry else None


def peek_ping(target: str, timeout: float = 2.0) -> dict | None:
    entry = _peek(f"ping|{target}|{timeout}")
    return dict(entry.value, at=round(entry.at, 3)) if entry else None


def card_identity(service, group_name: str | None = None) -> str:
    """Ключ карточки: по нему клиент находит своё состояние."""
    return f"{group_name or service.group}::{service.name}"


def _plan(site) -> dict[str, dict[str, tuple]]:
    """Что и по каким ключам кэша нужно для каждой карточки."""
    plan: dict[str, dict[str, tuple]] = {}
    for service in site.services:
        item: dict[str, tuple] = {}
        if service.ping:
            timeout = float(service.ping_timeout or 2.0)
            key = f"ping|{service.ping}|{timeout}"
            item["ping"] = (key, lambda s=service: ping(s.ping, timeout), PING_TTL)
        if service.widget:
            widget = site.widgets.get(service.widget.casefold())
            if widget is not None:
                key = f"widget|{widget.name.casefold()}"
                item["widget"] = (key, lambda w=widget: run_widget(w), WIDGET_TTL)
        if item:
            group = site.groups.get(service.group_key)
            plan[card_identity(service, group.name if group else None)] = item
    return plan


def snapshot(site, online: bool = True) -> dict:
    """Состояние всех карточек. ``online=False`` — только из кэша, без сети."""
    plan = _plan(site)
    if online:
        wanted: dict[str, tuple] = {}
        for item in plan.values():
            for key, producer, ttl in item.values():
                wanted.setdefault(key, (producer, ttl))
        stale = [key for key in wanted if (_peek(key) is None or _peek(key).expires <= time.monotonic())]
        if stale:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(stale))) as pool:
                list(pool.map(lambda key: _cached(key, wanted[key][0], wanted[key][1]), stale))
    cards: dict[str, dict] = {}
    for identity, item in plan.items():
        card: dict = {}
        for kind, (key, _producer, _ttl) in item.items():
            entry = _peek(key)
            if entry is not None:
                # пинг отдаём без адреса: клиент не должен знать, куда мы стучимся
                card[kind] = wire_ping(entry.value) if kind == "ping" else entry.value
                card[f"{kind}_at"] = round(entry.at, 3)
        if card:                      # карточка без данных не занимает места
            cards[identity] = card
    return {"cards": cards, "poll": site.system.poll, "ts": round(time.time(), 3)}
