"""Тесты цветов: имена, hex, пары «имя/#hex» и автоподбор акцента."""

import unittest

from hp.colors import ACCENTS, auto, parse


class ParseTests(unittest.TestCase):
    def test_hex_forms(self):
        self.assertEqual(parse("#ff0000"), "#ff0000")
        self.assertEqual(parse("#FF0000"), "#ff0000")
        self.assertEqual(parse("#f00"), "#ff0000")
        self.assertEqual(parse("ff00ff"), "#ff00ff")
        self.assertEqual(parse("#ff000080"), "#ff000080")

    def test_names_use_the_dracula_palette(self):
        self.assertEqual(parse("red"), "#ff5555")
        self.assertEqual(parse("Red"), "#ff5555")
        self.assertEqual(parse("cyan"), "#8be9fd")

    def test_name_slash_hex(self):
        self.assertEqual(parse("red/#ff0000"), "#ff5555")
        self.assertEqual(parse("#ff0000/red"), "#ff0000")

    def test_rgb_and_junk(self):
        self.assertEqual(parse("rgb(255, 0, 0)"), "#ff0000")
        self.assertEqual(parse("rgba(0,128,255,0.5)"), "#0080ff")
        self.assertIsNone(parse("не-цвет"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse(True))


class AutoTests(unittest.TestCase):
    def test_stable_and_from_the_palette(self):
        first = auto("Media")
        self.assertEqual(first, auto("media"))       # регистр не важен
        self.assertEqual(first, auto("Media"))       # перезапуск не меняет цвет
        self.assertIn(first, ACCENTS)

    def test_names_spread_over_the_palette(self):
        picked = {auto(name) for name in ("Media", "Infra", "Home", "Default", "Docs", "Games", "Lab")}
        self.assertGreater(len(picked), 1)

    def test_empty_seed_still_returns_a_colour(self):
        self.assertIn(auto(""), ACCENTS)
