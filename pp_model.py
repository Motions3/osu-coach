#!/usr/bin/env python3
"""pp_model.py - self-learning PP predictor for ONE player (CONTRACT_PP2.md).  Python 3.11 stdlib only.

What it does
------------
Predicts the player's OUTCOME DISTRIBUTION on a map + mods + rate (pass chance, accuracy, misses, combo) from
their own history, then converts outcomes to pp with a local port of the osu!standard performance formula,
calibrated against the player's real server pp (osu! API scores).  It never guesses pp directly.

Why a local pp formula (verified live 2026-11-10): the user's tosu build IGNORES the `path` parameter of
/api/calculate/pp - every call (even with a nonexistent path) is calculated on the map currently selected in
game.  So tosu can only calculate the selected map.  Difficulty attributes therefore come from
  * the osu! API  POST /beatmaps/{id}/attributes  (server values = what ranking uses), cached forever, or
  * tosu's calculation of the map currently selected in game (hover; checksum checked before and after).
Map structure (object counts, OD/AR/CS/HP, length) comes from the local .osu file (read-only) or the API.

Data (player = the osu! user id from coach_identity.py: osu_api_config.json "user_id"/"user", else the account
logged in to osu!, else the newest replay's; every name on that id counts; "exclude_users" never; RX/AP/AT/CN
excluded):  .coach_replays.json (+ the .osr header for 300/100/50 counts), live_attempts.jsonl (passes AND
fails/quits with progress), the player's osu! API scores per beatmap (server pp, slider-tail/tick stats).

Learners (compared by a strict time-ordered backtest; the best by pp error is used):
  knn     recency-weighted kernel k-NN in standardised attribute space
  ridge   recency-weighted ridge regression on transformed targets (+ same-map random effect)
  gbm     small pure-Python gradient-boosted stumps (+ same-map random effect)
  curve   star-rating-only kernel curve (the old skill_model idea; reference)
Targets: log miss rate, logit accuracy-on-hit-objects, log(combo / combo expected for that many misses).
Ranges: empirical residual VECTORS (time-honest) added to the point prediction, each turned into pp, then
weighted 20/50/80th percentiles of that pp distribution.  Pass chance: Poisson hazard model over map progress
fitted on live attempts (fails are events, quits are censored at their progress).

Files (all next to this script; env OSU_COACH_PP_DIR moves them, tests use a temp dir):
  .pp_attr_cache.json   map info + difficulty attributes per (md5, mods, rate) + API scores
  .pp_model.json        fitted model (rows, coefficients, residual pools, backtest, calibration)
  pp_predictions.jsonl  {"kind":"pre"} snapshot at hover/play start; {"kind":"scored"} prediction vs actual

CLI:  python pp_model.py                 report
      python pp_model.py --fetch [N]     fetch missing API data (map info, attributes, your scores), N calls max
      python pp_model.py --backtest      run the learner comparison and refit (prints the table)
      python pp_model.py --update        ingest new attempts, score predictions, refit
      python pp_model.py --predict MD5 [MODS] [RATE]
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UID = None              # the player's osu! user id; None = resolve it (player_uid()); tests set it directly
EXCLUDE_UIDS = set()    # extra ids never counted (tests); osu_api_config.json "exclude_users" adds to these
ASSISTED = {"RX", "AP", "AT", "CN", "AU"}
ASSISTED_BITS = 128 | 2048 | 8192 | 4194304
DIFF_MODS = ("EZ", "HD", "HR", "FL", "DT", "HT")
MODEL_VERSION = 4
HALF_LIFE_DAYS = 7.0           # default recency half-life (tuned by the backtest)
MIN_TRAIN = 25                 # rows before the backtest starts scoring
API_INTERVAL = 1.1             # s between osu! API calls (< 60/min)
_lock = threading.RLock()


def _dir() -> Path:
    d = os.environ.get("OSU_COACH_PP_DIR")
    return Path(d) if d else HERE


def _f(name):
    return _dir() / name


def _log(msg):
    try:
        p = _f("pp_model_log.txt")
        if p.exists() and p.stat().st_size > 300_000:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)[:500] + "\n")
    except Exception:
        pass


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, obj):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        for i in range(8):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.1 * (i + 1))
        raise PermissionError(str(path))
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


def _pl():
    """pp_live (tosu client, mods parsing, rank gain) - imported lazily (it imports nothing of ours)."""
    sys.path.insert(0, str(HERE))
    import pp_live
    return pp_live


_ident_memo = {}


def player_identity():
    """{"uid": int|None, "exclude_ids": set} for the player (coach_identity.py); cached per replay-cache mtime."""
    if UID is not None:
        return {"uid": UID, "exclude_ids": set(EXCLUDE_UIDS)}
    rp = _f(".coach_replays.json")
    try:
        mt = rp.stat().st_mtime
    except OSError:
        mt = None
    key = (str(rp), mt)
    if _ident_memo.get("key") != key:
        try:
            sys.path.insert(0, str(HERE))
            import coach_identity as ci
            rc = (_load(rp, {}) or {}).get("replays") or {}
            hdrs = [e["hdr"] for e in rc.values() if isinstance(e, dict) and isinstance(e.get("hdr"), dict)
                    and e["hdr"].get("mode") == 0]
            res = ci.resolve(hdrs, ci.game_username())
            uid = res["uid"]
            if uid is None:   # no replays with an id yet: the profile osu_api.py fetched (by name)
                u = (_load(_f("pp_data.json"), {}) or {}).get("user") or {}
                uid = u.get("id") if isinstance(u.get("id"), int) and u.get("id") not in res["exclude_ids"] else None
            _ident_memo.update(key=key, val={"uid": uid, "exclude_ids": set(res["exclude_ids"]) | EXCLUDE_UIDS})
        except Exception as e:
            _log(f"identity: {e}")
            _ident_memo.update(key=key, val={"uid": None, "exclude_ids": set(EXCLUDE_UIDS)})
    return _ident_memo["val"]


def player_uid():
    return player_identity()["uid"]


# ----------------------------------------------------------------------------
# math helpers
# ----------------------------------------------------------------------------

def logit(p, eps=1e-4):
    p = min(1 - eps, max(eps, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def wquant(vals, ws, q):
    pairs = sorted((v, w) for v, w in zip(vals, ws) if w > 0)
    if not pairs:
        return None
    tot = sum(w for _, w in pairs)
    c = 0.0
    for v, w in pairs:
        c += w
        if c >= q * tot - 1e-12:
            return v
    return pairs[-1][0]


def wmean(vals, ws):
    tw = sum(ws)
    return sum(v * w for v, w in zip(vals, ws)) / tw if tw > 0 else None


def erfinv(y):
    if y <= -1:
        return -math.inf
    if y >= 1:
        return math.inf
    a = 0.147
    ln = math.log(1 - y * y)
    t = 2 / (math.pi * a) + ln / 2
    x = math.copysign(math.sqrt(math.sqrt(t * t - ln / a) - t), y)
    for _ in range(3):
        x -= (math.erf(x) - y) / (2 / math.sqrt(math.pi) * math.exp(-x * x))
    return x


def solve(A, b):
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    M = [list(map(float, A[i])) + [float(b[i])] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / pv
                row_c = M[c]
                M[r] = [a - f * bb for a, bb in zip(M[r], row_c)]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def harmonic(n):
    return sum(1.0 / i for i in range(1, n + 1))


def exp_combo_frac(misses):
    """Expected longest unbroken fraction when `misses` breaks fall uniformly at random: H(m+1)/(m+1)."""
    m = max(0, int(round(misses)))
    return 1.0 if m == 0 else harmonic(m + 1) / (m + 1)


# ----------------------------------------------------------------------------
# mods / map structure
# ----------------------------------------------------------------------------

def norm_mods(mods, rate=None):
    """-> (diff acronyms sorted, rate, flags set incl. NF, assisted bool)."""
    pl = _pl()
    raw = []
    if isinstance(mods, str):
        raw = pl._split_acronyms(mods)
    elif isinstance(mods, (list, tuple)):
        raw = [str(x.get("acronym") if isinstance(x, dict) else x).upper() for x in mods]
    elif isinstance(mods, int) and not isinstance(mods, bool):
        raw = [a for b, a in pl.LEGACY_BITS if mods & b]
    acs, r, _ = pl.parse_mods(mods, rate)
    flags = set(acs) | {a.upper() for a in raw}
    assisted = bool(flags & ASSISTED) or (isinstance(mods, int) and not isinstance(mods, bool)
                                          and bool(mods & ASSISTED_BITS))
    diff = sorted(a for a in acs if a in DIFF_MODS)
    return diff, float(r), flags, assisted


def mods_key(diff, rate):
    return ("".join(sorted(diff)) or "NM") + f"|{float(rate):.2f}"


def attr_key(md5, diff, rate):
    return f"{md5}|{mods_key(diff, rate)}"


def display_mods(diff):
    order = {a: i for i, a in enumerate(["EZ", "NF", "HT", "HD", "HR", "DT", "FL"])}
    return "".join(sorted(diff, key=lambda a: order.get(a, 9))) or "NM"


def parse_osu_text(text):
    """Small .osu reader: difficulty settings, BeatmapID/SetID, object counts, first/last object time."""
    info = {}
    sec = None
    nC = nS = nSp = 0
    first = last = None
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("//"):
            continue
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1]
            continue
        if sec in ("General", "Metadata", "Difficulty"):
            if ":" in s:
                k, v = s.split(":", 1)
                info[k.strip()] = v.strip()
        elif sec == "HitObjects":
            p = s.split(",")
            if len(p) < 4:
                continue
            try:
                t, typ = int(float(p[2])), int(p[3])
            except ValueError:
                continue
            end = t
            if typ & 1:
                nC += 1
            elif typ & 2:
                nS += 1
            elif typ & 8:
                nSp += 1
                try:
                    end = int(float(p[5]))
                except (ValueError, IndexError):
                    pass
            first = t if first is None else min(first, t)
            last = end if last is None else max(last, end)
    od = _num(info.get("OverallDifficulty"))
    ar = _num(info.get("ApproachRate"))
    return {"mode": int(_num(info.get("Mode")) or 0), "bid": int(_num(info.get("BeatmapID")) or 0) or None,
            "sid": int(_num(info.get("BeatmapSetID")) or 0) or None,
            "artist": info.get("Artist"), "title": info.get("Title"), "version": info.get("Version"),
            "creator": info.get("Creator"), "od": od, "ar": ar if ar is not None else od,
            "cs": _num(info.get("CircleSize")), "hp": _num(info.get("HPDrainRate")),
            "nC": nC, "nS": nS, "nSp": nSp,
            "len_s": round((last - first) / 1000.0, 2) if first is not None and last is not None else None}


def eff_stats(m, diff, rate):
    """Effective AR / OD / CS / HP and lazer hit windows (ms, rate-adjusted) for mods + rate."""
    od, ar, cs, hp = (m.get("od") or 5.0), m.get("ar"), (m.get("cs") or 4.0), (m.get("hp") or 5.0)
    ar = od if ar is None else ar
    if "HR" in diff:
        od, ar, cs, hp = min(10, od * 1.4), min(10, ar * 1.4), min(10, cs * 1.3), min(10, hp * 1.4)
    if "EZ" in diff:
        od, ar, cs, hp = od * 0.5, ar * 0.5, cs * 0.5, hp * 0.5
    pre = 1800 - 120 * ar if ar < 5 else 1200 - 150 * (ar - 5)
    pre /= rate
    ar_e = (1800 - pre) / 120 if pre > 1200 else 5 + (1200 - pre) / 150
    gw = (math.floor(80 - 6 * od) - 0.5) / rate
    okw = (math.floor(140 - 8 * od) - 0.5) / rate
    mw = (math.floor(200 - 10 * od) - 0.5) / rate
    return {"ar": ar_e, "od": (80 - gw) / 6.0, "cs": cs, "hp": hp, "gw": gw, "okw": okw, "mw": mw}


# ----------------------------------------------------------------------------
# osu! performance formula (local port) + calibration to server pp
# ----------------------------------------------------------------------------

def _d2p(d):
    return (5.0 * max(1.0, d / 0.0675) - 4.0) ** 3 / 100000.0


def _miss_pen(m, dsc):
    return 0.96 / ((m / (4 * max(1e-6, math.log(max(dsc, 1.0001))) ** 0.94)) + 1)


def _deviation(rg, rok, rmeh, rmiss, gw, okw, mw):
    if rg + rok + rmeh <= 0:
        return None
    oc = rg + rok + rmeh + rmiss
    n = max(1.0, oc - rmiss - rmeh)
    z = 2.32634787404
    p = rg / n
    plb = (n * p + z * z / 2) / (n + z * z) - z / (n + z * z) * math.sqrt(max(0.0, n * p * (1 - p) + z * z / 4))
    lim = okw / math.sqrt(3)
    if plb <= 0:
        d = lim
    else:
        d = gw / (math.sqrt(2) * erfinv(min(plb, 1 - 1e-12)))
        rv = math.sqrt(2 / math.pi) * okw * math.exp(-0.5 * (okw / d) ** 2) / (d * math.erf(okw / (math.sqrt(2) * d)))
        if rv >= 1:
            d = lim
        else:
            d *= math.sqrt(1 - rv)
            d = min(d, lim)
    mv = (mw * mw + okw * mw + okw * okw) / 3
    return math.sqrt(((rg + rok) * d * d + rmeh * mv) / (rg + rok + rmeh))


def pp_raw(A, S, flags=(), classic=False):
    """osu!standard pp (lazer score, non-classic) from attributes A + statistics S.
    A: aim speed sf adsc snc adst sdst max_combo nC nS nSp ar od gw okw mw
    S: great ok meh miss combo acc [tails] [tick_miss]   -> dict(pp, aim, speed, acc, emc)."""
    g, ok, meh, miss = S["great"], S["ok"], S["meh"], S["miss"]
    combo, acc = S["combo"], S["acc"]
    tot = g + ok + meh + miss
    if tot <= 0:
        return {"pp": 0.0, "aim": 0.0, "speed": 0.0, "acc": 0.0, "emc": 0.0}
    nS, maxc = A["nS"], A["max_combo"]
    ends_dropped = nS - S.get("tails", nS)
    tick_miss = S.get("tick_miss", 0)
    emc = float(miss)
    if nS > 0:
        thr = maxc - ends_dropped
        if combo < thr:
            emc = thr / max(1.0, combo)
        emc = min(emc, tick_miss + miss)
    emc = min(float(tot), max(float(miss), emc))
    mult = 1.15
    if "NF" in flags:
        mult *= max(0.9, 1 - 0.02 * emc)
    if "SO" in flags and tot > 0:
        mult *= 1.0 - (A.get("nSp", 0) / tot) ** 0.85
    od, ar = A["od"], A["ar"]
    hd = "HD" in flags
    lb = 0.95 + 0.4 * min(1.0, tot / 2000.0) + (math.log10(tot / 2000.0) * 0.5 if tot > 2000 else 0.0)
    # aim
    ad = A["aim"]
    if nS > 0 and A.get("adsc", 0) > 0:
        est = min(max(ends_dropped + tick_miss, 0), A["adsc"])
        ad *= (1 - A["sf"]) * (1 - est / A["adsc"]) ** 3 + A["sf"]
    av = _d2p(ad) * lb
    if emc > 0:
        av *= _miss_pen(emc, A["adst"])
    arf = 0.3 * (ar - 10.33) if ar > 10.33 else (0.05 * (8 - ar) if ar < 8 else 0.0)
    av *= 1 + arf * lb
    if hd:
        av *= 1 + 0.04 * (12 - ar)
    av *= acc
    av *= 0.98 + max(0.0, od) ** 2 / 2500
    # speed
    snc = A["snc"] + (tot - A["snc"]) * 0.1
    rmiss = min(miss, snc)
    rmeh = min(meh, snc - rmiss)
    rok = min(ok, snc - rmiss - rmeh)
    rg = max(0.0, snc - rmiss - rmeh - rok)
    sdev = _deviation(rg, rok, rmeh, rmiss, A["gw"], A["okw"], A["mw"]) if (g + ok + meh) > 0 else None
    if sdev is None:
        sv = 0.0
    else:
        raw = _d2p(A["speed"])
        sv = raw * lb
        if emc > 0:
            sv *= _miss_pen(emc, A["sdst"])
        if ar > 10.33:
            sv *= 1 + 0.3 * (ar - 10.33) * lb
        if hd:
            sv *= 1 + 0.04 * (12 - ar)
        cut = 100 + 220 * (22 / sdev) ** 6.5
        if raw > cut:
            adj = 50 * (math.log((raw - cut) / 50 + 1) + cut / 50)
            t = min(1.0, max(0.0, (sdev - 22.0) / 5.0))
            adj = adj + (raw - adj) * (1 - t)
            sv *= adj / raw
        rtd = max(0.0, tot - A["snc"])
        rcg = max(0.0, g - rtd)
        rco = max(0.0, ok - max(0.0, rtd - g))
        rcm = max(0.0, meh - max(0.0, rtd - g - ok))
        racc = 0.0 if A["snc"] <= 0 else (rcg * 6 + rco * 2 + rcm) / (A["snc"] * 6)
        sv *= (0.95 + max(0.0, od) ** 2 / 750) * ((acc + racc) / 2) ** ((14.5 - od) / 2)
    # accuracy
    nacc = A["nC"] + (0 if classic else nS)
    bap = ((g - (tot - nacc)) * 6 + ok * 2 + meh) / (nacc * 6) if nacc > 0 else 0.0
    bap = max(0.0, bap)
    accv = 1.52163 ** od * bap ** 24 * 2.83 * min(1.15, (nacc / 1000.0) ** 0.3)
    if hd:
        accv *= 1.08
    if "FL" in flags:
        accv *= 1.02
    total = (av ** 1.1 + sv ** 1.1 + accv ** 1.1) ** (1 / 1.1) * mult
    return {"pp": total, "aim": av, "speed": sv, "acc": accv, "emc": emc}


CAL_FEATS = ("1", "hd", "hr", "lemc", "acc", "lowar_hd", "ar_hi", "dt")


def _cal_x(A, S, flags, emc):
    hd = 1.0 if "HD" in flags else 0.0
    return [1.0, hd, 1.0 if "HR" in flags else 0.0, math.log1p(emc), S["acc"] - 0.85,
            hd * max(0.0, 9.0 - A["ar"]), max(0.0, A["ar"] - 10.0), 1.0 if "DT" in flags else 0.0]


def pp_calc(A, S, flags=(), cal=None):
    """Calibrated pp: local formula x exp(calibration . features) (fitted on the player's server-pp scores)."""
    r = pp_raw(A, S, flags)
    if cal and cal.get("w"):
        x = _cal_x(A, S, flags, r["emc"])
        f = sum(a * b for a, b in zip(cal["w"], x))
        r["pp"] *= math.exp(max(-0.7, min(0.7, f)))
    return r["pp"]


def fit_calibration(pairs, lam=0.5):
    """pairs: [(A, S, flags, server_pp, w)] -> {"w", "n", "mae_pct_raw", "mae_pct_cal"} (ridge on log ratio)."""
    X, y, W = [], [], []
    raw_err = []
    for A, S, flags, srv, w in pairs:
        if not srv or srv <= 0.5:
            continue
        r = pp_raw(A, S, flags)
        if r["pp"] <= 0.5:
            continue
        X.append(_cal_x(A, S, flags, r["emc"]))
        y.append(math.log(srv / r["pp"]))
        W.append(w)
        raw_err.append(abs(r["pp"] / srv - 1))
    if len(X) < 5:
        return {"w": None, "n": len(X)}
    k = len(X[0])
    XtX = [[sum(W[i] * X[i][a] * X[i][b] for i in range(len(X))) + (lam if a and a == b else 0.0)
            for b in range(k)] for a in range(k)]
    Xty = [sum(W[i] * X[i][a] * y[i] for i in range(len(X))) for a in range(k)]
    w = solve(XtX, Xty)
    cal_err = [abs(math.exp(math.log(1) + 0) * 0) for _ in X]
    cal_err = [abs(math.exp(y[i] - sum(a * b for a, b in zip(w, X[i]))) - 1) for i in range(len(X))]
    return {"w": [round(v, 5) for v in w], "feats": list(CAL_FEATS), "n": len(X),
            "mae_pct_raw": round(100 * sum(raw_err) / len(raw_err), 2),
            "mae_pct_cal": round(100 * sum(cal_err) / len(cal_err), 2)}


def lazer_acc(g, ok, meh, miss, tails, nS, ticks_hit, nLT):
    num_ = 300 * g + 100 * ok + 50 * meh + 150 * tails + 30 * ticks_hit
    den = 300 * (g + ok + meh + miss) + 150 * nS + 30 * nLT
    return num_ / den if den > 0 else 0.0


def tails_from_acc(acc, g, ok, meh, miss, nS, nLT):
    """Slider tails hit implied by a lazer accuracy (ticks assumed hit at the same rate as tails)."""
    if not nS or acc is None:
        return nS
    den = 300 * (g + ok + meh + miss) + 150 * nS + 30 * nLT
    rest = acc * den - (300 * g + 100 * ok + 50 * meh)
    per = 150 + 30 * (nLT / nS)          # one tail + its share of ticks
    return int(max(0, min(nS, round(rest / per))))


# ----------------------------------------------------------------------------
# attribute / map / API cache
# ----------------------------------------------------------------------------

def _bad_attr_entry(key, val):
    """API attributes cached under a custom rate are the default-rate numbers (see custom_rate): drop them."""
    try:
        if not isinstance(val, dict) or val.get("src") != "api":
            return False
        _md5, mk, rt = key.split("|")
        diff = [mk[i:i + 2] for i in range(0, len(mk), 2)] if mk != "NM" else []
        return custom_rate(diff, float(rt))
    except Exception:
        return False


class Cache:
    """.pp_attr_cache.json: {"v", "maps": {md5: info}, "attrs": {md5|MODS|rate: attrs}, "scores": {bid: {...}},
    "lookup_fail": {md5: ts}}.  Merge-on-write (the pet's watcher and the update subprocess both add)."""
    FILE = ".pp_attr_cache.json"

    def __init__(self):
        self.d = {"v": 1, "maps": {}, "attrs": {}, "scores": {}, "lookup_fail": {}}
        self.mtime = None
        self.dirty = False
        self.load()

    def path(self):
        return _f(self.FILE)

    def load(self):
        try:
            mt = self.path().stat().st_mtime
        except OSError:
            return
        if mt == self.mtime:
            return
        disk = _load(self.path(), {})
        if isinstance(disk, dict) and disk.get("v") == 1:
            for k in ("maps", "attrs", "scores", "lookup_fail"):
                merged = dict(disk.get(k) or {})
                if self.dirty:            # keep what we added but haven't written yet
                    merged.update(self.d.get(k, {}))
                if k == "attrs":
                    merged = {kk: vv for kk, vv in merged.items() if not _bad_attr_entry(kk, vv)}
                self.d[k] = merged
        self.mtime = mt

    def flush(self):
        with _lock:
            if not self.dirty:
                return
            disk = _load(self.path(), {})
            if isinstance(disk, dict) and disk.get("v") == 1:
                for k in ("maps", "attrs", "scores", "lookup_fail"):
                    merged = dict(disk.get(k) or {})
                    for kk, vv in self.d.get(k, {}).items():
                        old = merged.get(kk)
                        # never let a tosu-sourced attribute set overwrite a server (API) one
                        if k == "attrs" and isinstance(old, dict) and old.get("src") == "api" and \
                                isinstance(vv, dict) and vv.get("src") != "api":
                            continue
                        merged[kk] = vv
                    if k == "attrs":
                        merged = {kk: vv for kk, vv in merged.items() if not _bad_attr_entry(kk, vv)}
                    self.d[k] = merged
            if _save(self.path(), self.d):
                self.dirty = False
                try:
                    self.mtime = self.path().stat().st_mtime
                except OSError:
                    pass

    def set(self, sect, key, val):
        with _lock:
            self.d.setdefault(sect, {})[key] = val
            self.dirty = True


_cache = None


def cache() -> Cache:
    global _cache
    with _lock:
        if _cache is None or _cache.path() != _f(Cache.FILE):
            _cache = Cache()
        else:
            if not _cache.dirty:
                _cache.load()
        return _cache


_lib = {"mtime": None, "maps": {}}
_paths = {"mtime": None, "map": {}}


def library():
    p = _f(".coach_library.json")
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    if _lib["mtime"] != mt:
        _lib["maps"] = (_load(p, {}) or {}).get("maps") or {}
        _lib["mtime"] = mt
    return _lib["maps"]


def osu_path_for(md5):
    p = _f(".osu_coach_cache.json")
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = None
    if mt is not None and _paths["mtime"] != mt:
        c = _load(p, {})
        _paths["map"] = {v: k for k, v in ((c or {}).get("osu") or {}).items() if v}
        _paths["mtime"] = mt
    fp = _paths["map"].get(md5)
    if fp and os.path.isfile(fp):
        return fp
    alt = _f(".osu_cache") / f"{md5}.osu"
    return str(alt) if alt.is_file() else None


def map_info(md5, osu_path=None, beatmap_id=None):
    """Map structure for md5 (cached): from the .osu file (read-only) and/or what the API told us."""
    c = cache()
    m = c.d["maps"].get(md5)
    if m and m.get("nC") is not None and (beatmap_id is None or m.get("bid")):
        return m
    m = dict(m or {})
    path = osu_path if osu_path and os.path.isfile(str(osu_path)) else osu_path_for(md5)
    if path and m.get("nC") is None:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            if hashlib.md5(raw).hexdigest() == md5:
                info = parse_osu_text(raw.decode("utf-8", "replace"))
                for k, v in info.items():
                    if v is not None and m.get(k) is None:
                        m[k] = v
        except OSError:
            pass
    lib = library().get(md5) or {}
    if lib:
        for k_m, k_l in (("bid", "bid"), ("sid", "sid"), ("artist", "a"), ("title", "t"), ("version", "v"),
                         ("creator", "c"), ("bpm", "bpm")):
            if m.get(k_m) is None and lib.get(k_l) is not None:
                m[k_m] = lib[k_l]
        m["lib"] = {k: lib.get(k) for k in ("sf", "bf", "jf", "jv", "jv75", "den", "slf", "sb", "len")
                    if lib.get(k) is not None}
    if beatmap_id and not m.get("bid"):
        m["bid"] = int(beatmap_id)
    if m:
        c.set("maps", md5, m)
    return m


def _api():
    """Shared osu! API client (osu_api.Api: client credentials, paced >= API_INTERVAL)."""
    g = globals()
    if g.get("_api_obj") is None:
        sys.path.insert(0, str(HERE))
        import osu_api
        cfg = osu_api.load_json(osu_api.CONFIG_FILE, {})
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return None
        g["_api_obj"] = osu_api.Api(cfg, budget=int(os.environ.get("OSU_COACH_PP_API_BUDGET", "400")),
                                    interval=API_INTERVAL)
    return g["_api_obj"]


def custom_rate(diff, rate):
    """True for a lazer custom speed (DT/HT at a rate other than 1.5/0.75, or a bare rate change).
    The osu! API ignores speed_change and answers with the default-rate attributes (verified 2026-11-10:
    Tornado DT 1.14 came back as the DT 1.5 star rating), so only tosu (clockRate) can describe these."""
    rate = float(rate or 1.0)
    if "DT" in diff:
        return abs(rate - 1.5) > 1e-3
    if "HT" in diff:
        return abs(rate - 0.75) > 1e-3
    return abs(rate - 1.0) > 1e-3


def api_attrs(bid, diff, rate):
    api = _api()
    if api is None or custom_rate(diff, rate):
        return None
    ml = [{"acronym": a} for a in diff]
    js = api.post("/beatmaps/%d/attributes" % int(bid), {"mods": ml, "ruleset": "osu"})
    return (js or {}).get("attributes")


def _attrs_from_api(a, m, diff, rate):
    e = eff_stats(m, diff, rate)
    nC, nS, nSp = m.get("nC") or 0, m.get("nS") or 0, m.get("nSp") or 0
    mc = int(a.get("max_combo") or (nC + 2 * nS + nSp))
    return {"src": "api", "ts": int(time.time()), "stars": a.get("star_rating"), "aim": a.get("aim_difficulty"),
            "speed": a.get("speed_difficulty"), "sf": a.get("slider_factor") or 1.0,
            "adsc": a.get("aim_difficult_slider_count") or 0.0, "snc": a.get("speed_note_count") or 0.0,
            "adst": a.get("aim_difficult_strain_count") or 1.0, "sdst": a.get("speed_difficult_strain_count") or 1.0,
            "max_combo": mc, "nC": nC, "nS": nS, "nSp": nSp, "nLT": max(0, mc - nC - nSp - 2 * nS), **e}


def seed_api_attrs(md5, mods, rate, raw, beatmap=None, osu_path=None):
    """Store osu! API difficulty attributes another module already fetched (the 'attributes' dict of
    POST /beatmaps/{id}/attributes for the same difficulty mods + rate), so predict_pp needs no network for
    this map. `beatmap`: optional {bid, sid, status, nC, nS, nSp, od, ar, cs, bpm, len_s} for maps without a
    local .osu. Returns True when attributes for (md5, mods, rate) are cached afterwards. Call flush() after."""
    try:
        if not isinstance(raw, dict) or raw.get("aim_difficulty") is None:
            return False
        diff, r, _flags, assisted = norm_mods(mods, rate)
        if assisted or custom_rate(diff, r):
            return False
        c = cache()
        k = attr_key(md5, diff, r)
        a = c.d["attrs"].get(k)
        if a and a.get("src") == "api":
            return True
        bm = {kk: v for kk, v in (beatmap or {}).items() if v is not None}
        m = map_info(md5, osu_path, bm.get("bid"))
        if bm:
            m2 = dict(m)
            for kk, v in bm.items():
                if m2.get(kk) is None:
                    m2[kk] = v
            if m2 != m:
                m = m2
                c.set("maps", md5, m)
        if m.get("nC") is None or m.get("od") is None:
            return False
        c.set("attrs", k, _attrs_from_api(raw, m, diff, r))
        return True
    except Exception as e:
        _log(f"seed_api_attrs {md5}: {type(e).__name__}: {e}")
        return False


def flush():
    """Write cached maps/attributes to .pp_attr_cache.json (merged with what is on disk)."""
    try:
        cache().flush()
    except Exception as e:
        _log(f"flush: {e}")


def attrs_from_tosu_calc(d, m, diff, rate):
    """tosu /api/calculate/pp response (current map) -> our attribute dict."""
    df = d.get("difficulty") if isinstance(d.get("difficulty"), dict) else {}
    if not df.get("aim") and not df.get("stars"):
        return None
    nC, nS, nSp = int(df.get("nCircles") or 0), int(df.get("nSliders") or 0), int(df.get("nSpinners") or 0)
    e = eff_stats(m, diff, rate) if m.get("od") is not None else {}
    gw = _num(df.get("greatHitWindow"))
    if gw:
        e.update({"gw": gw, "okw": _num(df.get("okHitWindow")) or e.get("okw", gw * 2),
                  "mw": _num(df.get("mehHitWindow")) or e.get("mw", gw * 3), "od": (80 - gw) / 6.0})
    if _num(df.get("ar")) is not None:
        e["ar"] = _num(df["ar"])
    if _num(df.get("hp")) is not None and "hp" not in e:
        e["hp"] = _num(df["hp"])
    if "gw" not in e:
        return None
    mc = int(df.get("maxCombo") or (nC + 2 * nS + nSp))
    return {"src": "tosu", "ts": int(time.time()), "stars": _num(df.get("stars")), "aim": _num(df.get("aim")),
            "speed": _num(df.get("speed")), "sf": _num(df.get("sliderFactor")) or 1.0,
            "adsc": _num(df.get("aimDifficultSliderCount")) or 0.0, "snc": _num(df.get("speedNoteCount")) or 0.0,
            "adst": _num(df.get("aimDifficultStrainCount")) or 1.0,
            "sdst": _num(df.get("speedDifficultStrainCount")) or 1.0, "max_combo": mc, "nC": nC, "nS": nS,
            "nSp": nSp, "nLT": int(df.get("nLargeTicks") or max(0, mc - nC - nSp - 2 * nS)),
            "cs": e.get("cs", m.get("cs") or 4.0), "hp": e.get("hp", 5.0), **{k: e[k] for k in ("ar", "od", "gw", "okw", "mw")},
            "pp_ss_tosu": _num(d.get("pp"))}


def tosu_current_attrs(md5, diff, rate):
    """Attributes of the map selected in game RIGHT NOW (tosu ignores path=), only if its checksum is md5.
    Checked before and after the calculation; cached."""
    pl = _pl()
    try:
        s0 = pl._http_json("/json/v2", timeout=0.4)
        if (s0.get("beatmap") or {}).get("checksum") != md5:
            return None
        params = {"mode": 0, "lazer": "true", "mods": sum(pl.LEGACY_VALUE.get(a, 0) for a in diff)}
        if abs(rate - 1.0) > 1e-6:
            params["clockRate"] = rate
        d = pl._http_json("/api/calculate/pp", params, timeout=2.0)
        s1 = pl._http_json("/json/v2", timeout=0.4)
        if (s1.get("beatmap") or {}).get("checksum") != md5:
            return None
    except Exception as e:
        _log(f"tosu attrs failed: {type(e).__name__}: {e}")
        return None
    m = map_info(md5)
    if not m.get("od"):
        st = (s0.get("beatmap") or {}).get("stats") or {}
        for k in ("od", "ar", "cs", "hp"):
            v = st.get(k)
            if isinstance(v, dict):
                v = v.get("original")
            if _num(v) is not None and m.get(k) is None:
                m[k] = _num(v)
        bid = (s0.get("beatmap") or {}).get("id")
        if isinstance(bid, int) and bid > 0 and not m.get("bid"):
            m["bid"] = bid
        cache().set("maps", md5, m)
    a = attrs_from_tosu_calc(d if isinstance(d, dict) else {}, m, diff, rate)
    if a:
        cache().set("attrs", attr_key(md5, diff, rate), a)
        # keep tosu-only attributes even when the server fetch fails, and let the cache reload from disk
        # again (it doesn't while it holds unsaved changes): flush at most once a minute
        if time.time() - _flush_at[0] > 60:
            _flush_at[0] = time.time()
            flush()
    return a


_flush_at = [0.0]


def get_attrs(md5, diff, rate, osu_path=None, beatmap_id=None, network=True, tosu=False):
    """Cached attributes for (md5, mods, rate): cache -> tosu (selected map only, when tosu=True) -> API."""
    c = cache()
    k = attr_key(md5, diff, rate)
    a = c.d["attrs"].get(k)
    if a and a.get("src") == "api" and custom_rate(diff, rate):
        a = None              # default-rate attributes stored under a custom rate (old cache): wrong, ignore
    if a and (a.get("src") == "api" or not network):
        return a
    if a is None and tosu:
        a = tosu_current_attrs(md5, diff, rate)
        if a:
            return a
    if network:
        m = map_info(md5, osu_path, beatmap_id)
        bid = m.get("bid") or beatmap_id or lookup_bid(md5, network=True)
        m = c.d["maps"].get(md5) or m
        if bid and m.get("nC") is None and time.time() - (c.d["lookup_fail"].get(md5) or 0) > 7 * 86400:
            # not owned and no .osu text: object counts / OD / AR from the API beatmap
            api = _api()
            try:
                b = api.get("/beatmaps/%d" % int(bid)) if api else None
                if isinstance(b, dict) and b.get("checksum") == md5:
                    _store_api_beatmap(b)
                elif isinstance(b, dict):
                    c.set("lookup_fail", md5, int(time.time()))   # online version differs from this file
            except Exception as e:
                _log(f"beatmap {bid}: {e}")
            m = c.d["maps"].get(md5) or m
        if bid and m.get("nC") is not None:
            try:
                raw = api_attrs(bid, diff, rate)
            except Exception as e:
                _log(f"api attrs {bid} {diff} {rate}: {type(e).__name__}: {e}")
                raw = None
            if raw and raw.get("aim_difficulty") is not None:
                a = _attrs_from_api(raw, m, diff, rate)
                c.set("attrs", k, a)
    return a


def lookup_bid(md5, network=True):
    c = cache()
    m = c.d["maps"].get(md5) or {}
    if m.get("bid"):
        return m["bid"]
    lib = library().get(md5) or {}
    if lib.get("bid"):
        return lib["bid"]
    if not network or time.time() - (c.d["lookup_fail"].get(md5) or 0) < 7 * 86400:
        return None
    api = _api()
    if api is None:
        return None
    try:
        b = api.get("/beatmaps/lookup", {"checksum": md5})
    except Exception as e:
        _log(f"lookup {md5}: {e}")
        c.set("lookup_fail", md5, int(time.time()))
        return None
    if not isinstance(b, dict) or not b.get("id"):
        c.set("lookup_fail", md5, int(time.time()))
        return None
    _store_api_beatmap(b)
    return b["id"]


def _store_api_beatmap(b):
    md5 = b.get("checksum")
    if not md5:
        return
    c = cache()
    m = dict(c.d["maps"].get(md5) or {})
    bs = b.get("beatmapset") or {}
    m.update({k: v for k, v in {
        "bid": b.get("id"), "sid": b.get("beatmapset_id"), "status": b.get("status"),
        "artist": bs.get("artist"), "title": bs.get("title"), "creator": bs.get("creator"),
        "version": b.get("version"), "bpm": b.get("bpm")}.items() if v is not None})
    if m.get("nC") is None:
        m.update({"nC": b.get("count_circles"), "nS": b.get("count_sliders"), "nSp": b.get("count_spinners"),
                  "od": b.get("accuracy"), "ar": b.get("ar"), "cs": b.get("cs"), "hp": b.get("drain"),
                  "len_s": b.get("hit_length")})
    m["api_ts"] = int(time.time())
    c.set("maps", md5, m)


def _iso_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_user_scores(bid):
    api = _api()
    if api is None:
        return None
    uid = player_uid()
    if uid is None:
        return None
    js = api.get("/beatmaps/%d/scores/users/%d/all" % (int(bid), uid), {"mode": "osu"})
    out = []
    for s in (js or {}).get("scores") or []:
        st = s.get("statistics") or {}
        out.append({"ts": _iso_ts(s.get("ended_at")), "pp": s.get("pp"), "acc": s.get("accuracy"),
                    "combo": s.get("max_combo"), "mods": [m.get("acronym") for m in s.get("mods") or []],
                    "rate": next(((m.get("settings") or {}).get("speed_change") for m in s.get("mods") or []
                                  if m.get("acronym") in ("DT", "NC", "HT", "DC")), None),
                    "great": st.get("great", 0), "ok": st.get("ok", 0), "meh": st.get("meh", 0),
                    "miss": st.get("miss", 0), "tails": st.get("slider_tail_hit"),
                    "ticks": st.get("large_tick_hit"), "tick_miss": st.get("large_tick_miss", 0),
                    "passed": s.get("passed", True), "legacy": s.get("legacy_score_id") is not None})
    cache().set("scores", str(bid), {"ts": int(time.time()), "scores": out})
    return out


def fetch_missing(max_calls=400, deadline_s=1800, want_scores=True, verbose=False):
    """Fill API data for every map/mods the player has played: beatmap info (batched), attributes, and the
    player's own scores per beatmap (server pp).  Paced >= 1.1 s; resumable (cache flushed as it goes)."""
    api = _api()
    if api is None:
        return {"error": "osu! API not configured"}
    api.budget = api.count + max_calls
    t0 = time.time()
    rows = raw_rows()
    c = cache()
    need = []
    for r in rows:
        k = attr_key(r["md5"], r["diff"], r["rate"])
        a = c.d["attrs"].get(k)
        if not a or a.get("src") != "api":
            need.append(r)
    md5s = sorted({r["md5"] for r in rows})
    done = Counter()
    # 1) beatmap info in batches of 50 (status, counts, ids) for md5s with a known id
    ids = []
    for md5 in md5s:
        m = map_info(md5)
        if m.get("bid") and not m.get("status"):
            ids.append(int(m["bid"]))
    for i in range(0, len(ids), 50):
        if time.time() - t0 > deadline_s:
            break
        try:
            js = api.get("/beatmaps", [("ids[]", x) for x in ids[i:i + 50]])
            for b in (js or {}).get("beatmaps") or []:
                _store_api_beatmap(b)
            done["beatmaps"] += 1
        except Exception as e:
            _log(f"beatmaps batch: {e}")
            break
    c.flush()
    # 2) attributes, newest plays first
    seen = set()
    for r in sorted(need, key=lambda r: -r["ts"]):
        k = attr_key(r["md5"], r["diff"], r["rate"])
        if k in seen:
            continue
        seen.add(k)
        if time.time() - t0 > deadline_s or api.count >= api.budget:
            break
        try:
            a = get_attrs(r["md5"], r["diff"], r["rate"], network=True)
            done["attrs" if a and a.get("src") == "api" else "attrs_failed"] += 1
        except Exception as e:
            _log(f"attrs {k}: {e}")
            done["attrs_failed"] += 1
        if len(seen) % 10 == 0:
            c.flush()
            if verbose:
                print(f"  attrs {len(seen)}/{len(need)} calls={api.count}", flush=True)
    c.flush()
    # 3) the player's own scores per ranked/loved beatmap (server pp + slider stats)
    if want_scores:
        for md5 in md5s:
            if time.time() - t0 > deadline_s or api.count >= api.budget:
                break
            m = c.d["maps"].get(md5) or {}
            bid = m.get("bid")
            if not bid or m.get("status") not in ("ranked", "approved", "loved", "qualified"):
                continue
            last_play = max((r["ts"] for r in rows if r["md5"] == md5), default=0)
            got = c.d["scores"].get(str(bid))
            if got and got.get("ts", 0) > last_play + 60:
                continue
            try:
                fetch_user_scores(bid)
                done["scores"] += 1
            except Exception as e:
                _log(f"scores {bid}: {e}")
                done["scores_failed"] += 1
            if done["scores"] % 10 == 0:
                c.flush()
                if verbose:
                    print(f"  scores {done['scores']} calls={api.count}", flush=True)
    c.flush()
    return {"calls": api.count, **done, "seconds": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------
# dataset
# ----------------------------------------------------------------------------

def _replay_counts(path):
    """(n300, n100, n50, misses) from an .osr header (read-only), or None."""
    try:
        sys.path.insert(0, str(HERE))
        import osu_coach
        r = osu_coach.parse_osr(path, frames=False)
        return r.n300, r.n100, r.n50, r.misses
    except Exception:
        return None


_osr_counts = {}


def raw_rows():
    """All of the player's attempts as rows (passes from replays/live/API, fails/quits from live), oldest first.
    row: ts, md5, diff, rate, flags, src, passed, progress, g, ok, meh, miss, combo, acc, [tails, ticks, pp_srv]"""
    pl = _pl()
    rows = []
    rc = (_load(_f(".coach_replays.json"), {}) or {}).get("replays") or {}
    who = player_identity()
    for p, e in rc.items():
        h = e.get("hdr") if isinstance(e, dict) else None
        if not isinstance(h, dict) or h.get("mode") != 0 or not h.get("md5"):
            continue
        if who["uid"] is None or h.get("uid") != who["uid"] or h.get("uid") in who["exclude_ids"]:
            continue
        diff, rate, flags, assisted = norm_mods(str(h.get("mods") or "NM"), _num(h.get("rate")) or None)
        if assisted or int(h.get("mbits") or 0) & ASSISTED_BITS:
            continue
        acc, ts = _num(h.get("acc")), _num(h.get("ts"))
        if acc is None or ts is None:
            continue
        cnt = _osr_counts.get(p)
        if cnt is None:
            cnt = _replay_counts(p) if os.path.isfile(p) else None
            _osr_counts[p] = cnt or False
        s = e.get("sum") if isinstance(e.get("sum"), dict) else {}
        if cnt:
            g, ok, meh, miss = cnt
        else:
            miss = int(h.get("misses") or 0)
            n = int(s.get("n") or 0)
            if not n:
                continue
            g = int(s.get("n300") or 0)
            ok, meh = max(0, n - g - miss), 0
        rows.append({"ts": ts, "md5": h["md5"], "diff": diff, "rate": rate, "flags": sorted(flags), "src": "replay",
                     "passed": True, "progress": 1.0, "g": g, "ok": ok, "meh": meh, "miss": miss,
                     "combo": int(h.get("combo") or 0), "acc": acc, "player": h.get("player")})
    # live attempts (tosu): passes + fails + quits
    for a in pl.read_attempts(0):
        if not a.get("md5") or a.get("own") is False:
            continue
        diff, rate, flags, assisted = norm_mods(str(a.get("mods") or "NM"), _num(a.get("rate")) or None)
        if assisted:
            continue
        h = a.get("hits") or {}
        g, ok, meh, miss = (int(h.get(k) or 0) for k in ("300", "100", "50", "0"))
        if g + ok + meh + miss <= 0:
            continue
        rows.append({"ts": _num(a.get("ts")), "started_ts": _num(a.get("started_ts")), "md5": a["md5"],
                     "diff": diff, "rate": rate, "flags": sorted(flags), "src": "live",
                     "passed": a.get("outcome") == "passed", "outcome": a.get("outcome"),
                     "progress": _num(a.get("progress")) if a.get("outcome") != "passed" else 1.0,
                     "g": g, "ok": ok, "meh": meh, "miss": miss, "combo": int(a.get("max_combo") or 0),
                     "acc": _num(a.get("acc")), "bid": a.get("beatmap_id"), "ranked": a.get("ranked"), "live": True})
    rows = [r for r in rows if r.get("ts")]
    rows.sort(key=lambda r: r["ts"])
    # de-duplicate: a live pass and its replay are the same play (ts within 20 s, same map)
    out, by_md5 = [], defaultdict(list)
    for r in rows:
        dup = None
        if r["passed"]:
            for o in by_md5[r["md5"]]:
                if o["passed"] and abs(o["ts"] - r["ts"]) < 20 and o["miss"] == r["miss"]:
                    dup = o
                    break
        if dup:
            live = r if r["src"] == "live" else dup if dup["src"] == "live" else None
            if live is not None:      # the attempt was tracked live: it counts for the pass model
                dup.update({k: live[k] for k in ("started_ts", "bid", "ranked") if live.get(k) is not None})
                dup["live"], dup["outcome"] = True, "passed"
            continue
        by_md5[r["md5"]].append(r)
        out.append(r)
    # attach server scores (pp + slider stats) and add API-only passes
    c = cache()
    for md5, rs in by_md5.items():
        m = c.d["maps"].get(md5) or {}
        sc = (c.d["scores"].get(str(m.get("bid"))) or {}).get("scores") if m.get("bid") else None
        for s in sc or []:
            if not s.get("ts") or not s.get("passed", True):
                continue
            sd, srate, sflags, sass = norm_mods(s.get("mods") or [], s.get("rate"))
            if sass or s.get("legacy"):
                continue
            hit = None
            for r in rs:
                if r["passed"] and abs(r["ts"] - s["ts"]) < 45 and r["miss"] == s["miss"] and r["diff"] == sd:
                    hit = r
                    break
            if hit is None:
                hit = {"ts": s["ts"], "md5": md5, "diff": sd, "rate": srate, "flags": sorted(sflags), "src": "api",
                       "passed": True, "progress": 1.0, "g": s["great"], "ok": s["ok"], "meh": s["meh"],
                       "miss": s["miss"], "combo": s["combo"] or 0, "acc": s["acc"]}
                out.append(hit)
                rs.append(hit)
            hit.update({"g": s["great"], "ok": s["ok"], "meh": s["meh"], "miss": s["miss"],
                        "combo": s["combo"] or hit["combo"], "acc": s["acc"] or hit["acc"],
                        "pp_srv": s.get("pp"), "tails": s.get("tails"), "ticks": s.get("ticks"),
                        "tick_miss": s.get("tick_miss", 0)})
    out.sort(key=lambda r: r["ts"])
    return out


def _row_stats(r, A):
    """Row -> statistics dict for pp_raw (tails derived from lazer acc when the server didn't say)."""
    tails = r.get("tails")
    if tails is None:
        tails = tails_from_acc(r.get("acc"), r["g"], r["ok"], r["meh"], r["miss"], A["nS"], A["nLT"])
    acc = r.get("acc")
    if acc is None:
        acc = lazer_acc(r["g"], r["ok"], r["meh"], r["miss"], tails, A["nS"], A["nLT"] * (tails / A["nS"] if A["nS"] else 1), A["nLT"])
    return {"great": r["g"], "ok": r["ok"], "meh": r["meh"], "miss": r["miss"], "combo": r["combo"] or 1,
            "acc": acc, "tails": tails, "tick_miss": r.get("tick_miss", 0) or 0}


def feats(A, m, diff, rate):
    """Feature vector (dict) for a map + mods + rate."""
    n = max(1, A["nC"] + A["nS"] + A["nSp"])
    ln = (m.get("len_s") or n / 3.0) / rate
    lib = m.get("lib") or {}
    return {"stars": A.get("stars") or 0.0, "aim": A.get("aim") or 0.0, "speed": A.get("speed") or 0.0,
            "od": A["od"], "ar": A["ar"], "loglen": math.log(max(10.0, ln)), "logn": math.log(n),
            "hd": 1.0 if "HD" in diff else 0.0, "hr": 1.0 if "HR" in diff else 0.0,
            "lrate": math.log(rate), "spd_frac": (A.get("snc") or 0.0) / n, "sf": A.get("sf") or 1.0,
            "cs": A.get("cs") or 4.0, "stream": float(lib.get("sf") or 0.0),
            "jv": float(lib.get("jv") or 0.0) * rate, "den": float(lib.get("den") or n / max(ln, 10)) * rate,
            "lrep": 0.0}


def set_lrep(rows):
    """x["lrep"] = log1p(# of your earlier attempts on this map, any mods/outcome, strictly before the row).
    Practice effect: each retry of a map tends to go better than the last (backtest 2026-11-10: bias on
    replayed maps -3.4 -> -1.9 pp, MAE -0.33 pp)."""
    seen = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["ts"]):
        prev = seen[r["md5"]]
        n = sum(1 for t in prev if t < r["ts"] - 1)
        if "x" in r:
            r["x"]["lrep"] = math.log1p(n)
        prev.append(r["ts"])
    return rows


def pp_trainable(r, A):
    """A pass the pp model may learn from: not NF, hit-accuracy >= 50% and misses <= 25% of objects.
    (NF 'passes' and 591-miss survivals are kept for the pass model only.)"""
    if "NF" in (r.get("flags") or ()):
        return False
    n = max(1, A["nC"] + A["nS"] + A["nSp"])
    hits = r["g"] + r["ok"] + r["meh"]
    hacc = (6 * r["g"] + 2 * r["ok"] + r["meh"]) / (6 * hits) if hits else 0.0
    return hacc >= 0.5 and r["miss"] / n <= 0.25


FEATURES = ("stars", "aim", "speed", "od", "ar", "loglen", "logn", "hd", "hr", "lrate", "spd_frac", "sf", "cs",
            "stream", "jv", "den")


def targets(r, A):
    n = max(1, A["nC"] + A["nS"] + A["nSp"])
    hits = r["g"] + r["ok"] + r["meh"]
    hacc = (6 * r["g"] + 2 * r["ok"] + r["meh"]) / (6 * hits) if hits else 0.0
    cf = (r["combo"] or 1) / max(1, A["max_combo"])
    ecf = exp_combo_frac(r["miss"])
    return {"miss": math.log((r["miss"] + 0.5) / n), "acc": logit(hacc, 1e-3),
            "combo": max(-3.0, min(1.5, math.log(max(1e-3, cf) / ecf)))}


TARGETS = ("miss", "acc", "combo")


def build_dataset(network=False, verbose=False):
    """Rows with attributes, features, targets and actual pp. Rows whose map has no attributes are skipped."""
    rows = raw_rows()
    out, skipped = [], Counter()
    for r in rows:
        m = map_info(r["md5"], beatmap_id=r.get("bid"))
        if custom_rate(r["diff"], r["rate"]) and                 (cache().d["attrs"].get(attr_key(r["md5"], r["diff"], r["rate"])) or {}).get("src") != "tosu":
            skipped["custom_rate"] += 1      # lazer custom speed with no tosu attributes: the API can't do it
            continue
        A = get_attrs(r["md5"], r["diff"], r["rate"], beatmap_id=r.get("bid"), network=network)
        if not A or A.get("aim") is None or not m:
            skipped["no_attrs"] += 1
            continue
        n = A["nC"] + A["nS"] + A["nSp"]
        if n <= 0:
            skipped["no_objects"] += 1
            continue
        rr = dict(r)
        rr["A"] = A
        rr["x"] = feats(A, m, r["diff"], r["rate"])
        if r["passed"]:
            if r["g"] + r["ok"] + r["meh"] + r["miss"] < 0.9 * n:
                # a "pass" that judged far fewer objects than the map has: a different version of the map
                skipped["count_mismatch"] += 1
                continue
            if pp_trainable(r, A):
                rr["y"] = targets(r, A)
            else:
                skipped["pp_untrainable_pass"] += 1    # kept as a pass for the pass model
            S = _row_stats(r, A)
            rr["S"] = S
            rr["tail_drop"] = (A["nS"] - S["tails"]) / A["nS"] if A["nS"] else 0.0
            rr["meh_share"] = r["meh"] / (r["ok"] + r["meh"]) if (r["ok"] + r["meh"]) else None
        out.append(rr)
    set_lrep(out)
    if verbose:
        print(f"dataset: {len(out)} rows ({sum(1 for r in out if r['passed'])} passes), skipped {dict(skipped)}")
    return out, skipped


# ----------------------------------------------------------------------------
# learners
# ----------------------------------------------------------------------------

def recency_w(ts, now, hl):
    return 0.5 ** (max(0.0, now - ts) / 86400.0 / hl)


def _fxk(k):
    return f"{k[0]}|{''.join(k[1])}"


def _fxk_back(s_):
    md5, d = s_.split("|", 1)
    return (md5, tuple(d[i:i + 2] for i in range(0, len(d), 2)))


class Scaler:
    @classmethod
    def from_state(cls, st):
        o = cls.__new__(cls)
        o.names, o.mu, o.sd = tuple(st["names"]), st["mu"], st["sd"]
        return o

    def state(self):
        return {"names": list(self.names), "mu": self.mu, "sd": self.sd}

    def __init__(self, X, W, names):
        self.names = names
        self.mu, self.sd = {}, {}
        tw = sum(W) or 1.0
        for k in names:
            vals = [x[k] for x in X]
            mu = sum(v * w for v, w in zip(vals, W)) / tw
            var = sum(w * (v - mu) ** 2 for v, w in zip(vals, W)) / tw
            self.mu[k], self.sd[k] = mu, math.sqrt(var) if var > 1e-12 else 1.0

    def __call__(self, x):
        return [(x[k] - self.mu[k]) / self.sd[k] for k in self.names]


class KNN:
    """Recency-weighted Gaussian-kernel neighbours in standardised space; same map+mods counts as distance 0."""
    name = "knn"
    FEATS = ("stars", "aim", "speed", "od", "ar", "loglen", "hd", "lrate", "stream", "jv")
    SCALE = {"stars": 2.0, "aim": 1.0, "speed": 1.0, "od": 0.6, "ar": 0.6, "loglen": 0.6, "hd": 0.7,
             "lrate": 1.0, "stream": 0.5, "jv": 0.5}

    def __init__(self, hl=HALF_LIFE_DAYS, bw=0.9, k=40):
        self.hl, self.bw, self.k = hl, bw, k

    def fit(self, rows, now):
        self.rows = rows
        self.now = now
        W = [recency_w(r["ts"], now, self.hl) for r in rows]
        self.sc = Scaler([r["x"] for r in rows], W, self.FEATS)
        self.Z = [self.sc(r["x"]) for r in rows]
        self.W = W
        self.wt = [self.SCALE[k] for k in self.FEATS]
        return self

    def state(self):
        return None

    def load(self, st, rows, now):
        return self.fit(rows, now)

    def base_predict(self, x):
        return self.predict(x)

    def predict(self, x, md5=None, diff=None, ts=None):
        z = self.sc(x)
        cand = []
        for zi, wi, r in zip(self.Z, self.W, self.rows):
            if ts is not None and r["ts"] >= ts:
                continue
            d2 = sum(((a - b) * s) ** 2 for a, b, s in zip(z, zi, self.wt))
            if md5 and r["md5"] == md5 and r["diff"] == diff:
                d2 *= 0.25
            cand.append((d2, wi, r))
        cand.sort(key=lambda t: t[0])
        cand = cand[: self.k]
        if not cand:
            return None
        out = {}
        ws = [w * math.exp(-d2 / (2 * self.bw ** 2)) for d2, w, _ in cand]
        if sum(ws) < 1e-9:
            ws = [w for _, w, _ in cand]
        for t in TARGETS:
            out[t] = wmean([r["y"][t] for _, _, r in cand], ws)
        out["_n_eff"] = sum(ws) ** 2 / max(1e-12, sum(w * w for w in ws))
        return out


def _design(x, names):
    return [1.0] + [x[k] for k in names]


class Ridge:
    """Recency-weighted ridge per target on standardised features (+ squared stars), then a same-map
    random effect (shrunken mean residual of earlier plays of this map+mods)."""
    name = "ridge"
    # small on purpose: with ~250 noisy passes the backtest preferred 7-9 features and strong shrinkage
    # (2026-11-10: 14 features lam=3 -> 9.6 pp MAE; these 9 with lam=30 -> 8.5)
    # + lrep (practice on this map) 2026-11-10 review: MAE 7.38 -> 7.05 on the cleaned data
    FEATS = ("stars", "aim", "speed", "loglen", "hd", "od", "ar", "hr", "lrate", "lrep")

    def __init__(self, hl=HALF_LIFE_DAYS, lam=30.0, k_map=1.5):
        self.hl, self.lam, self.k_map = hl, lam, k_map

    def _z(self, x):
        z = self.sc(x)
        s = z[0]
        return [1.0] + z + [s * s]

    def fit(self, rows, now):
        self.now = now
        W = [recency_w(r["ts"], now, self.hl) for r in rows]
        self.sc = Scaler([r["x"] for r in rows], W, self.FEATS)
        Z = [self._z(r["x"]) for r in rows]
        k = len(Z[0])
        XtX = [[0.0] * k for _ in range(k)]
        for z, w in zip(Z, W):
            for a in range(k):
                wa = w * z[a]
                row = XtX[a]
                for b in range(k):
                    row[b] += wa * z[b]
        for a in range(1, k):
            XtX[a][a] += self.lam
        self.coef = {}
        for t in TARGETS:
            Xty = [sum(w * z[a] * r["y"][t] for z, w, r in zip(Z, W, rows)) for a in range(k)]
            self.coef[t] = solve(XtX, Xty)
        self._map_effects(rows, Z, W)
        return self

    def _base(self, z):
        return {t: sum(a * b for a, b in zip(self.coef[t], z)) for t in TARGETS}

    def _map_effects(self, rows, Z, W):
        acc = defaultdict(lambda: {t: [0.0, 0.0] for t in TARGETS})
        for r, z, w in zip(rows, Z, W):
            b = self._base(z)
            e = acc[(r["md5"], tuple(r["diff"]))]
            for t in TARGETS:
                e[t][0] += w * (r["y"][t] - b[t])
                e[t][1] += w
        self.fx = {k: {t: v[t][0] / (v[t][1] + self.k_map * 0.5 ** 0) if v[t][1] else 0.0 for t in TARGETS}
                   for k, v in acc.items()}
        self.fx_w = {k: v["miss"][1] for k, v in acc.items()}

    def state(self):
        return {"sc": self.sc.state(), "coef": self.coef, "fx": {_fxk(k): v for k, v in self.fx.items()},
                "fx_w": {_fxk(k): v for k, v in self.fx_w.items()}}

    def load(self, st, rows, now):
        self.sc = Scaler.from_state(st["sc"])
        self.coef = st["coef"]
        self.fx = {_fxk_back(k): v for k, v in st["fx"].items()}
        self.fx_w = {_fxk_back(k): v for k, v in st["fx_w"].items()}
        return self

    def base_predict(self, x):
        return self._base(self._z(x))

    def predict(self, x, md5=None, diff=None, ts=None):
        out = self._base(self._z(x))
        fx = self.fx.get((md5, tuple(diff or ()))) if md5 else None
        if fx:
            for t in TARGETS:
                out[t] += fx[t]
        out["_n_eff"] = self.fx_w.get((md5, tuple(diff or ())), 0.0)
        return out


class Stumps:
    """Gradient-boosted depth-1 trees (pure Python), squared loss, recency weights, shrinkage 0.1, then the
    same-map random effect like Ridge."""
    name = "gbm"
    FEATS = ("stars", "aim", "speed", "od", "ar", "loglen", "logn", "hd", "hr", "lrate", "spd_frac", "sf", "stream",
             "jv", "den")

    def __init__(self, hl=HALF_LIFE_DAYS, rounds=80, lr=0.1, min_w=3.0, k_map=1.5, n_cut=16):
        self.hl, self.rounds, self.lr, self.min_w, self.k_map, self.n_cut = hl, rounds, lr, min_w, k_map, n_cut

    def fit(self, rows, now):
        W = [recency_w(r["ts"], now, self.hl) for r in rows]
        n = len(rows)
        cols = {f: [r["x"][f] for r in rows] for f in self.FEATS}
        # candidate cut points: quantiles of each feature
        cuts = {}
        for f in self.FEATS:
            vs = sorted(set(cols[f]))
            if len(vs) <= 1:
                continue
            step = max(1, len(vs) // self.n_cut)
            cuts[f] = [(vs[i] + vs[i + 1]) / 2 for i in range(0, len(vs) - 1, step)]
        orders = {f: sorted(range(n), key=lambda i: cols[f][i]) for f in cuts}
        self.models = {}
        tw = sum(W)
        for t in TARGETS:
            y = [r["y"][t] for r in rows]
            f0 = sum(a * w for a, w in zip(y, W)) / tw
            pred = [f0] * n
            trees = []
            for _ in range(self.rounds):
                res = [a - p for a, p in zip(y, pred)]
                best = None
                for f, cs in cuts.items():
                    order = orders[f]
                    col = cols[f]
                    sw_l = swr_l = 0.0
                    tot_w, tot_r = tw, sum(rr * w for rr, w in zip(res, W))
                    j = 0
                    for c in cs:
                        while j < n and col[order[j]] <= c:
                            i = order[j]
                            sw_l += W[i]
                            swr_l += W[i] * res[i]
                            j += 1
                        sw_r = tot_w - sw_l
                        if sw_l < self.min_w * 0.05 * tw / max(1, n) * n / 20 or sw_r < 1e-9 or sw_l < 1e-9:
                            continue
                        gain = swr_l * swr_l / sw_l + (tot_r - swr_l) ** 2 / sw_r
                        if best is None or gain > best[0]:
                            best = (gain, f, c, swr_l / sw_l, (tot_r - swr_l) / sw_r)
                if best is None:
                    break
                _, f, c, vl, vr = best
                trees.append((f, c, self.lr * vl, self.lr * vr))
                col = cols[f]
                pred = [p + (self.lr * vl if col[i] <= c else self.lr * vr) for i, p in enumerate(pred)]
            self.models[t] = (f0, trees)
        # same-map effects on the boosted residuals
        acc = defaultdict(lambda: {t: [0.0, 0.0] for t in TARGETS})
        for r, w in zip(rows, W):
            b = self._base(r["x"])
            e = acc[(r["md5"], tuple(r["diff"]))]
            for t in TARGETS:
                e[t][0] += w * (r["y"][t] - b[t])
                e[t][1] += w
        self.fx = {k: {t: v[t][0] / (v[t][1] + self.k_map) if v[t][1] else 0.0 for t in TARGETS}
                   for k, v in acc.items()}
        self.fx_w = {k: v["miss"][1] for k, v in acc.items()}
        return self

    def state(self):
        return {"models": {t: [f0, [list(tr) for tr in trees]] for t, (f0, trees) in self.models.items()},
                "fx": {_fxk(k): v for k, v in self.fx.items()}, "fx_w": {_fxk(k): v for k, v in self.fx_w.items()}}

    def load(self, st, rows, now):
        self.models = {t: (v[0], [tuple(tr) for tr in v[1]]) for t, v in st["models"].items()}
        self.fx = {_fxk_back(k): v for k, v in st["fx"].items()}
        self.fx_w = {_fxk_back(k): v for k, v in st["fx_w"].items()}
        return self

    def _base(self, x):
        out = {}
        for t, (f0, trees) in self.models.items():
            v = f0
            for f, c, vl, vr in trees:
                v += vl if x[f] <= c else vr
            out[t] = v
        return out

    def base_predict(self, x):
        return self._base(x)

    def predict(self, x, md5=None, diff=None, ts=None):
        out = self._base(x)
        fx = self.fx.get((md5, tuple(diff or ()))) if md5 else None
        if fx:
            for t in TARGETS:
                out[t] += fx[t]
        out["_n_eff"] = self.fx_w.get((md5, tuple(diff or ())), 0.0)
        return out


class Curve:
    """Reference: kernel-weighted local-linear curve in star rating only (what the old skill model did)."""
    name = "curve"

    def __init__(self, hl=21.0, bw=0.35):
        self.hl, self.bw = hl, bw

    def fit(self, rows, now):
        self.pts = [(r["x"]["stars"], r["y"], recency_w(r["ts"], now, self.hl)) for r in rows]
        return self

    def state(self):
        return None

    def load(self, st, rows, now):
        return self.fit(rows, now)

    def base_predict(self, x):
        return self.predict(x)

    def predict(self, x, md5=None, diff=None, ts=None):
        s = x["stars"]
        ws = [w * math.exp(-((p - s) / self.bw) ** 2 / 2) for p, _, w in self.pts]
        if sum(ws) < 1e-9:
            ws = [w for _, _, w in self.pts]
        out = {}
        xs = [p for p, _, _ in self.pts]
        mx = wmean(xs, ws)
        vx = sum(w * (a - mx) ** 2 for a, w in zip(xs, ws))
        for t in TARGETS:
            ys = [y[t] for _, y, _ in self.pts]
            my = wmean(ys, ws)
            b = sum(w * (a - mx) * (yy - my) for a, yy, w in zip(xs, ys, ws)) / vx if vx > 1e-9 else 0.0
            out[t] = my + b * (s - mx)
        out["_n_eff"] = sum(ws) ** 2 / max(1e-12, sum(w * w for w in ws))
        return out


class Blend:
    """Average of the ridge and star-curve target predictions (variance reduction)."""
    name = "blend"

    def __init__(self, hl=HALF_LIFE_DAYS):
        self.parts = [Ridge(hl=hl), Curve(hl=hl)]

    def fit(self, rows, now):
        for p in self.parts:
            p.fit(rows, now)
        return self

    def state(self):
        return [p.state() for p in self.parts]

    def load(self, st, rows, now):
        for p, ps in zip(self.parts, st or [None] * len(self.parts)):
            if ps is not None:
                p.load(ps, rows, now)
            else:
                p.fit(rows, now)
        return self

    def base_predict(self, x):
        ps = [p.base_predict(x) for p in self.parts]
        return {t: sum(p[t] for p in ps) / len(ps) for t in TARGETS}

    def predict(self, x, md5=None, diff=None, ts=None):
        ps = [p.predict(x, md5, diff, ts) for p in self.parts]
        ps = [p for p in ps if p]
        out = {t: sum(p[t] for p in ps) / len(ps) for t in TARGETS}
        out["_n_eff"] = max(p.get("_n_eff", 0) for p in ps)
        return out


LEARNERS = {"knn": KNN, "ridge": Ridge, "gbm": Stumps, "curve": Curve, "blend": Blend}


# ----------------------------------------------------------------------------
# outcome <-> pp
# ----------------------------------------------------------------------------

def outcome_from_targets(y, A, aux):
    """Target vector -> concrete statistics (counts, combo, lazer acc)."""
    n = A["nC"] + A["nS"] + A["nSp"]
    m = max(0, min(n, int(round(math.exp(y["miss"]) * n - 0.5))))
    H = n - m
    hacc = sigmoid(y["acc"])
    s = aux.get("meh_share", 0.1)
    k = 6 * H * (1 - hacc) / (4 + s)
    k = max(0.0, min(H, k))
    meh = int(round(k * s))
    ok = int(round(k - meh))
    g = max(0, H - ok - meh)
    td = aux.get("tail_drop", 0.05)
    tails = int(round(A["nS"] * (1 - td)))
    ticks = int(round(A["nLT"] * (1 - td)))
    ecf = exp_combo_frac(m)
    cf = min(1.0, ecf * math.exp(y["combo"]))
    combo = int(max(1, min(A["max_combo"] - (0 if m == 0 else m), round(cf * A["max_combo"]))))
    if m == 0 and tails == A["nS"] and y["combo"] >= 0:
        combo = A["max_combo"]
    acc = lazer_acc(g, ok, meh, m, tails, A["nS"], ticks, A["nLT"])
    return {"great": g, "ok": ok, "meh": meh, "miss": m, "combo": combo, "acc": acc, "tails": tails,
            "tick_miss": A["nLT"] - ticks, "hit_acc": hacc}


# ----------------------------------------------------------------------------
# pass model: Poisson hazard over map progress (fails = events, quits = censored)
# ----------------------------------------------------------------------------

PASS_FEATS = ("stars", "aim", "speed", "loglen", "hd", "hr", "lrate")
QUIT_EVENT = 0.5          # see PassModel.exposure (pass backtest 2026-11-10: Brier 0.246 -> 0.173)
QUIT_MIN_PROGRESS = 0.2   # restarts in the first 20% of the map say nothing about passing


class PassModel:
    def __init__(self, hl=14.0, lam=2.0):
        self.hl, self.lam = hl, lam
        self.w = None

    @staticmethod
    def exposure(r):
        if r["passed"]:
            return 1.0, 0
        p = r.get("progress")
        p = 0.02 if p is None else max(0.02, min(1.0, p))
        if r.get("outcome") == "failed":
            return p, 1.0
        # a quit is not independent of how the run is going: it counts as a fraction of a fail (tuned by the
        # time-ordered pass backtest; 0 = pure censoring was clearly over-optimistic)
        return p, QUIT_EVENT if p >= QUIT_MIN_PROGRESS else 0.0

    def fit(self, rows, now):
        rows = [r for r in rows if r.get("live") and "NF" not in r["flags"]]
        if len(rows) < 15 or sum(1 for r in rows if r.get("outcome") == "failed") < 3:
            self.w = None
            self.n = len(rows)
            return self
        W = [recency_w(r["ts"], now, self.hl) for r in rows]
        self.sc = Scaler([r["x"] for r in rows], W, PASS_FEATS)
        Z = [[1.0] + self.sc(r["x"]) for r in rows]
        E = [self.exposure(r) for r in rows]
        k = len(Z[0])
        tot_e = sum(w * e for w, (e, _) in zip(W, E))
        tot_d = sum(w * d for w, (_, d) in zip(W, E))
        b = [math.log(max(1e-3, tot_d) / max(1e-3, tot_e))] + [0.0] * (k - 1)
        for _ in range(25):   # IRLS / Newton for Poisson with log-exposure offset
            H = [[0.0] * k for _ in range(k)]
            gvec = [0.0] * k
            for z, w, (e, d) in zip(Z, W, E):
                eta = sum(a * c for a, c in zip(b, z))
                mu = e * math.exp(max(-20, min(20, eta)))
                for a in range(k):
                    gvec[a] += w * (d - mu) * z[a]
                    for c in range(k):
                        H[a][c] += w * mu * z[a] * z[c]
            for a in range(1, k):
                gvec[a] -= self.lam * b[a]
                H[a][a] += self.lam
            step = solve(H, gvec)
            b = [x + s for x, s in zip(b, step)]
            if max(abs(s) for s in step) < 1e-6:
                break
        self.w = b
        self.n = len(rows)
        self.n_fail = sum(1 for r in rows if r.get("outcome") == "failed")
        return self

    def state(self):
        return {"w": self.w, "sc": self.sc.state() if self.w else None, "n": getattr(self, "n", 0),
                "n_fail": getattr(self, "n_fail", 0)}

    def load(self, st):
        st = st or {}
        self.w = st.get("w")
        self.sc = Scaler.from_state(st["sc"]) if st.get("sc") else None
        self.n, self.n_fail = st.get("n", 0), st.get("n_fail", 0)
        return self

    def p_pass(self, x):
        if not self.w:
            return None
        z = [1.0] + self.sc(x)
        lam = math.exp(max(-20, min(20, sum(a * c for a, c in zip(self.w, z)))))
        return math.exp(-lam)


# ----------------------------------------------------------------------------
# the fitted predictor
# ----------------------------------------------------------------------------

POOL_MAX = 160
POOL_BW = 0.7        # p20/p80 weight residuals by star distance (Gaussian, *): balances the range by difficulty
                     # (2026-11-10 review: coverage <4.5* 0.51 / >=4.5* 0.74 with one global pool); median stays global


def _pool_out(e):
    return [{t: round(v, 5) for t, v in e[0].items()}, e[1]] + ([round(e[2], 3)] if len(e) > 2 and e[2] is not None else [])


def _pool_in(e):
    return tuple(e) if len(e) > 2 else (e[0], e[1])


MAP_K = 0.4          # same map+mods effect: shrinkage pseudo-count (backtest: 0.25-0.6 all within 0.05 pp)
MAP_HL_D = 3.0       # ... and recency half-life (days) of earlier plays of that map
MAP_OTHER_MODS_W = 0.35   # an earlier play of the same map with OTHER mods counts this much
FORM_HL_H = 0.0      # "form today" (residuals of the last hours, half-life h); 0 = off: it made the backtest
                     # worse on this player's data (2026-11-10: +0.3..0.8 pp MAE), kept switchable
FORM_K = 2.0         # shrinkage (pseudo-count) of the form term
FORM_WINDOW_H = 8.0


class Predictor:
    def __init__(self, learner="gbm", hl=HALF_LIFE_DAYS, spread=1.0, cal=None, pp_mode="median",
                 form_hl=FORM_HL_H, form_k=FORM_K, map_k=MAP_K, map_hl=MAP_HL_D, pool_bw=None):
        self.learner_name, self.hl, self.spread, self.cal, self.pp_mode = learner, hl, spread, cal, pp_mode
        self.pool_bw = POOL_BW if pool_bw is None else pool_bw
        self.form_hl, self.form_k = form_hl, form_k
        self.map_k, self.map_hl = map_k, map_hl
        self.hist = None          # pass rows the map effect may use (default: the training passes)
        self._base_cache = {}

    def fit(self, rows, now, residual_pool=True, pool=None):
        self.now = now
        passes = [r for r in rows if r["passed"] and "y" in r]
        self.passes = passes
        self.n = len(passes)
        L = LEARNERS[self.learner_name]
        self.model = L(hl=self.hl).fit(passes, now) if passes else None
        W = [recency_w(r["ts"], now, self.hl) for r in passes]
        md = [(r["meh_share"], w) for r, w in zip(passes, W) if r.get("meh_share") is not None]
        self.aux = {"meh_share": wmean([a for a, _ in md], [w for _, w in md]) if md else 0.1,
                    "tail_drop": wmean([r["tail_drop"] for r in passes], W) if passes else 0.05}
        self.passm = PassModel().fit(rows, now)
        if pool is not None and len(pool) >= 20:
            self.pool = list(pool)[-POOL_MAX:]       # time-honest one-step-ahead residuals (backtest + log)
        elif residual_pool:
            self.pool = self._cv_residuals(passes, now)
        else:
            self.pool = []
        return self

    def state(self):
        return {"learner": self.learner_name, "hl": self.hl, "spread": self.spread, "pp_mode": self.pp_mode,
                "form_hl": self.form_hl, "form_k": self.form_k, "map_k": self.map_k, "map_hl": self.map_hl,
                "now": self.now, "model": self.model.state() if self.model else None, "aux": self.aux,
                "pass": self.passm.state(), "pool": [_pool_out(e) for e in self.pool]}

    @classmethod
    def from_state(cls, st, rows, cal):
        pr = cls(st["learner"], st["hl"], st["spread"], cal, st.get("pp_mode", "median"),
                 st.get("form_hl", FORM_HL_H), st.get("form_k", FORM_K), st.get("map_k", MAP_K),
                 st.get("map_hl", MAP_HL_D))
        pr.now = st["now"]
        pr.passes = [r for r in rows if r["passed"] and "y" in r]
        pr.n = len(pr.passes)
        L = LEARNERS[pr.learner_name]
        pr.model = L(hl=pr.hl).load(st["model"], pr.passes, pr.now) if pr.passes else None
        pr.aux = st["aux"]
        pr.passm = PassModel().load(st.get("pass"))
        pr.pool = [_pool_in(e) for e in st.get("pool") or []]
        return pr

    def _cv_residuals(self, passes, now, folds=5):
        """Residual vectors from K contiguous time blocks (each predicted by a model fitted on the others)."""
        n = len(passes)
        if n < 15:
            return []
        L = LEARNERS[self.learner_name]
        pool = []
        for f in range(folds):
            lo, hi = n * f // folds, n * (f + 1) // folds
            train = passes[:lo] + passes[hi:]
            if len(train) < 10:
                continue
            mdl = L(hl=self.hl).fit(train, now)
            for r in passes[lo:hi]:
                p = mdl.predict(r["x"], r["md5"], r["diff"])
                if p:
                    pool.append(({t: r["y"][t] - p[t] for t in TARGETS}, r["ts"], r["x"]["stars"]))
        return pool

    def form(self, now):
        """Shrunken mean of recent one-step-ahead residuals (strictly before `now`): today's form."""
        if not self.form_hl or not self.pool:
            return None
        acc = {t: 0.0 for t in TARGETS}
        tw = 0.0
        for res, t0, *_ in self.pool:
            dt_h = (now - t0) / 3600.0
            if dt_h <= 0 or dt_h > FORM_WINDOW_H:
                continue
            w = 0.5 ** (dt_h / self.form_hl)
            tw += w
            for t in TARGETS:
                acc[t] += w * res[t]
        if tw <= 0:
            return None
        return {t: acc[t] / (tw + self.form_k) for t in TARGETS}, tw

    def _row_base(self, r):
        k = id(r)
        v = self._base_cache.get(k)
        if v is None:
            v = self.model.base_predict(r["x"])
            self._base_cache[k] = v
        return v

    def map_effect(self, md5, diff, now):
        """Shrunken, recency-weighted mean residual of earlier passes of this map (same mods full weight, other
        mods partial) - strictly before `now`."""
        if not md5 or self.map_k is None:
            return None, 0.0
        hist = self.hist if self.hist is not None else self.passes
        acc = {t: 0.0 for t in TARGETS}
        tw = 0.0
        for r in self._by_md5(hist).get(md5, ()):
            if r["ts"] >= now - 1:
                continue
            w = 0.5 ** (max(0.0, now - r["ts"]) / 86400.0 / self.map_hl)
            if r["diff"] != list(diff or []):
                w *= MAP_OTHER_MODS_W
            b = self._row_base(r)
            tw += w
            for t in TARGETS:
                acc[t] += w * (r["y"][t] - b[t])
        if tw <= 0:
            return None, 0.0
        return {t: acc[t] / (tw + self.map_k) for t in TARGETS}, tw

    def _by_md5(self, hist):
        key = (id(hist), len(hist))
        if getattr(self, "_idx_key", None) != key:
            idx = defaultdict(list)
            for r in hist:
                idx[r["md5"]].append(r)
            self._idx, self._idx_key = idx, key
        return self._idx

    def target_pred(self, x, md5=None, diff=None, ts=None, now=None, form=True):
        if not self.model:
            return None
        y = self.model.base_predict(x)
        if y is None:
            return None
        y = dict(y)
        now_ = now if now is not None else (ts if ts is not None else time.time())
        fx, fw = self.map_effect(md5, diff, now_)
        if fx:
            for t in TARGETS:
                y[t] += fx[t]
        y["_n_eff"] = fw
        y["_base"] = {t: y[t] for t in TARGETS}
        f = self.form(now_) if form else None
        if f:
            y = dict(y)
            for t in TARGETS:
                y[t] += f[0][t]
            y["_form_w"] = f[1]
        return y

    def distribution(self, A, x, md5, diff, flags, ts=None, now=None, max_samples=80):
        """-> (median outcome, list of (pp, weight), list of outcomes) given a pass."""
        now = now or time.time()
        y = self.target_pred(x, md5, diff, ts, now=now)
        if y is None:
            return None
        pool = self.pool or [({t: 0.0 for t in TARGETS}, now)]
        ws = [recency_w(e[1], now, max(self.hl, 3.0) * 2) for e in pool]
        band = self.pool_bw and all(len(e) > 2 and e[2] is not None for e in pool)
        wb = [w * math.exp(-0.5 * ((e[2] - x["stars"]) / self.pool_bw) ** 2) for w, e in zip(ws, pool)]             if band else ws
        if len(pool) > max_samples:   # keep the most recent / heaviest (and the nearest in stars for the range)
            keep = set(sorted(range(len(pool)), key=lambda i: -ws[i])[:max_samples])
            if band:
                keep |= set(sorted(range(len(pool)), key=lambda i: -wb[i])[:max_samples // 2])
            idx = sorted(keep)
            pool = [pool[i] for i in idx]
            ws = [ws[i] for i in idx]
            wb = [wb[i] for i in idx]
        samples = []
        for e, w, w2 in zip(pool, ws, wb):
            res = e[0]
            yy = {t: y[t] + self.spread * res[t] for t in TARGETS}
            o = outcome_from_targets(yy, A, self.aux)
            samples.append((pp_calc(A, o, flags, self.cal), w, o, w2))
        med = outcome_from_targets({t: y[t] for t in TARGETS}, A, self.aux)
        return y, med, samples

    def predict_full(self, A, x, md5, diff, flags, ts=None, now=None):
        d = self.distribution(A, x, md5, diff, flags, ts, now)
        if d is None:
            return None
        y, med, samples = d
        pps = [s[0] for s in samples]
        ws = [s[1] for s in samples]
        wr = [s[3] for s in samples]     # range weights: residuals of plays near this star rating
        pp_med_outcome = pp_calc(A, med, flags, self.cal)
        pp_med = wquant(pps, ws, 0.5)
        out = {"y": y, "median_outcome": med, "pp_at_median": pp_med_outcome, "pp_median": pp_med,
               "pp_expected": pp_med if self.pp_mode == "median" else pp_med_outcome,
               "pp_low": wquant(pps, wr, 0.2), "pp_high": wquant(pps, wr, 0.8), "samples": samples,
               "n_eff": y.get("_n_eff", 0.0)}
        def q(key, qq):
            return wquant([s[2][key] for s in samples], ws, qq)
        out["acc_q"] = (q("acc", 0.2), q("acc", 0.5), q("acc", 0.8))
        out["miss_q"] = (q("miss", 0.2), q("miss", 0.5), q("miss", 0.8))
        out["combo_q"] = (q("combo", 0.2), q("combo", 0.5), q("combo", 0.8))
        out["p_pass"] = 1.0 if "NF" in flags else self.passm.p_pass(x)
        return out


def _actual_pp(r, cal):
    if r.get("pp_srv"):
        return float(r["pp_srv"])
    return pp_calc(r["A"], r["S"], r["flags"], cal)


# ----------------------------------------------------------------------------
# backtest
# ----------------------------------------------------------------------------

def backtest(rows, learner="gbm", hl=HALF_LIFE_DAYS, cal=None, block=6, min_train=MIN_TRAIN, spread=1.0,
             verbose=False, collect=False, form_hl=FORM_HL_H, form_k=FORM_K, map_k=MAP_K, map_hl=MAP_HL_D,
             pool_bw=None):
    """Strict time-ordered: each test pass is predicted by a model fitted ONLY on rows that ended before it
    (refit every `block` test passes, using data before the first of them)."""
    passes = [r for r in rows if r["passed"] and "y" in r]
    res = []
    pool = []        # one-step-ahead residual vectors of earlier test passes (strictly past)
    pr = None
    fit_upto = -1
    for i, r in enumerate(passes):
        if i < min_train:
            continue
        if pr is None or i - fit_upto >= block:
            train_rows = [x for x in rows if x["ts"] < r["ts"] - 1]
            pr = Predictor(learner, hl, spread, cal, form_hl=form_hl, form_k=form_k, map_k=map_k, map_hl=map_hl,
                           pool_bw=pool_bw).fit(
                train_rows, r["ts"], pool=pool if len(pool) >= 20 else None)
            fit_upto = i
        if len(pool) >= 20:
            pr.pool = pool[-POOL_MAX:]          # residuals of every earlier test pass (strictly past)
        pr.hist = passes                        # map effect: every earlier pass (filtered by time)
        # knn may also use passes between the fit and this row (still strictly in the past)
        p = pr.predict_full(r["A"], r["x"], r["md5"], r["diff"], r["flags"], ts=r["ts"] - 1, now=r["ts"])
        if p is None:
            continue
        act = _actual_pp(r, cal)
        rv = {t: r["y"][t] - p["y"]["_base"][t] for t in TARGETS}     # vs the learner (form is built on these)
        pool.append((rv, r["ts"], r["x"]["stars"]))
        res.append({"res": rv, "ts": r["ts"], "md5": r["md5"], "diff": r["diff"], "pp": act, "pp_pred": p["pp_expected"],
                    "pp_at_median": p["pp_at_median"], "pp_low": p["pp_low"], "pp_high": p["pp_high"],
                    "acc": r["S"]["acc"], "acc_pred": p["acc_q"][1], "miss": r["miss"], "miss_pred": p["median_outcome"]["miss"],
                    "miss_pred_q": p["miss_q"][1], "combo": r["combo"], "combo_pred": p["median_outcome"]["combo"],
                    "stars": r["x"]["stars"], "srv": bool(r.get("pp_srv")), "n_eff": p["n_eff"],
                    "seen_map": any(x["md5"] == r["md5"] and x["ts"] < r["ts"] - 1 for x in passes[:i])})
    return summarize(res, learner, hl), (res if collect else None)


def summarize(res, learner, hl):
    if not res:
        return {"learner": learner, "hl": hl, "n": 0}
    n = len(res)
    ae = sorted(abs(x["pp_pred"] - x["pp"]) for x in res)
    ae_med = sorted(abs(x["pp_at_median"] - x["pp"]) for x in res)
    cov = sum(1 for x in res if x["pp_low"] is not None and x["pp_low"] <= x["pp"] <= x["pp_high"]) / n
    below = sum(1 for x in res if x["pp_low"] is not None and x["pp"] < x["pp_low"]) / n
    rec = res[-20:]
    return {"learner": learner, "hl": hl, "n": n,
            "pp_mae": round(sum(ae) / n, 2), "pp_medae": round(ae[n // 2], 2),
            "pp_mae_at_median_outcome": round(sum(ae_med) / n, 2),
            "pp_bias": round(sum(x["pp_pred"] - x["pp"] for x in res) / n, 2),
            "pp_mape": round(100 * sum(abs(x["pp_pred"] - x["pp"]) / max(1.0, x["pp"]) for x in res) / n, 1),
            "acc_mae": round(sum(abs(x["acc_pred"] - x["acc"]) for x in res) / n, 4),
            "miss_mae": round(sum(abs(x["miss_pred"] - x["miss"]) for x in res) / n, 2),
            "combo_mae": round(sum(abs(x["combo_pred"] - x["combo"]) for x in res) / n, 1),
            "range_coverage": round(cov, 3), "below_low": round(below, 3),
            "recent": {"n": len(rec), "pp_mae": round(sum(abs(x["pp_pred"] - x["pp"]) for x in rec) / len(rec), 2),
                       "pp_bias": round(sum(x["pp_pred"] - x["pp"] for x in rec) / len(rec), 2),
                       "range_coverage": round(sum(1 for x in rec if x["pp_low"] <= x["pp"] <= x["pp_high"]) / len(rec), 2)},
            "coverage_by_stars": _cov_bands(res),
            "pp_mean_actual": round(sum(x["pp"] for x in res) / n, 1)}


def _cov_bands(res):
    out = {}
    for name, lo, hi in (("<4.5", 0, 4.5), (">=4.5", 4.5, 99)):
        b = [x for x in res if lo <= x["stars"] < hi]
        if b:
            out[name] = {"n": len(b), "inside": round(sum(1 for x in b if x["pp_low"] <= x["pp"] <= x["pp_high"]) / len(b), 2),
                         "below": round(sum(1 for x in b if x["pp"] < x["pp_low"]) / len(b), 2),
                         "above": round(sum(1 for x in b if x["pp"] > x["pp_high"]) / len(b), 2)}
    return out


def pass_backtest(rows, hl=14.0, min_train=40):
    """Time-ordered check of p_pass ("pass chance if you play it out"). Brier/logloss on live attempts that
    ended in a pass or a fail; plus the censored log-likelihood over EVERY live attempt (pass: log S(1),
    fail at progress p: log hazard density, quit at p: log S(p)) so the 2/3 of tries that are quits count."""
    live = [r for r in rows if r.get("live") and "NF" not in r["flags"]]
    out = []
    cens = []
    pm, fit_i = None, -99
    for i, r in enumerate(live):
        if i < min_train:
            continue
        if pm is None or i - fit_i >= 3:
            train = [x for x in live[:i] if x["ts"] < r["ts"] - 1]
            pm = PassModel(hl=hl).fit(train, r["ts"])
            fit_i = i
            pf = [x for x in train if x.get("outcome") in ("passed", "failed")]
            base = (sum(1 for x in pf if x["passed"]) + 1) / (len(pf) + 2)
        p = pm.p_pass(r["x"])
        if p is not None:
            lam = -math.log(max(1e-6, min(1 - 1e-6, p)))
            prog = 1.0 if r["passed"] else max(0.02, min(1.0, r.get("progress") or 0.02))
            if r["passed"]:
                cens.append(-lam)
            elif r.get("outcome") == "failed":
                cens.append(math.log(lam) - lam * prog)
            else:
                cens.append(-lam * prog)
        if r.get("outcome") not in ("passed", "failed"):
            continue
        if p is None:
            continue
        y = 1.0 if r["passed"] else 0.0
        out.append((p, base, y))
    if not out:
        return {"n": 0}
    n = len(out)
    brier = sum((p - y) ** 2 for p, _, y in out) / n
    brier0 = sum((b - y) ** 2 for _, b, y in out) / n
    ll = -sum(y * math.log(max(1e-4, p)) + (1 - y) * math.log(max(1e-4, 1 - p)) for p, _, y in out) / n
    return {"n": n, "brier": round(brier, 4), "brier_base_rate": round(brier0, 4), "logloss": round(ll, 4),
            "n_all": len(cens), "censored_nll": round(-sum(cens) / len(cens), 4) if cens else None,
            "quit_event": QUIT_EVENT,
            "mean_p": round(sum(p for p, _, _ in out) / n, 3), "pass_rate": round(sum(y for *_, y in out) / n, 3)}


# ----------------------------------------------------------------------------
# persistence / training
# ----------------------------------------------------------------------------

MODEL_FILE = ".pp_model.json"
REBACKTEST_S = 7 * 86400
_mem = {"pred": None, "sig": None, "rows": None, "meta": None, "mtime": None}


def _slim_row(r):
    keep = ("ts", "started_ts", "md5", "diff", "rate", "flags", "src", "live", "passed", "outcome", "progress", "g", "ok",
            "meh", "miss", "combo", "acc", "pp_srv", "tails", "ticks", "tick_miss", "bid")
    return {k: r[k] for k in keep if k in r and r[k] is not None}


def calibration_pairs(rows):
    pairs = []
    for r in rows:
        if r.get("pp_srv") and r.get("S") and r.get("tails") is not None:
            pairs.append((r["A"], r["S"], r["flags"], float(r["pp_srv"]), 1.0))
    return pairs


def train(full_backtest=False, network=False, verbose=False, learners=None, hls=None, extra_pool=None):
    """Build the dataset, (optionally) run the learner comparison, fit the chosen predictor, save."""
    t0 = time.time()
    rows, skipped = build_dataset(network=network, verbose=verbose)
    cal = fit_calibration(calibration_pairs(rows))
    if not cal.get("w"):
        cal = (_load(_f(MODEL_FILE), {}) or {}).get("calibration") or cal
    old = _load(_f(MODEL_FILE), {}) or {}
    choice = old.get("choice") or {"learner": "gbm", "hl": HALF_LIFE_DAYS, "spread": 1.0, "pp_mode": "median"}
    table = old.get("backtest_table") or []
    pass_bt = old.get("pass_backtest") or {}
    if full_backtest:
        table = []
        for name in (learners or ("knn", "ridge", "gbm", "curve", "blend")):
            for hl in (hls or (7.0, 21.0)):
                tt = time.time()
                s, _ = backtest(rows, name, hl, cal)
                s["seconds"] = round(time.time() - tt, 1)
                table.append(s)
                if verbose:
                    print(json.dumps(s), flush=True)
        ok = [s for s in table if s.get("n")]
        if ok:
            best = min(ok, key=lambda s: s["pp_mae"])
            choice = {"learner": best["learner"], "hl": best["hl"], "spread": 1.0, "pp_mode": "median"}
            # range width: scale the residual spread so ~60% of actual pp falls inside p20-p80
            cov = best.get("range_coverage") or 0.6
            choice["spread"] = 1.0
            if cov and abs(cov - 0.6) > 0.02:
                lo, hi = 0.5, 1.8
                for _ in range(5):
                    mid = (lo + hi) / 2
                    s2, _ = backtest(rows, best["learner"], best["hl"], cal, spread=mid)
                    if s2["range_coverage"] < 0.6:
                        lo = mid
                    else:
                        hi = mid
                    if verbose:
                        print(f"  spread {mid:.2f}: coverage {s2['range_coverage']}", flush=True)
                choice["spread"] = round((lo + hi) / 2, 3)
        pass_bt = pass_backtest(rows)
    now = time.time()
    final_bt = old.get("backtest") or {}
    bt_detail = None
    pool = [_pool_in(e) for e in (old.get("pool") or [])] if old.get("choice") == choice else []
    if extra_pool:
        pool = (pool + list(extra_pool))[-POOL_MAX:]
    if full_backtest:
        final_bt, det = backtest(rows, choice["learner"], choice["hl"], cal, spread=choice["spread"], collect=True)
        bt_detail = [[round(d["stars"], 2), round(abs(d["pp_pred"] - d["pp"]), 2)] for d in det or []]
        pool = [(d["res"], d["ts"], d["stars"]) for d in det or []][-POOL_MAX:]
        _save(_f("pp_backtest_detail.json"), {"generated": int(time.time()), "choice": choice,
                                               "rows": [{k: v for k, v in d.items() if k != "res"} for d in det or []]})
    pr = Predictor(choice["learner"], choice["hl"], choice["spread"], cal, choice.get("pp_mode", "median"))
    pr.fit(rows, now, pool=pool)
    meta = {"v": MODEL_VERSION, "updated": int(now), "trained_on": pr.n, "n_rows": len(rows),
            "n_attempts_live": sum(1 for r in rows if r.get("live")),
            "skipped": dict(skipped), "choice": choice, "calibration": cal, "backtest_table": table,
            "backtest": final_bt, "pass_backtest": pass_bt, "aux": pr.aux,
            "pass_model": {"n": getattr(pr.passm, "n", 0), "n_fail": getattr(pr.passm, "n_fail", 0),
                           "w": pr.passm.w},
            "rows": [_slim_row(r) for r in rows], "last_ts": max((r["ts"] for r in rows), default=0),
            "predictor": pr.state(), "pool": [_pool_out(e) for e in pr.pool],
            "backtest_detail": (old.get("backtest_detail") if not full_backtest else bt_detail),
            "fit_seconds": round(time.time() - t0, 1),
            "backtest_ts": int(now) if full_backtest else old.get("backtest_ts", int(now))}
    _save(_f(MODEL_FILE), meta)
    _mem.update({"pred": pr, "rows": rows, "meta": meta, "mtime": _mtime(_f(MODEL_FILE))})
    return meta


def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _rows_from_meta(meta):
    out = []
    c = cache()
    for r in meta.get("rows") or []:
        A = c.d["attrs"].get(attr_key(r["md5"], r["diff"], r["rate"]))
        m = c.d["maps"].get(r["md5"]) or {}
        if not A or A.get("aim") is None:
            continue
        if A.get("src") == "api" and custom_rate(r["diff"], r["rate"]):
            continue
        rr = dict(r)
        rr["A"] = A
        rr["x"] = feats(A, m, r["diff"], r["rate"])
        if r.get("passed"):
            if pp_trainable(r, A):
                rr["y"] = targets(r, A)
            rr["S"] = _row_stats(r, A)
            rr["tail_drop"] = (A["nS"] - rr["S"]["tails"]) / A["nS"] if A["nS"] else 0.0
            rr["meh_share"] = r["meh"] / (r["ok"] + r["meh"]) if (r["ok"] + r["meh"]) else None
        out.append(rr)
    set_lrep(out)
    return out


def predictor():
    """The fitted predictor (loaded from .pp_model.json, refit in memory from its stored rows)."""
    with _lock:
        mt = _mtime(_f(MODEL_FILE))
        if _mem["pred"] is not None and _mem["mtime"] == mt:
            return _mem["pred"]
        meta = _load(_f(MODEL_FILE), None)
        if not isinstance(meta, dict) or meta.get("v") != MODEL_VERSION:
            return None
        rows = _rows_from_meta(meta)
        try:
            pr = Predictor.from_state(meta["predictor"], rows, meta.get("calibration"))
        except Exception as e:
            _log(f"predictor state unusable ({type(e).__name__}: {e}); refitting")
            ch = meta.get("choice") or {}
            pr = Predictor(ch.get("learner", "gbm"), ch.get("hl", HALF_LIFE_DAYS), ch.get("spread", 1.0),
                           meta.get("calibration"), ch.get("pp_mode", "median"))
            pr.fit(rows, time.time())
        _mem.update({"pred": pr, "rows": rows, "meta": meta, "mtime": mt})
        return pr


# ----------------------------------------------------------------------------
# public API (CONTRACT_PP2.md)
# ----------------------------------------------------------------------------

MOD_FAMILY_MIN = 8


def mod_family(x):
    """'' for NM/HD; otherwise the speed/HR family of a feature vector ('DT', 'HT', 'HR', 'DTHR', ...)."""
    lr = x.get("lrate") or 0.0
    f = ("DT" if lr > 0.01 else "HT" if lr < -0.01 else "") + ("HR" if x.get("hr") else "")
    return f


def _conf(p, x, pr):
    neff = p.get("n_eff") or 0.0
    stars = x["stars"]
    near = sum(recency_w(r["ts"], pr.now, pr.hl) for r in pr.passes if abs(r["x"]["stars"] - stars) <= 0.4)
    n_sim = sum(1 for r in pr.passes if abs(r["x"]["stars"] - stars) <= 0.4)
    hi = max((r["x"]["stars"] for r in pr.passes), default=0)
    fam = mod_family(x)
    n_fam = None
    if fam:
        # DT/HT/HR predictions extrapolate from very few rows (2026-11-10: 3 clean DT passes): need >= 8
        # recent passes with that mod family before saying anything better than "low"
        n_fam = sum(1 for r in pr.passes if mod_family(r["x"]) == fam and pr.now - r["ts"] <= 30 * 86400)
    if stars > hi + 0.3 or n_sim < 4 or (n_fam is not None and n_fam < MOD_FAMILY_MIN):
        c = "low"
    elif near >= 6 or neff >= 2:
        c = "high"
    else:
        c = "medium"
    return c, n_sim, near


def _model_error_near(stars):
    meta = _mem.get("meta") or {}
    bt = meta.get("backtest_detail") or []
    near = [e for s, e in bt if abs(s - stars) <= 0.5]
    if len(near) >= 5:
        return round(sum(near) / len(near), 1)
    b = meta.get("backtest") or {}
    return b.get("pp_mae")


def _prep(md5, mods, rate, osu_path=None, beatmap_id=None, network=True, tosu=False):
    diff, r, flags, assisted = norm_mods(mods, rate)
    if assisted:
        return None
    m = map_info(md5, osu_path, beatmap_id)
    A = get_attrs(md5, diff, r, osu_path, beatmap_id, network=network, tosu=tosu)
    if not A or A.get("aim") is None:
        return None
    m = cache().d["maps"].get(md5) or m
    x = feats(A, m, diff, r)
    rows = _mem.get("rows") or []
    x["lrep"] = math.log1p(sum(1 for rw in rows if rw.get("md5") == md5))
    return diff, r, sorted(flags), A, m, x


def predict_outcome(md5, mods="NM", rate=None, osu_path=None, beatmap_id=None, network=True, tosu=False):
    """Outcome distribution (given a pass) + pass chance; None when the map/model is unavailable."""
    try:
        pr = predictor()
        if pr is None or pr.model is None:
            return None
        prep = _prep(md5, mods, rate, osu_path, beatmap_id, network, tosu)
        if prep is None:
            return None
        diff, r, flags, A, m, x = prep
        p = pr.predict_full(A, x, md5, diff, flags)
        if p is None:
            return None
        return _outcome_view(p, x, pr, A, m, diff, r, md5)
    except Exception as e:
        _log(f"predict_outcome failed: {type(e).__name__}: {e}")
        return None


def _outcome_view(p, x, pr, A, m, diff, rate, md5):
    conf, n_sim, _ = _conf(p, x, pr)
    same = [rw for rw in pr.passes if rw["md5"] == md5 and rw["diff"] == diff]
    basis = f"{n_sim} of your passes within 0.4* of {x['stars']:.2f}*"
    if same:
        basis += f", {len(same)} on this exact map+mods"
    basis += f"; {pr.learner_name} model, recent plays weigh most ({pr.hl:g}-day half-life)"
    if pr.passm.w is None:
        basis += "; pass chance unknown (not enough live attempts yet)"
    aq, mq, cq = p["acc_q"], p["miss_q"], p["combo_q"]
    return {"p_pass": round(p["p_pass"], 3) if p["p_pass"] is not None else None,
            "acc": {"p20": round(aq[0], 4), "p50": round(aq[1], 4), "p80": round(aq[2], 4)},
            "misses": {"p20": int(mq[0]), "p50": int(mq[1]), "p80": int(mq[2])},
            "combo_frac": {"p20": round(cq[0] / A["max_combo"], 3), "p50": round(cq[1] / A["max_combo"], 3),
                           "p80": round(cq[2] / A["max_combo"], 3)},
            "combo": {"p20": int(cq[0]), "p50": int(cq[1]), "p80": int(cq[2]), "max": A["max_combo"]},
            "n_similar": n_sim, "confidence": conf, "basis": basis,
            "stars": round(A["stars"], 2) if A.get("stars") else None, "attr_source": A.get("src")}


def predict_pp(md5, mods="NM", rate=None, osu_path=None, beatmap_id=None, network=True, tosu=False,
               top_plays=None, total_pp=None):
    """CONTRACT_PP2 predict_pp: pp at your median outcome given a pass, p20/p80 range, FC/SS, pass chance and
    the expected change of your total pp. None when the map can't be evaluated (no attributes / no model)."""
    try:
        pr = predictor()
        if pr is None or pr.model is None:
            return None
        prep = _prep(md5, mods, rate, osu_path, beatmap_id, network, tosu)
        if prep is None:
            return None
        diff, r, flags, A, m, x = prep
        p = pr.predict_full(A, x, md5, diff, flags)
        if p is None:
            return None
        ov = _outcome_view(p, x, pr, A, m, diff, r, md5)
        n = A["nC"] + A["nS"] + A["nSp"]
        ss = {"great": n, "ok": 0, "meh": 0, "miss": 0, "combo": A["max_combo"], "acc": 1.0, "tails": A["nS"]}
        med = p["median_outcome"]
        fc = dict(med)
        fc.update({"great": n - med["ok"] - med["meh"], "miss": 0, "combo": A["max_combo"], "tails": A["nS"],
                   "tick_miss": 0})
        fc["acc"] = lazer_acc(fc["great"], fc["ok"], fc["meh"], 0, A["nS"], A["nS"], A["nLT"], A["nLT"])
        pp_ss = pp_calc(A, ss, flags, pr.cal)
        pp_fc = pp_calc(A, fc, flags, pr.cal)
        # expected gain to the weighted total: average over the pp distribution, times the pass chance
        if top_plays is None:
            ppd = _load(_f("pp_data.json"), {}) or {}
            top_plays = ppd.get("top_plays") or []
            if total_pp is None:
                total_pp = (ppd.get("user") or {}).get("pp")
        bid = m.get("bid")
        gains = None
        if top_plays:
            pl = _pl()

            def rg(v):
                try:
                    return pl.rank_gain_pp(v, top_plays, beatmap_id=bid, md5=md5, total_pp=total_pp) or 0.0
                except TypeError:       # older pp_live without the below-#100 correction
                    return pl.rank_gain_pp(v, top_plays, beatmap_id=bid, md5=md5) or 0.0
            gvals = [(rg(s[0]), s[1]) for s in p["samples"]]
            tw = sum(w for _, w in gvals)
            eg = sum(gv * w for gv, w in gvals) / tw if tw > 0 else 0.0
            gains = (eg, rg(p["pp_high"]))
        pp_pass = p["p_pass"]
        unranked = m.get("status") not in (None, "ranked", "approved")
        out = {"pp_expected": round(p["pp_expected"], 1), "pp_low": round(min(p["pp_low"], p["pp_expected"]), 1),
               "pp_high": round(max(p["pp_high"], p["pp_expected"]), 1), "pp_fc": round(pp_fc, 1),
               "pp_ss": round(pp_ss, 1), "p_pass": ov["p_pass"],
               "stars": round(A["stars"], 2) if A.get("stars") else None,
               "gain_expected": None, "gain_high": None, "confidence": ov["confidence"], "basis": ov["basis"],
               "model_error_pp": _model_error_near(x["stars"]),
               "acc": ov["acc"], "misses": ov["misses"], "combo_frac": ov["combo_frac"], "n_similar": ov["n_similar"],
               "attr_source": A.get("src"), "status": m.get("status"), "beatmap_id": bid,
               "mods": display_mods(diff), "rate": r, "learner": pr.learner_name}
        if gains is not None and not unranked:
            out["gain_expected"] = round(gains[0] * (pp_pass if pp_pass is not None else 1.0), 2)
            out["gain_if_pass"] = round(gains[0], 2)       # mean gain given a pass (what the bubble says)
            out["gain_high"] = round(max(gains[1], gains[0]), 2)
        elif unranked:
            out["basis"] += f"; map status {m.get('status')} - no pp counts"
        # old pp_live.predict() keys, so older UI code keeps working
        out.update({"pp_good_run": out["pp_high"], "pp_fc_pred_acc": out["pp_fc"],
                    "acc_expected": ov["acc"]["p50"], "acc_good": ov["acc"]["p80"],
                    "misses_expected": ov["misses"]["p50"], "misses_good": ov["misses"]["p20"]})
        return out
    except Exception as e:
        _log(f"predict_pp failed: {type(e).__name__}: {e}")
        return None


NOT_TRAINED_NOTE = ("Not trained yet: play a few ranked maps (osu!lazer keeps a replay of each; with tosu running "
                    "fails and quits count too), then run 'python pp_model.py --update'. Until then pp predictions "
                    "use a rough star-rating curve.")


def model_report():
    meta = _mem.get("meta")
    if meta is None or _mem.get("mtime") != _mtime(_f(MODEL_FILE)):
        meta = _load(_f(MODEL_FILE), None)
    if not isinstance(meta, dict):
        return {"trained_on": 0, "updated": None, "learner": None, "backtest": {}, "recent": {"n": 0},
                "note": NOT_TRAINED_NOTE}
    bt = meta.get("backtest") or {}
    rec = recent_accuracy(20)
    ch = meta.get("choice") or {}
    return {"trained_on": meta.get("trained_on"), "updated": meta.get("updated"),
            "learner": f"{ch.get('learner')} (half-life {ch.get('hl')} d)",
            "backtest": {k: bt.get(k) for k in ("pp_mae", "pp_medae", "acc_mae", "miss_mae", "range_coverage", "n",
                                                 "pp_bias")},
            "recent": rec if rec.get("n") else {"n": (bt.get("recent") or {}).get("n", 0),
                                                "pp_mae": (bt.get("recent") or {}).get("pp_mae"),
                                                "source": "backtest (no logged predictions scored yet)"},
            "pass_model": meta.get("pass_backtest"), "calibration": {k: (meta.get("calibration") or {}).get(k)
                                                                        for k in ("n", "mae_pct_raw", "mae_pct_cal")},
            "comfort_stars": comfort_stars(), "n_attempts_live": meta.get("n_attempts_live")}


def comfort_stars():
    """Hardest star rating where your median predicted run is >= 90% hit-accuracy and <= 1 miss/100 objects:
    from the fitted rows (recency weighted)."""
    try:
        pr = predictor()      # not _mem: the answer must not depend on whether something loaded the model
    except Exception:
        pr = None
    if pr is None or not pr.passes:
        return None
    pts = sorted(((r["x"]["stars"], recency_w(r["ts"], pr.now, pr.hl), r) for r in pr.passes), key=lambda t: t[0])
    best = None
    s = 3.0
    while s <= 7.0:
        win = [(w, r) for st, w, r in pts if abs(st - s) <= 0.35]
        if sum(w for w, _ in win) >= 1.0:
            mr = wquant([r["miss"] / max(1, r["A"]["nC"] + r["A"]["nS"]) for _, r in win], [w for w, _ in win], 0.5)
            if mr is not None and mr <= 0.02:
                best = s
        s += 0.1

    def scan(lo_tenths, hi_tenths):
        found = None
        for k in range(lo_tenths, hi_tenths + 1):
            s = k / 10.0
            win = [(w, r) for st, w, r in pts if abs(st - s) <= 0.35]
            if sum(w for w, _ in win) >= 1.0:
                mr = wquant([r["miss"] / max(1, r["A"]["nC"] + r["A"]["nS"]) for _, r in win],
                            [w for w, _ in win], 0.5)
                if mr is not None and mr <= 0.02:
                    found = s
        return found
    # players outside 3-7*: look below 3* when nothing qualified, above 7* when the top of the scan did
    if best is None:
        best = scan(15, 29)
    elif best >= 6.95:
        best = scan(71, 90) or best
    return round(best, 1) if best else None


# ----------------------------------------------------------------------------
# prediction log + self-learning update
# ----------------------------------------------------------------------------

LOG_FILE = "pp_predictions.jsonl"


def _append_log(rec):
    try:
        line = (json.dumps(rec, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        p = _f(LOG_FILE)
        with _lock:
            fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except Exception as e:
        _log(f"log append failed: {e}")
        return False


def read_log():
    out = []
    try:
        with open(_f(LOG_FILE), "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def _pred_snapshot(p):
    if not p:
        return None
    return {k: p.get(k) for k in ("pp_expected", "pp_low", "pp_high", "pp_fc", "p_pass", "acc", "misses", "stars",
                                   "confidence", "learner", "attr_source", "gain_expected")}


def log_pre(md5, mods, rate, prediction, when=None, source="hover", beatmap_id=None):
    """Snapshot of the prediction made BEFORE an attempt (hover / play start)."""
    if not md5 or not prediction:
        return False
    diff, r, _, _ = norm_mods(mods, rate)
    return _append_log({"kind": "pre", "ts": round(when or time.time(), 3), "md5": md5, "mods": display_mods(diff),
                        "rate": r, "beatmap_id": beatmap_id, "source": source, "predicted": _pred_snapshot(prediction)})


def recent_accuracy(n=20):
    """Over the last n scored passes: mean |predicted - actual| pp and how often actual was inside the range."""
    sc = [r for r in read_log() if r.get("kind") == "scored" and (r.get("actual") or {}).get("passed")
          and (r.get("actual") or {}).get("pp") is not None and (r.get("predicted") or {}).get("pp_expected") is not None]
    sc = sc[-n:]
    if not sc:
        return {"n": 0}
    err = [abs(r["predicted"]["pp_expected"] - r["actual"]["pp"]) for r in sc]
    inside = [1 for r in sc if (r["predicted"].get("pp_low") or -1) <= r["actual"]["pp"] <= (r["predicted"].get("pp_high") or 1e9)]
    return {"n": len(sc), "pp_mae": round(sum(err) / len(err), 1), "within_range": round(len(inside) / len(sc), 2),
            "source": "logged pre-attempt predictions"}


def _row_actual(r, cal):
    out = {"passed": bool(r["passed"]), "outcome": r.get("outcome") or ("passed" if r["passed"] else None),
           "acc": r.get("acc"), "misses": r["miss"], "combo": r["combo"], "progress": r.get("progress")}
    if r["passed"] and r.get("A") and r.get("S"):
        out["pp"] = round(_actual_pp(r, cal), 2)
        out["pp_source"] = "server" if r.get("pp_srv") else "formula"
    return out


def update(force=False, network=None, verbose=False):
    """Ingest attempts newer than the last fit; score each against the prediction made before it (logged
    hover/play-start snapshot, else the model as it was before the attempt); refit; save."""
    with _lock:
        t0 = time.time()
        meta = _load(_f(MODEL_FILE), None)
        if network is None:
            network = not os.environ.get("OSU_COACH_NO_NETWORK")
        if not isinstance(meta, dict) or meta.get("v") != MODEL_VERSION:
            if not raw_rows():
                # nothing to learn from yet (fresh install): don't write an empty model
                return {"status": "no data", "trained_on": 0}
            m = train(full_backtest=True, network=network, verbose=verbose)
            return {"status": "trained", "trained_on": m["trained_on"], "seconds": round(time.time() - t0, 1)}
        last_ts = meta.get("last_ts") or 0
        rows_now = raw_rows()
        new = [r for r in rows_now if r["ts"] > last_ts + 0.5]
        if not new and not force:
            return {"status": "up to date", "trained_on": meta.get("trained_on")}
        if network and new:
            # server attributes for the new maps (cached forever) and your server scores for new passes
            # (exact pp + slider stats: the truth the pp formula is calibrated against) - a few paced calls
            calls = 0
            for r in new:
                try:
                    if attr_key(r["md5"], r["diff"], r["rate"]) not in cache().d["attrs"] and calls < 12:
                        calls += 1
                        get_attrs(r["md5"], r["diff"], r["rate"], beatmap_id=r.get("bid"), network=True)
                except Exception as e:
                    _log(f"update attrs: {e}")
            bids = []
            for r in new:
                m = cache().d["maps"].get(r["md5"]) or {}
                if r["passed"] and m.get("bid") and m.get("status") in ("ranked", "approved", "loved") \
                        and m["bid"] not in bids:
                    bids.append(m["bid"])
            for bid in bids[:6]:
                try:
                    fetch_user_scores(bid)
                except Exception as e:
                    _log(f"update scores {bid}: {e}")
            cache().flush()
            if bids:
                new = [r for r in raw_rows() if r["ts"] > last_ts + 0.5]   # now with server pp attached
        pr_before = predictor()          # fitted only on data up to last_ts: honest "before" predictions
        pres = [x for x in read_log() if x.get("kind") == "pre"]
        scored_keys = {(x.get("md5"), round(x.get("attempt_ts") or 0, 1)) for x in read_log() if x.get("kind") == "scored"}
        n_scored = 0
        new_res = []
        for r in new:
            key = (r["md5"], round(r["ts"], 1))
            if key in scored_keys:
                continue
            prep = _prep(r["md5"], r["diff"] + [f for f in r["flags"] if f == "NF"], r["rate"], network=False)
            start = r.get("started_ts") or (r["ts"] - 600)
            snap = None
            for x in reversed(pres):
                if x.get("md5") == r["md5"] and x.get("mods") == display_mods(r["diff"]) and \
                        abs((x.get("rate") or 1.0) - r["rate"]) < 0.01 and start - 900 <= (x.get("ts") or 0) <= r["ts"]:
                    snap = x
                    break
            pred, src = (snap.get("predicted"), snap.get("source")) if snap else (None, None)
            if pred is None and pr_before is not None and prep is not None:
                diff, rate, flags, A, m, xf = prep
                p = pr_before.predict_full(A, xf, r["md5"], diff, flags)
                if p:
                    pred = {"pp_expected": round(p["pp_expected"], 1), "pp_low": round(p["pp_low"], 1),
                            "pp_high": round(p["pp_high"], 1), "p_pass": p["p_pass"],
                            "acc": {"p50": round(p["acc_q"][1], 4)}, "misses": {"p50": p["miss_q"][1]},
                            "stars": A.get("stars"), "learner": pr_before.learner_name}
                    src = "model_before_attempt"
            rr = dict(r)
            if prep is not None:
                diff, rate, flags, A, m, xf = prep
                rr["A"] = A
                if r["passed"]:
                    rr["S"] = _row_stats(r, A)
                    if pr_before is not None and pr_before.model is not None and pp_trainable(r, A):
                        yp = pr_before.target_pred(xf, r["md5"], diff, form=False)
                        if yp:
                            ya = targets(r, A)
                            new_res.append(({t: ya[t] - yp[t] for t in TARGETS}, r["ts"], xf.get("stars")))
            actual = _row_actual(rr, (meta.get("calibration") or {}))
            _append_log({"kind": "scored", "ts": round(time.time(), 3), "attempt_ts": round(r["ts"], 3),
                         "md5": r["md5"], "mods": display_mods(r["diff"]), "rate": r["rate"], "src": r["src"],
                         "predicted": pred, "prediction_source": src, "actual": actual})
            n_scored += 1
        # the learner comparison is re-run weekly (more data can change which learner wins)
        full = time.time() - (meta.get("backtest_ts") or 0) > REBACKTEST_S
        m = train(full_backtest=full, network=False, verbose=verbose, extra_pool=new_res)
        return {"status": "updated", "new_attempts": len(new), "scored": n_scored, "trained_on": m["trained_on"],
                "seconds": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _below_normal_priority():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)
    except Exception:
        pass


def main(argv):
    _below_normal_priority()
    if "--fetch" in argv:
        i = argv.index("--fetch")
        n = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 400
        print(json.dumps(fetch_missing(max_calls=n, verbose=True)))
        return 0
    if "--backtest" in argv:
        hls = None
        if "--hl" in argv:
            hls = tuple(float(v) for v in argv[argv.index("--hl") + 1].split(","))
        m = train(full_backtest=True, verbose=True, hls=hls)
        print(json.dumps({k: m[k] for k in ("choice", "backtest", "pass_backtest", "calibration", "trained_on",
                                             "skipped")}, indent=1))
        return 0
    if "--update" in argv:
        res = update(force="--force" in argv, verbose=True)
        print(json.dumps(res))
        if isinstance(res, dict) and not res.get("trained_on"):
            print(NOT_TRAINED_NOTE)
        return 0
    if "--predict" in argv:
        i = argv.index("--predict")
        md5 = argv[i + 1]
        mods = argv[i + 2] if len(argv) > i + 2 else "NM"
        rate = float(argv[i + 3]) if len(argv) > i + 3 else None
        t = time.perf_counter()
        p = predict_pp(md5, mods, rate)
        print(json.dumps(p, indent=1))
        print(f"{(time.perf_counter() - t) * 1000:.0f} ms")
        return 0
    rep = model_report()
    print(json.dumps(rep, indent=1))
    if rep.get("note"):
        print(rep["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
