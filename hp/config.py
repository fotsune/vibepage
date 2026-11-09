"""TOML-like configuration parsing and loading for the homepage dashboard.

The format is deliberately *TOML-like* rather than strict TOML: section
headers carry free-form names (``[service:My Server]``) and values may stay
bare strings, so files stay pleasant to write by hand.

Sections
--------
``[system]``            global settings: title, columns, paths
``[header]``            the top bar: clock, search, icons, link mode
``[widget:Name]``       a live widget (portainer, …) a card can show
``[service:Name]``      one service card
``[group:Name]``        one group of services

Everything is re-read from disk on every request, so editing a file and
pressing F5 is the whole update cycle.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from glob import escape as glob_escape, glob

from .colors import parse as parse_color

DEFAULT_GROUP = "Default"

SECTION_RE = re.compile(r"^\[\s*([A-Za-z_][\w-]*)\s*(?::\s*(.*?))?\s*\]$")
KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*=\s*(.*)$")
INT_RE = re.compile(r"[+-]?\d+")

SERVICE_KEYS = {"link", "alt", "name", "icon", "icon_link", "icon_color", "widget", "ping", "ping_timeout",
                "desc", "group", "priority", "new_page"}
GROUP_KEYS = {"name", "priority", "link", "color"}
SYSTEM_KEYS = {"title", "icon_path", "config_path", "cols", "mobile_cols", "open_in_new_page", "poll"}
HEADER_KEYS = {"show", "enabled", "header", "time", "time_format", "icon", "favicon", "search",
               "link_mode", "main_alt", "desc", "tagline", "subtitle"}
LINK_MODES = {"auto": "auto", "main": "main", "link": "main", "primary": "main", "external": "main",
              "alt": "alt", "alternate": "alt", "local": "alt"}

URL_SCHEMES = ("http://", "https://", "//", "data:")


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #

@dataclass
class System:
    title: str = "Homepage"
    icon_path: str | None = None
    config_paths: list[str] = field(default_factory=list)
    cols: int = 3
    mobile_cols: int = 1
    open_in_new_page: bool = False
    poll: int = 10                   # раз в сколько секунд клиент спрашивает состояние


@dataclass
class Header:
    """The top bar. Every switch is on by default; link_mode stays automatic."""

    show: bool = True                 # вся шапка целиком
    time: bool = True                # часы
    time_format: int = 12            # 12 (AM/PM) или 24
    search: bool = True              # строка поиска
    icon: str | None = None          # логотип, путь относительно system.icon_path
    favicon: str | None = None       # favicon, путь относительно system.icon_path
    link_mode: str = "auto"          # auto | main | alt
    desc: str = "self-hosted dashboard"   # подпись под заголовком; "" — не показывать


@dataclass
class Widget:
    """``[widget:Name]`` — живое состояние карточки, за которым ходит сервер."""

    name: str
    type: str = ""
    options: dict = field(default_factory=dict)
    source: str | None = None


@dataclass
class Group:
    name: str
    priority: int | None = None
    link: str | None = None
    color: str | None = None          # accent; None means pick one from the name
    declared: bool = True
    source: str | None = None


@dataclass
class Service:
    name: str
    group: str = DEFAULT_GROUP
    link: str | None = None
    alt: str | None = None
    icon: str | None = None
    icon_link: str | None = None
    icon_color: str | None = None     # tint; None means the group accent
    desc: str | None = None
    priority: int | None = None
    new_page: bool | None = None
    widget: str | None = None         # имя секции [widget:…]
    ping: str | None = None           # host, host:port или URL
    ping_timeout: float | None = None
    source: str | None = None
    group_key: str = ""


@dataclass
class Site:
    """Everything a request needs: settings, declared groups, services."""

    system: System = field(default_factory=System)
    header: Header = field(default_factory=Header)
    groups: dict[str, Group] = field(default_factory=dict)   # keyed by casefolded name
    widgets: dict[str, Widget] = field(default_factory=dict)  # keyed by casefolded name
    services: list[Service] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config_dir: str = ""

    def group(self, name: str | None) -> Group:
        key = (name or DEFAULT_GROUP).casefold()
        found = self.groups.get(key)
        if found is None:
            found = Group(name=name or DEFAULT_GROUP, declared=False)
            self.groups[key] = found
        return found


ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    """``${PORTAINER_KEY}`` (и ``${VAR:-по умолчанию}``) → значение из окружения."""
    return ENV_RE.sub(lambda match: os.environ.get(match.group(1), match.group(2) or ""), text)


def group_key(name: str | None) -> str:
    return (name or DEFAULT_GROUP).casefold()


# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _unescape(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_array(raw: str) -> list:
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    inner = raw[1:-1]
    for i, ch in enumerate(inner):
        if quote:
            buf.append(ch)
            if ch == quote and inner[i - 1] != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf))
    return [parse_value(item) for item in items if item.strip()]


def parse_value(raw: str):
    """Parse one right-hand side: quoted string, list, bool, int or bare text."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        return _split_array(raw)
    if raw[0] in "\"'":
        quote = raw[0]
        for i in range(1, len(raw)):
            if raw[i] == quote and raw[i - 1] != "\\":
                return _unescape(raw[1:i])
        return _unescape(raw[1:])
    bare = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    low = bare.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if INT_RE.fullmatch(bare):
        return int(bare)
    return bare


def _strip_comment(text: str) -> str:
    """``env = 1   # пояснение`` → ``env = 1``; ``color = #8be9fd`` не трогаем."""
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and index and text[index - 1] in " \t":
            head = text[:index]
            if "=" in head:
                if head.split("=", 1)[1].strip():
                    return head.rstrip()
            elif head.strip():
                return head.rstrip()
    return text


def parse_file(path: str) -> tuple[list[dict], list[str]]:
    """Read one config file into raw sections plus a list of problems."""
    sections: list[dict] = []
    errors: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return [], [f"{os.path.basename(path)}: {exc.strerror or exc}"]
    current: dict | None = None
    for lineno, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        text = _strip_comment(text)
        if text.startswith("["):
            match = SECTION_RE.match(text)
            if not match:
                errors.append(f"{os.path.basename(path)}:{lineno}: не читается заголовок {text!r}")
                current = None
                continue
            kind = match.group(1).lower()
            name = (match.group(2) or "").strip().strip("'\"")
            if kind not in ("system", "header", "widget", "service", "group"):
                errors.append(f"{os.path.basename(path)}:{lineno}: неизвестная секция [{kind}]")
                current = None
                continue
            if kind not in ("system", "header") and not name:
                errors.append(f"{os.path.basename(path)}:{lineno}: у секции [{kind}] нет имени")
                current = None
                continue
            current = {
                "kind": kind,
                "name": name or None,
                "values": {},
                "source": path,
                "line": lineno,
            }
            sections.append(current)
            continue
        match = KEY_RE.match(text)
        if not match:
            errors.append(f"{os.path.basename(path)}:{lineno}: ожидалось 'ключ = значение' — {text!r}")
            continue
        if current is None:
            errors.append(f"{os.path.basename(path)}:{lineno}: '{match.group(1)}' вне секции")
            continue
        current["values"][match.group(1).lower()] = parse_value(match.group(2))
    return sections, errors


# --------------------------------------------------------------------------- #
# coercion helpers
# --------------------------------------------------------------------------- #

def _as_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return _expand(str(value).strip()) or None


def _as_int(value, where: str, key: str, warnings: list[str], default=None, minimum=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        number = value
    else:
        try:
            number = int(str(value).strip())
        except ValueError:
            warnings.append(f"{where}: {key} должно быть числом, получено {value!r}")
            return default
    if minimum is not None and number < minimum:
        warnings.append(f"{where}: {key} не может быть меньше {minimum}, использую {minimum}")
        return minimum
    return number


def _as_bool(value, where: str, key: str, warnings: list[str], default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    low = str(value).strip().lower()
    if low in ("true", "yes", "on", "1"):
        return True
    if low in ("false", "no", "off", "0"):
        return False
    warnings.append(f"{where}: {key} должно быть true/false, получено {value!r}")
    return default


def _as_float(value, where: str, key: str, warnings: list[str], default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        warnings.append(f"{where}: {key} должен быть числом, получено {value!r}")
        return default


def _resolve_path(value: str, base_dir: str) -> str:
    value = os.path.expanduser(str(value).strip())
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.normpath(value)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _config_dirs(sections: list[dict], base_dir: str, errors: list[str]) -> list[str]:
    """Every config_path from every [system] section, resolved to absolute dirs."""
    raw_values: list[str] = []
    for section in sections:
        if section["kind"] != "system":
            continue
        value = section["values"].get("config_path")
        if value in (None, ""):
            continue
        values = value if isinstance(value, list) else [value]
        raw_values += [str(item) for item in values if _as_str(item)]
    dirs: list[str] = []
    for raw in raw_values:
        resolved = _resolve_path(raw, base_dir)
        if any(char in resolved for char in "*?["):
            found = sorted(glob(resolved))
            dirs += [path for path in found if os.path.isdir(path) and path not in dirs]
            if not found:
                errors.append(f"config_path: ничего не найдено по маске {raw!r}")
            continue
        if not os.path.isdir(resolved):
            errors.append(f"config_path: каталог не найден: {resolved}")
            continue
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def _extra_files(main_path: str, dirs: list[str]) -> list[str]:
    main_abs = os.path.abspath(main_path)
    files: list[str] = []
    for directory in dirs:
        for path in sorted(glob(os.path.join(glob_escape(directory), "*.toml"))):
            path = os.path.abspath(path)
            if path != main_abs and path not in files:
                files.append(path)
    return files


def _apply_header(header: Header, values: dict, where: str, warnings: list[str]) -> None:
    for key, value in values.items():
        if key not in HEADER_KEYS:
            warnings.append(f"{where}: неизвестный ключ header.{key}")
            continue
        # main_alt — то же, что link_mode; enabled/header — то же, что show
        name = {"enabled": "show", "header": "show", "main_alt": "link_mode",
                "tagline": "desc", "subtitle": "desc"}.get(key, key)
        if name == "show":
            header.show = _as_bool(value, where, "header", warnings, header.show)
        elif name == "time":
            header.time = _as_bool(value, where, "time", warnings, header.time)
        elif name == "search":
            header.search = _as_bool(value, where, "search", warnings, header.search)
        elif name == "time_format":
            number = _as_int(value, where, "time_format", warnings, header.time_format)
            if number not in (12, 24):
                warnings.append(f"{where}: time_format бывает 12 или 24, получено {value!r} — беру 12")
                number = 12
            header.time_format = number
        elif name == "desc":
            # desc = "" прячет подпись, отсутствие ключа оставляет стандартную
            header.desc = _as_str(value) or ""
        elif name == "icon":
            header.icon = _as_str(value)
        elif name == "favicon":
            header.favicon = _as_str(value)
        elif name == "link_mode":
            mode = LINK_MODES.get(str(value).strip().lower())
            if mode is None:
                warnings.append(
                    f"{where}: link_mode бывает auto, main или alt, получено {value!r} — оставляю auto"
                )
            else:
                header.link_mode = mode


def _apply_system(site: Site, values: dict, source: str, where: str, warnings: list[str]) -> None:
    system = site.system
    base_dir = os.path.dirname(source)
    for key, value in values.items():
        if key in HEADER_KEYS:          # ключи шапки можно писать и в [system]
            _apply_header(site.header, {key: value}, where, warnings)
            continue
        if key not in SYSTEM_KEYS:
            warnings.append(f"{where}: неизвестный ключ system.{key}")
            continue
        if key == "title":
            system.title = _as_str(value) or system.title
        elif key == "icon_path":
            text = _as_str(value)
            if text and text.startswith(URL_SCHEMES):
                warnings.append(f"{where}: icon_path должен быть каталогом, пропускаю {text!r}")
            else:
                system.icon_path = _resolve_path(text, base_dir) if text else None
        elif key == "config_path":
            # only used to discover files, remembered for diagnostics
            values_list = value if isinstance(value, list) else [value]
            system.config_paths += [_resolve_path(v, base_dir) for v in values_list if _as_str(v)]
        elif key == "cols":
            system.cols = _as_int(value, where, "cols", warnings, system.cols, minimum=1)
        elif key == "mobile_cols":
            system.mobile_cols = _as_int(value, where, "mobile_cols", warnings, system.mobile_cols, minimum=1)
        elif key == "poll":
            system.poll = _as_int(value, where, "poll", warnings, system.poll, minimum=3)
        elif key == "open_in_new_page":
            system.open_in_new_page = _as_bool(
                value, where, "open_in_new_page", warnings, system.open_in_new_page
            )


def _color_of(value, where: str, key: str, warnings: list[str]) -> str | None:
    if value is None or value == "":
        return None
    found = parse_color(value)
    if found is None:
        warnings.append(f"{where}: {key}: не понимаю цвет {value!r}, беру акцент по имени")
    return found


def _make_group(section: dict, where: str, warnings: list[str]) -> Group:
    values = section["values"]
    for key in values:
        if key not in GROUP_KEYS:
            warnings.append(f"{where}: неизвестный ключ group.{key}")
    name = _as_str(values.get("name")) or section["name"] or "Unnamed"
    return Group(
        name=name,
        color=_color_of(values.get("color"), where, "color", warnings),
        priority=_as_int(values.get("priority"), where, "priority", warnings),
        link=_as_str(values.get("link")),
        declared=True,
        source=section["source"],
    )


def _make_widget(section: dict, where: str, warnings: list[str]) -> Widget:
    values = dict(section["values"])
    raw_type = values.pop("type", values.pop("kind", None))
    kind = (_as_str(raw_type) or "").strip().lower()
    options: dict = {}
    for key, value in values.items():
        if isinstance(value, list):
            options[key] = [_expand(str(item)) if isinstance(item, str) else item for item in value]
        else:
            options[key] = _expand(value) if isinstance(value, str) else value
    return Widget(name=section["name"], type=kind, options=options, source=section["source"])


def _make_service(section: dict, where: str, warnings: list[str]) -> Service:
    values = section["values"]
    for key in values:
        if key not in SERVICE_KEYS:
            warnings.append(f"{where}: неизвестный ключ service.{key}")
    name = _as_str(values.get("name")) or section["name"] or "Unnamed"
    link = _as_str(values.get("link"))
    alt = _as_str(values.get("alt"))
    if not link and not alt:
        warnings.append(f"{where}: у сервиса {name!r} нет ни link, ни alt")
    return Service(
        name=name,
        group=_as_str(values.get("group")) or DEFAULT_GROUP,
        link=link,
        alt=alt,
        icon=_as_str(values.get("icon")),
        icon_link=_as_str(values.get("icon_link")),
        icon_color=_color_of(values.get("icon_color"), where, "icon_color", warnings),
        desc=_as_str(values.get("desc")),
        widget=_as_str(values.get("widget")),
        ping=_as_str(values.get("ping")),
        ping_timeout=_as_float(values.get("ping_timeout"), where, "ping_timeout", warnings),
        priority=_as_int(values.get("priority"), where, "priority", warnings),
        new_page=_as_bool(values.get("new_page"), where, "new_page", warnings),
        source=section["source"],
    )


def load(main_path: str = "config/homepage.toml") -> Site:
    """Load the main config plus every file from its config_path directories."""
    main_path = os.path.abspath(os.path.expanduser(main_path))
    site = Site(config_dir=os.path.dirname(main_path))
    main_sections, file_errors = parse_file(main_path)
    site.errors += file_errors
    base_dir = os.path.dirname(main_path)
    if not main_sections and not file_errors:
        site.errors.append(f"{os.path.basename(main_path)}: файл пуст")

    extra_dirs = _config_dirs(main_sections, base_dir, site.errors)
    # extras first, main config last: it always wins on [system] settings
    ordered_files = _extra_files(main_path, extra_dirs) + [main_path]

    sections: list[dict] = []
    for path in ordered_files:
        if path == main_path:
            sections += [dict(section) for section in main_sections]
        else:
            parsed, problems = parse_file(path)
            site.errors += problems
            sections += parsed

    for section in sections:
        where = f"{os.path.basename(section['source'])}:{section['line']}"
        if section["kind"] == "system":
            _apply_system(site, section["values"], section["source"], where, site.warnings)
        elif section["kind"] == "widget":
            site.widgets[group_key(section["name"])] = _make_widget(section, where, site.warnings)
        elif section["kind"] == "header":
            _apply_header(site.header, section["values"], where, site.warnings)
        elif section["kind"] == "group":
            group = _make_group(section, where, site.warnings)
            key = group_key(section["name"])   # identity = имя секции, name = подпись
            existing = site.groups.get(key)
            if existing is not None:
                # same group named in several files: merge, later values win
                existing.name = group.name
                existing.priority = group.priority if group.priority is not None else existing.priority
                existing.link = group.link or existing.link
                existing.color = group.color or existing.color
                existing.declared = True
                existing.source = group.source
            else:
                site.groups[key] = group
        else:
            site.services.append(_make_service(section, where, site.warnings))

    if site.system.icon_path is None:
        site.system.icon_path = os.path.join(base_dir, "icons")

    # services point at groups either by section name or by displayed name
    by_label = {group_key(group.name): key for key, group in site.groups.items()}
    for service in site.services:
        key = group_key(service.group)
        if key not in site.groups:
            key = by_label.get(key, key)
        service.group_key = key
        # groups that only exist by being mentioned inside services
        if key not in site.groups:
            site.groups[key] = Group(name=service.group, declared=False)

    from . import widgets as widget_lib   # поздний импорт: widgets не знает про config

    for widget in site.widgets.values():
        site.warnings += widget_lib.validate(widget)
    for service in site.services:
        if service.widget and group_key(service.widget) not in site.widgets:
            known = ", ".join(sorted(widget.name for widget in site.widgets.values())) or "ни одного"
            site.warnings.append(
                f"сервис {service.name!r} ссылается на виджет {service.widget!r}, "
                f"а объявлены только: {known}"
            )

    for group in site.groups.values():
        if group.link and group_key(group.link) not in site.groups:
            site.warnings.append(f"группа {group.name!r} ссылается на неизвестную группу {group.link!r}")
    return site
