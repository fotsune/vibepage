"""Turn a loaded Site into the view model the template renders.

Rules implemented here:

* a group's ``priority`` picks the column it lives in (1-based, wrapped by
  ``system.cols``); groups without a priority fill columns left to right and
  wrap onto the bottom of the first column again;
* a group with ``link = Other`` is merged into Other's column and is stacked
  right after it — the one exception to "one group, one column";
* priorities may be missing or repeated: ties are broken by name;
* services inside a group are ordered the same way (priority, then name).
"""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from . import widgets as widget_lib
from .colors import auto as auto_color
from .config import Group, Service, Site, group_key


LOCALISH_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "home", "homepage"}


def sort_key(item) -> tuple:
    """Missing priority sorts after explicit ones; equal priorities sort by name."""
    priority = getattr(item, "priority", None)
    return (priority is None, priority or 0, item.name.casefold())


def host_is_local(host: str | None) -> bool:
    """True when the page was requested over a LAN/localhost address."""
    if not host:
        return True
    host = host.strip().lower()
    if host.startswith("["):                  # [::1]:8080
        bare = host[1:host.find("]")] if "]" in host else host.strip("[]")
    else:                                     # name:port or plain name
        bare = host.split(":", 1)[0]
    if bare in LOCALISH_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        # single-label hostnames ("nas", "server") count as local
        return bool(bare) and "." not in bare
    return address.is_private or address.is_loopback or address.is_link_local


def _root_of(key: str, groups: dict[str, Group], warnings: list[str]) -> str:
    """Follow the ``link`` chain; cycles collapse onto their alphabetically first member."""
    path: list[str] = []
    current = key
    while True:
        group = groups.get(current)
        target = group_key(group.link) if group and group.link else None
        if target is None or target not in groups:
            if target is not None:
                warnings.append(
                    f"группа {group.name!r} ссылается на неизвестную группу {group.link!r}"
                )
            return current
        if target == current:                       # self-reference
            return current
        if target in path:                          # A -> B -> A: one owner, no loop
            cycle = path[path.index(target):] + [current, target]
            return min(cycle)
        path.append(current)
        current = target


def _plural(count: int, one: str, few: str, many: str) -> str:
    """17 сервисов, 3 сервиса, 1 сервис."""
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _card_urls(service: Service, prefer_alt: bool) -> tuple[str | None, str | None]:
    """Pick the URL a card opens, plus an optional second one behind a chip.

    ``alt`` is the local/alternative address: it wins when the page prefers
    alt (private address, or ``[header] link_mode = "alt"``), ``link``
    otherwise.
    """
    link, alt = service.link, service.alt
    if not link:
        return alt, None
    if not alt or alt == link:
        return link, None
    return (alt, link) if prefer_alt else (link, alt)


def _url_is_local(url: str | None) -> bool:
    """Is this link an address only reachable from the LAN?"""
    if not url:
        return False
    try:
        host = urlsplit(url).netloc or urlsplit("//" + url).netloc
    except ValueError:
        return False
    return bool(host) and host_is_local(host)


def _link_title(url: str | None) -> str:
    """Подпись для ссылки — по тому, локальная она или внешняя; по-английски, как и прочие служебные."""
    return "Open local link" if _url_is_local(url) else "Open external link"


def _initial(name: str) -> str:
    for char in name:
        if char.isalnum():
            return char.upper()
    return "?"


def _widget_view(name: str, site: Site) -> dict:
    """Скелет виджета: поля с подписями и, если кэш уже тёплый, значения."""
    widget = site.widgets.get(group_key(name))
    if widget is None:
        return {"name": name, "title": name, "kind": "", "caption": None, "fields": [], "bar": None,
                "rows": None, "ok": False,
                "phase": "error", "error": "Widget is not declared.", "at": 0}
    spec_fields = widget_lib.fields_for(widget)
    state = widget_lib.peek_widget(widget)
    failed = bool(state) and not state.get("ok")   # ошибка уже известна — кресты, не старые цифры
    values = {}
    tones = {}
    tips = {}
    if state and state.get("ok"):
        for field in state.get("fields") or []:
            values[field["key"]] = field.get("value")
            tones[field["key"]] = field.get("tone")
            tips[field["key"]] = field.get("tip")
    # подпись: своя из name, иначе та, что пришла вместе с чужими данными
    caption = widget_lib.caption_for(widget) or (state or {}).get("caption")
    # у виджета-соседа подпись говорит, что цифры не отсюда; адреса на карточке нет
    label = widget_lib.remote_label(widget.options)
    return {
        "name": widget.name,
        "title": widget_lib.title_for(widget),
        "kind": widget.type,
        "caption": caption,
        "caption_tip": " · ".join(item for item in (caption, widget_lib.REMOTE_HINT) if item) if label else None,
        "fields": [
            {"key": field["key"], "label": field["label"],
             "value": widget_lib.CROSS if failed else values.get(field["key"]),
             "tone": "down" if failed else tones.get(field["key"], "idle"),
             "tip": None if failed else tips.get(field["key"])}
            for field in spec_fields
        ],
        # полосочка статуса: только у системных и дисковых, и только со свежими данными
        "bar": (state or {}).get("bar") if state and state.get("ok") else None,
        # строки под полями: очередь раздач, что играет сейчас
        "rows": (state or {}).get("rows") if state and state.get("ok") else None,
        "ok": bool(state and state.get("ok")),
        "phase": "error" if failed else ("ok" if state else "cold"),
        "error": (state or {}).get("error", "") if state else "",
        "at": (state or {}).get("at", 0),
    }


def _ping_view(service: Service) -> dict | None:
    """Точка статуса: хозяйство сервера, клиент только перекрашивает."""
    if not service.ping:
        return None
    state = widget_lib.peek_ping(service.ping, float(service.ping_timeout or 2.0))
    return {"state": (state or {}).get("state", "unknown"),
            "ms": (state or {}).get("ms"), "title": widget_lib.ping_title(state),
            "at": (state or {}).get("at", 0)}


def _build_service(service: Service, system, prefer_alt: bool, resolve_icon, accent: str,
                   site: Site | None = None, identity: str = "") -> dict:
    """One card: the URL it opens now, the other one, and both tints."""
    primary, secondary = _card_urls(service, prefer_alt)
    new_page = system.open_in_new_page if service.new_page is None else service.new_page
    main = service.link or service.alt                     # "основная" = link
    alt = service.alt if service.alt and service.alt != service.link else None
    return {
        "key": identity,
        "name": service.name,
        "desc": service.desc or "",
        "url": primary,
        "url_alt": secondary,
        "url_alt_title": _link_title(secondary) if secondary else "",
        # подсказка чипа зависит от режима: в main он ведёт на alt, в alt — на link
        "chip_title_main": _link_title(alt) if alt else "",
        "chip_title_alt": _link_title(main) if alt else "",
        "href_main": main or "#",
        "href_alt": alt or "",
        "icon": resolve_icon(service),
        "icon_color": service.icon_color,                  # None -> group accent
        "accent": accent,
        "initial": _initial(service.name),
        "new_page": bool(new_page),
        "priority": service.priority,
        # у карточки бывает несколько виджетов: каждый следующий — под предыдущим
        "widgets": [_widget_view(name, site) for name in service.widgets] if site is not None else [],
        "ping": _ping_view(service) if site is not None else None,
    }


REMOTE_SCHEMES = ("http://", "https://", "//", "data:")


def _favicon_url(header, resolve_icon, warnings: list[str]) -> str | None:
    """An <link rel=icon> target: the configured file, or a data URL for an inline svg."""
    if not header.favicon:
        return None
    raw = header.favicon.strip()
    if raw.startswith(REMOTE_SCHEMES):
        return raw
    found = resolve_icon(SimpleNamespace(icon=raw, icon_link=None))
    if not found:
        warnings.append(f"header.favicon: не найден {raw!r} среди иконок — оставляю стандартный")
        return None
    if "url" in found:
        return found["url"]
    return "data:image/svg+xml;utf8," + quote(found["svg"], safe="")


def build_page(site: Site, is_local: bool = True, resolve_icon=None) -> dict:
    """Assemble columns -> groups -> services, ready for the template."""
    if resolve_icon is None:
        resolve_icon = lambda service: service.icon_link  # noqa: E731
    warnings = list(site.warnings)
    header = site.header
    # link_mode = main/alt перебивает автоопределение по адресу подключения
    default_mode = header.link_mode if header.link_mode in ("main", "alt") else (
        "alt" if is_local else "main"
    )
    prefer_alt = default_mode == "alt"
    groups = site.groups
    cols = max(1, site.system.cols or 1)

    roots = {key: _root_of(key, groups, warnings) for key in groups}
    owners = [key for key in groups if roots[key] == key]

    column_of: dict[str, int] = {}
    next_column = 1
    for key in sorted(owners, key=lambda k: sort_key(groups[k])):
        group = groups[key]
        if group.priority is not None:
            column_of[key] = (group.priority - 1) % cols + 1
        else:
            column_of[key] = next_column
            next_column = next_column % cols + 1
    for key in groups:
        column_of.setdefault(key, column_of.get(roots[key], 1))

    services_by_group: dict[str, list[Service]] = {key: [] for key in groups}
    for service in site.services:
        services_by_group.setdefault(service.group_key or group_key(service.group), []).append(service)

    columns: list[list[dict]] = [[] for _ in range(cols)]
    ordered_keys = sorted(
        groups,
        key=lambda k: (
            sort_key(groups[roots[k]]),
            1 if k != roots[k] else 0,
            sort_key(groups[k]),
        ),
    )
    accent_of = {key: group.color or auto_color(group.name) for key, group in groups.items()}

    for flow, key in enumerate(ordered_keys):
        group = groups[key]
        cards = [_build_service(service, site.system, prefer_alt, resolve_icon, accent_of[roots[key]], site,
                                widget_lib.card_identity(service, group.name))
                 for service in sorted(services_by_group.get(key, []), key=sort_key)]
        root = groups[roots[key]]
        # сколько столбцов карточек отведено группе: своё cols, иначе system.group_cols; 0 — без предела
        card_cols = group.cols if group.cols is not None else site.system.group_cols
        columns[column_of[key] - 1].append({
            "name": group.name,
            "cols": max(0, card_cols or 0),
            "flow": flow,
            "accent": accent_of[roots[key]],
            "root": root.name,
            "merged": key != roots[key],
            "declared": group.declared,
            "services": cards,
        })

    live = widget_lib.snapshot(site, online=False)   # только кэш: первый рендер не ждёт сеть
    return {
        "title": site.system.title,
        "poll": site.system.poll,
        "state": live,
        "cols": cols,
        "mobile_cols": max(1, site.system.mobile_cols or 1),
        "columns": [column for column in columns],
        "group_count": len(groups),
        "service_count": len(site.services),
        "services_label": _plural(len(site.services), "сервис", "сервиса", "сервисов"),
        "groups_label": _plural(len(groups), "группа", "группы", "групп"),
        "errors": site.errors,
        "warnings": warnings,
        "is_local": is_local,
        "default_mode": default_mode,
        "header": {
            "show": header.show,
            "time": header.time,
            "time_format": header.time_format,
            "search": header.search,
            "link_mode": header.link_mode,
            "desc": header.desc,
            "icon": resolve_icon(SimpleNamespace(icon=header.icon, icon_link=None)) if header.icon else None,
            "favicon": _favicon_url(header, resolve_icon, warnings),
        },
    }
