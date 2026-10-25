# /// script
# dependencies = ["pymhf>=0.2.4"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
# interactive_console = false
#
# [tool.pymhf.gui]
# shown = false
#
# [tool.pymhf.logging]
# log_dir = "logs"
# log_level = "info"
# shown = false
# ///

import configparser
import ctypes
import importlib.metadata
import json
import logging
import logging.handlers
import math
import os
import random
import re
import sys
import threading
import time

from nmspy.common import gameData
from nmspy.data import types as nms
from nmspy.data.audiokinetic import AK
from nmspy.data.enums.external_enums import cGcAudioWwiseEvents
from nmspy.globals import globals as nms_globals
from pymhf import Mod
from pymhf.core.hooking import on_key_pressed
from pymhf.core.memutils import get_addressof, map_struct

logger = logging.getLogger(__name__)

# ---- config -------------------------------------------------------------------------
# Every game address, hook and struct layout comes from NMSpy. The mod carries none of
# its own, so a game patch cannot silently point it at the wrong memory.
FREEZE_VALUE = 999999.0
FREEZE_RUNTIME = True               # Stretch the native chase so the minigame decides the outcome.
VK_R = 0x52
OREO_RUNTIME_URL = "https://github.com/Carbonster/ProjectOreoRuntime"

# The mod's interface: RML documents and RCSS styles in ui/, HTML and CSS as RmlUi reads them.
# How the panel looks lives there; this file only says what the values are. UI_NAME is the data
# model the documents bind to, as in <body data-model="fishing">.
UI_NAME = "fishing"
UI_DOCUMENTS = {"catch": "ui/catch.rml", "settings": "ui/settings.rml"}

# ProductID to English display name. Unknown IDs fall back to the raw ProductID.
FISH_NAMES_FILE = "fish_names.json"
PRESETS_FILE = "fishing_presets.json"
UI_VISIBILITY_FILE = "ui_visibility.json"
# Junk catches use one generic display name.
UNKNOWN_DISPLAY_NAME = "UNKNOWN"

# cGcFishLaser mode for a successful catch.
#
# NMSpy names four of the eight modes (Inactive 0, Error 2, Chase 5, Escaped 7). This one it
# does not name, but the game handles it like any other: SetMode(6) runs the real catch, with
# the sound, the animation and the fish in the inventory. Verified in game, mod 0.3.1.
#
# An enum value, not an address - it does not move when the game is patched the way an offset
# does. Worth sending upstream so it stops being a bare number here.
MODE_CAUGHT = 6


def _enum_int(value):
    """Detour arguments typed as c_enum32 arrive either as a ctypes instance or a plain int."""
    return getattr(value, "value", value)


def _mod_dir():
    # Directory of this mod file (pymhf runs the .bat with cwd = mod dir, but resolve from
    # __file__ so the data file loads regardless of how the mod is launched).
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


def _sanitize_log_text(value):
    """Remove user/machine-identifying path fragments from support logs."""
    text = str(value)
    game_dir = os.path.abspath(os.path.join(_mod_dir(), "..", "..", ".."))
    replacements = [
        (_mod_dir(), "<MOD_DIR>"),
        (game_dir, "<GAME_DIR>"),
        (os.environ.get("LOCALAPPDATA"), "<LOCALAPPDATA>"),
        (os.environ.get("APPDATA"), "<APPDATA>"),
        (os.environ.get("USERPROFILE"), "<USERPROFILE>"),
        (os.environ.get("TEMP"), "<TEMP>"),
        (os.environ.get("TMP"), "<TEMP>"),
        (os.environ.get("USERNAME"), "<USER>"),
        (os.environ.get("COMPUTERNAME"), "<HOST>"),
    ]
    for private, replacement in sorted(
            ((p, r) for p, r in replacements if p), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(re.escape(private), replacement, text, flags=re.IGNORECASE)
    return text


class _PrivacyFormatter(logging.Formatter):
    def format(self, record):
        # Formatting first also sanitizes traceback filenames and exception messages.
        return _sanitize_log_text(super().format(record))


def _configure_mod_logging():
    """Configure bounded component logs and preserve one previous session."""
    global LOG_MODE, LOG_MAX_BYTES, LOG_KEEP_PREVIOUS, TRACE_UNTIL
    global LOG_MAX_MB, LOG_TRACE_MINUTES
    creator_cfg = configparser.ConfigParser()
    try:
        creator_cfg.read(os.path.join(_mod_dir(), "fishing_mod.ini"), encoding="utf-8")
    except (OSError, configparser.Error):
        creator_cfg = configparser.ConfigParser()
    LOG_MODE = creator_cfg.get("Logging", "Mode", fallback="normal").strip().lower()
    if LOG_MODE not in ("normal", "debug", "trace"):
        LOG_MODE = "normal"
    try:
        max_mb = min(50, max(1, creator_cfg.getint("Logging", "MaxFileMB", fallback=10)))
    except ValueError:
        max_mb = 10
    LOG_MAX_BYTES = max_mb * 1024 * 1024
    # Kept as the plain number too. The Runtime's native components are told these settings
    # rather than reading them out of this file themselves - a component shared by every mod
    # has no business knowing one mod filename. See oreo_runtime.start().
    LOG_MAX_MB = max_mb
    try:
        LOG_KEEP_PREVIOUS = creator_cfg.getboolean("Logging", "KeepPrevious", fallback=True)
    except ValueError:
        LOG_KEEP_PREVIOUS = True
    try:
        trace_minutes = min(60, max(1, creator_cfg.getint("Logging", "TraceMinutes", fallback=10)))
    except ValueError:
        trace_minutes = 10
    LOG_TRACE_MINUTES = trace_minutes
    TRACE_UNTIL = time.monotonic() + trace_minutes * 60 if LOG_MODE == "trace" else 0.0

    log_dir = os.path.join(_mod_dir(), "logs")
    previous_dir = os.path.join(log_dir, "previous")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(previous_dir, exist_ok=True)
    mod_target = os.path.abspath(os.path.join(log_dir, "mod.log"))
    framework_target = os.path.abspath(os.path.join(log_dir, "framework.log"))
    root = logging.getLogger()

    framework_sources = []
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            source = getattr(handler, "baseFilename", None)
            if source:
                framework_sources.append(os.path.abspath(source))
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    def rotate_start(path):
        if not os.path.isfile(path):
            return
        if LOG_KEEP_PREVIOUS:
            os.replace(path, os.path.join(previous_dir, os.path.basename(path)))
        else:
            os.remove(path)

    rotate_start(mod_target)
    rotate_start(framework_target)

    # Preserve pyMHF/NMSpy startup output produced before this module was imported. Without this,
    # replacing pyMHF's timestamped handler would erase the most useful framework diagnostics.
    with open(framework_target, "a", encoding="utf-8") as framework_out:
        for source in framework_sources:
            if source in (mod_target, framework_target) or not os.path.isfile(source):
                continue
            try:
                with open(source, "r", encoding="utf-8", errors="replace") as source_file:
                    framework_out.write(_sanitize_log_text(source_file.read()))
            except OSError:
                pass

    class ComponentFilter(logging.Filter):
        def __init__(self, framework):
            super().__init__()
            self.framework = framework

        def filter(self, record):
            is_framework = record.name in ("pymhf", "nmspy", "pymem") or record.name.startswith(
                ("pymhf.", "nmspy.", "pymem."))
            return is_framework if self.framework else not is_framework

    formatter = _PrivacyFormatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-7s] [%(process)d:%(threadName)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    def add_handler(path, framework):
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
        handler.namer = lambda _name, p=path: os.path.join(previous_dir, os.path.basename(p))
        handler.setFormatter(formatter)
        handler.addFilter(ComponentFilter(framework))
        root.addHandler(handler)

    add_handler(mod_target, False)
    add_handler(framework_target, True)
    root.setLevel({"normal": logging.INFO, "debug": logging.DEBUG, "trace": 5}[LOG_MODE])

    # Runtime components inherit these process-local paths and logging settings.
    os.environ["FISHING_MOD_LOG_DIR"] = log_dir
    os.environ["FISHING_MOD_DIR"] = _mod_dir()
    os.environ["FISHING_MOD_LOG_MODE"] = LOG_MODE
    logger.info(f"logging ready: mode={LOG_MODE} max_file_mb={max_mb} "
                f"keep_previous={LOG_KEEP_PREVIOUS} path={log_dir}")


_configure_mod_logging()

try:
    import oreo_runtime
    _OREO_IMPORT_ERROR = None
except Exception as exc:
    oreo_runtime = None
    _OREO_IMPORT_ERROR = exc

try:
    logger.info("runtime: python=%s pyMHF=%s NMSpy=%s pid=%d",
                sys.version.split()[0], importlib.metadata.version("pymhf"),
                importlib.metadata.version("nmspy"), os.getpid())
except importlib.metadata.PackageNotFoundError as e:
    logger.warning(f"runtime package version unavailable: {e}")

TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")


def _trace(message):
    if LOG_MODE == "trace" and time.monotonic() <= TRACE_UNTIL:
        logger.log(TRACE_LEVEL, message)


_ORIGINAL_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_EXCEPTHOOK = threading.excepthook


def _uncaught_exception(exc_type, exc_value, exc_tb):
    logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    _ORIGINAL_EXCEPTHOOK(exc_type, exc_value, exc_tb)


def _uncaught_thread(args):
    logger.critical(f"uncaught thread exception: {args.thread.name}",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    _ORIGINAL_THREAD_EXCEPTHOOK(args)


sys.excepthook = _uncaught_exception
threading.excepthook = _uncaught_thread


def _settings_path():
    root = os.environ.get("APPDATA") or _mod_dir()
    directory = os.path.join(root, "FishingMod")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "mod_settings.ini")


SETTINGS_DEFAULTS = {
    "Presets": {
        "Physics": "DEFAULT",
        "Difficulty": "preset_6",
    },
    "Hotkeys": {
        "MoveLeft": "F1", "MoveRight": "F2", "MoveUp": "F3", "MoveDown": "F4",
        "Theme": "F5", "Mirror": "F6", "ResetPosition": "F7", "Settings": "F8",
        # F9/F10/F11 used to sit here too - the developer toggles. They are no longer player
        # settings at all; see DEV_HOTKEYS below.
    },
    "Overlay": {"InsetX": "200", "InsetY": "0", "Mirror": "false", "Theme": "Blue"},
}


def _write_settings(path, cfg):
    temp = path + ".new"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            cfg.write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _load_settings():
    path = _settings_path()
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    existed = os.path.isfile(path)
    changed = False
    may_write = True
    if existed:
        try:
            cfg.read(path, encoding="utf-8")
        except (OSError, configparser.Error) as e:
            kept = f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
            logger.error(f"settings read failed: path={path}: {e}; kept as {kept}")
            try:
                os.replace(path, kept)
            except OSError:
                logger.error("settings: could not set the unreadable file aside, leaving it as is")
                may_write = False
            cfg = configparser.ConfigParser()
            cfg.optionxform = str
    for section, defaults in SETTINGS_DEFAULTS.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
            changed = True
        for key, value in defaults.items():
            if not cfg.has_option(section, key):
                cfg.set(section, key, value)
                changed = True
    if changed and may_write:
        try:
            _write_settings(path, cfg)
        except OSError as e:
            logger.error(f"settings write failed: path={path}: {e}")
    logger.info(f"settings loaded: path={path} existed={existed} defaults_added={changed}")
    return cfg


def _save_setting(section, key, value):
    path = _settings_path()
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    try:
        cfg.read(path, encoding="utf-8")
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, key, str(value))
        _write_settings(path, cfg)
        logger.debug(f"setting saved: [{section}] {key}={value} path={path}")
        return True
    except (OSError, configparser.Error) as e:
        logger.error(f"setting save failed: [{section}] {key}={value} path={path}: {e}")
        return False


def _load_presets():
    candidates = (os.path.join(_mod_dir(), "data", PRESETS_FILE),
                  os.path.join(_mod_dir(), PRESETS_FILE))
    path = next((candidate for candidate in candidates if os.path.isfile(candidate)), candidates[0])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != 1:
            raise ValueError("unsupported preset schema_version")
        for section in ("physics_presets", "difficulty_presets"):
            if not isinstance(data.get(section), dict) or not data[section]:
                raise ValueError(f"missing or empty {section}")
        return data
    except Exception as e:
        raise RuntimeError(f"cannot load required {PRESETS_FILE}: {e}") from e


def _load_ui_visibility():
    """Which parts of the panel are on by default - a creator setting, not a player one.

    Split out of fishing_presets.json on its own: it has nothing to do with physics or
    difficulty, it only ever shared a file with them because that file already existed.
    """
    candidates = (os.path.join(_mod_dir(), "data", UI_VISIBILITY_FILE),
                  os.path.join(_mod_dir(), UI_VISIBILITY_FILE))
    path = next((candidate for candidate in candidates if os.path.isfile(candidate)), candidates[0])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != 1:
            raise ValueError("unsupported ui_visibility schema_version")
        groups = data.get("groups")
        if not isinstance(groups, dict) or not groups:
            raise ValueError("missing or empty groups")
        return groups
    except Exception as e:
        raise RuntimeError(f"cannot load required {UI_VISIBILITY_FILE}: {e}") from e


MOD_SETTINGS = _load_settings()
PRESET_DATA = _load_presets()
# Creator-only UI flags: which parts of the panel are on by default. Lives in its own file,
# not in the user's INI - a player does not edit this, a modder packaging a variant does.
UI_VISIBLE = _load_ui_visibility()


def _optional_key(key):
    """Bind a method to a key only when one is named, otherwise leave the method unbound.

    pyMHF's on_key_pressed needs a real key, and an action with no key still has to exist: the
    logic below is intact, it simply has nothing pressing it.
    """
    def decorate(func):
        return on_key_pressed(key)(func) if key else func

    return decorate


def _load_debug_hotkeys():
    """Creator/developer switch shipped with the mod, intentionally not user-persistent."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(os.path.join(_mod_dir(), "fishing_mod.ini"), encoding="utf-8")
        return cfg.getboolean("Debug", "DebugHotkeys", fallback=False)
    except (OSError, ValueError, configparser.Error) as e:
        logger.warning(f"could not read fishing_mod.ini DebugHotkeys: {e}; using false")
        return False


DEBUG_HOTKEYS = _load_debug_hotkeys()
# Developer toggles: cycle the difficulty preset, cycle the physics preset, and hold stamina
# so a catch never ends. They change the game under the player mid-catch, so they ship with no
# key at all - a stray F9 during a fish should not silently rewrite the difficulty.
#
# They are deliberately NOT in mod_settings.ini. Putting them there would mean every install
# that has already run carries the old F9/F10/F11 lines, and unbinding here would change
# nothing for those players. To use them, name a key below ("f9", "f10", "f11") and restart the
# game; DebugHotkeys=true in fishing_mod.ini is still required on top of that.
DEV_HOTKEY_DIFFICULTY_PRESET = ""
DEV_HOTKEY_PHYSICS_PRESET = ""
DEV_HOTKEY_STAMINA_LOCK = ""


# ---- panel placement and theme -----------------------------------------------------------
#
# These keys used to be the overlay's. It read them, moved its own panel, cycled its own
# colours and saved the result - every one of those a decision about how one mod's interface
# looked, made by the renderer that is supposed to draw and decide nothing. They went out with
# the rest of the fishing code.
#
# They come back here, which is where they belonged all along. The mod reads the key, the mod
# remembers where this player put the panel, and the Runtime is told where to draw the box.
# What is inside the box is ui/catch.rml's, and nothing here knows a coordinate in it.
#
# Without them there is no way at all to move the panel off a monitor it does not suit, short
# of editing the file and restarting the game, so this is a regression to fix rather than a
# feature to schedule.


def _hotkey(name, default):
    return MOD_SETTINGS.get("Hotkeys", name, fallback=default).strip().lower()


HOTKEY_MOVE_LEFT = _hotkey("MoveLeft", "F1")
HOTKEY_MOVE_RIGHT = _hotkey("MoveRight", "F2")
HOTKEY_MOVE_UP = _hotkey("MoveUp", "F3")
HOTKEY_MOVE_DOWN = _hotkey("MoveDown", "F4")
HOTKEY_INFO = _hotkey("Info", "F5")
# The colour themes are still there, but no longer on a key by default: F5 used to switch them
# and now shows the fish info. A player can bind Theme to a key of their own.
HOTKEY_THEME = _hotkey("Theme", "")
if HOTKEY_THEME == HOTKEY_INFO:
    HOTKEY_THEME = ""
HOTKEY_MIRROR = _hotkey("Mirror", "F6")
HOTKEY_RESET = _hotkey("ResetPosition", "F7")
HOTKEY_SETTINGS = _hotkey("Settings", "F8")

# Named exactly as the themes in ui/themes.rcss (body.theme-<name>), so cycling is a list index
# and there is no table mapping one set of names onto another to fall out of step.
THEMES = ("Blue", "Sand", "HighContrast", "Colorblind")

# Which binding on the settings screen shows which key. Out here rather than inside the method
# that sends them so the mod's own test suite can read the list and check every name against
# ui/settings.rml - a label bound to nothing draws nothing, silently, and looks like a missing
# feature rather than a typo.
SETTINGS_KEY_LABELS = (
    ("keyMoveLeft", HOTKEY_MOVE_LEFT),
    ("keyMoveRight", HOTKEY_MOVE_RIGHT),
    ("keyMoveUp", HOTKEY_MOVE_UP),
    ("keyMoveDown", HOTKEY_MOVE_DOWN),
    ("keyInfo", HOTKEY_INFO),
    ("keyMirror", HOTKEY_MIRROR),
    ("keyReset", HOTKEY_RESET),
    ("keySettings", HOTKEY_SETTINGS),
)

PANEL_STEP_PX = 50.0
PANEL_DEFAULT_INSET_X = 200.0
PANEL_DEFAULT_INSET_Y = 0.0
PANEL_DEFAULT_MIRROR = False
PANEL_DEFAULT_THEME = THEMES[0]
# How much of the panel the info key shows, in the order it steps through them:
#   minigame - the catch bar and its scale, and the hint; the default
#   info     - plus the column of fish info: name, rarity, difficulty, depth
#   bare     - the catch bar and the hint alone, without the scale either
INFO_MODES = ("minigame", "info", "bare")
INFO_DEFAULT_MODE = INFO_MODES[0]


def _load_info_mode():
    mode = MOD_SETTINGS.get("Overlay", "InfoMode", fallback=INFO_DEFAULT_MODE).strip().lower()
    return mode if mode in INFO_MODES else INFO_DEFAULT_MODE


def _load_panel_placement():
    """Where this player last left the panel, and in which colours."""
    try:
        inset_x = MOD_SETTINGS.getfloat("Overlay", "InsetX", fallback=PANEL_DEFAULT_INSET_X)
        inset_y = MOD_SETTINGS.getfloat("Overlay", "InsetY", fallback=PANEL_DEFAULT_INSET_Y)
    except ValueError:
        inset_x, inset_y = PANEL_DEFAULT_INSET_X, PANEL_DEFAULT_INSET_Y
    try:
        mirror = MOD_SETTINGS.getboolean("Overlay", "Mirror", fallback=PANEL_DEFAULT_MIRROR)
    except ValueError:
        mirror = PANEL_DEFAULT_MIRROR
    theme = MOD_SETTINGS.get("Overlay", "Theme", fallback=PANEL_DEFAULT_THEME).strip()
    if theme not in THEMES:
        logger.warning(f"unknown overlay theme {theme!r}; using {PANEL_DEFAULT_THEME}")
        theme = PANEL_DEFAULT_THEME
    return inset_x, inset_y, mirror, theme


def _load_fish_names():
    for path in (os.path.join(_mod_dir(), "data", FISH_NAMES_FILE),
                 os.path.join(_mod_dir(), FISH_NAMES_FILE),
                 os.path.join(_mod_dir(), "..", FISH_NAMES_FILE)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"loaded {len(data)} fish names from {os.path.abspath(path)}")
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"could not load {path}: {e}; panel will show raw ProductID")
            return {}
    logger.warning(f"could not find {FISH_NAMES_FILE}; panel will show raw ProductID")
    return {}


FISH_NAMES = _load_fish_names()


def display_fish_name(pid):
    # ProductID -> the name shown in the panel. Junk (trash + bottles) collapses to a single
    # generic placeholder; everything else uses its real English name, falling back to the raw
    # ProductID if it is not in the map (new/unknown fish after a game update).
    if not pid:
        return ""
    up = pid.upper()
    if up.startswith("F_TRASH") or up == "F_BOTTLE" or up.startswith("S15_BOT"):
        return UNKNOWN_DISPLAY_NAME
    return FISH_NAMES.get(pid, "")

# --- minigame -------------------------------------------------------------------------

# Fish position bounds (shared across all classes) - keep the fish on-screen with a margin
# from the very top/bottom edge of the column. Two separate margins because the target (AI
# aim point) and the drawn position (target + jitter wobble) were clamped to slightly
# different bounds in the original code.
FISH_DRAW_MARGIN = 0.03     # fish_draw (target + jitter) clamped to [margin, 1-margin]

# Result banner hold time (seconds) before the native catch/fail lever actually fires.
RESULT_BANNER_DELAY = 0.1

# Progress bar bounds (0 = empty/escaped, 1 = full/caught).
PROGRESS_MIN = 0.0
PROGRESS_MAX = 1.0

# Shared initial state. Starting progress belongs to the active physics preset.
INIT_FISH_Y = 0.93   # start near the bottom of the reference track
# Catch bar starts flush with the bottom for the default 0.17 height.
INIT_BAR_Y = 0.915
INIT_BAR_V = 0.0
INIT_FISH_JITTER = 0.0

# Movement constants are normalized from a 548-unit reference track at 60 FPS.
TRACK_PX = 548.0          # the overlay maps this reference track onto normalized 0..1 space
INIT_FISH_V = 0.0         # fish velocity (units/frame at 60fps), integrated into position
INIT_FISH_FSA = 0.0       # floater/sinker drift acceleration (ramps up to a terminal velocity)

# Fish species affect motion, not catch-bar height.
BAR_HEIGHT_DEFAULT = 0.26   # used when the active physics preset does not override barH

# --- physics presets ------------------------------------------------------------------
PHYSICS_PRESETS = PRESET_DATA["physics_presets"]
ACTIVE_PRESET = MOD_SETTINGS.get("Presets", "Physics", fallback="DEFAULT")
if ACTIVE_PRESET not in PHYSICS_PRESETS:
    logger.warning(f"unknown physics preset {ACTIVE_PRESET!r}; using DEFAULT")
    ACTIVE_PRESET = "DEFAULT"
    _save_setting("Presets", "Physics", ACTIVE_PRESET)


def active_physics():
    return PHYSICS_PRESETS[ACTIVE_PRESET]


# --- difficulty presets ---------------------------------------------------------------
TOP_TIER_MIN_QUALITY = 3   # Epic(3)/Legendary(4): the top rarities that top_tier_scale hits
DIFFICULTY_PRESETS = PRESET_DATA["difficulty_presets"]
ACTIVE_DIFFICULTY = MOD_SETTINGS.get("Presets", "Difficulty", fallback="preset_6")
if ACTIVE_DIFFICULTY not in DIFFICULTY_PRESETS:
    logger.warning(f"unknown difficulty preset {ACTIVE_DIFFICULTY!r}; using preset_6")
    ACTIVE_DIFFICULTY = "preset_6"
    _save_setting("Presets", "Difficulty", ACTIVE_DIFFICULTY)


def active_difficulty():
    return DIFFICULTY_PRESETS[ACTIVE_DIFFICULTY]


# --- fish movement --------------------------------------------------------------------
# Edge hits reflect 35% of velocity and clear pattern acceleration.
FISH_EDGE_BOUNCE = 0.35

# Fish movement patterns.
MOTION_SMOOTH, MOTION_MIXED, MOTION_DART, MOTION_SINK, MOTION_FLOATER = range(5)
MOTION_NAMES = {0: "smooth", 1: "mixed", 2: "dart", 3: "sink", 4: "floater"}

# Mapping from NMS fish data to movement parameters:
#   difficulty = base by size + bonus by quality
FISH_SIZE_BASE_DIFFICULTY = {0: 20.0, 1: 40.0, 2: 65.0, 3: 85.0}   # size 0..3 (S/M/L/XL)
# NMS "quality" @0x44 is the cGcItemQuality rarity tier, 0..4:
#   0=Junk 1=Common 2=Rare 3=Epic 4=Legendary. Rarer -> harder.
FISH_QUALITY_DIFFICULTY_BONUS = {0: 0.0, 1: 10.0, 2: 20.0, 3: 30.0, 4: 40.0}
# Rarity names shown in the panel.
QUALITY_RARITY_NAME = {0: "JUNK", 1: "COMMON", 2: "RARE", 3: "EPIC", 4: "LEGENDARY"}
# Difficulty shown in the panel as a %: diff / this * 100. 110 = the movement ceiling (where
# build_fish_parameters saturates at this value, so 100% is maximally erratic movement.
DIFFICULTY_DISPLAY_MAX = 110.0
DEFAULT_FISH_DIFFICULTY = 45.0                              # if size/quality is unreadable
#   motionType by size; rare-and-up fish become the trickiest (FLOATER)
SIZE_MOTION_PATTERN = {0: MOTION_SMOOTH, 1: MOTION_MIXED, 2: MOTION_SINK, 3: MOTION_DART}
# Alternate mapping (motion_mode="rarity") ties movement patterns to rarity instead of size.
# Each tier has a list of allowed motions; the actual one is picked deterministically per species
# (seeded by ProductID), so a given fish always moves the same way but different species in the
# same tier vary. Commons stay calm (no dart/sink/floater); character escalates with rarity.
# Keyed by cGcItemQuality (1..4).
QUALITY_MOTION_PATTERNS = {
    1: [MOTION_SMOOTH, MOTION_MIXED],   # Common - calm
    2: [MOTION_MIXED, MOTION_SINK],     # Rare
    3: [MOTION_SINK, MOTION_DART],      # Epic
    4: [MOTION_DART, MOTION_FLOATER],   # Legendary
}
TOP_QUALITY_MOTION = MOTION_FLOATER
FLOATER_MIN_QUALITY = 3   # quality (rarity) at/above which a fish uses FLOATER: 3=Epic, 4=Legendary
# Trash/junk (quality == Junk, e.g. a boot) is NOT an active fish - it must never move like one
# (no dart/sink/floater). It gets a calm, low-difficulty smooth motion regardless of its size.
QUALITY_JUNK = 0            # cGcItemQuality.Junk
JUNK_DIFFICULTY = 12.0       # easy: junk barely moves
JUNK_MOTION = MOTION_SMOOTH


def fish_descriptor(size, quality, motion_mode="size", pid=""):
    """NMS (size, quality) -> (difficulty float, motionType int). Pure balance formula.
    motion_mode: "size" = motion by size (default); "rarity" = motion by rarity (commons calm,
    picked from the tier's list seeded by pid so each species is consistent but the tier varies)."""
    # Trash/junk first: not an active fish, so ignore size entirely (a big boot is still a boot).
    if quality == QUALITY_JUNK:
        return JUNK_DIFFICULTY, JUNK_MOTION
    diff = (FISH_SIZE_BASE_DIFFICULTY.get(size, DEFAULT_FISH_DIFFICULTY)
            + FISH_QUALITY_DIFFICULTY_BONUS.get(quality, 0.0))
    if motion_mode == "rarity":
        choices = QUALITY_MOTION_PATTERNS.get(quality, [MOTION_MIXED])
        motion = random.Random(pid).choice(choices)   # deterministic per fish species
    elif quality is not None and quality >= FLOATER_MIN_QUALITY:
        motion = TOP_QUALITY_MOTION
    else:
        motion = SIZE_MOTION_PATTERN.get(size, MOTION_MIXED)
    return diff, motion


def build_fish_parameters(diff, motion):
    """Build the values used by the inertial movement tick."""
    d = max(0.0, min(diff, 110.0)) / 110.0   # 0..1
    return dict(
        difficulty=max(0.0, min(diff, 110.0)),  # raw difficulty used by the movement formulas
        motion=motion,                          # drives smooth/dart/floater/sinker behavior
        jamp=0.006 + 0.012 * d,
        jfreq=2.0 + 3.5 * d,
    )

# Wwise events that mark fishing phases (from cGcAudioWwiseEvents).
EV_CAST = cGcAudioWwiseEvents.PL_FOLEY_FISHING_CASTOFF
EV_CHASE = cGcAudioWwiseEvents.PL_FISHING_LINE_PULL
EV_REEL = cGcAudioWwiseEvents.PL_FOLEY_FISHING_REEL_IN
EV_CHASE_END = cGcAudioWwiseEvents.PL_FISHING_LINE_PULL_STOP
EV_CAUGHT = cGcAudioWwiseEvents.FISHCAUGHT

user32 = ctypes.windll.user32
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

# Input snapshot logged with each fishing state transition.
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_SPACE = 0x20
VK_ESCAPE = 0x1B


def _key_down(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def input_snapshot():
    parts = []
    if _key_down(VK_LBUTTON):
        parts.append("LMB")
    if _key_down(VK_RBUTTON):
        parts.append("RMB")
    if _key_down(VK_MBUTTON):
        parts.append("MMB")
    if _key_down(VK_R):
        parts.append("R")
    if _key_down(VK_SPACE):
        parts.append("SPACE")
    if _key_down(VK_ESCAPE):
        parts.append("ESC")
    if controller_push_down():
        parts.append("PAD")
    return "+".join(parts) if parts else "(nothing)"


# ---- Project Oreo Runtime input -----------------------------------------------------
PAD_PUSH_BUTTON = "a"       # SDL "A" = bottom face button (Cross on PS, A on Xbox) - the bar control
_oreo_session = None


def controller_push_down():
    return bool(_oreo_session and _oreo_session.controller.is_pressed(PAD_PUSH_BUTTON))


def current_fish():
    # The fish on the hook right now, or None. NMSpy walks the chain for us:
    # cGcApplication -> Data -> mSimulation -> mFishManager -> mCurrentFish.
    simulation = gameData.simulation
    if simulation is None:
        return None
    fish = simulation.mFishManager.mCurrentFish
    if not fish:
        return None
    return fish.contents


# ---- mod -----------------------------------------------------------------------------
def rarity_from_pid(pid):
    # NMS fish ProductIDs self-document rarity in the id (e.g. F_ALL_RARE_M2, F_RAD_COM_S1,
    # F_TRASH_7). Parse it out; return "" if the id is missing/unreadable so callers can fall back.
    if not pid or not isinstance(pid, str):
        return ""
    up = pid.upper()
    if "TRASH" in up:
        return "TRASH"
    if "LEGEND" in up:
        return "LEGENDARY"
    if "EPIC" in up:
        return "EPIC"
    if "RARE" in up:
        return "RARE"
    if "COM" in up:
        return "COMMON"
    return ""


class _UnavailableOverlay:
    available = False
    connected = False
    error = "Project Oreo Runtime has not started."

    @staticmethod
    def send(_message):
        return False


class _NoUi:
    """Stands in until the Runtime hands us a real one, and after it fails to.

    Every drawing call in the mod goes through this object, so the gameplay code never has to
    ask whether an interface exists. A mod whose overlay did not start still plays: the fish
    still runs, the catch still happens, there is simply nothing on screen.
    """

    @staticmethod
    def set(_name, _value):
        pass

    @staticmethod
    def show(_document, modal=False):
        pass

    @staticmethod
    def hide(_document):
        pass

    @staticmethod
    def flush():
        return False


# Each rarity has its own pill in ui/catch.rml, sized to its own word; `rarity` picks one.
RARITY_WORDS = ("TRASH", "JUNK", "COMMON", "RARE", "EPIC", "LEGENDARY", "UNKNOWN")

# The switches of `ui_visible`, by the names the presets file has always used. Each one is a
# value the catch panel binds to - show_title, show_fish - and the panel decides what it covers.
UI_GROUPS = ("title", "fishname", "rarity", "ticks", "catchbar", "fish", "progress", "hint",
             "depth", "difficulty")


class FishingMinigameMod(Mod):
    __author__ = "Carbonster"
    __description__ = "Fishing minigame for No Man's Sky using Project Oreo Runtime"
    __version__ = "0.6.0"

    def __init__(self):
        super().__init__()
        self.pipe = _UnavailableOverlay()
        self.ui = _NoUi()
        # Whether the fish is inside the bar, as last told to the panel, which swaps a class on it.
        self._ui_inside = None
        self._inset_x, self._inset_y, self._mirror, self._theme = _load_panel_placement()
        self._info_mode = _load_info_mode()
        self._settings_open = False
        # A preview of the catch panel, shown while the settings screen is open and nothing is
        # actually being caught - the same trick the overlay used to do for itself with a fake
        # minigame (g_selftest) before the panel moved into this file. Lets a modder, or anyone
        # editing ui/catch.rml, see the panel move without having to go fish for it.
        self._demo_active = False
        self._demo_since = 0.0
        self._demo_progress = 0.0
        self._runtime_attempted = False
        self._runtime_disabled = False
        self.armed = False
        self.in_chase = False
        self.laser = None          # cGcFishLaser for the current attempt (from OnModeChanged)
        # minigame physics state (0=top,1=bottom)
        self.fish_y = INIT_FISH_Y
        self.fish_target = INIT_FISH_Y
        self.fish_jitter = INIT_FISH_JITTER
        self.fish_draw = INIT_FISH_Y
        # Inertial fish movement state.
        self.fish_v = INIT_FISH_V            # velocity, integrated into fish_y
        self.fish_fsa = INIT_FISH_FSA        # floater/sinker drift acceleration
        self.fish_targeting = True           # False means the fish is coasting on inertia
        self.bar_y = INIT_BAR_Y
        self.bar_v = INIT_BAR_V
        self.progress = active_physics()["start_progress"]
        self.result = 0            # 0=playing, 1=won, 2=lost
        self.result_timer = 0.0
        self.applied = False
        self._fish_pid = ""       # real ProductID of the current fish (raw, e.g. "F_ALL_RARE_M2")
        self._fish_name = ""      # human-readable English name shown in the panel
        self._fish_rarity = ""    # rarity parsed from the ProductID
        self._rarity_shown = "UNKNOWN"
        self._stamina_lock = False
        self._fish_parameters = build_fish_parameters(DEFAULT_FISH_DIFFICULTY, MOTION_MIXED)
        self._prev_lmb = False
        self._prev_rmb = False
        self._prev_r = False
        self._prev_space = False
        self._prev_pad_a = False
        self._active_device = "keyboard"
        logger.info(f"FishingMinigameMod loaded (physics={ACTIVE_PRESET}; "
                    f"difficulty={ACTIVE_DIFFICULTY}; debug_hotkeys={DEBUG_HOTKEYS}; "
                    "input=keyboard/controller).")

    def _send_ui_visibility(self):
        for group in UI_GROUPS:
            self.ui.set(f"show_{group}", UI_VISIBLE.get(group, True))
        # The divider sits between difficulty and depth, so it only makes sense with both.
        self.ui.set("show_rule", UI_VISIBLE.get("difficulty", True) and UI_VISIBLE.get("depth", True))
        self._apply_info_mode()
        self._set_hint(self._active_device)
        self._set_rarity(self._rarity_shown)
        self._ui_inside = None   # forget which variant was showing; it is being re-decided
        self._set_inside(False)
        self.ui.flush()

    def _set_hint(self, device):
        """The keyboard hint, or the controller one with its drawn button."""
        self.ui.set("pad", device == "controller")

    def _set_rarity(self, word):
        """Show the pill for this rarity; the panel hides the others."""
        self._rarity_shown = word if word in RARITY_WORDS else "UNKNOWN"
        self.ui.set("rarity", self._rarity_shown)

    def _set_inside(self, inside):
        """Swap the panel between its idle and its success colours, on the frame it changes."""
        if inside == self._ui_inside:
            return
        self._ui_inside = inside
        self.ui.set("inside", bool(inside))

    def _on_overlay_connected(self):
        logger.info("Connected to Project Oreo overlay.")

    def _ensure_runtime(self):
        global _oreo_session
        if self._runtime_disabled:
            return False
        if _oreo_session is not None:
            return _oreo_session.overlay.available
        if self._runtime_attempted:
            return False
        self._runtime_attempted = True

        if oreo_runtime is None:
            logger.error(
                "Project Oreo Runtime is required but could not be imported: %s. Install it from %s",
                _OREO_IMPORT_ERROR,
                OREO_RUNTIME_URL,
            )
            self._runtime_disabled = True
            return False

        try:
            log_dir = os.path.join(_mod_dir(), "logs")
            session = oreo_runtime.start(
                overlay=True,
                controller=True,
                mod_dir=_mod_dir(),
                log_dir=log_dir,
                log_mode=LOG_MODE,
                log_max_mb=LOG_MAX_MB,
                log_keep_previous=LOG_KEEP_PREVIOUS,
                log_trace_minutes=LOG_TRACE_MINUTES,
                ui=UI_NAME,
                on_overlay_connected=self._on_overlay_connected,
            )
            _oreo_session = session
            self.pipe = session.overlay
            if session.ui is not None:
                self.ui = session.ui
                for document, path in UI_DOCUMENTS.items():
                    self.ui.load(document, path)
                # Every value the documents bind to, said once: the Runtime keeps them and says
                # them again itself whenever the overlay reconnects.
                self.ui.set("fishName", "")
                self.ui.set("difficulty", "")
                self.ui.set("fishY", 0.5)
                self.ui.set("barY", 0.5)
                self.ui.set("barH", 0.0)
                self.ui.set("progress", 0.0)
                self._send_ui_visibility()
                self.ui.set("theme", self._theme)
                self._apply_placement()
                self._send_settings_labels()
            runtime_version = getattr(oreo_runtime, "__version__", "unknown")
            logger.info("Project Oreo Runtime started: version=%s", runtime_version)
            if not session.overlay.available:
                logger.error(
                    "Project Oreo overlay is unavailable: %s. Fishing Minigame Mod is disabled.",
                    session.overlay.error,
                )
                self._runtime_disabled = True
                return False
            if not session.controller.available:
                logger.warning(
                    "Controller support is unavailable: %s. Keyboard controls remain active.",
                    session.controller.error,
                )
            return True
        except Exception:
            logger.exception(
                "Project Oreo Runtime failed to start. Fishing Minigame Mod is disabled. "
                "Install or update Runtime from %s",
                OREO_RUNTIME_URL,
            )
            self._runtime_disabled = True
            return False

    def _arm(self, quiet=False):
        fg = nms_globals.GcFishingGlobals
        if fg is None:
            if not quiet:
                logger.warning("GcFishingGlobals not mapped yet; will retry.")
            return False
        if FREEZE_RUNTIME:
            try:
                for i in range(4):
                    fg.ChaseTimes[i] = FREEZE_VALUE
                readback = [round(fg.ChaseTimes[i], 1) for i in range(4)]
                logger.info(f"froze ChaseTimes[0..3] -> readback={readback}")
                logger.debug(f"GcFishingGlobals address=0x{ctypes.addressof(fg):X}")
            except Exception as e:
                logger.warning(f"Could not write ChaseTimes: {e}")  # always log, freeze correctness matters
        self.armed = True
        return True

    # ---- the panel's place on screen ------------------------------------------------------
    #
    # Everything below reads a key, changes one number, saves it and tells the Runtime where
    # the box goes. No coordinate inside the panel appears anywhere here; that is the file's,
    # and it stays the file's.

    def _panel_anchor(self):
        return "left" if self._mirror else "right"

    def _apply_placement(self):
        self.ui.set("insetX", self._inset_x)
        self.ui.set("insetY", self._inset_y)
        self.ui.set("anchor", self._panel_anchor())
        self.ui.flush()

    def _save_placement(self):
        _save_setting("Overlay", "InsetX", int(self._inset_x))
        _save_setting("Overlay", "InsetY", int(self._inset_y))
        _save_setting("Overlay", "Mirror", "true" if self._mirror else "false")

    def _send_settings_labels(self):
        """Fill in the settings screen: which key does what, and whether the fish info is on.

        Text, not layout: ui/settings.rml says where these sit and how they look, and this only
        supplies the words that depend on what the player configured.
        """
        for name, key in SETTINGS_KEY_LABELS:
            self.ui.set(name, key.upper() if key else "UNBOUND")
        self.ui.set("infoAction", f"FISH INFO AND SCALE (NOW: {self._info_mode.upper()})")
        self.ui.flush()

    @_optional_key(HOTKEY_MOVE_LEFT)
    def move_panel_left(self):
        # Mirrored, the inset is measured from the other edge, so the same key has to move the
        # number the other way to move the panel the way it says on the settings screen.
        self._inset_x += -PANEL_STEP_PX if self._mirror else PANEL_STEP_PX
        self._apply_placement()
        self._save_placement()
        logger.info(f"panel moved left: inset_x={self._inset_x} mirror={self._mirror}")

    @_optional_key(HOTKEY_MOVE_RIGHT)
    def move_panel_right(self):
        self._inset_x += PANEL_STEP_PX if self._mirror else -PANEL_STEP_PX
        self._apply_placement()
        self._save_placement()
        logger.info(f"panel moved right: inset_x={self._inset_x} mirror={self._mirror}")

    @_optional_key(HOTKEY_MOVE_UP)
    def move_panel_up(self):
        self._inset_y -= PANEL_STEP_PX
        self._apply_placement()
        self._save_placement()
        logger.info(f"panel moved up: inset_y={self._inset_y}")

    @_optional_key(HOTKEY_MOVE_DOWN)
    def move_panel_down(self):
        self._inset_y += PANEL_STEP_PX
        self._apply_placement()
        self._save_placement()
        logger.info(f"panel moved down: inset_y={self._inset_y}")

    @_optional_key(HOTKEY_MIRROR)
    def mirror_panel(self):
        """Flip to the other edge, keeping the margin.

        The inset is deliberately left alone: a panel 200px in from the right belongs 200px in
        from the left, not at whatever absolute position 200 happens to mean over there.
        """
        self._mirror = not self._mirror
        self._apply_placement()
        self._save_placement()
        logger.info(f"panel mirrored: anchor={self._panel_anchor()}")

    @_optional_key(HOTKEY_RESET)
    def reset_panel(self):
        self._inset_x = PANEL_DEFAULT_INSET_X
        self._inset_y = PANEL_DEFAULT_INSET_Y
        self._mirror = PANEL_DEFAULT_MIRROR
        self._apply_placement()
        self._save_placement()
        logger.info("panel reset to its default position")

    def _apply_info_mode(self):
        self.ui.set("show_info", self._info_mode == "info")
        self.ui.set("show_ticks", UI_VISIBLE.get("ticks", True) and self._info_mode != "bare")

    @_optional_key(HOTKEY_INFO)
    def toggle_info(self):
        self._info_mode = INFO_MODES[(INFO_MODES.index(self._info_mode) + 1) % len(INFO_MODES)]
        self._apply_info_mode()
        self._send_settings_labels()
        _save_setting("Overlay", "InfoMode", self._info_mode)
        logger.info(f"info mode -> {self._info_mode}")

    @_optional_key(HOTKEY_THEME)
    def cycle_theme(self):
        self._theme = THEMES[(THEMES.index(self._theme) + 1) % len(THEMES)]
        self.ui.set("theme", self._theme)
        self._send_settings_labels()
        _save_setting("Overlay", "Theme", self._theme)
        logger.info(f"theme -> {self._theme}")

    @_optional_key(HOTKEY_SETTINGS)
    def toggle_settings_screen(self):
        self._settings_open = not self._settings_open
        if self._settings_open:
            self._send_settings_labels()
            if not self.in_chase:
                self._start_demo_panel()
        elif self._demo_active:
            self._stop_demo_panel()
        if self._settings_open:
            self.ui.show("settings")
        else:
            self.ui.hide("settings")
        self.ui.flush()
        logger.debug(f"settings screen -> {self._settings_open}")

    def _start_demo_panel(self):
        """Fake fish, fake bar, so the catch panel can be previewed without going fishing."""
        self._demo_active = True
        self._demo_since = time.monotonic()
        self._demo_progress = active_physics()["start_progress"]
        self.ui.set("fishName", "TEST FISH")
        self._set_rarity("RARE")
        self.ui.set("difficulty", "50%")
        self._set_inside(False)
        self.ui.show("catch")
        self.ui.flush()

    def _stop_demo_panel(self):
        self._demo_active = False
        self.ui.hide("catch")
        self.ui.flush()

    def _update_demo_panel(self):
        tt = time.monotonic() - self._demo_since
        fish_y = 0.5 + 0.40 * math.sin(tt * 1.3)
        bar_y = 0.5 + 0.30 * math.sin(tt * 0.9 + 1.0)
        bar_h = 0.26
        inside = abs(fish_y - bar_y) < 0.13
        self._demo_progress = max(0.0, min(1.0, self._demo_progress + (0.006 if inside else -0.004)))
        self._set_inside(inside)
        self.ui.set("fishY", fish_y)
        self.ui.set("barY", bar_y)
        self.ui.set("barH", bar_h)
        self.ui.set("progress", self._demo_progress)
        self.ui.flush()

    @_optional_key(DEV_HOTKEY_DIFFICULTY_PRESET)
    def toggle_difficulty_preset(self):
        global ACTIVE_DIFFICULTY
        if not DEBUG_HOTKEYS:
            return
        names = list(DIFFICULTY_PRESETS.keys())
        ACTIVE_DIFFICULTY = names[(names.index(ACTIVE_DIFFICULTY) + 1) % len(names)]
        _save_setting("Presets", "Difficulty", ACTIVE_DIFFICULTY)
        self._send_dev_settings()
        logger.info(f"difficulty preset -> {ACTIVE_DIFFICULTY}")

    @_optional_key(DEV_HOTKEY_PHYSICS_PRESET)
    def toggle_physics_preset(self):
        global ACTIVE_PRESET
        if not DEBUG_HOTKEYS:
            return
        names = list(PHYSICS_PRESETS.keys())
        ACTIVE_PRESET = names[(names.index(ACTIVE_PRESET) + 1) % len(names)]
        _save_setting("Presets", "Physics", ACTIVE_PRESET)
        self._send_dev_settings()
        logger.info(f"physics preset -> {ACTIVE_PRESET}")

    @_optional_key(DEV_HOTKEY_STAMINA_LOCK)
    def toggle_stamina_lock(self):
        if not DEBUG_HOTKEYS:
            return
        self._stamina_lock = not self._stamina_lock
        self._send_dev_settings()
        logger.info(f"stamina lock -> {self._stamina_lock}")

    # cGcFishLaser.OnModeChanged supplies the current attempt object and the mode transition.
    @nms.cGcFishLaser.OnModeChanged.before
    def on_fishing_mode_changed(self, this, mePrevMode, meNewMode):
        try:
            chase = nms.cGcFishLaser.eMode.Chase
            old_state = _enum_int(mePrevMode)
            new_state = _enum_int(meNewMode)
            logger.info(f"STATE {old_state}->{new_state} in_chase={self.in_chase} "
                        f"inputs={input_snapshot()}")
            address = get_addressof(this)
            logger.debug(f"STATE address: cGcFishLaser=0x{address:X}")
            if new_state == chase and not self.in_chase:
                # A real catch always wins over the demo preview - it never gets to fight the
                # real panel for the same bindings.
                self._demo_active = False
                self.laser = map_struct(address, nms.cGcFishLaser)
                self.in_chase = True
                self._on_chase_start()
            elif old_state == chase and new_state != chase and self.in_chase:
                self.in_chase = False
                self.ui.hide("catch")
                self.ui.flush()
        except Exception as e:
            logger.warning(f"mode-changed hook error: {e}")

    @AK.SoundEngine.PostEvent.before
    def on_sound(self, in_ulEventID, in_GameObjID, *args):
        if not self.armed:
            self._arm()

        if in_ulEventID == EV_CAST:
            self._arm(quiet=True)

    def _on_chase_start(self):
        # Read the current fish.
        fish = current_fish()
        pid, quality, size = "?", -1, -1
        if fish is not None:
            try:
                pid = fish.ProductID.value.decode("ascii", "replace")
                quality = fish.Quality.value
                size = fish.Size.value
            except Exception as e:
                logger.warning(f"fish read failed: {e}")
        if size not in FISH_SIZE_BASE_DIFFICULTY:
            logger.warning(f"fish read looked invalid (id={pid!r} size={size}); "
                           "using default movement difficulty")
            logger.debug(f"invalid fish address=0x{get_addressof(fish) if fish is not None else 0:X}")
        pid_ok = bool(pid) and pid != "?" and all(32 <= ord(ch) < 127 for ch in pid)
        self._fish_pid = pid if pid_ok else ""
        self._fish_name = display_fish_name(self._fish_pid)
        if quality in QUALITY_RARITY_NAME:
            self._fish_rarity = QUALITY_RARITY_NAME[quality]
        elif pid_ok:
            self._fish_rarity = rarity_from_pid(pid)
        else:
            self._fish_rarity = ""
        s = size if size in FISH_SIZE_BASE_DIFFICULTY else None
        q = quality if quality in FISH_QUALITY_DIFFICULTY_BONUS else None
        dp = active_difficulty()
        raw_diff, self._fish_motion = fish_descriptor(s, q, dp["motion_mode"], self._fish_pid)
        scaled = raw_diff * dp["diff_scale"]
        if q is not None and q >= TOP_TIER_MIN_QUALITY:
            scaled *= dp["top_tier_scale"]
        # Lift weak fish without changing their order. Junk stays passive.
        low_target = dp.get("low_diff_target")
        low_pull = dp.get("low_diff_pull", 0.0)
        if q != QUALITY_JUNK and low_target is not None and scaled < low_target:
            scaled += (low_target - scaled) * low_pull
        self._fish_difficulty = scaled
        self._fish_parameters = build_fish_parameters(self._fish_difficulty, self._fish_motion)
        movement_description = (f"diff={self._fish_difficulty:.0f} "
                                f"motion={MOTION_NAMES[self._fish_motion]} [{ACTIVE_DIFFICULTY}]")
        display = self._fish_name or self._fish_pid or "?"
        logger.info(f"CHASE start: fish={pid} name={display!r} quality={quality} size={size} "
                    f"| movement {movement_description}")
        logger.debug(f"CHASE address: cGcFishLaser=0x{get_addressof(self.laser):X}")

        # reset minigame state and tell the overlay to show it
        self.result = 0
        self.result_timer = 0.0
        self.applied = False
        self.fish_y = self.fish_draw = INIT_FISH_Y
        # Start near the bottom but aim toward a difficulty-derived reachable point.
        start_target = (100.0 - min(100.0, self._fish_difficulty)) / 100.0
        self.fish_target = min(1.0 - FISH_DRAW_MARGIN,
                               max(FISH_DRAW_MARGIN, start_target))
        self.fish_v = INIT_FISH_V
        self.fish_fsa = INIT_FISH_FSA
        self.fish_targeting = True
        self.bar_y = INIT_BAR_Y
        self.bar_v = INIT_BAR_V
        self.progress = active_physics()["start_progress"]
        name = self._fish_name or self._fish_pid or UNKNOWN_DISPLAY_NAME
        rarity = self._fish_rarity or "UNKNOWN"
        diff_pct = round(min(self._fish_difficulty, DIFFICULTY_DISPLAY_MAX)
                         / DIFFICULTY_DISPLAY_MAX * 100)
        self.ui.set("fishName", name)
        self._set_rarity(rarity)
        self.ui.set("difficulty", f"{diff_pct}%")
        # Right from frame one, rather than after the first button press. _send_ui_visibility
        # re-asserts the keyboard/controller hint split itself, so one call covers both.
        self._send_ui_visibility()
        self.ui.show("catch")
        self.ui.flush()

    @nms.cGcPlayer.Update.after
    def on_update(self, this, lfStep):
        if not self._ensure_runtime():
            return

        lmb = _key_down(VK_LBUTTON)
        rmb = _key_down(VK_RBUTTON)
        r = _key_down(VK_R)
        if lmb != self._prev_lmb:
            logger.debug(f"INPUT LMB {'DOWN' if lmb else 'UP'} in_chase={self.in_chase}")
        if rmb != self._prev_rmb:
            logger.debug(f"INPUT RMB {'DOWN' if rmb else 'UP'} in_chase={self.in_chase}")
        if r != self._prev_r:
            logger.debug(f"INPUT R {'DOWN' if r else 'UP'} in_chase={self.in_chase}")
        self._prev_lmb, self._prev_rmb, self._prev_r = lmb, rmb, r

        space = _key_down(VK_SPACE)
        pad_a = controller_push_down()
        if space != self._prev_space:
            logger.debug(f"INPUT SPACE {'DOWN' if space else 'UP'} in_chase={self.in_chase}")
        if pad_a != self._prev_pad_a:
            logger.debug(f"INPUT CONTROLLER_A {'DOWN' if pad_a else 'UP'} in_chase={self.in_chase}")
        if space and not self._prev_space:
            if self._active_device != "keyboard":
                self._active_device = "keyboard"
                self._set_hint("keyboard")
                logger.info("input source -> keyboard")
        if pad_a and not self._prev_pad_a:
            if self._active_device != "controller":
                self._active_device = "controller"
                self._set_hint("controller")
                logger.info("input source -> controller")
        self._prev_space, self._prev_pad_a = space, pad_a

        # per-frame tick: run the minigame physics and stream state to the overlay
        if self._demo_active:
            self._update_demo_panel()
            return
        if not self.in_chase:
            return
        try:
            dt = float(lfStep)
        except Exception:
            dt = 1.0 / 60.0
        if dt <= 0 or dt > 0.1:
            dt = 1.0 / 60.0

        c = self._fish_parameters
        p = active_physics()
        dp = active_difficulty()
        # bar height priority: difficulty-preset barH > physics-preset override > fixed default.
        if dp["barH"] is not None:
            bar_h = dp["barH"]
        else:
            bar_h = p["barH_override"] if p["barH_override"] is not None else BAR_HEIGHT_DEFAULT

        # --- result phase: hold the banner, then trigger the real catch/fail ---
        if self.result != 0:
            self.result_timer -= dt
            self.ui.set("fishY", self.fish_draw)
            self.ui.set("barY", self.bar_y)
            self.ui.set("barH", bar_h)
            self.ui.set("progress", self.progress)
            self._set_inside(False)
            self.ui.flush()
            if self.result_timer <= 0 and not self.applied:
                self.applied = True
                if self.result == 1:
                    self._do_catch()
                else:
                    self._do_fail()
                self.in_chase = False
                self.ui.hide("catch")
                self.ui.flush()
            return

        # --- inertial fish movement -----------------------------------------------------
        f = dt * 60.0
        diff = c["difficulty"]
        motion = c["motion"]
        y = self.fish_y
        fish_lo = FISH_DRAW_MARGIN
        fish_hi = 1.0 - FISH_DRAW_MARGIN
        rt_chance = diff * (1.0 if motion != MOTION_SMOOTH else 20.0) / 4000.0
        if random.random() < rt_chance * f and (motion != MOTION_SMOOTH or not self.fish_targeting):
            space_below = fish_hi - y
            space_above = y - fish_lo
            percent = min(99.0, diff + random.randint(10, 44)) / 100.0
            lo = min(-space_above, space_below)
            self.fish_target = y + random.uniform(lo, space_below) * percent
            self.fish_targeting = True
        if motion == MOTION_FLOATER:
            self.fish_fsa = max(self.fish_fsa - (0.01 / TRACK_PX) * f, -1.5 / TRACK_PX)
        elif motion == MOTION_SINK:
            self.fish_fsa = min(self.fish_fsa + (0.01 / TRACK_PX) * f, 1.5 / TRACK_PX)
        else:
            self.fish_fsa = 0.0
        if self.fish_targeting and abs(y - self.fish_target) > (3.0 / TRACK_PX):
            denom = random.randint(10, 29) + (100.0 - min(100.0, diff))
            accel = (self.fish_target - y) / denom
            self.fish_v += (accel - self.fish_v) * (1.0 - 0.8 ** f)
        elif motion != MOTION_SMOOTH and random.random() < (diff / 2000.0) * f:
            step = (random.randint(-100, -52) if random.random() < 0.5
                    else random.randint(50, 100)) / TRACK_PX
            self.fish_target = y + step
            self.fish_targeting = True
        else:
            self.fish_targeting = False
        if motion == MOTION_DART and random.random() < (diff / 1000.0) * f:
            di = int(diff)
            step = (random.randint(-100 - di * 2, -52) if random.random() < 0.5
                    else random.randint(50, 100 + di * 2)) / TRACK_PX
            self.fish_target = y + step
            self.fish_targeting = True
        if self.fish_targeting:
            self.fish_target = min(fish_hi, max(fish_lo, self.fish_target))
        next_y = y + (self.fish_v + self.fish_fsa) * f
        if next_y > fish_hi:
            impact_speed = max(0.0, self.fish_v + self.fish_fsa)
            self.fish_y = fish_hi
            self.fish_v = -impact_speed * FISH_EDGE_BOUNCE
            self.fish_fsa = 0.0
            self.fish_targeting = False
        elif next_y < fish_lo:
            impact_speed = min(0.0, self.fish_v + self.fish_fsa)
            self.fish_y = fish_lo
            self.fish_v = -impact_speed * FISH_EDGE_BOUNCE
            self.fish_fsa = 0.0
            self.fish_targeting = False
        else:
            self.fish_y = next_y
        self.fish_jitter += dt * c["jfreq"]
        fish_draw = min(1 - FISH_DRAW_MARGIN,
                         max(FISH_DRAW_MARGIN, self.fish_y + math.sin(self.fish_jitter) * c["jamp"]))
        self.fish_draw = fish_draw

        # --- catch bar -----------------------------------------------------------------
        hold = (user32.GetAsyncKeyState(VK_SPACE) & 0x8000) != 0 or controller_push_down()
        # Check whether the fish is inside before moving the bar for the gravity assist.
        bar_in_now = abs(fish_draw - self.bar_y) < bar_h * 0.5
        accel = (-p["bar_accel_up"] if hold else p["bar_gravity"])
        if bar_in_now:
            accel *= p["in_bar_gravity_mult"]
        self.bar_v += accel * dt
        self.bar_v *= p["bar_damping"]
        self.bar_y += self.bar_v * dt
        lo, hi = bar_h * 0.5, 1 - bar_h * 0.5
        # LEGACY stops at an edge; DEFAULT reverses velocity at two thirds of the impact speed.
        if self.bar_y < lo:
            self.bar_y, self.bar_v = lo, -self.bar_v * p["edge_bounce"]
        if self.bar_y > hi:
            self.bar_y, self.bar_v = hi, -self.bar_v * p["edge_bounce"]

        # --- progress: grow while fish is inside the bar, shrink otherwise ---
        inside = abs(fish_draw - self.bar_y) < bar_h * 0.5
        grow = p["grow_override"] * dp["grow_mult"]
        decay = p["decay_override"] * dp["decay_mult"]
        # F11 dev flag: freeze the bar so the minigame never ends (play the physics forever).
        if not self._stamina_lock:
            self.progress += (grow if inside else -decay) * dt
            self.progress = min(PROGRESS_MAX, max(PROGRESS_MIN, self.progress))

        # win/lose -> enter result phase (banner shows for RESULT_BANNER_DELAY before the
        # native lever fires)
        if self.progress >= PROGRESS_MAX:
            self.result, self.result_timer = 1, RESULT_BANNER_DELAY
        elif self.progress <= PROGRESS_MIN:
            self.result, self.result_timer = 2, RESULT_BANNER_DELAY

        # stream the render state to the overlay
        self.ui.set("fishY", fish_draw)
        self.ui.set("barY", self.bar_y)
        self.ui.set("barH", bar_h)
        self.ui.set("progress", self.progress)
        self._set_inside(inside)
        self.ui.flush()

    def _do_catch(self):
        if self.laser is not None:
            try:
                self.laser.SetMode(MODE_CAUGHT)  # native catch (sound + anim + inventory)
            except Exception as e:
                logger.warning(f"catch call error: {e}")
        logger.info(f"WON -> catch (cGcFishLaser.SetMode({MODE_CAUGHT})).")

    def _do_fail(self):
        if self.laser is not None:
            try:
                self.laser.SetMode(nms.cGcFishLaser.eMode.Escaped)  # native escape (sound + anim)
            except Exception as e:
                logger.warning(f"fail call error: {e}")
        logger.info("LOST -> escape (cGcFishLaser.SetMode(Escaped)).")
