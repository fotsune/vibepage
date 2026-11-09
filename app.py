"""vibepage — a Dracula-purple start page for self-hosted services.

Every request re-reads the config files and re-renders the whole page, so the
browser never talks to the config: it only ever receives finished HTML. There
is no JSON endpoint, no client-side config fetch, no separate update call —
edit a file, press F5.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from flask import Flask, abort, make_response, render_template, request, send_file

from vp import icons as icon_lib
from vp import widgets as widget_lib
from vp.config import load
from vp.layout import build_page, host_is_local

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config", "vibepage.toml")
ENV_FILE = os.path.join(HERE, "config", ".env")


def load_env_file(path: str = ENV_FILE) -> None:
    """Секреты для ${VAR} из config/.env — файла, которого нет в git.

    Строки вида KEY=VALUE; пустые и начинающиеся с # пропускаются, кавычки
    по краям значения снимаются. Уже заданное окружение главнее файла.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


load_env_file()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


def config_path() -> str:
    return os.environ.get("VIBEPAGE_CONFIG") or DEFAULT_CONFIG


def _icon_resolver(site):
    icon_path = site.system.icon_path
    cache = os.path.join(site.config_dir, ".icon-cache") if site.config_dir else None
    return lambda service: icon_lib.resolve(service, icon_path, cache)


@app.get("/")
def index():
    path = config_path()
    try:
        site = load(path)
        page = build_page(site, is_local=host_is_local(request.host), resolve_icon=_icon_resolver(site))
        html = render_template("index.html", page=page)
    except Exception as exc:  # a broken config should explain itself, not 500
        html = render_template(
            "error.html",
            title="Конфиг не загрузился",
            message=str(exc),
            detail=traceback.format_exc(limit=3),
            path=path,
            page={"title": "Vibepage"},
        )
    response = make_response(html)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/api/state")
def api_state():
    """Состояние карточек для поллинга: ping и виджеты, из кэша где можно."""
    path = config_path()
    try:
        site = load(path)
    except Exception as exc:  # noqa: BLE001 — клиент получит ошибку, а не 500
        return json_response({"cards": {}, "poll": 10, "error": str(exc)})
    online = request.args.get("cached") not in ("1", "true", "yes")
    return json_response(widget_lib.snapshot(site, online=online))


@app.get("/api/widget")
def api_widget():
    """Один виджет по имени — для соседнего дашборда (``host`` + ``external``)."""
    ident = (request.args.get("id") or request.args.get("name") or "").strip()
    try:
        site = load(config_path())
    except Exception as exc:  # noqa: BLE001 — сосед получит причину, а не 500
        return json_response({"ok": False, "id": ident, "error": str(exc)})
    widget = widget_lib.find_widget(site, ident)
    if widget is None:
        return json_response({"ok": False, "id": ident,
                               "error": f"No widget {ident!r} on this dashboard"}), 404
    return json_response(widget_lib.remote_payload(widget))


def json_response(payload: dict):
    response = make_response(json.dumps(payload, ensure_ascii=False))
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/icon/<path:name>")
def icon(name: str):
    found = icon_lib.read_icon(load(config_path()).system.icon_path, name)
    if found is None:
        abort(404)
    return send_file(found, max_age=3600, conditional=True)


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the vibepage dashboard")
    parser.add_argument("--config", default=None, help="путь к главному конфигу")
    parser.add_argument("--host", default=os.environ.get("VIBEPAGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("VIBEPAGE_PORT", "8080")))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.config:
        os.environ["VIBEPAGE_CONFIG"] = os.path.abspath(args.config)
    print(
        "vibepage: %s · код %s · конфиг %s · secrets %s"
        % (
            sys.executable,
            HERE,
            os.environ.get("VIBEPAGE_CONFIG") or DEFAULT_CONFIG,
            "есть" if os.path.exists(ENV_FILE) else "нет",
        ),
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
