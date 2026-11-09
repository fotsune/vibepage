"""Состояние карточек: ping и виджеты (Portainer), с кэшем на сервере.

Страница по-прежнему рендерится сервером целиком. Клиент лишь раз в
``system.poll`` секунд спрашивает ``/api/state`` и обновляет уже нарисованные
карточки; кэш не даёт этому поллингу превратиться в поллинг Portainer.

Тип виджета — небольшой класс:

* ``FIELDS``   — до трёх пар (ключ, подпись), порядок = порядок в карточке;
* ``REQUIRED`` / ``OPTIONAL`` — ключи секции ``[widget:…]``;
* ``fetch(options)`` возвращает dict «ключ → значение»;
* ``tone(key, value)`` выбирает цвет значения: good / bad / idle.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PING_OK_MS = 350          # быстрее — зелёный, медленнее — жёлтый
PING_TTL = 5.0
WIDGET_TTL = 10.0
DEFAULT_TIMEOUT = 4.0
MAX_WORKERS = 8


class WidgetFailure(Exception):
    """Причина, которую не стыдно показать прямо в карточке."""


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


# --------------------------------------------------------------------------- #
# Portainer
# --------------------------------------------------------------------------- #

class Portainer:
    """Счётчики контейнеров: запущено / остановлено / всего."""

    TITLE = "Portainer"
    FIELDS = (("running", "запущено"), ("stopped", "остановлено"), ("total", "всего"))
    REQUIRED = ("url", "key")
    OPTIONAL = ("env", "timeout", "verify_tls")

    def fetch(self, options: dict) -> dict:
        base = str(options.get("url", "")).rstrip("/")
        endpoint = str(options.get("env", 1)).strip() or "1"
        timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        url = f"{base}/api/endpoints/{endpoint}/docker/containers/json?all=1"
        request = Request(url, headers={
            "X-API-Key": str(options.get("key", "")),
            "Accept": "application/json",
            "User-Agent": "homepage-dashboard",
        })
        context = None if _truthy(options.get("verify_tls", False)) else ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=timeout, context=context) as response:
                raw = response.read(4_000_000)
        except HTTPError as error:
            raise WidgetFailure(f"Portainer ответил {error.code}") from error
        except URLError as error:
            host = urlsplit(base).netloc or base
            raise WidgetFailure(f"нет связи с {host}: {error.reason}") from error
        except (socket.timeout, TimeoutError) as error:
            raise WidgetFailure("Portainer не ответил вовремя") from error
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise WidgetFailure("ответ не JSON — включён ли API у Portainer?") from error
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


REGISTRY = {"portainer": Portainer()}


def type_spec(kind: str | None):
    return REGISTRY.get((kind or "").strip().lower())


def fields_for(widget) -> list[dict]:
    """Поля для скелета карточки — их видно ещё до первого ответа."""
    spec = type_spec(widget.type)
    if spec is None:
        return []
    return [{"key": key, "label": label} for key, label in spec.FIELDS]


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
    extra = [key for key in widget.options if key not in spec.REQUIRED + spec.OPTIONAL + ("type",)]
    if extra:
        problems.append(f"виджет {widget.name!r} ({widget.type}): лишние ключи {', '.join(sorted(extra))}")
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
    fields = [{"key": key, "label": label, "value": values.get(key), "tone": spec.tone(key, values.get(key))}
              for key, label in spec.FIELDS]
    return {"ok": True, "type": widget.type, "title": spec.TITLE, "fields": fields, "at": stamp}


# --------------------------------------------------------------------------- #
# ping
# --------------------------------------------------------------------------- #

def split_target(target: str) -> tuple[str, int]:
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
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError(f"не разобрал порт в {target!r}") from error
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


# --------------------------------------------------------------------------- #
# кэш и снимок
# --------------------------------------------------------------------------- #

Entry = namedtuple("Entry", "value expires at")
_CACHE: dict[str, Entry] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


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
                card[kind] = entry.value
                card[f"{kind}_at"] = round(entry.at, 3)
        if card:                      # карточка без данных не занимает места
            cards[identity] = card
    return {"cards": cards, "poll": site.system.poll, "ts": round(time.time(), 3)}
