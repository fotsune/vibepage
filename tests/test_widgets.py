"""Виджеты (portainer, adguard, npm, …) и ping: конфиг, кэш, /api/state, разметка."""

import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

import app as app_module

from hp import widgets
from hp.config import load
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
        self.assertEqual(site.services[0].widget, "box")
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
        self.assertEqual(card["widget"]["fields"][0]["value"], 1)
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
        self.assertIn("запущено", body)
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
        fields = {field["key"]: field["value"] for field in data["cards"]["Infra::Box"]["widget"]["fields"]}
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
        self.assertIn('title="Ping: Unknown"', dot)       # до первого ответа — честное «нет данных»
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
        self.assertNotIn("data-stale", body)


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
        self.assertEqual(labels["total"], "всего")

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

    def test_four_fields_fit(self):
        body = ('type = "adguard"\nurl = "http://adguard"\nusername = "admin"\npassword = "pw"\n'
                'fields = ["queries", "blocked", "filtered", "latency"]\n')
        stats = {"num_dns_queries": 10, "num_blocked_filtering": 4, "num_replaced_safebrowsing": 1,
                 "num_replaced_safesearch": 1, "num_replaced_parental": 2, "avg_processing_time": 0.002}
        seen = []
        state = self.run_typed(body, {"/control/stats": stats}, seen)
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"queries": 10, "blocked": 4, "filtered": 4, "latency": "2 мс"})
        self.assertIn("Basic ", seen[0].headers.get("Authorization"))

    def test_more_than_max_fields_is_truncated_and_reported(self):
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
        self.assertTrue(any("полей больше 4" in warning for warning in site.warnings), site.warnings)

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
        self.assertIn("токен", state["error"])

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
        self.assertIn("Minecraft", state["error"])

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
                               'fields = ["up", "down", "uptime", "incident"]\n',
                               {"/api/status-page/heartbeat/": beats, "/api/status-page/": page})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state)["up"], 1)
        self.assertEqual(self.values(state)["down"], 1)
        self.assertEqual(self.values(state)["uptime"], "75.0%")
        self.assertIsNone(self.values(state)["incident"])

    def test_uptimekuma_incident_age_in_hours(self):
        from datetime import datetime, timedelta, timezone
        started = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        page = {"incidents": [{"createdDate": started}]}
        beats = {"heartbeatList": {"1": [{"status": 0}]}, "uptimeList": {"1_24": 0.9}}
        state = self.run_typed('type = "uptimekuma"\nurl = "http://kuma"\nslug = "s"\n'
                               'fields = ["uptime", "incident"]\n',
                               {"/api/status-page/heartbeat/": beats, "/api/status-page/": page})
        self.assertEqual(self.values(state)["incident"], "5 ч")
        self.assertEqual(self.values(state)["uptime"], "90.0%")

    def test_yourspotify_sums_the_interval(self):
        state = self.run_typed('type = "yourspotify"\nurl = "https://spoti"\nkey = "k"\ninterval = "month"\n',
                               {"songs_per": [{"count": 12}],
                                "time_per": [{"count": 7_200_000}],
                                "different_artists_per": [{"artists": [{}, {}, {}]}]})
        self.assertTrue(state["ok"], state.get("error"))
        self.assertEqual(self.values(state), {"songs": 12, "time": "2 ч", "artists": 3})

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
