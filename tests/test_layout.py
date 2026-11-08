"""Тесты раскладки: столбцы, приоритеты, объединённые группы, сортировка."""

import unittest

from hp.config import Group, Service, Site, System, group_key
from hp.layout import build_page, host_is_local


def make_site(groups=(), services=(), cols=3, mobile_cols=1, open_in_new_page=False):
    site = Site(
        system=System(
            cols=cols,
            mobile_cols=mobile_cols,
            open_in_new_page=open_in_new_page,
            icon_path=None,
        )
    )
    for name, priority, link in groups:
        site.groups[group_key(name)] = Group(name=name, priority=priority, link=link)
    for item in services:
        key = group_key(item.group)
        item.group_key = key
        if key not in site.groups:
            site.groups[key] = Group(name=item.group, priority=None, declared=False)
        site.services.append(item)
    return site


def service(name, group, priority=None, link=None, alt=None, new_page=None):
    return Service(
        name=name,
        group=group,
        priority=priority,
        link=link if link is not None else "https://%s.example.com" % name.lower(),
        alt=alt,
        new_page=new_page,
    )


def columns(page):
    return [[group["name"] for group in column] for column in page["columns"]]


def cards(page, group_name):
    for column in page["columns"]:
        for group in column:
            if group["name"] == group_name:
                return [card["name"] for card in group["services"]]
    raise AssertionError("группы %r нет на странице" % group_name)


class ColumnTests(unittest.TestCase):
    def test_priority_picks_column(self):
        site = make_site(groups=[("Left", 2, None), ("Right", 1, None)], cols=2)
        self.assertEqual(columns(build_page(site)), [["Right"], ["Left"]])

    def test_equal_priority_sorted_by_name(self):
        # одинаковый приоритет = один и тот же столбец, порядок по имени
        site = make_site(groups=[("Zeta", 1, None), ("Alpha", 1, None)], cols=2)
        self.assertEqual(columns(build_page(site)), [["Alpha", "Zeta"], []])

    def test_priority_beyond_column_count_wraps(self):
        site = make_site(groups=[("Fifth", 3, None)], cols=2)
        self.assertEqual(columns(build_page(site)), [["Fifth"], []])

    def test_groups_without_priority_fill_left_to_right_and_wrap(self):
        site = make_site(groups=[("A", None, None), ("B", None, None),
                                 ("C", None, None), ("D", None, None)], cols=3)
        self.assertEqual(columns(build_page(site)), [["A", "D"], ["B"], ["C"]])

    def test_prioritised_groups_sort_before_unprioritised(self):
        site = make_site(groups=[("Later", None, None), ("First", 1, None)], cols=1)
        self.assertEqual(columns(build_page(site)), [["First", "Later"]])

    def test_columns_count_comes_from_config(self):
        page = build_page(make_site(groups=[("A", None, None)], cols=4, mobile_cols=2))
        self.assertEqual(len(page["columns"]), 4)
        self.assertEqual(page["mobile_cols"], 2)


class MergeTests(unittest.TestCase):
    def test_linked_group_shares_column_and_follows_its_root(self):
        site = make_site(
            groups=[("G1", 1, None), ("G2", 2, None), ("G3", None, "G2"), ("G4", 3, None)],
            cols=3,
        )
        self.assertEqual(columns(build_page(site)), [["G1"], ["G2", "G3"], ["G4"]])

    def test_link_chain_collapses_into_one_column(self):
        site = make_site(groups=[("A", None, "B"), ("B", None, "C"), ("C", 2, None)], cols=3)
        self.assertEqual(columns(build_page(site)), [[], ["C", "A", "B"], []])

    def test_link_cycle_is_harmless(self):
        site = make_site(groups=[("A", None, "B"), ("B", None, "A")], cols=3)
        page = build_page(site)
        placed = [name for column in columns(page) for name in column]
        self.assertCountEqual(placed, ["A", "B"])
        self.assertEqual(len([column for column in page["columns"] if column]), 1)

    def test_unknown_link_is_reported(self):
        site = make_site(groups=[("A", None, "Nope")], cols=2)
        page = build_page(site)
        self.assertTrue(any("Nope" in warning for warning in page["warnings"]))

    def test_merged_group_is_marked(self):
        site = make_site(groups=[("Root", 1, None), ("Joined", None, "Root")], cols=2)
        page = build_page(site)
        joined = page["columns"][0][1]
        self.assertTrue(joined["merged"])
        self.assertEqual(joined["root"], "Root")
        self.assertEqual(joined["accent"], page["columns"][0][0]["accent"])


class CardTests(unittest.TestCase):
    def test_services_sorted_by_priority_then_name(self):
        site = make_site(
            groups=[("Box", 1, None)],
            services=[service("Zulu", "Box"), service("Alpha", "Box"),
                      service("Second", "Box", priority=2), service("First", "Box", priority=1)],
        )
        self.assertEqual(cards(build_page(site), "Box"), ["First", "Second", "Alpha", "Zulu"])

    def test_empty_group_is_still_rendered(self):
        site = make_site(groups=[("Empty", 1, None)], cols=1)
        page = build_page(site)
        self.assertEqual(columns(page), [["Empty"]])
        self.assertEqual(page["columns"][0][0]["services"], [])

    def test_local_address_prefers_the_alternative_link(self):
        site = make_site(
            groups=[("Box", 1, None)],
            services=[service("App", "Box", link="https://app.example.com", alt="http://10.0.0.2:8080")],
        )
        local = build_page(site, is_local=True)["columns"][0][0]["services"][0]
        remote = build_page(site, is_local=False)["columns"][0][0]["services"][0]
        self.assertEqual(local["url"], "http://10.0.0.2:8080")
        self.assertEqual(local["url_alt"], "https://app.example.com")
        self.assertEqual(remote["url"], "https://app.example.com")
        self.assertEqual(remote["url_alt"], "http://10.0.0.2:8080")

    def test_service_without_alternative_has_no_second_link(self):
        site = make_site(groups=[("Box", 1, None)], services=[service("App", "Box")])
        card = build_page(site)["columns"][0][0]["services"][0]
        self.assertIsNone(card["url_alt"])

    def test_new_page_inherits_system_default(self):
        site = make_site(groups=[("Box", 1, None)],
                         services=[service("A", "Box"), service("B", "Box", new_page=False)],
                         open_in_new_page=True)
        rendered = {card["name"]: card["new_page"]
                    for card in build_page(site)["columns"][0][0]["services"]}
        self.assertIs(rendered["A"], True)
        self.assertIs(rendered["B"], False)   # явное значение сильнее системного

    def test_placeholder_initial_is_always_a_letter(self):
        site = make_site(groups=[("Box", 1, None)], services=[service("qBittorrent", "Box")])
        card = build_page(site)["columns"][0][0]["services"][0]
        self.assertEqual(card["initial"], "Q")


class LocalHostTests(unittest.TestCase):
    def test_local_and_remote_hosts(self):
        for host in ("127.0.0.1:8765", "localhost:8080", "192.168.1.5:80", "[::1]:8765",
                     "10.0.0.2", "nas:8080", None, ""):
            self.assertTrue(host_is_local(host), host)
        for host in ("home.example.com", "grafana.example.com:3000", "8.8.8.8"):
            self.assertFalse(host_is_local(host), host)


if __name__ == "__main__":
    unittest.main()
