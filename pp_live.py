"""pp_live.py - tosu client, star-rating cache, personal skill model and PP prediction.

Implements the `pp_live.py` API of CONTRACT_PP.md. Python stdlib only.

* Talks ONLY to tosu over HTTP (default http://127.0.0.1:24050, override with env OSU_COACH_TOSU_URL).
  It never opens, writes or locks anything in the osu! data / game folders and never touches the osu!
  process. Beatmap paths are only handed to tosu (which reads the file itself).
* Reads (never writes) the engine caches next to this script: .coach_replays.json (per-replay records),
  .osu_coach_cache.json (file path -> .osu md5), coach_data.json ("player") and pp_data.json (top plays).
* Writes only its own files: .coach_sr_cache.json, .coach_skill.json, live_attempts.jsonl and a small
  pp_live_log.txt.
  Env OSU_COACH_PP_DIR moves all of these reads/writes to another folder (the tests use a temp folder).

CLI:  python pp_live.py                 status + skill model summary
      python pp_live.py --refresh       refit the skill model (fills missing star ratings if tosu runs)
      python pp_live.py --predict MD5 [MODS] [RATE]
      python pp_live.py --watch [--record]   print LiveWatcher states (+ finished attempts) until Ctrl+C
      python pp_live.py --attempts      print the last 2 days of live_attempts.jsonl

PP-first (CONTRACT_PP2.md): LiveWatcher's hover prediction comes from pp_model.predict_pp (self-learning
outcome model; falls back to the old skill_model predict() when pp_model is unavailable); at every play start
the prediction for that attempt is logged to pp_predictions.jsonl (pp_model.log_pre); the results state
carries "predicted" / "prediction_error_pp" / "within_range".
NOTE (verified 2026-11-10): the user's tosu IGNORES /api/calculate/pp?path= and always calculates the map
selected in game. calc_pp() therefore refuses a path that is not the selected map (tosu_ignores_path()), and
.coach_sr_cache.json v1 (rated on the wrong maps) is discarded (v2).

Live attempt telemetry (CONTRACT_ADAPT.md): LiveWatcher appends every finished attempt - passes, fails,
quits and retries - to live_attempts.jsonl (one JSON object per line, rotated at 5 MB) and calls
on_attempt(record). See AttemptTracker for the end-of-attempt rules verified against osu!lazer.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

TOSU = "http://127.0.0.1:24050"   # override with env OSU_COACH_TOSU_URL (tests use a mock server)

HALF_LIFE_DAYS = 21.0     # skill changes fast: last month's plays say much more than last quarter's
SR_BUDGET = 200           # max tosu calculations per skill-model refresh
SR_BUDGET_SECONDS = 60.0  # ... and max wall time spent on them
SR_RETRY_S = 600          # don't retry filling missing star ratings more often than this
NEAR = 0.35               # +- stars counted as "near this SR" for confidence
BIN = 0.25                # star bin width of the fit
ASSISTED_BITS = 128 | 2048 | 8192 | 4194304   # RX, AT, AP, CN (same as coach_engine)
ASSISTED_ACR = {"RX", "AT", "AP", "CN"}


def _dir() -> Path:
    d = os.environ.get("OSU_COACH_PP_DIR")
    return Path(d) if d else Path(__file__).resolve().parent


def _base_url() -> str:
    return (os.environ.get("OSU_COACH_TOSU_URL") or TOSU).rstrip("/")


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

_log_lock = threading.Lock()


def _log(msg: str):
    try:
        with _log_lock:
            p = _dir() / "pp_live_log.txt"
            if p.exists() and p.stat().st_size > 200_000:
                p.write_text("", encoding="utf-8")
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)[:400] + "\n")
    except Exception:
        pass


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _atomic_write_json(path: Path, obj) -> bool:
    """Write via a per-process tmp file + os.replace; retries briefly when the target is locked (OneDrive sync,
    another coach process reading it). -> True when written."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        for i in range(6):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if i == 5:
                    raise
                time.sleep(0.1)
    except Exception as e:
        _log(f"write {path.name} failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def _num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _g(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _http_json(path: str, params: dict | None = None, timeout: float = 0.4):
    url = _base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def tosu_available(timeout=0.4) -> bool:
    """True when the RUNNING tosu instance (whatever folder it was started from) answers /json/v2 with
    game data. False when tosu is down OR up but osu! isn't (tosu then answers HTTP 500 "osu is not
    ready/running" - see tosu_status() to tell the two apart)."""
    try:
        d = _http_json("/json/v2", timeout=timeout)
        return isinstance(d, dict) and "error" not in d
    except Exception:
        return False


def tosu_status(timeout=0.6) -> dict:
    """{"tosu": tosu process answering HTTP, "osu": game data available, "url", "error"}.
    Pure HTTP against the real running instance - no assumption about where tosu.exe lives."""
    out = {"tosu": False, "osu": False, "url": _base_url(), "error": None}
    try:
        d = _http_json("/json/v2", timeout=timeout)
        out["tosu"] = True
        if isinstance(d, dict) and "error" not in d:
            out["osu"] = True
        else:
            out["error"] = str(d.get("error") if isinstance(d, dict) else d)[:200]
    except urllib.error.HTTPError as e:   # tosu is up and says why there's no data
        out["tosu"] = True
        try:
            out["error"] = str(json.loads(e.read().decode("utf-8", "replace")).get("error"))[:200]
        except Exception:
            out["error"] = urllib.parse.unquote(str(e.reason or e))[:200]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


# ----------------------------------------------------------------------------
# mods
# ----------------------------------------------------------------------------

LEGACY_BITS = [(1, "NF"), (2, "EZ"), (4, "TD"), (8, "HD"), (16, "HR"), (32, "SD"), (64, "DT"), (128, "RX"),
               (256, "HT"), (512, "NC"), (1024, "FL"), (2048, "AT"), (4096, "SO"), (8192, "AP"),
               (16384, "PF"), (4194304, "CN"), (536870912, "SV2")]
LEGACY_VALUE = {"NF": 1, "EZ": 2, "TD": 4, "HD": 8, "HR": 16, "DT": 64, "HT": 256, "FL": 1024, "SO": 4096}
# no effect on star rating or pp - dropped from keys
NO_EFFECT = {"CL", "SD", "PF", "MU", "NM", "TP", "AS", ""}
ALIAS = {"NC": "DT", "DC": "HT"}
DISPLAY_ORDER = ["EZ", "NF", "HT", "HD", "HR", "DT", "FL", "SO", "TD"]


def _split_acronyms(s: str) -> list[str]:
    s = s.upper().replace(" ", "")
    if any(c in s for c in ",+|"):
        return [p for p in s.replace("+", ",").replace("|", ",").split(",") if p]
    out, i = [], 0
    while i < len(s):
        if s.startswith("SV2", i):
            out.append("SV2"); i += 3
        else:
            out.append(s[i:i + 2]); i += 2
    return out


def parse_mods(mods, clock_rate: float | None = None):
    """Any mods form -> (sorted canonical acronyms, rate, other settings by acronym).

    Accepts None, legacy int bits, "HDDT" / "HD,DT" / "NM", ["HD", "DT"] or lazer
    [{"acronym": "DT", "settings": {"speed_change": 1.2}}]. NC->DT and DC->HT (same difficulty).
    An explicit clock_rate overrides the mod's speed; a rate != 1 with no rate mod adds DT/HT.
    """
    acs, rate, extra = [], None, {}
    try:
        if isinstance(mods, bool):
            mods = None
        if isinstance(mods, (int, float)) and not isinstance(mods, bool):
            m = int(mods)
            acs = [a for b, a in LEGACY_BITS if m & b]
        elif isinstance(mods, str):
            acs = _split_acronyms(mods)
        elif isinstance(mods, (list, tuple)):
            for x in mods:
                if isinstance(x, dict):
                    a = str(x.get("acronym", "")).upper()
                    st = x.get("settings") if isinstance(x.get("settings"), dict) else {}
                    if a in ("DT", "NC", "HT", "DC") and _num(st.get("speed_change")):
                        rate = _num(st.get("speed_change"))
                    elif st:
                        extra[a] = {k: v for k, v in st.items() if isinstance(v, (int, float, str, bool))}
                    acs.append(a)
                else:
                    acs.append(str(x).upper())
    except Exception:
        acs = []
    out = set()
    for a in acs:
        a = ALIAS.get(a, a)
        if a not in NO_EFFECT:
            out.add(a)
    if rate is None:
        rate = 1.5 if "DT" in out else 0.75 if "HT" in out else 1.0
    cr = _num(clock_rate)
    if cr and cr > 0:
        rate = cr
    if abs(rate - 1.0) < 1e-6:
        out -= {"DT", "HT"}
        rate = 1.0
    elif rate > 1:
        out.discard("HT"); out.add("DT")
    else:
        out.discard("DT"); out.add("HT")
    return sorted(out), round(rate, 3), extra


def mods_key(mods, clock_rate: float | None = None) -> str:
    """Canonical mods key: sorted acronyms joined ("DTHD"), "NM" for none. The rate is NOT part of
    this string - it is the separate third field of every cache key ("md5|DTHD|1.20")."""
    acs, _, _ = parse_mods(mods, clock_rate)
    return "".join(acs) or "NM"


def mods_display(acs: list[str]) -> str:
    order = {a: i for i, a in enumerate(DISPLAY_ORDER)}
    return "".join(sorted(acs, key=lambda a: (order.get(a, 99), a))) or "NM"


def _lazer_mods_json(acs, rate, extra=None):
    out = []
    for a in acs:
        m = {"acronym": a}
        if a in ("DT", "HT"):
            m["settings"] = {"speed_change": rate}
        elif extra and extra.get(a):
            m["settings"] = extra[a]
        out.append(m)
    return json.dumps(out, separators=(",", ":"))


# ----------------------------------------------------------------------------
# tosu PP calculation
# ----------------------------------------------------------------------------

_path_probe = {"t": -1e9, "ignored": None}
_file_md5 = {}
PATH_PROBE_TTL = 600.0


def tosu_ignores_path() -> bool | None:
    """True when the running tosu ignores /api/calculate/pp?path= (verified on the user's build 2026-11-10:
    every call - even for a file that doesn't exist - is calculated on the map SELECTED IN GAME).
    Probed with a nonexistent .osu path (a path-honouring tosu answers an error), cached 10 minutes.
    None = tosu not reachable (unknown)."""
    now = time.monotonic()
    if _path_probe["ignored"] is not None and now - _path_probe["t"] < PATH_PROBE_TTL:
        return _path_probe["ignored"]
    try:
        d = _http_json("/api/calculate/pp", {"mode": 0, "mods": 0,
                                             "path": str(_dir() / ".pp_live_probe_no_such_map.osu")}, timeout=2.0)
        ign = isinstance(d, dict) and not d.get("error") and _num(d.get("pp")) is not None
    except urllib.error.HTTPError:
        ign = False
    except Exception:
        return None
    if ign and not _path_probe["ignored"]:
        _log("tosu ignores calculate?path= : pp/star ratings are only calculated for the map selected in game")
    _path_probe.update(t=now, ignored=ign)
    return ign


def _md5_of_file(path):
    try:
        st = os.stat(path)
        k = (str(path), st.st_mtime, st.st_size)
        if k not in _file_md5:
            if len(_file_md5) > 2000:
                _file_md5.clear()
            import hashlib
            with open(path, "rb") as f:
                _file_md5[k] = hashlib.md5(f.read()).hexdigest()
        return _file_md5[k]
    except OSError:
        return None


def calc_pp(osu_path: str | None, mods: list[dict] | int | None = None, clock_rate: float | None = None,
            acc: float | None = None, misses: int | None = None, combo: int | None = None,
            lazer: bool = True) -> dict | None:
    """GET /api/calculate/pp. Returns {"pp","stars","aim","speed","max_combo","n_objects","ar","od","cs","bpm"}
    or None (tosu down / error). osu_path=None -> tosu uses the map currently selected in game.
    acc is 0-100 (a 0-1 fraction is accepted too)."""
    try:
        if osu_path and tosu_ignores_path():
            # this tosu would silently calculate the SELECTED map instead: only go on when that is this file
            want = _md5_of_file(osu_path)
            cur = _g(_http_json("/json/v2", timeout=0.4), "beatmap", "checksum")
            if not want or cur != want:
                return None
        acs, rate, extra = parse_mods(mods, clock_rate)
        params = {"mode": 0, "lazer": "true" if lazer else "false"}
        if osu_path:
            params["path"] = str(osu_path)
        # mods are sent as LEGACY BITS even for lazer: the user's tosu build (checked live 2026-11-09)
        # rejects every JSON / acronym form with HTTP 500 "invalid type: floating point `NaN`, expected
        # GameMods" and only accepts an integer. The lazer speed is carried by clockRate, which rosu-pp
        # treats as overriding the mod's own rate (live: mods=64 + clockRate=1.2 == mods=0 + clockRate=1.2).
        # If an integer is ever refused, the lazer JSON form is tried once as a fallback.
        params["mods"] = sum(LEGACY_VALUE.get(a, 0) for a in acs)
        if abs(rate - 1.0) > 1e-6:
            params["clockRate"] = rate
        a = _num(acc)
        if a is not None:
            if a <= 1.0:
                a *= 100.0
            params["acc"] = round(max(0.0, min(100.0, a)), 3)
        if misses is not None:
            params["nMisses"] = max(0, int(misses))
        if combo is not None:
            params["combo"] = max(0, int(combo))
        try:
            d = _http_json("/api/calculate/pp", params, timeout=3.0)
        except urllib.error.HTTPError as e:   # tosu answers calculation errors with HTTP 500 + {"error"}
            try:
                d = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                raise e
            if not isinstance(d, dict):
                raise
        if lazer and isinstance(d, dict) and "gamemods" in str(d.get("error") or "").lower().replace(" ", ""):
            _log(f"calc_pp: tosu refused integer mods ({d.get('error')}); retrying with lazer mods JSON")
            params["mods"] = _lazer_mods_json(acs, rate, extra)
            d = _http_json("/api/calculate/pp", params, timeout=3.0)
    except Exception as e:
        _log(f"calc_pp failed: {type(e).__name__}: {e}")
        return None
    return _parse_calc(d)


def _parse_calc(d):
    if not isinstance(d, dict) or d.get("error"):
        if isinstance(d, dict):
            _log(f"calc_pp error: {d.get('error')}")
        return None
    perf = d.get("performance") if isinstance(d.get("performance"), dict) else d
    diff = perf.get("difficulty") if isinstance(perf.get("difficulty"), dict) else (d.get("difficulty") or {})
    attr = d.get("attributes") if isinstance(d.get("attributes"), dict) else {}
    pp = _num(perf.get("pp"))
    stars = _num(diff.get("stars")) or _num(perf.get("stars"))
    if pp is None and stars is None:
        return None

    def first(*vals):
        for v in vals:
            if _num(v) is not None:
                return _num(v)
        return None
    nobj = None
    parts = [first(attr.get(k), diff.get(k)) for k in ("nCircles", "nSliders", "nSpinners")]
    if any(p is not None for p in parts):
        nobj = int(sum(p or 0 for p in parts))
    mc = first(diff.get("maxCombo"), attr.get("maxCombo"))
    bpm = attr.get("bpm")
    if isinstance(bpm, dict):
        bpm = bpm.get("common") or bpm.get("max")
    return {"pp": round(pp, 2) if pp is not None else None, "stars": round(stars, 3) if stars is not None else None,
            "aim": first(perf.get("ppAim")), "speed": first(perf.get("ppSpeed")),
            "max_combo": int(mc) if mc is not None else None, "n_objects": nobj,
            "ar": first(attr.get("ar"), diff.get("ar")), "od": first(attr.get("od"), diff.get("od")),
            "cs": first(attr.get("cs"), diff.get("cs")), "bpm": _num(bpm)}


# ----------------------------------------------------------------------------
# beatmap paths (read-only lookup through osu_coach's index cache)
# ----------------------------------------------------------------------------

_idx = {"mtime": None, "map": {}}
_idx_lock = threading.Lock()


def osu_path_for_md5(md5: str | None) -> str | None:
    if not md5:
        return None
    p = _dir() / ".osu_coach_cache.json"
    try:
        with _idx_lock:
            mt = p.stat().st_mtime
            if _idx["mtime"] != mt:
                c = _load_json(p, {})
                osu = c.get("osu") if isinstance(c, dict) else None
                _idx["map"] = {v: k for k, v in (osu or {}).items() if v}
                _idx["mtime"] = mt
            fp = _idx["map"].get(md5)
        return fp if fp and os.path.isfile(fp) else None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# star rating cache
# ----------------------------------------------------------------------------

_sr = {"loaded": False, "data": {}, "dirty": 0, "mtime": None, "checked": 0.0}
_sr_lock = threading.Lock()


def _sr_key(md5, mods_k, rate):
    return f"{md5}|{mods_k or 'NM'}|{float(rate or 1.0):.2f}"


SR_CACHE_VERSION = 2   # v1 (before 2026-11-10) was polluted: tosu ignored path= and rated the selected map


def _sr_file_data():
    d = _load_json(_dir() / ".coach_sr_cache.json", {})
    if not isinstance(d, dict) or d.get("v") != SR_CACHE_VERSION:
        return {}
    return d.get("sr", {}) if isinstance(d.get("sr"), dict) else {}


def _sr_mtime():
    try:
        return (_dir() / ".coach_sr_cache.json").stat().st_mtime
    except OSError:
        return None


def _sr_load():
    """Load once, then pick up entries another process (pet watcher / engine) wrote, checked every ~2 s.
    Caller holds _sr_lock."""
    now = time.monotonic()
    if _sr["loaded"] and now - _sr["checked"] < 2.0:
        return
    _sr["checked"] = now
    mt = _sr_mtime()
    if not _sr["loaded"] or mt != _sr["mtime"]:
        disk = _sr_file_data()
        disk.update(_sr["data"])          # ours win (same key = same calculation)
        _sr["data"], _sr["mtime"], _sr["loaded"] = disk, mt, True


def _sr_flush():
    with _sr_lock:
        if _sr["dirty"]:
            merged = _sr_file_data()       # merge with what other processes added since we loaded
            merged.update(_sr["data"])
            if _atomic_write_json(_dir() / ".coach_sr_cache.json", {"v": SR_CACHE_VERSION, "sr": merged}):
                _sr["data"], _sr["dirty"], _sr["mtime"] = merged, 0, _sr_mtime()


def _sr_get(md5, mods_k, rate):
    with _sr_lock:
        _sr_load()
        v = _sr["data"].get(_sr_key(md5, mods_k, rate))
    return _num(v)


def star_rating(md5: str, osu_path: str, mods_key: str, clock_rate: float, _flush=True) -> float | None:
    """Star rating for md5 + mods + rate; cached in .coach_sr_cache.json keyed "md5|mods_key|rate".
    osu_path may be None (looked up from the engine index)."""
    try:
        mk = globals()["mods_key"](mods_key, clock_rate)  # canonicalise whatever was passed
        acs, rate, _ = parse_mods(mods_key, clock_rate)
        v = _sr_get(md5, mk, rate)
        if v is not None:
            return v
        path = osu_path or osu_path_for_md5(md5)
        if not path:
            return None
        r = calc_pp(path, acs, rate, acc=100, misses=0)
        if not r or r.get("stars") is None:
            return None
        with _sr_lock:
            _sr["data"][_sr_key(md5, mk, rate)] = r["stars"]
            _sr["dirty"] += 1
        if _flush:
            _sr_flush()
        return r["stars"]
    except Exception as e:
        _log(f"star_rating failed: {e}")
        return None


# ----------------------------------------------------------------------------
# skill model
# ----------------------------------------------------------------------------

def _player_records():
    """This player's osu!standard replays from the engine cache (same player rule as coach_engine:
    coach_data.json "player", widened to every name sharing its user id)."""
    rc = _g(_load_json(_dir() / ".coach_replays.json", {}), "replays") or {}
    hdrs = [(p, e) for p, e in rc.items() if isinstance(e, dict) and isinstance(e.get("hdr"), dict)
            and e["hdr"].get("mode") == 0]
    if not hdrs:
        return []
    me = str(_g(_load_json(_dir() / "coach_data.json", {}), "player") or "").lower()
    names = {e["hdr"].get("player", "").lower() for _, e in hdrs}
    uid = None
    if me not in names:
        # no coach_data.json yet: the engine's rule (coach_identity: config user / logged-in account /
        # newest replay, never an "exclude_users" account)
        try:
            import coach_identity
            res = coach_identity.resolve([e["hdr"] for _, e in hdrs], coach_identity.game_username())
            mine = {n.lower() for n in res["names"]}
            uid = res["uid"]
        except Exception:
            mine = set()
        if mine:
            me = max((e["hdr"] for _, e in hdrs if e["hdr"].get("player", "").lower() in mine),
                     key=lambda h: h.get("ts", 0)).get("player", "").lower()
        else:
            me = max(hdrs, key=lambda pe: pe[1]["hdr"].get("ts", 0))[1]["hdr"].get("player", "").lower()
    if uid is None:
        uids = Counter(e["hdr"].get("uid") for _, e in hdrs if e["hdr"].get("player", "").lower() == me
                       and e["hdr"].get("uid"))
        uid = uids.most_common(1)[0][0] if uids else None
    out, seen = [], set()
    for p, e in hdrs:
        h = e["hdr"]
        if not (h.get("player", "").lower() == me or (uid and h.get("uid") == uid)):
            continue
        sig = (h.get("md5"), h.get("ts"), h.get("score"))
        if sig in seen:
            continue
        seen.add(sig)
        if int(h.get("mbits") or 0) & ASSISTED_BITS or set(_split_acronyms(str(h.get("mods") or ""))) & ASSISTED_ACR:
            continue
        acc, ts = _num(h.get("acc")), _num(h.get("ts"))
        if acc is None or ts is None or not h.get("md5"):
            continue
        s = e.get("sum") if isinstance(e.get("sum"), dict) else {}
        out.append({"ts": ts, "md5": h["md5"], "mods": str(h.get("mods") or "NM"),
                    "rate": _num(h.get("rate")) or 1.0, "acc": acc, "misses": int(h.get("misses") or 0),
                    "nobj": int(s["nobj"]) if _num(s.get("nobj")) else None})
    return out


def _wquant(vals, ws, q):
    pairs = sorted(zip(vals, ws))
    tot = sum(w for _, w in pairs)
    if tot <= 0:
        return None
    c = 0.0
    for v, w in pairs:
        c += w
        if c >= q * tot - 1e-12:
            return v
    return pairs[-1][0]


def _pav(ys, ws, increasing=True):
    """Weighted isotonic regression (pool adjacent violators)."""
    sgn = 1 if increasing else -1
    blocks = []  # [value*sgn, weight, count]
    for y, w in zip(ys, ws):
        blocks.append([sgn * y, w, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            a, b = blocks.pop(), blocks.pop()
            wt = a[1] + b[1]
            blocks.append([(a[0] * a[1] + b[0] * b[1]) / wt, wt, a[2] + b[2]])
    out = []
    for v, _, n in blocks:
        out += [sgn * v] * n
    return out


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]) if xs[i] > xs[i - 1] else 0
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _fit(points):
    """points: [(stars, acc, miss_per_100|None, w)] -> fit dict (bins + isotonic curves)."""
    lo = min(p[0] for p in points)
    bins = {}
    for p in points:
        bins.setdefault(int((p[0] - lo) // BIN), []).append(p)
    rows = []
    for k in sorted(bins):
        ps = bins[k]
        w = [p[3] for p in ps]
        wm = [(p[2], p[3]) for p in ps if p[2] is not None]
        rows.append({"stars": round(sum(p[0] * p[3] for p in ps) / sum(w), 3), "n": len(ps),
                     "w": round(sum(w), 3),
                     "acc_med": _wquant([p[1] for p in ps], w, 0.5),
                     "acc_p80": _wquant([p[1] for p in ps], w, 0.8),
                     "miss_med": _wquant([m for m, _ in wm], [x for _, x in wm], 0.5) if wm else None,
                     "miss_p20": _wquant([m for m, _ in wm], [x for _, x in wm], 0.2) if wm else None,
                     "wm": sum(x for _, x in wm)})
    xs = [r["stars"] for r in rows]
    ws = [max(r["w"], 1e-3) for r in rows]
    acc_med = _pav([r["acc_med"] for r in rows], ws, increasing=False)
    acc_p80 = _pav([r["acc_p80"] for r in rows], ws, increasing=False)
    mrows = [(r["stars"], r["miss_med"], r["miss_p20"], max(r["wm"], 1e-3)) for r in rows if r["miss_med"] is not None]
    mx = [m[0] for m in mrows]
    miss_med = _pav([m[1] for m in mrows], [m[3] for m in mrows], increasing=True) if mrows else []
    miss_p20 = _pav([m[2] for m in mrows], [m[3] for m in mrows], increasing=True) if mrows else []
    # tail behaviour for extrapolation above the hardest maps played: fitted slope over the top ~1 star,
    # but never more optimistic than -2% acc and x1.8 misses per extra star (coach heuristic)
    hi = xs[-1]
    top = [i for i, x in enumerate(xs) if x >= hi - 1.0]
    acc_slope = -0.02
    if len(top) >= 2 and xs[-1] - xs[top[0]] >= 0.4:
        acc_slope = min(acc_slope, (acc_med[-1] - acc_med[top[0]]) / (xs[-1] - xs[top[0]]))
    miss_ratio = 1.8
    mtop = [i for i, x in enumerate(mx) if x >= hi - 1.0]
    if len(mtop) >= 2 and mx[-1] - mx[mtop[0]] >= 0.4 and miss_med[mtop[0]] > 0.05:
        miss_ratio = max(miss_ratio, (miss_med[-1] / miss_med[mtop[0]]) ** (1 / (mx[-1] - mx[mtop[0]])))
    for r, a, b in zip(rows, acc_med, acc_p80):
        r["acc_fit"], r["acc_p80_fit"] = round(a, 4), round(b, 4)
    for r in rows:
        for k in ("acc_med", "acc_p80", "miss_med", "miss_p20"):
            if r[k] is not None:
                r[k] = round(r[k], 4)
        r.pop("wm", None)
    return {"bins": rows, "x": xs, "acc_med": [round(v, 4) for v in acc_med], "acc_p80": [round(v, 4) for v in acc_p80],
            "mx": mx, "miss_med": [round(v, 4) for v in miss_med], "miss_p20": [round(v, 4) for v in miss_p20],
            "sr_range": [round(xs[0], 2), round(hi, 2)], "acc_slope_hi": round(acc_slope, 4),
            "miss_ratio_hi": round(miss_ratio, 3)}


def _curve_at(fit, s):
    """(acc_med, acc_p80, miss_med, miss_p20, extrapolated_by) at stars s; misses per 100 objects."""
    xs, hi = fit["x"], fit["x"][-1]
    over = max(0.0, s - hi)
    a_m = _interp(xs, fit["acc_med"], s) + fit["acc_slope_hi"] * over
    a_g = _interp(xs, fit["acc_p80"], s) + fit["acc_slope_hi"] * over
    if fit["mx"]:
        m_m = _interp(fit["mx"], fit["miss_med"], s)
        m_g = _interp(fit["mx"], fit["miss_p20"], s)
        if s > fit["mx"][-1]:
            k = fit["miss_ratio_hi"] ** (s - fit["mx"][-1])
            m_m = max(m_m, 0.3) * k
            m_g = max(m_g, 0.1) * k
    else:
        m_m, m_g = None, None
    a_m, a_g = max(0.5, min(1.0, a_m)), max(0.5, min(1.0, a_g))
    a_g = max(a_g, a_m)
    if m_m is not None:
        m_m, m_g = min(m_m, 20.0), min(m_g, 20.0)
        m_g = min(m_g, m_m)
    return a_m, a_g, m_m, m_g, over


def _model_cache_sig(recs_mtime):
    with _sr_lock:
        _sr_load()
        n = len(_sr["data"])
    return {"replays_mtime": recs_mtime, "sr_count": n}


_skill_mem = {"model": None}
_skill_lock = threading.Lock()


def skill_model(refresh=False) -> dict:
    """Personal skill curve: {"n","acc_at","miss_rate_at","comfort_stars","fit","updated", ...}.
    Cached in .coach_skill.json; refit when the replay cache or SR cache changed, or refresh=True.
    When tosu is reachable, up to SR_BUDGET missing star ratings are computed (newest plays first)."""
    try:
        with _skill_lock:
            return _skill_model(refresh)
    except Exception as e:
        _log(f"skill_model failed: {type(e).__name__}: {e}")
        return {"n": 0, "acc_at": {}, "miss_rate_at": {}, "comfort_stars": None, "fit": {}, "updated": int(time.time()),
                "confidence": "low", "basis": "Skill model unavailable (internal error, see pp_live_log.txt)."}


def _skill_model(refresh):
    d = _dir()
    try:
        rmt = int((d / ".coach_replays.json").stat().st_mtime)
    except OSError:
        rmt = None
    cached = _skill_mem["model"] or _load_json(d / ".coach_skill.json", None)
    if isinstance(cached, dict) and not refresh:
        sig = _model_cache_sig(rmt)
        fresh = cached.get("src") == sig
        retry = (cached.get("fit", {}).get("sr_missing", 0) > 0
                 and time.time() - cached.get("sr_attempt_ts", 0) > SR_RETRY_S)
        if fresh and not (retry and tosu_available()):
            _skill_mem["model"] = cached
            return cached
    recs = _player_records()
    now = time.time()
    attempt_ts = (cached or {}).get("sr_attempt_ts", 0) if isinstance(cached, dict) else 0
    # fill missing star ratings, newest first (budgeted, rate-limit friendly: sequential, local only)
    missing = []
    for r in sorted(recs, key=lambda r: -r["ts"]):
        k = (r["md5"], mods_key(r["mods"], r["rate"]), parse_mods(r["mods"], r["rate"])[1])
        if _sr_get(*k) is None and k not in missing:
            missing.append(k)
    if missing and tosu_available():
        attempt_ts = now
        t0, done = time.perf_counter(), 0
        for md5, mk, rate in missing[:SR_BUDGET]:
            if time.perf_counter() - t0 > SR_BUDGET_SECONDS:
                break
            if star_rating(md5, None, mk, rate, _flush=False) is not None:
                done += 1
        _sr_flush()
        _log(f"skill_model: computed {done}/{min(len(missing), SR_BUDGET)} star ratings")
    newest = max((r["ts"] for r in recs), default=now)
    pts, no_sr = [], 0
    for r in recs:
        acs, rate, _ = parse_mods(r["mods"], r["rate"])
        sr = _sr_get(r["md5"], "".join(acs) or "NM", rate)
        w = 0.5 ** (max(0.0, newest - r["ts"]) / 86400.0 / HALF_LIFE_DAYS)
        mr = 100.0 * r["misses"] / r["nobj"] if r["nobj"] else None
        r["_w"], r["_mr"] = w, mr
        if sr is None:
            no_sr += 1
            continue
        pts.append((sr, r["acc"], mr, w, r["ts"]))
    days_idle = (now - newest) / 86400.0 if recs else None
    model = {"n": len(pts), "n_total": len(recs), "acc_at": {}, "miss_rate_at": {}, "comfort_stars": None,
             "updated": int(now), "half_life_days": HALF_LIFE_DAYS, "days_since_last_play": round(days_idle, 1)
             if days_idle is not None else None, "sr_attempt_ts": int(attempt_ts)}
    if recs:
        ws = [r["_w"] for r in recs]
        mrs = [(r["_mr"], r["_w"]) for r in recs if r["_mr"] is not None]
        model["overall"] = {"acc_med": round(_wquant([r["acc"] for r in recs], ws, 0.5), 4),
                            "acc_p80": round(_wquant([r["acc"] for r in recs], ws, 0.8), 4),
                            "miss_med": round(_wquant([m for m, _ in mrs], [w for _, w in mrs], 0.5), 3) if mrs else None,
                            "miss_p20": round(_wquant([m for m, _ in mrs], [w for _, w in mrs], 0.2), 3) if mrs else None}
    if len(pts) >= 3:
        fit = _fit([p[:4] for p in pts])
        fit["sr_missing"] = no_sr
        fit["points"] = [[round(p[0], 2), round(p[1], 4), round(p[2], 2) if p[2] is not None else None,
                          round(p[3], 3)] for p in pts]
        model["fit"] = fit
        lo, hi = fit["sr_range"]
        s = math.floor(lo * 2) / 2
        while s <= hi + 1.0 + 1e-9:
            a, _, m, _, _ = _curve_at(fit, s)
            model["acc_at"][f"{s:.1f}"] = round(a, 4)
            if m is not None:
                model["miss_rate_at"][f"{s:.1f}"] = round(m, 3)
            s += 0.5
        # comfort = hardest SR (0.05 steps, inside the data) where your typical run is >= 93% acc and
        # <= 1 miss per 100 objects (about 2-4 misses on a TV-size map) - "plays it cleanly"
        comfort, s = None, lo
        while s <= hi + 1e-9:
            a, _, m, _, _ = _curve_at(fit, s)
            if a >= 0.93 and (m is None or m <= 1.0):
                comfort = round(s, 2)
            s += 0.05
        model["comfort_stars"] = comfort
        model["basis"] = (f"{len(pts)} of your {len(recs)} plays with star ratings ({lo:.1f}-{hi:.1f}*), "
                          f"recent plays weighted more ({HALF_LIFE_DAYS:.0f}-day half-life)")
    else:
        model["fit"] = {"sr_missing": no_sr}
        model["basis"] = ("No star ratings for your plays yet - start tosu so they can be calculated. "
                          "Until then predictions use your overall typical result on any map." if recs else
                          "No analysed replays yet - play a few maps and run the coach.")
    model["src"] = _model_cache_sig(rmt)
    _atomic_write_json(d / ".coach_skill.json", model)
    _skill_mem["model"] = model
    return model


def _model_at(model, stars):
    """(acc_e, miss_rate_e, acc_g, miss_rate_g, confidence, basis) for a map of `stars`."""
    fit = model.get("fit") or {}
    if fit.get("x") and stars is not None:
        a_m, a_g, m_m, m_g, over = _curve_at(fit, stars)
        near = sum(p[3] for p in fit.get("points", []) if abs(p[0] - stars) <= NEAR)
        n_near = sum(1 for p in fit.get("points", []) if abs(p[0] - stars) <= NEAR)
        conf = "high" if near >= 10 else "medium" if near >= 4 else "low"
        lo, hi = fit["sr_range"]
        basis = f"{n_near} of your plays within {NEAR}* of {stars:.2f}*"
        if over > 0.25:
            conf = "low"
            basis = (f"extrapolated: {stars:.2f}* is {over:.1f}* above the hardest maps you've played recently "
                     f"({hi:.1f}*)")
        elif stars < lo - 0.25:
            conf = "low" if n_near < 3 else conf
            basis = f"below your usual range ({lo:.1f}-{hi:.1f}*); {n_near} plays nearby"
        idle = model.get("days_since_last_play")
        if idle is not None and idle > 45 and conf != "low":
            conf = "medium" if conf == "high" else "low"
            basis += f"; your newest play is {idle:.0f} days old"
        if m_m is None:
            ov = model.get("overall") or {}
            m_m, m_g = ov.get("miss_med"), ov.get("miss_p20")
        return a_m, m_m, a_g, m_g, conf, basis + " (recent plays weigh more)"
    ov = model.get("overall")
    if ov:
        return (ov["acc_med"], ov.get("miss_med"), ov["acc_p80"], ov.get("miss_p20"), "low",
                "no star ratings for your plays yet (start tosu) - using your overall typical result")
    return None, None, None, None, "low", model.get("basis") or "no replay data"


# ----------------------------------------------------------------------------
# prediction
# ----------------------------------------------------------------------------

def _est_combo(max_combo, misses):
    # tosu/rosu-pp would otherwise assume the full combo minus nothing for the given misses. With m misses
    # placed at random, the expected LONGEST unbroken stretch is max * H(m+1)/(m+1) (H = harmonic number;
    # e.g. 600 combo, 1 miss -> 450, 5 misses -> 245). Real misses cluster in hard sections, which leaves
    # even longer stretches, so this is if anything slightly pessimistic.
    if not max_combo:
        return None
    if not misses:
        return int(max_combo)
    m = int(misses)
    h = sum(1.0 / i for i in range(1, m + 2))
    return int(max(1, min(max_combo - m, round(max_combo * h / (m + 1)))))


def predict(osu_path: str, mods, clock_rate: float, stars: float | None = None,
            allow_current: bool = False) -> dict | None:
    """PP for this map at your typical / good / perfect result. None when tosu can't calculate it.
    osu_path None -> None, unless allow_current=True (LiveWatcher only: tosu then calculates the map
    selected in game, and the watcher checks afterwards that the selection didn't change)."""
    if not osu_path and not allow_current:
        return None
    try:
        ss = calc_pp(osu_path, mods, clock_rate, acc=100, misses=0)
        if not ss or ss.get("pp") is None:
            return None
        st = _num(stars) or ss.get("stars")
        model = skill_model()
        acc_e, mr_e, acc_g, mr_g, conf, basis = _model_at(model, st)
        nobj, mc = ss.get("n_objects"), ss.get("max_combo")
        out = {"stars": round(st, 2) if st is not None else None, "pp_ss": round(ss["pp"], 1),
               "pp_fc_pred_acc": None, "pp_expected": None, "pp_good_run": None,
               "acc_expected": round(acc_e, 4) if acc_e is not None else None, "misses_expected": None,
               "acc_good": round(acc_g, 4) if acc_g is not None else None, "misses_good": None,
               "confidence": conf, "basis": basis}
        if acc_e is None:
            return out
        if mr_e is not None and nobj:
            out["misses_expected"] = int(round(mr_e * nobj / 100.0))
            out["misses_good"] = int(round((mr_g if mr_g is not None else mr_e) * nobj / 100.0))
        me, mg = out["misses_expected"] or 0, out["misses_good"] or 0
        fc = calc_pp(osu_path, mods, clock_rate, acc=acc_e, misses=0, combo=mc)
        ex = calc_pp(osu_path, mods, clock_rate, acc=acc_e, misses=me, combo=_est_combo(mc, me))
        gd = calc_pp(osu_path, mods, clock_rate, acc=acc_g, misses=mg, combo=_est_combo(mc, mg))
        out["pp_fc_pred_acc"] = round(fc["pp"], 1) if fc and fc.get("pp") is not None else None
        out["pp_expected"] = round(ex["pp"], 1) if ex and ex.get("pp") is not None else None
        out["pp_good_run"] = round(gd["pp"], 1) if gd and gd.get("pp") is not None else None
        if out["pp_good_run"] is not None and out["pp_expected"] is not None:
            out["pp_good_run"] = max(out["pp_good_run"], out["pp_expected"])
        return out
    except Exception as e:
        _log(f"predict failed: {type(e).__name__}: {e}")
        return None


# ----------------------------------------------------------------------------
# rank gain (exact weighted sum)
# ----------------------------------------------------------------------------

def weighted_total(pps):
    return sum(p * 0.95 ** i for i, p in enumerate(sorted(pps, reverse=True)[:100]))


BONUS_PP_MAX = 416.6667      # bonus pp = 416.6667 * (1 - 0.995^min(1000, ranked scores))


def tail_pp(total_pp, top_pps, n_scores=1000):
    """Weighted pp of your scores BELOW #100 (sum over i >= 100 of pp_i * 0.95^i), implied by the profile
    total: total - bonus - weighted top 100. n_scores defaults to 1000 (bonus at its max: an 11-year account).
    0 when unknown or when there are fewer than 100 top plays."""
    tot = _num(total_pp)
    pps = [_num(p) for p in (top_pps or []) if _num(p) is not None]
    if tot is None or len(pps) < 100:
        return 0.0
    bonus = BONUS_PP_MAX * (1 - 0.995 ** min(1000, max(0, int(n_scores or 0))))
    return max(0.0, tot - bonus - weighted_total(pps))


def tail_adjust(top_sorted, new_pp, replaced_in_list, tail):
    """Correction to the top-100 gain for the scores below #100: when a new score ENTERS a full top 100 (not
    replacing one in it), the old #100 moves to #101 (keeps 0.95^100 of its pp) and every lower score drops
    one place (loses 5% of its weighted value)."""
    if not tail and tail != 0.0:
        return 0.0
    if len(top_sorted) < 100 or replaced_in_list or new_pp <= top_sorted[99]:
        return 0.0
    return top_sorted[99] * 0.95 ** 100 - 0.05 * float(tail or 0.0)


def rank_gain_pp(new_pp, top_plays, beatmap_id=None, md5=None, total_pp=None):
    """Exact change of your total pp (bonus excluded) if a play of new_pp were set. A better play on the same
    beatmap replaces the old one (only your best per map counts). total_pp (profile total) adds the
    below-#100 correction (tail_adjust); without it only the weighted top 100 is counted."""
    new_pp = _num(new_pp)
    if new_pp is None or not isinstance(top_plays, list):
        return None
    plays = [t for t in top_plays if isinstance(t, dict) and _num(t.get("pp")) is not None]
    before = [_num(t["pp"]) for t in plays]
    after = []
    replaced = False
    for t in plays:
        same = (beatmap_id and t.get("beatmap_id") == beatmap_id) or (md5 and t.get("md5") == md5)
        if same:
            if _num(t["pp"]) >= new_pp:
                return 0.0
            replaced = True
            continue
        after.append(_num(t["pp"]))
    after.append(new_pp)
    g = weighted_total(after) - weighted_total(before)
    if total_pp is not None:
        g += tail_adjust(sorted(before, reverse=True), new_pp, replaced, tail_pp(total_pp, before))
    return round(max(0.0, g), 2)


# ----------------------------------------------------------------------------
# live watcher
# ----------------------------------------------------------------------------

STABLE_STATES = {0: "menu", 2: "playing", 5: "song_select", 13: "song_select", 7: "results", 14: "results",
                 17: "results", 18: "results"}


def screen_of(snap, _seen=set()):
    st = snap.get("state") if isinstance(snap.get("state"), dict) else {}
    num = st.get("number")
    name = str(st.get("name") or "").lower()
    scr = None
    if isinstance(num, int) and not isinstance(num, bool) and num in STABLE_STATES:
        scr = STABLE_STATES[num]
    elif name:
        if "select" in name:          # before "play": lazer/stable song select can be "selectPlay"/"PlaySongSelect"
            scr = "song_select"
        elif "result" in name or "rank" in name:
            scr = "results"
        elif "play" in name or "player" in name:
            scr = "playing"
        elif "menu" in name:
            scr = "menu"
    if scr is None:
        k = (num, name)
        if k not in _seen and len(_seen) < 100:
            _seen.add(k)
            _log(f"unknown tosu state number={num!r} name={name!r}")
        scr = "other"
    return scr


def _snap_mods(snap):
    m = _g(snap, "play", "mods")
    if not isinstance(m, dict) or not (m.get("name") or m.get("number") or m.get("array")):
        m2 = _g(snap, "menu", "mods")
        m = m2 if isinstance(m2, dict) else (m if isinstance(m, dict) else {})
    rate = _num(m.get("rate"))
    if isinstance(m.get("array"), list) and m["array"]:
        acs, r, _ = parse_mods(m["array"], rate)
        src = m["array"]
    elif isinstance(m.get("name"), str) and m.get("name"):
        acs, r, _ = parse_mods(m["name"], rate)
        src = m["name"]
    elif _num(m.get("number")) is not None:
        acs, r, _ = parse_mods(int(m["number"]), rate)
        src = int(m["number"])
    else:
        acs, r, src = [], 1.0, None
    return acs, r, src


# beatmap.status from tosu: only the NAME is trusted (stable and lazer number these differently)
RANKED_NAMES = ("ranked", "approved")
UNRANKED_NAMES = ("loved", "qualified", "pending", "graveyard", "wip", "notsubmitted", "not submitted",
                  "unknown", "none", "locallymodified", "locally modified", "unranked")


def ranked_of(snap, _seen=set()):
    """True (ranked/approved: gives pp), False (loved/unranked/...: no pp), None (tosu didn't say)."""
    stv = _g(snap, "beatmap", "status")
    name = stv.get("name") if isinstance(stv, dict) else stv if isinstance(stv, str) else None
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower()
    if any(n.startswith(x) for x in RANKED_NAMES):
        return True
    if any(x in n for x in UNRANKED_NAMES):
        return False
    if n not in _seen and len(_seen) < 50:
        _seen.add(n)
        _log(f"unknown beatmap status {name!r} (number={_g(stv, 'number') if isinstance(stv, dict) else None!r})")
    return None


def _snap_path(snap, md5):
    p = osu_path_for_md5(md5)
    if p:
        return p
    cand = [_g(snap, "directPath", "beatmapFile")]
    songs, folder, fn = _g(snap, "folders", "songs"), _g(snap, "folders", "beatmap"), _g(snap, "files", "beatmap")
    if songs and folder and fn:
        cand.append(os.path.join(str(songs), str(folder), str(fn)))
    for c in cand:
        try:
            if c and os.path.isfile(str(c)):
                return str(c)
        except Exception:
            pass
    return None   # tosu then calculates the map currently selected in game


# ----------------------------------------------------------------------------
# live attempt telemetry -> live_attempts.jsonl  (CONTRACT_ADAPT.md)
# ----------------------------------------------------------------------------
#
# Verified on the user's osu!lazer 2026.1005 + tosu (2026-11-09, read-only polling):
#   song select = state {5, "selectPlay"}; playing = {2, "play"}. During play, play.hits / combo / accuracy
#   (percent) / unstableRate / hitErrorArray / healthBar grow live and beatmap.time.live is the map clock (ms).
#   A FAIL in lazer: play.failed turns true and healthBar.normal hits 0 while the state stays 2; leaving the
#   fail screen goes straight to {5, "selectPlay"} and at that moment play.* is already reset to zeros (so the
#   attempt must be built from the last 'play' samples, never from the first non-play snapshot).
#   In song select beatmap.time.live is the music PREVIEW position - ignored outside play.
#   Re-verified 2026-11-09 18:xx: a pass goes {2,"play"} -> {7,"resultScreen"} (resultsScreen hits/accuracy/
#   playerName filled on the FIRST results poll, play.* still filled) -> {5,"selectPlay"} ~1 s later when the
#   player skips results. Retry from the fail screen: state stays 2, failed true->false, hits all 0, time.live
#   stays at the fail position ~3 s, then jumps to the start (handled in _is_restart).

ATTEMPTS_FILE = "live_attempts.jsonl"   # in _dir(); env OSU_COACH_ATTEMPTS_FILE overrides the full path
ATTEMPTS_ROTATE_BYTES = 5 * 1024 * 1024  # -> live_attempts.1.jsonl (one old generation kept)
HIT_ERRORS_CAP = 2000
RESULTS_WAIT_S = 3.0      # after entering results: wait this long for resultsScreen numbers to appear
RETRY_GRACE_S = 4.0       # left play to a loader/unknown screen: same map back in play within this -> "retry"
LOST_GIVEUP_S = 20.0      # tosu unreachable mid-attempt for this long -> written as quit (end "tosu_lost")

_attempt_lock = threading.Lock()


def attempts_path() -> Path:
    p = os.environ.get("OSU_COACH_ATTEMPTS_FILE")
    return Path(p) if p else _dir() / ATTEMPTS_FILE


def append_attempt(rec: dict, path: Path | None = None) -> bool:
    """Append one attempt as a single JSON line. One write() of the whole encoded line + flush + fsync, so a
    reader never sees half a record except after a crash mid-write (readers should skip lines that don't
    parse). Rotates to <name>.1.jsonl once the file exceeds ATTEMPTS_ROTATE_BYTES. Retries briefly when
    OneDrive or a reader holds the file. -> True when written."""
    path = Path(path) if path else attempts_path()
    try:
        line = (json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode("utf-8")
    except Exception as e:
        _log(f"attempt not serialisable: {e}")
        return False
    with _attempt_lock:
        for i in range(8):
            try:
                try:
                    if path.stat().st_size + len(line) > ATTEMPTS_ROTATE_BYTES:
                        os.replace(path, path.with_name(path.stem + ".1" + path.suffix))
                except FileNotFoundError:
                    pass
                fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
                try:
                    os.write(fd, line)
                    try:
                        os.fsync(fd)
                    except OSError:
                        pass
                finally:
                    os.close(fd)
                return True
            except PermissionError:
                time.sleep(0.1 * (i + 1))
            except Exception as e:
                _log(f"append attempt failed: {type(e).__name__}: {e}")
                return False
    _log("append attempt failed: file locked")
    return False


def read_attempts(since_ts: float = 0, path: Path | None = None) -> list[dict]:
    """All attempts with ts >= since_ts, oldest first (both generations; bad lines skipped)."""
    path = Path(path) if path else attempts_path()
    out = []
    for p in (path.with_name(path.stem + ".1" + path.suffix), path):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and (_num(r.get("ts")) or 0) >= since_ts:
                        out.append(r)
        except OSError:
            pass
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def _hits_of(d):
    h = d.get("hits") if isinstance(d, dict) and isinstance(d.get("hits"), dict) else {}
    out = {}
    for k in ("300", "100", "50", "0", "sliderBreaks"):
        v = _num(h.get(k))
        out[k] = int(v) if v is not None else 0
    return out


def _judged(h):
    return h["300"] + h["100"] + h["50"] + h["0"]


class AttemptTracker:
    """Turns a stream of tosu snapshots into finished attempts. Pure logic (no I/O, no threads):
    feed(snap, screen, now_wall, now_mono) -> list of finished attempt dicts (usually empty).

    Attempt end rules
      * results screen after play                     -> outcome "passed"  (end "results")
      * left play after the last object without a fail -> outcome "passed"  (end "completed")
      * play.failed seen during the attempt, then left -> outcome "failed"  (end "fail_quit" or "retry")
      * left play mid-map (song select / menu)         -> outcome "quit"    (end "quit")
      * same map restarted: judged hits went DOWN, or time.live jumped back > 3 s to before the first
        object + 2 s with (almost) no hits            -> previous attempt "quit" (or "failed") with end "retry"
      * another map appears while still in play       -> "quit", end "map_change"
      * tosu unreachable > LOST_GIVEUP_S              -> "quit", end "tosu_lost"; flush() -> end "stopped"
    Attempts with zero judged objects are dropped (a restart before the first note is not an attempt).
    """

    def __init__(self, poll_s: float = 0.5):
        self.poll_s = poll_s
        self.cur = None          # attempt being played
        self.pending = None      # (attempt, kind, deadline_mono) waiting for results data / retry decision
        self.lost_since = None
        self.dropped = 0
        self._stale_next = False
        self._other = None       # (md5, polls) another map seen while an attempt is open (needs 2 in a row)

    # -- building --
    def _start(self, snap, meta, now_wall, now_mono):
        bm = snap.get("beatmap") if isinstance(snap.get("beatmap"), dict) else {}
        t = bm.get("time") if isinstance(bm.get("time"), dict) else {}
        pl = snap.get("play") if isinstance(snap.get("play"), dict) else {}
        prof = _g(snap, "profile", "name")
        self.cur = {"md5": meta.get("md5"), "beatmap_id": meta.get("beatmap_id"), "artist": meta.get("artist"),
                    "title": meta.get("title"), "version": meta.get("version"), "mods": meta.get("mods"),
                    "rate": meta.get("rate"), "stars": meta.get("stars"), "ranked": meta.get("ranked"),
                    "first_ms": _num(t.get("firstObject")), "last_ms": _num(t.get("lastObject")),
                    "started_ts": round(now_wall, 3), "_t0": now_mono, "_last_mono": now_mono,
                    "_hits": {"300": 0, "100": 0, "50": 0, "0": 0, "sliderBreaks": 0}, "_live": None,
                    "_max_live": None, "miss_times_ms": [], "_errs": [], "_failed": False, "_acc": None,
                    "_ur": None, "_combo_max": 0, "_hp_min": None, "_samples": 0, "_paused": 0,
                    "player": pl.get("playerName") if isinstance(pl.get("playerName"), str) else None,
                    "profile": prof if isinstance(prof, str) else None, "joined_late": False}
        # watcher (re)started mid-map: misses already on the counter happened at unknown times - use the
        # current counts as the baseline so they are not all stamped "now" (they still count in hits)
        live, first = _num(t.get("live")), _num(t.get("firstObject")) or 0
        h = _hits_of(pl)
        if _judged(h) > 0 and live is not None and live - first > 3000:
            self.cur["_hits"] = h
            self.cur["_live"] = live
            self.cur["joined_late"] = True
        self.cur["_stale"] = None

    def _start_stale(self, snap, meta, now_wall, now_mono):
        """A retry detected from the clock while the previous try's counters still show: start the new attempt
        and ignore the counters until they drop (lazer resets them a poll later), then count from zero."""
        self._start(snap, meta, now_wall, now_mono)
        h = _hits_of(snap.get("play") if isinstance(snap.get("play"), dict) else {})
        if _judged(h) > 2 and not self.cur["joined_late"]:
            self.cur["_stale"] = _judged(h)

    def _sample(self, snap, now_mono):
        c = self.cur
        pl = snap.get("play") if isinstance(snap.get("play"), dict) else {}
        live = _num(_g(snap, "beatmap", "time", "live"))
        h = _hits_of(pl)
        if c.get("_stale") is not None:
            if _judged(h) >= c["_stale"]:
                c["_last_mono"] = now_mono      # still the previous try's counters
                c["_samples"] += 1
                return
            c["_stale"] = None
        if _judged(h) < _judged(c["_hits"]):
            h = c["_hits"]       # glitched read (see _is_restart): keep the counters we had
        prev_live = c["_live"]
        dm = h["0"] - c["_hits"]["0"]
        if dm > 0 and live is not None:
            # k misses happened between the previous sample and this one: place them at the midpoint
            # (error <= half a poll interval of map time)
            t = live
            if prev_live is not None and 0 <= live - prev_live <= 5000:
                t = (prev_live + live) / 2.0
            c["miss_times_ms"].extend([int(round(t))] * min(dm, 50))
        c["_hits"] = h
        if live is not None:
            c["_live"] = live
            # real lazer: the first ~3 s of state 2 still report the song-select PREVIEW position in
            # time.live (e.g. 48938) with zero hits, then the clock jumps to the map start (558). So the
            # furthest point reached only counts once something has been judged.
            if _judged(h) > 0:
                c["_max_live"] = live if c["_max_live"] is None else max(c["_max_live"], live)
        errs = pl.get("hitErrorArray")
        if isinstance(errs, list) and errs:
            c["_errs"] = errs          # tosu sends the whole array of this play every time
        acc = _num(pl.get("accuracy"))
        if acc is not None and _judged(h) > 0:
            c["_acc"] = acc / 100.0 if acc > 1.0 else acc
        ur = _num(pl.get("unstableRate"))
        if ur is not None and ur > 0:
            c["_ur"] = ur
        cm = _num(_g(pl, "combo", "max"))
        if cm is not None:
            c["_combo_max"] = max(c["_combo_max"], int(cm))
        hp = _num(_g(pl, "healthBar", "normal"))
        if hp is not None and _judged(h) > 0:   # before the first judgement lazer shows a stale 0
            c["_hp_min"] = hp if c["_hp_min"] is None else min(c["_hp_min"], hp)
        if pl.get("failed") is True:
            c["_failed"] = True
        if _g(snap, "game", "paused") is True:
            c["_paused"] += 1
        if isinstance(pl.get("playerName"), str) and pl.get("playerName"):
            c["player"] = pl["playerName"]
        c["_samples"] += 1
        c["_last_mono"] = now_mono

    def _is_restart(self, snap):
        c = self.cur
        pl = snap.get("play") if isinstance(snap.get("play"), dict) else {}
        h = _hits_of(pl)
        live = _num(_g(snap, "beatmap", "time", "live"))
        # real lazer retry from the fail screen (observed 2026-11-09): state stays 2, play.failed flips back
        # to false and every counter is 0 while time.live still shows the fail position for ~3 s, and only
        # then jumps to the map start. The failed->not-failed flip with zeroed counters is the restart.
        if c["_failed"] and pl.get("failed") is False and _judged(h) == 0:
            return True
        # counters only grow within one play; a drop together with the clock going back = restart
        # (a drop alone, with the clock still moving forward, is treated as a glitched read and ignored)
        if _judged(h) < _judged(c["_hits"]) and (live is None or c["_live"] is None or live < c["_live"] - 1500):
            return True
        first = c["first_ms"] or 0
        if live is not None and c["_live"] is not None and live < c["_live"] - 3000 \
                and live < first + 2000 and _judged(h) <= 2:
            return True
        # pause -> retry where lazer resets the clock a poll before the counters: back at the start, well
        # behind the furthest point reached = a restart, whatever the (stale) counters say
        if live is not None and c["_max_live"] is not None and live < first + 2000 and live < c["_max_live"] - 5000:
            return "clock"
        return False

    def _finish(self, a, outcome, end, now_wall, results=None):
        if a is None:
            return None
        h = dict(a["_hits"])
        acc, max_combo = a["_acc"], a["_combo_max"]
        if results:   # resultsScreen numbers (same play, final) override the last live sample
            rh = _hits_of(results)
            if _judged(rh) > 0:
                rh["sliderBreaks"] = h.get("sliderBreaks", 0)
                h = rh
            ra = _num(results.get("accuracy"))
            if ra:
                acc = ra / 100.0 if ra > 1.0 else ra
            rc = _num(results.get("maxCombo"))
            if rc:
                max_combo = int(rc)
        if _judged(h) == 0:
            self.dropped += 1
            return None
        first, last, mx = a["first_ms"], a["last_ms"], a["_max_live"]
        prog = None
        if outcome == "passed":
            prog = 1.0
        elif first is not None and last is not None and last > first and mx is not None:
            prog = round(max(0.0, min(1.0, (mx - first) / (last - first))), 4)
        errs = [round(float(e), 2) for e in a["_errs"] if _num(e) is not None]
        if len(errs) > HIT_ERRORS_CAP:
            errs = errs[-HIT_ERRORS_CAP:]      # keep the end of the map (late drift / fatigue)
        mean = sum(errs) / len(errs) if errs else None
        own = not (a.get("player") and a.get("profile") and a["player"].lower() != a["profile"].lower())
        rec = {"v": 1, "ts": round(now_wall, 3), "started_ts": a["started_ts"], "source": "live",
               "md5": a["md5"], "beatmap_id": a["beatmap_id"], "artist": a["artist"], "title": a["title"],
               "version": a["version"], "mods": a["mods"], "rate": a["rate"], "stars": a["stars"],
               "ranked": a["ranked"], "outcome": outcome, "end": end,
               "time_ms": int(mx) if mx is not None else None,
               "first_ms": int(first) if first is not None else None,
               "last_ms": int(last) if last is not None else None,
               "progress": prog, "acc": round(acc, 4) if acc is not None else None,
               "hits": {k: h[k] for k in ("300", "100", "50", "0")}, "slider_breaks": h.get("sliderBreaks", 0),
               "misses": h["0"], "max_combo": max_combo,
               "ur": round(a["_ur"], 1) if a["_ur"] is not None else None,
               "mean_error_ms": round(mean, 2) if mean is not None else None,
               "hit_errors": errs, "miss_times_ms": a["miss_times_ms"][:5000],
               "miss_time_res_ms": int(self.poll_s * 1000 / 2 * (_num(a.get("rate")) or 1.0)),
               "failed_seen": a["_failed"],
               "hp_min": round(a["_hp_min"], 1) if a["_hp_min"] is not None else None,
               "duration_s": round(a["_last_mono"] - a["_t0"], 1), "player": a.get("player"), "own": own,
               "joined_late": a.get("joined_late", False)}
        return rec

    @staticmethod
    def _left(a):
        """(outcome, end) for an attempt that left play without a results screen."""
        if a["_failed"]:
            return "failed", "fail_quit"
        if a["_max_live"] is not None and a["last_ms"] and a["_max_live"] >= a["last_ms"]:
            # every object was played without failing, then the results screen was skipped / missed
            return "passed", "completed"
        return "quit", "quit"

    # -- driving --
    def feed(self, snap, screen, now_wall=None, now_mono=None) -> list:
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        out = []
        self.lost_since = None
        md5 = _g(snap, "beatmap", "checksum") or None
        # pending decisions first
        if self.pending:
            a, kind, deadline = self.pending
            if kind == "results":
                rs = snap.get("resultsScreen") if isinstance(snap.get("resultsScreen"), dict) else {}
                ready = screen == "results" and _judged(_hits_of(rs)) > 0 and \
                    (not rs.get("playerName") or not a.get("player") or rs.get("playerName") == a.get("player"))
                if ready or now_mono >= deadline or screen == "playing":
                    out.append(self._finish(a, "passed", "results", now_wall, rs if ready else None))
                    self.pending = None
            elif kind == "left":
                if screen == "playing" and md5 == a["md5"]:
                    pl = snap.get("play") if isinstance(snap.get("play"), dict) else {}
                    live = _num(_g(snap, "beatmap", "time", "live"))
                    if _judged(_hits_of(pl)) >= _judged(a["_hits"]) and _judged(a["_hits"]) > 0 and \
                            live is not None and a["_live"] is not None and live >= a["_live"] - 1000:
                        # the counters and the clock carried on: one bad read, the same attempt continues
                        self.cur, self.pending = a, None
                        a.pop("_away", None)
                    else:
                        out.append(self._finish(a, "failed" if a["_failed"] else "quit", "retry", now_wall))
                        self.pending = None
                        if _judged(_hits_of(pl)) >= max(3, _judged(a["_hits"])):
                            self._stale_next = True      # clock went back but the old counters still show
                elif screen in ("song_select", "menu") and not a.get("_away"):
                    a["_away"] = 1           # one poll away might be a glitch: decide on the next one
                elif now_mono >= deadline or screen in ("song_select", "menu", "results") or \
                        (screen == "playing" and md5 and md5 != a["md5"]):
                    out.append(self._finish(a, *self._left(a), now_wall))
                    self.pending = None
        if screen == "playing":
            if self.cur and not md5:
                return [r for r in out if r]     # a play snapshot without a map checksum: a glitched read
            if self.cur and md5 != self.cur["md5"]:
                # another map only counts after two polls in a row (one odd read must not split the attempt)
                n = self._other[1] + 1 if self._other and self._other[0] == md5 else 1
                self._other = (md5, n)
                if n < 2:
                    return [r for r in out if r]
                a = self.cur
                out.append(self._finish(a, "failed" if a["_failed"] else "quit", "map_change", now_wall))
                self.cur = None
            elif self.cur and (why := self._is_restart(snap)):
                a = self.cur
                out.append(self._finish(a, "failed" if a["_failed"] else "quit", "retry", now_wall))
                self.cur = None
                if md5 and why == "clock":
                    self._start_stale(snap, _attempt_meta(snap), now_wall, now_mono)
            self._other = None
            if self.cur is None and md5:
                if self._stale_next:
                    self._start_stale(snap, _attempt_meta(snap), now_wall, now_mono)
                else:
                    self._start(snap, _attempt_meta(snap), now_wall, now_mono)
            self._stale_next = False
            if self.cur:
                self._sample(snap, now_mono)
        elif self.cur:
            a, self.cur = self.cur, None
            if screen == "results":
                if a["_failed"]:   # shouldn't happen in lazer, but a failed play never counts as a pass
                    out.append(self._finish(a, "failed", "fail_quit", now_wall))
                else:
                    self.pending = (a, "results", now_mono + RESULTS_WAIT_S)
                    out.extend(self.feed(snap, screen, now_wall, now_mono))   # results may already be there
            elif screen in ("song_select", "menu"):
                # one song-select read mid-map can be a glitch: decide on the next poll (0.5 s later); if the
                # play just carries on there, it is the same attempt
                a["_away"] = 1
                self.pending = (a, "left", now_mono + RETRY_GRACE_S)
            else:   # loader / unknown screen: maybe a restart
                self.pending = (a, "left", now_mono + RETRY_GRACE_S)
        return [r for r in out if r]

    def lost(self, now_wall=None, now_mono=None) -> list:
        """tosu unreachable this poll."""
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        if self.lost_since is None:
            self.lost_since = now_mono
        if now_mono - self.lost_since >= LOST_GIVEUP_S:
            return [r for r in (self.flush("tosu_lost", now_wall)) if r]
        return []

    def flush(self, end="stopped", now_wall=None) -> list:
        now_wall = time.time() if now_wall is None else now_wall
        out = []
        if self.pending:
            a, kind, _ = self.pending
            out.append(self._finish(a, "passed", "results", now_wall) if kind == "results" else
                       self._finish(a, "failed" if a["_failed"] else "quit", end, now_wall))
            self.pending = None
        if self.cur:
            a, self.cur = self.cur, None
            out.append(self._finish(a, "failed" if a["_failed"] else "quit", end, now_wall))
        return [r for r in out if r]


def _attempt_meta(snap):
    bm = snap.get("beatmap") if isinstance(snap.get("beatmap"), dict) else {}
    acs, rate, _ = _snap_mods(snap)
    stars = _num(_g(bm, "stats", "stars", "total"))
    bid = bm.get("id")
    return {"md5": bm.get("checksum") or None, "beatmap_id": bid if isinstance(bid, int) and bid > 0 else None,
            "artist": bm.get("artist"), "title": bm.get("title"), "version": bm.get("version"),
            "mods": mods_display(acs), "rate": rate, "stars": round(stars, 2) if stars else None,
            "ranked": ranked_of(snap)}


def attempt_summary(rec: dict | None) -> dict | None:
    """Small version of an attempt for the watcher state (no arrays)."""
    if not rec:
        return None
    return {k: rec.get(k) for k in ("ts", "md5", "beatmap_id", "artist", "title", "version", "mods", "rate",
                                    "stars", "outcome", "end", "progress", "acc", "misses", "ur", "max_combo",
                                    "own")}


# ----------------------------------------------------------------------------
# tosu executable (for the pet's "start tosu" - pp_live itself only ever talks HTTP)
# ----------------------------------------------------------------------------

def find_tosu_exe(configured: str | None = None) -> str | None:
    """Best tosu.exe to launch: env OSU_COACH_TOSU_EXE, then `configured` (pet_settings tosu_path), then
    %LOCALAPPDATA%\\tosu\\tosu.exe, %LOCALAPPDATA%\\Programs\\tosu\\tosu.exe, ~\\tosu\\tosu.exe.
    Paths inside OneDrive are skipped: tosu refuses to run from a OneDrive folder (its own log says so).
    Whether tosu is RUNNING is tosu_available() - an HTTP check against the real instance."""
    la = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    cands = [os.environ.get("OSU_COACH_TOSU_EXE"), configured,
             os.path.join(la, "tosu", "tosu.exe"), os.path.join(la, "Programs", "tosu", "tosu.exe"),
             str(Path.home() / "tosu" / "tosu.exe")]
    for c in cands:
        if not c:
            continue
        try:
            p = os.path.normpath(str(c))
            if "onedrive" in p.lower():
                continue
            if os.path.isfile(p):
                return p
        except Exception:
            pass
    return None


_pm = {"mod": None, "tried": -1e9}


def _pp_model():
    """pp_model (CONTRACT_PP2 self-learning predictor), imported lazily; None when unavailable."""
    if _pm["mod"] is not None:
        return _pm["mod"]
    if os.environ.get("OSU_COACH_NO_PP_MODEL"):
        return None
    if time.monotonic() - _pm["tried"] < 60:
        return None
    _pm["tried"] = time.monotonic()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pp_model
        _pm["mod"] = pp_model
    except Exception as e:
        _log(f"pp_model unavailable: {type(e).__name__}: {e}")
    return _pm["mod"]


def _pp_model_sig():
    try:
        return (_dir() / ".pp_model.json").stat().st_mtime
    except OSError:
        return None


DEBOUNCE_STEP_S = 0.2


class LiveWatcher:
    """Background thread polling tosu /json/v2 every 0.5 s (every 3 s while tosu is unreachable).

    on_change(state) is called FROM THE WATCHER THREAD whenever the state dict changes. A Tk caller must
    marshal it onto the Tk thread itself, e.g. `lambda s: root.after(0, show, s)`. Exceptions raised by
    the callback are caught and logged; the watcher never raises.

    Attempt telemetry: every finished attempt (pass / fail / quit / retry, see AttemptTracker) is appended
    to live_attempts.jsonl, then on_attempt(record) is called (watcher thread, after the line is on disk -
    the pet can start an engine run right away). The state dict also carries "last_attempt" (summary of
    the newest attempt) and "attempt_seq" (count of attempts written since start), so a pet that only
    listens to on_change can trigger on attempt_seq changing. record=False disables writing (tests/CLI).
    """

    def __init__(self, on_change: callable, poll: float = 0.25, slow_poll: float = 3.0, debounce: float = 0.15,
                 on_attempt: callable | None = None, record: bool = True):
        self.on_change = on_change
        self.on_attempt = on_attempt
        self.record = record
        self.poll, self.slow_poll, self.debounce = poll, slow_poll, debounce
        self._stop = threading.Event()
        self._thread = None
        self._pred_cache = {}
        self._pred_model = None
        self._last_emit = None
        self._results_key = None
        self._results = None
        self.tracker = AttemptTracker(poll_s=poll)
        self.attempt_seq = 0
        self.last_attempt = None
        self._seen_states = set()
        self._pre_logged = None   # started_ts of the attempt whose pre-attempt prediction was logged
        self._attr_jobs = {}      # prediction key -> when its server attributes were requested (background)
        self._repredict = None    # key to predict again (server attributes arrived)
        self.state = {"tosu_ok": False, "screen": "other", "prediction": None, "results": None,
                      "last_attempt": None, "attempt_seq": 0}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pp_live.LiveWatcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=4.0)
        if not (t and t.is_alive()):
            # an attempt still open (pet closed mid-map) is written as a quit, end "stopped"
            self._attempts_done(self.tracker.flush("stopped"), emit=False)

    # -- attempts --
    def _attempts_done(self, recs, emit=True):
        for rec in recs or []:
            try:
                if self.record and not append_attempt(rec):
                    continue
                self.attempt_seq += 1
                self.last_attempt = attempt_summary(rec)
                _log(f"attempt {rec.get('outcome')}/{rec.get('end')}: {rec.get('artist')} - {rec.get('title')} "
                     f"[{rec.get('version')}] {rec.get('mods')} x{rec.get('rate')} progress={rec.get('progress')} "
                     f"acc={rec.get('acc')} misses={rec.get('misses')} own={rec.get('own')}")
                if self.on_attempt and rec.get("own", True):
                    try:
                        self.on_attempt(dict(rec))
                    except Exception as e:
                        _log(f"on_attempt raised: {type(e).__name__}: {e}")
            except Exception as e:
                _log(f"attempt handling failed: {type(e).__name__}: {e}")
        if recs and emit:
            st = dict(self.state)
            st["last_attempt"], st["attempt_seq"] = self.last_attempt, self.attempt_seq
            self._emit(st)

    def _note_state(self, snap):
        st = snap.get("state") if isinstance(snap.get("state"), dict) else {}
        k = (st.get("number"), str(st.get("name")))
        if k not in self._seen_states and len(self._seen_states) < 60:
            self._seen_states.add(k)
            _log(f"tosu state seen: number={k[0]!r} name={k[1]!r} -> {screen_of(snap)} "
                 f"(client={snap.get('client')!r})")

    # -- internals --
    def _emit(self, state):
        try:
            sig = json.dumps(state, sort_keys=True, default=str)
        except Exception:
            sig = None
        if sig is not None and sig == self._last_emit:
            return
        self._last_emit = sig
        self.state = state
        try:
            self.on_change(dict(state))
        except Exception as e:
            _log(f"on_change raised: {type(e).__name__}: {e}")

    def _fetch(self):
        try:
            d = _http_json("/json/v2", timeout=0.4)
            return d if isinstance(d, dict) and "error" not in d else None
        except Exception:
            return None

    def _parse(self, snap):
        bm = snap.get("beatmap") if isinstance(snap.get("beatmap"), dict) else {}
        acs, rate, src = _snap_mods(snap)
        stars = _num(_g(bm, "stats", "stars", "total")) or _num(_g(bm, "stats", "stars", "live"))
        bid = bm.get("id")
        prof = _g(snap, "profile", "name")
        return {"tosu_ok": True, "screen": screen_of(snap), "md5": bm.get("checksum") or None,
                "beatmap_id": bid if isinstance(bid, int) and bid > 0 else None,
                "artist": bm.get("artist"), "title": bm.get("title"), "version": bm.get("version"),
                "mods": mods_display(acs), "rate": rate, "stars": round(stars, 2) if stars else None,
                "ranked": ranked_of(snap), "profile_name": prof if isinstance(prof, str) and prof else None,
                "_acs": acs, "_src": src}

    @staticmethod
    def _pred_key(md5, acs, rate):
        return f"{md5}|{''.join(sorted(acs)) or 'NM'}|{float(rate or 1.0):.2f}"

    def _prediction(self, st, snap):
        model_ts = (_pp_model_sig(), (_skill_mem.get("model") or {}).get("updated"))
        if model_ts != self._pred_model:
            self._pred_cache.clear()
            self._pred_model = model_ts
        key = self._pred_key(st["md5"], st["_acs"], st["rate"])
        if key in self._pred_cache:
            return self._pred_cache[key]
        path = _snap_path(snap, st["md5"])
        pm = _pp_model()
        if pm is not None:
            # self-learning model; attributes from its cache or tosu's calculation of the SELECTED map
            # (checksum checked before and after) - never the network on this path
            try:
                t0 = time.perf_counter()
                p = pm.predict_pp(st["md5"], st["_acs"], st["rate"], osu_path=path,
                                  beatmap_id=st.get("beatmap_id"), network=False, tosu=True)
                if p is not None:
                    p["source"] = "pp_model"
                    p["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            except Exception as e:
                _log(f"pp_model prediction failed: {type(e).__name__}: {e}")
                p = None
            if p is not None:
                if len(self._pred_cache) >= 300:
                    self._pred_cache.clear()
                self._pred_cache[key] = p
                if p.get("attr_source") == "tosu" and st.get("beatmap_id") and st.get("ranked") is not False:
                    self._server_attrs_later(pm, st, key)
                return p
        p = predict(path, st["_acs"], st["rate"], allow_current=True)
        if p is not None:
            p["source"] = "skill_model"
        if p is not None and not path:
            # no file path: tosu calculated the map selected in game. The 4 calculations take a moment, so
            # make sure the selection is still this map before trusting (and caching) the result.
            now = self._fetch()
            if not now or _g(now, "beatmap", "checksum") != st["md5"]:
                _log("prediction dropped: the selected map changed while it was being calculated")
                return None
        if p is not None:   # failures are not cached: the next hover of this map retries
            if len(self._pred_cache) >= 300:
                self._pred_cache.clear()
            self._pred_cache[key] = p
        return p

    def _results_of(self, st, snap):
        rs = snap.get("resultsScreen") if isinstance(snap.get("resultsScreen"), dict) else {}
        key = (st["md5"], rs.get("createdAt"), _g(rs, "score"), _g(rs, "accuracy"))
        if key == self._results_key:
            return self._results
        acc = _num(rs.get("accuracy"))
        if acc is not None and acc > 1.0:
            acc /= 100.0
        hits = rs.get("hits") if isinstance(rs.get("hits"), dict) else {}
        misses = _num(hits.get("0"))
        pp = _num(_g(rs, "pp", "current"))
        if pp is None and acc is not None:   # older tosu builds: calculate it ourselves
            ra, rr, _ = parse_mods(_g(rs, "mods", "name") or st["_src"], None)
            r = calc_pp(_snap_path(snap, st["md5"]), ra, rr, acc=acc, misses=int(misses or 0),
                        combo=int(_num(rs.get("maxCombo")) or 0) or None)
            pp = r.get("pp") if r else None
        gain = None
        ppd = _load_json(_dir() / "pp_data.json", {})
        top = _g(ppd, "top_plays")
        if pp is not None and isinstance(top, list) and top:
            gain = rank_gain_pp(pp, top, beatmap_id=st.get("beatmap_id"), md5=st.get("md5"),
                                total_pp=_g(ppd, "user", "pp"))
        if st.get("ranked") is False:
            gain = None   # loved / unranked: tosu still computes pp, but none of it counts
        pn = rs.get("playerName") if isinstance(rs.get("playerName"), str) else \
            rs.get("name") if isinstance(rs.get("name"), str) else None
        res = {"pp": round(pp, 1) if pp is not None else None, "acc": round(acc, 4) if acc is not None else None,
               "misses": int(misses) if misses is not None else None, "rank_gain_pp": gain,
               "ranked": st.get("ranked"), "player": pn or None, "created_at": rs.get("createdAt")}
        self._results_key, self._results = key, res
        return res

    def _server_attrs_later(self, pm, st, key):
        """The hover prediction had only tosu's attributes (its rosu-pp version rates maps differently from the
        osu! server - live check 2026-11-10: aim 2.42 vs 2.52, speed 1.75 vs 1.55 on the same map). Fetch the
        server's attributes in the background (one paced API call) and re-predict this map when they arrive."""
        now = time.time()
        # at most 20 maps an hour (paced API calls), and the window moves: maps hovered late in a session
        # still get the server's attributes (same numbers the rank-up list uses)
        self._attr_jobs = {k: t for k, t in self._attr_jobs.items() if now - t < 3600}
        if os.environ.get("OSU_COACH_NO_NETWORK") or key in self._attr_jobs or len(self._attr_jobs) >= 20:
            return
        self._attr_jobs[key] = now
        md5, acs, rate, bid = st["md5"], list(st["_acs"]), st["rate"], st.get("beatmap_id")

        def job():
            try:
                diff, r, _, _ = pm.norm_mods(acs, rate)
                a = pm.get_attrs(md5, diff, r, beatmap_id=bid, network=True)
                pm.cache().flush()
                if a and a.get("src") == "api":
                    self._pred_cache.pop(key, None)
                    self._repredict = key
            except Exception as e:
                _log(f"server attributes fetch failed: {type(e).__name__}: {e}")
        threading.Thread(target=job, name="pp_live.server_attrs", daemon=True).start()

    def _log_pre_attempt(self, cur):
        """At play start: snapshot the prediction for this attempt (the hover one when cached; else from
        pp_model's caches only - no tosu calls while playing) into pp_predictions.jsonl."""
        pm = _pp_model()
        if pm is None or not cur.get("md5") or not self.record:
            return
        try:
            acs, rate, _ = parse_mods(cur.get("mods") or "NM", cur.get("rate"))
            p = self._pred_cache.get(self._pred_key(cur["md5"], acs, rate))
            src = "hover"
            if p is None or p.get("source") != "pp_model":
                p = pm.predict_pp(cur["md5"], acs, rate, beatmap_id=cur.get("beatmap_id"), network=False,
                                  tosu=False)
                src = "play_start"
            if p:
                pm.log_pre(cur["md5"], acs, rate, p, when=cur.get("started_ts"), source=src,
                           beatmap_id=cur.get("beatmap_id"))
        except Exception as e:
            _log(f"pre-attempt log failed: {type(e).__name__}: {e}")

    @staticmethod
    def _with_prediction(res, pred):
        """results + what was predicted before the play ("You got 83pp - I predicted 74 (within range)")."""
        if not res or not pred or pred.get("pp_expected") is None:
            return res
        res = dict(res)
        res["predicted"] = {k: pred.get(k) for k in ("pp_expected", "pp_low", "pp_high", "p_pass", "source")}
        pp, lo, hi = res.get("pp"), pred.get("pp_low"), pred.get("pp_high") or pred.get("pp_good_run")
        if pp is not None:
            res["prediction_error_pp"] = round(pp - pred["pp_expected"], 1)
            if lo is not None and hi is not None:
                res["within_range"] = bool(lo <= pp <= hi)
        return res

    def _public(self, st, pred, res, pending=False):
        out = {k: v for k, v in st.items() if not k.startswith("_")}
        out["prediction"] = pred
        out["prediction_pending"] = bool(pending)
        out["results"] = res
        out["last_attempt"] = self.last_attempt
        out["attempt_seq"] = self.attempt_seq
        if pred and pred.get("stars"):
            out["stars"] = pred["stars"]   # same calculator as the skill model
        return out

    def _run(self):
        last_key, pred, pred_done, was_ok, since = None, None, False, None, 0.0
        while not self._stop.is_set():
            try:
                snap = self._fetch()
                if snap is None:
                    if was_ok is not False:
                        self._emit({"tosu_ok": False, "screen": "other", "prediction": None, "results": None,
                                    "last_attempt": self.last_attempt, "attempt_seq": self.attempt_seq})
                    was_ok = False
                    last_key, pred, pred_done = None, None, False
                    self._attempts_done(self.tracker.lost())
                    # keep polling at the normal rate while an attempt is open (a 3 s gap would lose misses)
                    self._stop.wait(self.poll if (self.tracker.cur or self.tracker.pending) else self.slow_poll)
                    continue
                was_ok = True
                self._note_state(snap)
                st = self._parse(snap)
                try:
                    done = self.tracker.feed(snap, st["screen"])
                except Exception as e:
                    done = []
                    _log(f"attempt tracker: {type(e).__name__}: {e}")
                if done:   # the emit below (every path of this loop emits) carries last_attempt/attempt_seq
                    self._attempts_done(done, emit=False)
                cur = self.tracker.cur
                if cur is not None and cur.get("started_ts") != self._pre_logged:
                    self._pre_logged = cur.get("started_ts")
                    self._log_pre_attempt(cur)
                key = (st["md5"], tuple(st["_acs"]), st["rate"])
                res = None
                now = time.monotonic()
                if key != last_key:
                    last_key, pred, pred_done, since = key, None, False, now
                elif self._repredict is not None and self._repredict == self._pred_key(*key):
                    self._repredict, pred_done = None, False      # server attributes arrived: predict again
                # no predictions while playing: no card is shown then, and it would cost 4 tosu
                # calculations right as the map starts (the song-select prediction is kept)
                if not pred_done and st["md5"] and st["screen"] in ("song_select", "results"):
                    # show the new map at once; predict only after the key was seen unchanged on every
                    # poll for `debounce` seconds (sampled densely meanwhile, so fast scrolling is skipped)
                    left = self.debounce - (now - since)
                    if left > 0:
                        self._emit(self._public(st, None, None, pending=True))
                        # re-check every 0.2 s (<= 5 tosu requests/s while scrolling); predict once the key was
                        # the same on every check for `debounce` seconds
                        self._stop.wait(DEBOUNCE_STEP_S)
                        continue
                    self._emit(self._public(st, None, None, pending=True))
                    pred, pred_done = self._prediction(st, snap), True
                if st["screen"] == "results":
                    res = self._with_prediction(self._results_of(st, snap), pred)
                self._emit(self._public(st, pred, res))
            except Exception as e:
                _log(f"watcher loop: {type(e).__name__}: {e}")
            self._stop.wait(self.poll)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv):
    if "--attempts" in argv:
        for r in read_attempts(time.time() - 86400 * 2)[-30:]:
            print(json.dumps({k: v for k, v in r.items() if k not in ("hit_errors",)}, default=str)[:700])
        return 0
    if "--watch" in argv:
        # --watch only prints; attempts are written by the pet's watcher (add --record to write them here)
        w = LiveWatcher(lambda s: print(json.dumps(s, default=str)[:600], flush=True),
                        on_attempt=lambda r: print("ATTEMPT", json.dumps(attempt_summary(r)), flush=True),
                        record="--record" in argv)
        w.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            w.stop()
        return 0
    if "--predict" in argv:
        i = argv.index("--predict")
        md5 = argv[i + 1] if len(argv) > i + 1 else None
        mods = argv[i + 2] if len(argv) > i + 2 else "NM"
        rate = float(argv[i + 3]) if len(argv) > i + 3 else None
        print(json.dumps(predict(osu_path_for_md5(md5), mods, rate), indent=1))
        return 0
    ts = tosu_status()
    print(f"tosu at {_base_url()}: " + ("reachable, game data OK" if ts["osu"] else
                                        f"running but no game data ({ts['error']})" if ts["tosu"] else
                                        f"NOT reachable ({ts['error']})"))
    print(f"live attempts: {attempts_path()} ({len(read_attempts(time.time() - 86400))} in the last 24 h)")
    m = skill_model(refresh="--refresh" in argv)
    print(f"skill model: {m.get('n')} plays with SR of {m.get('n_total')}; comfort {m.get('comfort_stars')}*")
    print(f"basis: {m.get('basis')}")
    for s, a in (m.get("acc_at") or {}).items():
        print(f"  {s}*: acc {a:.1%}  misses/100 {m.get('miss_rate_at', {}).get(s)}")
    if m.get("overall"):
        print(f"overall typical: {m['overall']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
