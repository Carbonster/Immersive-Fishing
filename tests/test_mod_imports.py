"""Import the mod for real, the way NMSpy would.

Nothing else in this project does that. `test_ui_file.py` reads the mod's source as text with
regular expressions on purpose - a real import needs pyMHF and NMSpy, which need a running No
Man's Sky.

That gap has a price, paid twice: `ce1038b` (0.4.0) moved HINT_KEYBOARD and HINT_CONTROLLER
below the UI_GROUPS table that reads them, so every import raised NameError and NMSpy dropped
the mod outright - no keys, no panel, nothing on screen. `py_compile` does not catch this
(names resolve at call time, not compile time), and it shipped in two releases before anyone
started the game.

pyMHF and NMSpy are stood in for with permissive stubs: anything they are asked for answers to
any attribute, call, or use as a decorator. The mod is copied into a temporary directory first
and imported from there - it configures its own logging as a side effect of import, and running
this against the real checkout would leave logs\\*.log sitting in the working tree after every
test run.
"""

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Everything _mod_dir()-relative loading touches at import time. ui/ is not needed:
# the stub environment has no oreo_runtime to import, so the mod never gets far enough to open
# its own documents - see _ensure_runtime()'s "Runtime is required but could not be imported" path.
MOD_FILES = (
    "fishing_minigame_mod.py",
    "fishing_mod.ini",
    "projectoreo.ini",
    "data/fish_names.json",
    "data/fishing_presets.json",
    "data/ui_visibility.json",
)


class _Stub:
    """Answers to any attribute, any call, and works as a decorator or a decorator factory."""

    def __init__(self, name="stub"):
        self._name = name

    def __getattr__(self, item):
        return _Stub(f"{self._name}.{item}")

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]  # used directly as a decorator: @on_key_pressed
        return _Stub(f"{self._name}()")  # used as a decorator factory: @on_key_pressed("f1")


class _StubMod:
    def __init__(self):
        pass


def _install_framework_stubs():
    """Enough of pymhf/nmspy's surface for fishing_minigame_mod.py to import cleanly."""
    modules = {
        "nmspy": types.ModuleType("nmspy"),
        "nmspy.common": types.ModuleType("nmspy.common"),
        "nmspy.data": types.ModuleType("nmspy.data"),
        "nmspy.data.audiokinetic": types.ModuleType("nmspy.data.audiokinetic"),
        "nmspy.data.enums": types.ModuleType("nmspy.data.enums"),
        "nmspy.data.enums.external_enums": types.ModuleType("nmspy.data.enums.external_enums"),
        "nmspy.globals": types.ModuleType("nmspy.globals"),
        "pymhf": types.ModuleType("pymhf"),
        "pymhf.core": types.ModuleType("pymhf.core"),
        "pymhf.core.hooking": types.ModuleType("pymhf.core.hooking"),
        "pymhf.core.memutils": types.ModuleType("pymhf.core.memutils"),
    }
    modules["nmspy.common"].gameData = _Stub("gameData")
    modules["nmspy.data"].types = _Stub("types")
    modules["nmspy.data.audiokinetic"].AK = _Stub("AK")
    modules["nmspy.data.enums.external_enums"].cGcAudioWwiseEvents = _Stub("events")
    modules["nmspy.globals"].globals = _Stub("globals")
    modules["pymhf"].Mod = _StubMod
    modules["pymhf.core.hooking"].on_key_pressed = _Stub("on_key_pressed")
    modules["pymhf.core.memutils"].get_addressof = _Stub("get_addressof")
    modules["pymhf.core.memutils"].map_struct = _Stub("map_struct")

    installed = []
    for name, module in modules.items():
        sys.modules[name] = module
        installed.append(name)
    return installed


class ModImportTests(unittest.TestCase):
    """One test class, because the framework stubs and the temp copy are process-global state -
    sys.modules and sys.path - and undoing them between tests buys nothing a fresh process
    wouldn't already give the next test file."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="fishing_mod_import_test_")
        for relative in MOD_FILES:
            source = REPO / relative
            dest = Path(cls._tmp) / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)

        cls._installed_modules = _install_framework_stubs()
        sys.path.insert(0, cls._tmp)
        import fishing_minigame_mod  # noqa: E402  (import after the stubs are in place)

        cls.mod_module = fishing_minigame_mod

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls._tmp)
        for name in cls._installed_modules:
            sys.modules.pop(name, None)
        sys.modules.pop("fishing_minigame_mod", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_the_module_imports(self):
        # The point of the whole file: this is the line that used to raise NameError.
        self.assertTrue(hasattr(self.mod_module, "FishingMinigameMod"))

    def test_the_mod_constructs(self):
        # Runs __init__, which reads mod_settings.ini and presets - the other place this class
        # of bug hides, since a broken default or a bad file read fails here, not at import.
        instance = self.mod_module.FishingMinigameMod()
        self.assertIsNotNone(instance)

    def test_every_panel_key_is_bound(self):
        instance = self.mod_module.FishingMinigameMod()
        expected = {
            "move_panel_left", "move_panel_right", "move_panel_up", "move_panel_down",
            "mirror_panel", "reset_panel", "cycle_theme", "toggle_settings_screen",
        }
        present = {name for name in expected if hasattr(instance, name)}
        self.assertEqual(expected, present)

    def test_every_dev_hotkey_is_bound(self):
        instance = self.mod_module.FishingMinigameMod()
        expected = {"toggle_difficulty_preset", "toggle_physics_preset", "toggle_stamina_lock"}
        present = {name for name in expected if hasattr(instance, name)}
        self.assertEqual(expected, present)

    def test_the_runtime_import_failure_is_handled_not_raised(self):
        # oreo_runtime is not installed in this test environment. A mod that raised instead of
        # disabling itself would take every other mod down with it - see KNOWN_ISSUES upstream.
        self.assertIsNone(self.mod_module.oreo_runtime)
        instance = self.mod_module.FishingMinigameMod()
        self.assertFalse(instance._ensure_runtime())

    def test_importing_leaves_no_log_files_in_the_real_repo(self):
        # The reason this whole file copies itself into a temp directory rather than importing
        # REPO/fishing_minigame_mod.py directly.
        real_logs = REPO / "logs"
        stray = [p for p in real_logs.glob("*.log")]
        self.assertEqual([], stray, f"import wrote logs into the repo: {stray}")


if __name__ == "__main__":
    unittest.main()
