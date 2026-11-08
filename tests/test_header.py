"""Тесты секции [header]: часы, поиск, иконки и принудительный режим ссылок."""

import os
import tempfile
import unittest

import app as app_module

from hp.config import Header, load
from hp.layout import build_page

LOGO = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect width="24" height="24" fill="currentColor"/></svg>')

BASE = """
[system]
title = "Header"
icon_path = "icons"
config_path = "."
cols = 2

[service:Solo]
link = "https://solo.example.com"
alt = "http://10.0.0.2:8080"
"""


class HeaderCase(unittest.TestCase):
    def make(self, extra="", logo=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        icons = os.path.join(tmp.name, "icons")
        os.makedirs(icons, exist_ok=True)
        if logo:
            with open(os.path.join(icons, "logo.svg"), "w", encoding="utf-8") as handle:
                handle.write(LOGO)
        path = os.path.join(tmp.name, "homepage.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(BASE + extra)
        return load(path), path

    def client(self, path):
        os.environ["HOMEPAGE_CONFIG"] = path
        self.addCleanup(os.environ.pop, "HOMEPAGE_CONFIG", None)
        return app_module.app.test_client()


class HeaderDefaultsTests(HeaderCase):
    def test_defaults(self):
        site, _ = self.make()
        self.assertEqual(site.header, Header())
        self.assertTrue(site.header.show and site.header.time and site.header.search)
        self.assertEqual(site.header.time_format, 12)
        self.assertEqual(site.header.link_mode, "auto")

    def test_every_key_is_read(self):
        site, _ = self.make("""
[header]
show = true
time = false
time_format = 24
search = false
icon = "logo.svg"
favicon = "logo.svg"
link_mode = "alt"
""")
        header = site.header
        self.assertFalse(header.time)
        self.assertFalse(header.search)
        self.assertEqual(header.time_format, 24)
        self.assertEqual(header.icon, "logo.svg")
        self.assertEqual(header.favicon, "logo.svg")
        self.assertEqual(header.link_mode, "alt")

    def test_header_keys_work_inside_system_too(self):
        site, _ = self.make("""
[system]
header = false
time = false
main_alt = "main"
""")
        self.assertFalse(site.header.show)
        self.assertFalse(site.header.time)
        self.assertEqual(site.header.link_mode, "main")

    def test_bad_values_warn_and_keep_defaults(self):
        site, _ = self.make("""
[header]
time_format = 13
link_mode = "sideways"
search = "может быть"
""")
        self.assertEqual(site.header.time_format, 12)
        self.assertEqual(site.header.link_mode, "auto")
        self.assertTrue(site.header.search)
        self.assertEqual(len([w for w in site.warnings if "time_format" in w]), 1)
        self.assertEqual(len([w for w in site.warnings if "link_mode" in w]), 1)
        self.assertEqual(len([w for w in site.warnings if "search" in w]), 1)

    def test_tagline_has_a_default_and_can_be_changed(self):
        site, _ = self.make()
        self.assertEqual(site.header.desc, "self-hosted dashboard")
        site, _ = self.make('\n[header]\ndesc = "мои сервисы"\n')
        self.assertEqual(site.header.desc, "мои сервисы")
        site, _ = self.make('\n[header]\nsubtitle = "сеть дома"\n', logo=False)
        self.assertEqual(site.header.desc, "сеть дома")
        site, _ = self.make('\n[header]\ndesc = ""\n', logo=False)
        self.assertEqual(site.header.desc, "")          # пустая строка прячет подпись

    def test_unknown_header_key_warns(self):
        site, _ = self.make("\n[header]\nclock = false\n")
        self.assertTrue(any("header.clock" in warning for warning in site.warnings))


class HeaderLayoutTests(HeaderCase):
    def test_auto_follows_the_connection(self):
        site, _ = self.make()
        self.assertEqual(build_page(site, is_local=True, resolve_icon=lambda s: None)["default_mode"], "alt")
        self.assertEqual(build_page(site, is_local=False, resolve_icon=lambda s: None)["default_mode"], "main")

    def test_forced_mode_beats_the_connection(self):
        site, _ = self.make('\n[header]\nlink_mode = "main"\n')
        page = build_page(site, is_local=True, resolve_icon=lambda s: None)
        self.assertEqual(page["default_mode"], "main")
        self.assertEqual(page["columns"][0][0]["services"][0]["url"], "https://solo.example.com")
        site, _ = self.make('\n[header]\nlink_mode = "alt"\n', logo=False)
        page = build_page(site, is_local=False, resolve_icon=lambda s: None)
        self.assertEqual(page["default_mode"], "alt")
        self.assertEqual(page["columns"][0][0]["services"][0]["url"], "http://10.0.0.2:8080")


class HeaderHtmlTests(HeaderCase):
    def test_clock_search_and_format(self):
        _, path = self.make("\n[header]\ntime_format = 24\n")
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertIn('data-time-format="24"', body)
        self.assertIn('id="clock-time"', body)
        self.assertIn('class="search"', body)

    def test_clock_can_be_switched_off(self):
        _, path = self.make("\n[header]\ntime = false\n")
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertNotIn('id="clock-time"', body)
        self.assertIn('class="search"', body)

    def test_search_can_be_switched_off(self):
        _, path = self.make("\n[header]\nsearch = false\n")
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertNotIn('class="search"', body)
        self.assertIn('id="clock-time"', body)

    def test_whole_header_can_be_switched_off(self):
        _, path = self.make("\n[header]\nshow = false\n")
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertNotIn('class="topbar"', body)
        self.assertNotIn('id="clock-time"', body)
        self.assertIn('class="no-header"', body)
        self.assertIn('<section class="panel"', body)  # сервисы на месте

    def test_header_icon_and_favicon_come_from_icon_path(self):
        _, path = self.make('\n[header]\nicon = "logo.svg"\nfavicon = "logo.svg"\n')
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertIn('class="brand-mark"', body)
        self.assertIn("<rect width=\"24\" height=\"24\" fill=\"currentColor\"/>", body)
        self.assertIn('rel="icon" href="data:image/svg+xml', body)

    def test_missing_favicon_falls_back_to_the_builtin_one(self):
        site, path = self.make('\n[header]\nfavicon = "nope.svg"\n')
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertIn('href="/static/favicon.svg"', body)

    def test_tagline_is_rendered_and_can_be_hidden(self):
        _, path = self.make('\n[header]\ndesc = "мои сервисы"\n')
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertIn("<small>мои сервисы</small>", body)
        _, path = self.make('\n[header]\ndesc = ""\n', logo=False)
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertNotIn("self-hosted dashboard", body)
        self.assertNotIn("<small></small>", body)

    def test_default_favicon_without_header_section(self):
        _, path = self.make()
        body = self.client(path).get("/").get_data(as_text=True)
        self.assertIn('href="/static/favicon.svg"', body)
        self.assertIn('data-time-format="12"', body)
        self.assertIn("<small>self-hosted dashboard</small>", body)
