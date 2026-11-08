"""homepage — a Dracula-purple start page for self-hosted services.

Every request re-reads the config files and re-renders the whole page, so the
browser never talks to the config: it only ever receives finished HTML. There
is no JSON endpoint, no client-side config fetch, no separate update call —
edit a file, press F5.
"""

from __future__ import annotations

import argparse
import os
import traceback

from flask import Flask, abort, make_response, render_template, request, send_file

from hp import icons as icon_lib
from hp.config import load
from hp.layout import build_page, host_is_local

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config", "homepage.toml")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


def config_path() -> str:
    return os.environ.get("HOMEPAGE_CONFIG") or DEFAULT_CONFIG


def _icon_resolver(site):
    icon_path = site.system.icon_path
    return lambda service: icon_lib.resolve(service, icon_path)


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
            page={"title": "Homepage"},
        )
    response = make_response(html)
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
    parser = argparse.ArgumentParser(description="Start the homepage dashboard")
    parser.add_argument("--config", default=None, help="путь к главному конфигу")
    parser.add_argument("--host", default=os.environ.get("HOMEPAGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HOMEPAGE_PORT", "8080")))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.config:
        os.environ["HOMEPAGE_CONFIG"] = os.path.abspath(args.config)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
