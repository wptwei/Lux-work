from pathlib import Path
import unittest

from core.ui_theme import CSS_PATH, LUX_ENTERPRISE_CSS

ROOT = Path(__file__).resolve().parents[1]


class FrontendDesignStandardTests(unittest.TestCase):
    def test_single_stylesheet_exists_and_is_loaded(self):
        self.assertTrue(CSS_PATH.is_file())
        self.assertEqual(CSS_PATH.resolve(), (ROOT / "frontend" / "lux.css").resolve())
        self.assertGreater(len(LUX_ENTERPRISE_CSS), 10_000)

    def test_app_does_not_embed_duplicate_css(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("from core.ui_theme import LUX_ENTERPRISE_CSS", source)
        self.assertIn("CSS = LUX_ENTERPRISE_CSS", source)
        self.assertNotIn('CSS = """', source)
        self.assertNotIn("CSS +=", source)

    def test_design_tokens_are_declared(self):
        for token in (
            "--lux-bg", "--lux-surface", "--lux-border", "--lux-text",
            "--lux-text-secondary", "--lux-primary", "--lux-accent",
            "--lux-radius-sm", "--lux-radius-md", "--lux-radius-lg",
        ):
            with self.subTest(token=token):
                self.assertIn(token, LUX_ENTERPRISE_CSS)

    def test_interaction_states_and_accessibility_hooks_exist(self):
        for selector in (
            "button:disabled", "button[aria-disabled=\"true\"]",
            ":focus-visible", "prefers-reduced-motion", "@media print",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, LUX_ENTERPRISE_CSS)

    def test_incident_page_has_responsive_rules(self):
        for marker in (
            ".incident-workspace", ".event-input-card", ".event-result-card",
            ".incident-flow", "@media (max-width: 1050px)",
            "@media (max-width: 680px)",
            "flex-direction:row!important",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, LUX_ENTERPRISE_CSS)


if __name__ == "__main__":
    unittest.main()
