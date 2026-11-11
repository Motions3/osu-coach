#!/usr/bin/env python3
"""
coach_adapt.py - the adaptive training loop (CONTRACT_ADAPT.md). Standard library only.

coach_engine.py calls update() on every run. It
  1. loads training_state.json (one difficulty level per skill track); on first use the levels are
     bootstrapped by sending the whole replay history through the staircases, so day 1 is not generic,
  2. rolls over to a new training day at 04:00 local time: yesterday's free plays nudge the levels
     (max one step per track), and today's steps are generated from the levels (minutes weighted by
     each track's gap to the roadmap target and its trend; every goal is written from the level),
  3. collects today's attempts (replay summaries + live_attempts.jsonl from pp_live, deduped) and the
     pet's session_cmd.json commands,
  4. re-derives the whole session from the day-start snapshot - deterministic, so re-running never
     double-counts and a replay that lands after its live attempt simply replaces it,
  5. returns the coach_data.json keys: session, last_debrief, debriefs_today, changes, tracks, plus a
     session_plan and recommendations derived from the steps.

Files (in OSU_COACH_STATE_DIR, default next to this script): training_state.json, session.json,
session_cmd.json (written by the pet, consumed here). live_attempts.jsonl is read from the state dir and
from pp_live's folder (OSU_COACH_PP_DIR).
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import osu_coach as oc  # map parsing for live attempts (which have no replay)
except Exception:  # pragma: no cover
    oc = None
try:
    import coach_knowledge as K
except Exception:  # pragma: no cover
    K = None

STATE_V = 2               # v2: accuracy level is an OD (timing window) instead of a star rating -> re-bootstrap
DAY_START_H = 4            # a training day runs 04:00 -> 04:00 (late-night sessions belong to the evening)
ACC_GOAL = 0.95            # the player's accuracy goal for the accuracy step
DEDUPE_S = 45              # live attempt and replay of the same play: same md5, end times within this
RESTART_S = 6              # a quit within the first seconds is a restart, not an attempt
RESTART_RETRY_S = 25       # a retry (same map again) within this much play, or before 15% of the map = restart
MAX_DEBRIEFS = 30

TRACKS = {
    # unit, step, min, max, label, how the level is used
    # accuracy: the knob is the timing window. Whether 95% is reachable depends on OD and UR, not stars:
    # 95% with no misses = ~92.5% 300s = +-1.78 SD inside the 300 window (80 - 6*OD ms) -> UR <= 10*w/1.78.
    # Stars only set the map band (below the push level) so the notes themselves stay easy to hit.
    "accuracy": {"unit": "OD", "step": 0.5, "min": 2.0, "max": 10.0, "label": "Accuracy (OD at 95%)",
                 "knob": "OD (timing window) of easy maps where you go for 95%"},
    "speed": {"unit": "BPM", "step": 5, "min": 110, "max": 290, "label": "Stream practice speed",
              "knob": "effective stream BPM (map BPM x custom speed)"},
    "aim": {"unit": "px/ms", "step": 0.1, "min": 0.5, "max": 3.2, "label": "Jump speed",
            "knob": "jump velocity of the hardest jumps (px/ms at the played speed)"},
    "stamina": {"unit": "min", "step": 0.5, "min": 1.5, "max": 6.0, "label": "Full-run length",
                "knob": "drain time of full runs without a late-map collapse"},
    "reading": {"unit": "AR", "step": 0.3, "min": 7.0, "max": 10.7, "label": "Reading (approach rate)",
                "knob": "approach rate with no 'lost' misses"},
    "push": {"unit": "★", "step": 0.15, "min": 3.0, "max": 8.5, "label": "Push star level",
             "knob": "star rating you can pass with few misses"},
}
RULE = "2-pass-up/1-fail-down"   # based on Levitt's (1971) transformed up-down staircase
BLOCK = {"accuracy": "Accuracy", "speed": "Speed", "aim": "Aim", "stamina": "Stamina", "reading": "Reading",
         "push": "Push", "warmup": "Warm-up", "benchmark": "Check-up", "break": "Break", "review": "Review"}
REASON_WORD = {"A": "cursor off the circle", "E": "tapped too early", "T": "cursor on it but the tap was off-time",
               "L": "cursor elsewhere (lost the pattern)", "?": "unclear"}
CAT_WORD = {"s": "streams", "b": "bursts", "j": "jumps", "l": "slider starts", "o": "regular notes",
            "a": "notes after breaks"}


# ----------------------------------------------------------------------------
# small utils
# ----------------------------------------------------------------------------

def state_dir() -> Path:
    d = os.environ.get("OSU_COACH_STATE_DIR")
    return Path(d) if d else HERE


def pp_dir() -> Path:
    d = os.environ.get("OSU_COACH_PP_DIR")
    return Path(d) if d else HERE


def load_json(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, obj, indent=None):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False)
    try:   # unchanged: don't rewrite (OneDrive re-uploads every write)
        if path.stat().st_size == len(data.encode("utf-8")) and path.read_text("utf-8") == data:
            return
    except OSError:
        pass
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def mss(ms):
    s = max(0, int((ms or 0) // 1000))
    return f"{s // 60}:{s % 60:02d}"


def clamp(x, a, b):
    return max(a, min(b, x))


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def day_of(ts) -> str:
    return (datetime.fromtimestamp(ts) - timedelta(hours=DAY_START_H)).date().isoformat()


def day_start_ts(day: str) -> float:
    d = datetime.fromisoformat(day) + timedelta(hours=DAY_START_H)
    return d.timestamp()


def snap(track, v):
    m = TRACKS[track]
    s = m["step"]
    return round(clamp(round(v / s) * s, m["min"], m["max"]), 3)


def fmt(track, v):
    if v is None:
        return "?"
    if track == "push":
        return f"{v:.2f}".rstrip("0").rstrip(".") + "*"
    if track == "speed":
        return f"{v:.0f} BPM"
    if track == "aim":
        return f"{v:.2f} px/ms"
    if track == "stamina":
        return f"{v:.1f} min"
    if track == "reading":
        return f"AR {v:.1f}"
    if track == "accuracy":
        return f"OD {v:.1f}"
    return f"{v:g}"


def ur95(od):
    """UR that gives ~95% with no misses at this (effective) OD."""
    return max(0.0, 10 * (80 - 6 * od) / 1.78)


def od_for_ur(ur):
    """The OD at which a UR gives ~95% (inverse of ur95)."""
    return (80 - 1.78 * ur / 10) / 6


def acc_band(levels):
    """(lo, hi) star band of accuracy maps: comfortably below the push level, so the notes are easy to hit
    and the only question is timing."""
    p = levels.get("push") or 4.2
    c = clamp(p - 1.2, 2.3, 6.5)
    return round(c - 0.6, 2), round(c + 0.5, 2)


_SN_STRIP = [re.compile(r"\s*[\(\[]\s*(?:feat\.|ft\.)[^\)\]]*[\)\]]", re.I),
             re.compile(r"\s*\((?:Cut Ver\.?|TV Size|TV Ver\.?|Short Ver\.?|Game Ver\.?|Sped Up(?: & Cut Ver\.?)?)\)", re.I),
             re.compile(r"\s+(?:feat\.|ft\.)\s.*$", re.I)]


def short_name(name, n=34):
    """Song title for headlines: no artist, no [diff], no 'feat. X' / '(TV Size)' / '(Cut Ver.)'; cut at a word."""
    nm = (name or "?").split(" [")[0]
    if " - " in nm:
        nm = nm.split(" - ", 1)[1]
    full = nm
    for rx in _SN_STRIP:
        nm = rx.sub("", nm).strip()
    nm = nm or full
    if len(nm) <= n:
        return nm
    i = nm.rfind(" ", 0, n - 1)
    return (nm[:i] if i >= n * 0.5 else nm[:n - 1]).rstrip(" ,;-(") + "…"


def rate_txt(rate):
    if rate is None or abs(rate - 1) < 1e-6:
        return "1.0x"
    return f"{rate:.2f}x"


def at_rate(rate):
    """' at 0.85x' for a changed speed, '' at normal speed (no 'at 1.0x' noise in the texts)."""
    return "" if rate is None or abs(rate - 1) < 1e-6 else f" at {rate:.2f}x"


def rate_mods(rate):
    return "NM" if abs(rate - 1) < 1e-6 else ("HT" if rate < 1 else "DT")


def pct_txt(x, signed=True):
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def wmedian(pairs):
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    tot = sum(w for _, w in pairs)
    if not tot:
        return None
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def preempt(ar):
    return 1800 - 120 * ar if ar < 5 else 1200 - 150 * (ar - 5)


def ar_at(ar, rate=1.0):
    ms = preempt(ar) / rate
    return (1800 - ms) / 120 if ms > 1200 else 5 + (1200 - ms) / 150


def od_at(od, rate=1.0):
    return (80 - (80 - 6 * od) / rate) / 6


def disp_bpm(f):
    b, sb = f.get("bpm") or 0, f.get("sb") or 0
    if b > 0 and sb >= 100 and abs(sb / (2 * b) - 1) <= 0.06:
        return 2 * b
    return b


def clean_search(f):
    return re.sub(r"[=<>!\"]", " ", f"{f.get('a', '')} {f.get('t', '')} {f.get('v', '')}").replace("  ", " ").strip()


def map_name(f):
    return f"{f.get('a', '?')} - {f.get('t', '?')} [{f.get('v', '?')}]"


def _std(f):
    return bool(f) and f.get("mode") == 0 and not f.get("skip") and bool(f.get("n"))


# ----------------------------------------------------------------------------
# star ratings: pp_live's cache, live attempts (tosu), API top plays, else an estimate from map features
# ----------------------------------------------------------------------------

DIFF_MODS = ("HR", "EZ", "FL")
RATE_EXP = 0.83     # SR(rate) ~ SR(1.0) * rate^0.83 (fits rosu-pp numbers for 0.75-1.5x within ~0.1*)


def _diff_key(mods):
    acs = re.findall(r"[A-Z]{2}", str(mods or "").upper())
    return "".join(sorted(a for a in acs if a in DIFF_MODS)) or "NM"


API_SR = ".coach_api_sr.json"   # md5 -> NoMod star rating from the osu! API (only where the local file matches)


def api_sr_fill(lib, budget=2, log=None):
    """NoMod star ratings for local maps from the osu! API (/beatmaps, 50 per request, the user's own client
    credentials via osu_api.Api). Only beatmaps whose online checksum equals the local md5 are stored, so an
    edited/outdated local file never gets someone else's rating. Returns the number of requests made."""
    path = pp_dir() / API_SR
    d = load_json(path, {})
    if not isinstance(d, dict) or d.get("v") != 1:
        d = {"v": 1, "sr": {}, "miss": {}}
    sr, miss = d.setdefault("sr", {}), d.setdefault("miss", {})
    todo = {}
    for md5, f in lib.items():
        if _std(f) and f.get("bid") and md5 not in sr and md5 not in miss:
            todo.setdefault(int(f["bid"]), md5)
    if not todo or budget <= 0:
        return 0
    try:
        import osu_api
        cfg = load_json(osu_api.CONFIG_FILE, {})
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return 0
        api = osu_api.Api(cfg, budget=budget + 1)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"api_sr_fill: {e}")
        return 0
    ids = list(todo)
    n = 0
    for i in range(0, len(ids), 50):
        if n >= budget:
            break
        chunk = ids[i:i + 50]
        try:
            js = api.get("/beatmaps", [("ids[]", b) for b in chunk])
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"api_sr_fill: {e}")
            break
        n += 1
        got = set()
        for bm in (js or {}).get("beatmaps") or []:
            b = bm.get("id")
            md5 = todo.get(b)
            got.add(b)
            if md5 and bm.get("checksum") == md5 and num(bm.get("difficulty_rating")):
                sr[md5] = round(float(bm["difficulty_rating"]), 3)
            elif md5:
                miss[md5] = "checksum differs" if bm.get("checksum") else "no rating"
        for b in chunk:
            if b not in got:
                miss[todo[b]] = "not found"
    try:
        atomic_write_json(path, d)
    except Exception:
        pass
    return n


class Stars:
    """Star rating lookup. Real values where known, otherwise a feature estimate (marked est=True)."""

    PRIOR = (1.10, 0.79, 0.70, 0.185, 0.8)   # const, jv75, ln(density), AR, stream term (fitted on 30 NM top plays)

    def __init__(self, lib, extra_points=()):
        self.lib = lib
        self.known = {}     # (md5, diffmods, rate2) -> stars
        d = load_json(pp_dir() / ".coach_sr_cache.json", {})
        d = d.get("sr", d) if isinstance(d, dict) else {}
        for k, v in (d.items() if isinstance(d, dict) else ()):
            parts = str(k).split("|")
            val = num(v if not isinstance(v, dict) else (v.get("stars") or v.get("sr")))
            if len(parts) >= 3 and val and 0 < val < 15 and num(parts[-1]):
                self.known[(parts[0], _diff_key(parts[1]), round(float(parts[-1]), 2))] = val
        # sanity: one value repeated over many different maps = a calculator that ignored the map path
        cnt = Counter(self.known.values())
        if cnt and len(self.known) >= 20 and cnt.most_common(1)[0][1] > 0.3 * len(self.known):
            bad = cnt.most_common(1)[0][0]
            self.known = {k: v for k, v in self.known.items() if v != bad}
        api = load_json(pp_dir() / API_SR, {})
        for md5, val in ((api.get("sr") or {}).items() if isinstance(api, dict) else ()):
            if num(val):
                self.known.setdefault((md5, "NM", 1.0), float(val))
        for md5, mods, rate, val in extra_points:
            if md5 and num(val) and 0 < float(val) < 15:
                self.known[(md5, _diff_key(mods), round(float(rate or 1.0), 2))] = float(val)
        self.w = list(self.PRIOR)
        self._fit()

    def _x(self, f):
        sb, sf = f.get("sb") or 0, f.get("sf") or 0
        st = max(0.0, (sb - 150) / 50) * min(1.0, sf * 5) if sb >= 100 else 0.0
        return [1.0, f.get("jv75") or 0, math.log(max(0.5, f.get("den") or 1)), f.get("ar") or 8, st]

    def _fit(self):
        pts = []
        for (md5, dm, r), v in self.known.items():
            f = self.lib.get(md5)
            if _std(f) and dm == "NM" and abs(r - 1) < 1e-6:
                pts.append((self._x(f), v))
        if len(pts) < 40:
            return
        ys = [v for _, v in pts]
        if max(ys) - min(ys) < 1.5:
            return   # too narrow a range to extrapolate; keep the prior
        n = len(self.w)
        lam = 2.0   # ridge towards the prior
        A = [[sum(x[i] * x[j] for x, _ in pts) + (lam if i == j else 0) for j in range(n)] for i in range(n)]
        b = [sum(x[i] * y for x, y in pts) + lam * self.PRIOR[i] for i in range(n)]
        try:
            for i in range(n):
                piv = A[i][i]
                for j in range(i + 1, n):
                    fac = A[j][i] / piv
                    for k in range(i, n):
                        A[j][k] -= fac * A[i][k]
                    b[j] -= fac * b[i]
            w = [0.0] * n
            for i in reversed(range(n)):
                w[i] = (b[i] - sum(A[i][k] * w[k] for k in range(i + 1, n))) / A[i][i]
            self.w = w
        except ZeroDivisionError:
            pass

    def get(self, md5, mods="NM", rate=1.0):
        """-> (stars, estimated: bool) or (None, True)."""
        rate = float(rate or 1.0)
        dm = _diff_key(mods)
        v = self.known.get((md5, dm, round(rate, 2)))
        if v is not None:
            return v, False
        fac = (1.08 if "HR" in dm else 1.0) * (0.8 if "EZ" in dm else 1.0)
        base = self.known.get((md5, dm, 1.0))
        if base is not None:
            return base * rate ** RATE_EXP, False   # derived from a real value
        base = self.known.get((md5, "NM", 1.0))
        if base is not None:
            # real NoMod value, scaled for the speed (and roughly for HR/EZ, which then counts as an estimate)
            return base * fac * rate ** RATE_EXP, fac != 1.0
        f = self.lib.get(md5)
        if not _std(f):
            return None, True
        e = sum(a * b for a, b in zip(self.w, self._x(f)))
        return clamp(e * fac * rate ** RATE_EXP, 1.0, 12.0), True


# ----------------------------------------------------------------------------
# attempts (replay summaries and live attempts -> one normalised shape)
# ----------------------------------------------------------------------------

def _fatigue_parts(thc):
    """(last-third misses, expected last-third misses from the first third's per-type rates)."""
    if not thc:
        return None, None
    n0 = sum(v[0] for v in thc.values())
    m0 = sum(v[1] for v in thc.values())
    tot0 = m0 / n0 if n0 else 0
    exp = sum(v[2] * (v[1] / v[0] if v[0] >= 15 else tot0) for v in thc.values())
    return sum(v[3] for v in thc.values()), exp


def att_from_replay(s, lib, stars):
    f = lib.get(s["md5"]) if lib else None
    f = f if _std(f) else None
    rate = s.get("rate") or 1.0
    bb = s.get("bpm_b") or {}
    sn = sum(v[0] for v in bb.values())
    sm = sum(v[1] for v in bb.values())
    runs = s.get("runs") or []
    sbpm = wmedian([(r[3], r[2]) for r in runs]) if runs else wmedian([(int(k) + 5, v[0]) for k, v in bb.items()])
    j = (s.get("cats") or {}).get("j") or [0, 0]
    if f and f.get("jv75"):
        jv = f["jv75"] * rate
    else:
        vb = s.get("vel_b") or {}
        jv = None
        if vb:
            ks = sorted((float(k) + 0.125, v[0]) for k, v in vb.items())
            tot = sum(w for _, w in ks)
            c = 0
            for k, w in ks:
                c += w
                if c >= 0.75 * tot:
                    jv = k
                    break
    fl, fe = _fatigue_parts(s.get("thc"))
    sr, est = stars.get(s["md5"], s.get("mods"), rate)
    ml = [m for m in (s.get("miss_list") or [])]
    drain = (f["len"] / rate) if f else (s.get("len") or 0)
    aims = sum(1 for m in ml if m[1] in ("s", "b") and m[2] == "A")
    smiss = sum(1 for m in ml if m[1] in ("s", "b"))
    return {
        "id": f"r{s['ts']}:{s['md5'][:8]}", "ts": s["ts"], "start_ts": s["ts"] - (s.get("len") or 60),
        "source": "replay", "md5": s["md5"], "name": s.get("name") or (map_name(f) if f else "?"),
        "mods": s.get("mods") or "NM", "rate": rate, "outcome": "passed", "progress": 1.0,
        "acc": s.get("acc"), "misses": s.get("misses", 0), "n": s.get("n") or 0, "ur": s.get("ur"),
        "mean_err": s.get("mean_err"), "stars": sr, "stars_est": est,
        "drain_min": drain / 60 if drain else None, "ar": s.get("ar"), "od": s.get("od"),
        "stream": {"notes": sn, "misses": sm, "bpm": sbpm, "drift": s.get("drift"),
                   "aim_share": (aims / smiss) if smiss >= 3 else None},
        "runs": runs, "jumps": {"n": j[0], "misses": j[1], "v": jv},
        "lost": sum(1 for m in ml if len(m) > 2 and m[2] == "L"),
        "fat_last": fl, "fat_exp": fe, "miss_list": ml, "seg": s.get("seg") or [],
        "n300": s.get("n300"), "combo": int(s.get("combo") or 0),
        "cats": s.get("cats") or {},
    }


_map_cache = {}


def _classified(osu_path, rate):
    """[(time, cat, run_len, bpm_stream)] for a map (cached), or None."""
    key = (osu_path, round(rate, 3))
    if key in _map_cache:
        return _map_cache[key]
    out = None
    try:
        bm = oc.parse_osu(Path(osu_path).read_text("utf-8", "replace"))
        objs = [o for o in bm.objects]
        radius = 54.4 - 4.48 * bm.cs
        js = oc.classify(bm, objs, radius, rate)
        cmap = {"stream": "s", "burst": "b", "jump": "j", "slider": "l", "other": "o", "after break": "a"}
        out = {"objs": [(j.obj.time, cmap.get(j.category, "o"), j.run_len, j.bpm_stream, j.run_pos)
                        for j in js if j.obj.kind != "spinner"], "od": bm.od}
    except Exception:
        out = None
    _map_cache[key] = out
    return out


def att_from_live(rec, lib, stars, osu_index):
    md5 = rec.get("md5") or ""
    f = lib.get(md5) if lib else None
    f = f if _std(f) else None
    rate = num(rec.get("rate")) or 1.0
    mods = str(rec.get("mods") or "NM") or "NM"
    hits = rec.get("hits") or {}
    h = {k: int(num(hits.get(k)) or 0) for k in ("300", "100", "50", "0")}
    n = sum(h.values())
    acc = num(rec.get("acc"))
    if acc is not None and acc > 1.0:
        acc /= 100.0
    if acc is None and n:
        acc = (300 * h["300"] + 100 * h["100"] + 50 * h["50"]) / (300 * n)
    first, last = num(rec.get("first_ms")), num(rec.get("last_ms"))
    tms = num(rec.get("time_ms"))
    prog = num(rec.get("progress"))
    if prog is None and first is not None and last and tms is not None and last > first:
        prog = clamp((tms - first) / (last - first), 0, 1)
    outcome = rec.get("outcome") or "quit"
    if outcome == "passed":
        prog = 1.0
    errs = [e for e in (rec.get("hit_errors") or []) if num(e) is not None]
    ur = num(rec.get("ur"))
    if ur is None and len(errs) >= 10:
        mu = mean(errs)
        ur = math.sqrt(mean((e - mu) ** 2 for e in errs)) * 10
    mts = sorted(num(t) for t in (rec.get("miss_times_ms") or []) if num(t) is not None)
    ts = num(rec.get("ts")) or time.time()
    played_s = ((tms - (first or 0)) / 1000 / rate) if tms is not None else 60
    end = str(rec.get("end") or "")
    # a quick retry is a restart, not an attempt (missed the opening, restarting for a clean run): it is not a
    # miss of the goal. Endings that weren't his choice (game/tosu gone, pet closed, map switched under a
    # running play) are recorded but not judged.
    restart = outcome != "passed" and (played_s < RESTART_S or (
        end == "retry" and ((prog is not None and prog < 0.15) or played_s < RESTART_RETRY_S)))
    unjudged = None
    if outcome != "passed" and not restart and end in ("tosu_lost", "stopped", "map_change"):
        unjudged = {"tosu_lost": "the game or tosu went away mid-map", "stopped": "the coach was closed mid-map",
                    "map_change": "the map changed mid-play"}[end]
    # classify misses with the map's own pattern analysis (no replay => no cursor data, so causes are unknown)
    path = (osu_index or {}).get(md5)
    cl = _classified(path, rate) if (path and oc) else None
    ml, stream_notes, stream_miss, runs_bpm = [], 0, 0, []
    if cl:
        objs = cl["objs"]
        win = (200 - 10 * (cl.get("od") or 8)) * rate   # meh window in map time: a miss registers after it
        reached = tms if tms is not None else (objs[-1][0] if objs else 0)
        for o in objs:
            if o[2] >= 7 and o[0] <= reached:
                stream_notes += 1
                if o[4] == 0:
                    runs_bpm.append((round(o[3]), o[2]))
        times = [o[0] for o in objs]
        import bisect
        for t in mts:
            tt = t - win
            i = bisect.bisect_left(times, tt)
            best = None
            for k in (i - 1, i, i + 1):
                if 0 <= k < len(objs) and abs(objs[k][0] - tt) <= 400 and (best is None or abs(objs[k][0] - tt) < abs(objs[best][0] - tt)):
                    best = k
            if best is None:
                ml.append([int(t), "o", "?", 0.0])
                continue
            o = objs[best]
            ml.append([int(o[0]), o[1], "?", round(o[3], 1) if o[2] >= 3 else 0.0])
            if o[2] >= 7:
                stream_miss += 1
    else:
        ml = [[int(t), "o", "?", 0.0] for t in mts]
    # first/last third by object index, like the replay judge's "thc" (stamina track: late fade or not)
    fl = fe = None
    if cl and outcome == "passed" and len(cl["objs"]) >= 30:
        from collections import Counter
        left = Counter(m[0] for m in ml)
        objs = cl["objs"]
        nt = len(objs)
        thc = {}
        for i, o in enumerate(objs):
            part = 0 if i < nt // 3 else 1 if i >= 2 * nt // 3 else None
            if part is None:
                continue
            v = thc.setdefault(o[1], [0, 0, 0, 0])
            v[2 * part] += 1
            if left.get(int(o[0]), 0) > 0:
                v[2 * part + 1] += 1
                left[int(o[0])] -= 1
        fl, fe = _fatigue_parts(thc)
    sr = num(rec.get("stars"))
    est = False
    if not sr:
        sr, est = stars.get(md5, mods, rate)
    sbpm = wmedian(runs_bpm) if runs_bpm else ((f["sb"] * rate) if f and f.get("sb", 0) >= 100 else None)
    name = (f"{rec.get('artist') or ''} - {rec.get('title') or ''} [{rec.get('version') or ''}]"
            if rec.get("title") else (map_name(f) if f else "?"))
    return {
        "id": f"l{int(ts)}:{md5[:8]}", "ts": ts, "start_ts": ts - played_s, "source": "live", "md5": md5,
        "name": name, "mods": mods, "rate": rate, "outcome": outcome, "progress": prog if prog is not None else 0.0,
        "acc": acc, "misses": h["0"], "n": n, "ur": ur, "mean_err": mean(errs) if errs else None,
        "stars": sr, "stars_est": est, "drain_min": (f["len"] / rate / 60) if f else None,
        "ar": ar_at(f["ar"], rate) if f else None, "od": od_at(f["od"], rate) if f else None,
        "stream": {"notes": stream_notes, "misses": stream_miss, "bpm": sbpm, "drift": None, "aim_share": None},
        "runs": [], "jumps": {"n": 0, "misses": 0, "v": (f["jv75"] * rate) if f and f.get("jv75") else None},
        "lost": 0, "fat_last": fl, "fat_exp": fe, "miss_list": ml, "seg": [], "n300": h["300"],
        "cats": {}, "time_ms": tms, "played_s": played_s, "combo": int(num(rec.get("max_combo")) or 0),
        "end": end, "unjudged": unjudged,
        "restart": restart,
    }


def read_live(paths=None):
    """All live attempts from live_attempts.jsonl files (bad lines skipped)."""
    if paths is None:
        # pp_live writes OSU_COACH_ATTEMPTS_FILE (else <pp dir>/live_attempts.jsonl) and rotates to
        # live_attempts.1.jsonl at 5 MB: read the rotated generation too, or today's attempts can vanish
        paths = []
        env = os.environ.get("OSU_COACH_ATTEMPTS_FILE")
        for p in ([Path(env)] if env else []) + [d / "live_attempts.jsonl" for d in (state_dir(), pp_dir())]:
            for q in (p.with_name(p.stem + ".1" + p.suffix), p):
                if q not in paths:
                    paths.append(q)
    out, seen = [], set()
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or not r.get("md5") or num(r.get("ts")) is None:
                        continue
                    if r.get("own") is False:   # spectating / someone else's play (pp_live marks it)
                        continue
                    k = (r["md5"], round(num(r["ts"]), 3))   # two tries can end in the same second (retry)
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(r)
        except OSError:
            continue
    return out


def _play_sig(a):
    """Identity of a finished play's result (md5, misses, max combo, acc to 0.1%); None when too thin."""
    if a.get("outcome") != "passed" or a.get("acc") is None or not a.get("combo"):
        return None
    return (a["md5"], int(a.get("misses") or 0), int(a["combo"]), round(a["acc"] * 1000))


def merge_attempts(reps, lives):
    """Replays + live attempts -> one list (oldest first). A live 'passed' attempt and a replay of the same
    play (same md5, end times within DEDUPE_S) are one attempt; the replay's richer data wins."""
    out = list(reps)
    used = set()
    seen_sig = {}
    for a in reps:
        seen_sig.setdefault(_play_sig(a), []).append(a["ts"])
    for lv in sorted(lives, key=lambda x: x["ts"]):
        if lv.get("restart"):
            continue
        # watching your own replay in lazer looks exactly like playing (state 'play', your name): it
        # reproduces the earlier play's result exactly. A genuinely new play practically never does.
        sg = _play_sig(lv)
        if lv["outcome"] == "passed" and sg and any(t < lv["ts"] - DEDUPE_S for t in seen_sig.get(sg, [])):
            continue
        if sg and lv["outcome"] == "passed":
            seen_sig.setdefault(sg, []).append(lv["ts"])
        dup = None
        if lv["outcome"] == "passed":
            for i, r in enumerate(reps):
                if i not in used and r["md5"] == lv["md5"] and abs(r["ts"] - lv["ts"]) <= DEDUPE_S:
                    dup = i
                    break
        if dup is not None:
            used.add(dup)
            r = out[dup]
            r["live_id"] = lv["id"]
            if r.get("stars_est") and lv.get("stars") and not lv.get("stars_est"):
                r["stars"], r["stars_est"] = lv["stars"], False
            continue
        out.append(lv)
    out.sort(key=lambda a: a["ts"])
    return out


# ----------------------------------------------------------------------------
# judging: per track pass / close / fail, and the attempt's difficulty on that track's knob
# ----------------------------------------------------------------------------

def push_allow(a):
    return max(5, round((a.get("n") or 400) / 100))


def difficulty(track, a):
    if track == "accuracy":
        return a.get("od")
    if track == "push":
        return a.get("stars")
    if track == "speed":
        s = a.get("stream") or {}
        return s.get("bpm") if ((s.get("notes") or 0) >= 25 or a.get("outcome") != "passed") else None
    if track == "aim":
        j = a.get("jumps") or {}
        return j.get("v") if ((j.get("n") or 0) >= 20 or a["source"] == "live") else None
    if track == "stamina":
        return a.get("drain_min")
    if track == "reading":
        return a.get("ar")
    return None


def _ended(a):
    """'quit' for a live attempt he left mid-map, else 'failed' (headline/why wording only)."""
    return "quit" if a.get("outcome") == "quit" else "failed"


def judge(track, a, ctx):
    """-> (outcome 'pass'|'close'|'fail'|None, metrics, text). None = this attempt can't be judged on the track."""
    done = a["outcome"] == "passed"
    acc = a.get("acc")
    if track == "warmup":
        # nothing is judged in the warm-up: below the bar is "close" (no tilt count, no level move)
        if not done:
            return "close", {"acc": acc}, "didn't finish"
        if acc >= 0.93 and a["misses"] <= 3:
            return "pass", {"acc": acc}, f"{acc:.1%}"
        return "close", {"acc": acc}, f"{acc:.1%}"
    if track == "accuracy":
        # 95% is the whole goal; the UR that 95% needs follows from the map's OD (see ur95), so a separate
        # UR bar would only contradict it
        m = {"acc": round(acc, 4) if acc is not None else None, "ur": round(a["ur"]) if a.get("ur") else None,
             "goal": ACC_GOAL}
        if a.get("od") is not None:
            m["ur_needed"] = round(ur95(a["od"]))
        if not done:
            return "fail", m, f"{_ended(a)} at {a['progress']:.0%}"
        if acc is None:
            return None, {}, "no accuracy data"
        txt = f"{acc:.1%} (goal {ACC_GOAL:.0%})"
        if acc >= ACC_GOAL:
            return "pass", m, txt
        if acc >= ACC_GOAL - 0.02:
            return "close", m, txt
        return "fail", m, txt
    if track == "speed":
        s = a["stream"]
        if not done:   # a fail/quit on the step is a miss of the goal, however few streams were reached
            mr = (s["misses"] / s["notes"]) if s.get("notes") else None
            return "fail", {"progress": a["progress"], "stream_miss": mr}, (
                (f"{mr:.0%} stream misses, " if mr is not None and s["notes"] >= 10 else "") + f"{_ended(a)} at {a['progress']:.0%}")
        if (s.get("notes") or 0) < 25:
            return None, {}, "too few stream notes to judge"
        mr = s["misses"] / s["notes"]
        dr = s.get("drift")
        m = {"stream_miss": round(mr, 3), "drift": dr, "notes": s["notes"]}
        txt = f"{mr:.0%} stream misses" + (f", drift {dr:+.0f} ms" if dr is not None else "")
        if not done:
            return "fail", m, txt + f", {_ended(a)} at {a['progress']:.0%}"
        if mr <= 0.03 and (dr is None or dr <= 15):
            return "pass", m, txt
        if mr <= 0.06 and (dr is None or dr <= 25):
            return "close", m, txt
        return "fail", m, txt
    if track == "aim":
        j = a["jumps"]
        if not done:
            return "fail", {"progress": a["progress"]}, f"{_ended(a)} at {a['progress']:.0%}"
        if a["source"] == "live" and not j["n"]:
            # no cursor data: judge on all misses vs objects
            mr = a["misses"] / max(1, a["n"])
            m = {"miss_rate": round(mr, 3)}
            return ("pass" if mr <= 0.01 else "close" if mr <= 0.02 else "fail"), m, f"{a['misses']} misses"
        if (j.get("n") or 0) < 20:
            return None, {}, "too few jumps to judge"
        jr = j["misses"] / j["n"]
        m = {"jump_miss": round(jr, 3), "jumps": j["n"]}
        txt = f"{jr:.1%} jump misses ({j['misses']}/{j['n']})"
        return ("pass" if jr <= 0.025 else "close" if jr <= 0.05 else "fail"), m, txt
    if track == "stamina":
        if not done:
            return "fail", {"progress": a["progress"]}, f"{_ended(a)} at {a['progress']:.0%}"
        fl, fe = a.get("fat_last"), a.get("fat_exp")
        if fl is None:
            return None, {}, "no section data"
        lim = max(2.0, 1.3 * fe)
        m = {"last_third": fl, "expected": round(fe, 1)}
        txt = f"last third {fl} misses vs ~{fe:.0f} expected"
        if fl <= lim and (acc or 0) >= 0.85:
            return "pass", m, txt
        if fl <= max(3.0, 1.6 * fe):
            return "close", m, txt
        return "fail", m, txt
    if track == "reading":
        if not done:
            return "fail", {"progress": a["progress"]}, f"{_ended(a)} at {a['progress']:.0%}"
        if a["source"] == "live":
            return None, {}, "no cursor data"
        allow = max(1, round(a["n"] / 400))
        m = {"lost": a["lost"], "allow": allow}
        txt = f"{a['lost']} 'lost' misses (allowed {allow})"
        if a["lost"] <= allow and a["misses"] <= max(3, 0.015 * a["n"]):
            return "pass", m, txt
        if a["lost"] <= 2 * allow + 1:
            return "close", m, txt
        return "fail", m, txt
    if track == "push":
        N = push_allow(a)
        m = {"misses": a["misses"], "allow": N, "acc": acc}
        if not done:
            return "fail", m, f"{_ended(a)} at {a['progress']:.0%}"
        txt = f"passed with {a['misses']} misses (target <= {N})"
        if a["misses"] <= N:
            return "pass", m, txt
        if a["misses"] <= 2 * N:
            return "close", m, txt
        return "fail", m, txt
    return None, {}, ""


WHAT_PASS = {"accuracy": "95%+", "speed": "clean streams", "aim": "clean jumps", "stamina": "a full run without a late fade",
             "reading": "no lost misses", "push": "a pass"}


def stair(tr, track, d, outcome, a, metrics, txt, caps):
    """Staircase based on Levitt's (1971) transformed up-down rule (2 passes up / 1 fail down), generalised to
    attempts that were not exactly at the level: a pass clearly above the level moves it up one step at once;
    a fail clearly above the level, a fail far below it, or a pass clearly below it says nothing.
    caps: {"drop": steps left today, "up": steps left today (None = no limit)}. -> change dict or None."""
    meta = TRACKS[track]
    s = meta["step"]
    L = tr["level"]
    if d is None or outcome is None:
        return None
    near_lo, near_hi = L - 0.75 * s, L + 0.75 * s
    entry = {"ts": int(a["ts"]), "md5": a["md5"], "outcome": outcome, "level": L, "d": round(d, 3),
             "metrics": metrics}
    change = None
    nm = short_name(a["name"])
    if outcome == "pass":
        if d < near_lo:
            entry["note"] = "below level"
            tr["history"].append(entry)
            return None
        if caps.get("up") is not None and caps["up"] <= 0:
            entry["note"] = "rise capped (one step per day)"
            tr["history"].append(entry)
            return None
        if d > near_hi and not caps.get("free"):
            new = min(snap(track, d), snap(track, L + s))
            if new > L:
                change = (new, f"{WHAT_PASS[track].capitalize()} on {nm} at {fmt(track, d)}, above your {fmt(track, L)} level")
                tr["streak"] = {"pass": 0, "fail": 0}
        else:
            tr["streak"]["pass"] += 1
            tr["streak"]["fail"] = 0
            if tr["streak"]["pass"] >= 2:
                new = snap(track, L + s)
                if new > L:
                    change = (new, (f"{WHAT_PASS[track].capitalize()} twice at or above {fmt(track, L)} "
                                    f"(the last: {nm})") if caps.get("free") else
                              f"{WHAT_PASS[track].capitalize()} twice in a row at {fmt(track, L)}")
                tr["streak"] = {"pass": 0, "fail": 0}
    elif outcome == "fail":
        if d > near_hi:
            entry["note"] = "above level"
            tr["history"].append(entry)
            return None
        if d < L - 1.5 * s:
            entry["note"] = "far below level"
            tr["history"].append(entry)
            return None
        tr["streak"]["fail"] += 1
        tr["streak"]["pass"] = 0
        if tr["streak"]["fail"] < caps.get("need_fails", 1):
            entry["note"] = f"fail {tr['streak']['fail']} of {caps['need_fails']} (free play)"
        elif caps.get("drop", 1) > 0:
            new = snap(track, L - s)
            if new < L:
                change = (new, f"{txt} on {nm} at {fmt(track, d)}")
                caps["drop"] = caps.get("drop", 1) - 1
            tr["streak"] = {"pass": 0, "fail": 0}
        else:
            entry["note"] = "drop capped (one step per session)"
    else:  # close
        if not (near_lo <= d <= near_hi):
            entry["note"] = "off level"
    tr["history"].append(entry)
    if len(tr["history"]) > 200:
        del tr["history"][:-200]
    if change:
        new, why = change
        if new > L and caps.get("up") is not None:
            caps["up"] -= 1
        tr["last_change"] = {"ts": int(a["ts"]), "from": L, "to": new, "why": why}
        tr.setdefault("changes", []).append(tr["last_change"])
        del tr["changes"][:-40]
        tr["level"] = new
        return {"track": track, "from": L, "to": new, "why": why}
    return None


# ----------------------------------------------------------------------------
# tracks: init + bootstrap from history
# ----------------------------------------------------------------------------

def new_track(track, level):
    m = TRACKS[track]
    return {"level": snap(track, level), "unit": m["unit"], "step": m["step"], "min": m["min"], "max": m["max"],
            "streak": {"pass": 0, "fail": 0}, "rule": RULE, "history": [], "last_change": None}


def initial_levels(atts, ctx):
    srs = [a["stars"] for a in atts if a.get("stars")]
    push_l = (median(srs) + 0.1) if srs else (ctx.get("current_stars") or 4.0) + 0.3
    # accuracy: the OD at which the player's UR on easier maps (below the push level) gives 95%, with 5% slack
    urs = [a["ur"] for a in atts if a.get("ur") and a["outcome"] == "passed" and a.get("stars")
           and a["stars"] <= push_l]
    ur0 = median(urs) if urs else ((ctx.get("stats") or {}).get("ur") or 330)
    ars = [a["ar"] for a in atts if a.get("ar")]
    # speed / aim start at the roadmap band at or just below the player's level (a 3* player doesn't start on
    # 150 BPM streams, a 6* player doesn't spend weeks climbing to their level); 150 / 0.9 without a level
    speed, aim, stamina = 150, 0.9, 2.5
    cs = ctx.get("current_stars")
    if K is not None and isinstance(cs, (int, float)):
        bands = sorted(getattr(K, "LOW_BANDS", []) + K.BANDS, key=lambda b: b["stars"])
        band = None
        for b in bands:
            if b["stars"] <= cs + 1e-6:
                band = b
        band = band or bands[0]
        speed, aim = band.get("stream") or speed, band.get("aim") or aim
        stamina = 2.0 if cs < 3.5 else 3.0 if cs >= 5.5 else 2.5
    return {"accuracy": od_for_ur(ur0 * 1.05), "speed": speed, "aim": aim, "stamina": stamina,
            "reading": (median(ars) if ars else 9.0), "push": push_l}


def evidence_tracks(a, levels=None):
    """Tracks a free (off-plan) attempt says something about. With `levels`, a map far above the player's
    push level only counts for push (it fails for many reasons at once), accuracy only counts on maps in
    the accuracy band, speed only on real streams, stamina only on maps not much harder than accuracy maps."""
    sr = a.get("stars")
    if levels:
        pl = levels.get("push")
        if sr and pl and sr > pl:
            return ["push"]
    out = ["push"]
    if not levels or (sr and sr <= acc_band(levels)[1] + 0.3):
        out.insert(0, "accuracy")
    sb = (a["stream"].get("bpm") or 0)
    if (a["stream"].get("notes") or 0) >= 25 and (not levels or sb >= TRACKS["speed"]["min"]):
        out.append("speed")
    if (a["jumps"].get("n") or 0) >= 20:
        out.append("aim")
    if (a.get("drain_min") or 0) >= 1.5 and a.get("fat_last") is not None and (
            not levels or (sr and sr <= acc_band(levels)[1] + 0.5)):
        out.append("stamina")
    if a["source"] == "replay" and a.get("ar"):
        out.append("reading")
    return out


def day_moves(tr, day):
    """(steps up, steps down) a track already moved on `day` (session + morning moves)."""
    s = TRACKS_STEP(tr)
    up = dn = 0
    for c in tr.get("changes") or []:
        if day_of(c["ts"]) != day:
            continue
        k = int(round(abs(c["to"] - c["from"]) / s))
        if c["to"] > c["from"]:
            up += k
        else:
            dn += k
    return up, dn


def TRACKS_STEP(tr):
    return tr.get("step") or 1


def apply_evidence(tracks, atts, ctx, max_up=1, drop_per_day=1, log_levels=None):
    """Feed attempts (oldest first) through the staircases with the off-level rule. Used to bootstrap
    from history and at day rollover for yesterday's free plays. Per track and day: at most `max_up` steps up
    and `drop_per_day` steps down, counting moves already made in that day's session. Free plays below the
    level count for nothing; a free play far above the level counts only as a pass; a map above the push
    level only counts for push."""
    changes = []
    by_day = {}
    seen_n = Counter()           # plays of each map so far: a map he has played 3+ times is partly memorised,
    for a in atts:               # so a free pass there says more about the map than about the level
        by_day.setdefault(day_of(a["ts"]), []).append(a)
    prior = {}
    for a in sorted(atts, key=lambda x: x["ts"]):
        prior[a["id"]] = seen_n[a["md5"]]
        seen_n[a["md5"]] += 1
    for day in sorted(by_day):
        # the same map played 5 times in a day is one piece of evidence, not five (his last try of it counts)
        last_of = {}
        for a in by_day[day]:
            if not a.get("restart") and not a.get("unjudged"):
                last_of[a["md5"]] = a["id"]
        by_day[day] = [a for a in by_day[day] if last_of.get(a["md5"]) == a["id"]]
        caps = {}
        for t in TRACKS:
            up, dn = day_moves(tracks[t], day)
            # free plays are noisy (not warmed up, not the step's focus): one fail is not enough to drop a level,
            # two at or near the level on the same day are - the mirror of the 2-pass rule going up
            caps[t] = {"drop": max(0, drop_per_day - dn), "up": None if max_up is None else max(0, max_up - up),
                       "need_fails": 2, "free": True}
            tracks[t]["streak"]["fail"] = 0
        for a in by_day[day]:
            if a.get("restart") or a.get("unjudged"):
                continue
            levels = {t: tracks[t]["level"] for t in TRACKS}
            for t in evidence_tracks(a, levels):
                d = difficulty(t, a)
                o, m, txt = judge(t, a, ctx)
                if o is None or d is None:
                    continue
                if t == "push" and o == "fail" and a["outcome"] == "passed" and d > tracks[t]["level"]:
                    continue
                if o == "pass" and prior.get(a["id"], 0) >= 3:
                    continue
                ch = stair(tracks[t], t, d, o, a, m, txt, caps[t])
                if ch:
                    ch["day"] = day
                    changes.append(ch)
        if log_levels is not None:
            for t in TRACKS:
                lg = log_levels.setdefault(t, [])
                if lg and lg[-1][0] == day:
                    lg[-1][1] = tracks[t]["level"]
                else:
                    lg.append([day, tracks[t]["level"]])
    return changes


def bootstrap(atts, ctx):
    tracks = {t: new_track(t, v) for t, v in initial_levels(atts[:40], ctx).items()}
    log = {}
    ch = apply_evidence(tracks, atts, ctx, max_up=1, log_levels=log)
    return tracks, log, ch


def trend_of(log, track, today):
    lg = (log or {}).get(track) or []
    if len(lg) < 2:
        return "flat"
    cutoff = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    old = [v for d, v in lg if d <= cutoff]
    ref = old[-1] if old else lg[0][1]
    cur = lg[-1][1]
    s = TRACKS[track]["step"]
    if cur - ref >= s - 1e-9:
        return "up"
    if ref - cur >= s - 1e-9:
        return "down"
    return "flat"


# ----------------------------------------------------------------------------
# picks: concrete local maps at a track's level
# ----------------------------------------------------------------------------

class Picker:
    def __init__(self, lib, stars, history, levels, ctx, seed):
        self.lib, self.stars, self.history, self.L, self.ctx = lib, stars, history, levels, ctx
        self.seed = seed
        self.pool = [(md5, f) for md5, f in lib.items()
                     if _std(f) and f["n"] >= 100 and 45 <= f["len"] <= 360 and f["bpm"] > 0]
        self.typ_den = ctx.get("typ_den") or 3.5
        self.typ_ar = (ctx.get("stats") or {}).get("typical_ar") or 9.0

    def h(self, md5):
        return self.history.get(md5) or {"plays": 0, "best": None}

    def _stream_ok(self, f, r, cap):
        sb = f.get("sb") or 0
        has = sb >= 100 and ((f.get("sf") or 0) >= 0.02 or (f.get("sc") or 0) >= 2)
        return not has or sb * r <= cap

    def cands(self, track, level, exclude=(), salt=""):
        """-> list of {md5, rate, d, stars, est, score} best first."""
        res = self._cands(track, level, exclude, salt, relaxed=False)
        if not res and track == "warmup":
            res = self._cands(track, level, exclude, salt, relaxed=True)
        if not res and track == "warmup":
            res = self._cands(track, level, exclude, salt, relaxed=2)
        return res

    def _cands(self, track, level, exclude, salt, relaxed):
        rng = random.Random(f"{self.seed}|{track}|{level}|{salt}")
        L = self.L
        out = []
        for md5, f in self.pool:
            if md5 in exclude:
                continue
            h = self.h(md5)
            best = h["best"]
            nov = 0.3 if 0 < h["plays"] <= 5 else 0.15 if h["plays"] == 0 else -0.3
            jit = rng.random() * 0.5
            if track in ("accuracy", "warmup", "push", "reading", "stamina"):
                sr, est = self.stars.get(md5, "NM", 1.0)
                if sr is None:
                    continue
            if track == "accuracy":
                # the knob is the OD as played: NoMod where the map's OD fits, else slightly slower (0.85-0.95x
                # widens the timing window in real time) - never a big star drop
                if not (60 <= f["len"] <= 240) or (best is not None and best >= 0.985):
                    continue
                lo, hi = acc_band(L)
                r = next((x for x in (1.0, 0.95, 0.9, 0.85) if abs(od_at(f["od"], x) - level) <= 0.35), None)
                if r is None:
                    continue
                if r != 1.0:
                    sr, est = self.stars.get(md5, rate_mods(r), r)
                    if sr is None:
                        continue
                if not (lo <= sr <= hi) or not self._stream_ok(f, r, L["speed"] - 5):
                    continue
                odp = od_at(f["od"], r)
                sc = (3 - abs(odp - level) * 2 - (1 - r) * 6 + nov + jit + (0.4 if best and 0.88 <= best < 0.95 else 0)
                      - (0.3 if est else 0) - abs(sr - (lo + hi) / 2) * 0.5)
                out.append({"md5": md5, "rate": r, "d": round(odp, 2), "stars": sr, "est": est, "score": sc})
            elif track == "warmup":
                if not (60 <= f["len"] <= (240 if relaxed == 2 else 170)) or not self._stream_ok(
                        f, 1.0, L["speed"] - (0 if relaxed == 2 else 15)):
                    continue
                # easy but not foreign: no Easy/Normal diffs with AR 4 for an AR 9 player
                lo, ar_min = ((level - 1.5, self.typ_ar - 3.0) if relaxed == 2 else
                              (level - 1.3, self.typ_ar - 2.5) if relaxed else (level - 0.8, self.typ_ar - 1.5))
                hi = level + (0.5 if relaxed == 2 else -0.1)
                if not (lo <= sr <= hi) or f["ar"] < ar_min or f["od"] < (3 if relaxed == 2 else 4):
                    continue
                sc = 2 + (0.6 if best and best >= 0.9 else 0) - abs(sr - (level - 0.4)) + jit
                out.append({"md5": md5, "rate": 1.0, "d": sr, "stars": sr, "est": est, "score": sc})
            elif track == "push":
                if f["len"] > 240 or (best is not None and best >= 0.95) or h["plays"] > 8:
                    continue
                dist = sr - level
                if not (-0.25 <= dist <= 0.35):
                    continue
                sc = 3 - abs(dist - 0.05) * 4 + nov + jit - (0.3 if est else 0)
                out.append({"md5": md5, "rate": 1.0, "d": sr, "stars": sr, "est": est, "score": sc})
            elif track == "speed":
                sb, sf, sl, sc_ = f.get("sb") or 0, f.get("sf") or 0, f.get("sl") or 0, f.get("sc") or 0
                if sb < 100 or not (sf >= 0.08 or (sl >= 16 and sc_ >= 3)) or sl > 96 or sf > 0.65 or f["len"] > 300:
                    continue
                r = 1.0 if abs(sb - level) <= 2 else round(level / sb, 2)
                if not (0.75 <= r <= 1.3):
                    continue
                if (f.get("jv75") or 0) * r > L["aim"] * 1.2 or ar_at(f["ar"], r) > 10.4 or ar_at(f["ar"], r) < 7:
                    continue
                sr, est = self.stars.get(md5, rate_mods(r), r)
                if sr and sr > L["push"] + 0.3:
                    continue
                sc = 2 + min(sf, 0.4) * 5 + min(sl, 48) / 48 - abs(r - 1) * 2.5 + nov + jit
                out.append({"md5": md5, "rate": r, "d": sb * r, "stars": sr, "est": est, "score": sc})
            elif track == "aim":
                jf, jv = f.get("jf") or 0, f.get("jv75") or 0
                if jf < 0.25 or jv <= 0 or f["n"] * jf < 40 or f["len"] > 300:
                    continue
                r = 1.0 if abs(jv - level) <= 0.04 else round(level / jv, 2)
                if not (0.8 <= r <= 1.25):
                    continue
                if not self._stream_ok(f, r, L["speed"]) or ar_at(f["ar"], r) > 10.4 or ar_at(f["ar"], r) < 7:
                    continue
                if best is not None and best >= 0.97:
                    continue
                sr, est = self.stars.get(md5, rate_mods(r), r)
                if sr and sr > L["push"] + 0.3:
                    continue
                sc = 2 + min(jf, 0.6) * 3 - abs(r - 1) * 2 + nov + jit
                out.append({"md5": md5, "rate": r, "d": jv * r, "stars": sr, "est": est, "score": sc})
            elif track == "stamina":
                mins = f["len"] / 60
                if not (level - 0.35 <= mins <= level + 0.6) or f["den"] < 0.85 * self.typ_den:
                    continue
                if not self._stream_ok(f, 1.0, L["speed"] - 5) or (f.get("jv75") or 0) > L["aim"] * 1.05:
                    continue
                if sr > acc_band(L)[1] + 0.7 or (best is not None and best >= 0.97):
                    continue
                sc = 2 + min(f["sf"] + f["bf"], 0.3) * 3 - abs(mins - level) + nov + jit
                out.append({"md5": md5, "rate": 1.0, "d": mins, "stars": sr, "est": est, "score": sc})
            elif track == "reading":
                # the knob is the approach rate as played: NoMod where the map's AR fits, otherwise a small speed-up
                # (1.05-1.25x) of a lower-AR map, which raises the AR without a big star jump
                if best is not None and best >= 0.97:
                    continue
                r = next((x for x in (1.0, 1.05, 1.1, 1.15, 1.2, 1.25)
                          if level - 0.2 <= ar_at(f["ar"], x) <= level + 0.35), None)
                if r is None:
                    continue
                if r != 1.0:
                    sr, est = self.stars.get(md5, rate_mods(r), r)
                    if sr is None:
                        continue
                if sr > max(acc_band(L)[1] + 0.6, L["push"] - 0.5):
                    continue
                if (f.get("jv75") or 0) * r > L["aim"] * 1.05 or not self._stream_ok(f, r, L["speed"] - 5):
                    continue
                arp = ar_at(f["ar"], r)
                sc = 2 - abs(arp - level) * 2 - (r - 1) * 2 + nov + jit
                out.append({"md5": md5, "rate": r, "d": round(arp, 2), "stars": sr, "est": est, "score": sc})
        out.sort(key=lambda c: -c["score"])
        # one difficulty per song
        res, songs = [], set()
        for c in out:
            f = self.lib[c["md5"]]
            k = (f["a"].lower(), f["t"].lower())
            if k in songs:
                continue
            songs.add(k)
            res.append(c)
            if len(res) >= 4:
                break
        return res


def rec_for(lib, c, track, level, ctx, L):
    """A recommendation object (CONTRACT.md shape) for a pick."""
    f = lib.get(c["md5"]) or {}
    r = c["rate"]
    sr = c.get("stars")
    st = (f"{'~' if c.get('est') else ''}{sr:.2f}*" if sr else "")
    sb, sl = f.get("sb") or 0, f.get("sl") or 0
    if track == "speed":
        reason = (f"{sb} BPM streams (longest {sl} notes, {f.get('sf', 0):.0%} of the map)"
                  + (f"; at {r:.2f}x that's {sb * r:.0f} BPM" if abs(r - 1) > 1e-6 else "")
                  + f" = your stream level {fmt('speed', level)}.")
        goal = (f"Streams at {fmt('speed', level)}{at_rate(r)}: almost no stream misses, don't fall behind the beat. "
                f"Two clean runs: {fmt('speed', level + 5)}.")
    elif track == "aim":
        jv = f.get("jv75") or 0
        reason = (f"Jump map ({f.get('jf', 0):.0%} jumps); hardest jumps {jv:.2f} px/ms"
                  + (f", at {r:.2f}x {jv * r:.2f}" if abs(r - 1) > 1e-6 else "") + f" = your jump level.")
        goal = "Almost no jump misses: land on the centre and stop before tapping. Two clean runs: faster jumps."
    elif track == "accuracy":
        odp = od_at(f.get("od", 0), r)
        reason = (f"{st}, OD {odp:.1f}{' at ' + rate_txt(r) if abs(r - 1) > 1e-6 else ''} (your accuracy level "
                  f"{fmt('accuracy', level)}): easy notes, so it's all timing. 95% here needs UR ~{ur95(odp):.0f}.")
        goal = f"{ACC_GOAL:.0%}+. Two in a row: a tighter timing window. Miss it: a bit wider."
    elif track == "push":
        reason = f"{st} - your push level is {fmt('push', level)}."
        goal = "Pass with 5 misses or fewer (a few more on long maps); max 3 tries, then the next map."
    elif track == "stamina":
        reason = f"{f.get('len', 0) / 60:.1f} min of drain at {f.get('den', 0):.1f} notes/s, nothing above your stream or jump level."
        goal = "Full run: no more misses in the last third than in the first."
    elif track == "reading":
        reason = f"AR {f.get('ar', 0):g} (your reading level {fmt('reading', level)}), easy otherwise ({st})."
        goal = "No 'lost' misses: read 2-3 notes ahead, eyes on the next note, not the cursor."
    else:  # warmup / benchmark
        reason = f"Easy start ({st}, {f.get('den', 0):.1f} notes/s)." if track == "warmup" else f"Weekly check-up map ({st})."
        goal = ("Aim for 93% with relaxed hands - nothing is judged." if track == "warmup" else
                "Up to 2 normal tries - the better one is recorded. Same map every week.")
    h = ctx.get("history", {}).get(c["md5"]) or {}
    if h.get("plays"):
        reason += f" Your best: {h['best']:.1%} in {h['plays']} play{'s' if h['plays'] > 1 else ''}."
    else:
        reason += " New to you."
    return {"id": c["md5"], "purpose": track if track in ("accuracy", "speed", "aim", "stamina", "reading", "push")
            else "warmup", "artist": f.get("a", ""), "title": f.get("t", ""), "version": f.get("v", ""),
            "creator": f.get("c", ""), "beatmap_id": f.get("bid"), "set_id": f.get("sid"),
            "mods": rate_mods(r), "rate": r, "bpm": round(disp_bpm(f) * r) if f else None,
            "ar": round(ar_at(f["ar"], r), 1) if f else None, "od": round(od_at(f["od"], r), 1) if f else None,
            "cs": f.get("cs"), "length_s": round(f["len"] / r) if f else None,
            "stars": round(sr, 2) if sr else None, "stars_estimated": bool(c.get("est")),
            "reason": reason, "goal": goal, "search": clean_search(f), "track": track, "level": level}


# ----------------------------------------------------------------------------
# plan: steps from levels
# ----------------------------------------------------------------------------

def targets(ctx):
    cs = ctx.get("current_stars") or 4.0
    band = None
    if K is not None:
        for b in K.BANDS:
            if b["stars"] >= cs + 0.25:
                band = b
                break
    band = band or {"stars": cs + 0.5, "stream": 160, "aim": 1.05}
    bs = band["stars"]
    return {"band": bs, "accuracy": clamp(5.0 + (bs - 4.5) * 1.5, 4.5, 8.5), "speed": band.get("stream") or 160,
            "aim": band.get("aim") or 1.05,
            "stamina": 3.0 if bs < 5.5 else 4.0, "reading": min(10.3, 8.7 + (bs - 4) * 0.6), "push": bs + 0.3}


def plan_weights(tracks, log, today, ctx, prev_sess):
    tg = targets(ctx)
    w, why = {}, {}
    fmap = {"ur": "accuracy", "level": "accuracy", "offset": "accuracy", "streams": "speed", "alternate": "speed",
            "aim": "aim", "jumps": "aim", "stamina": "stamina", "reading": "reading"}
    focus_tracks = {fmap[f] for f in (ctx.get("focus_ids") or [])[:2] if f in fmap}
    rates = {}
    for st in (prev_sess or {}).get("steps") or []:
        if st.get("track") in TRACKS and st.get("attempts"):
            rates[st["track"]] = st.get("passes", 0) / st["attempts"]
    for t in TRACKS:
        s = TRACKS[t]["step"]
        gap = (tg[t] - tracks[t]["level"]) / s
        tr = trend_of(log, t, today)
        x = 1.0 + clamp(gap, -2, 8) * 0.18
        reasons = []
        if gap >= 3:
            reasons.append(f"{fmt(t, tracks[t]['level'])} is {gap:.0f} steps from the {tg['band']:g}* checkpoint ({fmt(t, tg[t])})")
        elif gap <= 0:
            reasons.append(f"already at the {tg['band']:g}* checkpoint - maintenance")
        if tr == "down":
            x *= 1.35
            reasons.append("slipped this week")
        elif tr == "up":
            x *= 0.8
            reasons.append("improving - lighter maintenance")
        if t in rates and rates[t] < 0.3:
            x *= 1.15
            reasons.append("none of yesterday's tries passed" if rates[t] == 0 else
                           f"only {rates[t]:.0%} of yesterday's tries passed")
        if t in focus_tracks:
            x *= 1.25
            reasons.append("one of your top focus areas")
        w[t] = max(0.3, x)
        why[t] = reasons
    return w, why


def goal_text(track, l):
    """The step goal in plain words, from the level (never fixed boilerplate)."""
    s = TRACKS[track]["step"]
    if track == "stamina":
        return f"Full {l:.1f}-min runs: no more misses in the last third than in the first."
    if track == "accuracy":
        return (f"95%+ on easy maps at OD {l:.1f} (95% there needs UR ~{ur95(l):.0f}). Two in a row: OD {l + s:.1f}. "
                f"Miss it: OD {l - s:.1f}.")
    if track == "speed":
        return (f"Streams at {l:.0f} BPM: almost no stream misses, don't fall behind the beat. Two clean runs: "
                f"{l + s:.0f} BPM. Rest 30-60 s between tries.")
    if track == "aim":
        return f"Jumps at {l:.2f} px/ms: almost no jump misses, land on the centre. Two clean runs -> {l + s:.2f} px/ms."
    if track == "reading":
        return f"AR {l:.1f}: read 2-3 notes ahead, no 'lost' misses. Two clean runs: AR {l + s:.1f}."
    if track == "push":
        return (f"Pass a {l:.2f}* map with 5 misses or fewer (a few more on long maps); 3 tries per map. Two clean "
                f"passes (or one on a {l + 0.12:.2f}*+ map): {l + s:.2f}*.")
    return ""


def build_steps(tracks, log, today, ctx, picker, bench_due, bench_maps, prev_sess):
    w, why = plan_weights(tracks, log, today, ctx, prev_sess)
    ex = ctx.get("ex") or {}
    ordn = datetime.fromisoformat(today).toordinal()
    inc = ["accuracy", "speed", "aim"]
    if ex.get("fatigue_flag") or trend_of(log, "stamina", today) == "down" or ordn % 2 == 0:
        inc.insert(0, "stamina")
    if ex.get("reading_flag") or ordn % 2 == 1:
        inc.append("reading")
    work = 44 if bench_due else 52
    push_min = 8
    rest = work - push_min
    tot_w = sum(w[t] for t in inc)
    mins = {t: int(clamp(round(rest * w[t] / tot_w), 6, 18)) for t in inc}
    diff = rest - sum(mins.values())
    order_by_w = sorted(inc, key=lambda t: -w[t])
    i = 0
    while diff != 0 and i < 50:
        t = order_by_w[i % len(order_by_w)]
        if diff > 0 and mins[t] < 18:
            mins[t] += 1; diff -= 1
        elif diff < 0 and mins[t] > 6:
            mins[t] -= 1; diff += 1
        i += 1
    mins["push"] = push_min
    steps = []
    used = set()
    L = {t: tracks[t]["level"] for t in TRACKS}

    def add(track, block, minutes, level, goal, exit_rule, cands=None, extra=None):
        st = {"id": len(steps), "block": block, "track": track, "minutes": minutes, "state": "pending",
              "attempts": 0, "passes": 0, "level": level, "goal": goal, "exit_rule": exit_rule,
              "cands": cands or [], "pick_n": 0, "pick_md5": None, "rate": None, "mods": None,
              "level_ups": 0, "tries_on_pick": 0, "started": None, "ended": None, "why": why.get(track, [])}
        if extra:
            st.update(extra)
        if st["cands"]:
            c = st["cands"][0]
            st["pick_md5"], st["rate"], st["mods"] = c["md5"], c["rate"], rate_mods(c["rate"])
            used.update(x["md5"] for x in st["cands"])
        steps.append(st)
        return st

    wlv = round(sum(acc_band(L)) / 2, 2)
    wc = picker.cands("warmup", wlv, used)
    add("warmup", "Warm-up", 8, round(wlv - 0.4, 2),
        f"Two easy maps to loosen up (~{wlv - 0.4:.1f}*). Aim for 93%, relaxed grip - nothing is judged.",
        "2 plays or 8 min", wc)
    if bench_due and bench_maps:
        add("benchmark", "Check-up (weekly)", 14, None,
            f"Same {len(bench_maps)} maps every week, up to 2 normal tries each (the better one is recorded), so we "
            "can see your progress.", f"{len(bench_maps)} maps, up to 2 tries each",
            [{"md5": b["md5"], "rate": b["rate"], "track": b["track"]} for b in bench_maps])
    goals = {t: (lambda l, t=t: goal_text(t, l)) for t in TRACKS}
    order = [t for t in ("stamina", "accuracy", "speed") if t in inc]
    for t in order:
        add(t, BLOCK[t], mins[t], L[t], goals[t](L[t]), f"{mins[t]} min or 6 tries (2 passes = level up, keep going)",
            picker.cands(t, L[t], used))
    add("break", "Break (5 min)", 5, None, "Stand up, hands loose, look away from the screen. Stop for today if your "
        "wrist or hand hurts, tingles or feels numb.", "5 min")
    for t in [t for t in ("aim", "reading") if t in inc]:
        add(t, BLOCK[t], mins[t], L[t], goals[t](L[t]), f"{mins[t]} min or 6 tries (2 passes = level up, keep going)",
            picker.cands(t, L[t], used))
    add("push", "Push", mins["push"], L["push"], goals["push"](L["push"]), f"{mins['push']} min, 3 tries per map",
        picker.cands("push", L["push"], used))
    add("review", "Review", 2, None, "Read today's debriefs and what changed; one focus for tomorrow.", "-")
    # drop map steps with no local maps at that level (say so in the goal of the review)
    missing = [s["block"] for s in steps if s["track"] in TRACKS and not s["cands"]]
    steps = [s for s in steps if not (s["track"] in TRACKS and not s["cands"])]
    for i, s in enumerate(steps):
        s["id"] = i
    if missing:
        want = []
        for b in missing:
            t = next((k for k, v in BLOCK.items() if v == b), None)
            if t == "reading":
                want.append(f"2-3 maps around {sum(acc_band(L)) / 2 + 0.6:.0f}* with AR {L['reading']:.1f}-"
                            f"{L['reading'] + 0.3:.1f} for the Reading step")
            elif t in TRACKS:
                want.append(f"2-3 maps for the {b} step ({fmt(t, L[t])})")
        steps[-1]["goal"] += " (Download " + "; ".join(want) + ".)"
    return steps


def pick_benchmark(picker, tracks):
    L = {t: tracks[t]["level"] for t in TRACKS}
    out, used = [], set()
    for t, lvl in (("accuracy", L["accuracy"]), ("speed", L["speed"]), ("aim", L["aim"])):
        cs = picker.cands(t, lvl, used, salt="benchmark")
        if cs:
            used.add(cs[0]["md5"])
            out.append({"track": t, "md5": cs[0]["md5"], "rate": cs[0]["rate"], "level_then": lvl})
    return out


# ----------------------------------------------------------------------------
# observations / debriefs
# ----------------------------------------------------------------------------

def bench_metric(track, a, metrics):
    """The check-up number that matters for the map's track (week to week)."""
    m = {"acc": round(a["acc"], 4) if a.get("acc") is not None else None, "ur": round(a["ur"]) if a.get("ur") else None,
         "passed": a["outcome"] == "passed"}
    if track == "speed":
        s = a.get("stream") or {}
        m["stream_miss"] = round(s["misses"] / s["notes"], 4) if s.get("notes") else None
        m["drift"] = s.get("drift")
    elif track == "aim":
        j = a.get("jumps") or {}
        m["jump_miss"] = round(j["misses"] / j["n"], 4) if j.get("n") else None
    return m


def bench_better(track, x, y):
    """True when check-up result x is better than y."""
    if not y:
        return True
    if not x:
        return False
    if x.get("passed") != y.get("passed"):
        return bool(x.get("passed"))
    key = {"speed": "stream_miss", "aim": "jump_miss"}.get(track)
    if key and x.get(key) is not None and y.get(key) is not None and x[key] != y[key]:
        return x[key] < y[key]
    return (x.get("acc") or 0) > (y.get("acc") or 0)


def bench_compare_text(track, now, old, day):
    when = f"on {day}" if day else "last time"
    key = {"speed": ("stream_miss", "stream misses"), "aim": ("jump_miss", "jump misses")}.get(track)
    bits = []
    if key and now.get(key[0]) is not None and old.get(key[0]) is not None:
        bits.append(f"{key[1]} {now[key[0]]:.1%} vs {old[key[0]]:.1%}")
    if now.get("acc") is not None and old.get("acc") is not None:
        bits.append(f"accuracy {now['acc']:.1%} vs {old['acc']:.1%}")
    if track == "speed" and now.get("drift") is not None and old.get("drift") is not None:
        bits.append(f"late drift {now['drift']:+.0f} vs {old['drift']:+.0f} ms")
    better = bench_better(track, now, old)
    return (f"Check-up vs {when}: " + ", ".join(bits) + (" - better" if better else " - not better yet")) if bits else         f"Check-up vs {when}: recorded"


def clusters(ml, gap=4000):
    out, cur = [], []
    for m in sorted(ml) + [None]:
        if m is not None and (not cur or m[0] - cur[-1][0] <= gap):
            cur.append(m)
            continue
        if cur:
            out.append(cur)
        cur = [m] if m is not None else []
    return out


def section_obs(a):
    """Miss clusters with m:ss and pattern type."""
    ml = a.get("miss_list") or []
    tot = len(ml)
    if not tot:
        return []
    obs = []
    cl = sorted(clusters(ml), key=lambda c: -len(c))
    for c in cl[:2]:
        if len(c) < 3 and not (len(c) >= 2 and len(c) >= tot * 0.5):
            continue
        cats = Counter(m[1] for m in c)
        rs = Counter(m[2] for m in c if m[2] != "?")
        cat, cn = cats.most_common(1)[0]
        what = CAT_WORD.get(cat, "notes")
        bpm = [m[3] for m in c if m[1] in ("s", "b") and m[3]]
        if bpm and cat in ("s", "b"):
            what += f" at ~{median(bpm):.0f} BPM"
        elif cat == "j" and any(m[3] for m in c):
            what += f" (~{median([m[3] for m in c if m[3]]):.2f} px/ms)"
        cause = ""
        if rs:
            rc, rn = rs.most_common(1)[0]
            if rn >= len(c) * 0.6:
                cause = f", {REASON_WORD[rc]}" + (f" ({rn} of {len(c)})" if rn != len(c) else "")
        span = f"{mss(c[0][0])}-{mss(c[-1][0] + 1000)}"
        lead = (f"{len(c)} of {tot} misses" if len(c) != tot else
                "Both misses" if tot == 2 else f"All {tot} misses" if tot > 2 else "The miss")
        obs.append((len(c) / tot + 0.2, f"{lead} at {span}: {what}{cause}"))
    return obs


def seg_obs(a, track):
    """Timing observations from 10-s sections: where the 100s piled up, late/early stretches."""
    seg = a.get("seg") or []
    if len(seg) < 3:
        return []
    obs = []
    tot_bad = sum(g[3] for g in seg)
    cnt = sum(g[4] for g in seg)
    if cnt < 50:
        return []
    mu = sum(g[5] for g in seg) / cnt
    # worst 30 s window for non-300s
    if tot_bad >= 8:
        best = None
        for i in range(len(seg)):
            w = [g for g in seg[i:i + 3] if g[0] - seg[i][0] < 30]
            b = sum(g[3] for g in w)
            if best is None or b > best[0]:
                best = (b, w[0][0], w[-1][0] + 10, sum(g[1] for g in w))
        b, t0, t1, n = best
        share = b / tot_bad
        dens = n / max(1, (t1 - t0))
        frac = min(1.0, 30 / max(10, seg[-1][0] + 10 - seg[0][0]))   # share a 30 s window gets by chance
        if share >= max(0.3, 1.8 * frac) and b >= 5:
            obs.append((0.5 + share, f"{b} of your {tot_bad} 100s/50s came at {mss(t0 * 1000)}-{mss(t1 * 1000)}"
                        + (" (the densest part)" if dens > 1.3 * sum(g[1] for g in seg) / max(1, seg[-1][0] + 10 - seg[0][0])
                           else "")))
    # late/early drift by section
    secs = [(g[0], g[5] / g[4], g[4]) for g in seg if g[4] >= 15]
    if secs:
        worst = max(secs, key=lambda x: abs(x[1]))
        if abs(worst[1]) >= 18 and abs(worst[1] - mu) >= 10:
            obs.append((0.55, f"At {mss(worst[0] * 1000)}-{mss((worst[0] + 10) * 1000)} you hit {abs(worst[1]):.0f} ms "
                        f"{'late' if worst[1] > 0 else 'early'} on average (whole map {mu:+.0f} ms)"))
        if track == "stamina" or (len(secs) >= 6 and track in ("accuracy", "speed")):
            k = len(secs) // 3
            if k >= 2:
                def sur(part):
                    gs = [g for g in seg if g[4] >= 5 and part[0] <= g[0] <= part[1]]
                    c = sum(g[4] for g in gs)
                    if c < 30:
                        return None
                    s1 = sum(g[5] for g in gs); s2 = sum(g[6] for g in gs)
                    return math.sqrt(max(0, s2 / c - (s1 / c) ** 2)) * 10
                u1 = sur((secs[0][0], secs[k - 1][0]))
                u3 = sur((secs[-k][0], secs[-1][0]))
                if u1 and u3 and u3 > u1 * 1.2 and u3 - u1 >= 40:
                    obs.append((0.6, f"Your timing loosened late in the map: UR {u1:.0f} in the first third, {u3:.0f} in "
                                f"the last third"))
    return obs


def stream_obs(a, prev):
    runs = [r for r in (a.get("runs") or []) if r[2] >= 9]
    obs = []
    if not runs:
        return obs
    dr = [r for r in runs if r[5] is not None]
    if dr:
        w = max(dr, key=lambda r: r[5])
        if w[5] >= 12:
            last = ""
            if prev:
                pr = [r for r in (prev.get("runs") or []) if abs(r[0] - w[0]) <= 300 and r[5] is not None]
                if pr:
                    p = pr[0][5]
                    last = (f" (last try: {p:.0f} ms - better)" if p > w[5] + 4 else
                            f" (last try: {p:.0f} ms - worse)" if p < w[5] - 4 else f" (last try: {p:.0f} ms)")
            obs.append((0.75, f"You drifted {w[5]:.0f} ms late by the end of the {mss(w[0])} stream "
                        f"({w[2]} notes, {w[3]} BPM){last}"))
        elif all(r[5] <= 8 for r in dr) and len(dr) >= 2:
            obs.append((0.4, f"No late drift in your {len(dr)} long streams - the tempo held"))
    mr = [r for r in runs if r[4] >= 3]
    if mr:
        w = max(mr, key=lambda r: r[4] / r[2])
        share = (a["stream"].get("aim_share"))
        obs.append((0.7, f"{w[4]} of {w[2]} notes missed in the {mss(w[0])} stream ({w[3]} BPM)"
                    + (" - mostly the cursor sliding off the path, not the fingers" if share and share >= 0.5 else "")))
    return obs


def compare(a, prev, step_atts):
    if prev:
        vs, ref = "last attempt on this map", [prev]
    elif step_atts:
        vs, ref = "your average for this step", step_atts
    else:
        return None
    acc = [r["acc"] for r in ref if r.get("acc") is not None]
    ur = [r["ur"] for r in ref if r.get("ur")]
    mi = [r["misses"] for r in ref if r.get("misses") is not None]
    out = {"vs": vs}
    if acc and a.get("acc") is not None:
        out["acc"] = pct_txt(a["acc"] - mean(acc))
    if ur and a.get("ur"):
        out["ur"] = f"{a['ur'] - mean(ur):+.0f}"
    if mi and a.get("misses") is not None and a["outcome"] == "passed" and all(r["outcome"] == "passed" for r in ref):
        out["misses"] = f"{a['misses'] - mean(mi):+.0f}"
    return out


def general_obs(a, track, ctx, outcome):
    obs = []
    acc, ur = a.get("acc"), a.get("ur")
    if a["outcome"] != "passed":
        t = a.get("time_ms")
        where = f" at {mss(t)}" if t else ""
        obs.append((0.9, f"{'Failed' if a['outcome'] == 'failed' else 'Quit'}{where} ({a['progress']:.0%} of the map)"
                    + (f" with {a['misses']} misses so far" if a.get("misses") else "")))
    if track in ("accuracy", "warmup", None) and a["outcome"] == "passed" and acc is not None:
        if a["misses"] <= 1 and acc < ACC_GOAL - 0.03:
            obs.append((0.85, f"{a['misses']} miss{'es' if a['misses'] != 1 else ''} but {acc:.1%}: you're hitting "
                        f"{'everything' if not a['misses'] else 'nearly everything'}; the spread of your timing "
                        f"(UR {ur:.0f}) is what costs the accuracy" if ur else
                        f"{a['misses']} misses but {acc:.1%}: timing, not aim, is costing the accuracy"))
        od = a.get("od")
        if ur and od is not None and acc < ACC_GOAL and a["misses"] <= max(3, 0.01 * (a.get("n") or 0)):
            w = 80 - 6 * od           # 300 window (+- ms, real time, effective OD)
            sd = ur / 10              # UR = 10 x the standard deviation of the hit errors
            if w > 0 and sd > 0:
                out_share = 1 - math.erf(w / (sd * math.sqrt(2)))
                k = max(2, round(1 / out_share)) if out_share > 0.02 else None
                share = (f"About 1 in {k} of your hits land" if k and k <= 12 else f"About {out_share:.0%} of your hits land")
                obs.append((0.8, f"{share} outside the 300 window here (OD {od:.1f}, UR {ur:.0f}). 95% on this map "
                            f"needs UR ~{ur95(od):.0f}"))
    me = a.get("mean_err")
    if me is not None and abs(me) >= 10 and track in ("accuracy", "warmup", "stamina", None):
        obs.append((0.5, f"You hit {abs(me):.0f} ms {'late' if me > 0 else 'early'} on average - "
                    + ("tap to the music, don't wait for the approach circle to close" if me > 0
                       else "you're anticipating; let the beat come to you")))
    if track == "push" and a["outcome"] == "passed" and a.get("misses") is not None:
        obs.append((0.45, f"{a['misses']} misses, {acc:.1%}" + (f", UR {ur:.0f}" if ur else "")))
    return obs


ADVICE = {
    ("speed", "aim"): "Stream misses are mostly the cursor: follow the stream's path with the pen, don't cut corners.",
    ("speed", "drift"): "Late drift = the fingers fall behind: start relaxed, keep the alternation light, and stop the run rather than mashing.",
    ("accuracy", "late"): "Listen for the beat and tap on it; don't wait for the circle to close.",
    ("accuracy", "ur"): "Hits are landing but spread out: lighter taps, steady tempo, stop chasing the approach circle.",
    ("aim", "aim"): "Aim for the circle centre and stop on it; tablet: small wrist/finger moves, don't drag the whole arm.",
}


def tip(track, a, outcome):
    if outcome == "pass":
        return None
    if track == "speed":
        if (a["stream"].get("aim_share") or 0) >= 0.5:
            return ADVICE[("speed", "aim")]
        if (a["stream"].get("drift") or 0) > 15:
            return ADVICE[("speed", "drift")]
    if track == "accuracy":
        if (a.get("mean_err") or 0) >= 10:
            return ADVICE[("accuracy", "late")]
        return ADVICE[("accuracy", "ur")]
    if track == "aim":
        return ADVICE[("aim", "aim")]
    return None


# ----------------------------------------------------------------------------
# the session: re-derived from the day-start snapshot on every run
# ----------------------------------------------------------------------------

FREE_NOTE = "Not today's step, so it won't change your levels now."


class Derive:
    def __init__(self, st0, sess, atts, cmds, ctx, picker, lib, all_prev, now, hist_atts=()):
        self.tracks = copy.deepcopy(st0["day_start_tracks"])
        self.day_start = {t: self.tracks[t]["level"] for t in TRACKS}
        # UR baselines for the fatigue check: UR depends on the map, so compare each play with his own
        # earlier URs on the same map, else on similar maps (stars +-0.3, OD +-1)
        self.ur_hist = [(a["md5"], a.get("stars"), a.get("od"), a["ur"]) for a in hist_atts
                        if a.get("ur") and a["outcome"] == "passed"][-400:]
        self.steps = copy.deepcopy(sess["steps_initial"])
        self.pick_log = sess.setdefault("pick_log", {})
        self.atts, self.cmds, self.ctx, self.picker, self.lib = atts, cmds, ctx, picker, lib
        self.all_prev = all_prev       # attempts before today, by md5 (for "last attempt on this map")
        self.now = now
        self.cur = 0
        self.caps = {t: {"drop": 1} for t in TRACKS}
        self.debriefs = []
        self.level_changes = []
        self.counted = []
        self.free = []
        self.fails_in_row = 0
        self.tilt = False
        self.session_atts = []
        self.started = None
        self.break_taken_ts = None
        self.last_ts = None
        self.bench_results = []
        self.prev_by_md5 = {}
        self.stopped = False
        self.active_s = 0.0          # time actually spent playing today (attempts + short gaps), not wall clock
        self.sitting_i = 0           # index into session_atts where the current sitting began (30+ min gap = new)
        self._fit = None
        self.late_run = 0            # plays in a row with a mean hit error of +8 ms or more (offset check)
        if self.steps:
            self.steps[0]["state"] = "active"

    # --- picks
    def set_pick(self, st, c):
        st["pick_md5"], st["rate"], st["mods"] = c["md5"], c["rate"], rate_mods(c["rate"])
        st["tries_on_pick"] = 0

    def cands_for(self, st, level, variant="main"):
        key = f"{st['id']}|{st['track']}|{level}|{variant}"
        if key in self.pick_log:
            return self.pick_log[key]
        used = {c["md5"] for s in self.steps if s is not st for c in s.get("cands") or []}
        cs = self.picker.cands(st["track"], level, used | ({st["pick_md5"]} if variant == "next" else set()),
                               salt=variant)
        cs = [{"md5": c["md5"], "rate": c["rate"], "stars": c.get("stars"), "est": c.get("est")} for c in cs]
        self.pick_log[key] = cs
        return cs

    def same_map_rate(self, st, level):
        """For speed/aim: the current map at the rate that puts it at `level`, if within range."""
        f = self.lib.get(st["pick_md5"]) or {}
        base = (f.get("sb") if st["track"] == "speed" else f.get("jv75")) or 0
        if not base:
            return None
        r = 1.0 if abs(base - level) <= (2 if st["track"] == "speed" else 0.04) else round(level / base, 2)
        lo, hi = (0.75, 1.3) if st["track"] == "speed" else (0.8, 1.25)
        return r if lo <= r <= hi else None

    # --- step bookkeeping
    def active(self):
        return self.steps[self.cur] if 0 <= self.cur < len(self.steps) else None

    def finish(self, state, ts):
        st = self.active()
        if not st:
            return
        st["state"] = state
        st["ended"] = ts
        self.cur += 1
        while self.cur < len(self.steps) and self.steps[self.cur]["state"] in ("done", "skipped"):
            self.cur += 1
        nxt = self.active()
        if nxt:
            nxt["state"] = "active"
            if nxt["track"] == "break":
                nxt["started"] = ts

    def match_step(self, a):
        """Index of the step this attempt belongs to, else None (free play)."""
        st = self.active()
        if st and st["track"] == "break":
            return None
        order = ([self.cur] if self.cur < len(self.steps) else []) + [i for i in range(len(self.steps)) if i != self.cur]
        for i in order:
            s = self.steps[i]
            if s["state"] in ("done", "skipped") and i != self.cur:
                continue
            if s["track"] not in TRACKS and s["track"] not in ("warmup", "benchmark"):
                continue
            md5s = {s.get("pick_md5")} | {c["md5"] for c in s.get("cands") or []} | set(s.get("extra_md5") or ())
            if a["md5"] in md5s:
                if s["track"] == "benchmark" and a["md5"] in (s.get("bench_done") or []):
                    continue
                return i
        # warm-up: any map up to his push level counts while the warm-up is active (it is not judged anyway)
        if st and st["track"] == "warmup" and (not a.get("stars") or a["stars"] <= self.tracks["push"]["level"]):
            return self.cur
        return None

    def elapsed_on(self, st, ts):
        if "active_s" in st:
            return st["active_s"]
        return ts - st["started"] if st.get("started") else 0

    # --- main loop
    def run(self):
        evs = [(a["ts"], 1, a) for a in self.atts] + [(c["ts"], 0, c) for c in self.cmds]
        evs.sort(key=lambda e: (e[0], e[1]))
        for ts, kind, ev in evs:
            if kind == 0:
                self.on_cmd(ev)
            else:
                self.on_attempt(ev)
        # time-based auto advance at "now"
        st = self.active()
        if st and st["track"] == "break" and st.get("started") and self.now - st["started"] >= 300:
            self.finish("done", st["started"] + 300)
        return self

    def on_cmd(self, c):
        cmd = c.get("cmd")
        st = self.active()
        if cmd in ("done", "skip") and st:
            self.finish("done" if cmd == "done" else "skipped", c["ts"])
        elif cmd == "back":
            i = self.cur - 1
            while i >= 0 and (self.steps[i]["track"] == "review" or self.steps[i].get("note") == "jumped ahead"):
                i -= 1
            if i >= 0:
                if st:
                    st["state"] = "pending"
                self.cur = i
                self.steps[i]["state"] = "active"
                self.steps[i]["ended"] = None
        elif cmd == "restart":
            for s in self.steps:
                s.update({"state": "pending", "attempts": 0, "passes": 0, "started": None, "ended": None,
                          "level_ups": 0, "tries_on_pick": 0})
            self.cur = 0
            self.steps[0]["state"] = "active"
            self.stopped = False

    def ur_base(self, a):
        """His usual UR for a map like this: a straight-line fit of his past URs against star rating (harder
        maps = higher UR), so plays on different maps can be compared. None if unknown."""
        if self._fit is None:
            pts = [(s2, u) for _m, s2, _o, u in self.ur_hist if s2]
            self._fit = False
            if len(pts) >= 10:
                mx, my = mean(p[0] for p in pts), mean(p[1] for p in pts)
                vx = sum((p[0] - mx) ** 2 for p in pts)
                b = (sum((p[0] - mx) * (p[1] - my) for p in pts) / vx) if vx > 1e-9 else 0.0
                self._fit = (my - b * mx, clamp(b, 0.0, 120.0))
        sr = a.get("stars")
        if self._fit and sr:
            return max(80.0, self._fit[0] + self._fit[1] * sr)
        return self.ctx.get("ur_recent")

    def fatigue(self, ts):
        pl = self.tracks["push"]["level"]
        rel = []
        for x in self.session_atts[self.sitting_i:]:
            if not x.get("ur") or x["outcome"] != "passed" or x.get("_track") == "push" or                     (x.get("stars") and x["stars"] > pl):
                continue
            b = x.get("_ur_base")
            if b:
                rel.append((x["ur"] / b, x["ur"], b))
        out = {"ur_trend": None, "flag": False, "why": None}
        if len(rel) >= 6:
            f3, l3 = mean(r[0] for r in rel[:3]), mean(r[0] for r in rel[-3:])
            out["ur_trend"] = round((l3 - f3) * 100)    # % points vs his usual UR on those maps
            if l3 >= 1.15 * f3:
                out["flag"] = True
                out["why"] = (f"your last 3 plays had a UR {100 * (l3 - 1):+.0f}% vs your usual on those maps, "
                              f"at the start it was {100 * (f3 - 1):+.0f}%")
        plan_s = sum(s["minutes"] for s in self.steps) * 60
        if self.started and self.active_s > plan_s + 600:
            done = sum(1 for s in self.steps if s["state"] == "done" and s["track"] not in ("review", "break"))
            real = sum(1 for s in self.steps if s["track"] not in ("review", "break"))
            if self.active_s < 1.5 * plan_s and done < real / 2:
                out["short"] = True      # mostly free play so far: not a stop, a shorter plan
                out["why"] = out["why"] or (f"you've played {self.active_s / 60:.0f} min today (plan "
                                            f"{plan_s / 60:.0f}), most of it outside the plan")
            else:
                out["flag"] = True
                out["over_time"] = True
                out["why"] = f"you've played {self.active_s / 60:.0f} min today, the plan was {plan_s / 60:.0f}"
        return out

    def on_attempt(self, a):
        if a.get("restart"):
            return
        if self.started is None:
            self.started = a["start_ts"]
        # a gap of 4+ minutes counts as a break; 30+ minutes = a new sitting (fresh fatigue baseline)
        gap = (a["start_ts"] - self.last_ts) if self.last_ts else 0
        if self.last_ts and gap >= 240:
            self.break_taken_ts = a["start_ts"]
        if self.last_ts and gap >= 1800:
            self.sitting_i = len(self.session_atts)
        spent = max(0.0, a["ts"] - a["start_ts"]) + (gap if 0 < gap < 240 else 0)
        a["_ur_base"] = self.ur_base(a)
        if a.get("ur") and a["outcome"] == "passed":
            self.ur_hist.append((a["md5"], a.get("stars"), a.get("od"), a["ur"]))
        st = self.active()
        # warmed up on free plays: 8+ minutes of playing today means the warm-up's job is done
        if st and st["track"] == "warmup" and self.active_s >= st["minutes"] * 60 and                 self.match_step(a) != self.cur:
            st["note"] = "warmed up on your own maps"
            self.finish("done", a["start_ts"])
            st = self.active()
        self.active_s += spent
        if st and st["track"] == "break":
            gap = a["start_ts"] - (st.get("started") or a["start_ts"])
            st["break_s"] = int(max(0, gap))
            self.finish("done", a["start_ts"])
            if gap < 240:
                st["note"] = f"short break ({gap / 60:.0f} min)"
        prev = self.prev_by_md5.get(a["md5"]) or self.all_prev.get(a["md5"])
        idx = self.match_step(a)
        if idx is not None:
            self.steps[idx]["active_s"] = self.steps[idx].get("active_s", 0) + spent
        self.session_atts.append(a)
        if idx is None:
            d = self.free_debrief(a, prev)
        else:
            if idx != self.cur:
                cur = self.active()
                if cur and cur["state"] == "active":
                    if cur["attempts"]:
                        cur["state"] = "done"
                    elif idx > self.cur:
                        # he went straight to a later step's map: the step he jumped over is skipped, not
                        # left hanging (the 'back' button doesn't return to it)
                        cur["state"], cur["note"] = "skipped", "jumped ahead"
                    else:
                        cur["state"] = "pending"
                for j in range(self.cur + 1, idx):
                    if self.steps[j]["state"] == "pending" and self.steps[j]["track"] not in ("review",):
                        self.steps[j]["state"], self.steps[j]["note"] = "skipped", "jumped ahead"
                self.cur = idx
                self.steps[idx]["state"] = "active"
            d = self.step_debrief(a, prev, self.steps[idx])
        self.prev_by_md5[a["md5"]] = a
        self.last_ts = a["ts"]
        self.debriefs.append(d)

    # --- debrief builders
    def _base(self, a, st, verdict):
        return {"id": a["id"], "ts": int(a["ts"]), "source": a["source"], "map": a["name"], "md5": a["md5"],
                "mods": a["mods"], "rate": a["rate"], "block": st["block"] if st else None,
                "track": st["track"] if st else None, "verdict": verdict, "acc": round(a["acc"], 4) if a.get("acc") is not None else None,
                "misses": a.get("misses"), "ur": round(a["ur"]) if a.get("ur") else None,
                "stars": round(a["stars"], 2) if a.get("stars") else None, "stars_estimated": bool(a.get("stars_est")),
                "outcome": a["outcome"], "level_change": None}

    def observations(self, a, prev, track, outcome):
        obs = general_obs(a, track, self.ctx, outcome) + section_obs(a)
        # the mean-offset tip only means something on easy maps (on hard maps lateness is reading, not timing)
        hi = acc_band({t: self.tracks[t]["level"] for t in TRACKS})[1] + 0.3
        easy = not a.get("stars") or a["stars"] <= hi
        if not easy:
            obs = [o for o in obs if " on average - " not in o[1]]
        me = a.get("mean_err")
        if me is not None and a["outcome"] == "passed":
            if me >= 8 and a.get("_counted_late") is None:
                self.late_run += 1
                a["_counted_late"] = True
            elif me < 8:
                self.late_run = 0
            if self.late_run >= 5:
                obs = [o for o in obs if " on average - " not in o[1]]
                obs.append((0.7, f"Your last {self.late_run} plays all landed {me:.0f}+ ms late on average - that's "
                            "more likely an offset than your hands: in osu! Settings > Audio > Offset, run the "
                            "offset wizard (or lower it ~10 ms) and see if the 300s come back"))
        if track in ("speed", None, "benchmark") or (a["stream"].get("notes") or 0) >= 40:
            obs += stream_obs(a, prev)
        obs += seg_obs(a, track)
        if track == "stamina" and a.get("fat_last") is not None:
            obs.append((0.65, f"Last third: {a['fat_last']} misses where ~{a['fat_exp']:.0f} were expected from the first third"))
        obs.sort(key=lambda x: -x[0])
        out, seen = [], set()
        for _, t in obs:
            k = t[:25]
            if k in seen:
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= 3:
                break
        if not out:
            j = a.get("jumps") or {}
            if track == "aim" and j.get("n"):
                out.append(f"{j['misses']} of {j['n']} jumps missed" + (f" (hardest jumps ~{j['v']:.2f} px/ms)" if j.get("v") else ""))
            elif a.get("acc") is not None:
                out.append(f"{a['acc']:.1%}, {a['misses']} miss{'' if a['misses'] == 1 else 'es'}"
                           + (f", UR {a['ur']:.0f}" if a.get("ur") else "") + (" - no trouble spot stood out" if a["misses"] <= 3 else ""))
            else:
                out.append("No details for this attempt.")
        return out

    def free_debrief(self, a, prev):
        d = self._base(a, None, "free-play")
        self.free.append(a)
        st = self.active()
        mtxt = f"{a['misses']} miss" + ("" if a["misses"] == 1 else "es")
        res = (f"{a['acc']:.1%}" if a.get("acc") is not None else "") + (f", {mtxt}" if a["outcome"] == "passed" else
                                                                          f", {a['outcome']} at {a['progress']:.0%}")
        d["headline"] = f"Free play: {short_name(a['name'], 30)} {res}"[:80]
        d["observations"] = self.observations(a, prev, None, None)
        levels = {t: self.tracks[t]["level"] for t in TRACKS}
        # what it would have been as a step attempt (only where the map sits at that track's level)
        for t in ("accuracy", "speed", "aim", "push"):
            if t not in evidence_tracks(a, levels):
                continue
            dv, L, s_ = difficulty(t, a), self.tracks[t]["level"], TRACKS[t]["step"]
            o, _m, txt = judge(t, a, self.ctx)
            if dv is None or o is None or abs(dv - L) > 0.75 * s_:
                continue
            art = "an" if BLOCK[t][0] in "AEIOU" else "a"
            note = f"As {art} {BLOCK[t]} step ({fmt(t, L)}) this would be a {o}: {txt}"
            d["observations"] = (d["observations"][:2] + [note])
            break
        pl, sr = self.tracks["push"]["level"], a.get("stars")
        if sr and sr >= pl + 0.5:
            N = push_allow(a)
            gap = f"{sr:.1f}* is {sr - pl:.1f}* above your push level ({fmt('push', pl)})"
            gap = f"{sr:.1f}* is {sr - pl:.1f}* above your Push level ({fmt('push', pl)})"
            if a["outcome"] == "passed" and a["misses"] <= N:
                note = (f"{gap} and you passed it with {a['misses']} miss{'' if a['misses'] == 1 else 'es'} - that's "
                        f"push-level-up form; tomorrow morning it counts (one step)")
            elif a["outcome"] == "passed" and a["misses"] <= 2 * N:
                note = f"{gap}: {a['misses']} misses is close to a push pass there ({N} or fewer would count)"
            else:
                res = (f"so {a['misses']} misses is normal" if a["outcome"] == "passed" else
                       f"so {'failing' if a['outcome'] == 'failed' else 'quitting'} at {a['progress']:.0%} is normal")
                note = (f"{gap}, {res}. To level up: pass a {fmt('push', pl)}-{fmt('push', pl + 0.3)} map with "
                        f"{push_allow(dict(n=500))} misses or fewer")
            d["observations"] = [o for o in d["observations"] if not o.startswith("As a")][:2] + [note]
        else:
            # a hard-timing map played for accuracy: say plainly that 95% there isn't today's goal
            od, al = a.get("od"), self.tracks["accuracy"]["level"]
            if od is not None and od - al >= 1.5 and a["outcome"] == "passed" and (a.get("acc") or 1) < ACC_GOAL:
                acc_st = next((x for x in self.steps if x["track"] == "accuracy" and x["state"] != "skipped"), None)
                where = (f" - the Accuracy step's map ({self.next_for_step(acc_st)}) is where 95% is the goal"
                         if acc_st and acc_st.get("pick_md5") else "")
                d["observations"] = d["observations"][:2] + [
                    f"OD {od:.1f} is far above your accuracy level (OD {al:.1f}), so {a['acc']:.1%} here is no "
                    f"verdict on your accuracy{where}"]
        d["compare"] = compare(a, prev, [])
        # tilt counts misses of a goal he could reach: step attempts, and free fails up to his push level
        # (a 5.4* fail is expected and is not frustration evidence)
        if a["outcome"] != "passed" and (not sr or sr <= pl):
            self.fails_in_row += 1
        nxt = self.next_for_step(st) if st else None
        if st and st["track"] not in ("break", "review"):
            d["next_action"] = {"type": "next_map", "md5": st.get("pick_md5"), "rate": st.get("rate"),
                                "text": f"{FREE_NOTE} Back to {st['block']}: " + (nxt or "the plan"),
                                "why": "Free plays get looked at tomorrow morning."}
        elif st and st["track"] == "break":
            d["next_action"] = {"type": "break", "text": "You're on the break step: 5 minutes off, hands loose.",
                                "why": "Rest between blocks keeps the next block useful."}
        else:
            d["next_action"] = {"type": "stop", "text": "Plan's done for today - this one was a bonus.", "why": ""}
        f = self.fatigue(a["ts"])
        if f["flag"] or f.get("short"):
            d["next_action"] = self.rest_action(f, a["ts"])
        return d

    def next_for_step(self, st):
        if not st or not st.get("pick_md5"):
            return None
        f = self.lib.get(st["pick_md5"]) or {}
        return f"{short_name(map_name(f), 30)}{at_rate(st['rate'] or 1.0)}"

    def rest_action(self, f, ts):
        if f.get("short") and not f.get("flag"):
            w, _ = plan_weights(self.tracks, {}, day_of(ts), self.ctx, None)
            pend = [s for s in self.steps if s["state"] in ("pending", "active") and s["track"] in TRACKS]
            top = sorted(pend, key=lambda s: -w.get(s["track"], 0))[:2]
            names = " and ".join(s["block"] for s in top) or "one block"
            st = self.active()
            return {"type": "next_map",
                    "md5": st.get("pick_md5") if st else None, "rate": st.get("rate") if st else None,
                    "text": f"Short on time: do {names} only, then stop for today.",
                    "why": f["why"] + "; the two blocks that need it most are worth more than the whole list."}
        if f.get("over_time") or (self.break_taken_ts and ts - self.break_taken_ts < 1800):
            return {"type": "stop", "text": ("Stop for today: " + f["why"] + ". Tomorrow's plan starts from these results.")[:160],
                    "why": f["why"] + "; practice past this point mostly trains tired habits."}
        return {"type": "break", "text": "Take a 5-minute break now: stand up, shake out your hands, water.",
                "why": f["why"] + " (15%+ worse = fatigue, not skill)."}

    def unjudged_debrief(self, a, prev, st):
        """A step attempt that ended for a reason that wasn't his (game/tosu gone, map switched): recorded,
        not judged - no level move, no tilt count, no try used."""
        d = self._base(a, st, "not-counted")
        nm = short_name(a["name"], 30)
        d["headline"] = f"{nm}: not counted - {a['unjudged']}"[:80]
        d["observations"] = self.observations(a, prev, st["track"] if st["track"] in TRACKS else None, None)
        d["compare"] = None
        d["goal"] = st["goal"]
        d["next_action"] = {"type": "retry", "md5": st.get("pick_md5"), "rate": st.get("rate"),
                            "text": f"Play {self.next_for_step(st) or 'the map'} again when you're ready",
                            "why": "An attempt that was cut off doesn't say anything about your level."}
        self.counted.append(a["id"])
        return d

    def bench_debrief(self, a, prev, st):
        """Weekly check-up: a measurement, not a test - recorded, no level move, no tilt count."""
        b = next((c for c in st["cands"] if c["md5"] == a["md5"]), None)
        jtrack = b["track"] if b else "accuracy"
        tries = st.setdefault("bench_tries", {})
        tries[a["md5"]] = tries.get(a["md5"], 0) + 1
        if st.get("started") is None:
            st["started"] = a["start_ts"]
        st["attempts"] += 1
        outcome, metrics, txt = judge(jtrack, a, self.ctx)
        self.counted.append(a["id"])
        val = bench_metric(jtrack, a, metrics)
        self.bench_results.append({"md5": a["md5"], "track": jtrack, "acc": a.get("acc"), "ur": a.get("ur"),
                                   "misses": a.get("misses"), "metrics": metrics, "outcome": outcome,
                                   "metric": val, "try": tries[a["md5"]], "ts": int(a["ts"])})
        d = self._base(a, st, "done")
        order = [c["md5"] for c in st["cands"]]
        k = len([m for m in order if tries.get(m)]) or 1     # how many of the check-up maps he has played so far
        nm = short_name(a["name"], 22)
        res = (f"{a['acc']:.1%}" if a.get("acc") is not None else "") + (
            f", {a['misses']} miss{'' if a['misses'] == 1 else 'es'}" if a["outcome"] == "passed"
            else f", {_ended(a)} at {a['progress']:.0%}")
        weak = ""
        cats = Counter(m[1] for m in (a.get("miss_list") or []))
        if cats and sum(cats.values()) >= 4:
            c, n = cats.most_common(1)[0]
            if n >= 0.4 * sum(cats.values()):
                weak = f" - {CAT_WORD.get(c, 'notes')} were the weak spot"
        d["headline"] = f"Check-up {k} of {len(order)}: {nm} {res}{weak}"
        if len(d["headline"]) > 80:
            d["headline"] = f"Check-up {k} of {len(order)}: {nm} {res}"[:80]
        d["observations"] = self.observations(a, prev, jtrack, outcome)
        old = [r for r in (self.ctx.get("_bench_old") or []) if r.get("md5") == a["md5"] and r.get("metric")]
        if old and val:
            o = old[-1]
            d["observations"] = d["observations"][:2] + [bench_compare_text(jtrack, val, o["metric"], o.get("day"))]
        d["compare"] = None
        d["goal"] = st["goal"]
        d["bench"] = {"n": k, "of": len(order), "try": tries[a["md5"]]}
        newly = [m for m in order if tries.get(m, 0) >= 2 or (
            m == a["md5"] and a["outcome"] == "passed" and self._bench_good(jtrack, val))]
        st["bench_done"] = list(dict.fromkeys((st.get("bench_done") or []) + newly))
        d["next_action"] = self.next_action(a, st, jtrack, outcome, None, None)
        return d

    @staticmethod
    def _bench_good(track, val):
        """A first check-up try that is already clean needs no second one."""
        if not val:
            return False
        if track == "accuracy":
            return (val.get("acc") or 0) >= ACC_GOAL
        if track == "speed":
            return (val.get("stream_miss") if val.get("stream_miss") is not None else 1) <= 0.01
        if track == "aim":
            return (val.get("jump_miss") if val.get("jump_miss") is not None else 1) <= 0.01
        return False

    def step_debrief(self, a, prev, st):
        track = st["track"]
        if a.get("unjudged"):
            return self.unjudged_debrief(a, prev, st)
        if track == "benchmark":
            return self.bench_debrief(a, prev, st)
        if st.get("started") is None:
            st["started"] = a["start_ts"]
        st["attempts"] += 1
        st["tries_on_pick"] = st.get("tries_on_pick", 0) + (1 if a["md5"] == st.get("pick_md5") else 0)
        jtrack = track
        a["_track"] = track
        level = self.tracks[jtrack]["level"] if jtrack in TRACKS else None
        outcome, metrics, txt = judge(jtrack, a, self.ctx)
        d = self._base(a, st, outcome or "close")
        dval = difficulty(jtrack, a) if jtrack in TRACKS else None
        ch = None
        # every step attempt (warm-up too) is accounted for here, so the morning review never re-judges it
        self.counted.append(a["id"])
        if jtrack in TRACKS and outcome:
            ch = stair(self.tracks[jtrack], jtrack, dval, outcome, a, metrics, txt, self.caps[jtrack])
        if outcome == "pass":
            st["passes"] += 1
        easy_try = st.get("tilt_left", 0) > 0
        if track == "warmup":
            pass                     # nothing is judged in the warm-up: no tilt count either
        elif outcome == "fail" or a["outcome"] != "passed":
            self.fails_in_row += 1
        elif outcome in ("pass", "close"):
            self.fails_in_row = 0
            self.tilt = False
        if easy_try:
            st["tilt_left"] -= 1
            if st["tilt_left"] == 0:
                st["tilt_back"] = True
        if ch:
            d["level_change"] = {"track": jtrack, "from": ch["from"], "to": ch["to"]}
            self.level_changes.append(ch)
            if ch["to"] > ch["from"]:
                st["level_ups"] = st.get("level_ups", 0) + 1
            st["level"] = ch["to"]
        # headline
        h = self.headline(a, st, jtrack, outcome, txt, ch, dval, level)
        d["headline"] = h[:1].upper() + h[1:]
        d["observations"] = self.observations(a, prev, jtrack if jtrack in TRACKS else "warmup", outcome)
        t = tip(jtrack, a, outcome)
        if t and any(w in o for o in d["observations"] for w in ("late on average -", "early on average -"))                 and t == ADVICE[("accuracy", "late")]:
            t = None
        if t and len(d["observations"]) < 3:
            d["observations"].append(t)
        same = [x for x in self.session_atts[:-1] if x["id"] in self.counted and self.match_like(x, st)]
        d["compare"] = compare(a, prev, same)
        d["goal"] = st["goal"]
        d["next_action"] = self.next_action(a, st, jtrack, outcome, ch, dval)
        return d

    def match_like(self, x, st):
        md5s = {c["md5"] for c in st.get("cands") or []} | {st.get("pick_md5")}
        return x["md5"] in md5s

    def headline(self, a, st, track, outcome, txt, ch, d, level):
        nm = short_name(a["name"], 26)
        if st["track"] == "warmup":
            return (f"Warm-up {a['acc']:.1%} on {nm} - " + ("loose and ready" if outcome == "pass" else "keep it relaxed"))[:80]
        if outcome is None:
            return f"{nm}: {txt}"[:80]
        below = ((self.tracks[track].get("history") or [{}])[-1] or {}).get("note") == "below level"             if track in TRACKS else False
        if ch and ch["to"] > ch["from"]:
            return (f"{WHAT_PASS[track].capitalize()} at {fmt(track, d)} - level up: "
                    f"{fmt(track, ch['from'])} -> {fmt(track, ch['to'])}!")[:80]
        if ch and ch["to"] < ch["from"]:
            return f"{txt} on {nm} - easing to {fmt(track, ch['to'])}"[:80]
        if outcome == "pass" and below:
            return (f"{WHAT_PASS[track].capitalize()} at {fmt(track, d)} - below your {fmt(track, level)} level: "
                    f"a good rep, not a level-up")[:80]
        if outcome == "pass":
            h = f"{WHAT_PASS[track].capitalize()} at {fmt(track, d)} ({txt}) - 1 of 2!"
            if len(h) > 80:
                h = f"{WHAT_PASS[track].capitalize()} at {fmt(track, d)} - 1 of 2 for the next level!"
            return h[:80]
        if outcome == "close":
            return f"Close: {txt} on {nm}"[:80]
        return f"{txt} on {nm} - not this time"[:80]

    def next_action(self, a, st, track, outcome, ch, d):
        ts = a["ts"]
        f = self.fatigue(ts)
        if f["flag"]:
            na = self.rest_action(f, ts)
            if na["type"] == "stop":
                self.stopped = True
            return na
        # step exit: benchmark done / warm-up done / time / tries
        el = self.elapsed_on(st, ts)
        if st["track"] == "benchmark":
            done = st.get("bench_done") or []
            if a["md5"] not in done and any(c["md5"] == a["md5"] for c in st["cands"]):
                st["pick_md5"], st["rate"], st["mods"] = a["md5"], a.get("rate") or 1.0, rate_mods(a.get("rate") or 1.0)
                return {"type": "retry", "md5": a["md5"], "rate": a.get("rate") or 1.0,
                        "text": f"Once more on {short_name(a['name'], 30)} - the better of your 2 tries is recorded",
                        "why": "One try varies by 2-3% - more than a week's progress - so the check-up takes the "
                               "better of two."}
            left = [c for c in st["cands"] if c["md5"] not in done]
            if not left:
                return self.advance(st, ts, "Check-up done - the results go into this week's trend.")
            c = left[0]
            fl = self.lib.get(c["md5"]) or {}
            order = [x["md5"] for x in st["cands"]]
            nth = len([m for m in order if (st.get("bench_tries") or {}).get(m)]) + 1
            st["pick_md5"], st["rate"], st["mods"] = c["md5"], c["rate"], rate_mods(c["rate"])
            return {"type": "next_map", "md5": c["md5"], "rate": c["rate"], "bench_n": nth,
                    "bench_of": len(order),
                    "text": f"Check-up map {nth} of {len(order)}: "
                            f"{short_name(map_name(fl), 30)}{at_rate(c['rate'])}",
                    "why": "Same maps every week, so the numbers can be compared."}
        if st["track"] == "warmup":
            if st["attempts"] >= 2 or el >= st["minutes"] * 60:
                return self.advance(st, ts, "Warm-up done.")
            c = self.next_cand(st)
            return {"type": "next_map", "md5": c["md5"] if c else st["pick_md5"], "rate": 1.0,
                    "text": "One more easy map: " + (self.next_for_step(st) or ""), "why": "Two easy maps get the timing back."}
        if el >= st["minutes"] * 60 or st["attempts"] >= (6 if track != "push" else 9):
            why = (f"{st['block']} time is up ({st['minutes']} min)" if el >= st["minutes"] * 60
                   else f"{st['attempts']} tries on this step")
            return self.advance(st, ts, why + ".")
        # tilt: 3 misses of the goal in a row
        if self.fails_in_row >= 3:
            self.tilt = True
            self.fails_in_row = 0        # the easier plays start a fresh count
            st["tilt_left"] = 2
            return self.ease(st, track, "tilt")
        L = self.tracks[track]["level"]
        if st.get("tilt_left", 0) > 0 and not ch:
            # still on the 2 easier plays after a tilt
            if outcome == "fail":
                return self.ease(st, track, "tilt_again")
            return {"type": "retry", "md5": st["pick_md5"], "rate": st["rate"],
                    "text": f"Good - one more easy one like that, then back to {fmt(track, L)}",
                    "why": "Two easier plays to get the rhythm back, then up again."}
        if st.pop("tilt_back", False) or (outcome == "pass" and not ch and (
                (self.tracks[track].get("history") or [{}])[-1] or {}).get("note") == "below level"):
            # an easier map/speed is done: return to the level (never get parked below it)
            if track in ("speed", "aim"):
                r = self.same_map_rate(st, L)
                if r and r > (st["rate"] or 1.0) + 1e-6:
                    st["rate"], st["mods"] = r, rate_mods(r)
                    return {"type": "faster", "md5": st["pick_md5"], "rate": r,
                            "text": f"Back up to {rate_txt(r)} on the same map ({fmt(track, L)})",
                            "why": "That was below your level - it counts as a good rep, not as progress."}
            cs = [c for c in self.cands_for(st, L, "back") if c["md5"] != st.get("pick_md5")]
            if cs:
                self.set_pick(st, cs[0])
                return {"type": "next_map", "md5": cs[0]["md5"], "rate": cs[0]["rate"],
                        "text": f"Back to your level: {self.next_for_step(st)} ({fmt(track, L)})",
                        "why": "That was below your level - it counts as a good rep, not as progress."}
        if ch and ch["to"] > ch["from"]:
            if track in ("speed", "aim"):
                r = self.same_map_rate(st, L)
                if r:
                    st["rate"], st["mods"] = r, rate_mods(r)
                    st["goal"] = self.goal_now(st, track, L)
                    return {"type": "faster", "md5": st["pick_md5"], "rate": r,
                            "text": f"Go up to {rate_txt(r)} on the same map ({fmt(track, L)})",
                            "why": ch["why"] + " - the staircase moves up after 2 clean runs."}
            cs = self.cands_for(st, L)
            if cs:
                self.set_pick(st, cs[0])
                st["goal"] = self.goal_now(st, track, L)
                return {"type": "next_map", "md5": cs[0]["md5"], "rate": cs[0]["rate"],
                        "text": f"Harder map: {self.next_for_step(st)} ({fmt(track, L)})", "why": ch["why"] + "."}
            return self.advance(st, ts, f"No local maps at {fmt(track, L)} yet.")
        if ch and ch["to"] < ch["from"]:
            return self.ease(st, track, "down", ch)
        if outcome == "pass":
            need = 2 - self.tracks[track]["streak"]["pass"]
            return {"type": "retry", "md5": st["pick_md5"], "rate": st["rate"],
                    "text": f"Same again: one more like that moves you to {fmt(track, snap(track, L + TRACKS[track]['step']))}"
                    if need == 1 else "Same map again", "why": "Two passes in a row = level up."}
        if track == "push":
            if st.get("tries_on_pick", 0) >= 3:
                c = self.next_cand(st)
                if c:
                    return {"type": "next_map", "md5": c["md5"], "rate": 1.0,
                            "text": f"3 tries is the limit - next push map: {self.next_for_step(st)}",
                            "why": "Exposure to more maps beats grinding one."}
            return {"type": "retry", "md5": st["pick_md5"], "rate": 1.0,
                    "text": f"Try {short_name(a['name'], 30)} again ({3 - st.get('tries_on_pick', 0)} tries left)",
                    "why": "Push maps are for exposure - misses are fine, a pass with few misses levels you up."}
        if outcome == "close":
            return {"type": "retry", "md5": st["pick_md5"], "rate": st["rate"],
                    "text": "Retry, same map and speed - " + self.focus_hint(a, track),
                    "why": "Close = keep the level and the streak; one clean run counts."}
        if outcome == "fail":
            # level already at today's floor (one drop per session): ease without moving the level
            return self.ease(st, track, "capped")
        return {"type": "retry", "md5": st["pick_md5"], "rate": st["rate"], "text": "Same map again", "why": ""}

    def focus_hint(self, a, track):
        sec = sorted(section_obs(a), key=lambda x: -x[0])
        if sec and " at " in sec[0][1] and ": " in sec[0][1].split(" at ", 1)[1]:
            span, what = sec[0][1].split(" at ", 1)[1].split(": ", 1)
            return f"the misses were the {what.split(',')[0]} at {span}: slow down your reading there, not everywhere"
        if track == "accuracy":
            return "steady tempo, tap on the beat"
        if track == "speed":
            return "relaxed fingers through the long streams"
        return "one clean run"

    def next_cand(self, st):
        cs = st.get("cands") or []
        st["pick_n"] = st.get("pick_n", 0) + 1
        if st["pick_n"] < len(cs):
            self.set_pick(st, cs[st["pick_n"]])
            return cs[st["pick_n"]]
        more = self.cands_for(st, st.get("level") or self.tracks.get(st["track"], {}).get("level"), "next")
        if more:
            st["cands"] = (st.get("cands") or []) + [c for c in more if c["md5"] not in {x["md5"] for x in cs}]
            if st["pick_n"] < len(st["cands"]):
                self.set_pick(st, st["cands"][st["pick_n"]])
                return st["cands"][st["pick_n"]]
        return None

    def goal_now(self, st, track, L):
        return goal_text(track, L) if track in TRACKS else st["goal"]

    def ease(self, st, track, why, ch=None):
        L = self.tracks[track]["level"]
        s = TRACKS[track]["step"]
        if ch:   # the level moved down: the card's goal follows it (like on a level up)
            st["goal"] = self.goal_now(st, track, L)
        if why in ("tilt", "tilt_again"):
            # never more than 2 steps below this morning's level, whatever the level did today
            tgt = snap(track, min(L, self.day_start.get(track, L) - 2 * s))
        else:
            tgt = snap(track, L - (s if why == "capped" else 0))
        calm = ("Three misses of the goal in a row - that's frustration, not your level. Two easier plays, breathe out "
                "before you start, then back up. " if why == "tilt" else
                "Still tough - even easier for one play, slow breath first. " if why == "tilt_again" else "")
        last_note = ((self.tracks[track].get("history") or [{}])[-1] or {}).get("note")
        if ch:
            reason = ch["why"] + " - the level steps down one after a miss of the goal."
        elif why in ("tilt", "tilt_again"):
            reason = "The level stays; easier plays only until the rhythm is back."
        elif last_note == "drop capped (one step per session)":
            reason = ("Today's level already dropped once (max one step per session), so this is an easier "
                      "map/speed without moving the level.")
        elif last_note == "above level":
            reason = "That map was above your current level, so the level stays; an easier one first."
        else:
            reason = "The level stays where it is; an easier map/speed first to get the rhythm back."
        if track in ("speed", "aim"):
            r = self.same_map_rate(st, tgt)
            if r and r < (st["rate"] or 1.0) - 1e-6:
                st["rate"], st["mods"] = r, rate_mods(r)
                return {"type": "slower", "md5": st["pick_md5"], "rate": r,
                        "text": calm + f"Same map at {rate_txt(r)} ({fmt(track, tgt)})", "why": reason}
        cs = self.cands_for(st, tgt, "ease" if why != "down" else "main")
        cs = [c for c in cs if c["md5"] != st.get("pick_md5")] or cs
        if cs:
            self.set_pick(st, cs[0])
            return {"type": "slower" if track in ("speed", "aim") else "next_map", "md5": cs[0]["md5"],
                    "rate": cs[0]["rate"],
                    "text": calm + f"Easier: {self.next_for_step(st)} ({fmt(track, tgt)})", "why": reason}
        return {"type": "retry", "md5": st["pick_md5"], "rate": st["rate"], "text": calm + "Same map again, relaxed",
                "why": reason}

    def advance(self, st, ts, why):
        self.finish("done", ts)
        nxt = self.active()
        if not nxt or nxt["track"] == "review":
            if nxt:
                self.finish("done", ts)
            self.stopped = True
            return {"type": "stop", "text": "That's today's plan done - nice work. Rest; tomorrow builds on this.",
                    "why": why}
        if nxt["track"] == "break":
            return {"type": "break", "text": "Break time: 5 minutes away from the screen, then " +
                    (self.steps[self.cur + 1]["block"] if self.cur + 1 < len(self.steps) else "the next block"),
                    "why": why + " Rest between blocks."}
        return {"type": "next_step", "md5": nxt.get("pick_md5"), "rate": nxt.get("rate"),
                "text": f"Next: {nxt['block']} - {self.next_for_step(nxt) or 'see the card'}", "why": why}


# ----------------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------------

def _paths():
    d = state_dir()
    return d / "training_state.json", d / "session.json", d / "session_cmd.json"


def read_cmds(sess, cmd_path):
    c = load_json(cmd_path, None)
    if isinstance(c, dict) and c.get("cmd") in ("done", "skip", "back", "restart"):
        ts = num(c.get("ts")) or time.time()
        key = f"{c['cmd']}@{ts}"
        if key not in {f"{x['cmd']}@{x['ts']}" for x in sess.get("cmds", [])}:
            sess.setdefault("cmds", []).append({"cmd": c["cmd"], "ts": ts})
        try:
            os.remove(cmd_path)
        except OSError:
            pass
    return sess.get("cmds", [])


def update(plays_all, lib, osu_index, history, ctx, now=None, live=None, write=True, extra_stars=()):
    """Main entry. plays_all: replay summaries (any order) of the player. Returns the coach_data keys."""
    now = now or time.time()
    today = day_of(now)
    t0 = day_start_ts(today)
    sp, sesp, cmdp = _paths()
    state = load_json(sp, {}) if write else (ctx.get("_state") or {})
    sess = load_json(sesp, {}) if write else (ctx.get("_session") or {})
    migrated_from = None
    if not isinstance(state, dict) or state.get("v") != STATE_V:
        # older state: levels are re-derived from the history (the accuracy knob changed meaning), but the
        # weekly check-up maps and results are kept - they must stay the same maps every week
        old_bm = state.get("benchmark") if isinstance(state, dict) else None
        migrated_from = state.get("v") if isinstance(state, dict) and state.get("v") else None
        state = {"_keep_benchmark": old_bm} if isinstance(old_bm, dict) else {}
        if isinstance(sess, dict) and sess.get("v_state") != STATE_V:
            sess = {}
    lives_raw = read_live() if live is None else live
    stars = Stars(lib, list(extra_stars) + [(r.get("md5"), r.get("mods"), r.get("rate"), r.get("stars"))
                                            for r in lives_raw if not r.get("stars_estimated")])
    ctx = dict(ctx)
    ctx["history"] = history
    reps = sorted((att_from_replay(s, lib, stars) for s in plays_all if s.get("ts", 0) <= now), key=lambda a: a["ts"])
    # old live attempts were already fed through the staircases: only convert the recent ones (parsing each
    # map's objects costs ~7 ms per attempt)
    cut = ((state.get("processed_until") or 0) - 2 * 86400) if state.get("tracks") else 0
    lives = [att_from_live(r, lib, stars, osu_index) for r in lives_raw
             if num(r.get("ts")) and cut <= num(r["ts"]) <= now]
    seen_ids = set()
    for lv in sorted(lives, key=lambda x: x["ts"]):   # two tries on one map ending in the same second
        while lv["id"] in seen_ids:
            lv["id"] += "+"
        seen_ids.add(lv["id"])
    allatts = merge_attempts(reps, lives)
    before = [a for a in allatts if a["ts"] < t0]
    todays = [a for a in allatts if a["ts"] >= t0]
    # UR target reference: fixed for the day (re-deriving today's attempts must give the same verdicts)
    urs = [a["ur"] for a in before[-30:] if a.get("ur") and a["outcome"] == "passed"]
    ctx["ur_recent"] = median(urs) if urs else (ctx.get("stats") or {}).get("ur")
    changes = []
    seed = datetime.fromisoformat(today).toordinal()

    # 1. bootstrap
    if not state.get("tracks"):
        tracks, log, bch = bootstrap(before, ctx)
        keep = state.get("_keep_benchmark")
        state = {"v": STATE_V, "tracks": tracks, "level_log": log, "day": None, "day_start_tracks": None,
                 "bootstrapped": {"ts": int(now), "attempts": len(before), "changes": len(bch),
                                  "migrated_from": migrated_from},
                 "processed_until": t0,
                 "benchmark": keep if isinstance(keep, dict) else {"maps": [], "results": [], "last_day": None},
                 "sessions": 0, "prev_plan": {}}
        if before:
            changes.append(f"I set your starting levels from your last {len(before)} plays: "
                           + ", ".join(f"{BLOCK[t]} {fmt(t, tracks[t]['level'])}" for t in TRACKS))

    # 2. rollover
    rolled = state.get("day") != today
    if rolled:
        prev_start = state.get("day_start_tracks") or copy.deepcopy(state["tracks"])
        prev_day = state.get("day")
        counted = set((sess or {}).get("counted") or [])
        since = state.get("processed_until") or t0
        free = [a for a in before if a["ts"] >= since and a["id"] not in counted
                and (a.get("live_id") is None or a["live_id"] not in counted)]
        ech = apply_evidence(state["tracks"], free, ctx, max_up=1, log_levels=state.setdefault("level_log", {}))
        for t in TRACKS:
            lg = state["level_log"].setdefault(t, [])
            if not lg or lg[-1][0] != today:
                lg.append([today, state["tracks"][t]["level"]])
            del lg[:-120]
        state["day_start_tracks"] = copy.deepcopy(state["tracks"])
        state["day"] = today
        state["processed_until"] = t0
        state["sessions"] = state.get("sessions", 0) + (1 if (sess or {}).get("steps") and any(
            s.get("attempts") for s in sess.get("steps", [])) else 0)
        # what changed since yesterday's start, with the reason that moved it in that direction
        lo = day_start_ts(prev_day) if prev_day else 0
        for t in TRACKS:
            a0, a1 = prev_start[t]["level"], state["tracks"][t]["level"]
            if abs(a1 - a0) > 1e-9:
                win = [c for c in state["tracks"][t].get("changes", []) if lo - 1 <= c["ts"] < t0]
                chs = [c for c in win if (c["to"] - c["from"]) * (a1 - a0) > 0]
                opp = [c for c in win if (c["to"] - c["from"]) * (a1 - a0) < 0]
                why = chs[-1]["why"] if chs else None
                if why and opp:
                    ext = min(c["to"] for c in opp) if a1 > a0 else max(c["to"] for c in opp)
                    why = f"{'dropped' if a1 > a0 else 'rose'} to {fmt(t, ext)} first, then: {why}"
                elif why and len(chs) > 1:
                    why = f"{len(chs)} moves, the last: {why}"
                changes.append(f"{TRACKS[t]['label']}: {fmt(t, a0)} -> {fmt(t, a1)}" + (f" ({why})" if why else ""))
        if prev_day and ech:
            changes.append(f"{len(ech)} level move{'s' if len(ech) > 1 else ''} came from free plays "
                           f"(maps outside the plan; one step up or down per track per day at most)")
        sess = None
    tracks0 = state["day_start_tracks"]

    # 3. today's session (steps are fixed at the first run of the day)
    levels = {t: tracks0[t]["level"] for t in TRACKS}
    picker = Picker(lib, stars, history, levels, ctx, seed)
    bm = state.setdefault("benchmark", {"maps": [], "results": [], "last_day": None})
    bm["maps"] = [b for b in bm.get("maps", []) if _std(lib.get(b["md5"]))]
    if len(bm["maps"]) < 2:
        bm["maps"] = pick_benchmark(picker, tracks0)
    if not sess or sess.get("date") != today or not sess.get("steps_initial"):
        last_b = max([x for x in (bm.get("last_day"), bm.get("last_offered")) if x] or [None]) if (
            bm.get("last_day") or bm.get("last_offered")) else None
        due = (last_b is None or bm.get("last_offered") == today
               or (datetime.fromisoformat(today) - datetime.fromisoformat(last_b)).days >= 7
               or (bm.get("last_day") and state.get("sessions", 0) - bm.get("sessions_at", 0) >= 7))
        if due:
            bm["last_offered"] = today
        prev = load_json(sesp, {}) if write else (ctx.get("_session") or {})
        # a level with no local maps at all would silently drop its block: step it toward the nearest
        # level that has maps (at most 3 steps), and say so
        for t in ("accuracy", "speed", "aim", "stamina", "reading"):
            L0 = tracks0[t]["level"]
            if picker.cands(t, L0):
                continue
            stp = TRACKS[t]["step"]
            for k in (1, 2, 3):
                hit = None
                for lv in (snap(t, L0 - k * stp), snap(t, L0 + k * stp)):
                    if abs(lv - L0) > 1e-9 and picker.cands(t, lv):
                        hit = lv
                        break
                if hit is not None:
                    for trs in (state["tracks"], tracks0):
                        trs[t]["level"] = hit
                        trs[t]["streak"] = {"pass": 0, "fail": 0}
                    levels[t] = hit
                    picker.L = levels
                    lg = state.setdefault("level_log", {}).setdefault(t, [])
                    if lg and lg[-1][0] == today:
                        lg[-1][1] = hit
                    changes.append(f"{TRACKS[t]['label']}: {fmt(t, L0)} -> {fmt(t, hit)} (no maps at "
                                   f"{fmt(t, L0)} in your library - this is the nearest level with maps)")
                    break
        steps = build_steps(tracks0, state.get("level_log"), today, ctx, picker, due, bm["maps"], prev)
        sess = {"date": today, "created": int(now), "steps_initial": steps, "pick_log": {}, "cmds": [],
                "benchmark_due": bool(due), "v_state": STATE_V}
        # plan changes vs yesterday
        pp = state.get("prev_plan") or {}
        newp = {s["track"]: s["minutes"] for s in steps if s["track"] in TRACKS}
        pp = {k: v for k, v in pp.items() if k in TRACKS or k == "_bench"}
        for t, m in sorted(newp.items(), key=lambda x: -x[1]):
            o = pp.get(t)
            why = "; ".join(next((s["why"] for s in steps if s["track"] == t), []) or [])
            if o is None and pp:
                changes.append(f"{BLOCK[t]} is back today ({m} min)" + (f": {why}" if why else ""))
            elif o is not None and m - o >= 2:
                upw = [r for r in next((s["why"] for s in steps if s["track"] == t), []) or []
                       if "maintenance" not in r and "improving" not in r]
                if not upw:
                    upw = ["no check-up today, so the time goes back to the blocks" if pp.get("_bench") and not due
                           else "the other blocks need less time today"]
                changes.append(f"More time on {BLOCK[t]} ({m} min, was {o}): " + "; ".join(upw))
            elif o is not None and o - m >= 2:
                tr_t = trend_of(state.get("level_log"), t, today)
                gain = max(((x, newp[x] - pp.get(x, 0)) for x in newp if x != t), key=lambda q: q[1], default=None)
                lwhy = ("the weekly check-up needs 10 minutes today" if due else
                        "improving - lighter maintenance" if tr_t == "up" else
                        (f"{BLOCK[gain[0]]} is back in the rotation today" if gain[0] not in pp else
                         f"{BLOCK[gain[0]]} needs the time more today") if gain and gain[1] > 0 else
                        "rotation")
                changes.append(f"Less time on {BLOCK[t]} ({m} min, was {o}): {lwhy}")
        for t in pp:
            if t in TRACKS and t not in newp:
                changes.append(f"No {BLOCK[t]} block today (rotates, or no maps at the level)")
        if due:
            changes.append("Weekly check-up today: the same maps as every week, up to 2 tries each, so we can see "
                           "your progress.")
        state["prev_plan"] = dict(newp, _bench=bool(due))
        sess["changes_morning"] = changes
    else:
        changes = sess.get("changes_morning") or []
    cmds = read_cmds(sess, cmdp) if write else sess.get("cmds", [])

    # 4. derive
    prev_by_md5 = {}
    for a in before:
        prev_by_md5[a["md5"]] = a
    ctx["_bench_old"] = [r for r in bm.get("results", []) if r.get("day") != today]
    dv = Derive(state, sess, todays, cmds, ctx, picker, lib, prev_by_md5, now, hist_atts=before).run()
    state["tracks"] = dv.tracks
    sess["counted"] = dv.counted
    if dv.bench_results:
        bm["last_day"] = today
        bm["sessions_at"] = state.get("sessions", 0)
        res = [r for r in bm.get("results", []) if r.get("day") != today]
        best = {}
        for r in dv.bench_results:       # the better of today's tries per map is the recorded result
            o = best.get(r["md5"])
            if o is None or bench_better(r["track"], r.get("metric"), o.get("metric")):
                best[r["md5"]] = r
        for r in best.values():
            res.append(dict(r, day=today))
        bm["results"] = res[-60:]
    for ch in dv.level_changes:
        changes = changes + [f"Today: {TRACKS[ch['track']]['label']} {fmt(ch['track'], ch['from'])} -> "
                             f"{fmt(ch['track'], ch['to'])} ({ch['why']})"]
    # benchmark comparison
    bres = bm.get("results", [])
    if dv.bench_results:
        for r in [x for x in bres if x.get("day") == today]:
            old = [x for x in bres if x["md5"] == r["md5"] and x.get("day") != today]
            if old and r.get("metric") and old[-1].get("metric"):
                changes.append(f"Check-up ({BLOCK.get(r['track'], r['track'])} map): "
                               + bench_compare_text(r["track"], r["metric"], old[-1]["metric"], old[-1]["day"])
                               .split(": ", 1)[1])
            elif old and r.get("acc") is not None and old[-1].get("acc") is not None:
                o = old[-1]
                changes.append(f"Check-up {r['track']} map vs {o['day']}: {pct_txt(r['acc'] - o['acc'])} acc"
                               + (f", UR {r['ur'] - o['ur']:+.0f}" if r.get("ur") and o.get("ur") else ""))

    # 5. outputs
    st = dv.active()
    fat = dv.fatigue(now if not dv.session_atts else dv.session_atts[-1]["ts"])
    steps_out = []
    for s in dv.steps:
        steps_out.append({k: s.get(k) for k in ("id", "block", "track", "minutes", "state", "attempts", "passes",
                                                 "pick_md5", "rate", "mods", "goal", "exit_rule", "level",
                                                 "level_ups", "started", "note")})
    current = None
    recs = []
    if st and st["track"] not in ("review",):
        rec = None
        if st.get("pick_md5"):
            c = {"md5": st["pick_md5"], "rate": st["rate"] or 1.0}
            c["stars"], c["est"] = stars.get(c["md5"], rate_mods(c["rate"]), c["rate"])
            tr = st["track"] if st["track"] != "benchmark" else next(
                (x["track"] for x in st["cands"] if x["md5"] == st["pick_md5"]), "accuracy")
            rec = rec_for(lib, c, tr, st.get("level") or levels.get(tr), ctx, levels)
            rec["goal"] = st["goal"] if st["track"] in TRACKS else rec["goal"]
        lv = st.get("level")
        trk = st["track"]
        current = {"block": st["block"], "track": trk, "map": rec, "rate": st.get("rate"), "mods": st.get("mods"),
                   "goal": st["goal"], "attempt_n": st["attempts"] + 1, "passes": st["passes"],
                   "needed": (2 - dv.tracks[trk]["streak"]["pass"]) if trk in TRACKS and trk != "push" else 1,
                   "level": lv, "level_txt": fmt(trk, lv) if trk in TRACKS else None,
                   "exit_rule": st["exit_rule"], "minutes": st["minutes"],
                   "elapsed_min": round((now - st["started"]) / 60, 1) if st.get("started") else 0}
    for s in dv.steps:
        if not s.get("pick_md5"):
            continue
        tr = s["track"] if s["track"] in TRACKS else ("warmup" if s["track"] == "warmup" else None)
        lst = [{"md5": s["pick_md5"], "rate": s["rate"] or 1.0}] + [c for c in s.get("cands") or []
                                                                    if c["md5"] != s["pick_md5"]][:2]
        for c in lst:
            if any(r["id"] == c["md5"] for r in recs):
                continue
            t2 = tr or next((x["track"] for x in s["cands"] if x["md5"] == c["md5"]), "accuracy")
            cc = dict(c)
            cc["stars"], cc["est"] = stars.get(c["md5"], rate_mods(c["rate"]), c["rate"])
            r = rec_for(lib, cc, t2, s.get("level") or levels.get(t2), ctx, levels)
            if s["track"] == "benchmark":
                r["purpose"] = t2
                r["reason"] = "Weekly check-up map (same every week). " + r["reason"]
            recs.append(r)
    plan = [{"block": s["block"], "minutes": s["minutes"], "goal": s["goal"],
             "pick_ids": [x for x in dict.fromkeys([s.get("pick_md5")] + [c["md5"] for c in s.get("cands") or []]) if x][:3],
             "track": s["track"], "state": s["state"]} for s in dv.steps]
    debs = list(reversed(dv.debriefs))[:MAX_DEBRIEFS]
    tr_out = {}
    for t in TRACKS:
        tk = dv.tracks[t]
        stp = TRACKS[t]["step"]
        need = 2 - tk["streak"]["pass"]
        tr_out[t] = {"level": tk["level"], "unit": TRACKS[t]["unit"], "label": TRACKS[t]["label"],
                     "level_txt": fmt(t, tk["level"]), "trend": trend_of(state.get("level_log"), t, today),
                     "next": ((f"Pass once more to reach {fmt(t, snap(t, tk['level'] + stp))}" if need == 1 else
                               f"Pass twice in a row to reach {fmt(t, snap(t, tk['level'] + stp))}")
                              + (f" (or one pass on a {fmt(t, tk['level'] + 0.8 * stp)} or harder map)" if t == "push" else ""))
                     + (f"; a miss of the goal drops it to {fmt(t, snap(t, tk['level'] - stp))}" if t != "push" else ""),
                     "knob": TRACKS[t]["knob"], "last_change": tk.get("last_change"),
                     "target": round(targets(ctx)[t], 2),
                     "checkup": [{"day": r.get("day"), **(r.get("metric") or {})} for r in bres
                                 if r.get("track") == t and r.get("metric")][-8:]}
    session_out = {"date": today, "started": int(dv.started) if dv.started else None, "step_index": dv.cur,
                   "steps": steps_out, "fatigue": {"ur_trend": fat["ur_trend"], "flag": bool(fat["flag"]),
                                                   "why": fat.get("why")},
                   "tilt": {"fails_in_row": dv.fails_in_row, "flag": dv.tilt}, "current": current,
                   "stopped": dv.stopped, "benchmark_due": sess.get("benchmark_due", False)}
    if not changes:
        changes = ["No level changes since yesterday: " + ", ".join(f"{BLOCK[t]} {fmt(t, tracks0[t]['level'])}"
                                                                    for t in ("accuracy", "speed", "aim", "push"))]
    out = {"session": session_out, "last_debrief": debs[0] if debs else None, "debriefs_today": debs,
           "changes": changes[:12], "tracks": tr_out, "session_plan": plan, "recommendations": recs}
    if write:
        sess_save = dict(sess)
        sess_save.update({"steps": steps_out, "step_index": dv.cur, "fatigue": session_out["fatigue"],
                          "tilt": session_out["tilt"], "current": current, "started": session_out["started"]})
        atomic_write_json(sp, state)
        atomic_write_json(sesp, sess_save)
    else:
        ctx_out = {"_state": state, "_session": sess}
        out["_internal"] = ctx_out
    return out
