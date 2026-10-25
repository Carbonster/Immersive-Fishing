"""The mod and its RML documents have to agree about names, and nothing else checks that.

A value the documents bind to that the mod never sets draws nothing, silently; a value the mod
sets that no document reads is a panel that never moves. In the game either looks like a bug in
the physics rather than a typo in a string.

Reads both sides as text: the mod cannot be imported outside the game, and the Runtime is not a
dependency of the mod's own tests.

    python -m unittest discover -s tests
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"
MOD_FILE = REPO / "fishing_minigame_mod.py"

# Words a data expression may use that are not values.
EXPRESSION_WORDS = {"true", "false", "px", "auto"}


def mod_source():
    return MOD_FILE.read_text(encoding="utf-8")


def module_tuple(name):
    match = re.search(rf"^{name} = \(([^)]*)\)", mod_source(), re.M | re.S)
    return re.findall(r'"([^"]+)"', match.group(1))


def documents():
    match = re.search(r"^UI_DOCUMENTS = (\{[^}]*\})", mod_source(), re.M)
    return json.loads(match.group(1))


def names_in_expression(expression):
    without_strings = re.sub(r"'[^']*'", "", expression)
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", without_strings)) - EXPRESSION_WORDS


def names_documents_read():
    found = set()
    for path in UI.glob("*.rml"):
        text = path.read_text(encoding="utf-8")
        for expression in re.findall(r'data-[a-z-]+(?:-[A-Za-z]+)*="([^"]*)"', text):
            found |= names_in_expression(expression.replace("&amp;", "&"))
        for expression in re.findall(r"\{\{(.*?)\}\}", text):
            found |= names_in_expression(expression)
    found.discard("fishing")   # data-model="fishing" names the model, not a value
    return found


def names_mod_sets():
    source = mod_source()
    found = set(re.findall(r'self\.ui\.set\("([A-Za-z_][A-Za-z0-9_]*)"', source))
    found |= {f"show_{group}" for group in module_tuple("UI_GROUPS")}
    found |= set(re.findall(r'\("(key[A-Za-z]+)", HOTKEY_', source))
    return found


class NamesAgreeTests(unittest.TestCase):
    def test_every_value_a_document_reads_is_set_by_the_mod(self):
        missing = names_documents_read() - names_mod_sets()
        self.assertEqual(missing, set(), f"bound in ui/ but never set: {sorted(missing)}")

    def test_every_value_the_mod_sets_is_read_by_a_document(self):
        unread = names_mod_sets() - names_documents_read()
        self.assertEqual(unread, set(), f"set by the mod but read nowhere: {sorted(unread)}")

    def test_every_preset_switch_has_a_value(self):
        presets = json.loads((REPO / "data" / "ui_visibility.json").read_text(encoding="utf-8"))
        switches = set(presets["groups"])
        unknown = switches - set(module_tuple("UI_GROUPS"))
        self.assertEqual(unknown, set(), f"ui_visible switches with no value: {sorted(unknown)}")


class DocumentTests(unittest.TestCase):
    def test_every_document_the_mod_loads_exists(self):
        for name, path in documents().items():
            self.assertTrue((REPO / path).is_file(), f"{name}: {path}")

    def test_every_document_binds_to_the_mods_model(self):
        name = re.search(r'^UI_NAME = "([^"]+)"', mod_source(), re.M).group(1)
        for path in documents().values():
            self.assertIn(f'data-model="{name}"', (REPO / path).read_text(encoding="utf-8"), path)

    def test_every_file_a_document_links_exists(self):
        for path in UI.glob("*.rml"):
            for link in re.findall(r'(?:href|src)="([^"]+)"', path.read_text(encoding="utf-8")):
                self.assertTrue((UI / link).resolve().is_file(), f"{path.name}: {link}")

    def test_the_catch_panel_sits_on_the_visor_like_the_game_hud(self):
        self.assertIn("filter: visor(", (UI / "catch.rcss").read_text(encoding="utf-8"))


class ThemeTests(unittest.TestCase):
    def test_every_theme_the_mod_cycles_is_styled(self):
        themes = (UI / "themes.rcss").read_text(encoding="utf-8")
        for theme in module_tuple("THEMES"):
            self.assertIn(f"body.theme-{theme} {{", themes, theme)

    def test_every_theme_defines_the_same_colours(self):
        blocks = re.findall(r"body\.theme-(\w+) \{([^}]*)\}",
                            (UI / "themes.rcss").read_text(encoding="utf-8"))
        names = {theme: set(re.findall(r"--([\w-]+):", body)) for theme, body in blocks}
        first = next(iter(names.values()))
        for theme, colours in names.items():
            self.assertEqual(colours, first, theme)

    def test_every_colour_the_settings_screen_uses_is_defined(self):
        used = set(re.findall(r"var\(--([\w-]+)\)", (UI / "settings.rcss").read_text(encoding="utf-8")))
        defined = set(re.findall(r"--([\w-]+):", (UI / "themes.rcss").read_text(encoding="utf-8")))
        self.assertEqual(used - defined, set())


if __name__ == "__main__":
    unittest.main()
