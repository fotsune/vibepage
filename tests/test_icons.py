"""Тесты иконок: файлы, mdi/sdi через Iconify, кэш и запасные варианты."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from vp import icons

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M1 1h2"/></svg>'


def service(**kwargs):
    base = dict(icon=None, icon_link=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


class SplitSchemeTests(unittest.TestCase):
    def test_known_sets(self):
        self.assertEqual(icons.split_scheme("mdi:account-box"), ("mdi", "account-box"))
        self.assertEqual(icons.split_scheme("MDI:Home"), ("mdi", "home"))
        self.assertEqual(icons.split_scheme("sdi:gitea"), ("simple-icons", "gitea"))
        self.assertEqual(icons.split_scheme("mdi-download"), ("mdi", "download"))
        self.assertEqual(icons.split_scheme("mdi/account-box"), ("mdi", "account-box"))
        self.assertEqual(icons.split_scheme("simple-icons:github"), ("simple-icons", "github"))

    def test_file_names_and_urls_are_not_schemes(self):
        for icon in ("home.svg", "icons/home.svg", "https://cdn.example.com/a.svg", "", None):
            self.assertIsNone(icons.split_scheme(icon), icon)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.icons = os.path.join(self.tmp.name, "icons")
        self.cache = os.path.join(self.tmp.name, ".icon-cache")
        os.makedirs(self.icons)
        with open(os.path.join(self.icons, "chip.svg"), "w", encoding="utf-8") as handle:
            handle.write(SVG)

    def fetch(self, calls):
        def fetch(icon_set, name):
            calls.append((icon_set, name))
            return SVG
        return fetch

    def test_local_file_wins_over_scheme(self):
        found = icons.resolve(service(icon="chip.svg", icon_link="https://x/y.svg"), self.icons, self.cache, fetch=self.fetch([]))
        self.assertIn("<svg", found["svg"])

    def test_svg_suffix_is_stripped(self):
        icons.forget()
        found = icons.resolve(service(icon="mdi:home.svg", icon_link="https://x/y.png"), self.icons, self.cache,
                              fetch=lambda s, n: SVG)
        self.assertIn("<svg", found["svg"])

    def test_mdi_is_fetched_and_tinted(self):
        calls = []
        found = icons.resolve(service(icon="mdi:account-box"), self.icons, self.cache, fetch=self.fetch(calls))
        self.assertEqual(calls, [("mdi", "account-box")])
        self.assertIn('fill="currentColor"', found["svg"])
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "mdi", "account-box.svg")))

    def test_cache_survives_without_network(self):
        icons.forget()
        icons.resolve(service(icon="sdi:gitea"), self.icons, self.cache, fetch=self.fetch([]))
        def broken(icon_set, name):
            raise OSError("offline")
        found = icons.resolve(service(icon="sdi:gitea"), self.icons, self.cache, fetch=broken)
        self.assertIn("<svg", found["svg"])

    def test_failure_falls_back_to_icon_link(self):
        icons.forget()
        def broken(icon_set, name):
            raise OSError("offline")
        found = icons.resolve(service(icon="mdi:nope-nope", icon_link="https://x/y.png"), self.icons, self.cache, fetch=broken)
        self.assertEqual(found, {"url": "https://x/y.png"})

    def test_failure_without_backup_means_placeholder(self):
        icons.forget()
        def broken(icon_set, name):
            raise OSError("offline")
        self.assertIsNone(icons.resolve(service(icon="mdi:nope-nope"), self.icons, self.cache, fetch=broken))

    def test_missing_file_and_link_means_placeholder(self):
        self.assertIsNone(icons.resolve(service(), self.icons, self.cache))

    def test_existing_root_fill_is_kept(self):
        markup = '<svg xmlns="http://www.w3.org/2000/svg" fill="#fff"><path d="M1 1h2"/></svg>'
        self.assertEqual(icons._tint(markup), markup)
