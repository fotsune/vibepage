"""Виджеты (portainer, adguard, npm, …) и ping: конфиг, кэш, /api/state, разметка."""

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from collections import namedtuple
from unittest import mock

import app as app_module

from hp import thermal, widgets
from hp.config import Widget, load
from hp.layout import build_page

CONFIG = """
[system]
title = "Widgets"
cols = 1
poll = 7
config_path = "."

[group:Infra]

[service:Box]
link = "https://192.0.2.10:9000"
desc = "Контейнеры"
group = "Infra"
widget = "box"
ping = "192.0.2.10:9000"

[widget:box]
type = "portainer"
url = "https://192.0.2.10:9000"
env = 3
key = "${TEST_PORTAINER_KEY}"
"""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(payload, seen=None):
    def opener(request, timeout=None, context=None):
        if seen is not None:
            seen.append(request.full_url if hasattr(request, "full_url") else request)
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    return opener


def fake_neighbour(answer, seen=None):
    """Соседний дашборд: на /api/widget?id=… отвечает тем, что дали (или чем скажет answer)."""
    def opener(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if seen is not None:
            seen.append(url)
        payload = answer(url) if callable(answer) else answer
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    return opener


class WidgetTestCase(unittest.TestCase):
    def setUp(self):
        widgets.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "homepage.toml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG)
        self.saved = os.environ.get("TEST_PORTAINER_KEY")
        os.environ["TEST_PORTAINER_KEY"] = "secret-from-env"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.saved is None:
            os.environ.pop("TEST_PORTAINER_KEY", None)
        else:
            os.environ["TEST_PORTAINER_KEY"] = self.saved


class ConfigTests(WidgetTestCase):
    def test_widget_section_is_parsed(self):
        site = load(self.path)
        widget = site.widgets["box"]
        self.assertEqual(widget.type, "portainer")
        self.assertEqual(widget.options["env"], 3)
        self.assertEqual(widget.options["key"], "secret-from-env")   # из окружения
        self.assertEqual(site.system.poll, 7)
        self.assertEqual(site.services[0].widgets, ["box"])
        self.assertEqual(site.services[0].ping, "192.0.2.10:9000")
        self.assertEqual([w for w in site.warnings if "box" in w], [])

    def test_unknown_type_and_missing_key_are_reported(self):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("""
[widget:odd]
type = "nope"

[service:Lost]
link = "http://x"
widget = "missing"
""")
        site = load(self.path)
        joined = " | ".join(site.warnings)
        self.assertIn("nope", joined)
        self.assertIn("missing", joined)

    def test_inline_comments_do_not_break_values(self):
        site = load(self.path)
        self.assertEqual(site.widgets["box"].options["url"], "https://192.0.2.10:9000")

    def test_color_starting_with_hash_survives(self):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("\n[group:Hash]\ncolor = #8be9fd\n")
        site = load(self.path)
        self.assertEqual(site.groups["hash"].color, "#8be9fd")


class PingTests(unittest.TestCase):
    def test_split_target(self):
        self.assertEqual(widgets.split_target("nas"), ("nas", 80))
        self.assertEqual(widgets.split_target("nas:81"), ("nas", 81))
        self.assertEqual(widgets.split_target("https://192.168.0.2:9000/x"), ("192.168.0.2", 9000))
        self.assertEqual(widgets.split_target("https://example.com"), ("example.com", 443))
        with self.assertRaises(ValueError):
            widgets.split_target("   ")

    def test_live_socket_is_ok(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        state = widgets.ping(f"127.0.0.1:{port}", timeout=2)
        self.assertEqual(state["state"], "ok")
        self.assertIsInstance(state["ms"], int)

    def test_closed_port_is_down(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        state = widgets.ping(f"127.0.0.1:{port}", timeout=1)
        self.assertEqual(state["state"], "down")
        self.assertIsNone(state["ms"])
        self.assertTrue(state["error"])


class PortainerTests(WidgetTestCase):
    def test_fetch_counts_states(self):
        payload = [
            {"State": "running"}, {"State": "running"},
            {"State": "exited"}, {"State": "created"}, {"State": "restarting"},
        ]
        seen = []
        with mock.patch.object(widgets, "urlopen", fake_urlopen(payload, seen)):
            site = load(self.path)
            state = widgets.run_widget(site.widgets["box"])
        self.assertTrue(state["ok"])
        values = {field["key"]: field["value"] for field in state["fields"]}
        self.assertEqual(values, {"running": 2, "stopped": 3, "total": 5})
        tones = {field["key"]: field["tone"] for field in state["fields"]}
        self.assertEqual(tones["running"], "good")
        self.assertEqual(tones["stopped"], "bad")
        self.assertEqual(tones["total"], "idle")
        self.assertEqual(seen, ["https://192.0.2.10:9000/api/endpoints/3/docker/containers/json?all=1"])

    def test_http_error_becomes_state_not_exception(self):
        from urllib.error import HTTPError

        def opener(request, timeout=None, context=None):
            raise HTTPError(request.full_url, 401, "Unauthorized", None, None)

        with mock.patch.object(widgets, "urlopen", opener):
            site = load(self.path)
            state = widgets.run_widget(site.widgets["box"])
        self.assertFalse(state["ok"])
        self.assertIn("401", state["error"])

    def test_snapshot_is_cached(self):
        calls = []
        original = widgets.REGISTRY["portainer"].fetch

        def counted(options):
            calls.append(options["env"])
            return original(options)

        with mock.patch.object(widgets, "urlopen", fake_urlopen([{"State": "running"}])), \
                mock.patch.object(widgets.REGISTRY["portainer"], "fetch", counted), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "ok", "ms": 5, "target": target}):
            site = load(self.path)
            first = widgets.snapshot(site, online=True)
            second = widgets.snapshot(site, online=True)
        self.assertEqual(len(calls), 1)                    # второй раз — из кэша
        self.assertEqual(first["cards"], second["cards"])
        self.assertEqual(first["poll"], 7)
        card = first["cards"]["Infra::Box"]
        self.assertEqual(card["widgets"][0]["fields"][0]["value"], 1)
        self.assertEqual(card["ping"]["state"], "ok")

    def test_offline_snapshot_never_calls_out(self):
        with mock.patch.object(widgets, "urlopen", side_effect=AssertionError("сеть не нужна")), \
                mock.patch.object(widgets, "ping", side_effect=AssertionError("сеть не нужна")):
            site = load(self.path)
            live = widgets.snapshot(site, online=False)
        self.assertEqual(live["cards"], {})


class PageAndRouteTests(WidgetTestCase):
    def setUp(self):
        super().setUp()
        os.environ["HOMEPAGE_CONFIG"] = self.path
        self.addCleanup(os.environ.pop, "HOMEPAGE_CONFIG", None)
        self.client = app_module.app.test_client()

    def test_card_shows_field_skeleton_and_ping_dot(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-key="Infra::Box"', body)
        self.assertIn('class="card-widget" data-widget="box"', body)
        self.assertIn('data-field="running"', body)
        self.assertIn("running", body)
        self.assertIn("card-ping state-unknown", body)
        self.assertIn('id="live-state"', body)

    def test_api_state_returns_json(self):
        payload = [{"State": "running"}, {"State": "exited"}]
        with mock.patch.object(widgets, "urlopen", fake_urlopen(payload)), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "ok", "ms": 3, "target": target}):
            response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        data = json.loads(response.get_data(as_text=True))
        fields = {field["key"]: field["value"]
                  for field in data["cards"]["Infra::Box"]["widgets"][0]["fields"]}
        self.assertEqual(fields, {"running": 1, "stopped": 1, "total": 2})

    def test_ping_state_never_exposes_the_target(self):
        with mock.patch.object(widgets, "urlopen", fake_urlopen([{"State": "running"}])), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "ok", "ms": 3, "target": target}):
            data = json.loads(self.client.get("/api/state").get_data(as_text=True))
        self.assertEqual(data["cards"]["Infra::Box"]["ping"], {"state": "ok", "ms": 3})
        self.assertNotIn("192.0.2.10", json.dumps(data))

    def test_ping_tooltip_is_human_and_has_no_address(self):
        with mock.patch.object(widgets, "urlopen", fake_urlopen([{"State": "running"}])), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "slow", "ms": 900, "target": target}):
            body = self.client.get("/").get_data(as_text=True)
        # адрес может быть виден в ссылке карточки — но не в точке пинга
        dot = body[body.index('class="card-ping'):][:200]
        self.assertIn('data-tip="Ping: Unknown"', dot)    # до первого ответа — честное «нет данных»
        self.assertNotIn("192.0.2.10", dot)

    def test_ping_tooltip_words(self):
        self.assertEqual(widgets.ping_title({"state": "ok", "ms": 1}), "Ping: Up · 1 ms")
        self.assertEqual(widgets.ping_title({"state": "slow", "ms": 900}), "Ping: Slow · 900 ms")
        self.assertEqual(widgets.ping_title({"state": "down", "ms": None}), "Ping: Down")
        self.assertEqual(widgets.ping_title(None), "Ping: Unknown")
        self.assertEqual(widgets.wire_ping({"state": "ok", "ms": 4, "target": "h:1", "error": "x"}),
                         {"state": "ok", "ms": 4})

    def test_api_state_survives_broken_config(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("это не конфиг\n")
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertIn("cards", json.loads(response.get_data(as_text=True)))

    def test_error_tooltip_rides_on_data_tip(self):
        from urllib.error import URLError

        def opener(request, timeout=None, context=None):
            raise URLError(ConnectionRefusedError(10061, "No connection could be made"))
        with mock.patch.object(widgets, "urlopen", opener):
            widgets.snapshot(load(self.path), online=True)
            body = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-phase="error"', body)
        self.assertIn('data-tip="Port closed."', body)   # причина по-английски, без адреса
        self.assertNotIn(' title="', body)               # родных подсказок нет: они медлят

    def test_tooltip_is_ours_and_quick(self):
        js = open("static/js/app.js", encoding="utf-8").read()
        css = open("static/css/style.css", encoding="utf-8").read()
        self.assertIn("var TIP_DELAY = 90;", js)
        self.assertIn('closest("[data-tip]")', js)
        self.assertIn(".tip {", css)
        self.assertIn(".tip.on", css)

    def test_page_state_is_prefilled_from_cache(self):
        payload = [{"State": "running"}]
        with mock.patch.object(widgets, "urlopen", fake_urlopen(payload)), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "slow", "ms": 900, "target": target}):
            widgets.snapshot(load(self.path), online=True)
            widgets.clear_cache()  # важно: проверяем именно чтение из кэша страницей
        with mock.patch.object(widgets, "urlopen", fake_urlopen(payload)), \
                mock.patch.object(widgets, "ping", lambda target, timeout=2.0: {"state": "slow", "ms": 900, "target": target}):
            widgets.snapshot(load(self.path), online=True)
            body = self.client.get("/").get_data(as_text=True)
        self.assertIn('"state": "slow"', body)
        self.assertNotIn('data-phase="error"', body)


# --------------------------------------------------------------------------- #
# типы виджетов и порядок полей
# --------------------------------------------------------------------------- #

def fake_router(routes, seen=None):
    """urlopen по подстроке URL: значение — dict/list (JSON) или готовый текст."""
    def opener(request, timeout=None, context=None):
        url = request.full_url
        if seen is not None:
            seen.append(request)
        for needle, payload in routes.items():
            if needle in url:
                raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
                return FakeResponse(raw.encode("utf-8") if isinstance(raw, str) else raw)
        raise AssertionError(f"не ждали запрос {url}")
    return opener


class FakeConn:
    """Сокет, который отдаёт заранее собранный пакет и запоминает, что послали."""

    def __init__(self, data: bytes):
        self.data = data
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout):
        pass

    def sendall(self, payload):
        self.sent += payload

    def recv(self, size):
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


def mc_frame(status: dict) -> bytes:
    text = json.dumps(status).encode("utf-8")
    packet = widgets._varint(0) + widgets._varint(len(text)) + text
    return widgets._varint(len(packet)) + packet


ADGUARD_STATS = {"num_dns_queries": 10, "num_blocked_filtering": 4, "num_replaced_safebrowsing": 1,
                 "num_replaced_safesearch": 1, "num_replaced_parental": 2, "avg_processing_time": 0.002}


class TypedWidgetTests(WidgetTestCase):
    def make_site(self, widget_body: str):
        path = os.path.join(self.tmp.name, "typed.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[system]\ntitle = "W"\n\n[group:Infra]\n\n'
                         '[service:Card]\nlink = "http://x"\ngroup = "Infra"\nwidget = "w"\n\n'
                         '[widget:w]\n' + widget_body)
        return load(path)

    def run_typed(self, widget_body: str, routes: dict, seen=None):
        site = self.make_site(widget_body)
        with mock.patch.object(widgets, "urlopen", fake_router(routes, seen)):
            return widgets.run_widget(site.widgets["w"])

    def keys(self, state):
        return [field["key"] for field in state["fields"]]

    def values(self, state):
        return {field["key"]: field["value"] for field in state["fields"]}

    # ── fields = [...] ───────────────────────────────────────────────

    def test_fields_order_follows_the_config(self):
        routes = {"/api/tokens": {"token": "tok"},
                  "/api/nginx/proxy-hosts": [{"enabled": 1}, {"enabled": 1}, {"enabled": 0}]}
        body = ('type = "npm"\nurl = "http://npm"\nusername = "u"\npassword = "p"\n'
                'fields = ["total", "disabled", "enabled"]\n')
        state = self.run_typed(body, routes)
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.keys(state), ["total", "disabled", "enabled"])
        self.assertEqual(self.values(state), {"total": 3, "disabled": 1, "enabled": 2})
        labels = {field["key"]: field["label"] for field in state["fields"]}
        self.assertEqual(labels["total"], "total")

    def test_fields_can_drop_and_reorder(self):
        body = ('type = "npm"\nurl = "http://npm"\nusername = "u"\npassword = "p"\n'
                'fields = ["enabled", "total"]\n')
        state = self.run_typed(body, {"/api/tokens": {"token": "tok"},
                                     "/api/nginx/proxy-hosts": [{"enabled": 1}]})
        self.assertEqual(self.keys(state), ["enabled", "total"])

    def test_fields_accept_a_comma_string(self):
        site = self.make_site('type = "npm"\nurl = "http://npm"\nusername = "u"\npassword = "p"\n'
                              'fields = "total, enabled"\n')
        self.assertEqual([field["key"] for field in widgets.fields_for(site.widgets["w"])],
                         ["total", "enabled"])

    def test_default_order_without_fields(self):
        site = self.make_site('type = "portainer"\nurl = "http://p"\nkey = "k"\n')
        self.assertEqual([field["key"] for field in widgets.fields_for(site.widgets["w"])],
                         ["running", "stopped", "total"])

    def test_three_fields_fit(self):
        body = ('type = "adguard"\nurl = "http://adguard"\nusername = "admin"\npassword = "pw"\n'
                'fields = ["queries", "blocked", "latency"]\n')
        stats = ADGUARD_STATS
        seen = []
        state = self.run_typed(body, {"/control/stats": stats}, seen)
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"queries": 10, "blocked": 4, "latency": "2 ms"})
        self.assertIn("Basic ", seen[0].headers.get("Authorization"))

    def test_four_fields_fit(self):
        body = ('type = "adguard"\nurl = "http://adguard"\nusername = "admin"\npassword = "pw"\n'
                'fields = ["queries", "blocked", "filtered", "latency"]\n')
        state = self.run_typed(body, {"/control/stats": ADGUARD_STATS})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(list(self.values(state)), ["queries", "blocked", "filtered", "latency"])

    def test_more_than_max_fields_takes_the_first_four(self):
        class Wide(widgets.Spec):
            TITLE = "Wide"
            FIELDS = (("a", "а"), ("b", "б"), ("c", "в"), ("d", "г"), ("e", "д"))

            def fetch(self, options):
                return {key: 1 for key, _label in self.FIELDS}

        widgets.REGISTRY["wide"] = Wide()
        self.addCleanup(widgets.REGISTRY.pop, "wide", None)
        site = self.make_site('type = "wide"\nfields = ["a", "b", "c", "d", "e"]\n')
        self.assertEqual(widgets.MAX_FIELDS, 4)
        self.assertEqual([field["key"] for field in widgets.fields_for(site.widgets["w"])],
                         ["a", "b", "c", "d"])
        self.assertEqual(site.warnings, [])

    def test_unknown_field_is_reported_and_skipped(self):
        site = self.make_site('type = "portainer"\nurl = "http://p"\nkey = "k"\n'
                              'fields = ["running", "nope"]\n')
        self.assertTrue(any("nope" in warning for warning in site.warnings), site.warnings)
        self.assertEqual([field["key"] for field in widgets.fields_for(site.widgets["w"])], ["running"])

    # ── типы ────────────────────────────────────────────────────────

    def test_npm_counts_hosts(self):
        state = self.run_typed('type = "npm"\nurl = "http://npm"\nusername = "u"\npassword = "p"\n',
                               {"/api/tokens": {"token": "tok"},
                                "/api/nginx/proxy-hosts": [{"enabled": 1}, {"enabled": 0}, {"enabled": 0}]})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"enabled": 1, "disabled": 2, "total": 3})

    def test_npm_without_token_is_a_widget_error(self):
        state = self.run_typed('type = "npm"\nurl = "http://npm"\nusername = "u"\npassword = "p"\n',
                               {"/api/tokens": {"error": "bad credentials"}})
        self.assertFalse(state["ok"])
        self.assertIn("no token", state["error"])

    def test_minecraft_reads_status_packet(self):
        conn = FakeConn(mc_frame({"players": {"online": 3, "max": 8},
                                  "version": {"name": "1.21.11"}}))
        with mock.patch.object(widgets.socket, "create_connection", lambda *a, **kw: conn):
            state = self.run_typed('type = "minecraft"\nurl = "udp://192.0.2.5:25565"\n'
                                   'fields = ["players", "version", "status"]\n', {})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"players": "3 / 8", "version": "1.21.11", "status": "Up"})
        self.assertEqual(self.keys(state), ["players", "version", "status"])
        tones = {field["key"]: field["tone"] for field in state["fields"]}
        self.assertEqual(tones["status"], "good")

    def test_minecraft_down_server_is_an_error_not_a_crash(self):
        def refuse(*args, **kwargs):
            raise OSError("connection refused")

        with mock.patch.object(widgets.socket, "create_connection", refuse):
            state = self.run_typed('type = "minecraft"\nurl = "192.0.2.5:25565"\n', {})
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "Connection refused.")   # причина без имени: оно и так на карточке

    def test_changedetection_counts_unread_diffs(self):
        watches = {"u1": {"last_changed": 5, "viewed": False},
                   "u2": {"last_changed": 0, "viewed": False},
                   "u3": {"last_changed": 9, "viewed": True}}
        state = self.run_typed('type = "changedetectionio"\nurl = "http://cd"\nkey = "k"\n',
                               {"/api/v1/watch": watches})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"diffsDetected": 1, "totalObserved": 3})

    def test_uptimekuma_counts_monitors_and_uptime(self):
        page = {"incidents": []}
        beats = {"heartbeatList": {"1": [{"status": 1}], "2": [{"status": 1}, {"status": 0}]},
                 "uptimeList": {"1_24": 1.0, "2_24": 0.5}}
        state = self.run_typed('type = "uptimekuma"\nurl = "http://kuma"\nslug = "s"\n'
                               'fields = ["up", "down", "incident"]\n',
                               {"/api/status-page/heartbeat/": beats, "/api/status-page/": page})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state)["up"], 1)
        self.assertEqual(self.values(state)["down"], 1)
        self.assertIsNone(self.values(state)["incident"])

    def test_uptimekuma_incident_age_in_hours(self):
        from datetime import datetime, timedelta, timezone
        started = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        page = {"incidents": [{"createdDate": started}]}
        beats = {"heartbeatList": {"1": [{"status": 0}]}, "uptimeList": {"1_24": 0.9}}
        state = self.run_typed('type = "uptimekuma"\nurl = "http://kuma"\nslug = "s"\n'
                               'fields = ["uptime", "incident"]\n',
                               {"/api/status-page/heartbeat/": beats, "/api/status-page/": page})
        self.assertEqual(self.values(state)["incident"], "5 h")
        self.assertEqual(self.values(state)["uptime"], "90.0%")

    def test_yourspotify_sums_the_interval(self):
        state = self.run_typed('type = "yourspotify"\nurl = "https://spoti"\nkey = "k"\ninterval = "month"\n',
                               {"songs_per": [{"count": 12}],
                                "time_per": [{"count": 7_200_000}],
                                "different_artists_per": [{"artists": [{}, {}, {}]}]})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"songs": 12, "time": "2 h", "artists": 3})

    def test_watchtower_parses_prometheus_text(self):
        metrics = "# HELP x\nwatchtower_containers_scanned 7\nwatchtower_containers_updated 2\n" \
                  "watchtower_containers_failed 1\nwatchtower_scans_total 3\n"
        state = self.run_typed('type = "watchtower"\nurl = "http://wt"\nkey = "k"\n', {"/v1/metrics": metrics})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"containers_scanned": 7, "containers_updated": 2,
                                              "containers_failed": 1})
        tones = {field["key"]: field["tone"] for field in state["fields"]}
        self.assertEqual(tones["containers_failed"], "bad")

    def test_watchtower_without_metrics_explains_itself(self):
        state = self.run_typed('type = "watchtower"\nurl = "http://wt"\nkey = "k"\n',
                               {"/v1/metrics": "go_goroutines 4\n"})
        self.assertFalse(state["ok"])
        self.assertIn("watchtower_", state["error"])


class FailureShapeTests(WidgetTestCase):
    """Ошибка виджета: кресты вместо цифр, причина без адреса и кодов ОС."""

    def _refused(self):
        from urllib.error import URLError

        def opener(request, timeout=None, context=None):
            raise URLError(ConnectionRefusedError(10061, "No connection could be made"))
        return opener

    def test_reason_is_short_and_hides_the_address(self):
        with mock.patch.object(widgets, "urlopen", self._refused()):
            site = load(self.path)
            state = widgets.run_widget(site.widgets["box"])
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "Port closed.")   # одна фраза по-английски
        self.assertNotIn("192.0.2.10", state["error"])
        self.assertNotIn("9000", state["error"])
        self.assertNotIn("WinError", state["error"])
        self.assertLess(len(state["error"]), 40)

    def test_failure_replaces_numbers_with_crosses(self):
        good = [{"State": "running"}, {"State": "exited"}]
        site = load(self.path)
        with mock.patch.object(widgets, "urlopen", fake_urlopen(good)):
            self.assertTrue(widgets.run_widget(site.widgets["box"])["ok"])
        with mock.patch.object(widgets, "urlopen", self._refused()):
            state = widgets.run_widget(site.widgets["box"])
        self.assertEqual([field["value"] for field in state["fields"]],
                         [widgets.CROSS] * 3)
        self.assertEqual({field["tone"] for field in state["fields"]}, {"down"})
        self.assertEqual([field["label"] for field in state["fields"]],
                         ["running", "stopped", "total"])

    def test_auth_failure_says_so(self):
        from urllib.error import HTTPError

        def opener(request, timeout=None, context=None):
            raise HTTPError(request.full_url, 401, "Unauthorized", None, None)
        with mock.patch.object(widgets, "urlopen", opener):
            site = load(self.path)
            state = widgets.run_widget(site.widgets["box"])
        self.assertEqual(state["error"], "Access denied: HTTP 401.")


# --------------------------------------------------------------------------- #
# несколько виджетов, карточка без ссылки, cpu/ram/disk и имя виджета
# --------------------------------------------------------------------------- #

MULTI_CONFIG = r"""
[system]
title = "Multi"
cols = 1

[group:Host]

[service:Box]
group = "Host"
widget = "cpu, ram, disk"

[service:NoLink]
desc = "без ссылок"
group = "Host"

[widget:cpu]
type = cpu
tempmin = 20
tempmax = 80
units = imperial
fields = "cpu, cputemp, uptime"

[widget:ram]
type = ram
ramUnits = bbytes
fields = "free, consumed, total"

[widget:disk]
type = disk
name = "C:, D:"
disk = "C:\, D:\"
diskUnits = bbytes
fields = "free, consumed, total"
"""


def _disk_usage(total=1000, used=400, free=600, percent=40.0):
    return type("Usage", (), {"total": total, "used": used, "free": free, "percent": percent})()


def _memory(total=16 * 1024 ** 3, available=6 * 1024 ** 3, percent=61.5):
    return type("Memory", (), {"total": total, "available": available, "percent": percent})()


def _cpu_times_source(busy=0.42, step=10.0):
    """Фейковый psutil: cpu_times() добавляет step секунд, из них busy — работе."""
    tick = {"user": 0.0, "system": 0.0, "idle": 0.0, "iowait": 0.0}
    times = namedtuple("cpu_times_t", "user system idle iowait")

    def cpu_times():
        tick["user"] += step * busy
        tick["idle"] += step * (1 - busy)
        return times(**tick)

    return type("FakePsutil", (), {"cpu_times": staticmethod(cpu_times)})()


class MultiWidgetTestCase(unittest.TestCase):
    def setUp(self):
        widgets.clear_cache()
        widgets.forget_cpu_load()             # у каждого теста — чистые замеры CPU
        thermal.forget()                      # датчик температуры тоже кэширует
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "multi.toml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(MULTI_CONFIG)
        self.site = load(self.path)
        os.environ["HOMEPAGE_CONFIG"] = self.path
        self.addCleanup(os.environ.pop, "HOMEPAGE_CONFIG", None)
        self.client = app_module.app.test_client()

    def psutil_patch(self, **values):
        fake = {
            "cpu_times": _cpu_times_source().cpu_times,   # 42 % работы между двумя замерами
            "virtual_memory": lambda: _memory(),
            "boot_time": lambda: 1000.0,
            "sensors_temperatures": lambda: {
                "coretemp": [type("Temp", (), {"label": "Package id 0", "current": 65.0})()]},
            "disk_usage": lambda path: _disk_usage(),
        }
        fake.update(values)
        return mock.patch.multiple(widgets.psutil, create=True, **fake)

    # ── карточка без ссылки ────────────────────────────────────────

    def test_service_without_link_or_alt_is_allowed(self):
        self.assertEqual([w for w in self.site.warnings if "NoLink" in w], [])
        self.assertEqual(self.site.services[1].widgets, [])

    def test_card_without_link_is_rendered_but_not_clickable(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-key="Host::NoLink"', body)
        start = body.rindex("<article", 0, body.index('data-key="Host::NoLink"'))
        card = body[start:start + 900]
        self.assertIn("card-static", card)
        self.assertIn('<a class="card-link">', card)       # <a> без href: не кликается
        self.assertNotIn("href", card)

    # ── несколько виджетов на карточке ────────────────────────────

    def test_widget_list_is_split_on_commas(self):
        self.assertEqual(self.site.services[0].widgets, ["cpu", "ram", "disk"])

    def test_card_renders_widgets_in_order(self):
        with self.psutil_patch():
            body = self.client.get("/").get_data(as_text=True)
        card = body[body.index('data-key="Host::Box"'):][:4000]
        order = [card.index('data-widget="%s"' % name) for name in ("cpu", "ram", "disk")]
        self.assertEqual(order, sorted(order))               # каждый следующий — ниже

    def test_api_state_lists_every_widget_of_the_card(self):
        with self.psutil_patch():
            data = json.loads(self.client.get("/api/state").get_data(as_text=True))
        live = data["cards"]["Host::Box"]["widgets"]
        self.assertEqual([item["name"] for item in live], ["cpu", "ram", "disk"])
        self.assertEqual(live[0]["bar"]["tone"], "warn")     # полосочка едет вместе с данными

    # ── sysinfo ────────────────────────────────────────────────────

    def test_cpu_reads_load_temperature_and_uptime(self):
        with self.psutil_patch():
            state = widgets.run_widget(self.site.widgets["cpu"])
        self.assertTrue(state["ok"], state.get("error"))
        values = {field["key"]: field["value"] for field in state["fields"]}
        self.assertEqual([field["key"] for field in state["fields"]], ["cpu", "cputemp", "uptime"])
        self.assertEqual(values["cpu"], "42%")
        self.assertEqual(values["cputemp"], "149°F")          # units = imperial
        self.assertRegex(values["uptime"], r"^\d+[dhm] ")

    def _in_another_thread(self, call):
        """Значение из свежего потока: psutil.cpu_percent там отвечает 0 %."""
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("value", call()))
        thread.start()
        thread.join()
        return box["value"]

    def test_cpu_load_does_not_depend_on_the_calling_thread(self):
        fake = _cpu_times_source(busy=0.1)
        widgets.forget_cpu_load()
        with mock.patch.object(widgets, "CPU_MIN_GAP", 0.0):
            first = self._in_another_thread(lambda: widgets._cpu_load(fake, prime=0))
            second = self._in_another_thread(lambda: widgets._cpu_load(fake, prime=0))
            third = self._in_another_thread(lambda: widgets._cpu_load(fake, prime=0))
        self.assertEqual(first, 0.0)                    # один замер — ещё не о чем говорить
        for value in (second, third):
            self.assertAlmostEqual(value, 10.0, delta=0.5)   # не 0 и не 100

    def test_cpu_load_is_an_average_not_a_glimpse(self):
        widgets.forget_cpu_load()
        self.assertAlmostEqual(widgets._cpu_load(_cpu_times_source(busy=0.13), prime=0.01), 13.0, delta=0.5)

    def test_cpu_load_is_not_sampled_twice_in_a_row(self):
        fake = _cpu_times_source(busy=0.2)
        widgets.forget_cpu_load()
        widgets._cpu_load(fake, prime=0.01)              # первый замер (13 %…): копим историю
        value = widgets._cpu_load(fake, prime=0)
        self.assertEqual(widgets._cpu_load(fake, prime=0), value)   # второй запрос — из тех же замеров

    def test_api_widget_hands_one_widget_to_a_neighbour(self):
        with self.psutil_patch():
            data = json.loads(self.client.get("/api/widget?id=disk").get_data(as_text=True))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(data["id"], "disk")
        self.assertEqual(data["values"]["total"], 2000)     # два пути в конфиге, по 1000 каждый
        self.assertEqual(data["name"], "disk")             # и то же самое, что видит карточка
        self.assertEqual([field["key"] for field in data["fields"]], ["free", "consumed", "total"])

    def test_api_widget_explains_a_missing_id(self):
        with self.psutil_patch():
            missing = self.client.get("/api/widget?id=nope")
        self.assertEqual(missing.status_code, 404)
        self.assertFalse(json.loads(missing.get_data(as_text=True))["ok"])

    def test_cpu_metric_is_the_default(self):
        options = dict(self.site.widgets["cpu"].options)
        options.pop("units")
        self.assertEqual(widgets.Cpu().show("cputemp", 65.0, options), "65°C")

    def test_cpu_bar_is_the_load_and_its_tone_follows_temperature(self):
        with self.psutil_patch():
            state = widgets.run_widget(self.site.widgets["cpu"])
        self.assertAlmostEqual(state["bar"]["value"], 0.42, places=2)   # 42% загрузки
        self.assertEqual(state["bar"]["tone"], "warn")                  # 65 из 20..80

    def test_cpu_bar_without_a_sensor_follows_the_load(self):
        with self.psutil_patch(sensors_temperatures=lambda: {}):
            state = widgets.run_widget(self.site.widgets["cpu"])
        self.assertIsNone(state["fields"][1]["value"])                  # датчика нет — прочерк
        self.assertAlmostEqual(state["bar"]["value"], 0.42, places=2)
        self.assertEqual(state["bar"]["tone"], "good")

    def test_missing_sensor_explains_itself_in_the_tooltip(self):
        with self.psutil_patch(sensors_temperatures=lambda: {}):
            state = widgets.run_widget(self.site.widgets["cpu"])
            data = json.loads(self.client.get("/api/state").get_data(as_text=True))
        self.assertEqual(state["fields"][1]["tip"], widgets.NO_TEMPERATURE)
        live = data["cards"]["Host::Box"]["widgets"][0]
        self.assertEqual(live["fields"][1]["tip"], widgets.NO_TEMPERATURE)
        with open("static/js/app.js", encoding="utf-8") as handle:   # клиент рисует её сам
            self.assertIn("field.tip", handle.read())

    def test_old_sysinfo_type_still_works(self):
        self.assertIsInstance(widgets.type_spec("sysinfo"), widgets.Cpu)

    def test_ram_shows_bytes_and_how_full_it_is(self):
        with self.psutil_patch():
            state = widgets.run_widget(self.site.widgets["ram"])
        self.assertTrue(state["ok"], state.get("error"))
        values = {field["key"]: field["value"] for field in state["fields"]}
        self.assertEqual(values["free"], "6.0 GiB")            # ramUnits = bbytes
        self.assertEqual(values["consumed"], "10.0 GiB")
        self.assertEqual(values["total"], "16.0 GiB")
        self.assertAlmostEqual(state["bar"]["value"], 0.625, places=2)

    def test_ram_units_default_to_decimal(self):
        self.assertEqual(widgets.Ram().show("total", 1_500_000, {}), "1.5 MB")

    # ── disk ───────────────────────────────────────────────────────

    def test_disk_sums_every_listed_path(self):
        seen = []

        def usage(path):
            seen.append(path)
            return _disk_usage(total=1000, used=900, free=100, percent=90.0)
        with self.psutil_patch(disk_usage=usage):
            state = widgets.run_widget(self.site.widgets["disk"])
        self.assertEqual(seen, ["C:\\", "D:\\"])
        values = {field["key"]: field["value"] for field in state["fields"]}
        self.assertEqual(values["total"], "2.0 KiB")            # bbytes = двоичные единицы
        self.assertEqual(values["consumed"], "1.8 KiB")
        self.assertEqual(values["free"], "200 B")

    def test_disk_bar_shows_how_full_it_is(self):
        with self.psutil_patch(disk_usage=lambda path: _disk_usage(total=1000, used=920, free=80, percent=92.0)):
            state = widgets.run_widget(self.site.widgets["disk"])
        self.assertAlmostEqual(state["bar"]["value"], 0.92, places=2)
        self.assertEqual(state["bar"]["tone"], "bad")

    def test_unreadable_disk_is_a_short_phrase(self):
        def usage(path):
            raise OSError(2, "nope")
        with self.psutil_patch(disk_usage=usage):
            state = widgets.run_widget(self.site.widgets["disk"])
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "Cannot read C:\\.")
        self.assertIsNone(state["bar"])

    def test_disk_units_default_to_decimal(self):
        spec = widgets.Disk()
        self.assertEqual(spec.show("total", 1_500_000, {}), "1.5 MB")
        self.assertEqual(spec.show("total", 1_500_000, {"diskunits": "bbytes"}), "1.4 MiB")

    def test_disk_paths_fold_into_a_short_label(self):
        fold = widgets.short_disk
        self.assertEqual(fold("C:" + chr(92) + "Program Files"), "C:/Program Files")
        self.assertEqual(fold(chr(92)), "/")        # корень текущего диска — тоже «/»
        self.assertEqual(fold("/"), "/")
        self.assertEqual(fold("/mnt/data/"), "/mnt/data")
        self.assertEqual(fold("/var/lib/docker"), "…/lib/docker")   # хвост, не весь путь
        self.assertEqual(fold("D:" + chr(92)), "D:")

    def test_disk_without_name_is_captioned_by_its_paths(self):
        for options, caption in (("/", "/"), (["/", "/data"], "/ · /data"),
                                 (["/a", "/b", "/c"], "/a · /b +1"), (None, None)):
            widget = type("W", (), {"type": "disk", "options": {"disk": options}})()
            self.assertEqual(widgets.caption_for(widget), caption, options)
        named = type("W", (), {"type": "disk", "options": {"disk": "/", "name": "Диск"}})()
        self.assertEqual(widgets.caption_for(named), "Диск")

    def test_remote_disk_takes_caption_from_neighbour(self):
        far = type("W", (), {"type": "disk", "options": {"external": "disk", "host": "10.0.0.2:24556"}})()
        self.assertIsNone(widgets.caption_for(far))      # пути там свои — подпись придёт с данными

    def test_disk_name_is_a_compact_caption(self):
        with self.psutil_patch():
            state = widgets.run_widget(self.site.widgets["disk"])
            body = self.client.get("/").get_data(as_text=True)
        self.assertEqual(state["caption"], "C:, D:")
        self.assertIn('class="widget-caption"', body)
        self.assertIn(">C:, D:<", body)

    # ── psutil нет вовсе (образ без нативного модуля) ──────────────

    def test_missing_psutil_is_a_widget_error_not_a_crash(self):
        with mock.patch.object(widgets, "psutil", None):
            cpu_state = widgets.run_widget(self.site.widgets["cpu"])
            ram_state = widgets.run_widget(self.site.widgets["ram"])
            disk_state = widgets.run_widget(self.site.widgets["disk"])
            body = self.client.get("/").get_data(as_text=True)
        for state in (cpu_state, ram_state, disk_state):
            self.assertFalse(state["ok"])
        self.assertEqual(cpu_state["error"], "Not installed: psutil.")
        self.assertIn('data-key="Host::Box"', body)      # дашборд живёт дальше

    # ── fields ─────────────────────────────────────────────────────

    def test_fields_string_and_old_list_are_the_same(self):
        string_widget = type("W", (), {"options": {"fields": "a, b"}})()
        list_widget = type("W", (), {"options": {"fields": ["a", "b"]}})()
        self.assertEqual(widgets.raw_fields(string_widget), ["a", "b"])
        self.assertEqual(widgets.raw_fields(list_widget), ["a", "b"])


class RemoteWidgetTestCase(unittest.TestCase):
    """Виджет с соседнего дашборда: host = "192.168.0.112:24556", external = "disk"."""

    def setUp(self):
        widgets.clear_cache()

    def _widget(self, kind="disk", **options):
        options.setdefault("host", "192.168.0.112:24556")
        options.setdefault("external", "disk")
        return Widget(name="far", type=kind, options=options)

    def _answer(self, values=None, **extra):
        payload = {
            "ok": True, "id": "disk", "type": "disk", "title": "Disk", "caption": "/",
            "values": {"free": 400, "consumed": 600, "total": 1000} if values is None else values,
            "fields": [{"key": "free", "label": "free", "value": "1 GB", "tone": "idle", "tip": None}],
            "bar": {"value": 0.6, "tone": "good"}, "at": 1.0,
        }
        payload.update(extra)
        return payload

    def _run(self, widget, answer, seen=None):
        with mock.patch.object(widgets, "urlopen", fake_neighbour(answer, seen)):
            return widgets.run_widget(widget)

    # ── данные оттуда, оформление отсюда ───────────────────────────

    def test_numbers_come_from_the_neighbour_and_units_stay_local(self):
        state = self._run(self._widget(name="NAS"), self._answer())
        self.assertTrue(state["ok"], state.get("error"))
        values = {field["key"]: field["value"] for field in state["fields"]}
        self.assertEqual(values["free"], "400 B")       # сосед отдал байты — формат наш
        self.assertEqual(values["consumed"], "600 B")
        self.assertEqual(values["total"], "1.0 KB")
        self.assertEqual(state["caption"], "NAS")       # name с этой стороны главнее
        self.assertAlmostEqual(state["bar"]["value"], 0.6, places=2)

    def test_local_fields_pick_what_to_show(self):
        state = self._run(self._widget(fields="consumed"), self._answer())
        self.assertEqual([field["key"] for field in state["fields"]], ["consumed"])

    def test_neighbour_is_asked_by_external_then_by_type(self):
        seen = []

        def answer(url):
            return self._answer() if url.endswith("id=cpu") else {"ok": False, "error": "No widget there"}

        widget = self._widget(kind="cpu", external="missing_one")
        state = self._run(widget, answer, seen)
        self.assertTrue(state["ok"], state.get("error"))
        self.assertTrue(seen[0].endswith("id=missing_one"))
        self.assertTrue(seen[1].endswith("id=cpu"))     # второй заход — по типу

    def test_neighbour_address_gets_a_scheme(self):
        self.assertEqual(widgets.remote_host({"host": "192.168.0.112:24556"}), "http://192.168.0.112:24556")
        self.assertEqual(widgets.remote_host({"host": "http://box:8080/"}), "http://box:8080")
        self.assertEqual(widgets.remote_host({}), "")

    def test_widget_missing_on_the_neighbour_shows_crosses(self):
        state = self._run(self._widget(), {"ok": False, "error": "No widget 'disk' on this dashboard"})
        self.assertFalse(state["ok"])
        self.assertEqual([field["value"] for field in state["fields"]], [widgets.CROSS] * 3)
        self.assertIn("No widget", state["error"])

    def test_neighbour_down_is_one_short_phrase(self):
        from urllib.error import URLError

        def opener(request, timeout=None, context=None):
            raise URLError(ConnectionRefusedError(10061, "No connection could be made"))
        with mock.patch.object(widgets, "urlopen", opener):
            state = widgets.run_widget(self._widget())
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "Port closed.")
        self.assertNotIn("192.168.0.112", state["error"])

    # ── конфиг ─────────────────────────────────────────────────────

    def test_remote_widget_needs_no_disk_path(self):
        self.assertEqual(widgets.validate(self._widget()), [])

    def test_remote_widget_without_type_needs_external(self):
        bare = Widget(name="far", type="", options={"host": "192.168.0.112:24556", "external": "disk"})
        self.assertEqual(widgets.validate(bare), [])
        nameless = Widget(name="far", type="", options={"host": "192.168.0.112:24556"})
        self.assertIn("external", widgets.validate(nameless)[0])

    def test_remote_widget_without_type_takes_the_type_from_the_answer(self):
        bare = Widget(name="far", type="", options={"host": "192.168.0.112:24556", "external": "disk"})
        state = self._run(bare, self._answer())
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(state["title"], "Disk")
        self.assertEqual([field["key"] for field in state["fields"]], ["free", "consumed", "total"])

    def test_find_widget_by_name_card_or_type(self):
        site = type("Site", (), {"widgets": {"nas": Widget(name="nas", type="disk", options={})}})()
        self.assertEqual(widgets.find_widget(site, "nas").name, "nas")
        self.assertEqual(widgets.find_widget(site, "Box.nas").name, "nas")
        self.assertEqual(widgets.find_widget(site, "disk").name, "nas")   # по типу
        self.assertIsNone(widgets.find_widget(site, "ram"))
