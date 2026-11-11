"""Shared helpers for the osu! coach desktop pet (pet.pyw / pet_panel.py).

Paths, logging, settings, tolerant JSON readers and formatting helpers.
Everything here treats coach_data.json as untrusted data: any key may be
missing, null or of the wrong type, and nothing here ever raises on bad data.

The pet only READS files inside the osu! data folder (it stats client.realm
to notice new scores); it never writes, locks or deletes anything there.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "coach_data.json"
STATUS_FILE = BASE / "coach_status.json"
SETTINGS_FILE = BASE / "pet_settings.json"
LOG_FILE = BASE / "pet_log.txt"
ENGINE_FILE = BASE / "coach_engine.py"
ENGINE_LOG = BASE / "pet_engine_log.txt"
PP_DATA_FILE = BASE / "pp_data.json"                 # written by osu_api.py (read-only here)
API_SCRIPT = BASE / "osu_api.py"
API_LOG = BASE / "pet_api_log.txt"
API_CONFIG = BASE / "osu_api_config.json"            # filled in by the user - never by us
API_CONFIG_EXAMPLE = BASE / "osu_api_config.example.json"
TOSU_LOCAL = BASE / "tosu" / "tosu.exe"             # old spot: inside OneDrive tosu refuses to start
# Done/Skip/back/restart -> coach_adapt (CONTRACT_ADAPT.md); same folder coach_adapt reads (OSU_COACH_STATE_DIR)
SESSION_CMD_FILE = Path(os.environ.get("OSU_COACH_STATE_DIR") or BASE) / "session_cmd.json"
# pp_live appends one line per finished try (pet only stats it); same env overrides as pp_live
LIVE_ATTEMPTS_FILE = Path(os.environ.get("OSU_COACH_ATTEMPTS_FILE") or
                          Path(os.environ.get("OSU_COACH_PP_DIR") or BASE) / "live_attempts.jsonl")
# PP-first Mochi (CONTRACT_PP2.md): written by pp_model.py / pp_recommend.py (read-only here)
PP_DIR = Path(os.environ.get("OSU_COACH_PP_DIR") or BASE)
PP_RECS_FILE = PP_DIR / "pp_recs.json"
PP_PRED_LOG = PP_DIR / "pp_predictions.jsonl"
PP_MODEL_SCRIPT = BASE / "pp_model.py"
PP_RECOMMEND_SCRIPT = BASE / "pp_recommend.py"
PP_JOB_LOG = PP_DIR / "pet_pp_job_log.txt"
TOSU_RELEASES = "https://github.com/tosuapp/tosu/releases"
OSU_OAUTH_PAGE = "https://osu.ppy.sh/home/account/edit#oauth"

try:
    from coach_identity import lazer_dir as _lazer_dir  # honours a moved lazer folder (storage.ini)
    OSU_DIR = _lazer_dir()
except Exception:
    OSU_DIR = Path(os.environ.get("APPDATA", "")) / "osu"
REALM_FILE = OSU_DIR / "client.realm"   # stat() only - never opened for writing

PET_NAME = "Mochi"

log = logging.getLogger("pet")


def setup_logging(selftest=False):
    """selftest=True tags every line with [selftest], so fake tosu states never look like real ones."""
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    try:
        h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s " + ("[selftest] " if selftest else "")
                                         + "%(message)s"))
        log.addHandler(h)
    except Exception:
        log.addHandler(logging.NullHandler())


# ----------------------------------------------------------------------------
# tolerant accessors
# ----------------------------------------------------------------------------

def g(d, key, default=None):
    """d[key] if d is a dict and the value is not None, else default."""
    if isinstance(d, dict):
        v = d.get(key)
        if v is not None:
            return v
    return default


def dicts(x):
    """Only the dict items of x if x is a list, else []."""
    if isinstance(x, list):
        return [i for i in x if isinstance(i, dict)]
    return []


def num(x):
    """float(x) for real numbers (bools/strings/NaN rejected), else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return float(x)


def txt(x, default=""):
    if x is None or isinstance(x, (dict, list)):
        return default
    if isinstance(x, bool):
        return default
    if isinstance(x, float):
        return f"{x:g}"
    s = str(x).strip()
    return s if s else default


def strs(x):
    if isinstance(x, list):
        return [txt(i) for i in x if txt(i)]
    if isinstance(x, str) and x.strip():
        return [x.strip()]
    return []


def pos_int(x):
    """Positive int (int-valued floats allowed), else None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x if x > 0 else None
    if isinstance(x, float) and x.is_integer() and x > 0:
        return int(x)
    return None


# ----------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------

def fmt_acc(x, default="-"):
    v = num(x)
    if v is None:
        return default
    if v > 1.5:          # already a percentage
        return f"{v:.1f}%"
    return f"{v * 100:.1f}%"


def fmt_pct0(x, default="-"):
    v = num(x)
    if v is None:
        return default
    if v > 1.5:
        return f"{v:.0f}%"
    return f"{v * 100:.0f}%"


def fmt_num(x, digits=0, default="-", sign=False):
    v = num(x)
    if v is None:
        return default
    return f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"


def fmt_rate(rate):
    r = num(rate)
    if r is None or r <= 0:
        return ""
    s = f"{r:.2f}".rstrip("0").rstrip(".")
    if "." not in s:
        s += ".0"
    return s + "x"


def fmt_mods(mods, rate=None):
    """'HT' + 0.85 -> 'HT 0.85x'; 'NM' + 1.0 -> 'NM'; 'NM' + 0.9 -> 'Rate 0.9x'."""
    m = txt(mods, "NM").upper().replace("+", "")
    r = num(rate)
    if r is None or r <= 0 or abs(r - 1.0) < 0.004:
        return m or "NM"
    if m in ("NM", "", "NONE"):
        return f"Rate {fmt_rate(r)}"
    return f"{m} {fmt_rate(r)}"


def fmt_mmss(ms=None, sec=None, default="?"):
    v = num(sec)
    if v is None:
        v = num(ms)
        if v is None:
            return default
        v /= 1000.0
    v = max(0, int(round(v)))
    return f"{v // 60}:{v % 60:02d}"


def to_dt(ts):
    v = num(ts)
    if v is None or v <= 0:
        return None
    if v > 1e11:  # milliseconds
        v /= 1000.0
    try:
        return _dt.datetime.fromtimestamp(v)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_date(ts, fmt="%b %d %H:%M", default="-"):
    d = to_dt(ts)
    return d.strftime(fmt) if d else default


def ago(ts):
    d = to_dt(ts)
    if not d:
        return ""
    try:
        s = max(0, time.time() - d.timestamp())
    except (OverflowError, OSError, ValueError):  # Windows: naive datetimes near 1970 can't round-trip
        return ""
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400 * 2:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def map_name(rec):
    """'Artist - Title [Version]' from a recommendation-like dict."""
    if not isinstance(rec, dict):
        return "Unknown map"
    if txt(rec.get("map")):
        return txt(rec.get("map"))
    a, t, v = txt(rec.get("artist")), txt(rec.get("title")), txt(rec.get("version"))
    s = f"{a} - {t}" if a and t else (t or a)
    if v:
        s = f"{s} [{v}]" if s else f"[{v}]"
    return s or "Unknown map"


def search_text(rec):
    s = txt(g(rec, "search"))
    if s:
        return s
    return " ".join(x for x in (txt(g(rec, "artist")), txt(g(rec, "title")), txt(g(rec, "version"))) if x)


# ----------------------------------------------------------------------------
# files
# ----------------------------------------------------------------------------

def read_json(path):
    """Parsed JSON dict, or None when missing / half-written / not a dict."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def mtime(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


FILES_DIR = OSU_DIR / "files"   # lazer's content-addressed store: files/<h>/<hh>/<hash>


def files_signal():
    """Newest mtime of lazer's files/?/?? folders (stat only). A saved replay is a new file in one
    of them, which bumps that folder's mtime - unlike client.realm, which is written via mmap."""
    best = 0
    try:
        with os.scandir(FILES_DIR) as it1:
            firsts = [e.path for e in it1 if len(e.name) == 1 and e.is_dir()]
        for p in firsts:
            with os.scandir(p) as it2:
                for e in it2:
                    if len(e.name) == 2 and e.is_dir():
                        try:
                            best = max(best, os.stat(e.path).st_mtime_ns)
                        except OSError:
                            pass
    except OSError:
        return None
    return best or None


# --- Win32 foreground helpers (no-ops elsewhere) ----------------------------------------------
try:
    import ctypes as _ct
    from ctypes import wintypes as _wt
    _u32 = _ct.WinDLL("user32", use_last_error=True)
    _u32.GetForegroundWindow.restype = _wt.HWND
    _u32.SetForegroundWindow.argtypes = [_wt.HWND]
    _u32.IsWindow.argtypes = [_wt.HWND]
    _u32.GetWindowThreadProcessId.argtypes = [_wt.HWND, _ct.POINTER(_wt.DWORD)]
except Exception:
    _u32 = None


def foreground_hwnd():
    try:
        return _u32.GetForegroundWindow() if _u32 else None
    except Exception:
        return None


def hwnd_pid(h):
    if not (_u32 and h):
        return None
    try:
        pid = _wt.DWORD(0)
        _u32.GetWindowThreadProcessId(h, _ct.byref(pid))
        return pid.value or None
    except Exception:
        return None


def foreign_foreground():
    """The foreground window if it belongs to another process (e.g. the game), else None."""
    h = foreground_hwnd()
    return h if h and hwnd_pid(h) not in (None, os.getpid()) else None


def restore_foreground(h):
    """Give the foreground back to a window of another process (only works while we own it)."""
    if not (_u32 and h):
        return
    try:
        if _u32.IsWindow(h) and hwnd_pid(h) != os.getpid():
            _u32.SetForegroundWindow(h)
    except Exception:
        pass


def write_json_atomic(path, obj):
    """Write via a per-process tmp file + os.replace. OneDrive (or a reader) can lock the target for a
    moment, so os.replace is retried 5 times, 100 ms apart. -> True when saved."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
        for i in range(6):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if i == 5:
                    raise
                time.sleep(0.1)
    except Exception as e:
        log.warning("could not save %s: %s", path.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass
    return False


SETTINGS_VERSION = 2


class Settings:
    # v2: Mochi stays visible above osu! by default (no hiding while the game is focused / playing)
    DEFAULTS = {"pos": None, "always_show": False, "checks": {}, "last_seen_play_ts": None,
                "hide_while_playing": False, "live_bubble": True, "player_setup": None, "tosu_path": None,
                "hide_when_focused": False, "break_reminders": True, "card_pick": {},
                "settings_version": SETTINGS_VERSION}

    def __init__(self, path=SETTINGS_FILE, persist=True):
        self.path = path
        self.persist = persist
        self.d = dict(self.DEFAULTS)
        self.d["checks"], self.d["card_pick"] = {}, {}
        loaded = read_json(path) if persist else None
        if loaded:
            self.d.update(loaded)
            self.migrate(loaded)
        for k in ("checks", "card_pick"):
            if not isinstance(self.d.get(k), dict):
                self.d[k] = {}

    def migrate(self, loaded):
        """Older versions defaulted to hiding Mochi while osu! was focused / a map was playing, so he vanished
        mid-session. Reset both to 'stay visible' once; the user can turn them back on afterwards."""
        try:
            ver = int(loaded.get("settings_version") or 0)
        except (TypeError, ValueError):
            ver = 0
        if ver < 2:
            self.d["hide_when_focused"] = False
            self.d["hide_while_playing"] = False
            self.d["settings_version"] = SETTINGS_VERSION
            self.save()
            log.info("settings migrated to v%d (stay visible above osu!)", SETTINGS_VERSION)

    def get(self, k, default=None):
        v = self.d.get(k)
        return default if v is None else v

    def set(self, k, v):
        """-> False when the settings file couldn't be written (the value is still kept in memory)."""
        self.d[k] = v
        return self.save()

    def save(self):
        if self.persist:
            return write_json_atomic(self.path, self.d)
        return True

    # per-day session-plan checkboxes
    def check_get(self, day, key):
        day_d = self.d["checks"].get(day)
        return bool(day_d.get(key)) if isinstance(day_d, dict) else False

    def check_set(self, day, key, val):
        checks = self.d["checks"]
        day_d = checks.get(day)
        if not isinstance(day_d, dict):
            day_d = checks[day] = {}
        day_d[key] = bool(val)
        for old in sorted(checks)[:-14]:  # keep two weeks
            checks.pop(old, None)
        self.save()

    # quick card: which map of a block is shown today (Skip map)
    def pick_get(self, day, key):
        day_d = self.d["card_pick"].get(day)
        v = day_d.get(key) if isinstance(day_d, dict) else 0
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    def pick_set(self, day, key, idx):
        cp = self.d["card_pick"]
        day_d = cp.get(day)
        if not isinstance(day_d, dict):
            day_d = cp[day] = {}
        day_d[key] = int(idx)
        for old in sorted(cp)[:-3]:
            cp.pop(old, None)
        self.save()


def today():
    return _dt.date.today().isoformat()


# ----------------------------------------------------------------------------
# plain-language helpers (the player is not technical: say what to DO)
# ----------------------------------------------------------------------------

PURPOSE_PLAIN = {"warmup": "Warm-up", "accuracy": "Accuracy practice", "speed": "Speed practice",
                 "aim": "Aim practice", "reading": "Reading practice", "stamina": "Stamina", "push": "Push yourself",
                 "pp": "PP gain"}
MOD_NAMES = {"HT": "Half Time", "DC": "Daycore", "DT": "Double Time", "NC": "Nightcore", "HR": "Hard Rock",
             "HD": "Hidden", "EZ": "Easy", "FL": "Flashlight", "SD": "Sudden Death", "PF": "Perfect",
             "NF": "No Fail", "RX": "Relax", "AP": "Autopilot", "SO": "Spun Out", "TD": "Touch Device",
             "CL": "Classic", "RD": "Random", "MR": "Mirror", "DA": "Difficulty Adjust", "BL": "Blinds"}


def split_mods(mods):
    m = txt(mods, "").upper()
    for ch in "+ ,|/":
        m = m.replace(ch, "")
    if m in ("", "NM", "NOMOD", "NONE"):
        return []
    return [m[i:i + 2] for i in range(0, len(m) - 1, 2)]


def _rate_of(rate):
    r = num(rate)
    return None if r is None or r <= 0 or abs(r - 1.0) < 0.004 else r


def mod_pill(mods, rate=None):
    """'HT 0.80x' / 'No mods' / 'HD' / 'Speed 0.90x' - the big pill on the card."""
    ms, r = split_mods(mods), _rate_of(rate)
    if not ms:
        return f"Speed {r:.2f}x" if r else "No mods"
    return "".join(ms) + (f" {r:.2f}x" if r else "")


def mod_howto(mods, rate=None):
    """What to click in song select, in plain words."""
    ms, r = split_mods(mods), _rate_of(rate)
    if not ms:
        if not r:
            return "Turn all mods off"
        return f"Mods: {'Half Time' if r < 1 else 'Double Time'}, set its speed to {r:.2f}x in the mod's settings"
    parts = []
    for m in ms:
        name = MOD_NAMES.get(m, m)
        if r and m in ("HT", "DC", "DT", "NC"):
            parts.append(f"{name}, set its speed to {r:.2f}x in the mod's settings")
        else:
            parts.append(name)
    if r and not any(m in ("HT", "DC", "DT", "NC") for m in ms):
        parts.append(f"Rate Adjust {r:.2f}x")
    return "Mods: " + " + ".join(parts)


def shorten(text, n=100):
    """First sentence(s) up to n chars, cut at a word boundary."""
    s = " ".join(txt(text).split())
    if len(s) <= n:
        return s
    cut = -1
    for sep in (". ", "! ", "? "):
        i = s.rfind(sep, 0, n)
        if i > n * 0.45:
            cut = max(cut, i + 1)
    if cut > 0:
        return s[:cut]
    i = s.rfind(" ", 0, n - 1)
    return s[:i if i > n * 0.5 else n - 1].rstrip(" ,;-") + "…"


import re as _re  # noqa: E402

AIM_COMFORT = None   # the player's comfortable jump speed (stats.comfort_aim_velocity); set by the panel


def set_aim_comfort(data):
    global AIM_COMFORT
    v = num(g(g(data, "stats"), "comfort_aim_velocity"))
    AIM_COMFORT = v if v and v > 0 else None


def jump_words(v, comfort=None):
    """A jump speed (engine px/ms) as words relative to the player's comfort speed - never a bare number."""
    c = comfort or AIM_COMFORT
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "jump speed"
    if not c:
        return "jump speed"
    r = v / c
    if r < 0.8:
        return "an easy speed for you"
    if r <= 1.02:
        return "about your comfortable speed"
    if r < 1.15:
        return "a speed a bit above your comfort zone"
    return "a speed well above your comfort zone"


def _jw(m):
    return jump_words(m.group(1))


def _goal_faster(m):
    c = AIM_COMFORT
    try:
        v = float(m.group(1))
    except ValueError:
        return "faster jumps"
    if c and v > c * 1.02:
        return f"jumps {round((v / c - 1) * 100)}% faster than you're comfortable with now"
    return "jumps at your current comfortable speed"


def _goal_faster_v(v):
    m = _re.match(r"(\d+(?:\.\d+)?)", v)
    return _goal_faster(m) if m else "faster jumps"


def _cap(m):
    return m.group(1) + m.group(2).upper()


_N = r"~?(\d+(?:\.\d+)?)"
_PLAIN_SUBS = [
    # --- jump speed (px/ms) -> words; the specific engine phrases first, then a generic fallback
    (_re.compile(r"(\d(?:\.\d)?)\* maps need about " + _N + r" px/ms clean, (\d(?:\.\d)?)\* about " + _N + r"\+"),
     lambda m: f"{m.group(1)}* maps need {_goal_faster_v(m.group(2))}, {m.group(3)}* maps "
               f"{_goal_faster_v(m.group(4)).replace('jumps ', '', 1)}"),
    (_re.compile(r"jumps peak " + _N + r" px/ms \(your comfort ~?[\d.]+\)"), lambda m: "jumps at " + _jw(m)),
    (_re.compile(r"jumps to " + _N + r" px/ms \(your comfort ~?[\d.]+\)"), lambda m: "jumps at " + _jw(m)),
    (_re.compile(r"jumps under " + _N + r" px/ms"), lambda m: "only jumps at " + _jw(m)),
    (_re.compile(r"hardest jumps " + _N + r" px/ms(?:, at [\d.]+x [\d.]+)? - just under where your misses climb "
                 r"\(~?[\d.]+ px/ms\)"), "hardest jumps just under the speed where you start missing"),
    (_re.compile(r"[Tt]hese jumps \(" + _N + r" px/ms\) are within your range \(comfort ~?[\d.]+\)"),
     "These jumps are within your range"),
    (_re.compile(r"Jumps here move " + _N + r" px/ms \(your comfort ~?[\d.]+\)"), lambda m: "Jumps here are at " + _jw(m)),
    (_re.compile(r"climbing above " + _N + r" px/ms"), "climbing once jumps get faster than you're comfortable with"),
    (_re.compile(r"climbing already below 0\.75 px/ms \(the slowest the coach measures well\)"),
     "climbing even on slow jumps"),
    (_re.compile(r"Your jump misses climb from about " + _N + r" px/ms"),
     "Your jump misses climb once jumps get faster than you're comfortable with"),
    (_re.compile(r"up to " + _N + r" px/ms"), lambda m: "on " + _goal_faster(m)),
    (_re.compile(r"jumps \(" + _N + r" px/ms\)"), lambda m: "jumps (" + _jw(m) + ")"),
    (_re.compile(r"\s*\(your comfort ~?[\d.]+(?: px/ms)?\)"), ""),
    (_re.compile(r"jump speed in px/ms"), "jump speed"),
    (_re.compile(_N + r"\s*px/ms"), lambda m: "jumps at " + _jw(m)),
    (_re.compile(r"\s*\(?\bpx/ms\b\)?"), ""),
    # --- engine words
    (_re.compile(r"\s*\(each pick's rate\)"), " (use the speed shown on each map)"),
    (_re.compile(r"\bpick's\b"), "map's"),
    (_re.compile(r"\b[Pp]icks\b"), lambda m: "maps" if m.group(0)[0] == "p" else "Maps"),
    (_re.compile(r"\b(the|each|this|a|that) pick\b"), lambda m: m.group(1) + " map"),
    (_re.compile(r"\b(the )?push block\b"), "the Push yourself step"),
    (_re.compile(r"Roadmap (\d+(?:\.\d+)?)\* checkpoint"), lambda m: f"Goal for {m.group(1)}★"),
    (_re.compile(r"the coach's roadmap targets( are)?"), lambda m: "the coach's goals" + (m.group(1) or "")),
    (_re.compile(r"\(coach heuristic"), "(coach's rule of thumb"),
    (_re.compile(r"coach heuristics"), "the coach's rules of thumb"),
    (_re.compile(r"Median star rating"), "The typical star rating"),
    (_re.compile(r"\bcheckpoints\b"), "goals"),
    (_re.compile(r"\(see focus\)"), "(see the Tips tab)"),
    (_re.compile(r"were near-FCs( \(3 misses or fewer\))?"), "had 3 misses or fewer"),
    (_re.compile(r"near-FCs? \(3 misses or fewer\)"), "plays with 3 misses or fewer"),
    (_re.compile(r"\(near-FCs\)"), "(3 misses or fewer)"),
    (_re.compile(r"(\d+%) near-FCs\b"), r"\1 of plays with 3 misses or fewer"),
    (_re.compile(r"Near-FC on the last play \(0 misses\)!"), "No misses on the last play!"),
    (_re.compile(r"Near-FC \(0 misses\)!"), "No misses!"),
    (_re.compile(r"Click the pet and read the new advice"), "Open the full coach (Tips tab) and read what changed"),
    (_re.compile(r"pass it (NoMod|NM)\b"), "pass it with no mods"),
    (_re.compile(r"\b(NoMod|NM)\b"), "no mods"),
    (_re.compile(r"(\d)\*"), "\\1★"),
    (_re.compile(r"(^|[.!?]\s+)([a-z])"), _cap),
]


def plain(text):
    """Swap engine jargon for plain words (jump speed in words, 'pick' -> 'map', 4* -> 4★, NM -> no mods,
    first 'UR' explained, sentences capitalised)."""
    s = txt(text)
    for rx, rep in _PLAIN_SUBS:
        s = rx.sub(rep, s)
    if "spread" not in s.lower():
        s = _re.sub(r"\bUR\b(?! \()", "UR (timing spread)", s, count=1)
    return s


_STEP_IDS = [("warmup", ("warm",)), ("break", ("break",)), ("review", ("review",)), ("pp", ("score attempt", "(pp)")),
             ("push", ("push",)), ("stamina", ("stamina",)), ("reading", ("reading",)), ("aim", ("aim",)),
             ("speed", ("speed", "stream")), ("accuracy", ("accuracy",))]


def step_id(name):
    """Stable id of a plan block from what it trains ('Speed (stream test)' and 'Speed' are both 'speed'), so
    ticks survive the engine adding, removing or moving blocks after a play."""
    low = txt(name).lower()
    for k, words in _STEP_IDS:
        if any(w in low for w in words):
            return k
    return _re.sub(r"[^a-z0-9]+", "-", low).strip("-") or "step"


def plan_steps(data):
    """Today's session plan as steps: [{i, key, id, block, minutes, goal, picks:[rec...]}]. `key` (used for the
    per-day ticks and the card's map choice) is the block's purpose, not its position."""
    recs = {txt(g(r, "id")): r for r in dicts(g(data, "recommendations")) if txt(g(r, "id"))}
    out, seen = [], {}
    for i, b in enumerate(dicts(g(data, "session_plan"))):
        name = txt(g(b, "block"), f"Block {i + 1}")
        sid = step_id(name)
        seen[sid] = seen.get(sid, 0) + 1
        key = sid if seen[sid] == 1 else f"{sid}-{seen[sid]}"
        out.append({"i": i, "key": key, "id": sid, "block": name, "minutes": num(g(b, "minutes")),
                    "goal": txt(g(b, "goal")), "picks": [recs[p] for p in strs(g(b, "pick_ids")) if p in recs]})
    return out


def current_step(steps, settings, day=None):
    """Index of the first block not ticked off today (len(steps) when all are done)."""
    day = day or today()
    for s in steps:
        if not settings.check_get(day, s["key"]):
            return s["i"]
    return len(steps)


# ----------------------------------------------------------------------------
# adaptive session (CONTRACT_ADAPT.md): coach_data.session / last_debrief / debriefs_today / tracks / changes.
# When coach_data has no usable "session" (older engine), the pet falls back to session_plan + local ticks.
# ----------------------------------------------------------------------------

TRACK_NAMES = {"speed": "Speed", "accuracy": "Accuracy", "aim": "Aim", "stamina": "Stamina", "reading": "Reading",
               "push": "Push"}
TRACK_ORDER = ["accuracy", "speed", "aim", "stamina", "reading", "push"]
VERDICTS = {"pass": ("Pass!", "mint"), "close": ("Almost", "yellow"), "fail": ("Not yet", "peach"),
            "free-play": ("Free play", "lav"), "done": ("Recorded", "lav"), "not-counted": ("Not counted", "lav")}
SESSION_CMDS = ("done", "skip", "back", "restart")


def session_of(data):
    """coach_data.session when it is today's and has steps, else None."""
    s = g(data, "session")
    if not isinstance(s, dict) or not dicts(g(s, "steps")):
        return None
    day = txt(g(s, "date"))
    if day and day[:10] != today():
        return None   # yesterday's session: the engine makes today's on its next run
    return s


def has_plan(data):
    return bool(session_of(data)) or bool(plan_steps(data))


def _recs_by_id(data):
    return {txt(g(r, "id")): r for r in dicts(g(data, "recommendations")) if txt(g(r, "id"))}


def _needed_of(step):
    """Clean passes still needed for the next level up (coach_adapt: 2 - pass streak), if given."""
    v = num(g(step, "needed"))
    return int(v) if v is not None and v >= 0 else None


def _count(x):
    v = num(x)
    return int(v) if v is not None and v >= 0 else 0


def session_index(data):
    """Index of the step in progress (len(steps) when every step is done or skipped)."""
    s = session_of(data)
    if not s:
        return 0
    steps = dicts(g(s, "steps"))
    i = g(s, "step_index")
    if isinstance(i, int) and not isinstance(i, bool) and 0 <= i <= len(steps):
        return i
    for k, st in enumerate(steps):
        if txt(g(st, "state")).lower() not in ("done", "skipped"):
            return k
    return len(steps)


def session_steps(data):
    """The session's steps, normalised: [{i, block, track, id, minutes, state ('done'|'skipped'|'now'|'todo'),
    attempts, passes, needed, md5, map (recommendation or None), rate, mods, goal, exit_rule}]."""
    s = session_of(data)
    if not s:
        return []
    recs = _recs_by_id(data)
    idx = session_index(data)
    out = []
    for i, st in enumerate(dicts(g(s, "steps"))):
        state = txt(g(st, "state")).lower()
        if state not in ("done", "skipped"):
            state = "now" if i == idx else ("done" if i < idx else "todo")
        block = txt(g(st, "block"), f"Step {i + 1}")
        md5 = txt(g(st, "pick_md5"))
        mp = g(st, "map") if isinstance(g(st, "map"), dict) else recs.get(md5)
        out.append({"i": i, "block": block, "track": txt(g(st, "track")).lower(), "id": step_id(block),
                    "minutes": num(g(st, "minutes")), "state": state, "attempts": _count(g(st, "attempts")),
                    "passes": _count(g(st, "passes")), "needed": _needed_of(st), "md5": md5, "map": mp,
                    "rate": num(g(st, "rate")), "mods": txt(g(st, "mods")), "goal": txt(g(st, "goal")),
                    "exit_rule": txt(g(st, "exit_rule"))})
    return out


def session_current(data):
    """The step to play now, merged with session.current (which wins): {..step.., map, rate, mods, goal,
    attempt_n, passes, needed, n}. None when the session is finished or missing."""
    s = session_of(data)
    steps = session_steps(data)
    i = session_index(data)
    if not s or i >= len(steps):
        return None
    st = dict(steps[i])
    cur = g(s, "current") if isinstance(g(s, "current"), dict) else {}
    mp = g(cur, "map")
    if not isinstance(mp, dict):
        mp = _recs_by_id(data).get(txt(mp)) if txt(mp) else st["map"]
    st["map"] = mp if isinstance(mp, dict) else None
    st["block"] = txt(g(cur, "block")) or st["block"]
    st["track"] = txt(g(cur, "track")).lower() or st["track"]
    st["rate"] = num(g(cur, "rate")) or st["rate"] or num(g(st["map"], "rate"))
    st["mods"] = txt(g(cur, "mods")) or st["mods"] or txt(g(st["map"], "mods"))
    st["goal"] = txt(g(cur, "goal")) or st["goal"] or txt(g(st["map"], "goal"))
    st["attempt_n"] = pos_int(g(cur, "attempt_n"))
    if num(g(cur, "passes")) is not None:
        st["passes"] = _count(g(cur, "passes"))
    st["needed"] = _needed_of(cur) if _needed_of(cur) is not None else st["needed"]
    st["exit_rule"] = txt(g(cur, "exit_rule")) or st["exit_rule"]
    st["n"] = len(steps)
    return st


def progress_words(st, with_level=True):
    """'1 passed  ·  3 tries so far  ·  level up after 1 more clean pass' for a normalised step."""
    if not isinstance(st, dict):
        return ""
    bits = []
    p, a, nd = _count(st.get("passes")), _count(st.get("attempts")), st.get("needed")
    if p:
        bits.append(f"{p} passed")
    if a:
        bits.append(f"{a} {'try' if a == 1 else 'tries'} so far")
    if with_level and isinstance(nd, int) and nd > 0 and txt(st.get("track")) in TRACK_NAMES:
        bits.append("level up after 1 more clean pass" if nd == 1 else
                    f"level up after {nd} clean passes in a row")
    return "  ·  ".join(bits)


def session_apply_cmd(data, cmd):
    """What the session will look like once coach_adapt has consumed `cmd` - shown at once (and kept when the
    engine can't run). Returns a new data dict; `data` is not changed."""
    import copy
    s = session_of(data)
    if not s or cmd not in SESSION_CMDS:
        return data
    s = copy.deepcopy(s)
    steps = [st for st in s.get("steps", []) if isinstance(st, dict)]
    s["steps"] = steps
    n = len(steps)
    i = session_index(data)
    if cmd in ("done", "skip") and i < n:
        steps[i]["state"] = "done" if cmd == "done" else "skipped"
        i += 1
    elif cmd == "back" and i > 0:
        i -= 1
    elif cmd == "restart":
        i = 0
        for st in steps:
            st["state"] = "pending"
    if i < n:
        steps[i]["state"] = "active"
        st = steps[i]
        s["current"] = {"block": st.get("block"), "track": st.get("track"),
                        "map": _recs_by_id(data).get(txt(st.get("pick_md5"))), "rate": st.get("rate"),
                        "mods": st.get("mods"), "goal": st.get("goal"),
                        "attempt_n": _count(st.get("attempts")) + 1, "passes": _count(st.get("passes")),
                        "needed": None, "exit_rule": st.get("exit_rule")}
    else:
        s["current"] = None
    s["step_index"] = i
    s["optimistic"] = True
    out = dict(data)
    out["session"] = s
    return out


def write_session_cmd(cmd, data=None, path=None):
    """session_cmd.json {"cmd", "ts", "step_index", "block"} for coach_adapt (it consumes it on its next run).
    step_index/block are extras so a stale command can be recognised. -> True when written."""
    s = session_of(data) if data is not None else None
    obj = {"cmd": cmd, "ts": round(time.time(), 3)}   # coach_adapt de-dupes on cmd@ts: keep two quick clicks apart
    if s:
        i = session_index(data)
        obj["step_index"] = i
        st = dicts(g(s, "steps"))
        if i < len(st):
            obj["block"] = txt(g(st[i], "block"))
    return write_json_atomic(path or SESSION_CMD_FILE, obj)


def debriefs(data):
    return dicts(g(data, "debriefs_today"))[:30]


def debrief_sig(deb):
    """Identity of one debrief (a new one pops the card)."""
    if not isinstance(deb, dict):
        return ""
    return "|".join(txt(g(deb, k)) for k in ("ts", "md5", "source", "verdict", "headline"))


def same_attempt(a, b):
    """A live debrief and the replay debrief of the same play (same map, within ~45 s) are one attempt."""
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    ta, tb = num(g(a, "ts")), num(g(b, "ts"))
    return (txt(g(a, "md5")) == txt(g(b, "md5")) and bool(txt(g(a, "md5"))) and ta is not None and tb is not None
            and abs(ta - tb) <= 45)


def step_debrief(data, step):
    """Newest debrief of today that belongs to `step` (same block or track, not free play), else None."""
    if not step:
        return None
    for d in debriefs(data) or ([g(data, "last_debrief")] if isinstance(g(data, "last_debrief"), dict) else []):
        if txt(g(d, "verdict")).lower() == "free-play":
            continue
        if (txt(g(d, "block")) and txt(g(d, "block")) == txt(step.get("block"))) or \
                (txt(g(d, "track")) and txt(g(d, "track")).lower() == txt(step.get("track"))):
            return d
    return None


def verdict_of(deb):
    """(label, colour key) for a debrief's verdict. A debrief whose next action is a break shows 'Rest'
    (the attempt's own verdict would sit oddly above 'time for a rest')."""
    if txt(g(g(deb, "next_action"), "type")).lower() == "break":
        return ("Rest", "yellow")
    v = txt(g(deb, "verdict")).lower()
    return VERDICTS.get(v, ("Done", "lav"))


def is_latest_debrief(data, deb):
    """True when `deb` is the newest debrief (only then may the card mix in the session's current step)."""
    last = g(data, "last_debrief")
    if not isinstance(last, dict):
        ds = debriefs(data)
        last = ds[0] if ds else None
    return isinstance(last, dict) and isinstance(deb, dict) and debrief_sig(last) == debrief_sig(deb)


def rate_words(r):
    r = num(r)
    if r is None or r <= 0:
        return ""
    return "normal speed" if abs(r - 1.0) < 0.004 else f"{r:.2f}x"


def fmt_level(v, unit):
    """0.85 + 'x' -> '0.85x', 150 + 'BPM' -> '150 BPM', 4.3 + '★' -> '4.3★', 9 + 'AR' -> 'AR 9'."""
    x = num(v)
    u = txt(unit)
    if x is None:
        return "-"
    if u == "x":
        return f"{x:.2f}x"
    if u.upper() == "BPM":
        return f"{x:.0f} BPM"
    if u in ("★", "*", "stars"):
        s = f"{x:.2f}".rstrip("0")
        return (s + "0" if s.endswith(".") else s) + "★"
    if u == "min":
        return f"{x:g} min"
    if u == "px/ms":   # jump speed: never a bare px/ms number - relative to his comfortable jump speed
        c = AIM_COMFORT
        return f"{round(x / c * 100)}%" if c else f"level {x:.2f}"
    if u.upper() == "AR":
        return f"AR {x:g}"
    if u.upper() == "OD":
        return f"OD {x:g}"
    return f"{x:g}{(' ' + u) if u else ''}"


def level_text(text):
    """Engine text about levels in plain words; jump-speed levels ('2.1 px/ms') as % of his comfort speed."""
    s = txt(text)
    if AIM_COMFORT:
        s = _re.sub(r"(\d+(?:\.\d+)?)\s*px/ms", lambda m: fmt_level(float(m.group(1)), "px/ms") + " jump speed", s)
    return plain(s)


def track_name(track, tracks=None):
    t = txt(track).lower()
    return TRACK_NAMES.get(t, t.title() or "Skill")


def level_change(deb, tracks=None):
    """deb.level_change in words -> {"text", "up", "sig"} or None.
    'Speed level up: 0.85x → 0.90x'  /  'Speed level: 0.90x → 0.85x'."""
    lc = g(deb, "level_change")
    if not isinstance(lc, dict):
        return None
    a, b = num(g(lc, "from")), num(g(lc, "to"))
    if a is None or b is None or abs(a - b) < 1e-9:
        return None
    tr = txt(g(lc, "track")).lower() or txt(g(deb, "track")).lower()
    unit = txt(g(lc, "unit")) or txt(g(g(tracks, tr), "unit")) if isinstance(tracks, dict) else txt(g(lc, "unit"))
    unit = unit or {"speed": "BPM", "accuracy": "OD", "push": "★", "stamina": "min", "reading": "AR",
                    "aim": "px/ms"}.get(tr, "")
    up = b > a
    name = track_name(tr)
    text = f"{name} level {'up' if up else 'down'}: {fmt_level(a, unit)} → {fmt_level(b, unit)}"
    r = _rate_of(g(g(deb, "next_action"), "rate"))
    if tr == "speed" and r and txt(unit).upper() == "BPM":
        text += f" (play it at {r:.2f}x)"
    return {"text": text, "up": up, "sig": f"{tr}|{a}|{b}|{txt(g(deb, 'ts'))}"}


ACTION_WORDS = {"retry": "Again", "faster": "Faster", "slower": "Slower", "next_map": "Next map",
                "next_step": "Step done", "break": "Take a 5 min break", "stop": "That's enough for today"}


def next_action_big(na, data=None, deb=None):
    """The NEXT action as 2-6 big words: 'Again at 0.90x', 'Slower: 0.80x', 'Next map',
    'Step done - next: Aim', 'Take a 5 min break', 'Back to today's step at 0.85x' (after free play)."""
    if not isinstance(na, dict):
        return shorten(txt(na), 40) if txt(na) else ""
    t = txt(g(na, "type")).lower()
    r = rate_words(g(na, "rate"))
    other_map = bool(txt(g(na, "md5")) and txt(g(deb, "md5")) and txt(g(na, "md5")) != txt(g(deb, "md5")))
    free = txt(g(deb, "verdict")).lower() == "free-play"
    latest = deb is None or data is None or is_latest_debrief(data, deb)
    bn, bof = pos_int(g(na, "bench_n")), pos_int(g(na, "bench_of"))
    if bn and bof:
        return f"Check-up map {bn} of {bof}"
    if txt(g(deb, "verdict")).lower() == "done" and t == "retry":
        return "Once more (check-up)"
    if (t in ("retry", "faster", "slower") and other_map) or (free and t in ("retry", "faster", "slower", "next_map")
                                                             and (other_map or not txt(g(na, "md5")))):
        # an older debrief must not borrow today's current step: what was said then is in the detail line
        cur = session_current(data) if (data is not None and latest) else None
        blk = txt(g(cur, "block")) if cur else ""
        where = f"Back to {blk}" if blk else "Back to today's step"
        return f"{where} at {r}" if r and r != "normal speed" else where
    if t == "next_map":
        return f"Next map at {r}" if r and r != "normal speed" else "Next map"
    if t == "retry":
        return f"Again at {r}" if r else "Same map again"
    if t == "faster":
        return f"Faster: {r}" if r else "A bit faster"
    if t == "slower":
        return f"Slower: {r}" if r else "A bit slower"
    if t == "next_step":
        cur = session_current(data) if (data is not None and latest) else None
        nb = txt(g(cur, "block")) if cur else ""
        return f"Step done - next: {nb}" if nb else "Step done!"
    if t in ACTION_WORDS:
        return ACTION_WORDS[t]
    return shorten(txt(g(na, "text")), 40) or "Keep going"


FREE_NOTE = "Not today's step, so it won't change your levels now."
_OLD_FREE_NOTES = ("Want this to count? It's not today's step.", FREE_NOTE)


def action_detail(na, deb=None, level=None):
    """next_action.text in plain words, without the free-play note (the card shows that on its own line).
    With a level change on the card, a detail that only repeats the new speed ('Back to 0.85x on the same
    map') is dropped - the banner already says it."""
    s = level_text(g(na, "text")) if isinstance(na, dict) else ""
    for fn in _OLD_FREE_NOTES:
        if s.startswith(fn):
            s = s[len(fn):].strip()
    if level and _re.match(r"^(Back to|Go up to|Back up to|Same map at) [\d.]+x( on the same map)?( \([^)]*\))?\.?$", s):
        return ""
    s = _re.sub(r" at 1\.0x\b", "", s)
    return s


def today_hero(data):
    """The Today tab's hero line: what to do next, in big words, plus the map to copy -> {"text", "rec"} or None."""
    cur = session_current(data)
    if not cur:
        return None
    rec = cur.get("map") if isinstance(cur.get("map"), dict) else None
    last = g(data, "last_debrief") if isinstance(g(data, "last_debrief"), dict) else None
    big = ""
    if last and txt(g(g(last, "next_action"), "type")).lower() in ("break", "stop"):
        big = next_action_big(g(last, "next_action"), data, last)
    mname = ""
    if rec:
        t = txt(g(rec, "title"))
        mname = shorten(t, 34) if t else shorten(map_name(rec), 34)
    if not big:
        blk = txt(cur.get("block"))
        r = rate_words(cur.get("rate"))
        big = blk + (f" at {r}" if r and r != "normal speed" else "")
    return {"text": "Next: " + big + (f" - {mname}" if mname and big and "break" not in big.lower() else ""),
            "rec": rec}


def history_action(na):
    """What the coach said next at the time of an older debrief: 'Then I said: ...'."""
    d = action_detail(na)
    return ("Then I said: " + shorten(d, 90)) if d else ""


def compare_parts(cmp):
    """deb.compare -> ('vs last attempt on this map', [(label, '+3.1%', good: bool|None)])."""
    if not isinstance(cmp, dict):
        return "", []
    parts = []
    for key, label, lower_better, unit in (("acc", "accuracy", False, "%"), ("ur", "UR", True, ""),
                                           ("misses", "misses", True, "")):
        v = g(cmp, key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s = f"{v:+.1f}{unit}" if key == "acc" else f"{v:+.0f}"
        else:
            s = txt(v)
        if not s:
            continue
        good = None
        if s[0] in "+-" and s.strip("+-0.%") != "":
            good = (s[0] == "-") == lower_better
        parts.append((label, s, good))
    return txt(g(cmp, "vs")), parts


# ----------------------------------------------------------------------------
# osu! API config / tosu helpers (CONTRACT_PP.md)
# ----------------------------------------------------------------------------

API_TEMPLATE = {"client_id": 0, "client_secret": "paste your client secret here", "user": "", "user_id": 0,
                "exclude_users": []}


def api_configured():
    """True when osu_api_config.json has a client id and a secret that isn't the placeholder."""
    c = read_json(API_CONFIG)
    if not c:
        return False
    cid, sec = c.get("client_id"), txt(c.get("client_secret"))
    try:
        cid_ok = int(str(cid).strip()) > 0
    except (TypeError, ValueError):
        cid_ok = False
    # same rule as osu_api.load_config: the example's "PASTE_YOUR_CLIENT_SECRET_HERE" is not a secret
    return cid_ok and len(sec) >= 8 and " " not in sec and "PASTE" not in sec.upper()


def ensure_api_config():
    """Create osu_api_config.json from the example (or a blank template) if it is missing.
    Never overwrites an existing file. Returns the path."""
    if API_CONFIG.exists():
        return API_CONFIG
    tpl = read_json(API_CONFIG_EXAMPLE)
    if not tpl:
        tpl = dict(API_TEMPLATE)
    try:
        API_CONFIG.write_text(json.dumps(tpl, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("could not create %s: %s", API_CONFIG.name, e)
    return API_CONFIG


def tosu_appdata_exe():
    """%LOCALAPPDATA%\\tosu\\tosu.exe (where tosu works: it refuses to start from inside OneDrive).
    Read at call time so tests can point LOCALAPPDATA at a temp folder."""
    la = os.environ.get("LOCALAPPDATA", "")
    return Path(la) / "tosu" / "tosu.exe" if la else None


def in_onedrive(path):
    """True when `path` lies inside a OneDrive folder (by the OneDrive env vars or a 'OneDrive' path part)."""
    try:
        p = Path(os.path.expandvars(str(path))).resolve()
    except (OSError, ValueError):
        p = Path(str(path))
    low = str(p).lower()
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root:
            r = str(Path(root)).lower().rstrip("\\/")
            if low == r or low.startswith(r + os.sep) or low.startswith(r + "/"):
                return True
    return any(part.lower().startswith("onedrive") for part in p.parts)


def _exe_ok(p):
    try:
        return p is not None and Path(p).is_file() and Path(p).suffix.lower() == ".exe"
    except OSError:
        return False


def tosu_status(settings_path=None):
    """Which tosu.exe to start, in this order (tosu refuses to run from inside OneDrive):
      1. pet_settings.json 'tosu_path' if that file exists AND is not inside a OneDrive folder,
      2. %LOCALAPPDATA%\\tosu\\tosu.exe,
      3. <coach>/tosu/tosu.exe (last resort - inside OneDrive it won't start; Settings offers to move it).
    -> {"exe": Path|None, "source": "settings"|"localappdata"|"project"|None, "onedrive": bool,
        "ignored": str|None (a tosu_path skipped because it is in OneDrive), "movable": Path|None (folder to copy)}"""
    s = read_json(settings_path or SETTINGS_FILE) or {}
    out = {"exe": None, "source": None, "onedrive": False, "ignored": None, "movable": None}
    p = txt(s.get("tosu_path"))
    setting = None
    if p:
        try:
            setting = Path(os.path.expandvars(p))
        except (OSError, ValueError):
            setting = None
    if _exe_ok(setting):
        if not in_onedrive(setting):
            out.update(exe=setting, source="settings")
            return out
        out["ignored"] = str(setting)
    la = tosu_appdata_exe()
    if _exe_ok(la):
        out.update(exe=la, source="localappdata")
        return out
    for cand, src in ((setting, "settings"), (TOSU_LOCAL, "project")):
        if _exe_ok(cand):
            out.update(exe=Path(cand), source=src, onedrive=in_onedrive(cand), ignored=None)
            if out["onedrive"]:
                out["movable"] = Path(cand).parent
            return out
    return out


def find_tosu(settings_path=None):
    """Path to the tosu.exe to use (see tosu_status), or None. We never download tosu - the user installs
    it from its official GitHub releases page."""
    return tosu_status(settings_path)["exe"]


def launch_tosu_path(settings_path=None):
    """What the launcher should start: the tosu.exe outside OneDrive, else '' (a copy inside OneDrive refuses
    to start, so starting it only produces an error log; the Settings tab offers to move it)."""
    st = tosu_status(settings_path)
    return str(st["exe"]) if st["exe"] is not None and not st["onedrive"] else ""


TOSU_SKIP_DIRS = {"logs", "log"}


def move_tosu_out(src_dir, dest_dir=None):
    """Copy the tosu folder to %LOCALAPPDATA%\\tosu (or dest_dir), leaving out its logs. Never deletes or
    overwrites the source. -> (ok, message, new exe path or None)."""
    import shutil
    src = Path(src_dir)
    if dest_dir is None:
        la = tosu_appdata_exe()
        if la is None:
            return False, "I can't find your local app folder (LOCALAPPDATA).", None
        dest_dir = la.parent
    dest = Path(dest_dir)
    if not (src / "tosu.exe").is_file():
        return False, f"There's no tosu.exe in {src}.", None
    if in_onedrive(dest):
        return False, "That folder is inside OneDrive too - tosu won't start there.", None
    if (dest / "tosu.exe").is_file():
        # never copy an older OneDrive copy over a tosu that already lives there (it may be newer)
        return True, f"tosu already lives in {dest} - nothing copied.", dest / "tosu.exe"

    def ignore(dirpath, names):
        return [n for n in names if n.lower() in TOSU_SKIP_DIRS or n.lower().endswith(".log")]
    try:
        shutil.copytree(src, dest, ignore=ignore, dirs_exist_ok=True)
    except Exception as e:  # noqa: BLE001
        log.warning("tosu copy failed: %s", e)
        return False, f"Copying failed: {e}", None
    exe = dest / "tosu.exe"
    if not exe.is_file():
        return False, "The copy finished but tosu.exe is missing - try again.", None
    log.info("tosu copied out of OneDrive: %s -> %s", src, dest)
    return True, f"Done! tosu now lives in {dest}.", exe


def tosu_url():
    return (os.environ.get("OSU_COACH_TOSU_URL") or "http://127.0.0.1:24050").rstrip("/")


def tosu_probe(timeout=0.8):
    """One read-only GET of tosu's /json/v2 -> "ok" (tosu sees osu!), "waiting" (tosu answers but can't see
    osu! yet, e.g. HTTP 500 'osu is not ready/running') or "down" (nothing answers)."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(tosu_url() + "/json/v2", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return "ok" if isinstance(d, dict) and "error" not in d else "waiting"
    except urllib.error.HTTPError:
        return "waiting"
    except Exception:
        return "down"


def tosu_running(timeout=0.8):
    return tosu_probe(timeout) == "ok"


# ----------------------------------------------------------------------------
# live PP bubble text (pp_live.LiveWatcher state -> lines)
# ----------------------------------------------------------------------------

SCREENS = ("song_select", "playing", "results", "menu", "other")


def screen_of(state):
    s = txt(g(state, "screen"), "other").lower()
    return s if s in SCREENS else "other"


def map_key(state):
    """Identity of the hovered map + mods + rate (the bubble changes when this changes)."""
    ident = txt(g(state, "md5")) or txt(g(state, "beatmap_id")) or f"{txt(g(state, 'title'))}|{txt(g(state, 'version'))}"
    r = num(g(state, "rate"))
    return (ident, txt(g(state, "mods"), "NM").upper(), round(r, 3) if r else 1.0)


def fmt_pp(x):
    v = num(x)
    if v is None:
        return None
    return f"{v:.0f}pp" if v >= 9.5 else f"{v:.1f}pp"


def results_gain(state):
    """PP the last result would add to the profile total (>0), else None. None for maps tosu reports as
    unranked/loved (no pp) - the caller still has to check that this result is a fresh play of his."""
    res = g(state, "results")
    if g(res, "ranked") is False or g(state, "ranked") is False:
        return None
    v = num(g(res, "rank_gain_pp"))
    return v if v is not None and v > 0.05 else None


def results_sig(state):
    """Identity of one result (map + pp + acc + misses + time) so the same score is never celebrated twice."""
    res = g(state, "results")
    return (txt(g(state, "md5")), num(g(res, "pp")), num(g(res, "acc")), num(g(res, "misses")),
            txt(g(res, "created_at")))


def sample_live_states():
    """Fake pp_live.LiveWatcher states for --selftest (song select -> mods change -> play -> results)."""
    pred = {"stars": 4.62, "pp_ss": 48.3, "pp_fc_pred_acc": 36.0, "pp_expected": 19.6, "pp_good_run": 28.4,
            "acc_expected": 0.915, "misses_expected": 6, "acc_good": 0.95, "misses_good": 2,
            "confidence": "medium", "basis": "31 of your plays between 4.3 and 4.9 stars"}
    base = {"tosu_ok": True, "md5": "f00d", "beatmap_id": 123, "artist": "TrySail", "title": "whiz",
            "version": "Insane", "mods": "NM", "rate": 1.0, "stars": 4.64, "prediction": pred, "results": None}
    hd = dict(base, mods="HD", rate=1.2, stars=5.4,
              prediction=dict(pred, stars=5.41, pp_ss=92.0, pp_expected=31.2, pp_good_run=45.0, confidence="low"))
    return [
        dict(base, screen="menu", prediction=None),
        dict(base, screen="song_select"),
        dict(hd, screen="song_select"),
        dict(hd, screen="song_select", prediction=None, md5="beef", title="no estimate map"),
        dict(hd, screen="playing"),
        dict(hd, screen="results", results={"pp": 44.0, "acc": 0.951, "misses": 2, "rank_gain_pp": 12.4}),
        dict(hd, screen="song_select", prediction="junk", stars=None, mods=None, rate="x"),
        # opening a leaderboard score / replay from song select: results screen WITHOUT a play -> no party
        dict(hd, screen="results", results={"pp": 61.0, "acc": 0.982, "misses": 0, "rank_gain_pp": 20.1}),
        {"tosu_ok": False, "screen": "other"},
    ]


# ----------------------------------------------------------------------------
# built-in sample (matches CONTRACT.md) for --selftest / first launch preview
# ----------------------------------------------------------------------------

def sample_data():
    now = int(time.time())
    recs = [
        {"id": "a1", "purpose": "warmup", "artist": "Sample Artist", "title": "Gentle Morning", "version": "Normal",
         "creator": "mapperA", "beatmap_id": 111111, "set_id": 22222, "mods": "NM", "rate": 1.0,
         "bpm": 140, "ar": 6.0, "od": 5.0, "cs": 4.0, "length_s": 92,
         "reason": "Relaxed 140 BPM rhythm to settle your timing before the hard stuff.",
         "goal": "95% accuracy, watch the hit error bar stay centred.", "search": "Sample Artist Gentle Morning Normal"},
        {"id": "a2", "purpose": "accuracy", "artist": "Example Band", "title": "Clockwork", "version": "Hard",
         "creator": "mapperB", "beatmap_id": None, "set_id": None, "mods": "NM", "rate": 1.0,
         "bpm": 170, "ar": 8.0, "od": 7.0, "cs": 4.0, "length_s": 140,
         "reason": "Clean 1/2 rhythm at OD7 - the best place to bring UR down.",
         "goal": "UR under 300 and 92%+ accuracy.", "search": "Example Band Clockwork Hard"},
        {"id": "a3", "purpose": "speed", "artist": "Demo", "title": "Stream Practice", "version": "Insane",
         "creator": "mapperC", "beatmap_id": 333333, "set_id": 44444, "mods": "HT", "rate": 0.85,
         "bpm": 175, "ar": 9.0, "od": 8.0, "cs": 4.0, "length_s": 95,
         "reason": "Long 175-BPM streams; at 0.85x that's 149 BPM, right at your break point.",
         "goal": "No stream misses, then raise to 0.9x.", "search": "Demo Stream Practice Insane"},
        {"id": "a4", "purpose": "aim", "artist": "Placeholder", "title": "Big Jumps", "version": "Insane",
         "creator": "mapperD", "beatmap_id": 555555, "set_id": 66666, "mods": "NM", "rate": 1.0,
         "bpm": 180, "ar": 9.0, "od": 8.0, "cs": 4.0, "length_s": 120,
         "reason": "Jump spacing you already land well - keeps aim warm while you fix timing.",
         "goal": "Under 5 misses.", "search": "Placeholder Big Jumps Insane"},
        {"id": "a5", "purpose": "push", "artist": "Test", "title": "Reach", "version": "Extra",
         "creator": "mapperE", "beatmap_id": 777777, "set_id": 88888, "mods": "DT", "rate": 1.2,
         "bpm": 160, "ar": 8.5, "od": 8.0, "cs": 4.2, "length_s": 110,
         "reason": "A step above your level; 1.2x makes it 192 BPM.", "goal": "Pass it, any accuracy.",
         "search": "Test Reach Extra"},
    ]
    d = {
        "version": 1, "generated_at": now - 300, "player": "player", "n_plays": 40, "latest_play_ts": now - 600,
        "mood": "neutral", "headline": "UR 466 - let's bring it under 400 this week!",
        "latest_play": {"map": "Demo - Stream Practice [Insane]", "mods": "NM", "acc": 0.584, "misses": 36, "ur": 384,
                        "comment": "27 of 36 misses were streams breaking in the second half."},
        "stats": {"ur": 466.0, "mean_error_ms": 14.1, "avg_acc": 0.66, "near_fc_rate": 0.05,
                  "stream_comfort_bpm": 140, "stream_break_bpm": 150, "stream_drift_ms": 32.2,
                  "single_tap_ratio": 0.10, "jump_miss_rate": 0.063, "jump_landing_radii": -0.05,
                  "jump_spike_spacing": 5, "comfort_aim_velocity": 1.9, "typical_ar": 8.0, "typical_od": 6.0,
                  "typical_bpm": 180},
        "focus": [
            {"id": "ur", "title": "Accuracy / rhythm consistency", "priority": 1,
             "why": "Your UR is 466; 6-7* players sit around 100-180.",
             "drill": "15 min on maps well below your level aiming for 95% accuracy.",
             "metric": "UR", "current": 466, "target": 300},
            {"id": "streams", "title": "Streams / speed", "priority": 2,
             "why": "Streams break around 150 BPM and you drift +32 ms late by the end.",
             "drill": "Tap 140 BPM streams for 30-60 s sets, rest equal time; +5 BPM when UR stays stable.",
             "metric": "stream break BPM", "current": 150, "target": 180},
        ],
        "session_plan": [
            {"block": "Warm-up", "minutes": 10, "goal": "95% acc, relaxed tapping", "pick_ids": ["a1"]},
            {"block": "Accuracy", "minutes": 20, "goal": "UR under 300", "pick_ids": ["a2"]},
            {"block": "Speed", "minutes": 20, "goal": "Clean streams at 0.85x", "pick_ids": ["a3", "missing-id"]},
            {"block": "Push", "minutes": 15, "goal": "One attempt at a harder map", "pick_ids": ["a5"]},
        ],
        "recommendations": recs,
        "trouble_spots": [
            {"map": "Demo - Stream Practice [Insane]", "id": "a3", "start_ms": 83000, "end_ms": 95000,
             "start": "1:23", "end": "1:35", "misses": 9, "kind": "streams",
             "reason": "timing drifted late during the 16-note stream",
             "advice": "Replay this section at 0.8x until clean, then 0.9x, then 1.0x.", "practice_rate": 0.8},
            {"map": "Placeholder - Big Jumps [Insane]", "id": "a4", "start_ms": 40000, "end_ms": 47000,
             "misses": 4, "kind": "jumps", "reason": "aim - tapped in time but cursor off the circle",
             "advice": "Slow it to 0.9x and focus on stopping on each circle.", "practice_rate": 0.9},
        ],
        "recent_plays": [
            {"ts": now - 600 - i * 1800, "map": f"Sample map {i + 1} [Hard]", "id": "x", "mods": ["NM", "HD", "DT"][i % 3],
             "acc": 0.55 + (i % 7) * 0.05, "misses": (i * 7) % 30, "ur": 420 + (i * 13) % 80, "max_combo": 100 + i * 20,
             "verdict": ["too hard", "good practice", "too easy"][i % 3], "main_issue": ["streams", "jumps", "rhythm"][i % 3]}
            for i in range(12)
        ],
        "trends": [
            {"week": "2026-09-28", "plays": 20, "ur": 510, "acc": 0.61, "misses_per_100": 8.4, "stream_break_bpm": 140},
            {"week": "2026-10-05", "plays": 25, "ur": 495, "acc": 0.62, "misses_per_100": 8.0, "stream_break_bpm": 140},
            {"week": "2026-10-12", "plays": 18, "ur": None, "acc": 0.64, "misses_per_100": 7.6, "stream_break_bpm": None},
            {"week": "2026-10-19", "plays": 33, "ur": 480, "acc": 0.65, "misses_per_100": 7.1, "stream_break_bpm": 150},
            {"week": "2026-10-26", "plays": 31, "ur": 470, "acc": 0.65, "misses_per_100": 7.1, "stream_break_bpm": 150},
            {"week": "2026-11-02", "plays": 29, "ur": 455, "acc": 0.67, "misses_per_100": 6.5, "stream_break_bpm": 150},
        ],
        "notes": ["Sample data - the real analysis appears after coach_engine.py runs.",
                  "Slider bodies are not judged; star rating not computed (difficulty shown as BPM/AR/OD/spacing)."],
        # ---- CONTRACT_PP.md keys
        "phase": {"name": "re-sharpen", "since": "2026-10-12",
                  "why": "You're back after a break. A level you reached before usually returns faster than it took "
                         "to build, so first we get timing and aim sharp again before chasing new star ratings."},
        "roadmap": {"current_stars": 4.3, "basis": "Median star rating of your recent passes with 90%+ accuracy (sample)",
                    "bands": [
                        {"stars": 4.0, "status": "done", "checkpoints": [
                            {"metric": "accuracy on 4.0* maps", "target": ">= 93%", "current": "93.4%", "met": True}],
                         "focus": "Comfort zone.", "typical_maps": "TV-size anime Insanes, 170-180 BPM"},
                        {"stars": 4.5, "status": "current", "checkpoints": [
                            {"metric": "UR on 4.5* maps", "target": "<= 250", "current": "388", "met": False},
                            {"metric": "accuracy on 4.5* maps", "target": ">= 94%", "current": "91.2%", "met": False},
                            {"metric": "passes with <= 3 misses", "target": ">= 5", "current": "6", "met": True}],
                         "focus": "Re-sharpen: timing first, then jump aim.",
                         "typical_maps": "Anime/J-rock Insanes, 170-190 BPM"},
                        {"stars": 5.0, "status": "next", "checkpoints": [
                            {"metric": "stream break BPM", "target": ">= 170", "current": "150", "met": False}],
                         "focus": "Back-to-back jumps and 9-16 note streams.",
                         "typical_maps": "5* Insanes / light Extras"},
                        {"stars": 5.5, "status": "later", "checkpoints": [],
                         "focus": "New territory: needs a speed base (streams at 180+ BPM).", "typical_maps": "Extras"},
                        {"stars": 6.0, "status": "later", "checkpoints": [], "focus": "", "typical_maps": ""},
                        {"stars": 7.0, "status": "later", "checkpoints": [
                            {"metric": "stream break BPM", "target": ">= 220", "current": "150", "met": False}],
                         "focus": "The goal. Usually many months of steady practice from 5*.", "typical_maps": ""},
                    ]},
        "techniques": [
            {"id": "hit-error", "category": "timing", "title": "Centre your hit-error bar",
             "for_you": "You hit about +12 ms late on average; fixing the bias is the cheapest accuracy gain.",
             "steps": ["Turn on the hit error meter in the skin/HUD settings.",
                       "Play a comfortable map and watch where the ticks cluster.",
                       "Adjust your offset only if the bias is the same on many maps."],
             "mistakes": ["Changing offset after one map.", "Chasing the bar instead of the music."],
             "drill": "Two easy maps at the start of each session, eyes on the bar.",
             "evidence": "community consensus", "source": None},
            {"id": "alt", "category": "tapping", "title": "Alternate streams from a slower BPM",
             "for_you": "Your streams break around 150 BPM and drift late - build speed slowly and cleanly.",
             "steps": ["Pick stream maps 10 BPM under your break point.", "Raise 5 BPM only when clean."],
             "mistakes": ["Mashing at a BPM you can't hold.", "Tensing the forearm."],
             "drill": "3 sets of 60 s streams, equal rest.", "evidence": "coach heuristic", "source": None},
            {"id": "warmup", "category": "mindset", "title": "Warm up, rest, stop on pain",
             "for_you": "Short warm-ups and regular breaks keep you improving and protect your wrists.",
             "steps": ["10 min easy maps first.", "Break every 20-30 min.", "Stop if your wrist or hand hurts."],
             "mistakes": ["Grinding through pain."], "drill": "", "evidence": "research",
             "source": "general sports/RSI guidance"},
            {"id": "flow", "category": "aim", "title": "Snap vs flow aim",
             "for_you": "Most of your misses are on dense jump sections.",
             "steps": ["Slow the section down (Rate Adjust if your version has it)."], "mistakes": [],
             "drill": "", "evidence": "community consensus", "source": None},
        ],
        "setup_checks": [
            {"id": "sens", "title": "Sensitivity", "status": "ok", "detail": "CursorSensitivity 4.2",
             "how": "Fine if it feels comfortable; don't change it often."},
            {"id": "offset", "title": "Audio offset", "status": "check",
             "detail": "You hit +12 ms late on average.", "how": "Check it on a few easy maps before changing offset."},
            {"id": "wasapi", "title": "Experimental WASAPI", "status": "warn",
             "detail": "On - lowers latency but can shift your timing after toggling.",
             "how": "Keep it on or off and stick with it."},
        ],
        "pp": {"configured": True, "pp": 2000.0, "rank": 500000, "gain_7d": 12.3,
               "next_goal": "A 60pp play would add ~14pp to your total.", "top50_floor": 49.0, "tosu": False},
        "player_setup": {"device": "mouse", "keys": "keyboard", "style": None},
    }
    recs = d["recommendations"]
    recs.append({"id": "p1", "purpose": "pp", "artist": "TrySail", "title": "whiz", "version": "Insane",
                 "creator": "mapperF", "beatmap_id": 999999, "set_id": 11112, "mods": "HD", "rate": 1.0,
                 "bpm": 175, "ar": 9.0, "od": 8.0, "cs": 4.0, "length_s": 89, "stars": 4.9,
                 "reason": "6 players ranked 400-450k have this as a top play.",
                 "goal": "A clean 95% run.", "search": "TrySail whiz Insane",
                 "pp_expected": 52.0, "pp_good_run": 70.0, "pp_ss": 110.0, "est_gain_pp": 14.2})
    return d


def sample_pp_data():
    today = _dt.date.today()
    hist = [{"date": (today - _dt.timedelta(days=13 - i)).isoformat(), "pp": 1720.0 + i * 2.8 + (i % 3),
             "global_rank": 600000 - i * 900, "accuracy": 92.8 + i * 0.01} for i in range(14)]
    return {
        "version": 1, "generated_at": int(time.time()) - 600, "configured": True, "error": None,
        "user": {"id": 12345, "username": "player1", "pp": 2000.0, "global_rank": 500000, "country_rank": 20000,
                 "accuracy": 95.0, "play_count": 10000, "peak_rank": 200000, "peak_rank_date": "2020-01-01"},
        "history": hist,
        "top_plays": [{"beatmap_id": 100 + i, "md5": "", "artist": "Artist", "title": f"Top play {i + 1}",
                       "version": "Insane", "stars": 4.9 - i * 0.05, "mods": ["NM", "HD"][i % 2], "pp": 103.0 - i * 4,
                       "acc": 0.986 - i * 0.004, "date": f"201{6 + i % 2}-0{1 + i % 9}-10"} for i in range(12)],
        "pp_threshold": {"top1": 103.0, "top50": 49.0, "top100": 40.0},
        "climb": [
            {"beatmap_id": 999999, "set_id": 11112, "md5": "p1", "artist": "TrySail", "title": "whiz",
             "version": "Insane", "stars": 4.9, "bpm": 175, "ar": 9.0, "od": 8.0, "cs": 4.0, "length_s": 89,
             "mods": "HD", "typical_pp": 85.0, "typical_acc": 0.96, "source_players": 6, "owned": True,
             "pp_predicted": 70.0, "est_gain_pp": 14.2,
             "reason": "6 players ranked 400k-450k have this as a top play"},
            {"beatmap_id": 888888, "set_id": 22223, "md5": "", "artist": "Some Band", "title": "Not Owned",
             "version": "Extra", "stars": 5.1, "bpm": 190, "ar": 9.3, "od": 8.5, "cs": 4.0, "length_s": 95,
             "mods": "NM", "typical_pp": 92.0, "typical_acc": 0.95, "source_players": 4, "owned": False,
             "pp_predicted": 61.0, "est_gain_pp": 9.8, "reason": "4 players slightly above you farm this"},
        ],
    }


def sample_debrief(kind="pass", now=None):
    """One last_debrief per situation for --selftest / screenshots: pass, close, fail, free-play, break,
    level-up, level-down, sparse."""
    now = int(now or time.time())
    base = {"ts": now - 20, "source": "replay", "map": "Demo - Stream Practice [Insane]", "md5": "a3", "mods": "HT",
            "rate": 0.85, "block": "Speed", "track": "speed", "level_change": None}
    if kind in ("pass", "level-up"):
        d = dict(base, verdict="pass", headline="Clean streams at 150 BPM - level up: 150 BPM -> 155 BPM!",
                 observations=["You drifted 12 ms late in the 1:12 stream (last try: 35 ms - much better)",
                               "No misses in any stream this time"],
                 compare={"vs": "last attempt on this map", "acc": "+3.1%", "ur": "-22", "misses": "-4"},
                 next_action={"type": "faster", "text": "Go up to 0.90x on the same map (155 BPM)", "rate": 0.90,
                              "md5": "a3", "why": "Clean streams twice in a row at 150 BPM."},
                 level_change={"track": "speed", "from": 150, "to": 155})
        if kind == "pass":
            d.update(headline="Clean streams at 150 BPM (1% stream misses) - 1 of 2!", level_change=None,
                     next_action={"type": "retry", "text": "Same again: one more like that moves you to 155 BPM",
                                  "rate": 0.85, "md5": "a3", "why": "Two passes in a row = level up."})
        return d
    if kind == "close":
        return dict(base, verdict="close", headline="Almost! 2 stream misses - goal is 0",
                    observations=["Both misses were at 1:40-1:44: the long stream after the break",
                                  "You started that stream 18 ms late and never caught up"],
                    compare={"vs": "last attempt on this map", "acc": "+0.8%", "ur": "-6", "misses": "-3"},
                    next_action={"type": "retry", "text": "Same map, same speed - start the 1:40 stream relaxed",
                                 "rate": 0.85, "md5": "a3", "why": "You're within one or two misses of the goal."})
    if kind in ("fail", "level-down"):
        d = dict(base, verdict="fail", source="live", headline="12% stream misses on Stream Practice - easing to 150 BPM",
                 observations=["9 of 12 misses at 0:58-1:15: streams at ~155 BPM",
                               "You drifted 40 ms late by the end of the 0:58 stream (24 notes, 155 BPM)"],
                 compare={"vs": "your average for this step", "acc": "-6.2%", "ur": "+48", "misses": "+9"},
                 next_action={"type": "slower", "text": "Same map at 0.85x (150 BPM)", "rate": 0.85, "md5": "a3",
                              "why": "12% stream misses at 155 BPM - the level steps down one after a miss of the goal."},
                 rate=0.90)
        if kind == "fail":
            d["headline"] = "12% stream misses on Stream Practice - not this time"
            d["next_action"] = {"type": "retry", "text": "Retry, same map and speed - the misses were the streams "
                                "at 0:58-1:15: relaxed fingers there", "rate": 0.90, "md5": "a3",
                                "why": "Today's level already dropped once (max one step per session)."}
        if kind == "level-down":
            d["level_change"] = {"track": "speed", "from": 155, "to": 150}
        return d
    if kind == "free-play":
        return dict(base, verdict="free-play", map="Camellia - Exit This Earth's Atomosphere [Extra]", md5="zz",
                    mods="NM", rate=1.0, block=None, track=None,
                    headline="Free play: Exit This Earth's Atomosphere 92.4%, 19 misses",
                    observations=["14 of 19 misses at 2:05-2:30: jumps (~1.40 px/ms), cursor off the circle",
                                  "5.4★ is 0.6★ above your Push level (4.8★), so 19 misses is normal. To level up: "
                                  "pass a 4.8-5.1★ map with 5 misses or fewer"],
                    compare={"vs": "last attempt on this map", "acc": "+1.5%", "ur": "-12", "misses": "-2"},
                    next_action={"type": "next_map", "text": FREE_NOTE + " Back to Speed: Stream Practice at 0.85x",
                                 "rate": 0.85, "md5": "a3", "why": "Free plays get looked at tomorrow morning."})
    if kind == "break":
        return dict(base, verdict="close", headline="Your timing is getting worse - time for a rest",
                    observations=["UR 395 on the last 3 tries vs 330 at the start of the session"],
                    compare={"vs": "your first 3 tries today", "ur": "+65"},
                    next_action={"type": "break", "text": "Stand up, shake your hands out, look away for 5 minutes",
                                 "why": "Tired hands learn the wrong movement."})
    # sparse: almost nothing filled in
    return {"ts": now - 5, "verdict": "weird", "headline": None, "observations": "one string", "compare": 5,
            "next_action": "go", "level_change": {"from": "a"}}


def sample_adapt_data(kind="pass"):
    """sample_data() plus the CONTRACT_ADAPT.md keys (session, last_debrief, debriefs_today, changes, tracks)."""
    d = sample_data()
    now = int(time.time())
    up = kind == "level-up"
    srate = 0.90 if up else 0.85
    sgoal = ("Streams at 155 BPM: almost no stream misses, don't fall behind the beat. Two clean runs: 160 BPM."
             if up else "Streams at 150 BPM: almost no stream misses, don't fall behind the beat. Two clean runs: "
             "155 BPM.")
    steps = [
        {"block": "Warm-up", "track": "warmup", "minutes": 8, "state": "done", "attempts": 2, "passes": 1,
         "pick_md5": "a1", "rate": 1.0, "mods": "NM", "goal": "Two easy maps to loosen up. Aim for 93%, but nothing "
         "is judged.", "exit_rule": "2 plays or 8 min"},
        {"block": "Accuracy", "track": "accuracy", "minutes": 15, "state": "done", "attempts": 4, "passes": 2,
         "pick_md5": "a2", "rate": 1.0, "mods": "NM", "goal": "95%+ on easy maps at OD 4.0 (95% there needs UR ~315).",
         "exit_rule": "15 min or 6 tries (2 passes = level up, keep going)"},
        {"block": "Speed", "track": "speed", "minutes": 15, "state": "active", "attempts": 3, "passes": 0 if up else 1,
         "pick_md5": "a3", "rate": srate, "mods": "HT", "goal": sgoal,
         "exit_rule": "15 min or 6 tries (2 passes = level up, keep going)"},
        {"block": "Aim", "track": "aim", "minutes": 12, "state": "active", "attempts": 0, "passes": 0,
         "pick_md5": "a4", "rate": 1.0, "mods": "NM", "goal": "Jump misses under 3%", "exit_rule": "12 min or 6 tries (2 passes = level up, keep going)"},
        {"block": "Push", "track": "push", "minutes": 10, "state": "active", "attempts": 0, "passes": 0,
         "pick_md5": "a5", "rate": 1.2, "mods": "DT", "goal": "Pass a 4.8★ map with 10 misses or fewer",
         "exit_rule": "10 min, 3 tries per map"},
    ]
    rec = {r["id"]: r for r in d["recommendations"]}
    d["session"] = {"date": today(), "started": now - 1800, "step_index": 2, "steps": steps,
                    "fatigue": {"ur_trend": 8, "flag": False}, "tilt": {"fails_in_row": 0, "flag": False},
                    "current": {"block": "Speed", "track": "speed", "map": rec["a3"], "rate": srate, "mods": "HT",
                                "goal": sgoal, "attempt_n": 4, "passes": 0 if up else 1, "needed": 2 if up else 1}}
    deb = sample_debrief(kind, now)
    d["last_debrief"] = deb
    older = [sample_debrief("close", now - 400), sample_debrief("fail", now - 700),
             dict(sample_debrief("pass", now - 1300), block="Accuracy", track="accuracy", map=U_MAP_ACC,
                  headline="95.3% - accuracy goal reached", mods="NM", rate=1.0,
                  next_action={"type": "next_step", "text": "Accuracy done - on to Speed"})]
    for i, o in enumerate(older):
        o["ts"] = now - 400 - i * 300
    d["debriefs_today"] = [deb] + older
    d["changes"] = ["Stream practice speed: 145 BPM -> 150 BPM (Clean streams twice in a row at 145 BPM)",
                    "Accuracy (OD at 95%): OD 4.5 -> OD 4.0 (89.1% (goal 95%) on The Emptiness Machine at OD 4.6)",
                    "More time on Speed (it's your weakest track), less on Aim (it's improving)"]
    d["tracks"] = {
        "accuracy": {"level": 4.0, "unit": "OD", "label": "Accuracy (OD at 95%)", "trend": "down",
                     "next": "Pass twice in a row to reach OD 4.5; a miss of the goal drops it to OD 3.5"},
        "speed": {"level": 155 if up else 150, "unit": "BPM", "label": "Stream practice speed", "trend": "up",
                  "next": ("Pass twice in a row to reach 160 BPM; a miss of the goal drops it to 150 BPM" if up else
                           "Pass once more to reach 155 BPM; a miss of the goal drops it to 145 BPM")},
        "aim": {"level": 2.0, "unit": "px/ms", "label": "Jump speed", "trend": "flat",
                "next": "Pass twice in a row to reach 2.1 px/ms"},
        "stamina": {"level": 2.5, "unit": "min", "label": "Long maps (drain time)", "trend": "flat",
                    "next": "Hold your streams to the end of a 2.5 min map"},
        "reading": {"level": 9.0, "unit": "AR", "label": "Approach rate", "trend": "up", "next": "AR 9.3 next"},
        "push": {"level": 4.8, "unit": "★", "label": "Push maps", "trend": "flat", "next": "Pass with 10 misses or fewer"},
    }
    return d


U_MAP_ACC = "Example Band - Clockwork [Hard]"


def sparse_data():
    """Deliberately broken/sparse data for the selftest."""
    return {
        "session": {"date": None, "step_index": 99, "current": "junk",
                    "steps": [None, {}, {"block": 5, "state": 7, "attempts": "x", "passes": None, "pick_md5": 3,
                                         "rate": "fast", "exit_rule": None}]},
        "last_debrief": {"ts": "soon", "verdict": 3, "observations": "one string", "compare": [1],
                         "next_action": {"type": None, "rate": "x"}, "level_change": {"from": 1, "to": "2"}},
        "debriefs_today": [None, "junk", {"verdict": "pass"}, {}],
        "changes": [None, "", 5, "Something changed"],
        "tracks": {"speed": "x", "aim": {"level": "hi", "unit": None}, "push": {"level": 5, "trend": "sideways"}, "x": 1},
        "mood": "confused", "headline": None, "latest_play": None, "stats": None,
        "focus": [{}, {"title": "Only a title", "current": "high", "target": 0}, "junk"],
        "session_plan": [{"pick_ids": None}, {"block": "Lonely", "pick_ids": "not-a-list"}],
        "recommendations": [{}, {"title": "No artist", "beatmap_id": -5, "rate": "fast", "bpm": True}],
        "trouble_spots": [{"misses": None}],
        "recent_plays": [{}, {"ts": 99999999999999, "acc": 97.2}],
        "trends": [{"week": "?"}, {"ur": 300}],
        "notes": "a single string",
        "phase": "re-sharpen",
        "roadmap": {"current_stars": "high", "bands": [{}, {"stars": None, "status": "weird",
                                                          "checkpoints": [{}, "x", {"met": "yes"}]}, 5]},
        "techniques": [{}, {"title": "No category", "evidence": "rumour", "steps": "one string", "mistakes": None},
                       "junk"],
        "setup_checks": [{}, {"status": None, "title": 3}],
        "pp": {"pp": "lots", "gain_7d": None},
        "player_setup": ["mouse"],
    }


def sparse_pp_data():
    return {"configured": False, "error": "no config", "user": {"pp": None, "global_rank": "x"},
            "history": [{"date": "?"}, {"pp": 5}, {}], "top_plays": [{}, {"pp": "a lot", "acc": 2}],
            "climb": [{}, {"owned": "maybe", "beatmap_id": 1.5, "est_gain_pp": True}], "pp_threshold": 7}


# ============================================================================
# PP-first Mochi (CONTRACT_PP2.md): the PP card, hover bubble, results bubble, rank-up picks, model accuracy.
# Everything below is pure (no Tk): view models the card / panel / bubbles just lay out, so they can be
# checked without a window. pp_recs.json, pp_predictions.jsonl, model_report() and pp_live predictions are
# written by other modules: any key may be missing or oddly typed.
# ============================================================================

def _p01(x):
    """A probability 0..1 (accepts 0..100 too); None if unknown."""
    v = num(x)
    if v is None:
        return None
    if v > 1.0001:
        v /= 100.0
    return max(0.0, min(1.0, v))


def _q(d, key, q="p50"):
    """pp_model style {"p20":..,"p50":..,"p80":..} or a plain number."""
    v = g(d, key)
    if isinstance(v, dict):
        return num(v.get(q))
    return num(v) if q == "p50" else None


def fmt_pp_total(x):
    v = num(x)
    return f"{v:,.0f}pp" if v is not None else None


def fmt_gain(x, digits=None):
    v = num(x)
    if v is None:
        return None
    if digits is None:
        digits = 0 if abs(v) >= 9.5 else 1
    return f"{v:+.{digits}f}pp"


def fmt_pass(p):
    v = _p01(p)
    if v is None:
        return None
    pct = v * 100
    return "under 1%" if 0 < pct < 1 else f"{pct:.0f}%"


def pred_view(pred):
    """Normalise a prediction (pp_model.predict_pp, pp_live's older predict(), or a pp_recs pick) -> dict with
    expected / low / high / fc / ss / p_pass / gain / gain_high / acc / misses / confidence / basis / error,
    or None when it has no pp at all."""
    if not isinstance(pred, dict):
        return None
    exp = num(g(pred, "pp_expected"))
    ss = num(g(pred, "pp_ss"))
    if exp is None and ss is None:
        return None
    low = num(g(pred, "pp_low"))
    high = num(g(pred, "pp_high"))
    if high is None:
        high = num(g(pred, "pp_good_run"))
    fc = num(g(pred, "pp_fc"))
    if fc is None:
        fc = num(g(pred, "pp_fc_pred_acc"))
    acc = _q(pred, "acc")
    if acc is None:
        acc = num(g(pred, "acc_expected"))
    mis = _q(pred, "misses")
    if mis is None:
        mis = num(g(pred, "misses_expected"))
    if low is not None and exp is not None and low > exp:
        low = exp
    if high is not None and exp is not None and high < exp:
        high = exp
    return {"expected": exp, "low": low, "high": high, "fc": fc, "ss": ss, "p_pass": _p01(g(pred, "p_pass")),
            "gain": num(g(pred, "gain_expected")), "gain_high": num(g(pred, "gain_high")),
            "gain_if_pass": num(g(pred, "gain_if_pass")), "n_similar": pos_int(g(pred, "n_similar")),
            "stars": num(g(pred, "stars")), "acc": acc, "misses": mis,
            "confidence": txt(g(pred, "confidence")).lower() or None, "basis": txt(g(pred, "basis")),
            "error": num(g(pred, "model_error_pp"))}


def weighted_gain(new_pp, top_plays, beatmap_id=None, md5=None, total_pp=None):
    """How much a play worth new_pp raises the weighted total (top 100, 0.95^i), replacing a lower score on
    the same map. Same idea as pp_live.rank_gain_pp, for predictions that don't carry gain_expected."""
    v = num(new_pp)
    if v is None or v <= 0:
        return None
    plays = [p for p in (top_plays if isinstance(top_plays, list) else []) if isinstance(p, dict)
             and num(p.get("pp")) is not None]
    if not plays:
        return None
    bid, md5 = pos_int(beatmap_id), txt(md5)

    def same(p):
        return (bid and pos_int(p.get("beatmap_id")) == bid) or (md5 and txt(p.get("md5")) == md5)
    pps = sorted((num(p["pp"]) for p in plays), reverse=True)[:100]
    old = sum(x * 0.95 ** i for i, x in enumerate(pps))
    mine = [num(p["pp"]) for p in plays if same(p)]
    rest = [num(p["pp"]) for p in plays if not same(p)]
    best_here = max(mine) if mine else None
    new_list = sorted(rest + [max(v, best_here or 0)], reverse=True)[:100]
    new = sum(x * 0.95 ** i for i, x in enumerate(new_list))
    gain = new - old
    # below-#100 correction (same as pp_live.tail_adjust): the old #100 moves to #101 instead of vanishing
    tot = num(total_pp)
    if tot is not None and len(pps) >= 100 and not mine and v > pps[99]:
        tail = max(0.0, tot - 416.6667 * (1 - 0.995 ** 1000) - old)
        gain += pps[99] * 0.95 ** 100 - 0.05 * tail
    return max(0.0, gain)


def pp_header(pp_data, recs=None, coach=None):
    """Total pp + rank + change today (vs the last daily snapshot before today)."""
    user = g(pp_data, "user") if isinstance(g(pp_data, "user"), dict) else {}
    total = num(g(user, "pp"))
    rank = num(g(user, "global_rank"))
    if total is None:
        total = num(g(recs, "total_pp"))
    if rank is None:
        rank = num(g(recs, "rank"))
    summ = g(coach, "pp") if isinstance(g(coach, "pp"), dict) else {}
    if total is None:
        total = num(g(summ, "pp"))
    if rank is None:
        rank = num(g(summ, "rank"))
    today_s = _dt.date.today().isoformat()
    base = None
    for h in dicts(g(pp_data, "history")):
        d = txt(g(h, "date"))[:10]
        if len(d) == 10 and d < today_s and num(g(h, "pp")) is not None:
            if base is None or d >= txt(g(base, "date"))[:10]:
                base = h
    change = rank_change = None
    if base is not None and total is not None:
        change = round(total - num(g(base, "pp")), 1)
        br = num(g(base, "global_rank"))
        if br is not None and rank is not None:
            rank_change = int(br - rank)   # > 0 = climbed
    if change is None:
        today_txt = None
    elif abs(change) < 0.05:
        today_txt = "no change today yet"
    else:
        today_txt = f"{change:+.1f}pp today"
        if rank_change:
            today_txt += f" ({'▲' if rank_change > 0 else '▼'}{abs(rank_change):,} ranks)"
    return {"total": total, "rank": rank, "change": change, "rank_change": rank_change,
            "total_txt": fmt_pp_total(total), "rank_txt": f"#{rank:,.0f}" if rank is not None else None,
            "today_txt": today_txt}


MOD_VARIANT_KEYS = ("by_mods", "mod_variants", "variants", "alternatives", "alts", "alt_mods")


def mods_label(mods, rate=None):
    """fmt_mods, without the default speed of DT/NC (1.5x) and HT/DC (0.75x)."""
    ms, r = split_mods(mods), num(rate)
    if r is not None and ((any(m in ("DT", "NC") for m in ms) and abs(r - 1.5) < 0.004) or
                          (any(m in ("HT", "DC") for m in ms) and abs(r - 0.75) < 0.004)):
        r = None
    return fmt_mods(mods, r)


def norm_mods(m):
    s = "".join(split_mods(m)) if not isinstance(m, (list, tuple)) else "".join(txt(x).upper() for x in m)
    s = s.replace("NC", "DT")
    order = ["EZ", "HD", "HR", "DT", "HT", "FL"]
    parts = [s[i:i + 2] for i in range(0, len(s), 2)]
    parts = [p for p in parts if p not in ("NM", "")]
    parts.sort(key=lambda p: order.index(p) if p in order else 9)
    return "".join(parts) or "NM"


def mod_alternatives(pred=None, recs=None, md5=None):
    """[{mods, expected, gain, p_pass}] for other mod choices on the same map, from the prediction itself
    (any of MOD_VARIANT_KEYS, dict keyed by mods or a list) and from pp_recs picks of the same md5."""
    out = {}

    def add(mods, d):
        v = pred_view(d) if isinstance(d, dict) else None
        if v is None or v["expected"] is None:
            return
        k = norm_mods(mods)
        if k not in out or (v["expected"] or 0) > (out[k]["expected"] or 0):
            out[k] = {"mods": k, "expected": v["expected"], "gain": v["gain"], "p_pass": v["p_pass"]}
    for key in MOD_VARIANT_KEYS:
        alts = g(pred, key)
        if isinstance(alts, dict):
            for m, d in alts.items():
                add(m, d)
        elif isinstance(alts, list):
            for d in alts:
                if isinstance(d, dict):
                    add(g(d, "mods"), d)
    md5 = txt(md5)
    if md5:
        for p in dicts(g(recs, "picks")) + dicts(g(recs, "smash_picks")):
            if txt(g(p, "md5")) == md5 and (num(g(p, "rate")) or 1.0) == 1.0:
                add(g(p, "mods"), p)
                for a in dicts(g(p, "alt_mods")):
                    add(g(a, "mods"), a)
    return list(out.values())


def mod_tip(cur_mods, cur_view, alts):
    """'Try HD: +5pp expected' when another mod choice is clearly better for this map (None otherwise)."""
    if not cur_view or cur_view.get("expected") is None or not alts:
        return None
    cur = norm_mods(cur_mods)
    pc = cur_view.get("p_pass")

    def value(exp, pp_):   # expected pp x pass chance: a +16pp mod you'd pass 30% of the time is no tip
        return exp * (pp_ if pp_ is not None else (pc if pc is not None else 1.0))
    cur_val = value(cur_view["expected"], pc)
    best = None
    for a in alts:
        if a["mods"] == cur or a.get("expected") is None:
            continue
        diff = a["expected"] - cur_view["expected"]
        dv = value(a["expected"], a.get("p_pass")) - cur_val
        if diff >= 2.0 and dv >= 1.5 and (best is None or dv > best[2]):
            best = (a, diff, dv)
    if best is None:
        others = [a for a in alts if a["mods"] != cur]
        if len(others) >= 2:
            return f"{'No mods' if cur == 'NM' else cur} is your best choice here"
        return None
    a, diff, _dv = best
    tip = f"Try {'no mods' if a['mods'] == 'NM' else a['mods']}: {diff:+.0f}pp expected"
    pa = a.get("p_pass")
    if pc is not None and pa is not None and pc - pa >= 0.1:
        tip += f", but pass chance {fmt_pass(pc)} → {fmt_pass(pa)}"
    return tip


def gain_if_pass(view):
    """Gain to your total GIVEN a pass (the number the bubble and the pick list both show): pp_model's
    gain_if_pass, else gain_expected / pass chance. None when unknown."""
    if not isinstance(view, dict):
        return None
    gp = num(view.get("gain_if_pass"))
    if gp is not None:
        return gp
    gain, p = num(view.get("gain")), num(view.get("p_pass"))
    if gain is None:
        return None
    return gain / p if p and p > 0.05 else gain


def gain_words(view, top_plays=None, beatmap_id=None, md5=None, floor=None, ranked=None):
    """'+9pp to your total if you pass it' (gain_expected already includes the pass chance: undo that), or why
    it wouldn't count. -> (text, gain_if_pass) ; text None when unknown."""
    if ranked is False:
        return "not ranked: no pp from this map", None
    if not view or view.get("expected") is None:
        return None, None
    g_pass = gain_if_pass(view)
    if g_pass is None:
        g_pass = weighted_gain(view["expected"], top_plays, beatmap_id, md5)
    if g_pass is None:
        return None, None
    if g_pass >= 0.05:
        return f"{fmt_gain(g_pass)} to your total if you pass it", g_pass
    fl = num(floor)
    if fl is not None and view["expected"] < fl:
        return f"too low for your top 100 (it starts at {fl:.0f}pp)", 0.0
    return "won't raise your total (you have better plays)", 0.0


def live_view(state, pp_data=None, recs=None, alts_extra=None):
    """The hovered map (pp_live state) -> everything the bubble and the PP card show, or None."""
    if not isinstance(state, dict):
        return None
    pred = g(state, "prediction")
    pred = pred if isinstance(pred, dict) else {}
    v = pred_view(pred)
    stars = (v or {}).get("stars") or num(g(state, "stars"))
    mods, rate = g(state, "mods"), g(state, "rate")
    name = txt(g(state, "title"))
    if txt(g(state, "version")):
        name = f"{name} [{txt(g(state, 'version'))}]" if name else f"[{txt(g(state, 'version'))}]"
    if not name and v is None and stars is None:
        return None
    short = name if len(name) <= 44 else name[:42].rstrip() + "…"
    title = " ".join(([f"{stars:.2f}★"] if stars is not None else []) + [mods_label(mods, rate)])
    ranked = g(state, "ranked")
    pst = txt(g(pred, "status")).lower()
    if ranked is None and pst and pst not in ("ranked", "approved"):
        ranked = False   # pp_model knows the map's status (loved / graveyard ...): no pp
    out = {"title": title, "name": short, "full_name": name, "mods": mods_label(mods, rate), "view": v,
           "big": None, "range": None, "fcss": None, "pass": None, "gain": None, "tip": None,
           "confidence": None, "basis": "", "status": None, "key": map_key(state)}
    if v is None:
        out["status"] = ("working it out..." if g(state, "prediction_pending") is True else
                         "no PP estimate for this one yet" if stars is not None else "looking at this map...")
        return out
    if ranked is False:
        out["status"] = "not ranked: this map gives no pp"
    out["big"] = f"~{fmt_pp(v['expected'])}" if v["expected"] is not None else None
    lo, hi = v["low"], v["high"]
    if lo is not None and hi is not None:
        out["range"] = f"likely {lo:.0f}-{hi:.0f}pp"
    elif hi is not None:
        out["range"] = f"great run {fmt_pp(hi)}"
    bits = []
    if v["fc"] is not None:
        bits.append(f"FC {v['fc']:.0f}")
    if v["ss"] is not None:
        bits.append(f"SS {v['ss']:.0f}")
    out["fcss"] = " · ".join(bits) or None
    if v["p_pass"] is not None:
        out["pass"] = f"{fmt_pass(v['p_pass'])} pass chance"
    top = g(pp_data, "top_plays")
    floor = num(g(g(pp_data, "pp_threshold"), "top100"))
    out["gain"], out["gain_pass"] = gain_words(v, top, g(state, "beatmap_id"), g(state, "md5"), floor, ranked)
    alts = mod_alternatives(pred, recs, g(state, "md5")) + list(alts_extra or [])
    out["tip"] = mod_tip(mods, v, alts) if ranked is not False else None
    out["confidence"] = v["confidence"]
    nsim = v.get("n_similar") or pos_int(g(pred, "n_similar"))
    out["basis"] = (f"Based on {nsim} of your passes on similar maps" if nsim else
                    shorten(v["basis"], 90) if v["basis"] else "")
    sub = []
    if v["acc"] is not None:
        sub.append(f"you'd likely get ~{fmt_acc(v['acc'])}" + (f", {v['misses']:.0f} misses"
                                                                if v["misses"] is not None else ""))
    out["sub"] = "  ·  ".join(sub)
    return out


def live_lines(state, top100_floor=None, pp_data=None, recs=None, alts_extra=None):
    """Song-select bubble lines: title (stars + mods), name, big (expected pp), range, gain, pass, tip,
    confidence; 'main' keeps the one-line summary (older callers / logs). None if nothing to show."""
    ppd = pp_data if isinstance(pp_data, dict) else {}
    if top100_floor is not None and not g(ppd, "pp_threshold"):
        ppd = dict(ppd, pp_threshold={"top100": top100_floor})
    lv = live_view(state, ppd, recs, alts_extra)
    if lv is None:
        return None
    if lv["big"] and not (lv["status"] or "").startswith("not ranked"):
        main = " · ".join(x for x in (lv["big"], lv["range"]) if x)
    else:
        main = lv["status"] or "looking at this map..."
    return {"title": lv["title"], "name": lv["name"], "main": main,
            "big": lv["big"] if not (lv["status"] or "").startswith("not ranked") else None,
            "status": lv["status"], "range": " · ".join(x for x in (lv["range"], lv["fcss"]) if x) or None,
            "gain": lv["gain"], "pass": lv["pass"], "tip": lv["tip"], "sub": lv.get("sub", ""),
            "confidence": lv["confidence"], "basis": lv["basis"]}


def results_view(state, pred=None, pp_data=None):
    """Results bubble: 'You got 83pp' / 'I predicted 74pp - inside my range (55-96)' / '+4.2pp to your total'.
    pred = the prediction made before the play (the state's own one is used when it has none)."""
    res = g(state, "results")
    if not isinstance(res, dict):
        return None
    pp = num(g(res, "pp"))
    if pp is None:
        return None
    ranked = g(res, "ranked") if g(res, "ranked") is not None else g(state, "ranked")
    p = g(res, "predicted") if isinstance(g(res, "predicted"), dict) else None
    v = pred_view(p) or pred_view(pred) or pred_view(g(state, "prediction"))
    line = None
    within = None
    if v and v["expected"] is not None:
        lo, hi = v["low"], v["high"]
        line = f"I predicted {fmt_pp(v['expected'])}"
        if lo is not None and hi is not None:
            within = lo - 0.5 <= pp <= hi + 0.5
            if within:
                line += f" - inside my range ({lo:.0f}-{hi:.0f})"
            elif pp > hi:
                line += f" - better than I thought! (range {lo:.0f}-{hi:.0f})"
            else:
                line += f" - under my range ({lo:.0f}-{hi:.0f}), next one!"
        else:
            within = abs(pp - v["expected"]) <= max(5.0, 0.12 * v["expected"])
            line += " - spot on" if within else ""
    gain = num(g(res, "rank_gain_pp"))
    if gain is None and ranked is not False:
        gain = weighted_gain(pp, g(pp_data, "top_plays"), g(state, "beatmap_id"), g(state, "md5"),
                             total_pp=g(g(pp_data, "user"), "pp"))
    total = num(g(g(pp_data, "user"), "pp"))
    if ranked is False:
        gtxt = "Not a ranked map, so it doesn't change your total."
    elif gain is not None and gain >= 0.05:
        gtxt = f"{fmt_gain(gain)} to your total" + (f" (about {total + gain:,.0f}pp once it submits)"
                                                    if total is not None else "")
    elif gain is not None:
        gtxt = "Doesn't change your total this time - you have better plays."
    else:
        gtxt = None
    good = (within is True or (v and v["high"] is not None and pp > v["high"]) or
            (gain is not None and gain >= 0.05 and ranked is not False))
    title = f"You got {fmt_pp(pp)}!" if pp >= 1 and good else f"You got {fmt_pp(pp)}"
    acc, mis = num(g(res, "acc")), num(g(res, "misses"))
    stats = "  ·  ".join(x for x in ((fmt_acc(acc) if acc is not None else None),
                                     (f"{int(mis)} misses" if mis is not None else None)) if x)
    body = "\n".join(x for x in (line, gtxt, stats) if x)
    return {"title": title, "line": line, "gain_txt": gtxt, "body": body, "pp": pp, "gain": gain,
            "within": within, "predicted": v["expected"] if v else None}


def fail_note(rec, pred=None):
    """After a failed / quit try that got at least 25% in: ('That try:', 'Failed at 62%. I gave it a 40% pass
    chance - noted for next time.') or None. pred = the song-select prediction for that map."""
    if not isinstance(rec, dict) or txt(g(rec, "outcome")) not in ("failed", "quit"):
        return None
    prog = num(g(rec, "progress"))
    if prog is None or prog < 0.25:
        return None
    v = pred_view(pred) if isinstance(pred, dict) else None
    p = v["p_pass"] if v else None
    what = "Failed" if txt(g(rec, "outcome")) == "failed" else "Stopped"
    body = f"{what} at {prog:.0%}."
    if p is not None:
        body += f" I gave it a {fmt_pass(p)} pass chance - noted for next time."
    else:
        body += " Noted - I learn from fails too."
    return "That try:", body


# ---- prediction log + model report

def read_pred_log(path=None, n=80, max_bytes=400_000):
    """Last n rows of pp_predictions.jsonl, normalised: {ts, md5, mods, rate, name, pred (pred_view),
    pp, acc, misses, passed}. Never raises."""
    path = Path(path) if path else PP_PRED_LOG
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if len(raw) >= max_bytes and lines:
        lines = lines[1:]   # first line is probably cut
    rows = []
    for ln in lines[-n * 3:]:
        try:
            d = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or txt(g(d, "kind")) == "pre":
            continue   # pp_model also logs the snapshot taken before a try ("pre"); only scored rows count here
        act = g(d, "actual") if isinstance(g(d, "actual"), dict) else {}
        rows.append({"ts": num(g(d, "attempt_ts")) or num(g(d, "ts")), "md5": txt(g(d, "md5")), "mods": txt(g(d, "mods"), "NM"),
                     "rate": num(g(d, "rate")) or 1.0,
                     "name": txt(g(d, "map")) or txt(g(d, "title")) or txt(g(act, "map")),
                     "pred": pred_view(g(d, "predicted")), "pp": num(g(act, "pp")), "acc": num(g(act, "acc")),
                     "misses": num(g(act, "misses")), "passed": g(act, "passed")})
    return rows[-n:]


def scored_rows(rows):
    """Rows with both a predicted and an actual pp (passed plays)."""
    return [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)
            and isinstance(r.get("pred"), dict) and num(r["pred"].get("expected")) is not None
            and num(r.get("pp")) is not None and r.get("passed") is not False
            and all(k in r["pred"] for k in ("low", "high"))]


def model_summary(report=None, rows=None, n=20):
    """{'line': 'My last 20 guesses were off by ±6pp on average', 'detail': ..., 'mae', 'n', 'coverage'}."""
    rep = report if isinstance(report, dict) else {}
    rec = g(rep, "recent") if isinstance(g(rep, "recent"), dict) else {}
    bt = g(rep, "backtest") if isinstance(g(rep, "backtest"), dict) else {}
    mae, cnt, cov = num(g(rec, "pp_mae")), pos_int(g(rec, "n")), None
    from_backtest = txt(g(rec, "source")).startswith("backtest")
    sc = scored_rows(rows)[-n:]
    if sc and (mae is None or from_backtest or len(sc) >= (cnt or 0)):
        errs = [abs(r["pp"] - r["pred"]["expected"]) for r in sc]
        mae, cnt = sum(errs) / len(errs), len(sc)
        from_backtest = False
        inr = [r for r in sc if r["pred"]["low"] is not None and r["pred"]["high"] is not None]
        if inr:
            cov = sum(1 for r in inr if r["pred"]["low"] - 0.5 <= r["pp"] <= r["pred"]["high"] + 0.5) / len(inr)
    if mae is not None and cnt and from_backtest:
        line = f"Tested on your recent plays: off by ±{mae:.0f}pp on average"
    elif mae is not None and cnt:
        line = (f"My last {cnt} guesses were off by ±{mae:.0f}pp on average" if cnt > 1 else
                f"My last guess was off by {mae:.0f}pp")
    elif num(g(bt, "pp_mae")) is not None:
        line = f"Tested on your past plays: off by ±{num(g(bt, 'pp_mae')):.0f}pp on average"
    else:
        line = "I'm still learning your plays - my guesses get better after every map"
    det = []
    if num(g(bt, "pp_mae")) is not None:
        nb = pos_int(g(bt, "n"))
        det.append(f"Tested on {nb:,} of your past plays: typically off by ±{num(g(bt, 'pp_mae')):.0f}pp"
                   if nb else f"Backtest: typically off by ±{num(g(bt, 'pp_mae')):.0f}pp")
    bcov = num(g(bt, "range_coverage"))
    if cov is None and bcov is not None:
        cov = bcov
    if cov is not None:
        det.append(f"{cov * 100:.0f}% of results landed inside my range")
    if num(g(bt, "acc_mae")) is not None:
        det.append(f"accuracy guesses off by ±{num(g(bt, 'acc_mae')) * 100:.1f}%")
    if num(g(bt, "miss_mae")) is not None:
        det.append(f"misses off by ±{num(g(bt, 'miss_mae')):.0f}")
    det.append("pass chances assume you play the map to the end")
    meta = []
    if pos_int(g(rep, "trained_on")):
        meta.append(f"learned from {pos_int(g(rep, 'trained_on')):,} plays")
    if ago(g(rep, "updated")):
        meta.append(f"updated {ago(g(rep, 'updated'))}")
    return {"line": line, "detail": " · ".join(det), "meta": " · ".join(meta), "mae": mae, "n": cnt or 0,
            "coverage": cov}


# ---- rank-up picks (pp_recs.json)

def pick_view(p):
    """One pp_recs pick -> display fields + its action ('copy', search) for owned maps or ('web', url)."""
    if not isinstance(p, dict):
        return None
    v = pred_view(p) or {}
    name = map_name(p)
    if name == "Unknown map":
        return None
    mods = mods_label(g(p, "mods"), g(p, "rate"))
    owned = g(p, "owned") is True
    bid = pos_int(g(p, "beatmap_id"))
    search = search_text(p)
    url = txt(g(p, "url"))
    if not url.startswith("https://osu.ppy.sh/"):
        url = f"https://osu.ppy.sh/b/{bid}" if bid else ""
    if owned and search:
        action = ("copy", search)
    elif url:
        action = ("web", url)
    elif search:
        action = ("copy", search)
    else:
        action = None
    exp = v.get("expected")
    bits = [mods]
    if exp is not None:
        bits.append(f"~{fmt_pp(exp)}")
    gain = v.get("gain")
    gpass = gain_if_pass(v)
    if gpass is not None:
        bits.append(f"{fmt_gain(gpass)} if you pass")
    if v.get("p_pass") is not None:
        bits.append(f"{fmt_pass(v['p_pass'])} pass")
    if g(p, "stretch") is True:
        bits.append("stretch")
    stars = num(g(p, "stars"))
    meta = []
    if stars is not None:
        meta.append(f"{stars:.2f}★")
    if num(g(p, "length_s")):
        meta.append(fmt_mmss(sec=g(p, "length_s")))
    if num(g(p, "bpm")):
        meta.append(f"{num(g(p, 'bpm')):.0f} BPM")
    rng = (f"likely {v['low']:.0f}-{v['high']:.0f}pp" if v.get("low") is not None and v.get("high") is not None
           else None)
    return {"name": name, "short": shorten(name, 52), "mods": mods, "line": "  ·  ".join(bits), "meta": " · ".join(meta),
            "range": rng, "gain": gain, "gain_if_pass": gpass, "gain_high": v.get("gain_high"),
            "p_pass": v.get("p_pass"), "stars": stars, "stretch": g(p, "stretch") is True,
            "expected": exp, "owned": owned, "action": action, "search": search,
            "confidence": v.get("confidence"), "why": plain(txt(g(p, "why")) or txt(g(p, "reason"))),
            "raw_mods": txt(g(p, "mods"), "NM"), "rate": num(g(p, "rate")) or 1.0, "smash": g(p, "smash") is True,
            "md5": txt(g(p, "md5")),
            "alts": [a for a in (pick_alt(x) for x in dicts(g(p, "alt_mods"))) if a]}


def pick_alt(a):
    """'HR ~71pp (55% pass)' for one alt_mods entry of a pick."""
    v = pred_view(a)
    if not v or v["expected"] is None:
        return None
    t = f"{mods_label(g(a, 'mods'))} ~{fmt_pp(v['expected'])}"
    if v["p_pass"] is not None:
        t += f" ({fmt_pass(v['p_pass'])} pass)"
    return t


def rec_picks(recs, pp_data=None, which="picks"):
    """Picks from pp_recs.json ('picks', or 'smash' = smash_picks: likely passes worth a lot); falls back to
    pp_data.climb (older, coarser estimates). -> (picks, source)."""
    if which == "smash":
        sm = [v for v in (pick_view(p) for p in dicts(g(recs, "smash_picks"))) if v]
        if sm:
            return sm, "smash"
        sm = [v for v in (pick_view(p) for p in dicts(g(recs, "picks"))) if v and v["smash"]]
        return sm, ("smash" if sm else None)
    picks = [v for v in (pick_view(p) for p in dicts(g(recs, "picks"))) if v]
    if picks:
        return picks, "recs"
    out = []
    for c in dicts(g(pp_data, "climb")):
        exp, gain = num(g(c, "pp_predicted")), num(g(c, "est_gain_pp"))
        if exp is None and gain is None:
            continue
        v = pick_view(dict(c, pp_expected=exp, gain_expected=gain, why=g(c, "reason")))
        if v:
            out.append(v)
    out.sort(key=lambda v: -(v["gain"] or 0))
    return out, ("climb" if out else None)


PICK_SORTS = {"gain": "Biggest gain", "smash": "Smash picks", "pass": "Safest first", "stars": "Easiest first"}


def sort_picks(picks, key="gain"):
    if key == "pass":
        return sorted(picks, key=lambda v: (-(v["p_pass"] if v["p_pass"] is not None else -1),
                                            -(v["gain"] or 0)))
    if key == "smash":
        return sorted(picks, key=lambda v: (not v.get("smash"), -(v["gain"] if v["gain"] is not None else -1)))
    if key == "stars":
        return sorted(picks, key=lambda v: (v["stars"] if v["stars"] is not None else 99, -(v["gain"] or 0)))
    return sorted(picks, key=lambda v: (-(v["gain"] if v["gain"] is not None else -1), -(v["expected"] or 0)))


def pp_card_view(pp_data=None, recs=None, report=None, rows=None, live=None, coach=None, alts_extra=None,
                 n_picks=5, tosu_note=None):
    """Everything the PP card shows, as plain strings."""
    hdr = pp_header(pp_data, recs, coach)
    lv = live_view(live, pp_data, recs, alts_extra) if isinstance(live, dict) else None
    picks, src = rec_picks(recs, pp_data)
    picks = sort_picks(picks, "gain")
    # maps he could smash (likely pass AND big pp): at least 2 of them in the top picks when there are any
    smash, _ = rec_picks(recs, pp_data, "smash")
    if smash and n_picks >= 3:
        def key(x):
            return x["md5"] or x["name"]
        top = picks[:n_picks]
        keys = {key(x) for x in top}
        extra = [x for x in sort_picks(smash, "gain") if key(x) not in keys]
        need = max(0, min(2, len(smash)) - sum(1 for x in top if x["smash"]))
        for x in extra[:need]:
            idx = max((i for i, y in enumerate(top) if not y["smash"]), default=None)
            if idx is None:
                if len(top) >= n_picks:
                    break
                top.append(dict(x, smash=True))
            else:
                top[idx] = dict(x, smash=True)
        keys = {key(x) for x in top}
        picks = sort_picks(top, "gain") + [x for x in picks if key(x) not in keys]
    ms = model_summary(g(recs, "model") if report is None else report, rows)
    if lv is None:
        hint = tosu_note or "Hover a map in osu! song select and I'll tell you what it's worth for you."
    else:
        hint = None
    note = None
    if src == "climb":
        note = "Rough picks for now - better ones come after my next look at your plays."
    elif not picks:
        note = "I'm still finding maps that would rank you up - they show up here after your next session."
    return {"header": hdr, "map": lv, "map_hint": hint, "model": ms, "picks": picks[:n_picks],
            "n_picks": len(picks), "picks_note": note, "by_mods_tip": plain(txt(g(recs, "by_mods_tip"))) or None,
            "recs_age": ago(g(recs, "generated_at"))}


# ---- background jobs: pp_model.update + pp_recommend refresh (subprocesses, one at a time)

_BG_PRIORITY = ("try:\n"
                "    import ctypes\n"
                "    _k = ctypes.windll.kernel32\n"
                "    _k.SetPriorityClass(_k.GetCurrentProcess(), 0x00100000)  # background mode: low CPU/disk/memory\n"
                "except Exception:\n"
                "    pass\n")
PP_UPDATE_CODE = (_BG_PRIORITY + "import sys, pp_model as m\n"
                  "f = getattr(m, 'update', None)\n"
                  "if not callable(f): sys.exit('pp_model has no update()')\n"
                  "f(force=False)\n")
PP_RECOMMEND_CODE = (_BG_PRIORITY + "import sys, pp_recommend as m\n"
                     "for n in ('refresh', 'update', 'run', 'generate', 'main'):\n"
                     "    f = getattr(m, n, None)\n"
                     "    if callable(f):\n"
                     "        try:\n"
                     "            r = f()\n"
                     "        except TypeError:\n"
                     "            r = f([])\n"
                     "        sys.exit(r if isinstance(r, int) else 0)\n"
                     "sys.exit('pp_recommend has no refresh()')\n")


def _file_age_ts(path):
    """-> callable giving the file's modification time (epoch seconds), None when missing."""
    def f():
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None
    return f


class PPJobs:
    """Schedules pp_model.update (after every try, debounced) and pp_recommend refresh (after a session, and at
    least daily) as background subprocesses - never two at once, never started while a map is being played.
    spawn(kind, code) -> a Popen-like object (poll(), kill()) or None; injected so tests never start anything."""
    UPDATE_DELAY_S = 20          # after a try: wait a little (the replay and the live line both arrive)
    STARTUP_DELAY_S = 60
    SESSION_GAP_S = 15 * 60      # no try for this long after some tries = the session is over
    REC_MIN_GAP_S = 2 * 3600
    REC_STALE_S = 20 * 3600
    TIMEOUT_S = 20 * 60
    FAIL_BACKOFF_S = 30 * 60
    ATTEMPT_DEDUPE_S = 30        # one try reaches us up to 4 times (watcher, replay file, live line, results)
    FLAG_TOUCH_S = 5

    def __init__(self, spawn, model_ok=None, rec_ok=None, recs_mtime=None, now=None, playing_flag=None):
        self.spawn = spawn
        self.model_ok = model_ok or (lambda: PP_MODEL_SCRIPT.exists())
        self.rec_ok = rec_ok or (lambda: PP_RECOMMEND_SCRIPT.exists())
        self.recs_mtime = recs_mtime or _file_age_ts(PP_RECS_FILE)
        t = time.time() if now is None else now
        self.boot = t
        self.playing_flag = playing_flag   # Path kept fresh while a map is played (pp_recommend then pauses)
        self._flag_at = None
        self.proc = None
        self.kind = None
        self.started = 0.0
        self.update_at = t + self.STARTUP_DELAY_S
        self.rec_wanted = False
        self.last_attempt = None
        self.tries_since_rec = 0
        self.last_rec_start = -1e18
        self.failed_until = {"update": -1e18, "recommend": -1e18}
        self.history = []        # (kind, start, end, rc) - newest last
        self.on_done = None      # callback(kind, rc)

    def running(self):
        return self.proc is not None

    def attempt(self, now=None):
        now = time.time() if now is None else now
        if self.last_attempt is None or now - self.last_attempt > self.ATTEMPT_DEDUPE_S:
            self.tries_since_rec += 1
        self.last_attempt = now
        self.update_at = now + self.UPDATE_DELAY_S

    def request(self, kind, now=None):
        now = time.time() if now is None else now
        if kind == "update":
            self.update_at = now
        else:
            self.rec_wanted = True
            self.failed_until["recommend"] = -1e18

    def rec_due(self, now):
        if self.rec_wanted:
            return True
        if now - self.last_rec_start < self.REC_MIN_GAP_S:
            return False
        if self.tries_since_rec and self.last_attempt is not None and now - self.last_attempt >= self.SESSION_GAP_S:
            return True
        m = self.recs_mtime()
        return m is None or now - m >= self.REC_STALE_S

    def _start(self, kind, now):
        p = None
        try:
            p = self.spawn(kind, PP_UPDATE_CODE if kind == "update" else PP_RECOMMEND_CODE)
        except Exception as e:  # noqa: BLE001
            log.error("pp job %s could not start: %s", kind, e)
        if p is None:
            self.failed_until[kind] = now + self.FAIL_BACKOFF_S
            if kind == "update":
                self.update_at = None
            else:
                self.rec_wanted = False
            return None
        self.proc, self.kind, self.started = p, kind, now
        if kind == "update":
            self.update_at = None
        else:
            self.rec_wanted, self.tries_since_rec, self.last_rec_start = False, 0, now
        log.info("pp job started: %s", kind)
        return kind

    def _flag(self, busy, now):
        """Keep the playing flag fresh while busy and a job runs; remove it otherwise."""
        f = self.playing_flag
        if f is None:
            return
        try:
            if busy and self.proc is not None:
                if self._flag_at is None or now - self._flag_at >= self.FLAG_TOUCH_S:
                    Path(f).write_text(str(int(now)), encoding="utf-8")
                    self._flag_at = now
            elif self._flag_at is not None:
                self._flag_at = None
                Path(f).unlink()
        except OSError:
            pass

    def tick(self, now=None, busy=False, busy_api=False):
        """Call every second. busy=True while a map is being played (a running recommend then pauses between
        steps via the playing flag); busy_api=True while the pet's own osu! API job runs. -> the kind started."""
        now = time.time() if now is None else now
        self._flag(busy, now)
        if self.proc is not None:
            rc = None
            try:
                rc = self.proc.poll()
            except Exception:  # noqa: BLE001
                rc = -1
            if rc is None:
                if now - self.started > self.TIMEOUT_S:
                    log.warning("pp job %s ran for %.0f min - stopping it", self.kind, (now - self.started) / 60)
                    try:
                        self.proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    rc = -9
                else:
                    return None
            kind = self.kind
            self.history.append((kind, self.started, now, rc))
            del self.history[:-30]
            self.proc, self.kind = None, None
            if rc != 0:
                self.failed_until[kind] = now + self.FAIL_BACKOFF_S
                log.error("pp job %s exited with %s", kind, rc)
            else:
                log.info("pp job %s done in %.0f s", kind, now - self.history[-1][1])
            if self.on_done:
                try:
                    self.on_done(kind, rc)
                except Exception:  # noqa: BLE001
                    log.error("pp job on_done failed", exc_info=True)
            return None
        if busy:
            return None
        if self.update_at is not None and now >= self.update_at and now >= self.failed_until["update"] \
                and self.model_ok():
            return self._start("update", now)
        if now >= self.failed_until["recommend"] and self.rec_ok() and self.rec_due(now):
            if now - self.boot < self.STARTUP_DELAY_S + 60 and not self.rec_wanted:
                return None   # just started: the model refresh goes first (and osu! is often being launched)
            if self.model_ok() and self.update_at is not None and now >= self.failed_until["update"]:
                return None   # a model update is pending: recommend with the refreshed model after it
            if busy_api:
                return None   # the pet's own osu! API job is running: don't share the API rate with it
            return self._start("recommend", now)
        return None


# ---- samples matching CONTRACT_PP2.md (selftest / headless tests)

def sample_model_report(now=None):
    now = int(now or time.time())
    return {"trained_on": 812, "updated": now - 300, "learner": "recency-weighted k-NN + logistic pass model",
            "backtest": {"pp_mae": 7.4, "acc_mae": 0.011, "miss_mae": 2.6, "range_coverage": 0.63, "n": 412},
            "recent": {"n": 20, "pp_mae": 6.1}}


def sample_pp_recs(now=None):
    now = int(now or time.time())
    def pk(i, **kw):
        d = {"md5": f"m{i}", "beatmap_id": 4000 + i, "set_id": 900 + i, "artist": f"Artist {i}",
             "title": f"Song {i}", "version": "Insane", "mapper": "someone", "owned": i % 3 != 2,
             "status": "ranked", "mods": ["NM", "HD", "HR", "HDHR", "DT"][i % 5], "rate": 1.0,
             "stars": round(4.2 + i * 0.12, 2), "length_s": 80 + i * 7, "bpm": 160 + i * 4, "ar": 9.0, "od": 8.0,
             "cs": 4.0, "pp_expected": 60.0 - i * 1.5, "pp_low": 45.0 - i, "pp_high": 78.0 - i, "pp_fc": 90.0 - i,
             "p_pass": round(0.9 - i * 0.04, 2), "gain_expected": round(9.4 - i * 0.6, 1),
             "gain_high": round(18.1 - i, 1), "confidence": ["high", "medium", "low"][i % 3],
             "why": "Jump-heavy - your strongest pattern; you average 95% on similar maps",
             "search": f"Artist {i} Song {i} Insane", "url": f"https://osu.ppy.sh/b/{4000 + i}",
             "smash": i in (0, 1, 4), "alt_mods": [{"mods": "HR", "pp_expected": 70.0 - i, "p_pass": 0.5,
                                                    "gain_expected": 5.0, "smash": False}]}
        d.update(kw)
        return d
    picks = [pk(i) for i in range(12)]
    picks.append(pk(20, md5="f00d", beatmap_id=123, artist="TrySail", title="whiz", mods="HD", stars=4.75,
                    pp_expected=40.0, pp_low=30.0, pp_high=50.0, gain_expected=2.1, p_pass=0.75,
                    search="TrySail whiz Insane", smash=False, alt_mods=[]))
    smash = [dict(picks[0]), dict(picks[4]), pk(30, md5="s30", title="Safe Song", mods="NM", p_pass=0.93,
                                                  gain_expected=6.0, smash=True)]
    return {"generated_at": now - 3600, "total_pp": 2001.5, "rank": 500000, "model": sample_model_report(now),
            "picks": picks, "smash_picks": smash,
            "by_mods_tip": "HD adds ~+4pp on your typical map; HR costs you ~-12% pass chance"}


def sample_pred_rows(n=20, now=None):
    now = int(now or time.time())
    rows = []
    for i in range(n):
        exp = 40 + (i * 7) % 35
        act = exp + ((i * 13) % 21 - 10)
        rows.append({"ts": now - (n - i) * 600, "md5": f"r{i}", "mods": "NM", "rate": 1.0, "name": f"Map {i}",
                     "pred": pred_view({"pp_expected": exp, "pp_low": exp - 9, "pp_high": exp + 9}),
                     "pp": float(act), "acc": 0.95, "misses": 3, "passed": i % 6 != 5})
    return rows


def sample_pred_log_lines(n=20, now=None):
    """JSONL text in the pp_predictions.jsonl format (for read_pred_log tests)."""
    out = []
    for r in sample_pred_rows(n, now):
        p = r["pred"]
        out.append(json.dumps({"kind": "pre", "ts": r["ts"] - 60, "md5": r["md5"],
                               "predicted": {"pp_expected": 1.0}}))
        out.append(json.dumps({"kind": "scored", "ts": r["ts"] + 5, "attempt_ts": r["ts"], "md5": r["md5"],
                               "mods": r["mods"], "rate": r["rate"],
                               "predicted": {"pp_expected": p["expected"], "pp_low": p["low"], "pp_high": p["high"],
                                             "p_pass": 0.8},
                               "actual": {"pp": r["pp"] if r["passed"] else None, "acc": r["acc"],
                                          "misses": r["misses"], "passed": r["passed"]}}))
    return "\n".join(out) + "\n"


def sample_live_states_pp():
    """Fake LiveWatcher states with pp_model-style predictions (song select -> mods -> play -> results)."""
    pred = {"stars": 4.62, "pp_expected": 34.0, "pp_low": 26.0, "pp_high": 43.0, "pp_fc": 52.0, "pp_ss": 61.0,
            "p_pass": 0.82, "gain_expected": 0.0, "gain_high": 1.0, "confidence": "high",
            "acc": {"p20": 0.92, "p50": 0.945, "p80": 0.962}, "misses": {"p20": 1, "p50": 4, "p80": 11},
            "basis": "23 of your plays like this one", "model_error_pp": 6.3,
            "by_mods": {"HD": {"pp_expected": 39.0, "p_pass": 0.8}, "HR": {"pp_expected": 44.0, "p_pass": 0.55}}}
    base = {"tosu_ok": True, "md5": "f00d", "beatmap_id": 123, "artist": "TrySail", "title": "whiz",
            "version": "Insane", "mods": "NM", "rate": 1.0, "stars": 4.64, "ranked": True, "prediction": pred,
            "results": None}
    hd = dict(base, mods="HD", prediction=dict(pred, pp_expected=55.0, pp_low=44.0, pp_high=66.0, pp_fc=71.0, pp_ss=83.0,
                                               gain_expected=6.2, p_pass=0.78, by_mods=None))
    return [dict(base, screen="song_select"), dict(hd, screen="song_select"), dict(hd, screen="playing"),
            dict(hd, screen="results", results={"pp": 61.0, "acc": 0.951, "misses": 2, "rank_gain_pp": 7.4,
                                                "ranked": True})]


if __name__ == "__main__":  # used by "Play osu with Coach.bat"
    import sys
    if "--tosu-path" in sys.argv:
        print(launch_tosu_path())
