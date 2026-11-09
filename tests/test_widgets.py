"""Виджеты (Portainer) и ping: конфиг, кэш, /api/state, разметка карточки."""

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
