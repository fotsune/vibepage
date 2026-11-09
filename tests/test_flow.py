"""Порядок групп: столбцы на десктопе, поток приоритетов на узком экране."""

import os
import shutil
import tempfile
import unittest

import app as app_module

from vp.config import load
from vp.layout import build_page

CONFIG = """
[system]
title = "Flow"
cols = 3
mobile_cols = 1

[group:Group1]
priority = 1

[group:Group2]
priority = 2

[group:Group3]
link = "Group2"

[group:Group4]
priority = 3

[group:Group5]
priority = 4

[group:Default]
priority = 5

[service:A1]
link = "https://a1.example.com"
alt = "http://10.0.0.2:1"
group = "Group1"
"""

PRIORITY_ORDER = ["Group1", "Group2", "Group3", "Group4", "Group5", "Default"]


def load_config():
    """mkdtemp, не TemporaryDirectory: каталог должен жить до конца теста."""
    directory = tempfile.mkdtemp(prefix="vp-flow-")
    path = os.path.join(directory, "vibepage.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(CONFIG)
    return load(path), directory


class ColumnTests(unittest.TestCase):
    def setUp(self):
        site, self.dir = load_config()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.page = build_page(site, is_local=True, resolve_icon=lambda service: None)

    def test_priority_still_picks_the_desktop_column(self):
        names = [[group["name"] for group in column] for column in self.page["columns"]]
        self.assertEqual(names, [["Group1", "Group5"], ["Group2", "Group3", "Default"], ["Group4"]])

    def test_every_group_gets_a_flow_number_in_priority_order(self):
        flat = sorted(
            (group for column in self.page["columns"] for group in column),
            key=lambda group: group["flow"],
        )
        self.assertEqual([group["name"] for group in flat], PRIORITY_ORDER)

    def test_merged_group_flows_right_after_its_root(self):
        flows = {group["name"]: group["flow"] for column in self.page["columns"] for group in column}
        self.assertEqual(flows["Group3"], flows["Group2"] + 1)
        self.assertTrue(all(isinstance(flows[name], int) for name in PRIORITY_ORDER))


class ChipHintTests(unittest.TestCase):
    def setUp(self):
        site, self.dir = load_config()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.page = build_page(site, is_local=True, resolve_icon=lambda service: None)
        self.card = self.page["columns"][0][0]["services"][0]

    def test_hint_describes_the_link_the_chip_opens(self):
        # в режиме main чип ведёт на alt (локальный), в режиме alt — на link (внешний)
        self.assertEqual(self.card["chip_title_main"], "Open local link")
        self.assertEqual(self.card["chip_title_alt"], "Open external link")
        self.assertEqual(self.card["url_alt_title"], "Open external link")


class FlowHtmlTests(unittest.TestCase):
    def test_template_carries_flow_and_both_hints(self):
        site, directory = load_config()
        self.addCleanup(shutil.rmtree, directory, True)
        os.environ["VIBEPAGE_CONFIG"] = os.path.join(directory, "vibepage.toml")
        self.addCleanup(os.environ.pop, "VIBEPAGE_CONFIG", None)
        body = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("--flow: 0", body)
        self.assertIn("--flow: 5", body)
        self.assertIn('data-title-main="Open local link"', body)
        self.assertIn('data-title-alt="Open external link"', body)
        self.assertIn("order: var(--flow", open("static/css/style.css", encoding="utf-8").read())
