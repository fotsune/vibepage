"""Тесты акцентов групп, icon_color и переключателя «основная / alt»."""

import os
import tempfile
import unittest

import app as app_module

from vp.colors import ACCENTS, auto
from vp.config import Group, Service, Site, System, group_key, load
from vp.layout import build_page

CONFIG = """
[system]
title = "Colors"
cols = 2
config_path = "."

[group:Media]
color = green

[group:Infra]
color = "#8be9fd"

[group:Auto]

[service:Jellyfin]
link = "https://jellyfin.example.com"
alt = "http://10.0.0.2:8096"
icon_color = pink
group = "Media"

[service:Solo]
link = "https://solo.example.com"
group = "Infra"
"""


def load_config(text=CONFIG):
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "vibepage.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    site = load(path)
    return site, tmp


def panel_of(page, name):
    for column in page["columns"]:
        for panel in column:
            if panel["name"] == name:
                return panel
    raise AssertionError(f"нет группы {name}")


class ConfigColorTests(unittest.TestCase):
    def test_group_colours_are_parsed(self):
        site, tmp = load_config()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(site.groups[group_key("Media")].color, "#50fa7b")
        self.assertEqual(site.groups[group_key("Infra")].color, "#8be9fd")
        self.assertIsNone(site.groups[group_key("Auto")].color)

    def test_service_icon_colour_is_parsed(self):
        site, tmp = load_config()
        self.addCleanup(tmp.cleanup)
        found = {service.name: service.icon_color for service in site.services}
        self.assertEqual(found["Jellyfin"], "#ff79c6")
        self.assertIsNone(found["Solo"])

    def test_bad_colour_warns_and_falls_back(self):
        site, tmp = load_config()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "extra.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[group:Broken]\ncolor = "не-цвет"\n')
        site = load(os.path.join(tmp.name, "vibepage.toml"))
        self.assertIsNone(site.groups[group_key("Broken")].color)
        self.assertTrue(any("не понимаю цвет" in warning for warning in site.warnings))


class LayoutColorTests(unittest.TestCase):
    def setUp(self):
        self.site, tmp = load_config()
        self.addCleanup(tmp.cleanup)

    def test_explicit_colour_wins_and_auto_is_stable(self):
        page = build_page(self.site, resolve_icon=lambda service: None)
        self.assertEqual(panel_of(page, "Media")["accent"], "#50fa7b")
        self.assertEqual(panel_of(page, "Auto")["accent"], auto("Auto"))
        self.assertIn(panel_of(page, "Auto")["accent"], ACCENTS)

    def test_icon_colour_and_group_accent_reach_the_card(self):
        page = build_page(self.site, resolve_icon=lambda service: None)
        jellyfin = panel_of(page, "Media")["services"][0]
        solo = panel_of(page, "Infra")["services"][0]
        self.assertEqual(jellyfin["icon_color"], "#ff79c6")
        self.assertIsNone(solo["icon_color"])                 # берётся акцент группы
        self.assertEqual(solo["accent"], "#8be9fd")

    def test_both_links_travel_to_the_page(self):
        page = build_page(self.site, is_local=True, resolve_icon=lambda service: None)
        card = panel_of(page, "Media")["services"][0]
        self.assertEqual(card["href_main"], "https://jellyfin.example.com")
        self.assertEqual(card["href_alt"], "http://10.0.0.2:8096")
        self.assertEqual(card["url"], "http://10.0.0.2:8096")   # локально по умолчанию alt
        solo = panel_of(page, "Infra")["services"][0]
        self.assertEqual(solo["href_alt"], "")
        self.assertEqual(page["default_mode"], "alt")

    def test_remote_page_defaults_to_the_main_link(self):
        page = build_page(self.site, is_local=False, resolve_icon=lambda service: None)
        self.assertEqual(page["default_mode"], "main")
        card = panel_of(page, "Media")["services"][0]
        self.assertEqual(card["url"], "https://jellyfin.example.com")
        self.assertEqual(card["url_alt"], "http://10.0.0.2:8096")


class PageHtmlTests(unittest.TestCase):
    def setUp(self):
        self.site, self.tmp = load_config()
        self.addCleanup(self.tmp.cleanup)
        os.environ["VIBEPAGE_CONFIG"] = os.path.join(self.tmp.name, "vibepage.toml")
        self.addCleanup(os.environ.pop, "VIBEPAGE_CONFIG", None)
        self.client = app_module.app.test_client()

    def test_switch_and_data_attributes_are_rendered(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-default-link-mode=', body)
        self.assertIn('class="linkmode-btn" data-mode="main"', body)
        self.assertIn('class="linkmode-btn" data-mode="alt"', body)
        self.assertIn('data-main="https://jellyfin.example.com"', body)
        self.assertIn('data-alt="http://10.0.0.2:8096"', body)
        self.assertIn("--icon-color: #ff79c6", body)

    def test_group_accent_is_rendered_as_a_css_variable(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("--accent: #50fa7b", body)
        self.assertIn("--accent: #8be9fd", body)
