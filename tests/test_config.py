"""Тесты разбора TOML-like конфига и сборки модели."""

import os
import re
import shutil
import tempfile
import unittest

from hp.config import DEFAULT_GROUP, load, parse_file, parse_value


class ParseValueTests(unittest.TestCase):
    def test_quoted_string(self):
        self.assertEqual(parse_value('"Home"'), "Home")
        self.assertEqual(parse_value("'My services'"), "My services")

    def test_bare_string_and_inline_comment(self):
        self.assertEqual(parse_value("https://a.example.com # внешняя"), "https://a.example.com")
        self.assertEqual(parse_value("https://a.example.com/#/dash"), "https://a.example.com/#/dash")

    def test_bools_and_ints(self):
        for text in ("true", "True", "yes", "on"):
            self.assertIs(parse_value(text), True, text)
        for text in ("false", "False", "no", "off"):
            self.assertIs(parse_value(text), False, text)
        self.assertEqual(parse_value("42"), 42)
        self.assertEqual(parse_value("-3"), -3)
        self.assertEqual(parse_value("3.5"), "3.5")

    def test_arrays(self):
        self.assertEqual(parse_value('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_value("conf.d"), "conf.d")

    def test_escaped_quote(self):
        self.assertEqual(parse_value(r'"a\"b"'), 'a"b')


class ParseFileTests(unittest.TestCase):
    def make(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_sections_and_names_with_spaces(self):
        path = self.make(
            """
            # комментарий
            [service:My Server]
            link = "https://example.com"
            new_page = True

            [group:My services]
            priority = 2
            """
        )
        sections, errors = parse_file(path)
        self.assertEqual(errors, [])
        self.assertEqual([s["kind"] for s in sections], ["service", "group"])
        self.assertEqual(sections[0]["name"], "My Server")
        self.assertIs(sections[0]["values"]["new_page"], True)
        self.assertEqual(sections[1]["name"], "My services")

    def test_unknown_section_and_stray_key(self):
        path = self.make(
            """
            link = "https://elsewhere"
            [nonsense]
            foo = "bar"
            """
        )
        sections, errors = parse_file(path)
        self.assertEqual(len(errors), 3)  # ключ до секции, лишняя секция, ключ внутри неё
        self.assertEqual(sections, [])


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "conf.d"), exist_ok=True)

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_extra_files_and_system_precedence(self):
        self.write(
            "homepage.toml",
            """
            [system]
            title = "Home"
            icon_path = "icons"
            config_path = "conf.d"
            cols = 3

            [group:Media]
            priority = 1
            """,
        )
        self.write(
            "conf.d/dashboard.toml",
            """
            [system]
            cols = 5
            open_in_new_page = true

            [group:Media]
            name = "Media (all)"
            priority = 4
            """,
        )
        self.write(
            "conf.d/services.toml",
            """
            [service:Jellyfin]
            link = "https://jellyfin.example.com"
            group = "Media"
            icon = "film.svg"

            [service:Loose]
            link = "http://10.0.0.5:8080"
            """,
        )
        site = load(os.path.join(self.root, "homepage.toml"))
        self.assertEqual(site.errors, [])
        # главный конфиг выигрывает у доп. файла
        self.assertEqual(site.system.cols, 3)
        self.assertIs(site.system.open_in_new_page, True)
        self.assertEqual(site.system.title, "Home")
        self.assertEqual(site.system.icon_path, os.path.normpath(os.path.join(self.root, "icons")))
        self.assertEqual(len(site.services), 2)
        # сервис без group попадает в Default, а группа создаётся неявно
        loose = site.services[1]
        self.assertEqual(loose.group, DEFAULT_GROUP)
        self.assertIn("default", site.groups)
        self.assertFalse(site.groups["default"].declared)
        # повторное объявление группы дополняет, а не дублирует её:
        # главный конфиг идёт последним и побеждает
        self.assertEqual(len(site.groups), 2)
        self.assertEqual(site.groups["media"].name, "Media")
        self.assertEqual(site.groups["media"].priority, 1)

    def test_name_defaults_and_validation(self):
        self.write(
            "homepage.toml",
            """
            [system]
            cols = 0
            mobile_cols = "x"

            [service:Cam]
            alt = "http://10.0.0.2:8080"
            priority = "first"
            whatever = 1
            """,
        )
        site = load(os.path.join(self.root, "homepage.toml"))
        self.assertEqual(site.services[0].name, "Cam")   # имя берётся из секции
        self.assertIsNone(site.services[0].priority)     # мусор в priority — предупреждение, не падение
        self.assertEqual(site.system.cols, 1)            # 0 поднимается до минимума
        self.assertEqual(site.system.mobile_cols, 1)     # тот же дефолт
        self.assertTrue(any("priority" in w for w in site.warnings))
        self.assertTrue(any("whatever" in w for w in site.warnings))

    def test_missing_config_dir_is_reported(self):
        self.write(
            "homepage.toml",
            """
            [system]
            config_path = "nowhere"
            """,
        )
        site = load(os.path.join(self.root, "homepage.toml"))
        self.assertTrue(any("nowhere" in error for error in site.errors))


class ExampleConfigTests(unittest.TestCase):
    """config/homepage.toml.example — образец, который обязан оставаться рабочим."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.example = os.path.join(root, "config", "homepage.toml.example")
        os.mkdir(os.path.join(self.tmp.name, "conf.d"))
        shutil.copytree(os.path.join(root, "config", "icons"),
                        os.path.join(self.tmp.name, "icons"))
        self.path = os.path.join(self.tmp.name, "homepage.toml")
        shutil.copy(self.example, self.path)

    def test_example_loads_without_a_single_complaint(self):
        site = load(self.path)
        self.assertEqual(site.errors, [])
        self.assertEqual(site.warnings, [])
        # дефолты, о которых говорит образец, — те же, что в коде
        self.assertEqual(site.system.poll, 10)        # state_interval
        self.assertEqual(site.system.group_cols, 3)
        self.assertEqual(site.system.cols, 3)

    def text(self):
        with open(self.example, encoding="utf-8") as handle:
            return handle.read()

    def test_example_names_every_widget_type(self):
        from hp import widgets
        site = load(self.path)
        declared = {widgets.canonical_type(widget.type) for widget in site.widgets.values()}
        self.assertEqual(declared, set(widgets.REGISTRY))

    def test_every_widget_can_be_taken_from_a_neighbour(self):
        # external работает у каждого типа, значит и расписан в каждом блоке
        blocks = re.split(r"(?m)^\[widget:", self.text())[1:]
        self.assertEqual(len(blocks), 11)
        for block in blocks:
            head = block.split("\n", 1)[0].strip(" ]")
            for key in ("type", "host", "external", "fields"):
                self.assertRegex(block, r"(?m)^\s*#?\s*%s\s*=" % key, f"{head}: нет ключа {key}")

    def test_every_config_key_is_documented(self):
        from hp.config import GROUP_KEYS, HEADER_KEYS, SERVICE_KEYS, SYSTEM_KEYS
        from hp import widgets
        text = self.text()
        aliases = {"poll", "enabled", "header", "main_alt", "tagline", "subtitle"}
        for keys in (SYSTEM_KEYS, HEADER_KEYS, GROUP_KEYS, SERVICE_KEYS):
            for key in keys:
                if key in aliases:
                    self.assertIn(key, text)        # алиас хотя бы назван по имени
                else:
                    self.assertRegex(text, r"(?m)^\s*#?\s*%s\s*=" % key, key)
        for spec in widgets.REGISTRY.values():      # и ключи самих виджетов
            for key in spec.REQUIRED + spec.OPTIONAL:
                self.assertRegex(text, r"(?m)^\s*#?\s*%s\s*=" % key, key)

    def test_example_copies_into_a_quiet_page(self):
        site = load(self.path)
        card = site.services[0]
        self.assertEqual(card.widgets, ["cpu"])
        self.assertEqual(card.alt, "http://192.168.1.10:3000")
        self.assertTrue(card.new_page)
        self.assertEqual(card.group, "Main")


if __name__ == "__main__":
    unittest.main()
