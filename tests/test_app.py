"""Тесты Flask-приложения: страница отдаётся готовой, конфиг наружу не уходит."""

import os
import tempfile
import unittest

import app as app_module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "icons"), exist_ok=True)
        with open(os.path.join(self.root, "icons", "chip.svg"), "w", encoding="utf-8") as handle:
            handle.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/></svg>')
        self.config = self.write(
            "homepage.toml",
            """
            [system]
            title = "Test Home"
            icon_path = "icons"
            cols = 2

            [group:Group A]
            priority = 1

            [service:Alpha]
            link = "https://alpha.example.com"
            icon = "chip.svg"
            desc = "Первый сервис"
            group = "Group A"

            [service:Beta]
            link = "http://10.0.0.9:1234"
            group = "Group A"
            """,
        )
        os.environ["HOMEPAGE_CONFIG"] = self.config
        self.addCleanup(os.environ.pop, "HOMEPAGE_CONFIG", None)
        self.client = app_module.app.test_client()

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_page_is_rendered_server_side(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Group A", body)
        self.assertIn("Alpha", body)
        self.assertIn("Первый сервис", body)
        self.assertIn("https://alpha.example.com", body)
        self.assertIn("<circle", body)          # svg-иконка вставлена инлайном
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_only_html_routes_exist(self):
        # состояние карточек — единственный JSON в приложении
        rules = {rule.rule for rule in app_module.app.url_map.iter_rules()}
        self.assertEqual(rules, {"/", "/api/state", "/icon/<path:name>", "/healthz", "/static/<path:filename>"})

    def test_page_contains_no_config_syntax(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("[service:", body)
        self.assertNotIn("icon_path", body)

    def test_icon_route_serves_and_guards(self):
        for path, expected in (("/icon/chip.svg", 200), ("/icon/missing.svg", 404),
                               ("/icon/../homepage.toml", 404)):
            response = self.client.get(path)
            self.assertEqual(response.status_code, expected, path)
            response.close()

    def test_config_is_re_read_on_every_request(self):
        self.assertNotIn("Gamma", self.client.get("/").get_data(as_text=True))
        with open(self.config, "a", encoding="utf-8") as handle:
            handle.write('\n[service:Gamma]\nlink = "https://gamma.example.com"\n')
        self.assertIn("Gamma", self.client.get("/").get_data(as_text=True))

    def test_broken_config_still_answers_with_a_banner(self):
        os.unlink(self.config)
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("notice-error", body)


if __name__ == "__main__":
    unittest.main()
