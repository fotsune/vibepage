"""Тесты ключей state_interval, group_cols и [group:…] cols."""

import os
import tempfile
import unittest

from hp.config import Group, Service, Site, System, group_key, load
from hp.layout import build_page


def make_site(groups=(), group_cols=3):
    site = Site(system=System(group_cols=group_cols))
    for name, priority, cols in groups:
        site.groups[group_key(name)] = Group(name=name, priority=priority, cols=cols)
    site.services.append(Service(name="One", group="A", group_key="a"))
    return site


def cols_of(page, name):
    for column in page["columns"]:
        for group in column:
            if group["name"] == name:
                return group["cols"]
    raise AssertionError("группы %r нет на странице" % name)


class SystemKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_defaults_match_the_code(self):
        # дефолты — те, что были захардкожены до появления ключей
        path = self.write("homepage.toml", "[system]\ntitle = \"Home\"\n")
        site = load(path)
        self.assertEqual(site.system.poll, 10)
        self.assertEqual(site.system.group_cols, 3)
        self.assertEqual(site.warnings, [])

    def test_state_interval_is_poll(self):
        path = self.write("homepage.toml", "[system]\nstate_interval = 25\n")
        site = load(path)
        self.assertEqual(site.system.poll, 25)
        self.assertEqual(site.warnings, [])

    def test_state_interval_keeps_the_floor_of_three_seconds(self):
        path = self.write("homepage.toml", "[system]\nstate_interval = 1\n")
        site = load(path)
        self.assertEqual(site.system.poll, 3)
        self.assertTrue(any("state_interval" in warning for warning in site.warnings))

    def test_group_cols_and_group_override(self):
        path = self.write("homepage.toml", """
            [system]
            group_cols = 4

            [group:Wide]
            priority = 1
            cols = 1

            [group:Plain]
            priority = 2
            """)
        site = load(path)
        self.assertEqual(site.system.group_cols, 4)
        self.assertEqual(site.groups["wide"].cols, 1)
        self.assertIsNone(site.groups["plain"].cols)
        page = build_page(site)
        self.assertEqual(cols_of(page, "Wide"), 1)
        self.assertEqual(cols_of(page, "Plain"), 4)

    def test_group_cols_is_used_when_a_group_says_nothing(self):
        path = self.write("homepage.toml", "[system]\ngroup_cols = 2\n\n[group:One]\npriority = 1\n")
        self.assertEqual(cols_of(build_page(load(path)), "One"), 2)

    def test_zero_means_no_limit(self):
        path = self.write("homepage.toml", "[system]\ngroup_cols = 0\n\n[group:One]\npriority = 1\n")
        self.assertEqual(cols_of(build_page(load(path)), "One"), 0)


class MarkupTests(unittest.TestCase):
    def test_cards_pass_the_column_limit_to_css(self):
        site = make_site(groups=[("A", 1, 2), ("B", 2, None)])
        page = build_page(site)
        self.assertEqual(cols_of(page, "A"), 2)
        self.assertEqual(cols_of(page, "B"), 3)
        with open("templates/index.html", encoding="utf-8") as handle:
            template = handle.read()
        self.assertIn('style="--group-cols: {{ group.cols }}"', template)
        with open("static/css/style.css", encoding="utf-8") as handle:
            css = handle.read()
        self.assertIn("var(--group-cols, 99)", css)
        self.assertIn("max(196px", css)
