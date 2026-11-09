"""Turn a service's ``icon`` / ``icon_link`` into something to render.

Sources, in priority order:

1. ``icon = file.svg``      a file inside ``system.icon_path`` — SVG is inlined
                            so it takes the card's colour (and its hover tint);
2. ``icon = mdi:account-box`` an Iconify icon (``mdi``, ``sdi``, or any set
                            Iconify hosts). Fetched once, cached on disk under
                            ``<config>/.icon-cache``, then inlined;
3. ``icon_link = https://…`` a plain ``<img>`` source;
4. nothing                   the letter placeholder drawn by CSS.

Inlined icons are tinted with ``currentColor``, which is what lets
``icon_color`` (or the group accent) reach them at all.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

REMOTE = ("http://", "https://", "//", "data:")
MAX_INLINE = 131072          # 128 KiB, plenty for an icon
MAX_DOWNLOAD = 262144
TIMEOUT = 3.0
RETRY_AFTER = 120.0          # keep quiet after a failed fetch instead of stalling
CACHE_OK = 24 * 3600.0

# what people mean by "sdi" (Simple Icons) / shortcuts for the mdI set
SET_ALIASES = {"sdi": "simple-icons", "si": "simple-icons", "mdI": "mdi", "mdi": "mdi", "simple": "simple-icons"}
SCHEME_RE = re.compile(r"^(?P<set>[A-Za-z0-9][A-Za-z0-9-]*):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)$")
ALIAS_RE = re.compile(r"^(?P<set>mdi|sdi)[-/:](?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)$", re.I)
ICONIFY = "https://api.iconify.design/{set}/{name}.svg"
USER_AGENT = "vibepage-dashboard/1.0 (+iconify)"

_memo: dict[str, tuple[float, str | None]] = {}


# --------------------------------------------------------------------------- #
# icon names
# --------------------------------------------------------------------------- #

def split_scheme(icon: str | None) -> tuple[str, str] | None:
    """``'mdi:account-box'`` -> ``('mdi', 'account-box')``; else None."""
    text = (icon or "").strip()
    if not text:
        return None
    match = SCHEME_RE.match(text) or ALIAS_RE.match(text)
    if not match:
        return None
    name = match.group("name")
    if name.lower().endswith(".svg"):
        name = name[:-4]
    name = name.strip("_-")
    if not name:
        return None
    icon_set = match.group("set").lower()
    return SET_ALIASES.get(icon_set, icon_set), name.lower()


def _tint(markup: str) -> str | None:
    """Inline markup whose glyph follows ``currentColor``."""
    start = markup.find("<svg")
    if start < 0:
        return None
    markup = markup[start:].strip()
    open_end = markup.find(">")
    head = markup[:open_end] if open_end > 0 else markup
    if "fill=" not in head:
        markup = markup.replace("<svg", '<svg fill="currentColor"', 1)
    return markup


def _cache_file(cache_dir: str | None, icon_set: str, name: str) -> str | None:
    if not cache_dir:
        return None
    return os.path.join(cache_dir, icon_set, name + ".svg")


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _store(path: str | None, markup: str) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markup)
    except OSError:
        pass


def _download(icon_set: str, name: str) -> str:
    """Fetch one icon from Iconify (the only network call the server makes)."""
    request = Request(ICONIFY.format(set=quote(icon_set), name=quote(name)), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT) as response:
        return response.read(MAX_DOWNLOAD).decode("utf-8", "replace")


def iconify(icon: str | None, cache_dir: str | None = None, fetch=None, now=None) -> str | None:
    """Inline SVG for an ``mdi:``/``sdi:``/``set:name`` icon, or None."""
    scheme = split_scheme(icon)
    if scheme is None:
        return None
    icon_set, name = scheme
    key = f"{icon_set}/{name}"
    clock = time.time() if now is None else now
    remembered = _memo.get(key)
    if remembered and remembered[0] > clock:
        return remembered[1]

    path = _cache_file(cache_dir, icon_set, name)
    if path:
        cached = _read(path)
        if cached:
            markup = _tint(cached)
            if markup:
                _memo[key] = (clock + CACHE_OK, markup)
                return markup

    mark = time.time() if now is None else now
    try:
        markup = _tint((fetch or _download)(icon_set, name))
    except Exception:                                  # offline, typo, rate limit
        _memo[key] = (mark + RETRY_AFTER, None)
        return None
    if not markup:
        _memo[key] = (mark + RETRY_AFTER, None)
        return None
    _store(path, markup)
    _memo[key] = (mark + CACHE_OK, markup)
    return markup


def forget(icon: str | None = None) -> None:
    """Drop memoised icons (all of them, or the named one)."""
    if icon is None:
        _memo.clear()
        return
    scheme = split_scheme(icon)
    if scheme:
        _memo.pop(f"{scheme[0]}/{scheme[1]}", None)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def _inside(base: str, candidate: str) -> bool:
    base = os.path.normpath(base)
    candidate = os.path.normpath(candidate)
    return candidate == base or candidate.startswith(base + os.sep)


def resolve(service, icon_path: str | None, cache_dir: str | None = None, fetch=None) -> dict | None:
    """``{'svg': markup}``, ``{'url': src}`` or ``None`` for the placeholder."""
    icon = service.icon
    if icon:
        if icon.startswith(REMOTE):
            return {"url": icon}
        if icon_path:
            candidate = os.path.join(icon_path, icon)
            if _inside(icon_path, candidate) and os.path.isfile(candidate):
                if candidate.lower().endswith(".svg") and os.path.getsize(candidate) <= MAX_INLINE:
                    markup = _read(candidate)
                    if markup:
                        start = markup.lstrip("\ufeff").strip().find("<svg")
                        if start >= 0:
                            return {"svg": markup.lstrip("\ufeff").strip()[start:]}
                return {"url": "/icon/" + quote(icon.replace(os.sep, "/"))}
        markup = iconify(icon, cache_dir, fetch=fetch)
        if markup:
            return {"svg": markup}
    if service.icon_link:
        return {"url": service.icon_link}
    return None


def read_icon(icon_path: str | None, name: str) -> str | None:
    """Absolute path of an icon file, or None when it escapes the icon dir."""
    if not icon_path:
        return None
    candidate = os.path.join(icon_path, name)
    if not _inside(icon_path, candidate) or not os.path.isfile(candidate):
        return None
    return candidate
