import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "display_name.py"
spec = importlib.util.spec_from_file_location("display_name", MODULE_PATH)
display_name = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["display_name"] = display_name
spec.loader.exec_module(display_name)
resolve_display_name = display_name.resolve_display_name


class ResolveDisplayNameTest(unittest.TestCase):
    def test_bot_name_is_shown_as_sent(self) -> None:
        self.assertEqual(resolve_display_name("Vibe", "BaaS onboarder", False), "Vibe")

    def test_surrounding_whitespace_is_dropped(self) -> None:
        self.assertEqual(resolve_display_name("  Vibe ", "BaaS onboarder", False), "Vibe")

    def test_persona_name_stands_in_without_bot_name(self) -> None:
        self.assertEqual(resolve_display_name("", "BaaS onboarder", False), "BaaS onboarder")
        self.assertEqual(resolve_display_name(None, "BaaS onboarder", False), "BaaS onboarder")
        self.assertEqual(resolve_display_name("   ", "BaaS onboarder", False), "BaaS onboarder")

    def test_persona_key_shows_the_persona_name(self) -> None:
        self.assertEqual(
            resolve_display_name("baas_onboarder", "BaaS onboarder", True), "BaaS onboarder"
        )


if __name__ == "__main__":
    unittest.main()
