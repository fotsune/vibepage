"""Состояние карточек: ping и виджеты (portainer, adguard, npm, …), с кэшем.

Страница по-прежнему рендерится сервером целиком. Клиент лишь раз в
``system.poll`` секунд спрашивает ``/api/state`` и обновляет уже нарисованные
карточки; кэш не даёт этому поллингу превратиться в поллинг чужих API.

Тип виджета — небольшой класс:

* ``FIELDS``   — пары (ключ, подпись), порядок по умолчанию;
* ``REQUIRED`` / ``OPTIONAL`` — ключи секции ``[widget:…]``;
* ``fetch(options)`` возвращает dict «ключ → значение»;
* ``tone(key, value, options)`` выбирает цвет значения: good / bad / warn / idle;
* ``show(key, value, options)`` превращает число в текст (проценты, байты, часы);
* ``bar(values, options)`` — доля заполнения для полосочки, если она нужна.

Ключ ``fields = "total, enabled"`` в секции виджета задаёт, какие поля
показывать и в каком порядке (не больше ``MAX_FIELDS``). Старая запись
``fields = ["total", "enabled"]`` понимается точно так же.

Системные виджеты (``cpu``, ``ram``, ``disk``) несут ещё и полосочку статуса:
``Spec.bar()`` возвращает долю заполнения 0…1, её рисует страница.

Любой тип можно показать с чужого хоста: ``host = "box:8080"`` и ``external = "disk"``
означают «возьми у соседа виджет disk и нарисуй его здесь своими ``fields`` и единицами»
— сосед отдаёт ``/api/widget?id=…`` (см. :func:`remote_payload`).
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

from . import thermal

try:
    import psutil           # нативный модуль: нужен cpu/ram/disk, без него дашборд живёт
except ImportError:        # pragma: no cover — зависит от окружения, не от кода
    psutil = None

PING_OK_MS = 350          # быстрее — зелёный, медленнее — жёлтый
PING_TTL = 5.0
WIDGET_TTL = 10.0
DEFAULT_TIMEOUT = 4.0
MAX_WORKERS = 8
MAX_FIELDS = 4            # сколько полей влезает в карточку
MC_DEFAULT_PORT = 25565


CROSS = "✕"                # значение поля, когда данных нет
NO_TEMPERATURE = "No CPU temperature sensor"   # подсказка на прочерке вместо градусов


class WidgetFailure(Exception):
    """Причина, которую не стыдно показать в подсказке.

    ``str(error)`` — одна короткая фраза по-английски, без адреса и кодов ОС:
    клиенту незачем знать, куда именно мы стучимся (так же, как у ping).
    """

    def __init__(self, detail: str):
        super().__init__(detail)


_ADDRESS = re.compile(r"(?:https?://)?(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\])(?::\d+)?")
_WINERR = re.compile(r"\[?WinError\s*\d+\]?|\[?Errno\s*-?\d+\]?")

# коды отказов: WinError и errno в одном месте
_REFUSED = (10061, 111)          # порт закрыт
_TIMEOUT = (10060, 110, 10035)   # никто не ответил
_UNREACH = (10065, 113, 101)     # узел недоступен
_DNS = (11001, 11004, -2, -3, 8)


def _clean(text: str) -> str:
    """Причина без адреса и кодов ОС."""
    text = _WINERR.sub("", str(text or ""))
    text = _ADDRESS.sub("address", text)
    return " ".join(text.split()).strip(" :.,")


def _reason_text(reason) -> str:
    """Отказ соединения одной фразой по-английски: без адреса и кода ОС."""
    code = getattr(reason, "errno", None)
    if code is None and isinstance(reason, int):
        code = reason
    if code in _REFUSED:
        return "Port closed"
    if code in _TIMEOUT:
        return "No response"
    if code in _UNREACH:
        return "Host unreachable"
    if code in _DNS:
        return "Name not resolved"
    text = _clean(reason)
    if not text or len(text) > 40 or not re.search(r"[A-Za-z]", text):
        return "Connection failed"   # хвост ОС без латиницы ни о чём не говорит
    return text


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


# --------------------------------------------------------------------------- #
# общие походы в чужие API
# --------------------------------------------------------------------------- #

def _request(url: str, headers: dict | None = None, data: bytes | None = None,
             timeout: float = DEFAULT_TIMEOUT, insecure: bool = True) -> bytes:
    """Один GET/POST: любая беда — WidgetFailure с человеческой причиной."""
    request = Request(url, headers=headers or {}, data=data)
    context = ssl._create_unverified_context() if insecure else None
    host = urlsplit(url).netloc or url   # только для логов, в карточку не идёт
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.read(4_000_000)
    except HTTPError as error:
        kind = "Access denied" if error.code in (401, 403) else "API error"
        raise WidgetFailure(f"{kind}: HTTP {error.code}") from error
    except URLError as error:
        raise WidgetFailure(_reason_text(error.reason)) from error
    except (socket.timeout, TimeoutError) as error:
        raise WidgetFailure("No response") from error


def _json(raw: bytes, hint: str = ""):
    """JSON из ответа; hint — догадка, что проверить, если пришло не то."""
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as error:
        raise WidgetFailure("Response is not JSON" + (f" — {hint}" if hint else "")) from error


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
        return f"{seconds / 3600:.0f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


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

    def tone(self, key: str, value, options: dict | None = None) -> str:
        return "idle"

    def show(self, key: str, value, options: dict | None = None):
        return value

    def bar(self, values: dict, options: dict | None = None) -> dict | None:
        """Полосочка статуса: ``{"value": 0…1, "tone": …}`` или None."""
        return None


def _level(fraction: float) -> str:
    """0.7 — уже жёлто, 0.9 — красный: одна шкала на все полосочки."""
    if fraction >= 0.9:
        return "bad"
    if fraction >= 0.7:
        return "warn"
    return "good"


def canonical_type(kind: str | None) -> str:
    """Имя типа без синонимов: sysinfo/system → cpu, memory → ram."""
    key = (kind or "").strip().lower()
    return ALIASES.get(key, key)


def _bar(fraction, tone: str | None = None) -> dict | None:
    if fraction is None:
        return None
    try:
        value = max(0.0, min(1.0, float(fraction)))
    except (TypeError, ValueError):
        return None
    return {"value": round(value, 4), "tone": tone or _level(value)}


# --------------------------------------------------------------------------- #
# Portainer
# --------------------------------------------------------------------------- #

class Portainer(Spec):
    """Счётчики контейнеров: запущено / остановлено / всего."""

    TITLE = "Portainer"
    FIELDS = (("running", "running"), ("stopped", "stopped"), ("total", "total"))
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
            insecure=not _truthy(options.get("verify_tls", False)))
        payload = _json(raw)
        containers = payload if isinstance(payload, list) else payload.get("containers") or []
        running = sum(1 for item in containers if str(item.get("State", "")).lower() == "running")
        total = len(containers)
        return {"running": running, "stopped": total - running, "total": total}

    def tone(self, key: str, value, options: dict | None = None) -> str:
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
    FIELDS = (("queries", "queries"), ("blocked", "blocked"), ("filtered", "filtered"), ("latency", "ms"))
    REQUIRED = ("url",)
    OPTIONAL = ("username", "password", "timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        headers = {"Accept": "application/json"}
        if options.get("username"):
            headers["Authorization"] = _basic(str(options.get("username")), str(options.get("password", "")))
        raw = _request(f"{base}/control/stats", headers,
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)))
        stats = _json(raw)
        if not isinstance(stats, dict):
            raise WidgetFailure("Unexpected answer from /control/stats")
        return {
            "queries": _number(stats.get("num_dns_queries")),
            "blocked": _number(stats.get("num_blocked_filtering")),
            "filtered": sum(_number(stats.get(key)) for key in ADGUARD_REPLACED),
            "latency": int(round(float(stats.get("avg_processing_time") or 0) * 1000)),
        }

    def tone(self, key: str, value, options: dict | None = None) -> str:
        if key == "blocked":
            return "warn" if value else "idle"
        if key == "latency":
            return "good" if value is not None and value <= 20 else "warn"
        return "idle"

    def show(self, key: str, value, options: dict | None = None):
        return f"{value} ms" if key == "latency" and value is not None else value


# --------------------------------------------------------------------------- #
# Nginx Proxy Manager
# --------------------------------------------------------------------------- #

_NPM_TOKENS: dict[str, tuple[str, float]] = {}
NPM_TOKEN_TTL = 3600.0


class Npm(Spec):
    """Прокси-хосты: включено / выключено / всего."""

    TITLE = "Nginx Proxy Manager"
    FIELDS = (("enabled", "enabled"), ("disabled", "disabled"), ("total", "total"))
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
                       timeout=timeout, insecure=insecure)
        token = (_json(raw) or {}).get("token")
        if not token:
            raise WidgetFailure("NPM returned no token — check login and password")
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
                       timeout=timeout, insecure=insecure)
        hosts = _json(raw)
        if not isinstance(hosts, list):
            raise WidgetFailure("Unexpected answer from /api/nginx/proxy-hosts")
        enabled = sum(1 for host in hosts if host.get("enabled"))
        return {"enabled": enabled, "disabled": len(hosts) - enabled, "total": len(hosts)}

    def tone(self, key: str, value, options: dict | None = None) -> str:
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
    raise WidgetFailure("Minecraft: broken packet")


def _read_exactly(conn, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise WidgetFailure("Minecraft: closed the connection")
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
        raise WidgetFailure(_reason_text(error)) from error
    # тело пакета: varint длина, varint id, varint длина JSON, JSON
    length, pos = _take_varint(raw, 0)
    packet = raw[pos:pos + length]
    _packet_id, pos = _take_varint(packet, 0)
    text_length, pos = _take_varint(packet, pos)
    text = packet[pos:pos + text_length].decode("utf-8", "replace")
    try:
        status = json.loads(text or "{}")
    except ValueError as error:
        raise WidgetFailure("Minecraft: cannot parse the answer") from error
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
    FIELDS = (("status", "status"), ("players", "players"), ("version", "version"))
    REQUIRED = ("url",)
    OPTIONAL = ("timeout",)

    def fetch(self, options: dict) -> dict:
        return minecraft_status(str(options.get("url", "")),
                                timeout=float(options.get("timeout", DEFAULT_TIMEOUT)))

    def tone(self, key: str, value, options: dict | None = None) -> str:
        if key == "status":
            return "good" if value == "Up" else "bad"
        return "idle"


# --------------------------------------------------------------------------- #
# changedetection.io
# --------------------------------------------------------------------------- #

class ChangeDetectionIo(Spec):
    """Наблюдения за страницами: сколько изменилось и сколько всего."""

    TITLE = "changedetection.io"
    FIELDS = (("diffsDetected", "changes"), ("totalObserved", "watches"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        raw = _request(f"{base}/api/v1/watch", {"x-api-key": str(options.get("key", "")),
                                                 "Accept": "application/json"},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)))
        watches = _json(raw)
        if not isinstance(watches, dict):
            raise WidgetFailure("Unexpected answer from /api/v1/watch")
        diffs = sum(1 for watch in watches.values()
                    if _number(watch.get("last_changed")) > 0 and not watch.get("viewed"))
        return {"diffsDetected": diffs, "totalObserved": len(watches)}

    def tone(self, key: str, value, options: dict | None = None) -> str:
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
    FIELDS = (("up", "up"), ("down", "down"), ("uptime", "uptime"), ("incident", "since"))
    REQUIRED = ("url", "slug")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        slug = quote(str(options.get("slug", "")), safe="")
        timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        insecure = not _truthy(options.get("verify_tls", False))
        page = _json(_request(f"{base}/api/status-page/{slug}", {"Accept": "application/json"},
                              timeout=timeout, insecure=insecure))
        beats = _json(_request(f"{base}/api/status-page/heartbeat/{slug}", {"Accept": "application/json"},
                               timeout=timeout, insecure=insecure))
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

    def tone(self, key: str, value, options: dict | None = None) -> str:
        if key == "up":
            return "good" if value else "idle"
        if key == "down":
            return "bad" if value else "good"
        if key == "uptime":
            return "good" if value >= 99 else ("warn" if value >= 90 else "bad")
        if key == "incident":
            return "bad" if value is not None else "idle"
        return "idle"

    def show(self, key: str, value, options: dict | None = None):
        if key == "uptime" and value is not None:
            return f"{value:.1f}%"
        if key == "incident" and value is not None:
            return f"{value} h"
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
    FIELDS = (("songs", "tracks"), ("time", "time"), ("artists", "artists"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("interval", "timeout", "verify_tls")

    def _page(self, base: str, endpoint: str, options: dict) -> list:
        target = (f"{base}/spotify/{endpoint}?token={quote(str(options.get('key', '')), safe='')}"
                  f"&timeSplit=all&start={quote(spotify_start(str(options.get('interval', 'week'))), safe='')}")
        raw = _request(target, {"Accept": "application/json"},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)))
        payload = _json(raw)
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

    def show(self, key: str, value, options: dict | None = None):
        return _duration(value) if key == "time" else value


# --------------------------------------------------------------------------- #
# Watchtower
# --------------------------------------------------------------------------- #

WATCHTOWER_METRIC = re.compile(r"^watchtower_([a-z_]+)\s+([0-9.eE+-]+)$")


class Watchtower(Spec):
    """Последнее сканирование: сколько контейнеров проверено, обновлено, сломалось."""

    TITLE = "Watchtower"
    FIELDS = (("containers_scanned", "scanned"), ("containers_updated", "updated"),
              ("containers_failed", "failed"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        raw = _request(f"{base}/v1/metrics", {"Authorization": "Bearer " + str(options.get("key", ""))},
                       timeout=float(options.get("timeout", DEFAULT_TIMEOUT)),
                       insecure=not _truthy(options.get("verify_tls", False)))
        metrics: dict[str, float] = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            match = WATCHTOWER_METRIC.match(line.strip())
            if match:
                metrics[match.group(1)] = float(match.group(2))   # без префикса watchtower_
        if not metrics:
            raise WidgetFailure("No watchtower_* in /v1/metrics — is the API enabled?")
        return {key: int(metrics.get(key, 0)) for key, _label in self.FIELDS}

    def tone(self, key: str, value, options: dict | None = None) -> str:
        if key == "containers_updated":
            return "good" if value else "idle"
        if key == "containers_failed":
            return "bad" if value else "idle"
        return "idle"


# --------------------------------------------------------------------------- #
# сам хост: cpu (загрузка, температура, аптайм) и ram
# --------------------------------------------------------------------------- #

def _psutil():
    """Модуль psutil или внятная причина, почему cpu/ram/disk не у дел."""
    if psutil is None:
        raise WidgetFailure("Not installed: psutil")
    return psutil


# Загрузка CPU считается по своим замерам, а не через psutil.cpu_percent(interval=None):
# тот помнит предыдущий замер в словаре по номеру потока, а snapshot опрашивает
# карточки из свежего потока на каждый запрос. Первый же вызов в новом потоке
# сравнивает время с самим собой — дельта в микросекунды, отсюда прыжки 0 % / 100 %.

CPU_WINDOW = 3.0          # окно усреднения, секунды: короче — полоска дёргается
CPU_KEEP = 30.0           # сколько секунд помним замеры
CPU_MIN_GAP = 0.5         # два замера ближе этого — второй не нужен
CPU_PRIME = 0.2           # первый замер за жизнь процесса ждёт столько

_CpuSample = namedtuple("CpuSample", "at busy total")
_CPU_SAMPLES: list = []
_CPU_LOAD = 0.0
_CPU_LOCK = threading.Lock()


def forget_cpu_load() -> None:
    """Забыть накопленные замеры: тестам и смене железа."""
    global _CPU_LOAD
    with _CPU_LOCK:
        _CPU_SAMPLES.clear()
        _CPU_LOAD = 0.0


def _cpu_sample(module, at: float) -> _CpuSample:
    """Счётчики процессора в секундах: занято и всего с момента загрузки."""
    times = module.cpu_times()
    fields = dict(times._asdict()) if hasattr(times, "_asdict") else dict(vars(times))
    if not fields:      # не namedtuple и без __dict__: берём по именам счётчиков
        fields = {name: getattr(times, name, 0) for name in (
            "user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice")}
    total = sum(float(value) for value in fields.values() if isinstance(value, (int, float)))
    # guest уже входит в user — второй раз не считаем; iowait — это не работа CPU
    total -= float(fields.get("guest", 0) or 0) + float(fields.get("guest_nice", 0) or 0)
    idle = float(fields.get("idle", 0) or 0) + float(fields.get("iowait", 0) or 0)
    return _CpuSample(at=at, busy=max(0.0, total - idle), total=max(0.0, total))


def _cpu_share(start, end) -> float | None:
    """Доля занятого времени между двумя замерами: 0.42 — это 42 %."""
    spent = end.total - start.total
    if spent <= 0:
        return None
    return max(0.0, min(1.0, (end.busy - start.busy) / spent))


def _cpu_load(module=None, window: float = CPU_WINDOW, prime: float = CPU_PRIME) -> float:
    """Загрузка CPU в процентах — среднее за окно не короче ``window`` секунд.

    Замеры копятся в одном месте на весь процесс, поэтому частота опроса и
    поток, из которого спросили, на результат не влияют: чем реже спрашивают,
    тем длиннее окно и спокойнее цифра.
    """
    global _CPU_LOAD
    module = module or _psutil()
    with _CPU_LOCK:
        now = time.monotonic()
        if _CPU_SAMPLES and now - _CPU_SAMPLES[-1].at < CPU_MIN_GAP:
            return _CPU_LOAD                     # спросили слишком часто — прежний ответ
        _CPU_SAMPLES.append(_cpu_sample(module, now))
        if len(_CPU_SAMPLES) == 1 and prime > 0:
            time.sleep(prime)                 # самый первый замер за жизнь процесса
            _CPU_SAMPLES.append(_cpu_sample(module, time.monotonic()))
        start = next((sample for sample in reversed(_CPU_SAMPLES[:-1])
                      if time.monotonic() - sample.at >= window), None)
        if start is None and len(_CPU_SAMPLES) > 1:
            start = _CPU_SAMPLES[0]          # окно ещё не набралось — берём что есть
        share = _cpu_share(start, _CPU_SAMPLES[-1]) if start is not None else None
        if share is not None:
            _CPU_LOAD = round(share * 100, 1)
        keep_from = time.monotonic() - CPU_KEEP
        _CPU_SAMPLES[:] = [sample for sample in _CPU_SAMPLES if sample.at >= keep_from] \
            or _CPU_SAMPLES[-1:]
        return _CPU_LOAD


def _cpu_percent() -> int:
    """Загрузка CPU для карточки: целые проценты от устойчивой оценки."""
    return int(round(_cpu_load()))


def _uptime_text(seconds) -> str:
    """138600 → «1d 14h»: в карточку лезет только короткая запись."""
    minutes = int(max(0.0, float(seconds)) // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _temp_fraction(temperature, options: dict | None) -> float | None:
    """Где температура между tempmin и tempmax: 0 — холодно, 1 — на пределе."""
    if temperature is None:
        return None
    low = _num((options or {}).get("tempmin"), 0.0)
    high = _num((options or {}).get("tempmax"), 100.0)
    if high <= low:
        return None
    return (float(temperature) - low) / (high - low)


IMPERIAL = ("imperial", "f", "fahrenheit")


SEVERITY = {"good": 0, "idle": 0, "warn": 1, "bad": 2}


def _worst(*tones: str) -> str:
    """Самое тревожное из состояний: одна полоска, одно мнение."""
    known = [tone for tone in tones if tone in SEVERITY]
    return max(known, key=SEVERITY.get, default="good")


def _temp_text(celsius, options: dict | None) -> str:
    """65.0 → «65°C» или «149°F» — смотря что просят в units."""
    if str((options or {}).get("units", "metric")).strip().lower() in IMPERIAL:
        return f"{float(celsius) * 9 / 5 + 32:.0f}°F"
    return f"{float(celsius):.0f}°C"


def _binary(options: dict | None, key: str) -> bool:
    """``diskUnits``/``ramUnits = bbytes`` — двоичные единицы (GiB), иначе GB."""
    return str((options or {}).get(key, "")).strip().lower() == "bbytes"


class Cpu(Spec):
    """Процессор того хоста, где живёт dashboard: загрузка, температура, аптайм."""

    TITLE = "CPU"
    FIELDS = (("cpu", "cpu"), ("cputemp", "temp"), ("uptime", "uptime"))
    REQUIRED = ()
    OPTIONAL = ("tempmin", "tempmax", "units")

    def fetch(self, options: dict) -> dict:
        return {
            "cpu": _cpu_percent(),
            "cputemp": thermal.cpu_temperature(),
            "uptime": int(time.time() - _psutil().boot_time()),
        }

    def show(self, key, value, options=None):
        if value is None:
            return None
        if key == "cputemp":
            return _temp_text(value, options)
        if key == "cpu":
            return f"{value}%"
        if key == "uptime":
            return _uptime_text(value)
        return value

    def tone(self, key, value, options=None) -> str:
        if key == "cputemp":
            fraction = _temp_fraction(value, options)
            return _level(fraction) if fraction is not None else "idle"
        if key == "cpu":
            return _level(value / 100) if value is not None else "idle"
        return "idle"

    def bar(self, values, options=None):
        # длина полоски — загрузка CPU, а цвет — худшее из загрузки и температуры:
        # горячий процессор видно и без ста процентов, а tempmin/tempmax задают шкалу
        load = values.get("cpu")
        fraction = load / 100 if load is not None else None
        temperature = _temp_fraction(values.get("cputemp"), options)
        levels = [_level(item) for item in (fraction, temperature) if item is not None]
        return _bar(fraction, _worst(*levels) if levels else None)


class Ram(Spec):
    """Оперативная память хоста: свободно, занято, всего."""

    TITLE = "RAM"
    FIELDS = (("free", "free"), ("consumed", "used"), ("total", "total"))
    REQUIRED = ()
    OPTIONAL = ("ramunits",)

    def fetch(self, options: dict) -> dict:
        memory = _psutil().virtual_memory()
        total = int(memory.total)
        free = int(getattr(memory, "available", 0) or 0)
        return {"free": free, "consumed": max(0, total - free), "total": total}

    def show(self, key, value, options=None):
        if value is None:
            return None
        return _bytes(value, _binary(options, "ramunits"))

    def bar(self, values, options=None):
        total = values.get("total") or 0
        return _bar(values.get("consumed") / total) if total else None


# --------------------------------------------------------------------------- #
# диски: сколько занято на указанных путях
# --------------------------------------------------------------------------- #

BINARY_UNITS = ("bbytes", "binary", "ibytes")
DISK_CAPTION_JOIN = " · "       # несколько путей в подписи: «/ ·/data»


def _bytes(value, binary: bool = False) -> str:
    """208291381248 → «208 GB» (bytes, 1000) или «194 GiB» (bbytes, 1024)."""
    step = 1024.0 if binary else 1000.0
    units = (("B", "KiB", "MiB", "GiB", "TiB", "PiB") if binary
             else ("B", "KB", "MB", "GB", "TB", "PB"))
    amount = float(value)
    index = 0
    while amount >= step and index < len(units) - 1:
        amount /= step
        index += 1
    if index == 0:
        return f"{amount:.0f} B"
    if amount >= 100:
        return f"{amount:.0f} {units[index]}"
    return f"{amount:.1f} {units[index]}"


def disk_paths(raw) -> list[str]:
    """``disk = "C:\\, D:\\"`` → ``["C:\\", "D:\\"]``; список тоже понимается."""
    items = raw if isinstance(raw, list) else re.split(r"[,;]", str(raw or ""))
    paths: list[str] = []
    for item in items:
        path = str(item).strip().strip("'\"")
        if path and path not in paths:
            paths.append(path)
    return paths


class Disk(Spec):
    """Занятое место по указанным дискам; несколько путей складываются в одно."""

    TITLE = "Disk"
    FIELDS = (("free", "free"), ("consumed", "used"), ("total", "total"))
    REQUIRED = ("disk",)
    OPTIONAL = ("diskunits", "name")        # name — подпись виджета, видна только на карточке

    def fetch(self, options: dict) -> dict:
        paths = disk_paths(options.get("disk"))
        if not paths:
            raise WidgetFailure("Not configured: missing disk")
        module = _psutil()
        free = consumed = total = 0
        for path in paths:
            try:
                usage = module.disk_usage(path)
            except (OSError, ValueError) as error:
                raise WidgetFailure(f"Cannot read {path}") from error
            free += usage.free
            consumed += usage.used
            total += usage.total
        return {"free": free, "consumed": consumed, "total": total}

    def show(self, key, value, options=None):
        if value is None:
            return None
        return _bytes(value, _binary(options, "diskunits"))

    def bar(self, values, options=None):
        total = values.get("total") or 0
        return _bar(values.get("consumed") / total) if total else None


REGISTRY = {type(spec).__name__.lower(): spec for spec in (
    Portainer(), AdGuard(), Npm(), Minecraft(), ChangeDetectionIo(), UptimeKuma(), YourSpotify(), Watchtower(),
    Cpu(), Ram(), Disk(),
)}

# sysinfo разделился на cpu и ram; старые имена в конфиге продолжают работать
ALIASES = {"sysinfo": "cpu", "system": "cpu", "memory": "ram"}


def type_spec(kind: str | None):
    name = (kind or "").strip().lower()
    return REGISTRY.get(ALIASES.get(name, name))


def labels_for(spec) -> dict:
    return {key: label for key, label in spec.FIELDS}


FIELD_SPLIT = re.compile(r"[,;\s]+")


def raw_fields(widget) -> list[str]:
    """``fields = "cpu, memory"`` и старое ``fields = ["cpu", "memory"]`` — одно и то же."""
    raw = widget.options.get("fields")
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, list) else FIELD_SPLIT.split(str(raw))
    keys: list[str] = []
    for item in items:
        key = str(item).strip().strip("\"'")
        if key and key not in keys:
            keys.append(key)
    return keys


def field_keys(widget, spec=None) -> list[str]:
    """Порядок полей: из конфига (fields = "а, б") или как объявлено в типе."""
    spec = spec or type_spec(widget.type)
    if spec is None:
        return []
    known = [key for key, _label in spec.FIELDS]
    keys = [key for key in raw_fields(widget) if key in known]
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
    return spec.TITLE if spec else (widget.type or "widget")


def short_disk(path) -> str:
    """``C:\\Program Files`` → «C:/Program Files», ``/`` → «/», ``/mnt/data`` → «/mnt/data».

    Длинный путь ужимается до двух последних шагов: ``/var/lib/docker`` → «…/lib/docker».
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    drive = ""
    match = re.match(r"^([A-Za-z]):(.*)$", text)
    if match:
        drive, text = match.group(1).upper() + ":", match.group(2)
    parts = [part for part in text.split("/") if part]
    if not parts:
        return drive or "/"
    tail = "/".join(parts[-2:])
    short = ("…/" if len(parts) > 2 else "/") + tail
    return drive + short if drive else short


def disk_caption(widget) -> str | None:
    """У диска без ``name`` подпись — сами пути, но коротко: «/», «C:», «/ ·/data»."""
    if canonical_type(widget.type) != "disk" or remote_host(widget.options):
        return None      # у соседа пути его, а не наши: подпись придёт вместе с данными
    paths = [short_disk(path) for path in disk_paths(widget.options.get("disk"))]
    paths = [path for path in paths if path]
    if not paths:
        return None
    if len(paths) > 2:
        return f"{DISK_CAPTION_JOIN.join(paths[:2])} +{len(paths) - 2}"
    return DISK_CAPTION_JOIN.join(paths)


def caption_for(widget) -> str | None:
    """``name = "C:"`` — подпись виджета на карточке; у диска иначе короткие пути."""
    raw = widget.options.get("name")
    text = str(raw).strip() if raw is not None else ""
    return text or disk_caption(widget)


def validate(widget) -> list[str]:
    """Проверка секции ``[widget:…]`` на этапе загрузки конфига."""
    problems: list[str] = []
    spec = type_spec(widget.type)
    remote = bool(remote_host(widget.options))
    if spec is None and not remote:
        hint = repr(widget.type) if widget.type else "не указан type"
        problems.append(f"виджет {widget.name!r}: {hint} — известны {', '.join(sorted(REGISTRY))}")
        return problems
    if spec is None:
        # тип придёт вместе с данными: у соседа виджет уже описан
        extra = [key for key in widget.options if key not in ("fields",) + REMOTE_OPTIONS]
        if extra:
            problems.append(f"виджет {widget.name!r}: лишние ключи {', '.join(sorted(extra))}")
        if not widget.options.get("external"):
            problems.append(f"виджет {widget.name!r}: без type нужен external — какой виджет брать у хоста")
        return problems
    if not remote:      # у соседа диск уже прочитан: disk = "" локально не нужен
        missing = [key for key in spec.REQUIRED if not widget.options.get(key)]
        if missing:
            problems.append(f"виджет {widget.name!r} ({widget.type}): не заданы {', '.join(missing)}")
    extra = [key for key in widget.options
             if key not in spec.REQUIRED + spec.OPTIONAL + ("type", "fields") + REMOTE_OPTIONS]
    if extra:
        problems.append(f"виджет {widget.name!r} ({widget.type}): лишние ключи {', '.join(sorted(extra))}")
    problems += _field_problems(widget, spec)
    return problems


def _field_problems(widget, spec) -> list[str]:
    """Понятная ошибка, если в fields опечатка в имени поля."""
    known = [key for key, _label in spec.FIELDS]
    requested = raw_fields(widget)
    problems: list[str] = []
    unknown = [key for key in requested if key not in known]
    if unknown:
        problems.append(f"виджет {widget.name!r} ({widget.type}): неизвестные поля "
                        f"{', '.join(unknown)} — известны {', '.join(known)}")
    return problems


def _crossed(widget, spec) -> list[dict]:
    """Поля с крестами: старые цифры не выдаём за свежие."""
    labels = labels_for(spec)
    return [{"key": key, "label": labels[key], "value": CROSS, "tone": "down", "tip": None}
            for key in field_keys(widget, spec)]


def _sentence(text: str) -> str:
    """Причина одной фразой для подсказки: без хвостов, с точкой на конце."""
    text = _clean(text)
    if not text:
        return "Unknown error."
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _failed(widget, spec, stamp: float, detail: str) -> dict:
    """Ошибка — полноправное состояние карточки: кресты и причина в подсказке."""
    return {"ok": False, "name": widget.name, "type": widget.type, "at": stamp,
            "error": _sentence(detail), "caption": caption_for(widget),
            "fields": _crossed(widget, spec) if spec is not None else [],
            "bar": None}


def _render_fields(widget, spec, values: dict) -> list[dict]:
    """Поля карточки из сырых значений: подписи и формат — от типа виджета."""
    labels = labels_for(spec)
    options = widget.options
    fields = []
    for key in field_keys(widget, spec):
        value = values.get(key)
        fields.append({"key": key, "label": labels[key],
                       "value": spec.show(key, value, options),
                       "tone": spec.tone(key, value, options),
                       "tip": NO_TEMPERATURE if key == "cputemp" and value is None else None})
    return fields


def _safe_bar(spec, values: dict, options: dict) -> dict | None:
    """Полосочка не имеет права ронять карточку."""
    try:
        return spec.bar(values, options)
    except Exception:  # noqa: BLE001
        return None


def run_widget(widget) -> dict:
    """Один поход в чужой API. Никогда не бросает: ошибка — это тоже состояние."""
    stamp = time.time()
    if remote_host(widget.options):
        return _remote_widget(widget, stamp)      # данные со своего соседа, не из psutil
    spec = type_spec(widget.type)
    if spec is None:
        return _failed(widget, None, stamp, f"Unknown widget type {widget.type}")
    missing = [key for key in spec.REQUIRED if not widget.options.get(key)]
    if missing:
        return _failed(widget, spec, stamp, "Not configured: missing " + ", ".join(missing))
    try:
        values = spec.fetch(widget.options)
    except WidgetFailure as error:
        return _failed(widget, spec, stamp, str(error))
    except Exception as error:  # noqa: BLE001 — виджет не имеет права ронять страницу
        return _failed(widget, spec, stamp, f"{type(error).__name__}: {error}")
    if not isinstance(values, dict):
        return _failed(widget, spec, stamp, "Widget returned no data")
    return {"ok": True, "name": widget.name, "type": widget.type, "title": spec.TITLE,
            "caption": caption_for(widget), "fields": _render_fields(widget, spec, values),
            "values": _plain(values), "bar": _safe_bar(spec, values, widget.options), "at": stamp}


# --------------------------------------------------------------------------- #
# виджет с другого хоста: host = "192.168.0.112:24556", external = "disk"
#
# Сосед — такой же dashboard. Он отдаёт ``/api/widget?id=…``: готовые поля и сырые
# значения. Сырые нужны, чтобы подпись, единицы и шкала температуры остались
# своими: цифры чужие, оформление — этого хоста.
# --------------------------------------------------------------------------- #

REMOTE_PATH = "/api/widget?id="
REMOTE_OPTIONS = ("host", "external", "timeout")


def remote_host(options: dict | None) -> str:
    """``host = "192.168.0.112:24556"`` → ``http://192.168.0.112:24556``; "" — свой хост."""
    raw = str((options or {}).get("host") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def remote_label(options: dict | None) -> str:
    """Адрес соседа как его написали в конфиге — для подсказки на карточке."""
    raw = str((options or {}).get("host") or "").strip()
    return raw.replace("http://", "").replace("https://", "").rstrip("/")


def remote_identifiers(widget) -> list[str]:
    """Кого спрашивать у соседа: external, иначе тип, иначе имя секции."""
    asked = [str(widget.options.get("external") or "").strip(), (widget.type or "").strip(),
             (widget.name or "").strip()]
    found: list[str] = []
    known: set[str] = set()
    for item in asked:
        if item and item.casefold() not in known:
            found.append(item)
            known.add(item.casefold())
    return found


def find_widget(site, ident: str):
    """Виджет по имени секции, по «карточка.виджет» или по типу — что просит сосед."""
    key = (ident or "").strip().casefold()
    if not key:
        return None
    found = site.widgets.get(key)
    if found is None and re.search(r"[/.]", key):
        found = site.widgets.get(re.split(r"[/.]", key)[-1])
    if found is None:
        wanted = canonical_type(key)
        found = next((widget for widget in site.widgets.values()
                      if canonical_type(widget.type) == wanted), None)
    return found


def _plain(values: dict) -> dict:
    """Только то, что переживёт JSON: соседу незачем больше."""
    return {key: value for key, value in (values or {}).items()
            if value is None or isinstance(value, (bool, int, float, str))}


def remote_payload(widget) -> dict:
    """Ответ соседу: тот же виджет, что на карточке, вместе с сырыми значениями.

    Ключ кэша тот же, что у карточки: сосед и своя страница берут один поход
    к источнику, а не два.
    """
    answer = _cached(f"widget|{widget.name.casefold()}", lambda: run_widget(widget), WIDGET_TTL).value
    return dict(answer, id=widget.name)      # name — как на карточке, id — как спросили


def _fetch_remote(widget) -> dict:
    """Первый ответ соседа, где виджет нашёлся: external → тип → имя секции."""
    base = remote_host(widget.options)
    timeout = _num(widget.options.get("timeout"), DEFAULT_TIMEOUT)
    problem = f"No widget {widget.name!r} there"
    for ident in remote_identifiers(widget):
        try:
            raw = _request(base + REMOTE_PATH + quote(ident, safe=""), timeout=timeout)
            answer = _json(raw, "this dashboard, without /api/widget?")
        except WidgetFailure as error:
            raise WidgetFailure(str(error)) from error   # хост один: другой id не поможет
        if answer.get("ok"):
            return answer
        problem = str(answer.get("error") or problem)
    raise WidgetFailure(problem)


def _remote_widget(widget, stamp: float) -> dict:
    """Виджет соседа: цифры оттуда, подпись, единицы и шкала — свои."""
    spec = type_spec(widget.type)
    try:
        payload = _fetch_remote(widget)
    except WidgetFailure as error:
        return _failed(widget, spec, stamp, str(error))
    except Exception as error:  # noqa: BLE001
        return _failed(widget, spec, stamp, f"{type(error).__name__}: {error}")
    spec = spec or type_spec(payload.get("type"))
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    fields = _render_fields(widget, spec, values) if spec is not None and values else []
    if not [field for field in fields if field.get("value") is not None]:
        fields = [dict(field) for field in payload.get("fields") or []]   # не сошлись типами
    bar = _safe_bar(spec, values, widget.options) if spec is not None and values else payload.get("bar")
    return {"ok": True, "name": widget.name,
            "type": widget.type or str(payload.get("type") or ""),
            "title": spec.TITLE if spec is not None else str(payload.get("title") or widget.name),
            "caption": caption_for(widget) or payload.get("caption"),
            "fields": fields, "values": _plain(values), "bar": bar, "at": stamp,
            "remote": remote_host(widget.options)}


# --------------------------------------------------------------------------- #
# ping
# --------------------------------------------------------------------------- #

def split_target(target: str, fallback_port: int | None = None) -> tuple[str, int]:
    """``host``, ``host:port`` и ``http(s)://host:port/путь`` → (host, port)."""
    text = (target or "").strip()
    if not text:
        raise ValueError("Empty target")
    if "://" not in text:
        text = "//" + text
    parts = urlsplit(text)
    host = parts.hostname
    if not host:
        raise ValueError(f"Cannot parse target {target!r}")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"Cannot parse port in {target!r}") from error
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
        return {"state": "down", "ms": None, "target": f"{host}:{port}", "error": str(error) or "No response"}
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
        _KEY_LOCKS.clear()
        _NPM_TOKENS.clear()


def _peek(key: str) -> Entry | None:
    with _LOCK:
        return _CACHE.get(key)


_KEY_LOCKS: dict[str, threading.Lock] = {}


def _key_lock(key: str) -> threading.Lock:
    with _LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = _KEY_LOCKS[key] = threading.Lock()
        return lock


def _cached(key: str, producer, ttl: float) -> Entry:
    """Значение под ключом: свежее отдаём, протухшее обновляет один поток.

    Без замка на ключ две вкладки в одну секунду давали два похода к источнику
    и два замера CPU: второй считался по дельте в микросекунды.
    """
    entry = _peek(key)
    if entry is not None and entry.expires > time.monotonic():
        return entry
    with _key_lock(key):
        entry = _peek(key)
        if entry is not None and entry.expires > time.monotonic():
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


def _plan(site) -> dict[str, dict]:
    """Что и по каким ключам кэша нужно для каждой карточки.

    У карточки бывает несколько виджетов (``widget = "один, другой"``):
    каждый живёт своим ключом кэша и рисуется под предыдущим.
    """
    plan: dict[str, dict] = {}
    for service in site.services:
        ping_item = None
        widgets: list[tuple] = []          # (ключ кэша, чем обновлять, ttl, имя)
        if service.ping:
            timeout = float(service.ping_timeout or 2.0)
            ping_item = (f"ping|{service.ping}|{timeout}",
                         lambda s=service: ping(s.ping, timeout), PING_TTL)
        for name in service.widgets:
            widget = site.widgets.get(name.casefold())
            if widget is None:
                continue
            widgets.append((f"widget|{widget.name.casefold()}",
                            lambda w=widget: run_widget(w), WIDGET_TTL, widget.name))
        if ping_item or widgets:
            group = site.groups.get(service.group_key)
            plan[card_identity(service, group.name if group else None)] = {
                "ping": ping_item, "widgets": widgets}
    return plan


def snapshot(site, online: bool = True) -> dict:
    """Состояние всех карточек. ``online=False`` — только из кэша, без сети."""
    plan = _plan(site)
    if online:
        wanted: dict[str, tuple] = {}
        for item in plan.values():
            if item["ping"]:
                key, producer, ttl = item["ping"]
                wanted.setdefault(key, (producer, ttl))
            for key, producer, ttl, _name in item["widgets"]:
                wanted.setdefault(key, (producer, ttl))
        stale = [key for key in wanted if (_peek(key) is None or _peek(key).expires <= time.monotonic())]
        if stale:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(stale))) as pool:
                list(pool.map(lambda key: _cached(key, wanted[key][0], wanted[key][1]), stale))
    cards: dict[str, dict] = {}
    for identity, item in plan.items():
        card: dict = {}
        if item["ping"]:
            entry = _peek(item["ping"][0])
            if entry is not None:
                # пинг отдаём без адреса: клиент не должен знать, куда мы стучимся
                card["ping"] = wire_ping(entry.value)
                card["ping_at"] = round(entry.at, 3)
        live = [dict(entry.value) for entry in (_peek(key) for key, _p, _t, _n in item["widgets"])
                if entry is not None]
        if live:
            card["widgets"] = live
        if card:                      # карточка без данных не занимает места
            cards[identity] = card
    return {"cards": cards, "poll": site.system.poll, "ts": round(time.time(), 3)}
