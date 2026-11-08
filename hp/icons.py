"""Turn a service's ``icon`` / ``icon_link`` into something to render.

A local SVG is inlined, so it inherits the accent colour of its card and can
be tinted on hover like the rest of the interface. Raster files and remote
pictures are served as plain ``<img>`` sources; anything missing falls back to
the letter placeholder drawn by CSS.
"""

from __future__ import annotations

import os
from urllib.parse import quote

REMOTE = ("http://", "https://", "//", "data:")
MAX_INLINE = 131072  # 128 KiB, plenty for an icon


def _inside(base: str, candidate: str) -> bool:
    base = os.path.normpath(base)
    candidate = os.path.normpath(candidate)
    return candidate == base or candidate.startswith(base + os.sep)


def resolve(service, icon_path: str | None) -> dict | None:
    """``{'svg': markup}``, ``{'url': src}`` or ``None`` for the placeholder."""
    icon = service.icon
    if icon:
        if icon.startswith(REMOTE):
            return {"url": icon}
        if icon_path:
            candidate = os.path.join(icon_path, icon)
            if _inside(icon_path, candidate) and os.path.isfile(candidate):
                if candidate.lower().endswith(".svg") and os.path.getsize(candidate) <= MAX_INLINE:
                    try:
                        with open(candidate, encoding="utf-8") as handle:
                            markup = handle.read()
                    except OSError:
                        return {"url": "/icon/" + quote(icon.replace(os.sep, "/"))}
                    markup = markup.lstrip("\ufeff").strip()
                    start = markup.find("<svg")
                    if start >= 0:
                        return {"svg": markup[start:]}
                return {"url": "/icon/" + quote(icon.replace(os.sep, "/"))}
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
