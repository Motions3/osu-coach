#!/usr/bin/env python3
"""Rank-up recommender (CONTRACT_PP2.md): maps + mods that should raise YOUR total pp -> pp_recs.json.

    python pp_recommend.py                # auto: full refresh if the last one is > 24 h old, else incremental
    python pp_recommend.py --full         # full refresh (API status lookups, search, .osu fetches; < ~3 min)
    python pp_recommend.py --incremental  # after a session: new top plays + re-predict the best picks (< 30 s)
    python pp_recommend.py --show         # print the current pp_recs.json picks

Pipeline
--------
1. Candidates (maps):
   * owned osu!standard maps from .coach_library.json (+ .osu paths from inverting .osu_coach_cache.json);
     ranked status from the osu! API (GET /beatmaps?ids[]=..., 50 per call, cached in .pp_status_cache.json,
     ranked/approved/loved kept 30 days, others re-checked after 7) or from tosu's live attempts.
     Maps whose status is unknown are NOT recommended (unranked/loved maps give no pp).  A local file whose md5
     differs from the online checksum is "outdated" (scores would not submit) and is skipped too.
   * the climb list in pp_data.json (maps in the top 100s of players slightly above you: ranked by definition)
   * GET /beatmapsets/search (ranked, osu!, sorted by plays) in your star band (and the band DT lifts into it).
     Non-owned maps get their .osu text from https://osu.ppy.sh/osu/{beatmap_id} into .osu_cache/ (<= 200
     files a day, >= 1.1 s apart) so tosu can calculate them like owned maps.
2. Variants: NM, HD, HR, HDHR, DT, HDDT, kept when the (rough) modded star rating is inside the band
   [comfort - 0.5, comfort + 0.9].  comfort = your current comfortable level (pp_model / recent passes).
3. Stage A (cheap, cached forever): one tosu SS calculation per map x core mods (NM / HR / DT) -> real stars,
   SS pp, aim/speed.  Order: a no-tosu pre-score (rough SS pp from stars + length), so a budget-limited run
   always spends its calls on the most promising maps first; the next run continues where it stopped.
4. Stage B (the model): pp_model.predict_pp for the best variants (by a pre-gain from stage A), cached per
   (md5, mods, rate) with the model's signature.  If pp_model.py is missing, a rough star-curve fallback is
   used and every pick is labelled low confidence.
5. Gain = exact change of your total: the weighted top-100 sum (sum pp_i * 0.95^i, current top 100 from the
   API) plus the below-#100 correction (tail_pp: a new top-100 score pushes the old #100 to #101 instead of
   dropping it).  g(x) = weighted_gain(top100, x, replaces=your score on that beatmap, tail) - a better score
   on the same map replaces the old one, a worse one adds nothing.
       pp_model:  gain_expected = p_pass * E[g(pp)] over the model's whole outcome distribution (the same
                  number the song-select bubble shows; gain_if_pass = E[g(pp)])
       fallback:  E[g(pp)] over 15 quantiles of a split-lognormal fitted to pp_low / pp_expected / pp_high
   gain_high = max(g(pp_high), gain_if_pass).  No cap: the model's own distribution is the estimate.
6. Ranking: gain_expected; one mod per beatmap (the others listed as alt_mods), one diff per song title (so at
   most one per set), owned and not-owned mixed (<= 70% of the list one kind while the other has something
   worthwhile), maps where your existing score already beats pp_high are dropped.  DT/HR variants the model
   calls low confidence (too few recent plays with that mod) are never a main pick or smash pick.  Picks with
   < 25% pass chance are labelled "stretch" and listed after the passable ones.  "smash": p_pass >= SMASH_P and
   gain_expected >= max(SMASH_MIN_GAIN, 40% of the best candidate's gain) - a good bet AND among your biggest
   gains; smash_picks lists the best 5 of those (per map its best smash mod).
7. Explanations from data: map style (library pattern features), your own average accuracy on similar maps
   (replays + live passes), the model's expected acc / misses / pass chance and how many similar plays it saw.

Budgets: full refresh deadline 170 s, incremental 28 s; caches are flushed as it goes, so an interrupted or
timed-out refresh resumes.  tosu calls from the refresh thread are throttled to <= 5/s (other threads, e.g.
the pet's live watcher, are not slowed).  Read-only on osu!/tosu folders.  Never opens a window.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

OUT_NAME = "pp_recs.json"
CACHE_NAME = ".pp_recs_cache.json"
STATUS_NAME = ".pp_status_cache.json"
OSU_DIR_NAME = ".osu_cache"

VARIANTS = [("NM", 1.0), ("HD", 1.0), ("HR", 1.0), ("HDHR", 1.0), ("DT", 1.5), ("HDDT", 1.5)]
CORE = {"NM": "NM", "HD": "NM", "HR": "HR", "HDHR": "HR", "DT": "DT", "HDDT": "DT"}
GAIN_STATUSES = ("ranked", "approved")
NO_PP_STATUSES = ("loved", "qualified", "pending", "graveyard", "wip", "unranked", "outdated", "missing")

BAND_LO, BAND_HI = -0.5, 0.9         # candidate band around comfort (contract: -0.3..+0.8, padded for rough SR)
SEARCH_LO, SEARCH_HI = -0.3, 0.8     # API search band
FULL_DEADLINE_S = 170.0
INC_DEADLINE_S = 28.0
FULL_TTL_S = 24 * 3600
TOSU_RATE = 5.0                      # requests / s from the refresh thread
API_BUDGET = 150                     # osu! API requests per full refresh (~1.1 s each)
API_STATUS_SHARE = 40.0              # seconds of a full refresh that status lookups may use
SEARCH_PAGES = 2                     # per band
SEARCH_TTL_S = 3 * 24 * 3600
FETCH_PER_DAY = 200
FETCH_PER_RUN = 20                   # .osu downloads per refresh (only needed for pattern features / tosu)
FETCH_INTERVAL_S = 1.1
STATUS_TTL_FINAL = 30 * 86400
STATUS_TTL_OTHER = 7 * 86400
ATTR_MAX_FULL = 260                  # stage A calcs per full refresh (cached: later runs extend coverage)
ATTR_MAX_INC = 20
PRED_MAX_FULL = 600                  # stage B model predictions per full refresh (fast once pp_model is seeded
                                     # with this run's API attributes; the time budget still applies)
PRED_MAX_INC = 120
VARIANTS_PER_MAP = 3                 # model predictions per map (NM + the 2 most promising mod variants)
PRED_STALE_S = 6 * 3600              # model changed + prediction older than this -> re-predict when budget allows
N_PICKS = 30
MAX_PER_SET = 2
OWN_SHARE_MAX = 0.7
SMASH_P = 0.55                       # smash = better-than-even pass chance (live pass rates are often ~30%) ...
SMASH_MIN_GAIN = 0.5                 # ... and a real gain: at ~1800pp with a flat top 100 (floor ~40pp) a 55pp
                                     # pass adds only ~2pp, so an absolute 2pp bar was never reachable
SMASH_REL = 0.4                      # ... and >= 40% of the best expected gain of any candidate
SMASH_KEEP = 5                       # always list the best 5 smash maps
MIN_GAIN_HIGH = 0.3                  # drop picks whose GOOD run (pp_high) adds < 0.3 pp to your total
UA = "osu-coach/1.0 (personal training helper)"
PLAYING_FLAG = ".pp_playing"         # written by the pet while you play a map: the refresh waits between steps
PLAYING_FLAG_FRESH_S = 20.0
PAUSE_STEP_S = 2.0
PAUSE_MAX_S = 600.0
BOM = bytes([0xEF, 0xBB, 0xBF])


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def data_dir() -> Path:
    d = os.environ.get("OSU_COACH_RECS_DIR") or os.environ.get("OSU_COACH_PP_DIR")
    return Path(d) if d else HERE


def _log(msg):
    try:
        p = data_dir() / "pp_recommend_log.txt"
        if p.exists() and p.stat().st_size > 200_000:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)[:500] + "\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if v is not None else default
    except Exception:
        return default


def atomic_write_json(path, obj, compact=True) -> bool:
    path = Path(path)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if compact:
                json.dump(obj, f, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            else:
                json.dump(obj, f, indent=1, ensure_ascii=False, allow_nan=False)
        for i in range(20):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.1)
    except Exception as e:
        _log("write %s failed: %s" % (path.name, e))
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def r1(x):
    x = num(x)
    return None if x is None else round(x, 1)


def r2(x):
    x = num(x)
    return None if x is None else round(x, 2)


def wquant(vals, ws, q):
    pts = sorted((v, w) for v, w in zip(vals, ws) if v is not None and w > 0)
    if not pts:
        return None
    tot = sum(w for _, w in pts)
    acc = 0.0
    for v, w in pts:
        acc += w
        if acc >= q * tot - 1e-12:
            return v
    return pts[-1][0]


def vkey(md5, mods, rate):
    return "%s|%s|%.2f" % (md5, mods, float(rate))


# ----------------------------------------------------------------------------
# pp math (same as osu_api.weighted_gain / pp_live.rank_gain_pp)
# ----------------------------------------------------------------------------

def weighted_total(pps):
    s = sorted((float(p) for p in pps if p is not None), reverse=True)[:100]
    return sum(p * 0.95 ** i for i, p in enumerate(s))


BONUS_PP_MAX = 416.6667


def tail_pp(total_pp, top_pps, n_scores=1000):
    """Weighted pp of the scores below #100 implied by the profile total (total - bonus - weighted top 100);
    same as pp_live.tail_pp. 0 when unknown / fewer than 100 top plays."""
    tot = num(total_pp)
    pps = [float(p) for p in top_pps or [] if num(p) is not None]
    if tot is None or len(pps) < 100:
        return 0.0
    bonus = BONUS_PP_MAX * (1 - 0.995 ** min(1000, max(0, int(n_scores or 0))))
    return max(0.0, tot - bonus - weighted_total(pps))


def weighted_gain(top_pps, new_pp, replaces=None, tail=None):
    """Exact change of the weighted top-100 sum if a play worth new_pp is set; replaces = your existing pp on the
    same beatmap (only the best score per map counts).  tail (tail_pp) adds the below-#100 correction: a new
    score entering a full top 100 pushes the old #100 to #101 (keeps 0.95^100 of it) and every lower score down
    one place (-5% of their weighted value)."""
    new_pp = num(new_pp)
    if new_pp is None:
        return 0.0
    old = sorted((float(p) for p in top_pps if p is not None), reverse=True)
    new = list(old)
    replaced = False
    if replaces is not None:
        if new_pp <= replaces:
            return 0.0
        for i, p in enumerate(new):
            if abs(p - float(replaces)) < 1e-6:
                del new[i]
                replaced = True
                break
    new.append(new_pp)
    g = weighted_total(new) - weighted_total(old)
    if tail is not None and len(old) >= 100 and not replaced and new_pp > old[99]:
        g += old[99] * 0.95 ** 100 - 0.05 * float(tail)
    return max(0.0, g)


_Z15 = None
Z80 = 0.8416212335729143


def dist_gain(top_pps, lo, mid, hi, replaces=None, tail=None):
    """Mean gain given a pass over 15 quantiles of a split-lognormal through (p20, p50, p80) = (lo, mid, hi).
    (A 3-point average undercounts: the gain is 0 below your top-100 floor and convex above it.)"""
    global _Z15
    if _Z15 is None:
        from statistics import NormalDist
        _Z15 = [NormalDist().inv_cdf((i + 0.5) / 15) for i in range(15)]
    mid = num(mid)
    if mid is None or mid <= 0:
        return 0.0
    lo = num(lo) or mid
    hi = num(hi) or mid
    lm = math.log(mid)
    sl = (lm - math.log(max(1e-3, lo))) / Z80 if lo < mid else 0.0
    sh = (math.log(hi) - lm) / Z80 if hi > mid else 0.0
    vals = [math.exp(lm + z * (sh if z > 0 else sl)) for z in _Z15]
    return sum(weighted_gain(top_pps, v, replaces, tail) for v in vals) / len(vals)


def title_key(m):
    """Normalised song title: two sets of the same song count as one for diversity."""
    t = "".join(ch for ch in str(m.get("title") or "").lower() if ch.isalnum())
    return t or None


STRETCH_P = 0.25


def est_combo(max_combo, misses):
    """Expected longest combo with `misses` random misses (same as pp_live._est_combo)."""
    if not max_combo:
        return None
    m = int(misses or 0)
    if not m:
        return int(max_combo)
    h = sum(1.0 / i for i in range(1, m + 2))
    return int(max(1, min(max_combo - m, round(max_combo * h / (m + 1)))))


def rough_stars(nm, mods, rate):
    """Pre-filter estimate (same factors as osu_api.rough_stars)."""
    s = float(nm)
    if abs(rate - 1) > 1e-6:
        s *= 1 + (rate - 1) * 0.8 if rate > 1 else 1 - (1 - rate)
    if "HR" in mods:
        s *= 1.08
    return s


def rough_ss_pp(stars, n_objects=None):
    """No-tosu SS pp guess, only to ORDER work (stage A gets the real number). ~ rosu-pp at AR9-10 / OD8-9."""
    n = n_objects or 600
    lb = 0.95 + 0.4 * min(1.0, n / 2000.0) + (math.log10(n / 2000.0) * 0.5 if n > 2000 else 0.0)
    return 1.1 * float(stars) ** 3 * lb


MOD_PASS = {"HR": 0.55, "DT": 0.5}    # ordering only: your pass chance with HR (~-46% vs HD) / DT is much lower


def hd_factor(ar):
    """HD pp bonus vs NM for ordering / sanity checks. Your server scores: HD at AR < 9 earns ~x1.18 over the
    plain formula vs ~x1.07 without HD (26 scores, 2026) - the lazer HD bonus grows as AR drops."""
    a = num(ar)
    if a is None:
        return 1.06
    return 1.10 if a < 9 else 1.04


def crude_p_pass(stars, comfort):
    return 1.0 / (1.0 + math.exp((stars - comfort - 0.35) / 0.22))


def crude_frac(stars, comfort):
    """Fraction of SS pp a typical pass gets (acc + misses), only for ordering."""
    d = stars - comfort
    return max(0.25, min(0.85, 0.62 - 0.12 * d))


# ----------------------------------------------------------------------------
# tosu throttle (only the refresh thread is slowed)
# ----------------------------------------------------------------------------

class Throttle:
    def __init__(self, rate=TOSU_RATE, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / rate
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.count = 0

    def wait(self):
        with self._lock:
            now = self.clock()
            if now < self._next:
                self.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval
            self.count += 1


class _TosuThrottle:
    """Context manager: patch pp_live._http_json so calls from THIS thread are paced (pp_model calls tosu through
    pp_live, whatever name it imported calc_pp under, so this catches its calls too)."""

    def __init__(self, throttle):
        self.t = throttle
        self.pl = None
        self.orig = None

    def __enter__(self):
        try:
            import pp_live
        except Exception:
            return self
        self.pl, self.orig = pp_live, getattr(pp_live, "_http_json", None)
        if self.orig is None or getattr(self.orig, "_pp_rec_wrapped", False):
            self.pl = None
            return self
        tid, orig, t = threading.get_ident(), self.orig, self.t

        def paced(*a, **kw):
            if threading.get_ident() == tid:
                t.wait()
            return orig(*a, **kw)
        paced._pp_rec_wrapped = True
        pp_live._http_json = paced
        return self

    def __exit__(self, *exc):
        if self.pl is not None:
            self.pl._http_json = self.orig
        return False


# ----------------------------------------------------------------------------
# predictors
# ----------------------------------------------------------------------------

class ModelPredictor:
    """pp_model (CONTRACT_PP2.md API), imported defensively."""

    def __init__(self, mod):
        self.m = mod
        self.name = "pp_model"

    def report(self):
        try:
            r = self.m.model_report()
            return r if isinstance(r, dict) else {}
        except Exception as e:
            _log("model_report failed: %s" % e)
            return {}

    def signature(self):
        r = self.report()
        return "%s|%s|%s" % (r.get("updated"), r.get("trained_on"), r.get("learner"))

    def trained(self):
        r = self.report()
        return bool(r.get("trained_on"))

    def _call(self, fn, md5, mods, rate, osu_path, extra):
        """Pass the optional keywords pp_model accepts (beatmap_id, network, top_plays), nothing else."""
        f = getattr(self.m, fn, None)
        if f is None:
            return None
        try:
            import inspect
            names = set(inspect.signature(f).parameters)
        except (TypeError, ValueError):
            names = set()
        kw = {k: v for k, v in (extra or {}).items() if k in names and v is not None}
        if "osu_path" in names or not names:
            kw["osu_path"] = osu_path
        return f(md5, mods, rate, **kw)

    def predict_pp(self, md5, mods, rate, osu_path=None, **extra):
        return self._call("predict_pp", md5, mods, rate, osu_path, extra)

    def predict_outcome(self, md5, mods, rate, osu_path=None, **extra):
        return self._call("predict_outcome", md5, mods, rate, osu_path, extra)

    def seed(self, md5, mods, rate, raw, beatmap=None, osu_path=None):
        """Hand the API attributes this run already has to pp_model (saves it ~2 paced API calls per map)."""
        f = getattr(self.m, "seed_api_attrs", None)
        if f is None or not raw:
            return False
        try:
            return bool(f(md5, mods, rate, raw, beatmap=beatmap, osu_path=osu_path))
        except Exception as e:
            _log("seed_api_attrs failed: %s" % e)
            return False

    def flush(self):
        f = getattr(self.m, "flush", None)
        if f is not None:
            try:
                f()
            except Exception as e:
                _log("pp_model flush failed: %s" % e)


class FallbackPredictor:
    """Used only while pp_model is missing / untrained: your own recent results on maps of similar star rating
    (recency-weighted 20/50/80th percentiles of accuracy and misses, misses scaled to the map's length) plus a
    pass-chance curve around your comfort level that falls with drain length; pp from the recommender's calc.
    Everything it says is 'low' confidence."""

    uses_ref = True      # gets the recommender's calc reference (path or "bid:<id>") as osu_path
    family = "fallback2"

    def __init__(self, calc, comfort, history=None, now=None):
        self.calc, self.comfort, self.name = calc, comfort, "fallback"
        now = now or time.time()
        self.hist = [(h["stars"], h["acc"], h.get("misses") or 0, 0.5 ** (max(0.0, now - h["ts"]) / 86400.0 / 21.0))
                     for h in (history or []) if h.get("stars") and num(h.get("acc")) is not None]

    def report(self):
        return {"learner": "fallback: your results near this star rating (pp_model not available)",
                "trained_on": len(self.hist), "updated": None, "backtest": None}

    def signature(self):
        return "fallback2|%.2f|%d" % (self.comfort, len(self.hist))

    def _outcome(self, stars, n, mods="NM", length_s=None):
        d = stars - self.comfort
        p = crude_p_pass(stars, self.comfort)
        if length_s:
            p = p ** max(0.6, float(length_s) / 150.0)          # longer maps: more chances to fail
        rows = []
        for width in (0.35, 0.6, 1.0):
            rows = [r for r in self.hist if abs(r[0] - stars) <= width]
            if sum(r[3] for r in rows) >= 3.0 and len(rows) >= 6:
                break
        if len(rows) >= 4:
            ws = [r[3] for r in rows]
            a = [wquant([r[1] for r in rows], ws, q) for q in (0.2, 0.5, 0.8)]
            scale = (n or 500) / 500.0
            mm = [wquant([r[2] for r in rows], ws, q) * scale for q in (0.2, 0.5, 0.8)]
            basis = "your %d plays within %.2g* of %.2f*" % (len(rows), width, stars)
        else:
            a50 = max(0.80, min(0.985, 0.952 - 0.035 * d))
            a = [a50 - 0.02, a50, min(0.995, a50 + 0.012)]
            m50 = max(0.0, (n or 600) * 0.005 * math.exp(1.1 * d))
            mm = [m50 * 0.4, m50, m50 * 2.0 + 1]
            basis = "rough star curve around your comfort level"
        pen = (0.015 if "HR" in mods else 0.0) + (0.02 if "DT" in mods else 0.0)
        a = [max(0.5, min(1.0, x - pen)) for x in a]
        return {"p_pass": p, "acc": {"p20": a[0], "p50": a[1], "p80": a[2]},
                "misses": {"p20": int(round(mm[0])), "p50": int(round(mm[1])), "p80": int(round(mm[2]))},
                "n_similar": len(rows) if len(rows) >= 4 else 0, "basis": basis}

    def predict_outcome(self, md5, mods, rate, osu_path=None, length_s=None):
        ss = self.calc(osu_path, mods, rate) if osu_path else None
        if not ss or ss.get("stars") is None:
            return None
        o = self._outcome(ss["stars"], ss.get("n_objects"), mods, length_s)
        o["confidence"] = "low"
        return o

    def predict_pp(self, md5, mods, rate, osu_path=None, length_s=None):
        if not osu_path:
            return None
        ss = self.calc(osu_path, mods, rate)
        if not ss or ss.get("pp") is None or ss.get("stars") is None:
            return None
        o = self._outcome(ss["stars"], ss.get("n_objects"), mods, length_s)
        mc = ss.get("max_combo")

        def pp(acc, miss):
            r = self.calc(osu_path, mods, rate, acc=acc, misses=int(miss), combo=est_combo(mc, miss))
            return r.get("pp") if r else None
        lo = pp(o["acc"]["p20"], o["misses"]["p80"])
        mid = pp(o["acc"]["p50"], o["misses"]["p50"])
        hi = pp(o["acc"]["p80"], o["misses"]["p20"])
        fc = pp(o["acc"]["p50"], 0)
        if mid is None:
            return None
        return {"pp_expected": mid, "pp_low": lo if lo is not None else mid, "pp_high": hi if hi is not None else mid,
                "pp_fc": fc, "pp_ss": ss["pp"], "p_pass": o["p_pass"], "stars": ss["stars"],
                "confidence": "low", "basis": o["basis"] + " (pp_model not available)", "n_similar": o["n_similar"],
                "model_error_pp": None, "acc_p50": o["acc"]["p50"], "misses_p50": o["misses"]["p50"]}


def default_predictor(calc, comfort):
    try:
        sys.path.insert(0, str(HERE))
        import pp_model  # noqa
        if hasattr(pp_model, "predict_pp"):
            return ModelPredictor(pp_model)
    except Exception as e:
        _log("pp_model unavailable: %s: %s" % (type(e).__name__, e))
    return FallbackPredictor(calc, comfort)


def default_calc():
    """-> calc(path, mods, rate, acc=None, misses=None, combo=None) via pp_live (tosu), or None."""
    try:
        sys.path.insert(0, str(HERE))
        import pp_live
        if not pp_live.tosu_available(timeout=0.6):
            return None
    except Exception:
        return None

    def calc(path, mods, rate, acc=None, misses=None, combo=None):
        if acc is None:
            acc, misses = 100, 0
        return pp_live.calc_pp(path, mods, rate, acc=acc, misses=misses, combo=combo)
    return calc


# ----------------------------------------------------------------------------
# osu! API source
# ----------------------------------------------------------------------------

class ApiSource:
    """Thin wrapper over osu_api.Api (or a test fake with .get(path, params))."""

    def __init__(self, api):
        self.api = api
        self.requests = 0
        self.error = None

    def _get(self, path, params):
        self.requests += 1
        return self.api.get(path, params)

    def post(self, path, body):
        self.requests += 1
        return self.api.post(path, body)

    def beatmaps(self, ids):
        js = self._get("/beatmaps", {"ids[]": [int(i) for i in ids]})
        return (js or {}).get("beatmaps") or []

    def lookup(self, checksum):
        try:
            return self._get("/beatmaps/lookup", {"checksum": checksum})
        except Exception as e:
            if getattr(e, "status", None) == 404 or "404" in str(e):
                return {}
            raise

    def search(self, lo, hi, cursor=None):
        p = {"m": 0, "s": "ranked", "sort": "plays_desc", "q": "stars>=%.2f stars<=%.2f" % (lo, hi)}
        if cursor:
            p["cursor_string"] = cursor
        return self._get("/beatmapsets/search", p) or {}


def default_api():
    try:
        sys.path.insert(0, str(HERE))
        import osu_api
        cfg, err = osu_api.load_config()
        if cfg is None:
            return None, err
        return ApiSource(osu_api.Api(cfg, budget=API_BUDGET + 20)), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def default_fetcher():
    state = {"last": 0.0}

    def fetch(bid):
        dt = time.monotonic() - state["last"]
        if dt < FETCH_INTERVAL_S:
            time.sleep(FETCH_INTERVAL_S - dt)
        state["last"] = time.monotonic()
        req = urllib.request.Request("https://osu.ppy.sh/osu/%d" % int(bid), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(4_000_000)
    return fetch


def bm_status(bm):
    st = bm.get("status")
    if isinstance(st, str):
        return st.lower()
    rs = bm.get("ranked")
    return {1: "ranked", 2: "approved", 3: "qualified", 4: "loved", 0: "pending", -1: "wip",
            -2: "graveyard"}.get(rs, "unknown")


def status_entry(bm, now):
    bs = bm.get("beatmapset") or {}
    return {"st": bm_status(bm), "md5": bm.get("checksum") or "", "ts": int(now),
            "sid": bm.get("beatmapset_id") or bs.get("id"), "mode": bm.get("mode_int", 0 if bm.get("mode") in
                                                                            (None, "osu") else -1),
            "a": bs.get("artist") or "", "t": bs.get("title") or "", "c": bs.get("creator") or "",
            "v": bm.get("version") or "", "stars": num(bm.get("difficulty_rating")), "bpm": num(bm.get("bpm")),
            "ar": num(bm.get("ar")), "od": num(bm.get("accuracy")), "cs": num(bm.get("cs")),
            "len": bm.get("hit_length") or bm.get("total_length"),
            "n": sum(int(bm.get(k) or 0) for k in ("count_circles", "count_sliders", "count_spinners")) or None,
            "nc": bm.get("count_circles"), "ns": bm.get("count_sliders"), "nsp": bm.get("count_spinners"),
            "mc": bm.get("max_combo")}


# ----------------------------------------------------------------------------
# pp from difficulty attributes (used when tosu can't calculate arbitrary maps)
# ----------------------------------------------------------------------------
# tosu v4.26 ignores /api/calculate/pp?path=... and always answers for the map selected in game (checked
# 2026-11-10: three different files and a nonexistent path all returned the selected map). So for maps that are
# not selected, difficulty attributes come from the osu! API (POST /beatmaps/{id}/attributes, the live server's
# values) and pp from the osu!standard performance formula below.  The formula reproduces tosu's rosu-pp output
# to ~1-2% given the same attributes (test_pp_recommend checks it against numbers captured from tosu); the live
# server's pp differs from that version (newer difficulty algorithm), so a scale factor is fitted on your real
# top-100 scores (API pp) - see ApiCalc.calibrate.

def _dtp(d):
    return (5.0 * max(1.0, d / 0.0675) - 4.0) ** 3 / 100000.0


def _miss_penalty(miss, strain_count):
    if not strain_count or strain_count <= 1:
        return 0.96 / (miss / 4.0 + 1)
    return 0.96 / ((miss / (4.0 * math.log(strain_count) ** 0.94)) + 1)


def hit_counts(total, acc, misses):
    """(n300, n100, n50) for a score of `acc` (0-1) with `misses`, 100s before 50s."""
    misses = max(0, min(int(misses), total))
    rest = total - misses
    target = acc * 300.0 * total
    n100 = (rest * 300.0 - target) / 200.0
    n50 = 0.0
    if n100 > rest:                       # can't reach that acc with 100s alone
        n100, n50 = 0.0, 0.0
        n50 = max(0.0, min(rest, (rest * 300.0 - target) / 250.0))
        n100 = 0.0
    n100 = max(0.0, min(rest, n100))
    return rest - n100 - n50, n100, n50


def adj_ar_od(ar, od, mods, rate):
    if "HR" in mods:
        ar, od = min(10.0, ar * 1.4), min(10.0, od * 1.4)
    if "EZ" in mods:
        ar, od = ar / 2, od / 2
    if abs(rate - 1.0) > 1e-6:
        pre = 1200 + 600 * (5 - ar) / 5 if ar < 5 else 1200 - 750 * (ar - 5) / 5
        pre /= rate
        ar = 5 + (1200 - pre) * 5 / 750 if pre <= 1200 else 5 - (pre - 1200) * 5 / 600
        od = (80 - (80 - 6 * od) / rate) / 6
    return ar, od


def pp_formula(at, nc, ns, nsp, ar, od, mods, acc=1.0, misses=0, combo=None, classic=False, speed_dev_nerf=1.0):
    """osu!standard pp from difficulty attributes (rosu-pp 2025 structure).  at: API attribute names
    (aim_difficulty, speed_difficulty, speed_note_count, aim_difficult_strain_count, speed_difficult_strain_count,
    slider_factor, max_combo).  ar/od already mod-adjusted.  acc 0-1."""
    total = int(nc + ns + nsp)
    if total <= 0:
        return None
    acc = max(0.0, min(1.0, acc))
    n300, n100, n50 = hit_counts(total, acc, misses)
    misses = max(0, int(misses))
    mc = at.get("max_combo") or total
    eff = float(misses)
    if combo is not None and ns:
        thr = mc - 0.1 * ns
        if combo < thr:
            eff = max(eff, min(thr / max(1.0, combo), n100 + n50 + misses))
    lb = 0.95 + 0.4 * min(1.0, total / 2000.0) + (math.log10(total / 2000.0) * 0.5 if total > 2000 else 0.0)
    hd = "HD" in mods
    # aim
    aim = _dtp(at.get("aim_difficulty") or 0.0) * lb
    if eff > 0:
        aim *= _miss_penalty(eff, at.get("aim_difficult_strain_count"))
    arf = 0.3 * (ar - 10.33) if ar > 10.33 else (0.05 * (8.0 - ar) if ar < 8.0 else 0.0)
    aim *= 1 + arf * lb
    if hd:
        aim *= 1 + 0.04 * (12.0 - ar)
    aim *= acc
    # speed
    speed = _dtp(at.get("speed_difficulty") or 0.0) * lb
    if eff > 0:
        speed *= _miss_penalty(eff, at.get("speed_difficult_strain_count"))
    speed *= 1 + (0.3 * (ar - 10.33) if ar > 10.33 else 0.0) * lb
    if hd:
        speed *= 1 + 0.04 * (12.0 - ar)
    snc = at.get("speed_note_count") or 0.0
    rel = 0.0
    if snc > 0:
        diff = max(0.0, total - snc)
        g = max(0.0, n300 - diff)
        o = max(0.0, n100 - max(0.0, diff - n300))
        m = max(0.0, n50 - max(0.0, diff - n300 - n100))
        rel = (g * 6 + o * 2 + m) / (snc * 6)
    speed *= ((acc + rel) / 2.0) ** ((14.5 - max(od, 8.5)) / 2.0) * speed_dev_nerf
    speed *= 0.99 ** max(0.0, n50 - total / 500.0)
    # accuracy
    amount = nc if classic else nc + ns
    if amount > 0:
        bacc = max(0.0, ((n300 - (total - amount)) * 6 + n100 * 2 + n50) / (amount * 6.0))
    else:
        bacc = 0.0
    accv = 1.52163 ** od * bacc ** 24 * 2.83 * min(1.15, (amount / 1000.0) ** 0.3)
    if hd:
        accv *= 1.08
    tot = (aim ** 1.1 + speed ** 1.1 + accv ** 1.1) ** (1 / 1.1) * 1.14
    return {"pp": tot, "aim": aim, "speed": speed, "acc": accv, "eff_miss": eff}


def _api_mods(mods, rate):
    out = []
    for a in ("HD", "HR", "DT"):
        if a in mods:
            m = {"acronym": a}
            if a == "DT" and abs(rate - 1.5) > 1e-6:
                m["settings"] = {"speed_change": rate}
            out.append(m)
    return out


class ApiCalc:
    """calc(ref, mods, rate, acc=None, misses=None, combo=None) like tosu's, from osu! API attributes.
    ref: an .osu path registered with register() or "bid:<id>".  Attributes are cached per (bid, mods, rate) in
    the recommender cache ('api_attrs'); `fetch_ok` gates new API requests (budget / time)."""

    def __init__(self, api, cache, status, now=None):
        self.api, self.cache, self.status = api, cache, status
        self.now = now or time.time()
        self.cache.setdefault("api_attrs", {})
        self.refs = {}
        self.fetch_ok = lambda: True
        self.requests = 0
        self.k = num((cache.get("calib") or {}).get("k")) or 1.0

    def register(self, ref, bid, meta=None):
        if ref and bid:
            self.refs[str(ref)] = (int(bid), meta or {})

    def _bid(self, ref):
        ref = str(ref or "")
        if ref.startswith("bid:"):
            return int(ref[4:]), {}
        return self.refs.get(ref, (None, {}))

    def meta(self, bid, extra=None):
        e = self.status["bid"].get(str(bid)) or {}
        m = dict(extra or {})
        for k in ("nc", "ns", "nsp", "ar", "od", "cs", "bpm", "mc"):
            if e.get(k) is not None:
                m[k] = e[k]
        return m

    def attrs(self, bid, mods, rate):
        # HD changes only the displayed star rating server-side (aim/speed attributes are identical, checked
        # 2026-11-10), and the formula adds the HD bonus itself - so HD variants reuse the core attributes
        key = "%d|%s|%.2f" % (bid, "".join(a for a in ("HR", "DT") if a in mods) or "NM", rate)
        a = self.cache["api_attrs"].get(key)
        if a is not None:
            return a or None
        if not self.api or not self.fetch_ok():
            return None
        try:
            js = self.api.post("/beatmaps/%d/attributes" % bid, {"mods": _api_mods(mods.replace("HD", ""), rate),
                                                                  "ruleset": "osu"})
        except Exception as e:
            _log("attributes %s failed: %s" % (key, e))
            return None
        self.requests += 1
        a = (js or {}).get("attributes") or {}
        self.cache["api_attrs"][key] = a or {}
        return a or None

    def raw(self, bid, mods, rate, acc=1.0, misses=0, combo=None, meta=None, classic=False):
        at = self.attrs(bid, mods, rate)
        m = self.meta(bid, meta)
        if not at or m.get("nc") is None or m.get("od") is None or m.get("ar") is None:
            return None, at, m
        ar, od = adj_ar_od(float(m["ar"]), float(m["od"]), mods, rate)
        r = pp_formula(at, int(m["nc"]), int(m.get("ns") or 0), int(m.get("nsp") or 0), ar, od, mods, acc, misses,
                       combo, classic=classic)
        return r, at, dict(m, ar_adj=ar, od_adj=od)

    def __call__(self, ref, mods, rate, acc=None, misses=None, combo=None):
        bid, meta = self._bid(ref)
        if not bid:
            return None
        a = 1.0 if acc is None else (acc / 100.0 if acc > 1 else acc)
        r, at, m = self.raw(bid, mods, rate, a, misses or 0, combo, meta)
        if not r:
            return None
        n = int(m["nc"]) + int(m.get("ns") or 0) + int(m.get("nsp") or 0)
        bpm = num(m.get("bpm"))
        return {"pp": r["pp"] * self.k, "stars": num(at.get("star_rating")), "aim": r["aim"] * self.k,
                "speed": r["speed"] * self.k, "max_combo": at.get("max_combo") or m.get("mc"), "n_objects": n,
                "ar": round(m["ar_adj"], 2), "od": round(m["od_adj"], 2), "cs": m.get("cs"),
                "bpm": bpm * rate if bpm else None, "source": "api+formula"}

    def calibrate(self, top, max_req=16):
        """Fit k = median(real pp / formula pp) on your top plays without misses (stable scores -> classic
        accuracy).  Cached for 30 days."""
        cal = self.cache.get("calib") or {}
        if cal.get("k") and self.now - (cal.get("ts") or 0) < 30 * 86400:
            self.k = float(cal["k"])
            return self.k
        plays = [t for t in top if num(t.get("pp")) and t.get("beatmap_id") and not t.get("misses")
                 and num(t.get("acc"))][:max_req]
        need = [int(t["beatmap_id"]) for t in plays if str(t["beatmap_id"]) not in self.status["bid"]
                or self.status["bid"][str(t["beatmap_id"])].get("nc") is None]
        if need and self.api:
            try:
                for bm in self.api.beatmaps(need[:50]):
                    if bm.get("id"):
                        self.status["bid"][str(bm["id"])] = status_entry(bm, self.now)
                self.requests += 1
            except Exception as e:
                _log("calibration lookup failed: %s" % e)
        ratios = []
        for t in plays:
            ms = str(t.get("mods") or "NM")
            rate = 1.5 if ("DT" in ms or "NC" in ms) else 1.0
            r, _, _ = self.raw(int(t["beatmap_id"]), ms, rate, float(t["acc"]), 0, None, classic=True)
            if r and r["pp"] > 0:
                ratios.append(float(t["pp"]) / r["pp"])
        if len(ratios) >= 5:
            ratios.sort()
            k = ratios[len(ratios) // 2]
            spread = (ratios[int(len(ratios) * 0.8)] - ratios[int(len(ratios) * 0.2)]) / 2
            self.cache["calib"] = {"k": round(k, 4), "n": len(ratios), "spread": round(spread, 4),
                                   "ts": int(self.now)}
            self.k = k
        return self.k


# ----------------------------------------------------------------------------
# context (everything local, read-only)
# ----------------------------------------------------------------------------

def _style_of(f):
    if not isinstance(f, dict) or "jf" not in f:
        return None
    if (f.get("sf") or 0) >= 0.08 or (f.get("bf") or 0) >= 0.28:
        return "stream"
    if (f.get("jf") or 0) >= 0.35:
        return "jump"
    return "mixed"


STYLE_WORD = {"stream": "Stream-heavy", "jump": "Jump-heavy", "mixed": "Mixed"}
STYLE_PLURAL = {"stream": "stream maps", "jump": "jump maps", "mixed": "mixed maps"}


class Ctx:
    def __init__(self):
        self.now = time.time()
        self.top = []
        self.top_pps = []
        self.top_by_bid, self.top_by_md5 = {}, {}
        self.total_pp = None
        self.rank = None
        self.lib = {}
        self.paths = {}
        self.nm_stars = {}
        self.attempts = []
        self.history = []       # [{ts, md5, mods, rate, acc, misses, passed, stars}]
        self.climb = []
        self.comfort = 4.5
        self.comfort_basis = "default"
        self.tail = None           # weighted pp below #100 (weighted_gain tail correction)
        self.recent_stars = None   # median star rating of your recent passes (last 30 days)

    def set_top(self, top):
        self.top = [t for t in top or [] if isinstance(t, dict) and num(t.get("pp")) is not None]
        self.top_pps = [float(t["pp"]) for t in self.top]
        self.top_by_bid = {}
        self.top_by_md5 = {}
        for t in self.top:
            if t.get("beatmap_id"):
                self.top_by_bid[int(t["beatmap_id"])] = max(float(t["pp"]), self.top_by_bid.get(int(t["beatmap_id"]), 0))
            if t.get("md5"):
                self.top_by_md5[t["md5"]] = max(float(t["pp"]), self.top_by_md5.get(t["md5"], 0))

    def existing(self, bid, md5):
        v = None
        if bid and int(bid) in self.top_by_bid:
            v = self.top_by_bid[int(bid)]
        if md5 and md5 in self.top_by_md5:
            v = max(v or 0, self.top_by_md5[md5])
        return v


def load_context(d: Path, predictor_report=None, now=None) -> Ctx:
    c = Ctx()
    c.now = now or time.time()
    ppd = load_json(d / "pp_data.json", {}) or {}
    u = ppd.get("user") or {}
    c.total_pp, c.rank = num(u.get("pp")), u.get("global_rank")
    c.set_top(ppd.get("top_plays") or [])
    c.tail = tail_pp(c.total_pp, c.top_pps) if c.total_pp is not None and len(c.top_pps) >= 100 else None
    c.climb = [x for x in (ppd.get("climb") or []) if isinstance(x, dict)]
    lib = (load_json(d / ".coach_library.json", {}) or {}).get("maps") or {}
    c.lib = {m: f for m, f in lib.items() if isinstance(f, dict) and f.get("mode") == 0}
    osu = (load_json(d / ".osu_coach_cache.json", {}) or {}).get("osu") or {}
    c.paths = {v: k for k, v in osu.items() if v}
    sr = (load_json(d / ".coach_api_sr.json", {}) or {}).get("sr") or {}
    for m, v in sr.items():
        if num(v):
            c.nm_stars[m] = float(v)
    src = (load_json(d / ".coach_sr_cache.json", {}) or {}).get("sr") or {}
    for k, v in src.items():
        if k.endswith("|NM|1.00") and num(v):
            c.nm_stars.setdefault(k.split("|", 1)[0], float(v))
    c.attempts = _read_attempts(d)
    c.history = _history(d, c)
    c.comfort, c.comfort_basis = comfort_level(c, predictor_report)
    rec = sorted(h["stars"] for h in c.history if h.get("stars") and h.get("passed", True)
                 and c.now - (h.get("ts") or 0) < 30 * 86400)
    c.recent_stars = rec[len(rec) // 2] if len(rec) >= 8 else None
    return c


def _read_attempts(d):
    p = Path(os.environ.get("OSU_COACH_ATTEMPTS_FILE") or (d / "live_attempts.jsonl"))
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("md5"):
                    r.pop("hit_errors", None)
                    out.append(r)
    except OSError:
        pass
    return out


def _history(d, c):
    """Your plays with stars: local replays (pp_live's player rule) + live passes."""
    out = []
    recs = []
    try:
        if Path(d).resolve() == data_dir().resolve():
            sys.path.insert(0, str(HERE))
            import pp_live
            recs = pp_live._player_records()
    except Exception:
        recs = []
    for r in recs:
        nm = c.nm_stars.get(r.get("md5"))
        rate = num(r.get("rate")) or 1.0
        mods = str(r.get("mods") or "NM")
        out.append({"ts": r["ts"], "md5": r["md5"], "acc": r["acc"], "misses": r.get("misses", 0), "passed": True,
                    "stars": rough_stars(nm, mods, rate) if nm else None, "src": "replay"})
    for a in c.attempts:
        if a.get("outcome") != "passed" or num(a.get("acc")) is None:
            continue
        if "AT" in str(a.get("mods") or "") or "RX" in str(a.get("mods") or ""):
            continue
        out.append({"ts": num(a.get("ts")) or 0, "md5": a["md5"], "acc": float(a["acc"]),
                    "misses": int(a.get("misses") or 0), "passed": True, "stars": num(a.get("stars")), "src": "live"})
    return out


def comfort_level(c, rep=None):
    """Your comfortable star level now: pp_model's, else recency-weighted median of recent clean-ish passes."""
    if isinstance(rep, dict) and num(rep.get("comfort_stars")):
        return float(rep["comfort_stars"]), "model"
    pts = [(h["stars"], 0.5 ** (max(0.0, c.now - h["ts"]) / 86400.0 / 21.0)) for h in c.history
           if h.get("stars") and h["acc"] >= 0.88 and c.now - h["ts"] < 90 * 86400]
    if len(pts) >= 5:
        v = wquant([p for p, _ in pts], [w for _, w in pts], 0.6)
        return round(v, 2), "your last %d passes (60th pct, 21-day half-life)" % len(pts)
    rec = [float(t["stars"]) for t in c.top if num(t.get("stars")) and str(t.get("date") or "") >=
           datetime.fromtimestamp(c.now - 180 * 86400).strftime("%Y-%m-%d")]
    if len(rec) >= 3:
        return round(sorted(rec)[len(rec) // 2], 2), "recent top plays"
    allt = sorted(float(t["stars"]) for t in c.top if num(t.get("stars")))
    if len(allt) >= 3:   # older top plays: still a better guess of the player's level than a fixed number
        return round(allt[len(allt) // 2], 2), "top plays (none from the last 180 days)"
    return 4.5, "default (no recent plays)"


# ----------------------------------------------------------------------------
# similar-map stats (for plain-words explanations)
# ----------------------------------------------------------------------------

class StyleStats:
    def __init__(self, c: Ctx):
        self.rows = []
        for h in c.history:
            st = _style_of(c.lib.get(h["md5"]))
            if st and h.get("stars"):
                w = 0.5 ** (max(0.0, c.now - h["ts"]) / 86400.0 / 30.0)
                self.rows.append((st, h["stars"], h["acc"], h["misses"], w))
        self.by_style = {}
        for st in STYLE_WORD:
            rs = [r for r in self.rows if r[0] == st]
            if len(rs) >= 3:
                tw = sum(r[4] for r in rs)
                self.by_style[st] = sum(r[2] * r[4] for r in rs) / tw if tw > 0 else None
        cands = [(v, k) for k, v in self.by_style.items() if v is not None]
        self.best = max(cands)[1] if len(cands) >= 2 else None
        self.worst = min(cands)[1] if len(cands) >= 2 else None

    def similar(self, style, stars, width=0.6):
        rs = [r for r in self.rows if r[0] == style and abs(r[1] - stars) <= width]
        if len(rs) < 3:
            return None
        tw = sum(r[4] for r in rs)
        if tw <= 0:
            return None
        return {"n": len(rs), "acc": sum(r[2] * r[4] for r in rs) / tw,
                "misses": wquant([r[3] for r in rs], [r[4] for r in rs], 0.5)}


# ----------------------------------------------------------------------------
# the recommender
# ----------------------------------------------------------------------------

class Recommender:
    def __init__(self, d=None, mode="auto", api="default", predictor=None, calc="default", fetcher="default",
                 clock=time.monotonic, now=None, deadline_s=None, throttle=None, offline=False):
        self.d = Path(d) if d else data_dir()
        self.clock = clock
        self.t0 = clock()
        self.paused_s = 0.0
        self.no_pause = clock is not time.monotonic     # tests drive their own clock: never sleep there
        self.now = now or time.time()
        self.cache = load_json(self.d / CACHE_NAME, {}) or {}
        if self.cache.get("v") != 1:
            self.cache = {"v": 1}
        for k in ("preds", "osu"):
            self.cache.setdefault(k, {})
        self.cache.pop("attrs", None)
        self.status = load_json(self.d / STATUS_NAME, {}) or {}
        if self.status.get("v") != 1:
            self.status = {"v": 1}
        for k in ("bid", "md5", "search"):
            self.status.setdefault(k, {})
        self.status.setdefault("fetch", {"day": "", "n": 0})
        if mode == "auto":
            last = num(self.cache.get("last_full")) or 0
            prev = load_json(self.d / OUT_NAME, None)
            mode = "full" if (self.now - last > FULL_TTL_S or not prev or prev.get("partial")) else "incremental"
        self.mode = mode
        self.deadline = deadline_s if deadline_s is not None else (FULL_DEADLINE_S if mode == "full" else INC_DEADLINE_S)
        self.offline = offline
        if calc == "default":
            self.calc, self.calc_kind = default_calc(), "tosu"
            if self.calc is None:
                self.calc_kind = "api"
        elif calc == "api":
            self.calc, self.calc_kind = None, "api"
        else:
            self.calc, self.calc_kind = calc, ("custom" if calc else None)
        self.api_err = None
        if api == "default":
            api, self.api_err = (None, "offline") if offline or mode != "full" else default_api()
        self.api = api
        self.fetcher = (default_fetcher() if fetcher == "default" else fetcher) if not offline else None
        self._pred_in = predictor
        self.throttle = throttle or Throttle()
        self.notes = []
        self.counts = {"maps": 0, "variants_in_band": 0, "attrs_new": 0, "attrs_cached": 0, "preds_new": 0,
                       "preds_cached": 0, "preds_rejected": 0, "status_lookups": 0, "osu_fetched": 0,
                       "unknown_status": 0, "not_ranked": 0, "no_file": 0, "calc_calls": 0, "api_attr_calls": 0,
                       "extrapolated_only": 0}
        self.api_calc = None
        self.attrs = self.cache.setdefault("attrs_%s" % self.calc_kind, {})
        if self.calc_kind == "api":
            self._use_api_calc()
        self.partial = False
        self._last_flush = clock()

    def _use_api_calc(self):
        self.api_calc = ApiCalc(self.api, self.cache, self.status, self.now)
        self.api_calc.fetch_ok = lambda: (self.left() > 12 * min(1.0, self.deadline / FULL_DEADLINE_S) and
                                          (getattr(self.api, "requests", 0) < API_BUDGET))
        self.calc, self.calc_kind = self.api_calc, "api"
        self.attrs = self.cache.setdefault("attrs_api", {})

    def check_tosu(self, maps):
        """tosu builds that ignore ?path= answer for the selected map: compare two owned maps whose (API) star
        ratings differ by >= 1*.  Verdict cached for 6 h."""
        chk = self.cache.get("tosu_check") or {}
        if self.now - (chk.get("ts") or 0) < 6 * 3600 and "ok" in chk:
            return chk["ok"]
        cands = sorted((m for m in maps.values() if m["owned"] and m.get("path") and m.get("nm_stars")),
                       key=lambda m: m["nm_stars"])
        if len(cands) < 2 or cands[-1]["nm_stars"] - cands[0]["nm_stars"] < 1.0:
            return True
        pick = [cands[len(cands) // 4], cands[(3 * len(cands)) // 4]]
        if pick[1]["nm_stars"] - pick[0]["nm_stars"] < 1.0:
            pick = [cands[0], cands[-1]]
        ok = True
        res = []
        for m in pick:
            r = self._calc(m["path"], "NM", 1.0)
            if not r or r.get("stars") is None or abs(r["stars"] - m["nm_stars"]) > 0.4:
                ok = False
            res.append(r and r.get("stars"))
        self.cache["tosu_check"] = {"ok": ok, "ts": int(self.now), "seen": res,
                                    "want": [pick[0]["nm_stars"], pick[1]["nm_stars"]]}
        if not ok:
            _log("tosu path calc unreliable: wanted %s* got %s*" % (self.cache["tosu_check"]["want"], res))
        return ok

    def ref_of(self, m):
        if self.calc_kind == "api":
            return ("bid:%d" % m["beatmap_id"]) if m.get("beatmap_id") else None
        return m.get("path")

    # -- time -----------------------------------------------------------------
    def left(self):
        return self.deadline - (self.clock() - self.t0 - self.paused_s)

    def _playing(self):
        """The pet keeps PLAYING_FLAG fresh (touched every few s) while a map is being played."""
        try:
            return time.time() - (self.d / PLAYING_FLAG).stat().st_mtime < PLAYING_FLAG_FRESH_S
        except OSError:
            return False

    def pause_if_playing(self):
        """Sleep between steps while you play a map (time paused doesn't count against the deadline); gives up
        after PAUSE_MAX_S in total (the run then ends as partial and the next one resumes)."""
        if self.no_pause or not self._playing():
            return True
        _log("map being played - pausing the refresh")
        while self._playing() and self.paused_s < PAUSE_MAX_S:
            t = time.monotonic()
            time.sleep(PAUSE_STEP_S)
            self.paused_s += time.monotonic() - t
        return self.paused_s < PAUSE_MAX_S

    def out_of_time(self, reserve=0.0):
        """reserve is given for the full 170 s budget and scaled down for shorter deadlines."""
        if not self.pause_if_playing():
            self.partial = True
            return True
        reserve = min(reserve, reserve * self.deadline / FULL_DEADLINE_S)
        if self.left() <= reserve:
            self.partial = True
            return True
        return False

    def flush(self, force=False):
        if force or self.clock() - self._last_flush > 15:
            atomic_write_json(self.d / CACHE_NAME, self.cache)
            atomic_write_json(self.d / STATUS_NAME, self.status)
            self._last_flush = self.clock()

    # -- calc (stage A) ------------------------------------------------------
    def _calc(self, path, mods, rate, **kw):
        self.throttle.wait()
        self.counts["calc_calls"] += 1      # tosu or the local formula, whichever calc_kind is active
        try:
            return self.calc(path, mods, rate, **kw)
        except Exception as e:
            _log("calc failed: %s" % e)
            return None

    # -- candidate maps --------------------------------------------------------
    def build_maps(self, c: Ctx):
        maps = {}
        osu_dir = self.d / OSU_DIR_NAME
        for md5, f in c.lib.items():
            nm = c.nm_stars.get(md5)
            path = c.paths.get(md5)
            maps[md5] = {"md5": md5, "beatmap_id": int(f["bid"]) if f.get("bid") else None,
                         "set_id": int(f["sid"]) if f.get("sid") else None, "artist": f.get("a") or "",
                         "title": f.get("t") or "", "version": f.get("v") or "", "mapper": f.get("c") or "",
                         "nm_stars": nm, "length_s": f.get("len"), "bpm": f.get("bpm"), "ar": f.get("ar"),
                         "od": f.get("od"), "cs": f.get("cs"), "n": f.get("n"), "owned": True, "path": path,
                         "feats": f, "src": "library", "status": None, "hint_mods": set()}
        # status from the cache / live attempts
        live_ranked = {}
        for a in c.attempts:
            if a.get("ranked") is not None:
                live_ranked[a["md5"]] = bool(a["ranked"])
        for m in maps.values():
            self._apply_status(m, live_ranked)
        # climb list (ranked by definition: these scores have pp)
        for x in c.climb:
            md5 = x.get("md5")
            if not md5:
                continue
            m = maps.get(md5)
            if m is None:
                m = maps[md5] = self._api_map(md5, x.get("beatmap_id"), x.get("set_id"), x.get("artist"),
                                              x.get("title"), x.get("version"), x.get("mapper") or x.get("creator"),
                                              None, x.get("length_s"), x.get("bpm"), x.get("ar"), x.get("od"),
                                              x.get("cs"), osu_dir, "climb")
                m["nm_stars"] = self._nm_from_climb(x)
            m["status"] = m["status"] or "ranked"
            if x.get("mods"):
                m["hint_mods"].add(str(x["mods"]))
            m["climb"] = True
        # search results cached in the status file
        for q in self.status["search"].values():
            for bid in q.get("bids") or []:
                e = self.status["bid"].get(str(bid))
                if not e or not e.get("md5") or e.get("mode", 0) != 0:
                    continue
                if e["md5"] in maps:
                    continue
                m = maps[e["md5"]] = self._api_map(e["md5"], bid, e.get("sid"), e.get("a"), e.get("t"), e.get("v"),
                                                   e.get("c"), e.get("stars"), e.get("len"), e.get("bpm"),
                                                   e.get("ar"), e.get("od"), e.get("cs"), osu_dir, "search")
                m["status"] = e.get("st")
                m["n"] = e.get("n")
        return maps

    def _maps(self, c):
        maps = self.build_maps(c)
        feats = self.cache.get("feats") or {}
        if feats:
            for m in maps.values():
                if not m.get("feats") and m.get("beatmap_id") and str(m["beatmap_id"]) in feats:
                    m["feats"] = feats[str(m["beatmap_id"])]
        return maps

    def _nm_from_climb(self, x):
        s = num(x.get("stars"))
        if s is None:
            return None
        ms = str(x.get("mods") or "NM")
        rate = num(x.get("rate")) or (1.5 if "DT" in ms or "NC" in ms else 1.0)
        f = rough_stars(1.0, ms, rate)
        return s / f if f else s

    def _api_map(self, md5, bid, sid, a, t, v, cr, stars, length, bpm, ar, od, cs, osu_dir, src):
        p = osu_dir / ("%d.osu" % int(bid)) if bid else None
        ok = self.cache["osu"].get(str(bid)) if bid else None
        path = str(p) if p and p.is_file() and ok else None
        extra = {"md5_file": ok} if path and isinstance(ok, str) and md5 and ok != md5 else {}
        return {**extra, "md5": md5, "beatmap_id": int(bid) if bid else None, "set_id": int(sid) if sid else None,
                "artist": a or "", "title": t or "", "version": v or "", "mapper": cr or "", "nm_stars": stars,
                "length_s": length, "bpm": bpm, "ar": ar, "od": od, "cs": cs, "n": None, "owned": False,
                "path": path, "feats": None, "src": src, "status": None, "hint_mods": set()}

    def _apply_status(self, m, live_ranked):
        e = self.status["bid"].get(str(m["beatmap_id"])) if m.get("beatmap_id") else None
        if e is None and m["md5"] in self.status["md5"]:
            e = self.status["bid"].get(str(self.status["md5"][m["md5"]]))
            if e is None:
                e = {"st": self.status["md5"][m["md5"]], "md5": m["md5"]} if isinstance(
                    self.status["md5"][m["md5"]], str) else None
        if e:
            st = e.get("st")
            if e.get("md5") and e["md5"] != m["md5"] and st in GAIN_STATUSES:
                st = "outdated"
            m["status"] = st
            if m["nm_stars"] is None and e.get("stars") and e.get("md5") == m["md5"]:
                m["nm_stars"] = e["stars"]
            if m.get("set_id") is None and e.get("sid"):
                m["set_id"] = int(e["sid"])
        elif m["md5"] in live_ranked:
            m["status"] = "ranked" if live_ranked[m["md5"]] else "unranked"

    def _status_fresh(self, bid):
        e = self.status["bid"].get(str(bid))
        if not e:
            return False
        ttl = STATUS_TTL_FINAL if e.get("st") in GAIN_STATUSES + ("loved",) else STATUS_TTL_OTHER
        return self.now - (e.get("ts") or 0) < ttl

    # -- variants ------------------------------------------------------------
    def band_center(self, c):
        """comfort, raised to (median stars of your recent passes - 0.2) when you are playing harder maps than
        the comfort estimate says (an improving player: the model's comfort lags)."""
        if c.recent_stars is not None:
            return max(c.comfort, c.recent_stars - 0.2)
        return c.comfort

    def band(self, c):
        cc = self.band_center(c)
        return cc + BAND_LO, cc + BAND_HI

    def variants_of(self, m, c):
        lo, hi = self.band(c)
        out = []
        if m["nm_stars"] is None:
            return out
        for mods, rate in VARIANTS:
            k = vkey(m["md5"], CORE[mods], rate)
            a = self.attrs.get(k)
            s = num(a.get("stars")) if a else None
            if s is None:
                s = rough_stars(m["nm_stars"], mods, rate)
            if lo <= s <= hi or mods in m["hint_mods"]:
                out.append((mods, rate, s))
        return out

    def pre_score(self, m, mods, rate, stars, c):
        """No-model ordering score: rough expected gain (uses the real SS pp once stage A has it)."""
        a = self.attrs.get(vkey(m["md5"], CORE[mods], rate))
        ss = num(a.get("pp")) if a else None
        if ss is None:
            ss = rough_ss_pp(stars, m.get("n") or (a or {}).get("n_objects"))
        elif a and (num(a.get("aim")) or 0) + (num(a.get("speed")) or 0) > 0:
            # SS pp is mostly accuracy pp at high OD (HR: OD ~10 -> 2x the NM SS pp), which a ~78-90% run never
            # gets: order by the aim + speed part (2026-11-10: HR SS pp made 2/3 of the predictions HR/HDHR)
            ss = min(ss, 1.25 * (num(a.get("aim")) + num(a.get("speed"))))
        if "HD" in mods:
            ss *= hd_factor(a.get("ar") if a else m.get("ar"))
        exp = ss * crude_frac(stars, c.comfort)
        g = weighted_gain(c.top_pps, exp, c.existing(m.get("beatmap_id"), m["md5"]), c.tail)
        g_hi = weighted_gain(c.top_pps, ss * min(0.95, crude_frac(stars, c.comfort) + 0.15),
                             c.existing(m.get("beatmap_id"), m["md5"]), c.tail)
        pp_ = crude_p_pass(stars, c.comfort) * MOD_PASS.get(CORE[mods], 1.0)
        ln = num(m.get("length_s"))
        if ln:
            pp_ = pp_ ** max(0.6, ln / rate / 150.0)     # longer maps: more chances to fail (same as the fallback)
        return pp_ * (0.7 * g + 0.3 * g_hi)

    # -- API stages (full refresh only) ---------------------------------------
    def refresh_status(self, maps, c):
        if not self.api:
            return
        lo, hi = self.band(c)
        need, need_md5 = [], []
        for m in maps.values():
            if not m["owned"] or m["status"] in GAIN_STATUSES and m.get("beatmap_id") and \
                    self._status_fresh(m["beatmap_id"]):
                continue
            if not self.variants_of(m, c):
                continue
            if m.get("beatmap_id"):
                if not self._status_fresh(m["beatmap_id"]) or m["status"] is None:
                    need.append(m)
            elif m["md5"] not in self.status["md5"]:
                need_md5.append(m)
        # most promising first, so a budget-limited run looks up what matters
        need.sort(key=lambda m: -max(self.pre_score(m, md, r, s, c) for md, r, s in self.variants_of(m, c)))
        sc = min(1.0, self.deadline / FULL_DEADLINE_S)
        t_end = self.clock() + min(API_STATUS_SHARE * sc, max(0.0, self.left() - 60 * sc))
        live = {a["md5"]: bool(a["ranked"]) for a in c.attempts if a.get("ranked") is not None}
        for i in range(0, len(need), 50):
            if self.clock() > t_end or self.out_of_time(60):
                self.partial = True
                self.notes.append("status lookups stopped early (%d maps left)" % (len(need) - i))
                break
            chunk = need[i:i + 50]
            try:
                bms = self.api.beatmaps([m["beatmap_id"] for m in chunk])
            except Exception as e:
                self.notes.append("status lookup failed: %s" % e)
                _log("status lookup failed: %s" % e)
                break
            self.counts["status_lookups"] += 1
            got = set()
            for bm in bms:
                if not bm.get("id"):
                    continue
                self.status["bid"][str(bm["id"])] = status_entry(bm, self.now)
                got.add(int(bm["id"]))
            for m in chunk:
                if m["beatmap_id"] not in got:   # deleted / never submitted under this id
                    self.status["bid"][str(m["beatmap_id"])] = {"st": "missing", "md5": "", "ts": int(self.now)}
                self._apply_status(m, live)
            self.flush()
        for m in need_md5[:10]:
            if self.out_of_time(60):
                break
            try:
                bm = self.api.lookup(m["md5"])
            except Exception as e:
                self.notes.append("checksum lookup failed: %s" % e)
                break
            self.counts["status_lookups"] += 1
            if bm and bm.get("id"):
                self.status["bid"][str(bm["id"])] = status_entry(bm, self.now)
                self.status["md5"][m["md5"]] = int(bm["id"])
                m["beatmap_id"] = int(bm["id"])
            else:
                self.status["md5"][m["md5"]] = "unranked"
            self._apply_status(m, live)

    def refresh_search(self, maps, c):
        if not self.api:
            return
        lo, hi = c.comfort + SEARCH_LO, c.comfort + SEARCH_HI
        bands = [("nm", lo, hi), ("dt", lo / 1.4, hi / 1.4)]
        for name, a, b in bands:
            key = "%s|%.1f|%.1f" % (name, a, b)
            q = self.status["search"].get(key)
            if q and self.now - (q.get("ts") or 0) < SEARCH_TTL_S:
                continue
            bids, cursor = [], None
            for _page in range(SEARCH_PAGES):
                if self.out_of_time(60):
                    break
                try:
                    js = self.api.search(a, b, cursor)
                except Exception as e:
                    self.notes.append("search failed: %s" % e)
                    break
                for bs in js.get("beatmapsets") or []:
                    for bm in bs.get("beatmaps") or []:
                        if bm.get("mode_int", 0) != 0 or not num(bm.get("difficulty_rating")):
                            continue
                        if not (a - 0.05 <= bm["difficulty_rating"] <= b + 0.05):
                            continue
                        bm = dict(bm)
                        bm.setdefault("beatmapset", bs)
                        bm.setdefault("beatmapset_id", bs.get("id"))
                        self.status["bid"][str(bm["id"])] = status_entry(bm, self.now)
                        bids.append(int(bm["id"]))
                cursor = js.get("cursor_string")
                if not cursor:
                    break
            if bids:
                for k in [k for k in self.status["search"] if k.startswith(name + "|")]:
                    del self.status["search"][k]
                self.status["search"][key] = {"ts": int(self.now), "bids": bids}
        # drop stale search bands (comfort moved)
        self.flush()

    def fetch_osu(self, ranked_cands):
        """.osu text for the best not-owned candidates -> .osu_cache/{bid}.osu (verified by md5)."""
        if not self.fetcher:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        fs = self.status["fetch"]
        if fs.get("day") != day:
            fs.update({"day": day, "n": 0})
        osu_dir = self.d / OSU_DIR_NAME
        done = 0
        for m in ranked_cands:
            if m["owned"] or m.get("path") or not m.get("beatmap_id"):
                continue
            if str(m["beatmap_id"]) in self.cache["osu"] and self.cache["osu"][str(m["beatmap_id"])] is False:
                continue      # fetched before, didn't match / not a std map
            if fs["n"] >= FETCH_PER_DAY or done >= FETCH_PER_RUN:
                self.notes.append(".osu download cap reached (%d today)" % fs["n"])
                break
            if self.out_of_time(45):
                break
            try:
                text = self.fetcher(m["beatmap_id"])
            except Exception as e:
                self.notes.append("osu download failed: %s" % e)
                _log("fetch %s failed: %s" % (m["beatmap_id"], e))
                break
            fs["n"] += 1
            done += 1
            self.counts["osu_fetched"] += 1
            raw = text.encode("utf-8") if isinstance(text, str) else (text or b"")
            if not raw.lstrip(BOM).lstrip().startswith(b"osu file format"):
                self.cache["osu"][str(m["beatmap_id"])] = False
                continue
            md5 = hashlib.md5(raw).hexdigest()
            if m["md5"] and md5 != m["md5"]:
                # the API checksum is of the exact bytes; a mismatch means the map changed since the lookup
                _log("md5 mismatch for %s (%s vs %s) - using file md5" % (m["beatmap_id"], md5, m["md5"]))
                m["md5_file"] = md5
            osu_dir.mkdir(exist_ok=True)
            p = osu_dir / ("%d.osu" % m["beatmap_id"])
            tmp = p.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, p)
            self.cache["osu"][str(m["beatmap_id"])] = m.get("md5_file") or m["md5"]
            m["path"] = str(p)
            feats = _features_of(raw)
            if feats:
                m["feats"] = feats
                self.cache.setdefault("feats", {})[str(m["beatmap_id"])] = feats
            self.flush()

    # -- stage A ---------------------------------------------------------------
    def stage_attrs(self, vlist, c, limit):
        if not self.calc:
            self.notes.append("tosu not reachable: only cached map calculations used")
            return
        n = 0
        for m, mods, rate, _s, _p in vlist:
            core = CORE[mods]
            k = vkey(m["md5"], core, rate)
            if k in self.attrs:
                continue
            ref = self.ref_of(m)
            if not ref:
                continue
            # leave ~45% of the time for the model predictions (stage B)
            if n >= limit or self.out_of_time(0.45 * FULL_DEADLINE_S if self.mode == "full" else 8):
                self.partial = self.partial or n >= limit
                break
            r = self._calc(ref, core, rate)
            n += 1
            self.counts["attrs_new"] += 1
            if r is None and self.calc_kind == "api" and not self.api_calc.fetch_ok():
                n -= 1
                self.counts["attrs_new"] -= 1
                self.partial = True
                break                    # out of API budget / time: not a failure, try again next refresh
            if r and r.get("pp") is not None:
                self.attrs[k] = {kk: r.get(kk) for kk in ("pp", "stars", "aim", "speed", "max_combo",
                                                                  "n_objects", "ar", "od", "cs", "bpm")}
                self.attrs[k]["ts"] = int(self.now)
            else:
                self.attrs[k] = {"pp": None, "ts": int(self.now)}
            self.flush()

    # -- stage B ---------------------------------------------------------------
    def stage_preds(self, vlist, c, limit, sig, pred):
        # predictions of the same family may be reused for a few hours after the model was refit (full mode)
        fam = getattr(pred, "family", pred.name)
        n = 0
        for m, mods, rate, _s, _p in vlist:
            ref = self.ref_of(m)
            if not ref:
                continue
            md5 = m.get("md5_file") or m["md5"]
            k = vkey(md5, mods, rate)
            e = self.cache["preds"].get(k)
            fresh = e and (e.get("sig") == sig or (
                self.mode == "full" and e.get("p") is not None and e.get("pred") == fam and
                str(e.get("sig")).rsplit("|", 1)[-1] == sig.rsplit("|", 1)[-1] and
                self.now - (e.get("ts") or 0) < PRED_STALE_S))
            if e and (fresh or n >= limit):
                self.counts["preds_cached"] += 1
                continue
            if n >= limit or self.out_of_time(6 if self.mode == "full" else 3):
                break
            arg = ref if getattr(pred, "uses_ref", False) else m.get("path")
            try:
                if isinstance(pred, FallbackPredictor):
                    ln = num(m.get("length_s"))
                    p = pred.predict_pp(md5, mods, rate, osu_path=arg, length_s=ln / rate if ln else None)
                elif not isinstance(pred, ModelPredictor):
                    p = pred.predict_pp(md5, mods, rate, osu_path=arg)
                else:
                    self._seed_model(pred, m, md5, mods, rate)
                    p = pred.predict_pp(md5, mods, rate, osu_path=arg, beatmap_id=m.get("beatmap_id"),
                                        network=(self.mode == "full" and not self.offline), top_plays=c.top,
                                        total_pp=c.total_pp)
            except Exception as ex:
                _log("predict_pp failed for %s %s: %s" % (md5, mods, ex))
                p = None
            p = self._checked(p, m, mods, rate, ref, md5, pred)
            n += 1
            self.counts["preds_new"] += 1
            keep = {}
            if isinstance(p, dict):
                for kk in ("pp_expected", "pp_low", "pp_high", "pp_fc", "pp_ss", "p_pass", "stars", "confidence",
                           "basis", "model_error_pp", "gain_expected", "gain_if_pass", "gain_high", "acc_p50",
                           "misses_p50", "n_similar"):
                    if kk in p:
                        keep[kk] = p[kk]
                if isinstance(p.get("acc"), dict) and "acc_p50" not in keep:
                    keep["acc_p50"] = p["acc"].get("p50")
                if isinstance(p.get("misses"), dict) and "misses_p50" not in keep:
                    keep["misses_p50"] = p["misses"].get("p50")
            self.cache["preds"][k] = {"sig": sig, "pred": fam, "ts": int(self.now), "p": keep or None}
            self.flush()
            if n % 20 == 0 and hasattr(pred, "flush"):
                pred.flush()
        if hasattr(pred, "flush"):
            pred.flush()       # keep the attributes pp_model fetched/was given for the next run

    def _seed_model(self, pred, m, md5, mods, rate):
        """Give pp_model the osu! API attributes (and map structure) this run already holds for the map."""
        api_calc = getattr(self, "api_calc", None)
        bid = m.get("beatmap_id")
        if not hasattr(pred, "seed") or api_calc is None or not bid:
            return
        try:
            key = "%d|%s|%.2f" % (int(bid), "".join(a for a in ("HR", "DT") if a in mods) or "NM", float(rate))
            raw = (self.cache.get("api_attrs") or {}).get(key)
            if not raw:
                return
            e = self.status["bid"].get(str(bid)) or {}
            bm = {"bid": int(bid), "sid": e.get("sid") or m.get("set_id"), "nC": e.get("nc"), "nS": e.get("ns"),
                  "nSp": e.get("nsp"), "od": e.get("od"), "ar": e.get("ar"), "cs": e.get("cs"),
                  "bpm": e.get("bpm"), "len_s": e.get("len"), "status": m.get("status")}
            pred.seed(md5, mods, rate, raw, beatmap=bm, osu_path=m.get("path"))
        except Exception as ex:
            _log("seed %s %s: %s" % (md5, mods, ex))

    def _checked(self, p, m, mods, rate, ref, md5, pred):
        """Reject model numbers computed for the wrong map (tosu answering for the selected map) by comparing
        with this run's own SS calculation; then rebuild pp from the model's outcome distribution instead."""
        if getattr(pred, "uses_ref", False) or self.calc_kind not in ("api", "tosu") or not self.calc:
            return p
        a = self.attrs.get(vkey(m["md5"], CORE[mods], rate)) or {}
        ss_ref, st_ref = num(a.get("pp")), num(a.get("stars"))
        if isinstance(p, dict) and ss_ref:
            ss, st = num(p.get("pp_ss")), num(p.get("stars"))
            want = ss_ref * (hd_factor(a.get("ar")) if "HD" in mods else 1.0)   # stage A: core mods, no HD
            bad = ((ss is not None and abs(ss / want - 1) > 0.3) or
                   (st is not None and st_ref is not None and abs(st - st_ref) > 0.5))
            if not bad:
                return p
            self.counts["preds_rejected"] += 1
        elif isinstance(p, dict):
            return p
        try:
            o = pred.predict_outcome(md5, mods, rate, osu_path=m.get("path"))
        except Exception:
            o = None
        q = self._pp_from_outcome(o, ref, mods, rate)
        if q is not None:
            q["basis"] = ((o or {}).get("basis") or "your model's outcome") + "; pp from osu! API attributes"
        return q

    def _pp_from_outcome(self, o, ref, mods, rate):
        if not isinstance(o, dict) or not isinstance(o.get("acc"), dict):
            return None
        acc, mis = o["acc"], o.get("misses") or {}
        ss = self._calc(ref, mods, rate)
        if not ss or ss.get("pp") is None:
            return None
        mc = ss.get("max_combo")

        def pp(a, mi):
            if a is None:
                return None
            mi = int(round(mi or 0))
            r = self._calc(ref, mods, rate, acc=a, misses=mi, combo=est_combo(mc, mi))
            return r.get("pp") if r else None
        mid = pp(num(acc.get("p50")), mis.get("p50"))
        if mid is None:
            return None
        lo = pp(num(acc.get("p20")), mis.get("p80", mis.get("p50")))
        hi = pp(num(acc.get("p80")), mis.get("p20", mis.get("p50")))
        return {"pp_expected": mid, "pp_low": lo if lo is not None else mid, "pp_high": hi if hi is not None else mid,
                "pp_fc": pp(num(acc.get("p50")), 0), "pp_ss": ss["pp"], "p_pass": num(o.get("p_pass")),
                "stars": ss.get("stars"), "confidence": o.get("confidence") or "low",
                "acc_p50": num(acc.get("p50")), "misses_p50": mis.get("p50")}

    # -- run -----------------------------------------------------------------
    def run(self):
        pred = self._pred_in
        rep0 = None
        if pred is None:
            # comfort first needs a context; the fallback needs the comfort - build a provisional one
            try:
                sys.path.insert(0, str(HERE))
                import pp_model  # noqa
                pred = ModelPredictor(pp_model) if hasattr(pp_model, "predict_pp") else None
            except Exception as e:
                _log("pp_model unavailable: %s: %s" % (type(e).__name__, e))
                pred = None
        if isinstance(pred, ModelPredictor) and not pred.trained():
            self.notes.append("pp_model.py is not trained yet (no plays learned) - rough fallback predictions")
            pred = None
        if pred is not None:
            rep0 = pred.report()
        c = load_context(self.d, rep0, self.now)
        if pred is None:
            pred = FallbackPredictor(lambda *a, **kw: self._calc(*a, **kw) if self.calc else None, c.comfort,
                                     c.history, self.now)
            if not any("pp_model" in n for n in self.notes):
                self.notes.append("pp_model.py not available - rough fallback predictions (low confidence)")
        with _TosuThrottle(self.throttle):
            maps = self._maps(c)
            if self.calc_kind == "tosu" and not self.check_tosu(maps):
                self.notes.append("tosu calculates only the map selected in game (it ignores ?path=), so map pp comes "
                                  "from osu! API difficulty attributes + the pp formula")
                self._use_api_calc()
            if self.mode == "full" and self.api:
                self.refresh_search(maps, c)
                maps = self._maps(c)                     # include new search results
                self.refresh_status(maps, c)
            if self.calc_kind == "api":
                if self.mode == "full" and self.api:
                    self.api_calc.calibrate(c.top)
                for m in maps.values():
                    if m.get("beatmap_id"):
                        self.api_calc.register("bid:%d" % m["beatmap_id"], m["beatmap_id"], lib_meta(m))
            sig = "%s|%s" % (pred.signature(), self.calc_kind)
            if self.mode == "full" and not self.api:
                self.notes.append("osu! API not available (%s): only maps with known ranked status" % self.api_err)
            self.counts["maps"] = len(maps)
            vlist = []
            for m in maps.values():
                vs = self.variants_of(m, c)
                if not vs:
                    continue
                if m["status"] not in GAIN_STATUSES:
                    if m["status"] is None:
                        self.counts["unknown_status"] += 1
                    else:
                        self.counts["not_ranked"] += 1
                    continue
                for mods, rate, s in vs:
                    vlist.append((m, mods, rate, s, self.pre_score(m, mods, rate, s, c)))
            self.counts["variants_in_band"] = len(vlist)
            vlist.sort(key=lambda v: -v[4])
            if self.mode == "full":
                # fetch .osu for the best not-owned maps (by pre-score) before stage A
                seen, order = set(), []
                for m, *_ in vlist:
                    if not m["owned"] and m["md5"] not in seen:
                        seen.add(m["md5"])
                        order.append(m)
                self.fetch_osu(order[:FETCH_PER_RUN * 2])
            self.counts["no_file"] = len({v[0]["md5"] for v in vlist if not v[0].get("path")})
            self.stage_attrs(vlist, c, ATTR_MAX_FULL if self.mode == "full" else ATTR_MAX_INC)
            self.counts["attrs_cached"] = sum(1 for v in vlist if vkey(v[0]["md5"], CORE[v[1]], v[2]) in
                                              self.attrs)
            # re-score with real stars / SS pp; drop variants now outside the band or worthless
            lo, hi = self.band(c)
            v2 = []
            for m, mods, rate, _s, _p in vlist:
                a = self.attrs.get(vkey(m["md5"], CORE[mods], rate))
                s = num(a.get("stars")) if a else None
                if a and a.get("pp") is None:
                    continue
                if s is not None and not (lo <= s <= hi) and mods not in m["hint_mods"]:
                    continue
                s = s if s is not None else _s
                if a and weighted_gain(c.top_pps, a["pp"], c.existing(m.get("beatmap_id"), m["md5"]),
                                       c.tail) <= 0.05:
                    continue      # even an SS adds nothing
                v2.append((m, mods, rate, s, self.pre_score(m, mods, rate, s, c)))
            v2.sort(key=lambda v: -v[4])
            if self.mode == "incremental":
                prev = load_json(self.d / OUT_NAME, {}) or {}
                order = {(p.get("md5"), p.get("mods")): i for i, p in enumerate(prev.get("picks") or [])}
                v2.sort(key=lambda v: (order.get((v[0]["md5"], v[1]), 10 ** 6), -v[4]))
            if self.mode == "full":
                v2 = interleave_safe(v2, c)
            v2 = group_by_map(v2)
            self.stage_preds(v2, c, PRED_MAX_FULL if self.mode == "full" else PRED_MAX_INC, sig, pred)
            out = self.assemble(v2, c, pred, sig)
        if self.mode == "full":
            self.cache["last_full"] = int(self.now) if not self.partial else self.cache.get("last_full", 0)
        self.cache["last_run"] = int(self.now)
        self.flush(force=True)
        atomic_write_json(self.d / OUT_NAME, out, compact=False)
        return out

    # -- output ----------------------------------------------------------------
    def assemble(self, v2, c, pred, sig):
        styles = StyleStats(c)
        rows = []
        for m, mods, rate, s, _p in v2:
            md5 = m.get("md5_file") or m["md5"]
            e = self.cache["preds"].get(vkey(md5, mods, rate))
            p = (e or {}).get("p")
            if not p or num(p.get("pp_expected")) is None:
                continue
            row = self._row(m, mods, rate, p, c, e)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: (-r["gain_expected"], -r["gain_high"]))
        top_gain = max((r["gain_expected"] for r in rows), default=0.0)
        for r in rows:
            r["smash"] = bool(r["p_pass"] >= SMASH_P and not r["_extrap"] and
                              r["gain_expected"] >= max(SMASH_MIN_GAIN, SMASH_REL * top_gain))
        # one mod per beatmap (best expected gain); the other evaluated mods are listed as alt_mods
        by_map = {}
        for r in rows:
            by_map.setdefault(r["md5"], []).append(r)

        def alts(rs, main):
            return [{"mods": r["mods"], "pp_expected": r["pp_expected"], "p_pass": r["p_pass"],
                     "gain_expected": r["gain_expected"], "smash": r["smash"],
                     **({"low_confidence": True} if r["_extrap"] else {})} for r in rs if r is not main][:3]
        uniq = []
        for rs in by_map.values():
            ok = [r for r in rs if not r["_extrap"]]   # DT/HR guesses from too few plays never lead a pick
            if not ok:
                self.counts["extrapolated_only"] += 1
                continue
            main = dict(ok[0], alt_mods=alts(rs, ok[0]))
            uniq.append(main)
        uniq.sort(key=lambda r: (-r["gain_expected"], -r["gain_high"]))
        picks = diversify(uniq, N_PICKS)
        picks.sort(key=lambda r: bool(r.get("stretch")))      # stable: passable picks first, stretches last
        # smash list: likely passes worth a lot - per map its best smash variant (may be a safer mod than the
        # main pick's), so these show up even when riskier maps / mods have the higher expected gain
        sm = []
        for rs in by_map.values():
            s = [r for r in rs if r["smash"]]
            if s:
                sm.append(dict(s[0], alt_mods=alts(rs, s[0])))
        sm.sort(key=lambda r: (-r["gain_expected"], -r["p_pass"]))
        smash = diversify(sm, SMASH_KEEP)
        smash.sort(key=lambda r: (-r["gain_expected"], -r["p_pass"]))   # diversify may reorder
        # explanations (predict_outcome only for the listed maps)
        for r in picks + smash:
            o = None
            if r.get("_path") and self.left() > 0:
                try:
                    o = pred.predict_outcome(r["md5"], r["mods"], r["rate"], osu_path=r["_path"])
                except Exception as ex:
                    _log("predict_outcome failed: %s" % ex)
            r["why"] = explain(r, o, styles)
        rep = pred.report()
        out = {"version": 1, "generated_at": int(time.time()), "mode": self.mode, "partial": self.partial,
               "total_pp": c.total_pp, "rank": c.rank, "comfort_stars": round(c.comfort, 2),
               "comfort_basis": c.comfort_basis, "star_band": [round(x, 2) for x in self.band(c)],
               "predictor": pred.name, "model": rep, "model_signature": sig,
               "pp_calc": {"kind": self.calc_kind, "calibration": self.cache.get("calib"),
                           "tosu_check": self.cache.get("tosu_check"),
                           "note": {"api": "osu! API difficulty attributes + pp formula, scaled to your real scores",
                                    "tosu": "tosu (rosu-pp) per .osu file"}.get(self.calc_kind, self.calc_kind)},
               "picks": [{k: v for k, v in r.items() if not k.startswith("_")} for r in picks],
               "smash_picks": [{k: v for k, v in r.items() if not k.startswith("_")} for r in smash],
               "by_mods_tip": mods_tip([r for r in rows if not r["_extrap"]]),
               "counts": self.counts, "notes": self.notes[:12],
               "took_s": round(self.clock() - self.t0, 1),
               "requests": getattr(self.api, "requests", 0) if self.api else 0,
               "method": ("gain_expected = p_pass x the mean exact gain to your total (weighted top 100 + below-#100"
                          " correction) over the model's outcome distribution; one mod per map, <= 2 diffs per set,"
                          " one diff per song; low-confidence DT/HR variants and unranked/loved/unknown-status maps"
                          " excluded; < 25% pass = stretch, listed last")}
        return out

    def _row(self, m, mods, rate, p, c, e):
        ex = c.existing(m.get("beatmap_id"), m["md5"])
        mid = float(p["pp_expected"])
        lo = num(p.get("pp_low"))
        hi = num(p.get("pp_high"))
        lo = mid if lo is None else min(lo, mid)
        hi = mid if hi is None else max(hi, mid)
        if ex is not None and ex >= hi:
            return None
        pp_pass = num(p.get("p_pass"))
        pp_pass = 0.5 if pp_pass is None else max(0.0, min(1.0, pp_pass))
        g_hi = weighted_gain(c.top_pps, hi, ex, c.tail)
        mg = num(p.get("gain_expected"))
        is_model = bool(e and e.get("pred") == "pp_model")
        if is_model and mg is not None and mg >= 0:
            # pp_model averages the gain over its whole outcome distribution: the same number the song-select
            # bubble shows. Used as is (no cap - the 2026-11-10 cap re-ranked 11 of 30 picks)
            ge = mg
            gip = num(p.get("gain_if_pass"))
            if gip is None:
                gip = mg / pp_pass if pp_pass > 0.05 else mg
            gh = max(g_hi, num(p.get("gain_high")) or 0.0, gip)
        else:
            gip = dist_gain(c.top_pps, lo, mid, hi, ex, c.tail)
            ge = pp_pass * gip
            gh = max(g_hi, gip)
        if gh < MIN_GAIN_HIGH:
            return None
        conf = p.get("confidence") or "low"
        extrap = bool(is_model and conf == "low" and ("DT" in mods or "HR" in mods))
        a = self.attrs.get(vkey(m["md5"], CORE[mods], rate)) or {}
        length = num(m.get("length_s"))
        bpm = num(m.get("bpm")) or num(a.get("bpm"))
        if length and rate != 1.0:
            length = length / rate
        if bpm and rate != 1.0 and not num(a.get("bpm")):
            bpm = bpm * rate
        stars = num(p.get("stars")) or num(a.get("stars"))
        return {"md5": m["md5"], "beatmap_id": m.get("beatmap_id"), "set_id": m.get("set_id"),
                "artist": m["artist"], "title": m["title"], "version": m["version"], "mapper": m["mapper"],
                "owned": m["owned"], "status": m["status"], "source": m["src"] + ("+climb" if m.get("climb") and
                                                                                 m["src"] != "climb" else ""),
                "mods": mods, "rate": rate, "stars": r2(stars), "length_s": int(length) if length else None,
                "bpm": int(round(bpm)) if bpm else None,
                "ar": r1(a.get("ar")) if a.get("ar") is not None else r1(m.get("ar")),
                "od": r1(a.get("od")) if a.get("od") is not None else r1(m.get("od")),
                "cs": r1(a.get("cs")) if a.get("cs") is not None else r1(m.get("cs")),
                "pp_expected": r1(mid), "pp_low": r1(lo), "pp_high": r1(hi), "pp_fc": r1(p.get("pp_fc")),
                "pp_ss": r1(p.get("pp_ss") or a.get("pp")), "p_pass": round(pp_pass, 3),
                "gain_expected": round(ge, 2), "gain_high": round(gh, 2), "gain_if_pass": round(gip, 2),
                "gain_per_min": round(ge / max(0.5, length / 60.0), 2) if length else None,
                "stretch": pp_pass < STRETCH_P,
                "gain_fc": round(weighted_gain(c.top_pps, p.get("pp_fc"), ex, c.tail), 2) if num(p.get("pp_fc"))
                else None,
                "existing_pp": r1(ex), "confidence": conf, "_extrap": extrap,
                "model_error_pp": r1(p.get("model_error_pp")),
                "pred_age_h": round((self.now - (e.get("ts") or self.now)) / 3600.0, 1) if e else None,
                "search": search_text(m), "url": ("https://osu.ppy.sh/b/%d" % m["beatmap_id"]) if m.get("beatmap_id")
                else None, "_path": m.get("path"), "_style": _style_of(m.get("feats")), "_basis": p.get("basis"),
                "_acc": p.get("acc_p50"), "_miss": p.get("misses_p50"), "_nsim": p.get("n_similar")}


def interleave_safe(vs, c, every=3):
    """Every `every`-th slot goes to the best 'probably passable' variant (crude pass chance >= 0.6), so the
    model also sees the maps that can become smash picks, not only the high-ceiling risky ones."""
    safe = [v for v in vs if crude_p_pass(v[3], c.comfort) >= 0.6]
    out, used, i, j = [], set(), 0, 0
    while i < len(vs) or j < len(safe):
        take_safe = (len(out) % every == every - 1) and j < len(safe)
        if take_safe:
            v = safe[j]
            j += 1
        elif i < len(vs):
            v = vs[i]
            i += 1
        else:
            v = safe[j]
            j += 1
        if id(v) not in used:
            used.add(id(v))
            out.append(v)
    return out


def group_by_map(vs, per_map=VARIANTS_PER_MAP):
    """Keep the given map order (first appearance) but put each map's variants together: NM first when it is in
    band (your usual way to play it, and the baseline for mod tips), then its best other variants by pre-score."""
    groups, order = {}, []
    for v in vs:
        k = v[0]["md5"]
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(v)
    out = []
    for k in order:
        g = groups[k]
        nm = [v for v in g if v[1] == "NM"]
        # HD next: it is how you play most maps, costs no extra attribute lookup (same aim/speed as NM) and
        # the pre-score can't see that HR/DT cut your pass chance (2026-11-10: with HR first, 2/3 of a run's
        # predictions went to HR/HDHR variants nobody would pick)
        hd = [v for v in g if v[1] == "HD"]
        rest = [v for v in g if v[1] not in ("NM", "HD")]
        out.extend((nm + hd + rest)[:per_map])
    return out


def lib_meta(m):
    """Object counts / AR / OD for the pp formula when the API lookup didn't store them (library features)."""
    f = m.get("feats") or {}
    out = {}
    if f.get("n"):
        n = int(f["n"])
        ns = int(round(n * float(f.get("slf") or 0)))
        out.update({"nc": n - ns, "ns": ns, "nsp": 0})
    for k in ("ar", "od", "cs", "bpm"):
        v = f.get(k) if f.get(k) is not None else m.get(k)
        if v is not None:
            out[k] = v
    return out


def search_text(m):
    s = "%s - %s [%s]" % (m.get("artist") or "", m.get("title") or "", m.get("version") or "")
    return s.strip()


def diversify(rows, n):
    """Greedy: best gain first, <= MAX_PER_SET per set, ownership mix capped at OWN_SHARE_MAX while the other
    kind still has a pick worth >= half the best remaining."""
    out, per_set, titles = [], {}, {}
    pool = list(rows)

    def blocked(r):
        if r.get("set_id") is not None and per_set.get(r["set_id"], 0) >= MAX_PER_SET:
            return True
        tk = title_key(r)
        # one pick per song: another diff of it (same set or another mapset of the song) adds little choice
        return bool(tk and tk in titles)
    while pool and len(out) < n:
        i_pick = None
        for i, r in enumerate(pool):
            if blocked(r):
                continue
            i_pick = i
            break
        if i_pick is None:
            break
        r = pool[i_pick]
        same = sum(1 for x in out if x["owned"] == r["owned"])
        if len(out) >= 2 and same + 1 > OWN_SHARE_MAX * (len(out) + 1):
            for j, x in enumerate(pool):
                if x["owned"] != r["owned"] and not blocked(x) and x["gain_expected"] >= 0.5 * r["gain_expected"]:
                    i_pick = j
                    break
        r = pool.pop(i_pick)
        out.append(r)
        if r.get("set_id") is not None:
            per_set[r["set_id"]] = per_set.get(r["set_id"], 0) + 1
        tk = title_key(r)
        if tk and tk not in titles:
            titles[tk] = r.get("set_id")
    return out


def _pct(x):
    return "%.0f%%" % (100 * x) if x < 0.995 else "%.1f%%" % (100 * x)


def explain(r, o, styles: StyleStats):
    parts = []
    st = r.get("_style")
    stars = r.get("stars") or 0
    mods = "" if r["mods"] == "NM" else " " + r["mods"]
    head = "%s %.1f*%s" % (STYLE_WORD.get(st, "Ranked"), stars, mods)
    if st and st == styles.best:
        head += " - your strongest pattern"
    elif st == "stream" and styles.worst == "stream":
        head += " - streams are your weak spot, so the pass chance is lower"
    parts.append(head)
    sim = styles.similar(st, stars) if st else None
    if sim:
        parts.append("you average %s on %d similar %s" % (_pct(sim["acc"]), sim["n"], STYLE_PLURAL[st]))
    acc = miss = None
    nsim = None
    if isinstance(o, dict):
        acc = num((o.get("acc") or {}).get("p50"))
        miss = num((o.get("misses") or {}).get("p50"))
        nsim = o.get("n_similar")
    nsim = nsim or r.get("_nsim")
    acc = acc if acc is not None else num(r.get("_acc"))
    miss = miss if miss is not None else num(r.get("_miss"))
    if acc is not None:
        t = "expect ~%s" % _pct(acc)
        if miss is not None:
            t += " with %d miss%s" % (round(miss), "" if round(miss) == 1 else "es")
        t += ", %d%% pass chance" % round(100 * r["p_pass"])
        parts.append(t)
    else:
        parts.append("%d%% pass chance" % round(100 * r["p_pass"]))
    if nsim:
        parts.append("based on %d similar plays of yours (%s confidence)" % (nsim, r["confidence"]))
    else:
        parts.append("%s confidence" % r["confidence"])
    if r.get("stretch"):
        parts.append("a stretch: worth trying, but expect a few fails")
    if r.get("existing_pp"):
        parts.append("beats your %.0fpp score here" % r["existing_pp"])
    if not r["owned"]:
        parts.append("not downloaded yet")
    s = "; ".join(parts)
    return s[0].upper() + s[1:]


def mods_tip(rows):
    """'HD adds ~+4pp on your typical map; HR costs you ~-12% pass chance' from maps evaluated with both."""
    by = {}
    for r in rows:
        by.setdefault(r["md5"], {})[r["mods"]] = r
    tips = []
    for mod, base in (("HD", "NM"), ("HR", "NM"), ("DT", "NM"), ("HDHR", "HD")):
        d_pp, d_pass = [], []
        for v in by.values():
            if mod in v and base in v:
                d_pp.append(v[mod]["pp_expected"] - v[base]["pp_expected"])
                d_pass.append(v[mod]["p_pass"] - v[base]["p_pass"])
        if len(d_pp) >= 3:
            dp = sorted(d_pp)[len(d_pp) // 2]
            dq = sorted(d_pass)[len(d_pass) // 2]
            t = "%s %s ~%+.0fpp" % (mod, "adds" if dp >= 0 else "costs", dp)
            if abs(dq) >= 0.03:
                t += " and %s ~%+.0f%% pass chance" % ("gains" if dq > 0 else "costs", 100 * dq)
            tips.append(t + (" vs %s" % base if base != "NM" else ""))
    return ("; ".join(tips[:3]) + " on your typical map") if tips else None


def _features_of(raw: bytes):
    """Library-style pattern features for a downloaded map (coach_engine.lib_features), best effort."""
    try:
        sys.path.insert(0, str(HERE))
        import coach_engine
        f = coach_engine.lib_features(raw.decode("utf-8", "replace"))
        return f if isinstance(f, dict) and f.get("mode") == 0 and "jf" in f else None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------

_run_lock = threading.Lock()


def refresh(mode="auto", **kw) -> dict | None:
    """Rebuild pp_recs.json. mode: 'auto' | 'full' | 'incremental'. Returns the written dict (None if another
    refresh is already running in this process)."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        return Recommender(mode=mode, **kw).run()
    except Exception as e:
        _log("refresh failed: %s: %s" % (type(e).__name__, e))
        raise
    finally:
        _run_lock.release()


def load_recs(d=None) -> dict:
    return load_json(Path(d or data_dir()) / OUT_NAME, {}) or {}


def _below_normal():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)
        except Exception:
            pass


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="rank-up recommender -> pp_recs.json")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true")
    g.add_argument("--incremental", action="store_true")
    g.add_argument("--show", action="store_true")
    ap.add_argument("--offline", action="store_true", help="no osu! API, no downloads")
    a = ap.parse_args(argv)
    if a.show:
        out = load_recs()
    else:
        _below_normal()
        out = refresh("full" if a.full else "incremental" if a.incremental else "auto", offline=a.offline)
    if not out:
        print("no recommendations")
        return 1
    print("%s refresh, %s s, partial=%s, comfort %.2f*, predictor %s" % (
        out.get("mode"), out.get("took_s"), out.get("partial"), out.get("comfort_stars") or 0, out.get("predictor")))
    for i, p in enumerate(out.get("picks") or [], 1):
        print("%2d. %-4s %4.2f* %5.1fpp (%s-%s) pass %3d%% +%.1f/%.1f %s%s %s" % (
            i, p["mods"], p["stars"] or 0, p["pp_expected"], p["pp_low"], p["pp_high"], round(100 * p["p_pass"]),
            p["gain_expected"], p["gain_high"], "SMASH " if p.get("smash") else "", "own" if p["owned"] else "dl",
            p["search"][:60]))
        print("      " + p["why"])
    if out.get("by_mods_tip"):
        print("tip:", out["by_mods_tip"])
    print("counts:", json.dumps(out.get("counts")), "notes:", out.get("notes"))
    if not out.get("picks"):
        print("No picks yet. The recommender needs your osu! maps (open the coach / run 'python coach_engine.py' "
              "once so it indexes your osu!lazer library), a few plays, and ideally the osu! API for ranked status "
              "and your top plays (README: \"Set up the osu! API\"). Run 'python setup_check.py' to see what is "
              "missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
