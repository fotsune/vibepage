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


def _card_urls(service: Service, is_local: bool) -> tuple[str | None, str | None]:
    """Pick the URL a card opens, plus an optional second one behind a chip.

    ``alt`` is the local/alternative address: it wins while browsing from a
    private address, ``link`` wins everywhere else.
    """
    link, alt = service.link, service.alt
    if not link:
        return alt, None
    if not alt or alt == link:
        return link, None
    return (alt, link) if is_local else (link, alt)


def _initial(name: str) -> str:
    for char in name:
        if char.isalnum():
            return char.upper()
    return "?"


def _build_service(service: Service, system, is_local: bool, resolve_icon, accent: str) -> dict:
    """One card: the URL it opens now, the other one, and both tints."""
    primary, secondary = _card_urls(service, is_local)
    new_page = system.open_in_new_page if service.new_page is None else service.new_page
    if secondary is None or secondary == service.alt:
        secondary_title = "Открыть внешнюю ссылку"
    else:
        secondary_title = "Открыть локальную ссылку"
    main = service.link or service.alt                     # "основная" = link
    alt = service.alt if service.alt and service.alt != service.link else None
    return {
        "name": service.name,
        "desc": service.desc or "",
        "url": primary or "#",
        "url_alt": secondary,
        "url_alt_title": secondary_title,
        "href_main": main or "#",
        "href_alt": alt or "",
        "icon": resolve_icon(service),
        "icon_color": service.icon_color,                  # None -> group accent
        "accent": accent,
        "initial": _initial(service.name),
        "new_page": bool(new_page),
        "priority": service.priority,
    }


def build_page(site: Site, is_local: bool = True, resolve_icon=None) -> dict:
    """Assemble columns -> groups -> services, ready for the template."""
    if resolve_icon is None:
        resolve_icon = lambda service: service.icon_link  # noqa: E731
    warnings = list(site.warnings)
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

    for key in ordered_keys:
        group = groups[key]
        cards = [_build_service(service, site.system, is_local, resolve_icon, accent_of[roots[key]])
                 for service in sorted(services_by_group.get(key, []), key=sort_key)]
        root = groups[roots[key]]
        columns[column_of[key] - 1].append({
            "name": group.name,
            "accent": accent_of[roots[key]],
            "root": root.name,
            "merged": key != roots[key],
            "declared": group.declared,
            "services": cards,
        })

    return {
        "title": site.system.title,
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
        "default_mode": "alt" if is_local else "main",
    }
