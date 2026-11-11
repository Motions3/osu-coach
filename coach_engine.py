#!/usr/bin/env python3
"""
coach_engine.py - builds coach_data.json for the osu! pet companion (see CONTRACT.md).

  python coach_engine.py [--last N] [--out coach_data.json] [--player NAME] [--osu-dir DIR]

Reads osu! data READ-ONLY (beatmaps + replays), re-judges replays with osu_coach.py,
measures the player's abilities and picks concrete practice maps from the local library.
Caches (next to this script): .coach_replays.json (per-replay summaries),
.coach_library.json (per-beatmap features), plus osu_coach's .osu_coach_cache.json.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import lzma
import math
import os
import random
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import osu_coach as oc  # noqa: E402
import coach_knowledge as K  # noqa: E402
import coach_identity as identity  # noqa: E402

try:  # written in parallel by the live builder; everything here works without it
    import pp_live  # noqa: E402
except Exception:
    pp_live = None
try:  # adaptive loop (CONTRACT_ADAPT.md); the classic plan below is the fallback if it fails
    import coach_adapt  # noqa: E402
except Exception:
    coach_adapt = None

ENGINE_VER = 6      # bump to invalidate replay summaries (6: per-stream runs + 10 s sections for debriefs)
LIB_VER = 3         # bump to invalidate library features
REPLAY_CACHE = HERE / ".coach_replays.json"
LIB_CACHE = HERE / ".coach_library.json"
STATUS_FILE = HERE / "coach_status.json"
PP_DATA = HERE / "pp_data.json"              # written by osu_api.py (may be absent)
SR_CACHE = HERE / ".coach_sr_cache.json"     # written by pp_live.py (may be absent)
PHASE_FILE = HERE / ".coach_phase.json"      # ours: remembers when the current phase started
SETTINGS_FILE = HERE / "pet_settings.json"   # the pet's; read only here

CAT = {"stream": "s", "burst": "b", "jump": "j", "slider": "l", "other": "o", "after break": "a"}
CAT_NAME = {v: k for k, v in CAT.items()}
REASON = {"aim (tapped in time, cursor off the circle)": "A", "aim": "A",
          "timing (tapped too early)": "E", "timing / no tap (cursor was on it)": "T",
          "lost / no tap (cursor elsewhere)": "L"}
REASON_TXT = {"A": "aim - tapped in time but cursor off the circle",
              "E": "timing - tapped too early",
              "T": "timing - cursor was on the note but the tap was off-time or missing",
              "L": "reading - cursor was elsewhere and no tap",
              "?": "unclear"}
KIND = {"s": "streams", "b": "bursts", "j": "jumps", "l": "sliders", "o": "rhythm", "a": "rhythm"}

# ----------------------------------------------------------------------------
# small utils
# ----------------------------------------------------------------------------


def log(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def atomic_write_json(path: Path, obj, indent=None, only_if_changed=False):
    """Write JSON atomically. only_if_changed: skip the write when the file already holds exactly this (the
    caches live in OneDrive: rewriting MBs of unchanged data on every run costs CPU, disk and upload while the
    player plays)."""
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False)
    if only_if_changed:
        try:
            if path.stat().st_size == len(data.encode("utf-8")) and path.read_text("utf-8") == data:
                return
        except OSError:
            pass
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    for attempt in range(20):  # the pet may have the file open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def status(state, message):
    try:
        atomic_write_json(STATUS_FILE, {"state": state, "message": message[:300], "updated": int(time.time())})
    except Exception:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def r1(x):
    return None if x is None else round(float(x), 1)


def r2(x):
    return None if x is None else round(float(x), 2)


def r3(x):
    return None if x is None else round(float(x), 3)


def mss(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def pooled_ur(triples):
    c = sum(t[0] for t in triples)
    if c < 2:
        return None
    s = sum(t[1] for t in triples)
    ss = sum(t[2] for t in triples)
    var = max(0.0, ss / c - (s / c) ** 2)
    return math.sqrt(var) * 10


def pooled_mean(triples):
    c = sum(t[0] for t in triples)
    return sum(t[1] for t in triples) / c if c else None


def clamp(x, a, b):
    return max(a, min(b, x))


def preempt(ar):
    return 1800 - 120 * ar if ar < 5 else 1200 - 150 * (ar - 5)


def ar_from_preempt(ms):
    return (1800 - ms) / 120 if ms > 1200 else 5 + (1200 - ms) / 150


def ar_at(ar, rate=1.0, hr=False, ez=False):
    if hr:
        ar = min(10, ar * 1.4)
    if ez:
        ar *= 0.5
    return ar_from_preempt(preempt(ar) / rate)


def od_at(od, rate=1.0, hr=False, ez=False):
    if hr:
        od = min(10, od * 1.4)
    if ez:
        od *= 0.5
    w = (80 - 6 * od) / rate
    return (80 - w) / 6


def week_start(ts):
    d = datetime.fromtimestamp(ts).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def ticks_to_unix(ticks):
    return (ticks - 621355968000000000) / 1e7


ASSISTED_BITS = 128 | 2048 | 8192 | 4194304   # RX, AT, AP, CN (lazer also writes these legacy bits)


def game_ini_username(root):
    """Account logged in to osu! (lazer game.ini or stable osu!.*.cfg in `root`, opened read-only), or None."""
    return identity.game_username(root)


# ----------------------------------------------------------------------------
# replay header (+ lazer score info appended after the frames)
# ----------------------------------------------------------------------------


def read_header(path):
    d = Path(path).read_bytes()
    r = oc.Reader(d)
    mode = r.byte()
    ver = r.int()
    md5 = r.string()
    player = r.string()
    r.string()
    n300, n100, n50, _g, _k, miss = (r.short() for _ in range(6))
    score = r.int()
    combo = r.short()
    r.byte()
    mods = r.int()
    r.string()
    ticks = r.long()
    clen = r.int()
    r.p += max(0, clen)
    info = None
    if ver >= 30000001 and len(d) - r.p >= 12:
        try:
            r.long()  # online score id
            m = r.int()
            if 0 < m <= len(d) - r.p:
                info = json.loads(lzma.decompress(r.raw(m)).decode("utf-8", "replace"))
        except Exception:
            info = None
    tot = n300 + n100 + n50 + miss
    acc = (300 * n300 + 100 * n100 + 50 * n50) / (300 * tot) if tot else 0.0
    legacy = oc.Replay(str(path), mode, ver, md5, player, n300, n100, n50, miss, score, combo, mods, ticks, [])
    mod_str, rate, da = legacy.mod_str, legacy.rate, {}
    uid = None
    if isinstance(info, dict):
        uid = info.get("user_id")
        acs, rate = [], 1.0
        for m in info.get("mods") or []:
            a = str(m.get("acronym", "")).upper()
            st = m.get("settings") or {}
            if a in ("DT", "NC"):
                rate = float(st.get("speed_change", 1.5))
            elif a in ("HT", "DC"):
                rate = float(st.get("speed_change", 0.75))
            elif a == "DA":
                da = {k: st[k] for k in ("circle_size", "approach_rate", "overall_difficulty") if k in st}
            if a and a != "CL":
                acs.append(a)
        mod_str = "".join(acs) or "NM"
        la = lazer_accuracy(info)
        if la is not None:
            acc = la
    return {"mode": mode, "md5": md5, "player": player, "uid": uid, "ts": int(ticks_to_unix(ticks)),
            "score": score, "mods": mod_str, "mbits": mods, "rate": round(rate, 3), "acc": round(acc, 4),
            "misses": miss, "combo": combo, "da": da}


BASE = {"great": 300, "ok": 100, "meh": 50, "large_tick_hit": 30, "small_tick_hit": 10, "slider_tail_hit": 150}


def lazer_accuracy(info):
    st, mx = info.get("statistics") or {}, info.get("maximum_statistics") or {}
    num = sum(BASE[k] * int(st.get(k, 0)) for k in BASE)
    den = sum(BASE[k] * int(mx.get(k, 0)) for k in BASE)
    return num / den if den > 0 else None


# ----------------------------------------------------------------------------
# per-replay compact summary
# ----------------------------------------------------------------------------


def compact_summary(path, hdr, bm_path):
    bm = oc.parse_osu(Path(bm_path).read_text("utf-8", "replace"))
    da = hdr.get("da") or {}
    if "circle_size" in da: bm.cs = float(da["circle_size"])
    if "overall_difficulty" in da: bm.od = float(da["overall_difficulty"])
    if "approach_rate" in da: bm.ar = float(da["approach_rate"])
    rep = oc.parse_osr(path)
    rep.rate_override = hdr["rate"]
    s = oc.summarize(rep, bm)
    tgt = s["js"]
    rate = rep.rate
    cats, bpm_b, sp_b, vel_b = {}, {}, {}, {}
    misses = []
    reasons = Counter()
    jal = [0.0, 0]
    for j in tgt:
        cc = CAT.get(j.category, "o")
        c = cats.setdefault(cc, [0, 0, 0, 0.0, 0.0])
        c[0] += 1
        miss = j.result == "miss"
        c[1] += miss
        if j.error is not None:
            c[2] += 1; c[3] += j.error; c[4] += j.error * j.error
        extra = 0.0
        if j.run_len >= 7:
            k = str(int(j.bpm_stream // 10 * 10))
            b = bpm_b.setdefault(k, [0, 0]); b[0] += 1; b[1] += miss
        if j.run_len >= 3:
            extra = j.bpm_stream
        if j.category == "jump":
            v = j.dist_in / j.dt_in
            extra = v
            k = f"{math.floor(v * 4) / 4:.2f}"
            b = vel_b.setdefault(k, [0, 0, 0]); b[0] += 1; b[1] += miss
            b[2] += miss and j.miss_reason.startswith("aim")
            k = str(round(j.dist_in / (2 * s["radius"])))
            b = sp_b.setdefault(k, [0, 0]); b[0] += 1; b[1] += miss
            if j.along is not None:
                jal[0] += j.along; jal[1] += 1
        if miss:
            rc = REASON.get(j.miss_reason, "?")
            reasons[rc] += 1
            misses.append([int(j.obj.time), cc, rc, round(extra, 2)])
    same = alt = 0
    for a, b in zip(tgt, tgt[1:]):
        if b.run_len >= 3 and b.run_pos > 0 and a.key and b.key:
            if a.key == b.key: same += 1
            else: alt += 1
    errs = [j.error for j in tgt if j.error is not None]
    th = s["thirds"]
    # notes/misses per category in the first and last third (same split as s["thirds"]) - lets the
    # stamina check compare like with like instead of blaming fatigue for a harder climax
    thc = {}
    nt = len(tgt)
    for idx, j in enumerate(tgt):
        part = 0 if idx < nt // 3 else 1 if idx >= 2 * nt // 3 else None
        if part is None:
            continue
        v = thc.setdefault(CAT.get(j.category, "o"), [0, 0, 0, 0])
        v[2 * part] += 1
        v[2 * part + 1] += j.result == "miss"
    # long streams one by one (for "you drifted 22 ms late in the 1:12 stream"):
    # [start_ms, end_ms, notes, effective bpm, misses, drift ms (late half - early half) | None, mean err | None]
    runs = []
    for idx, j in enumerate(tgt):
        if j.run_len >= 7 and j.run_pos == 0:
            run = tgt[idx:idx + j.run_len]
            half = j.run_len / 2
            e1 = [q.error for q in run if q.error is not None and q.run_pos < half]
            e2 = [q.error for q in run if q.error is not None and q.run_pos >= half]
            ea = e1 + e2
            runs.append([int(run[0].obj.time), int(run[-1].obj.time), len(run), round(j.bpm_stream),
                         sum(1 for q in run if q.result == "miss"),
                         round(mean(e2) - mean(e1), 1) if e1 and e2 else None,
                         round(mean(ea), 1) if ea else None])
    if len(runs) > 60:
        runs = sorted(sorted(runs, key=lambda r: -r[2])[:60])
    # 10-second sections of map time: [t0_s, notes, misses, 100s+50s, n_err, sum_err, sumsq_err]
    seg = {}
    for j in tgt:
        k = int(j.obj.time // 10000) * 10
        g = seg.setdefault(k, [k, 0, 0, 0, 0, 0.0, 0.0])
        g[1] += 1
        if j.result == "miss":
            g[2] += 1
        elif j.result != "300":
            g[3] += 1
        if j.error is not None:
            g[4] += 1; g[5] += j.error; g[6] += j.error * j.error
    seg = [[g[0], g[1], g[2], g[3], g[4], round(g[5], 1), round(g[6])] for _, g in sorted(seg.items())]
    objs = [o for o in bm.objects if o.kind != "spinner"]
    length = ((max(o.end_time for o in bm.objects) - bm.objects[0].time) / 1000 / rate) if bm.objects else 0
    hr, ez = bool(rep.mods & 16), bool(rep.mods & 2)
    return {
        "ts": hdr["ts"], "md5": hdr["md5"], "mods": hdr["mods"], "rate": rate,
        "acc": hdr["acc"], "misses": hdr["misses"], "combo": hdr["combo"],
        "rj_misses": s["misses"], "n": s["n"], "ur": r1(s["ur"]), "mean_err": r2(s["mean_err"]),
        "e": [len(errs), round(sum(errs), 2), round(sum(e * e for e in errs), 1)],
        "cats": {k: [v[0], v[1], v[2], round(v[3], 2), round(v[4], 1)] for k, v in cats.items()},
        "thirds": [t["miss"] for t in th] + [t["n"] for t in th], "thc": thc,
        "drift": r2(s["stream_drift"]), "same": same, "alt": alt,
        "bpm_b": bpm_b, "sp_b": sp_b, "vel_b": vel_b, "jal": [round(jal[0], 2), jal[1]],
        "reasons": dict(reasons), "miss_list": misses, "runs": runs, "seg": seg,
        "n300": sum(1 for j in tgt if j.result == "300"),
        "ar": r2(ar_at(bm.ar, rate, hr, ez)), "od": r2(od_at(bm.od, rate, hr, ez)),
        "cs": bm.cs, "bpm": r1(s["bpm"]), "len": round(length), "nobj": len(objs),
        "name": bm.name,
    }


# ----------------------------------------------------------------------------
# library features
# ----------------------------------------------------------------------------


def scan_extra(text):
    mode, bid, sid, breaks, section = 0, None, None, [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section == "HitObjects":
                break
            continue
        if section == "General" and line.startswith("Mode"):
            try: mode = int(line.split(":", 1)[1].strip())
            except ValueError: pass
        elif section == "Metadata" and ":" in line:
            k, v = (q.strip() for q in line.split(":", 1))
            try:
                if k == "BeatmapID": bid = int(v)
                elif k == "BeatmapSetID": sid = int(v)
            except ValueError:
                pass
        elif section == "Events":
            p = line.split(",")
            if len(p) >= 3 and p[0].strip() in ("2", "Break"):
                try: breaks.append((float(p[1]), float(p[2])))
                except ValueError: pass
    return mode, (bid if bid and bid > 0 else None), (sid if sid and sid > 0 else None), breaks


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


def lib_features(text):
    mode, bid, sid, breaks = scan_extra(text)
    if mode != 0:
        return {"mode": mode}
    bm = oc.parse_osu(text)
    objs = bm.objects
    tgt_n = sum(1 for o in objs if o.kind != "spinner")
    if tgt_n < 30:
        return {"mode": 0, "skip": 1}
    radius = 54.4 - 4.48 * bm.cs
    js = oc.classify(bm, objs, radius, 1.0)
    t0, t1 = objs[0].time, max(o.end_time or o.time for o in objs)
    br = sum(max(0.0, min(e, t1) - max(s, t0)) for s, e in breaks)
    drain = max(1.0, (t1 - t0 - br) / 1000)
    # duration-weighted main bpm
    un = [(t, bl) for t, bl, u in bm.timing if u and bl > 0]
    w = Counter()
    for i, (t, bl) in enumerate(un):
        a = max(t, t0) if i else t0
        b = min(un[i + 1][0], t1) if i + 1 < len(un) else t1
        if b > a:
            w[round(60000 / bl)] += b - a
    bpm = w.most_common(1)[0][0] if w else (round(60000 / un[0][1]) if un else 0)
    tg = [j for j in js if j.obj.kind != "spinner"]
    n = len(tg)
    st = [j for j in tg if j.run_len >= 7]
    bu = [j for j in tg if 3 <= j.run_len < 7]
    jumps = [j for j in tg if j.category == "jump"]
    runs = Counter()
    for j in st:
        if j.run_pos == 0:
            runs[j.run_len] += 1
    vel = [j.dist_in / j.dt_in for j in jumps]
    half = []
    prev = None
    for j in tg:
        if j.category == "jump" and prev is not None:
            gap = j.obj.time - prev.obj.time
            beat = bm.beat_at(j.obj.time)
            if 0.4 * beat <= gap <= 0.6 * beat:
                half.append(j.dist_in / j.dt_in)
        prev = j
    sbpm = [j.bpm_stream for j in st]
    return {
        "mode": 0, "a": bm.artist, "t": bm.title, "v": bm.version, "c": bm.creator, "bid": bid, "sid": sid,
        "bpm": bpm, "len": round(drain), "n": n, "ar": bm.ar, "od": bm.od, "cs": bm.cs, "hp": bm.hp,
        "sl": max((j.run_len for j in tg), default=0), "sf": r3(len(st) / n), "sc": sum(runs.values()),
        "sb": round(median(sbpm)) if sbpm else 0, "sbx": round(max(sbpm)) if sbpm else 0,
        "bf": r3(len(bu) / n), "jf": r3(len(jumps) / n),
        "jsp": r2(median([j.dist_in / (2 * radius) for j in jumps])) if jumps else 0,
        "jv": r2(median(half)) if half else (r2(median(vel)) if vel else 0),
        "jv75": r2(pct(vel, 0.75)) if vel else 0, "jv90": r2(pct(vel, 0.9)) if vel else 0,
        "den": r2(n / drain), "slf": r3(sum(1 for j in tg if j.obj.kind == "slider") / n),
    }


def build_library(osu_index, deadline_note):
    cache = load_json(LIB_CACHE, {})
    if cache.get("v") != LIB_VER:
        cache = {"v": LIB_VER, "maps": {}}
    maps = cache["maps"]
    todo = [m for m in osu_index if m not in maps]
    for i, md5 in enumerate(todo):
        if i % 200 == 0:
            status("running", f"Reading beatmaps {i}/{len(todo)}...")
        try:
            maps[md5] = lib_features(Path(osu_index[md5]).read_text("utf-8", "replace"))
        except Exception as e:
            maps[md5] = {"mode": -1, "err": str(e)[:80]}
    for md5 in [m for m in maps if m not in osu_index]:
        del maps[md5]
    if todo:
        atomic_write_json(LIB_CACHE, cache)
    return maps, len(todo)


# ----------------------------------------------------------------------------
# replays
# ----------------------------------------------------------------------------


def load_replays(paths, osu_index):
    cache = load_json(REPLAY_CACHE, {})
    if cache.get("v") != ENGINE_VER:
        cache = {"v": ENGINE_VER, "replays": {}}
    rc = cache["replays"]
    new_hdr = judged = 0
    changed = False
    for p in paths:
        try:
            stt = os.stat(p)
        except OSError:
            continue
        e = rc.get(p)
        if e and e.get("size") == stt.st_size and e.get("mtime") == int(stt.st_mtime):
            continue
        try:
            hdr = read_header(p)
            rc[p] = {"size": stt.st_size, "mtime": int(stt.st_mtime), "hdr": hdr, "sum": None}
        except Exception as ex:
            rc[p] = {"size": stt.st_size, "mtime": int(stt.st_mtime), "hdr": None, "err": str(ex)[:80]}
        new_hdr += 1
        changed = True
    alive = set(paths)
    for p in [p for p in rc if p not in alive]:
        del rc[p]
        changed = True
    return cache, new_hdr, changed


def judge_needed(cache, keys, osu_index):
    rc = cache["replays"]
    todo = [p for p in keys if rc[p].get("hdr") and rc[p].get("sum") is None and not rc[p].get("err")
            and rc[p]["hdr"]["md5"] in osu_index and rc[p]["hdr"]["mode"] == 0]
    for i, p in enumerate(todo):
        if i % 20 == 0:
            status("running", f"Analysing replays {i}/{len(todo)}...")
        try:
            rc[p]["sum"] = compact_summary(p, rc[p]["hdr"], osu_index[rc[p]["hdr"]["md5"]])
        except Exception as ex:
            rc[p]["err"] = str(ex)[:80]
    return len(todo)


# ----------------------------------------------------------------------------
# ability profile
# ----------------------------------------------------------------------------


def _isotonic_up(rows):
    """Weighted pool-adjacent-violators: non-decreasing miss-rate fit over BPM bands.
    rows: [(bpm, n, misses)] sorted by bpm -> [(bpm, fitted rate)]."""
    blocks = []  # [n, misses, [bpm...]]
    for k, n, m in rows:
        blocks.append([n, m, [k]])
        while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][0] > blocks[-1][1] / blocks[-1][0]:
            n2, m2, k2 = blocks.pop()
            blocks[-1][0] += n2; blocks[-1][1] += m2; blocks[-1][2] += k2
    return [(k, m / n) for n, m, ks in blocks for k in ks]


def stream_profile(plays, min_n=20, min_maps=2):
    """-> (comfort, break, bpm_bands, threshold, unmeasurable).
    Miss rate per 10-BPM band gets a monotone (isotonic) fit so one lucky or unlucky map can't set the break,
    and a band needs notes from >= min_maps different maps. When the fit is already above the threshold at
    the slowest measured band, the limit is NOT measurable from these plays (unmeasurable=True, comfort and
    break None): naming that slowest band as the 'break' would only reflect which BPMs happened to be played."""
    b = defaultdict(lambda: [0, 0])
    maps = defaultdict(set)
    for s in plays:
        for k, v in s["bpm_b"].items():
            b[int(k)][0] += v[0]; b[int(k)][1] += v[1]
            if v[0]:
                maps[int(k)].add(s.get("md5"))
    other = [0, 0]
    for s in plays:
        for c in ("o", "l", "j"):
            v = s["cats"].get(c)
            if v:
                other[0] += v[0]; other[1] += v[1]
    base = other[1] / other[0] if other[0] else 0.05
    thr = max(0.08, 1.5 * base)
    rows = [(k, n, m) for k, (n, m) in sorted(b.items()) if n >= min_n and k >= 100 and len(maps[k]) >= min_maps]
    if not rows:
        return None, None, b, thr, False
    fit = _isotonic_up(rows)
    brk = next((k for k, r in fit if r > thr), None)
    if brk is None:
        comfort = rows[-1][0] + 10
        brk = comfort + 10
    elif brk == fit[0][0]:
        return None, None, b, thr, True
    else:
        lower = [k for k, r in fit if k < brk and r <= thr]
        comfort = min(max(lower) + 10, brk - 5) if lower else None
    return comfort, brk, b, thr, False


def stream_causes(plays):
    """Share of stream/burst misses that were aim (cursor off the circle) vs timing/no tap."""
    c = Counter()
    for s in plays:
        for m in s.get("miss_list") or ():
            if len(m) >= 3 and m[1] in ("s", "b"):
                c[m[2]] += 1
    tot = sum(c.values())
    return (c.get("A", 0) / tot if tot >= 20 else None), tot


# speed test when the stream limit can't be measured: a short 1.0x check at 150-160 BPM (coach heuristic,
# roughly the slowest streams of the 4.5* band this player used to play) tells us where streams really break
STREAM_TEST_BPM = (150, 160)


def stream_notes_below(plays, bpm=160):
    return sum(v[0] for s in plays for k, v in s["bpm_b"].items() if int(k) < bpm)


def comfort_bpm(st, ex):
    """Measured stream comfort, else a conservative estimate under the lowest breaking band."""
    return st.get("stream_comfort_bpm") or ex.get("stream_comfort_est") or 140


def aim_profile(plays):
    b = defaultdict(lambda: [0, 0, 0])
    for s in plays:
        for k, v in s["vel_b"].items():
            q = b[float(k)]
            q[0] += v[0]; q[1] += v[1]; q[2] += v[2]
    rows = [(k, n, m) for k, (n, m, _a) in sorted(b.items()) if n >= 15]
    if not rows:
        return None, b
    # "spike" = miss rate clearly above this player's baseline on slow jumps
    low = rows[:2]
    base = sum(m for _, _, m in low) / max(1, sum(n for _, n, _ in low))
    thr = max(0.05, 2.0 * base)
    for i, (k, n, m) in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if m / n >= thr and (nxt is None or nxt[2] / nxt[1] >= thr):
            return k, b   # may be below 0.75: callers treat < 0.75 as "below 0.75, not measured" (aim_floor)
    return rows[-1][0] + 0.25, b


AIM_FLOOR = 0.75   # picks never target slower jumps than this; a measured limit below it is shown as "below 0.75"


def profile(plays, all_plays, lib):
    if len(plays) < 5:
        return None, {}
    cats = defaultdict(list)
    for s in plays:
        for c, v in s["cats"].items():
            cats[c].append(v)
    allerr = [s["e"] for s in plays]
    URv = pooled_ur(allerr) or 0
    me = mean([s["mean_err"] for s in plays if s["n"] > 50 and s["mean_err"] is not None] or [0])
    # streams: widen the window until a clean band below the break is measured and the slower bands
    # (< 160 BPM) have enough notes; otherwise use the widest window (closest to the real break)
    stream_plays, sp_res = plays, None
    for n_w in (len(plays), 80, 150, len(all_plays)):
        cand = all_plays[:max(len(plays), n_w)] if n_w != len(plays) else plays
        res = stream_profile(cand)
        stream_plays, sp_res = cand, res
        if res[0] is not None and stream_notes_below(cand) >= 60:
            break
    C, B, bpm_b, sthr, s_unmeas = sp_res
    s_aim_share, s_miss_n = stream_causes(stream_plays)
    drifts = [s["drift"] for s in stream_plays if s["drift"] is not None]
    same = sum(s["same"] for s in plays); alt = sum(s["alt"] for s in plays)
    V, vel_b = aim_profile(plays)
    aim_floor = V is not None and V < AIM_FLOOR
    jn = sum(v[0] for v in cats["j"]); jm = sum(v[1] for v in cats["j"])
    jal = [sum(s["jal"][0] for s in plays), sum(s["jal"][1] for s in plays)]
    sp = defaultdict(lambda: [0, 0])
    for s in plays:
        for k, v in s["sp_b"].items():
            sp[int(k)][0] += v[0]; sp[int(k)][1] += v[1]
    jr = jm / jn if jn else 0
    spike = [k for k, (n, m) in sp.items() if n >= 8 and m / n > max(jr, 0.05) * 1.15]
    reasons = Counter()
    for s in plays:
        reasons.update(s["reasons"])
    fat = [sum(s["thirds"][0] for s in plays), sum(s["thirds"][2] for s in plays)]
    # expected last-third misses if each note type were missed as often as in the first third
    tc = defaultdict(lambda: [0, 0, 0, 0])
    for s in plays:
        for c, v in (s.get("thc") or {}).items():
            for i in range(4):
                tc[c][i] += v[i]
    tot0 = sum(v[1] for v in tc.values()) / max(1, sum(v[0] for v in tc.values()))
    fat_exp = sum(v[2] * (v[1] / v[0] if v[0] >= 30 else tot0) for v in tc.values())
    fat_last = sum(v[3] for v in tc.values())
    feats = [lib.get(s["md5"]) for s in plays]
    dens = [f["den"] * s["rate"] for f, s in zip(feats, plays) if f and f.get("den")]
    jv75 = [f["jv75"] * s["rate"] for f, s in zip(feats, plays) if f and f.get("jv75")]
    stats = {
        "ur": r1(URv), "mean_error_ms": r1(me), "avg_acc": r3(mean(s["acc"] for s in plays)),
        "near_fc_rate": r3(sum(1 for s in plays if s["misses"] <= 3) / len(plays)),
        "stream_comfort_bpm": C, "stream_break_bpm": B,
        "stream_drift_ms": r1(mean(drifts)) if drifts else None,
        "single_tap_ratio": r2(same / (same + alt)) if same + alt >= 20 else None,
        "jump_miss_rate": r3(jr) if jn else None,
        "jump_landing_radii": r2(jal[0] / jal[1]) if jal[1] else None,
        "jump_spike_spacing": min(spike) if spike else None,
        "comfort_aim_velocity": r2(V),
        "typical_ar": r1(median(s["ar"] for s in plays)), "typical_od": r1(median(s["od"] for s in plays)),
        "typical_bpm": round(median(s["bpm"] for s in plays)),
    }
    tot = sum(reasons.values()) or 1

    def cat_mean(cs):  # mean hit error over some note categories ([n, miss, cnt, sum, sumsq])
        c = sum(s["cats"][k][2] for s in plays for k in cs if k in s["cats"])
        return sum(s["cats"][k][3] for s in plays for k in cs if k in s["cats"]) / c if c >= 200 else None

    pm = [s["mean_err"] for s in plays if s["n"] > 50 and s["mean_err"] is not None]
    extra = {
        "me_ns": r1(cat_mean("ojla")), "me_s": r1(cat_mean("sb")),
        "me_spread": r1(math.sqrt(sum((x - mean(pm)) ** 2 for x in pm) / len(pm))) if len(pm) >= 5 else None,
        "cats": {c: [sum(v[0] for v in vs), sum(v[1] for v in vs), pooled_ur([v[2:] for v in vs])]
                 for c, vs in cats.items()},
        "reasons": reasons, "tot_miss": tot, "fatigue": fat, "bpm_b": bpm_b, "vel_b": vel_b,
        "stream_thr": sthr, "n_stream_plays": len(stream_plays),
        "typ_den": median(dens) if dens else 4.0, "typ_jv75": median(jv75) if jv75 else 2.0,
        "fatigue_exp": round(fat_exp),
        "fatigue_flag": bool(tc) and fat_last > fat_exp * 1.3 and fat_last >= 10 and fat[1] > fat[0],
        "stream_comfort_est": (C or (B - 15 if B else None) or
                               (STREAM_TEST_BPM[0] + 2 if s_unmeas else None)),
        "stream_measured": C is not None,
        "stream_unmeasurable": bool(s_unmeas),
        "stream_aim_share": r2(s_aim_share) if s_aim_share is not None else None, "stream_miss_n": s_miss_n,
        "aim_floor": aim_floor, "aim_v_pick": (max(AIM_FLOOR, V) if V is not None else None),
        "reading_flag": reasons.get("L", 0) / tot >= 0.12 and reasons.get("L", 0) >= 8,
        "stream_n": sum(v[0] for v in bpm_b.values()), "n_plays": len(plays),
    }
    return stats, extra


# ----------------------------------------------------------------------------
# recommendations
# ----------------------------------------------------------------------------


def clean_search(f):
    s = f"{f['a']} {f['t']} {f['v']}"
    return re.sub(r"[=<>!\"]", " ", s).replace("  ", " ").strip()


def best_rate(cur, target, lo_ok, hi_ok):
    """Rate (0.5..1.5) so cur*rate lands in [lo_ok, hi_ok]; prefers 0.05 steps."""
    if cur <= 0 or lo_ok <= cur <= hi_ok:
        return 1.0
    for r in (round(target / cur * 20) / 20, round(target / cur, 2)):
        r = clamp(r, 0.5, 1.5)
        if lo_ok <= cur * r <= hi_ok:
            return r
    return clamp(round(target / cur, 2), 0.5, 1.5)


def rate_mods(rate):
    return "NM" if abs(rate - 1) < 1e-6 else ("HT" if rate < 1 else "DT")


def rate_txt(rate):
    if abs(rate - 1) < 1e-6:
        return "1.0x"
    return f"{rate:.2f}x ({'HT' if rate < 1 else 'DT'} custom speed)"


def ur_target(st):
    """One UR goal used everywhere (focus target, accuracy block, warm-up picks)."""
    return max(150, round((st["ur"] or 300) * 0.85 / 10) * 10)


def disp_bpm(f):
    """Main BPM as the player hears it: maps timed at half tempo (1/8 'streams') get doubled."""
    b, sb = f.get("bpm") or 0, f.get("sb") or 0
    if b > 0 and sb >= 100 and abs(sb / (2 * b) - 1) <= 0.06:
        return 2 * b
    return b


# accuracy-pick limits, loosened step by step only when too few local maps qualify
ACC_STEPS = [  # (jump limit x V, density x usual, OD below typical, OD above typical, min best acc)
    (0.80, 0.80, 0.0, 1.0, 0.88),
    (0.95, 0.85, 0.5, 1.0, 0.85),
    (1.10, 0.90, 1.0, 1.5, 0.82),
    (1.30, 1.00, 1.5, 2.0, 0.80),
]


def recommend(lib, st, ex, history, today_seed):
    C = comfort_bpm(st, ex)
    B = st["stream_break_bpm"] or C + 10
    c_meas = ex.get("stream_measured", True)
    s_test = bool(ex.get("stream_unmeasurable"))
    V = ex.get("aim_v_pick") or st["comfort_aim_velocity"] or 1.5
    Vt = "below 0.75" if ex.get("aim_floor") else f"{V:.2f}"
    me_ns = ex.get("me_ns")
    tod = st["typical_od"] or 6
    tar = st["typical_ar"] or 8
    den = ex["typ_den"]
    avg_acc = st["avg_acc"] or 0.7
    URt = ur_target(st)
    rng = random.Random(today_seed)
    pool = []
    for md5, f in lib.items():
        if f.get("mode") != 0 or f.get("skip") or not f.get("n"):
            continue
        if f["n"] < 100 or f["len"] < 45 or f["len"] > 420 or f["bpm"] <= 0:
            continue
        h = history.get(md5, {"plays": 0, "best": None})
        pool.append((md5, f, h))

    def hist_txt(h):
        if not h["plays"]:
            return "New to you."
        return f"Your best: {h['best']:.1%} in {h['plays']} play{'s' if h['plays'] > 1 else ''}."

    def stream_load(f, r):
        return f["sb"] * r if f["sb"] >= 100 and (f["sf"] >= 0.02 or f["sc"] >= 2) else 0

    def burst_ok(f, r, cap):
        # bursts at a much higher bpm than the cap are still hard; approximate with the main BPM
        return f["bf"] < 0.08 or disp_bpm(f) * r <= cap

    def acc_level(f, h, sl, jv, d):
        """Lowest ACC_STEPS index this map passes as an accuracy pick, or None."""
        if not (60 <= f["len"] <= 240) or not (sl == 0 or sl <= C - 10) or not burst_ok(f, 1, C + 10):
            return None
        for k, (jf, dfac, od_lo, od_hi, min_best) in enumerate(ACC_STEPS):
            if jv <= jf * V and d <= dfac * den and tod - od_lo <= f["od"] <= tod + od_hi \
                    and (h["best"] is None or h["best"] >= min_best):
                return k
        return None

    cands = defaultdict(list)
    for md5, f, h in pool:
        mastered = h["best"] is not None and h["best"] >= 0.95
        jitter = rng.random() * 0.6
        nov = 0.3 if 0 < h["plays"] <= 5 else 0.15 if h["plays"] == 0 else -0.2
        sl, jv, d = stream_load(f, 1.0), f["jv75"], f["den"]
        # --- warmup: clearly easy
        if (sl == 0 or sl <= C - 25) and jv <= 0.7 * V and d <= den * 0.85 and 60 <= f["len"] <= 160 \
                and f["od"] >= tod - 2 and burst_ok(f, 1, C + 20):
            sc = 2 - abs(f["od"] - (tod - 1)) * 0.3 + (0.5 if h["best"] and h["best"] >= 0.85 else 0) + jitter
            cands["warmup"].append((sc, md5, 1.0, {
                "reason": "Easy start: " + (f"jumps peak ~{jv:.2f} px/ms (your comfort {Vt}), " if jv > 0.05
                                            else "almost no jumps, ")
                          + f"{d:.1f} notes/s vs your usual {den:.1f}"
                          + (", no streams." if sl == 0 else f", streams only {sl:.0f} BPM.") + " " + hist_txt(h),
                "goal": f"95%+ accuracy, relaxed hands; UR under {URt}."}))
        # --- accuracy: clearly below the player's level (easy jumps, lower density), decent OD
        lvl = None if mastered else acc_level(f, h, sl, jv, d)
        if lvl is not None:
            sc = 3 - abs(f["od"] - (tod + 0.5)) * 0.5 - abs(d - 0.75 * den) * 0.3 + nov + jitter - lvl * 0.4
            if h["best"] and 0.88 <= h["best"] < 0.95:
                sc += 0.5
            goal = 0.92 if h["best"] is None else min(0.95, h["best"] + 0.03)
            cands["accuracy"].append((sc, md5, 1.0, {
                "reason": f"OD {f['od']:g} (you usually play OD ~{tod:g}) with jumps under {jv:.2f} px/ms, "
                          f"{d:.1f} notes/s (your usual {den:.1f}) and "
                          + ("no streams" if sl == 0 else f"streams only {sl:.0f} BPM")
                          + f": pure timing practice. {hist_txt(h)}",
                "goal": f"{goal:.0%} accuracy at 1.0x. Tap to the music, not the approach circle"
                        + (f"; on regular notes you hit {me_ns:+.0f} ms {'late' if me_ns > 0 else 'early'} on "
                           "average, so listen for the beat."
                           if me_ns is not None and abs(me_ns) > K.OFFSET_OK_MS else "."),
                "lvl": lvl}))
        # --- speed: long streams at a rate putting them at comfort..comfort+5
        if f["sb"] >= 100 and (f["sf"] >= 0.08 or (f["sl"] >= 16 and f["sc"] >= 3)) and f["len"] <= 300 \
                and f["sl"] <= 96 and f["sf"] <= 0.6:
            if s_test:  # limit not measurable: a test at 150-160 BPM (>= 0.8x), not a map slowed far below it
                lo_t, hi_t = STREAM_TEST_BPM
                r = best_rate(f["sb"], (lo_t + hi_t) / 2, lo_t, hi_t)
                ok_rng = lo_t <= f["sb"] * r <= hi_t and r >= 0.8   # not slowed so far that AR/OD fall apart
            else:
                r = best_rate(f["sb"], C + 2.5, C, C + 5)
                ok_rng = C - 3 <= f["sb"] * r <= C + 8
            eff = f["sb"] * r
            if ok_rng and f["jv75"] * r <= 1.0 * V and od_at(f["od"], r) <= tod + 2 \
                    and f["den"] * r <= 1.6 * den:
                sc = 2 + min(f["sf"], 0.4) * 5 + min(f["sl"], 48) / 48 - (0 if 0.75 <= r <= 1.2 else 1.5) \
                    - (1.0 if f["sl"] > 64 else 0) \
                    - abs(r - 1) * (4.0 if s_test else 0.8) + nov + jitter
                arr = ar_at(f["ar"], r)
                if s_test:
                    reason = (f"Stream test: 1/4 streams at {f['sb']} BPM (longest {f['sl']} notes, {f['sf']:.0%} of "
                              "the map)" + (f", at {r:.2f}x {eff:.0f} BPM" if abs(r - 1) > 1e-6 else "")
                              + ". Your stream misses are high at every BPM you've played so far, so the coach can't "
                              "measure your stream limit yet; this checks whether 150-160 BPM is clean."
                              + (f" AR becomes {arr:.1f}." if abs(r - 1) > 0.04 else ""))
                    goal = ("One or two full runs. Clean with little late drift = your limit is higher, so the "
                            "coach will start measuring it from these plays; many stream misses = try "
                            f"{max(0.75, r - 0.1):.2f}x next time.")
                else:
                    reason = (f"1/4 streams at {f['sb']} BPM (longest {f['sl']} notes, {f['sf']:.0%} of the map); "
                              + (f"at {r:.2f}x that's {eff:.0f} BPM" if abs(r - 1) > 1e-6 else f"that's {eff:.0f} BPM")
                              + (f", right at your comfort ({C}) and under your break point ({B})." if c_meas
                                 else f", safely under {B} BPM where your streams start breaking.")
                              + (f" AR becomes {arr:.1f}." if abs(r - 1) > 0.04 else ""))
                    goal = ("No stream misses and no late drift at the end of streams; then go "
                            + f"{min(1.5, r + 0.05):.2f}x (~{f['sb'] * min(1.5, r + 0.05):.0f} BPM).")
                cands["speed"].append((sc, md5, r, {"reason": reason, "goal": goal}))
        # --- aim: jump heavy, hardest jumps just below the spike
        if f["jf"] >= 0.25 and f["jv75"] > 0 and f["n"] * f["jf"] >= 40 and f["len"] <= 300:
            r = best_rate(f["jv75"], 0.93 * V, 0.85 * V, 1.0 * V)
            eff = f["jv75"] * r
            if 0.82 * V <= eff <= 1.03 * V and stream_load(f, r) <= C and not mastered \
                    and ar_at(f["ar"], r) <= tar + 1 and od_at(f["od"], r) <= tod + 2:
                sc = 2 + min(f["jf"], 0.6) * 3 - (0 if 0.75 <= r <= 1.2 else 1.5) - abs(r - 1) * 0.8 + nov + jitter
                cands["aim"].append((sc, md5, r, {
                    "reason": f"Jump map ({f['jf']:.0%} jumps, median spacing {f['jsp']:.1f} circles); hardest jumps "
                              f"~{f['jv75']:.2f} px/ms" + (f", at {r:.2f}x {eff:.2f}" if abs(r - 1) > 1e-6 else "")
                              + (" - near the slowest jump speed the coach uses; your misses already climb below "
                                 "0.75 px/ms, so this is about landing precision." if ex.get("aim_floor") else
                                 f" - just under where your misses climb (~{V:.2f} px/ms)."),
                    "goal": f"Under {max(2, round(f['n'] * f['jf'] * 0.02))} jump misses; snap onto each circle centre. "
                            f"Then {min(1.5, r + 0.05):.2f}x."}))
        # --- stamina
        if ex["fatigue_flag"] and f["len"] >= 150 and d >= 0.9 * den and stream_load(f, 1) <= C and jv <= V \
                and not mastered:
            sc = 2 + min(f["len"], 300) / 150 + min(f["sf"] + f["bf"], 0.3) * 3 + nov + jitter
            cands["stamina"].append((sc, md5, 1.0, {
                "reason": f"{mss(f['len'] * 1000)} of drain at {d:.1f} notes/s"
                          + (f" with {f['sb']}-BPM streams" if stream_load(f, 1) else "")
                          + f", but no jumps or streams above your comfort. Your last thirds had "
                          f"{ex['fatigue'][1]} misses where ~{ex['fatigue_exp']} were expected from the note types "
                          f"(last {ex['n_plays']} plays).",
                "goal": "Same accuracy in the last minute as the first minute; relax your grip in breaks."}))
        # --- reading
        if ex["reading_flag"] and tar + 0.5 <= f["ar"] <= tar + 1.0 and stream_load(f, 1) <= C - 5 \
                and jv <= 0.9 * V and d <= den and not mastered:
            sc = 2 + nov + jitter
            cands["reading"].append((sc, md5, 1.0, {
                "reason": f"AR {f['ar']:g}, a step above your usual AR ~{tar:g}, while jumps ({jv:.1f} px/ms) and "
                          f"density ({d:.1f}/s) stay easy for you. {ex['reasons'].get('L', 0)} recent misses "
                          "had your cursor far from the note.",
                "goal": "Pass with fewer 'lost' misses; read the next 2-3 notes ahead, not just the current one."}))
        # --- push: a bit above comfort
        if f["len"] <= 240 and not mastered and h["plays"] <= 4 \
                and not (h["best"] is not None and h["best"] >= avg_acc + 0.05):  # already doing well on it
            up_s = C + 5 <= sl <= C + 20
            up_a = 1.03 * V < jv <= 1.15 * V and f["jf"] >= 0.15
            if (up_s or up_a) and sl <= C + 25 and jv <= 1.25 * V and den * 0.9 <= d <= den * 1.3 \
                    and f["od"] <= tod + 1.5 and f["ar"] <= tar + 1:
                sc = 2 + (0.5 if up_s and up_a else 0) - abs(d - den * 1.05) * 0.3 + nov + jitter
                bits = []
                if up_s: bits.append(f"{sl:.0f}-BPM streams" + ("" if s_test else f" (your break ~{B})"))
                if up_a: bits.append(f"jumps to {jv:.2f} px/ms (your comfort {Vt})")
                cands["push"].append((sc, md5, 1.0, {
                    "reason": "A step up: " + " and ".join(bits) + f", {d:.1f} notes/s. {hist_txt(h)}",
                    "goal": f"Pass it; aim for over "
                            f"{max(0.6, avg_acc if h['best'] is None else max(avg_acc, h['best'] + 0.02)):.0%} "
                            "accuracy. Misses are fine here."}))

    # accuracy: use the strictest limits that still leave a choice of maps
    acc = cands["accuracy"]
    for k in range(len(ACC_STEPS)):
        if sum(1 for c in acc if c[3]["lvl"] <= k) >= 8 or k == len(ACC_STEPS) - 1:
            cands["accuracy"] = [c for c in acc if c[3]["lvl"] <= k]
            break

    want = [("warmup", 2), ("accuracy", 4), ("speed", 4), ("aim", 3)]
    if ex["fatigue_flag"]: want.append(("stamina", 2))
    if ex["reading_flag"]: want.append(("reading", 2))
    want.append(("push", 3))
    used_md5, used_sets = set(), Counter()
    recs = []
    for purpose, k in want:
        lst = sorted(cands[purpose], key=lambda c: -c[0])
        got, sets, songs = 0, set(), set()
        for sc, md5, r, txt in lst:
            if got >= k:
                break
            f = lib[md5]
            skey = f.get("sid") or (f["a"].lower(), f["t"].lower())
            song = (f["a"].lower(), f["t"].lower())
            if md5 in used_md5 or skey in sets or song in songs:
                continue
            if used_sets[skey] and sc - 1.0 < (lst[min(len(lst) - 1, k * 3)][0] if lst else 0):
                continue  # prefer songs not used for another purpose
            got += 1
            sets.add(skey); songs.add(song); used_md5.add(md5); used_sets[skey] += 1
            recs.append({
                "id": md5, "purpose": purpose, "artist": f["a"], "title": f["t"], "version": f["v"],
                "creator": f["c"], "beatmap_id": f.get("bid"), "set_id": f.get("sid"),
                "mods": rate_mods(r), "rate": r, "bpm": round(disp_bpm(f) * r), "ar": r1(ar_at(f["ar"], r)),
                "od": r1(od_at(f["od"], r)), "cs": f["cs"], "length_s": round(f["len"] / r),
                "reason": txt["reason"], "goal": txt["goal"], "search": clean_search(f),
            })
    return recs[:25], {p: len(v) for p, v in cands.items()}


# ----------------------------------------------------------------------------
# trouble spots
# ----------------------------------------------------------------------------


SHORT_R = {"A": "aim", "E": "early", "T": "timing", "L": "lost", "?": "unclear"}


def trouble_spots(plays, st, ex):
    C = comfort_bpm(st, ex)
    V = ex.get("aim_v_pick") or st["comfort_aim_velocity"] or 1.5
    secs = []
    for s in plays:
        ml = sorted(s["miss_list"])
        cur = []
        for m in ml + [None]:
            if m is not None and (not cur or m[0] - cur[-1][0] <= 4000):
                cur.append(m)
                continue
            if len(cur) >= 3:
                secs.append({"md5": s["md5"], "name": s["name"], "a": cur[0][0], "b": cur[-1][0],
                             "m": list(cur), "plays": 1, "rate": s["rate"]})
            cur = [m] if m is not None else []
    # merge overlapping sections of the same map (repeated plays)
    secs.sort(key=lambda x: (x["md5"], x["a"]))
    merged = []
    for x in secs:
        y = merged[-1] if merged else None
        if y and y["md5"] == x["md5"] and x["a"] <= y["b"] + 2000:
            y["b"] = max(y["b"], x["b"]); y["m"] += x["m"]; y["plays"] += 1
        else:
            merged.append(x)
    merged.sort(key=lambda x: -len(x["m"]))
    out = []
    for x in merged[:15]:
        cats = Counter(m[1] for m in x["m"])
        rs = Counter(m[2] for m in x["m"])
        c, cn = cats.most_common(1)[0]
        rc, rn = rs.most_common(1)[0]
        kind = KIND.get(c, "rhythm")
        if rc == "L" and rn >= len(x["m"]) / 2:
            kind = "reading"
        elif rc in ("T", "E") and kind in ("sliders",) and rn >= len(x["m"]) / 2:
            kind = "rhythm"
        nm = len(x["m"])
        start, end = mss(x["a"]), mss(x["b"] + 1000)
        rep = f" (missed here in {x['plays']} plays)" if x["plays"] > 1 else ""
        extras = [m[3] / x["rate"] for m in x["m"] if m[1] == c and m[3]]  # back to 1.0x values
        pr = 0.85 if nm < 6 else 0.8
        aim = rc == "A" and rn >= nm / 2
        timing = rc in ("T", "E") and rn >= nm / 2
        early = rc == "E" and rn >= nm / 2
        where = f"{start}-{end}"
        if kind in ("streams", "bursts") and extras:
            bpm1 = median(extras)
            pr = clamp(round(C / bpm1 * 20) / 20, 0.75, 0.9)
            head = (f"{'Stream' if kind == 'streams' else 'Bursts'} at ~{bpm1:.0f} BPM. Play the map at {pr:.2f}x "
                    f"(HT custom speed, ~{bpm1 * pr:.0f} BPM) and focus on {where}. ")
            if aim:
                adv = head + "Your taps kept time but the cursor slid off: follow the path note by note, don't cut corners."
            else:
                adv = head + "Alternate keys and keep tapping to the music; +0.05x after a clean run."
        elif kind == "jumps" and extras and median(extras) < V:
            # jumps inside the player's range: speed isn't the problem, so don't slow the map down much
            v1 = median(extras)
            pr = 1.0 if nm < 6 else 0.9
            base = (f"These jumps (~{v1:.2f} px/ms) are within your range (comfort ~{V:.2f}), so speed isn't the "
                    f"problem at {where}. ")
            if aim:
                adv = base + ("The cursor stopped short or overshot: aim for the circle centre and stop on it "
                              "before tapping. Watch your replay of this part.")
            elif rc == "L" and rn >= nm / 2:
                adv = base + "You lost track of the pattern: watch the replay, then read 2-3 notes ahead."
            elif timing:
                adv = base + "The cursor got there but the taps were off-time: tap with the music, not the circle."
            else:
                adv = base + "Misses here are focus or reading - watch your replay to see what went wrong."
        elif kind == "jumps" and extras:
            v1 = median(extras)
            pr = clamp(round(0.95 * V / v1 * 20) / 20, 0.75, 0.9)
            if timing:
                adv = (f"Cursor reached the jumps but taps were off-time at {where}. Play at {pr:.2f}x (HT custom "
                       "speed) and tap exactly on each beat; then 1.0x.")
            else:
                adv = (f"Jumps here move ~{v1:.2f} px/ms (your comfort ~{V:.2f}). Play at {pr:.2f}x (HT custom "
                       f"speed) and focus on {where}; watch your replay of this part to see where the cursor lands. "
                       "Snap to the circle centre, then +0.05x per clean run.")
        elif rn < nm / 2:
            adv = (f"Mixed misses at {where} (no single cause). Watch your replay of this part first, then play it "
                   f"at {pr:.2f}x (HT custom speed) until clean and raise the speed 0.05x at a time.")
        elif kind == "reading":
            adv = (f"You lost the pattern here. Watch your replay from {start}, then play at {pr:.2f}x "
                   f"(HT custom speed) and focus on {where}, reading 2-3 notes ahead.")
        elif aim:
            what = "slider starts" if kind == "sliders" else "closely spaced notes"
            adv = (f"Aim misses on {what} at {where}: you tapped on time but the cursor was off the circle. "
                   f"Play at {pr:.2f}x (HT custom speed) and keep the cursor on each note until you tap it.")
        elif early:
            adv = (f"Early taps at {where} (you hit before the note). Play at {pr:.2f}x (HT custom speed) and wait "
                   "for the beat; count the rhythm out loud once.")
        elif kind == "sliders":
            adv = (f"Misses on slider starts at {where}. Play at {pr:.2f}x (HT custom speed) and tap each slider "
                   "head on its beat, then follow; raise speed once clean.")
        else:
            adv = (f"Taps were off-time at {where} with the cursor on the notes. Play at {pr:.2f}x "
                   "(HT custom speed), listen to the rhythm of this part and tap with the music; then 1.0x.")
        out.append({"map": x["name"], "id": x["md5"], "start_ms": int(x["a"]), "end_ms": int(x["b"] + 1000),
                    "start": start, "end": end, "misses": nm, "kind": kind,
                    "reason": (f"{REASON_TXT.get(rc, rc)} ({rn} of {nm}){rep}" if rn >= nm / 2 else
                               "mixed: " + ", ".join(f"{n} {SHORT_R.get(r, r)}" for r, n in rs.most_common())
                               + rep),
                    "advice": adv, "practice_rate": pr})
    return out


# ----------------------------------------------------------------------------
# per play text
# ----------------------------------------------------------------------------


def main_issue(s):
    if s["rj_misses"] == 0:
        return "accuracy" if s["acc"] < 0.9 else "none"
    cats = Counter()
    for m in s["miss_list"]:
        cats[m[1]] += 1
    rs = Counter(m[2] for m in s["miss_list"])
    if rs.get("L", 0) > len(s["miss_list"]) / 2:
        return "reading"
    c = cats.most_common(1)[0][0]
    return KIND.get(c, "rhythm")


def verdict(s):
    mp100 = s["misses"] / max(1, s["n"]) * 100
    if s["acc"] < 0.75 or mp100 > 5:
        return "too hard"
    if s["acc"] > 0.96 and s["misses"] <= 1:
        return "too easy"
    return "good practice"


def play_comment(s, avg_acc):
    ml = s["miss_list"]
    nm = len(ml)
    if s["misses"] == 0:
        return f"Full combo! {s['acc']:.1%} accuracy, UR {s['ur']:.0f}."[:140]
    if nm == 0:
        return f"{s['misses']} misses (probably slider ends); UR {s['ur']:.0f}."[:140]
    cats = Counter(m[1] for m in ml)
    c, cn = cats.most_common(1)[0]
    rs = Counter(m[2] for m in ml if m[1] == c)
    rc, rcn = rs.most_common(1)[0]
    span = max(1, max(m[0] for m in ml) - min(m[0] for m in ml) + 1)
    t0 = min(m[0] for m in ml)
    # where in the map: use object count thirds
    th = s["thirds"][:3]
    part = ["first", "middle", "last"][th.index(max(th))] if max(th) >= nm * 0.5 else None
    what = {"A": "aim", "E": "early taps", "T": "timing", "L": "lost reading", "?": "misses"}[rc]
    cname = {"s": "streams", "b": "bursts", "j": "jumps", "l": "slider starts", "o": "regular notes",
             "a": "notes after breaks"}[c]
    if rcn == cn:
        txt = f"{cn} of {nm} misses were {what} on {cname}"
    elif rcn >= cn * 0.6:
        txt = f"{cn} of {nm} misses were on {cname}, mostly {what} ({rcn})"
    else:
        txt = f"{cn} of {nm} misses were on {cname}"
    lead = "; most were" if "mostly" in txt else ", mostly"
    txt += f"{lead} in the {part} third." if part else "."
    if s["misses"] == 0:
        txt = "No misses! " + txt
    elif s["misses"] <= 3:
        txt = f"Near-FC ({s['misses']} miss{'es' if s['misses'] != 1 else ''})! " + txt
    elif avg_acc and s["acc"] > avg_acc + 0.03:
        txt += f" Acc {s['acc']:.1%} beats your average."
    return txt[:140]


# ----------------------------------------------------------------------------
# focus, plan, trends, mood
# ----------------------------------------------------------------------------


def make_focus(st, ex, phase=None, roadmap=None):
    # Scores are on one scale: miss causes score their share of all misses (0-100); UR / offset /
    # level / stamina get fixed-range scores comparable to a 25-60% miss share.
    F = []
    U = st["ur"] or 0
    tot = ex["tot_miss"] or 1
    cm = {c: v[1] for c, v in ex["cats"].items()}
    cat_tot = sum(cm.values()) or 1
    if U > 200:
        F.append((clamp((U - 150) / 6, 0, 60), {"id": "ur", "title": "Accuracy / rhythm consistency",
                          "why": f"Your UR is {U:.0f}; the coach's roadmap targets are {K.UR_TARGETS_TXT} "
                                 "(coach heuristic).",
                          "drill": "15 min on maps well below your level (the 'accuracy' picks), aiming for "
                                   "92-95% (each pick has its own goal).",
                          "metric": "UR", "current": round(U), "target": ur_target(st)}))
    A = ex["reasons"].get("A", 0)
    jr = st["jump_miss_rate"]
    V = st["comfort_aim_velocity"]
    v_txt = ("climbing already below 0.75 px/ms (the slowest the coach measures well)" if ex.get("aim_floor")
             else f"climbing above ~{V:.2f} px/ms" if V else "")
    if A / tot >= 0.2 and A >= 10:
        F.append((100 * A / tot, {"id": "aim", "title": "Aim precision",
                  "why": f"{A} of {tot} misses ({A / tot:.0%}) were taps in time with the cursor off the circle"
                         + (f"; {jr:.1%} of jumps missed, {v_txt}" if jr and V else "")
                         + (f" and at {st['jump_spike_spacing']}+ circle spacing." if st["jump_spike_spacing"]
                            else "."),
                  "drill": "Aim picks at a speed just under your limit; snap onto each circle centre and stop "
                           "before tapping. Slow the trouble spots down rather than the whole map.",
                  "metric": "aim misses %", "current": round(100 * A / tot),
                  "target": max(20, round(100 * A / tot * 0.7))}))
    elif jr is not None and jr > 0.03 and cm.get("j"):
        F.append((100 * cm["j"] / cat_tot, {"id": "jumps", "title": "Jump aim",
                  "why": f"{jr:.1%} of jumps missed" + (f"; misses {v_txt}" if V else "")
                         + (f" and at {st['jump_spike_spacing']}+ circle spacing." if st["jump_spike_spacing"] else "."),
                  "drill": "Aim picks at a speed just under your limit; snap and stop on each circle.",
                  "metric": "jump miss %", "current": r1(jr * 100), "target": r1(max(2.0, jr * 100 * 0.6))}))
    me = st["mean_error_ms"]
    me_ns, me_s = ex.get("me_ns"), ex.get("me_s")
    # offset only explains a bias that is also there on regular notes; stream lateness is speed (drift)
    me_off = me_ns if me_ns is not None else me
    if me_off is not None and abs(me_off) > K.OFFSET_OK_MS:
        F.append((abs(me_off) * 2.5, {"id": "offset", "title": "Timing bias (" + ("late" if me_off > 0 else "early") + ")",
                                      "why": f"You hit {abs(me_off):.0f} ms {'late' if me_off > 0 else 'early'} on average"
                                             + (" on regular notes (streams excluded)." if me_ns is not None else "."),
                                      "drill": "Calibrate once on 3-4 steady maps without streams: adjust lazer's global "
                                               "audio offset (Settings > Audio) in 5 ms steps, or use a calibrate-from-"
                                               "last-play button if your version has one. Don't re-adjust daily.",
                                      "metric": "mean hit error (ms)", "current": r1(me_off), "target": 0}))
    B = st["stream_break_bpm"]
    C = comfort_bpm(st, ex)
    sc = ex["cats"].get("s")
    s_aim = ex.get("stream_aim_share")
    if ex.get("stream_unmeasurable") and sc and sc[0] >= 50:
        srate = sc[1] / sc[0]
        smiss = cm.get("s", 0) + cm.get("b", 0)
        d = st["stream_drift_ms"] or 0
        lo_t, hi_t = STREAM_TEST_BPM
        F.append((100 * smiss / cat_tot + clamp(d / 3, 0, 10), {"id": "streams", "title": "Streams / speed",
                  "why": f"{srate:.0%} of stream notes missed, and the miss rate is high at every stream BPM you've "
                         f"played, so your real stream limit can't be measured yet (streams and bursts are {smiss} "
                         f"of {cat_tot} misses)"
                         + (f"; {s_aim:.0%} of stream misses were the cursor off the circle, so this is partly aim/"
                            "cursor control, not only finger speed" if s_aim is not None and s_aim >= 0.35 else "")
                         + (f"; you fall {d:.0f} ms behind by the end of long streams" if d > 8 else "") + ".",
                  "drill": f"Speed picks: a stream test at {lo_t}-{hi_t} BPM first (1.0x where a map fits). Keep the cursor on each "
                           "stream note (follow the path, don't cut corners). 30-60 s on, equal rest.",
                  "metric": "stream miss %", "current": round(srate * 100), "target": max(5, round(srate * 60))}))
    elif B and sc and sc[0] >= 50:
        srate = sc[1] / sc[0]
        smiss = cm.get("s", 0) + cm.get("b", 0)
        d = st["stream_drift_ms"] or 0
        F.append((100 * smiss / cat_tot + clamp(d / 3, 0, 10), {"id": "streams", "title": "Streams / speed",
                  "why": f"Streams break around {B} BPM ({srate:.0%} of stream notes missed; streams and bursts "
                         f"are {smiss} of {cat_tot} misses)"
                         + (f"; you fall {d:.0f} ms behind by the end of long streams" if d > 8 else "")
                         + (f"; stream notes average {me_s:+.0f} ms vs {me_ns:+.0f} ms on other notes, so your "
                            "late bias is speed, not offset." if me_s is not None and me_ns is not None
                            and me_s - me_ns > 10 else "")
                         + (f" {s_aim:.0%} of stream misses were the cursor off the circle (aim, not speed)."
                            if s_aim is not None and s_aim >= 0.35 else ""),
                  "drill": f"Speed picks: streams rated to {C}-{C + 5} BPM. 30-60 s on, equal rest. "
                           "+0.05x when clean with no drift.",
                  "metric": "stream break BPM", "current": B, "target": B + 10}))
    nfc = st["near_fc_rate"]
    if nfc is not None and nfc < 0.3:
        F.append((25 - nfc * 40, {"id": "level", "title": "Practise at the right difficulty",
                  "why": ("None" if nfc == 0 else f"Only {nfc:.0%}") + " of your recent plays were near-FCs (3 misses or fewer); "
                         f"average acc {st['avg_acc']:.0%}.",
                  "drill": "Spend most of each session on maps where you get 90%+; keep hard maps for the Push step.",
                  "metric": "near-FC rate %", "current": round(nfc * 100), "target": 30}))
    if ex["fatigue_flag"]:
        f0, f2 = ex["fatigue"]
        fe = max(1, ex["fatigue_exp"])
        F.append((18, {"id": "stamina", "title": "Stamina",
                       "why": f"Last third of maps: {f2} misses vs {f0} in the first third, and ~{fe} would be "
                              "expected from the note types there, so it's more than harder patterns.",
                       "drill": "Stamina picks: 2.5-5 min maps below your speed limit; full-length runs.",
                       "metric": "last-third misses vs expected", "current": r2(f2 / fe), "target": 1.1}))
    if ex["reading_flag"]:
        L = ex["reasons"].get("L", 0)
        F.append((100 * L / tot + 5, {"id": "reading", "title": "Reading",
                       "why": f"{L} of {ex['tot_miss']} misses had your cursor far from the note.",
                       "drill": "Reading picks (AR +0.5 to +1), and watch replays of the sections you got lost in.",
                       "metric": "lost misses %", "current": round(L / ex['tot_miss'] * 100), "target": 5}))
    s1 = st["single_tap_ratio"]
    if s1 is not None and s1 > 0.35:
        F.append((15, {"id": "alternate", "title": "Alternate in streams",
                       "why": f"{s1:.0%} of stream/burst taps repeat the same key.",
                       "drill": "Slow streams with strict alternation (speed picks at 0.8x).",
                       "metric": "same-key %", "current": round(s1 * 100), "target": 15}))
    # phase: re-sharpen favours timing/accuracy recovery, plateau-break favours the speed base
    pb = {"re-sharpen": {"ur": 15, "level": 10, "offset": 5, "streams": 5},
          "rebuild": {"aim": 5, "jumps": 5, "streams": 5},
          "plateau-break": {"streams": 12, "stamina": 8, "reading": 5}}.get(phase, {})
    F = [(sc + pb.get(f["id"], 0), f) for sc, f in F]
    # tie each focus to the next roadmap checkpoint it moves
    band = next((b for b in (roadmap or {}).get("bands", []) if b.get("status") == "current"), None)
    tg = K.band_targets(band["stars"]) if band else None
    if tg:
        s = f"{band['stars']:g}*"
        add = {"ur": f" Roadmap {s} checkpoint: UR <= {tg['ur']}.",
               "streams": f" Roadmap {s} checkpoint: clean streams at {tg['stream']} BPM.",
               "aim": f" Roadmap {s} checkpoint: under ~5% jump misses up to {tg['aim']:.2f} px/ms.",
               "jumps": f" Roadmap {s} checkpoint: under ~5% jump misses up to {tg['aim']:.2f} px/ms.",
               "level": f" Roadmap {s} checkpoint: {tg['nfc']:.0%} near-FCs."}
        for _, f in F:
            if f["id"] in add:
                f["why"] += add[f["id"]]
    F.sort(key=lambda x: -x[0])
    out = []
    for i, (_, f) in enumerate(F[:5], 1):
        f["priority"] = i
        out.append(f)
    return out


def make_plan(recs, st, ex, seed, focus=(), phase=None):
    by = defaultdict(list)
    for r in recs:
        if r["purpose"] == "pp" and not r.get("owned", True):
            continue  # not downloaded yet: can't be played in this session
        by[r["purpose"]].append(r["id"])
    C = comfort_bpm(st, ex)
    s_test = bool(ex.get("stream_unmeasurable"))
    plan = []
    if by["warmup"]:
        plan.append({"block": "Warm-up", "minutes": 10, "goal": "95% acc, relaxed tapping", "pick_ids": by["warmup"]})
    after_warmup = len(plan)  # stamina/interval work goes here (fresh hands), not at the end of the session
    if by["accuracy"]:
        plan.append({"block": "Accuracy", "minutes": 18 if phase == "re-sharpen" else 15,
                     "goal": f"Each pick's accuracy goal (92-95%); UR under {ur_target(st)}",
                     "pick_ids": by["accuracy"][:3]})
    if by["speed"]:
        if phase == "plateau-break":
            plan.append({"block": "Speed (overload)", "minutes": 12,
                         "goal": f"Clean at the pick's rate (~{C}-{C + 5} BPM), then +0.05x per clean run up to "
                                 "+0.15x, then one run back at the start rate", "pick_ids": by["speed"][:3]})
        elif s_test:
            plan.append({"block": "Speed (stream test)", "minutes": 12,
                         "goal": f"Stream test at {STREAM_TEST_BPM[0]}-{STREAM_TEST_BPM[1]} BPM (each pick's rate): keep "
                                 "the cursor on each stream note, alternate, note whether it stays clean; 30-60 s on, "
                                 "equal rest",
                         "pick_ids": by["speed"][:3]})
        else:
            plan.append({"block": "Speed", "minutes": 12,
                         "goal": f"Clean streams at {C}-{C + 5} BPM, no late drift; 30-60 s on, equal rest",
                         "pick_ids": by["speed"][:3]})
    goals = {"aim": "Under 2% jump misses; snap and stop on each circle",
             "stamina": "Same accuracy at the end as at the start",
             "reading": "No 'lost' misses; read 2-3 notes ahead"}
    top2 = [f.get("id") for f in list(focus)[:2]]
    rot = [p for p in ("aim", "stamina", "reading") if by[p]]
    if by["aim"] and ("aim" in top2 or "jumps" in top2):
        # aim is a top problem: it gets its own block every day; the rest rotate beside it
        plan.append({"block": "Aim", "minutes": 15, "goal": goals["aim"], "pick_ids": by["aim"][:3]})
        rot = [p for p in rot if p != "aim"]
        if rot:
            p = rot[seed % len(rot)]
            blk = {"block": f"Skill rotation: {p}", "minutes": 10, "goal": goals[p], "pick_ids": by[p][:2]}
            plan.insert(after_warmup, blk) if p == "stamina" else plan.append(blk)
    elif rot:
        p = rot[seed % len(rot)]
        blk = {"block": f"Skill rotation: {p}", "minutes": 15, "goal": goals[p], "pick_ids": by[p][:3]}
        plan.insert(after_warmup, blk) if p == "stamina" else plan.append(blk)
    if by["push"]:
        if phase == "re-sharpen":
            plan.append({"block": "Push: a level you've passed before", "minutes": 10,
                         "goal": "Pass a map at a level you've passed before; misses are fine, max 3 tries each",
                         "pick_ids": by["push"][:2]})
        else:
            plan.append({"block": "Push", "minutes": 12, "goal": "Pass a harder map; misses are fine, max 3 tries each",
                         "pick_ids": by["push"][:2]})
    if by["pp"]:
        plan.append({"block": "Score attempt (pp)", "minutes": {"re-sharpen": 6, "rebuild": 8}.get(phase, 10),
                     "goal": "Full attempts only, max 5 per session, then stop - win or lose",
                     "pick_ids": by["pp"][:2]})
    # keep the session at 60-75 min even when some purposes had no local maps
    work = [b for b in plan if b["pick_ids"]]
    total = sum(b["minutes"] for b in plan) + 3
    i = 0
    while work and total < 60:
        work[i % len(work)]["minutes"] += 1
        total += 1
        i += 1
    while work and total > 70:   # 70 + the 5-minute break = 75 at most
        b = max(work, key=lambda b: b["minutes"])
        b["minutes"] -= 1
        total -= 1
    # health-breaks rule: 5 minutes off every 30-40 minutes -> a break block once ~35 min are planned
    t, at = 0, None
    for i, b in enumerate(plan):
        t += b["minutes"]
        if t >= 30 and i < len(plan) - 1:
            at = i + 1
            break
    if at is not None:
        plan.insert(at, {"block": "Break (5 min)", "minutes": 5,
                         "goal": "Stand up, let your hands hang loose, look away from the screen. Stop for today if "
                                 "your wrist or hand hurts, tingles or feels numb.", "pick_ids": []})
    plan.append({"block": "Review", "minutes": 3, "goal": "Open the full coach (Tips tab) and read what changed",
                 "pick_ids": []})
    return plan


def make_trends(plays_all):
    wk = defaultdict(list)
    for s in plays_all:
        wk[week_start(s["ts"])].append(s)
    out = []
    for w in sorted(wk):
        ps = wk[w]
        _, brk, _, _, _ = stream_profile(ps, min_n=15)
        u = pooled_ur([s["e"] for s in ps])
        out.append({"week": w, "plays": len(ps), "ur": round(u) if u else None,
                    "acc": r3(mean(s["acc"] for s in ps)),
                    "misses_per_100": r1(sum(s["rj_misses"] for s in ps) / max(1, sum(s["n"] for s in ps)) * 100),
                    "stream_break_bpm": brk})
    return out


def overload_check(plays_all, now):
    """Last 7 days vs the 7 before: a sharp rise in play volume together with worse results is a fatigue /
    overload signal (or just harder maps - told apart by the median OD / AR / BPM). -> text or None."""
    w1 = [s for s in plays_all if now - 7 * 86400 <= s["ts"] <= now]
    w0 = [s for s in plays_all if now - 14 * 86400 <= s["ts"] < now - 7 * 86400]
    if len(w0) < 5 or len(w1) < 1.5 * len(w0):
        return None
    acc0, acc1 = mean(s["acc"] for s in w0), mean(s["acc"] for s in w1)
    mp = lambda ps: sum(s["rj_misses"] for s in ps) / max(1, sum(s["n"] for s in ps)) * 100  # noqa: E731
    m0, m1 = mp(w0), mp(w1)
    if not (acc1 <= acc0 - 0.02 or m1 >= m0 * 1.2):
        return None
    med = lambda ps, k: median(s[k] for s in ps)  # noqa: E731
    harder = (med(w1, "od") - med(w0, "od") >= 0.5 or med(w1, "ar") - med(w0, "ar") >= 0.5
              or med(w1, "bpm") - med(w0, "bpm") >= 10)
    txt = (f"You played {len(w1)} maps in the last 7 days vs {len(w0)} the week before, and your results dropped "
           f"(accuracy {acc0:.1%} -> {acc1:.1%}, misses per 100 notes {m0:.1f} -> {m1:.1f}). ")
    if harder:
        txt += ("The maps were also harder (higher median OD/AR/BPM), which explains part of it; still, keep "
                "session lengths in check.")
    else:
        txt += ("The maps weren't harder, so this looks like fatigue: take a lighter week (shorter sessions, "
                "easier maps, one more rest day).")
    return txt + " Stop for the day if your wrist or hand hurts, tingles or feels numb."


def mood_and_headline(trends, latest, st, now):
    if not latest:
        return "sleepy", "No plays found yet - go play a map and I'll take a look!"
    days = (now - latest["ts"]) / 86400
    if days >= 3:
        return "sleepy", f"No plays for {int(days)} days... a 20-minute warm-up would wake us both up!"
    tw = [t for t in trends if t["plays"] >= 3]
    if len(tw) >= 2:
        a, b = tw[-2], tw[-1]
        if a["ur"] and b["ur"] and a["ur"] - b["ur"] >= 5:
            return "happy", f"UR down {a['ur'] - b['ur']} since last week ({b['ur']}) - nice!"
        if a["acc"] and b["acc"] and b["acc"] - a["acc"] >= 0.01:
            return "happy", f"Accuracy up {100 * (b['acc'] - a['acc']):.1f}% vs last week ({b['acc']:.1%})!"
    avg = st["avg_acc"] or 0.7
    mp100 = latest["misses"] / max(1, latest["n"]) * 100
    if latest["acc"] < avg - 0.05 or mp100 > 8:
        nm = latest["name"].split(" [")[0]
        nm = nm if len(nm) <= 34 else nm[:33] + "~"
        return "worried", f"Rough one: {latest['misses']} misses on {nm}. Try an accuracy pick?"
    if latest["misses"] == 0:
        return "happy", "No misses on the last play!"
    if latest["misses"] <= 3:
        return "happy", f"Only {latest['misses']} miss{'es' if latest['misses'] != 1 else ''} on the last play - nearly a full combo!"
    if st.get("stream_break_bpm"):
        return "neutral", f"UR {st['ur']:.0f}, streams break ~{st['stream_break_bpm']} BPM. Click me for today's plan."
    return "neutral", f"UR {st['ur'] or 0:.0f}, avg acc {avg:.0%}. Click me for today's plan."


# ----------------------------------------------------------------------------
# contract v2 (CONTRACT_PP.md): phase, roadmap, techniques, setup checks, pp
# ----------------------------------------------------------------------------


def call_timeout(fn, timeout, *a, **kw):
    """Run an optional other-module call without letting it hang or crash the engine."""
    import threading
    res = {}

    def work():
        try:
            res["v"] = fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            res["e"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log(f"{getattr(fn, '__name__', fn)} timed out after {timeout}s")
        return None
    if "e" in res:
        log(f"{getattr(fn, '__name__', fn)} failed: {res['e']}")
    return res.get("v")


SR_DIFF_MODS = {"HR", "EZ", "FL"}   # mods (besides rate) that change star rating


def _diff_mods(s):
    return frozenset(m for m in re.findall(r"[A-Z]{2}", str(s).upper()) if m in SR_DIFF_MODS)


def load_sr_index():
    """md5 -> [(difficulty mods, rate, stars)] from pp_live's .coach_sr_cache.json (read only, any shape
    keyed "md5|mods_key|rate")."""
    d = load_json(SR_CACHE, None)
    if not isinstance(d, dict):
        return {}
    for k in ("entries", "sr", "cache", "data"):
        if isinstance(d.get(k), dict):
            d = d[k]
            break
    idx = defaultdict(list)
    for k, v in d.items():
        parts = str(k).split("|")
        if len(parts) < 3:
            continue
        val = v if isinstance(v, (int, float)) and not isinstance(v, bool) else \
            (v.get("stars") or v.get("sr")) if isinstance(v, dict) else None
        try:
            rate, val = float(parts[-1]), float(val)
        except (TypeError, ValueError):
            continue
        if 0 < val < 20:
            idx[parts[0]].append((_diff_mods("|".join(parts[1:-1])), rate, val))
    return idx


def sr_of(idx, md5, mods="NM", rate=1.0):
    dm = _diff_mods(mods)
    for m, r, v in idx.get(md5, ()):
        if m == dm and abs(r - rate) < 0.011:
            return v
    return None


def read_player_setup():
    s = load_json(SETTINGS_FILE, {})
    ps = s.get("player_setup") if isinstance(s, dict) else None
    ok = {"device": ("mouse", "tablet"), "keys": ("keyboard", "keypad", "mouse buttons"),
          "style": ("alternate", "singletap", "mixed")}
    out = {k: None for k in ok}
    if isinstance(ps, dict):
        for k, allowed in ok.items():
            v = ps.get(k)
            out[k] = v if v in allowed else None
    return out


def weighted_gain(pps, new_pp):
    """Exact change of the weighted top-100 sum (0.95^i) if a play worth new_pp were added."""
    old = sorted((p for p in pps if isinstance(p, (int, float))), reverse=True)[:100]
    new = sorted(old + [new_pp], reverse=True)[:100]
    w = lambda xs: sum(p * 0.95 ** i for i, p in enumerate(xs))  # noqa: E731
    return w(new) - w(old)


def _skill_acc_at(model, stars):
    acc_at = (model or {}).get("acc_at") if isinstance(model, dict) else None
    if not isinstance(acc_at, dict):
        return None
    best = None
    for k, v in acc_at.items():
        try:
            k, v = float(k), float(v)
        except (TypeError, ValueError):
            continue
        if abs(k - stars) <= 0.3 and (best is None or abs(k - stars) < best[0]):
            best = (abs(k - stars), v if v <= 1 else v / 100)
    return best[1] if best else None


def build_context(st, ex, plays, setup, game, fw, inp):
    st, ex = st or {}, ex or {}
    tot = ex.get("tot_miss") or 0
    rs = ex.get("reasons") or {}
    hi = [s for s in plays if (s.get("od") or 0) >= 7]
    tab = (inp or {}).get("tablet") or {}
    mouse = (inp or {}).get("mouse") or {}
    C = comfort_bpm(st, ex) if st else None
    c = {
        "ur": st.get("ur"), "me": st.get("mean_error_ms"), "me_ns": ex.get("me_ns"), "me_s": ex.get("me_s"),
        "me_spread": ex.get("me_spread"),
        "acc": st.get("avg_acc"), "acc_median": r3(median(s["acc"] for s in plays)) if plays else None,
        "nfc": st.get("near_fc_rate"), "C": C, "B": st.get("stream_break_bpm"),
        "stream_measured": bool(ex.get("stream_measured")), "drift": st.get("stream_drift_ms"),
        "single": st.get("single_tap_ratio"), "jr": st.get("jump_miss_rate"),
        "V": st.get("comfort_aim_velocity"),
        "V_floor": bool(ex.get("aim_floor")),
        "stream_unmeasurable": bool(ex.get("stream_unmeasurable")),
        "stream_aim_share": ex.get("stream_aim_share"),
        "landing": st.get("jump_landing_radii"),
        "aim_share": rs.get("A", 0) / tot if tot >= 20 else None,
        "lost_share": rs.get("L", 0) / tot if tot >= 20 else None,
        "early_share": rs.get("E", 0) / tot if tot >= 20 else None,
        "timing_share": rs.get("T", 0) / tot if tot >= 20 else None,
        "fatigue_flag": bool(ex.get("fatigue_flag")),
        "stamina_ratio": (r2(ex["fatigue"][1] / max(1, ex["fatigue_exp"]))
                          if ex.get("fatigue") and ex.get("fatigue_exp") is not None else None),
        "typical_ar": st.get("typical_ar"), "typical_od": st.get("typical_od"), "typical_bpm": st.get("typical_bpm"),
        "ur_hi_od": r1(pooled_ur([s["e"] for s in hi])) if len(hi) >= 5 else None,
        "device": setup.get("device"), "keys": setup.get("keys"), "style": setup.get("style"),
        "tablet_known": bool(tab.get("name")),
        "sens": K._num((fw or {}).get("CursorSensitivity")) or mouse.get("sensitivity"),
        "framesync": (fw or {}).get("FrameSync"),
        "mouse_buttons_off": str((game or {}).get("MouseDisableButtons", "")).lower() == "true",
        "dim_full": (K._num((game or {}).get("DimLevel")) or 0) >= 0.95,
    }
    return c


def band_measures(plays, sr_idx):
    """Per roadmap band: UR / median acc / near-FC rate on plays whose star rating is within 0.25*."""
    out = {}
    if not sr_idx:
        return out, 0
    rated = []
    for s in plays:
        v = sr_of(sr_idx, s["md5"], s["mods"], s["rate"])
        if v is not None:
            rated.append((v, s))
    for b in K.LOW_BANDS + K.BANDS:
        ps = [s for v, s in rated if abs(v - b["stars"]) <= 0.25]
        if ps:
            out[b["stars"]] = {"n": len(ps), "ur": r1(pooled_ur([s["e"] for s in ps])),
                               "acc": r3(median(s["acc"] for s in ps)),
                               "nfc": r3(sum(1 for s in ps if s["misses"] <= 3) / len(ps))}
    return out, len(rated)


def build_pp(ppd, tosu_ok, picks=()):
    base = {"configured": False, "pp": None, "rank": None, "gain_7d": None, "top50_floor": None,
            "tosu": bool(tosu_ok),
            "next_goal": "Add your own osu! API client (osu_api_config.json, see the .example file) and run "
                         "osu_api.py to track pp and get pp-targeted maps."}
    if not isinstance(ppd, dict) or not ppd.get("configured"):
        if isinstance(ppd, dict) and ppd.get("error"):
            err = str(ppd["error"])
            if len(err) > 240:   # cut at a sentence end, not mid-word
                cut = err[:240].rfind(". ")
                err = err[:cut + 1] if cut > 80 else err[:237].rstrip() + "..."
            base["next_goal"] = f"pp tracking: {err}"
        return base
    u = ppd.get("user") or {}
    pp = K._num(u.get("pp"))
    hist = [h for h in (ppd.get("history") or []) if isinstance(h, dict) and K._num(h.get("pp")) is not None
            and isinstance(h.get("date"), str)]
    hist.sort(key=lambda h: h["date"])
    gain = None
    if hist and pp is not None:
        cutoff = (datetime.now().date() - timedelta(days=7)).isoformat()
        older = [h for h in hist if h["date"] <= cutoff]
        ref = older[-1] if older else hist[0]
        if ref["date"] < datetime.now().date().isoformat():
            gain = r1(pp - float(ref["pp"]))
    th = ppd.get("pp_threshold") or {}
    top = [K._num(t.get("pp")) for t in (ppd.get("top_plays") or []) if isinstance(t, dict)]
    top = [p for p in top if p is not None]
    t50, t100 = K._num(th.get("top50")), K._num(th.get("top100"))
    goal = None
    picks = [p for p in picks if p.get("est_gain_pp") and p.get("title")]
    if picks:  # the first pp pick the plan actually recommends (owned first)
        cb = picks[0]
        goal = (f"Next: {cb['title']} [{cb.get('version') or '?'}]"
                + (f" {cb['mods']}" if cb.get("mods") and cb["mods"] != "NM" else "")
                + f" - a good run from you would add ~{cb['est_gain_pp']:.1f}pp (estimate).")
    elif top and t50:
        target = round(t50 * 1.2)
        goal = (f"A {target}pp play would add ~{weighted_gain(top, target):.1f}pp; anything over "
                f"{t100 or t50:.0f}pp counts toward your total.")
    return {"configured": True, "pp": pp, "rank": u.get("global_rank"), "gain_7d": gain,
            "next_goal": goal or "Set a new top-50 play: that's what moves your total.",
            "top50_floor": t50, "tosu": bool(tosu_ok)}


def _mods_list(mods, rate):
    out = []
    for a in re.findall(r"[A-Z]{2}", str(mods or "").upper()):
        if a == "NM":
            continue
        m = {"acronym": a}
        if a in ("DT", "NC", "HT", "DC") and abs(rate - (1.5 if a in ("DT", "NC") else 0.75)) > 1e-6:
            m["settings"] = {"speed_change": rate}
        out.append(m)
    return out


def pp_recs(ppd, lib, osu_index, history, ctx, phase, former_peak, budget=8):
    """purpose 'pp' recommendations from the climb list in pp_data.json (owned maps first)."""
    if not isinstance(ppd, dict) or not ppd.get("configured"):
        return []
    cur = ctx.get("current_stars")
    cap = None
    if cur:
        # a returning player can aim at their former peak, but not more than ~1* above today's level
        cap = max(cur + (0.4 if phase == "re-sharpen" else 0.7), min(former_peak or 0, cur + 1.0))
    rows = []
    for c in ppd.get("climb") or []:
        if not isinstance(c, dict) or not ((c.get("md5") and c["md5"] in lib) or c.get("title")):
            continue
        stars = K._num(c.get("stars"))
        mods = str(c.get("mods") or "NM").upper()
        if cap and stars and stars > cap:
            continue
        if phase == "re-sharpen" and ("DT" in mods or "NC" in mods) and cur and stars and stars > cur + 0.3:
            continue
        rows.append(c)
    rows.sort(key=lambda c: (not (c.get("owned") and c.get("md5") in lib), -(K._num(c.get("est_gain_pp")) or 0)))
    out = []
    for c in rows[:5]:
        md5 = c.get("md5") or ""
        f = lib.get(md5) if md5 else None
        owned = bool(f and f.get("mode") == 0 and f.get("n"))
        mods = str(c.get("mods") or "NM").upper() or "NM"
        rate = K._num(c.get("rate")) or             (1.5 if ("DT" in mods or "NC" in mods) else 0.75 if ("HT" in mods or "DC" in mods) else 1.0)
        hr = "HR" in mods
        rec = {"id": md5 or f"b{c.get('beatmap_id') or 0}", "purpose": "pp", "owned": owned,
               "artist": (f or {}).get("a") or c.get("artist") or "", "title": (f or {}).get("t") or c.get("title") or "",
               "version": (f or {}).get("v") or c.get("version") or "", "creator": (f or {}).get("c") or "",
               "beatmap_id": c.get("beatmap_id") or (f or {}).get("bid"), "set_id": c.get("set_id") or (f or {}).get("sid"),
               "mods": mods, "rate": rate, "stars": K._num(c.get("stars")), "est_gain_pp": K._num(c.get("est_gain_pp"))}
        if owned:
            rec.update({"bpm": round(disp_bpm(f) * rate), "ar": r1(ar_at(f["ar"], rate, hr)),
                        "od": r1(od_at(f["od"], rate, hr)), "cs": f["cs"], "length_s": round(f["len"] / rate),
                        "search": clean_search(f)})
        else:
            rec.update({"bpm": c.get("bpm"), "ar": c.get("ar"), "od": c.get("od"), "cs": c.get("cs"),
                        "length_s": c.get("length_s"),
                        "search": re.sub(r"[=<>!\"]", " ", f"{rec['artist']} {rec['title']} {rec['version']}").strip()})
        pred = None
        if pp_live is not None and owned and budget > 0 and hasattr(pp_live, "predict"):
            budget -= 1
            pred = call_timeout(pp_live.predict, 6, osu_index.get(md5), _mods_list(mods, rate), rate, rec["stars"])
        if isinstance(pred, dict):
            for k in ("pp_expected", "pp_good_run", "pp_ss"):
                if K._num(pred.get(k)) is not None:
                    rec[k] = round(float(pred[k]), 1)
            if rec["stars"] is None and K._num(pred.get("stars")):
                rec["stars"] = round(float(pred["stars"]), 2)
        h = history.get(md5, {"plays": 0, "best": None}) if md5 else {"plays": 0, "best": None}
        bits = [str(c.get("reason") or "A common top play for players a bit above you.").rstrip(".") + "."]
        if rec.get("pp_expected") is not None:
            bits.append(f"At your usual level here: ~{rec['pp_expected']:.0f}pp"
                        + (f", a good run ~{rec['pp_good_run']:.0f}pp" if rec.get("pp_good_run") is not None else "")
                        + (f" (SS {rec['pp_ss']:.0f}pp)." if rec.get("pp_ss") is not None else "."))
        if rec["est_gain_pp"]:
            pp_pred = K._num(c.get("pp_predicted"))
            bits.append("A good run from you" + (f" (~{pp_pred:.0f}pp, estimate)" if pp_pred else " (estimate)")
                        + f" would add ~{rec['est_gain_pp']:.1f}pp to your total."
                        + (f" Players a bit above you scored ~{K._fmt(c.get('typical_pp'))}pp on it."
                           if K._num(c.get("typical_pp")) else ""))
        if not owned:
            bits.append("Not downloaded yet: find it in lazer's beatmap listing (search below).")
        elif h["plays"]:
            bits.append(f"Your best: {h['best']:.1%} in {h['plays']} plays.")
        bits.append(K.mod_note(rec, ctx))
        rec["reason"] = " ".join(bits)
        ta = K._num(c.get("typical_acc"))
        rec["goal"] = ("Score attempts in the pp block only: max 5 full tries per session"
                       + (f", aim for {ta:.0%}+" if ta else "") + "; misses early in a run = finish it anyway as practice.")
        out.append(rec)
    return out


def sr_push_recs(lib, sr_idx, history, used, lo, hi, k=2, seed=0):
    """Push picks by real star rating (when pp_live has rated local maps): maps in [lo, hi]* NoMod."""
    rng = random.Random(seed)
    cands = []
    for md5, entries in sr_idx.items():
        f = lib.get(md5)
        if md5 in used or not f or f.get("mode") != 0 or not f.get("n") or f["len"] > 240 or f["len"] < 45:
            continue
        v = sr_of(sr_idx, md5, "NM", 1.0)
        if v is None or not lo <= v <= hi:
            continue
        h = history.get(md5, {"plays": 0, "best": None})
        if h["best"] is not None and h["best"] >= 0.95:
            continue
        cands.append((rng.random() + (0.3 if h["plays"] == 0 else 0), md5, v, h))
    cands.sort(key=lambda x: -x[0])
    out, songs = [], set()
    for _, md5, v, h in cands:
        f = lib[md5]
        song = (f["a"].lower(), f["t"].lower())
        if song in songs:
            continue
        songs.add(song)
        out.append({"id": md5, "purpose": "push", "artist": f["a"], "title": f["t"], "version": f["v"],
                    "creator": f["c"], "beatmap_id": f.get("bid"), "set_id": f.get("sid"), "mods": "NM", "rate": 1.0,
                    "bpm": round(disp_bpm(f)), "ar": f["ar"], "od": f["od"], "cs": f["cs"], "length_s": f["len"],
                    "stars": round(v, 2),
                    "reason": f"{v:.2f}* - inside the push range for your phase ({lo:g}-{hi:g}*). "
                              + ("New to you." if not h["plays"] else f"Your best: {h['best']:.1%}."),
                    "goal": "Pass it; misses are fine. Max 3 tries, then move on.", "search": clean_search(f)})
        if len(out) >= k:
            break
    return out


def recent_top_stars(ppd, now, days=180, min_acc=0.88):
    """(median stars, n) of API top plays set in the last `days` with acc >= min_acc, else None (needs >= 3)."""
    if not isinstance(ppd, dict) or not ppd.get("configured"):
        return None
    cutoff = datetime.fromtimestamp(now - days * 86400).date().isoformat()
    xs = []
    for t in ppd.get("top_plays") or []:
        if not isinstance(t, dict):
            continue
        sr, acc, d = K._num(t.get("stars")), K._num(t.get("acc")), str(t.get("date") or "")[:10]
        if sr and acc is not None and (acc if acc <= 1 else acc / 100) >= min_acc and d >= cutoff:
            xs.append(sr)
    return (median(xs), len(xs)) if len(xs) >= 3 else None


def prepare_v2(root, rc, keys, plays_all, recent, st, ex, now):
    """Everything that doesn't depend on the recommendations: context, roadmap, phase, checks."""
    setup = read_player_setup()
    game = K.read_ini(root / "game.ini", ["AudioOffset", "AutomaticallyAdjustBeatmapOffset", "MouseDisableButtons",
                                          "DimLevel", "ShowStoryboard", "HitLighting", "KeyOverlay"])
    fw = K.read_ini(root / "framework.ini", ["FrameSync", "WindowMode", "CursorSensitivity",
                                             "AudioUseExperimentalWasapi", "SizeFullscreen", "ConfineMouseMode"])
    inp = K.read_input_config(root / "input.json")
    ctx = build_context(st, ex, recent, setup, game, fw, inp)
    notes = []
    ctx["overload_txt"] = overload_check(plays_all, now)
    if ctx["overload_txt"]:
        notes.append("Load check: " + ctx["overload_txt"])
    ppd = load_json(PP_DATA, None)

    # star level: pp_live skill model > SR-rated plays > estimate from skills
    sr_idx = load_sr_index()
    model = None
    if pp_live is not None and hasattr(pp_live, "skill_model"):
        model = call_timeout(pp_live.skill_model, 30)
    measures, n_rated = band_measures(plays_all[:200], sr_idx) if sr_idx else ({}, 0)
    cs = K._num((model or {}).get("comfort_stars")) if isinstance(model, dict) else None
    # (only the main bands set the level here; LOW_BANDS measures are for the roadmap of players under 4*)
    solid = [s for s, m in measures.items() if s >= K.BANDS[0]["stars"] and m["n"] >= 3 and (m["acc"] or 0) >= 0.90]
    if cs:
        ctx["current_stars"] = round(cs, 2)
        ctx["stars_basis"] = (f"Star level from your skill model (pp_live, {model.get('n', '?')} star-rated plays): "
                              f"the highest star rating where your typical result is still a solid pass.")
    elif solid:
        ctx["current_stars"] = max(solid)
        ctx["stars_basis"] = (f"Highest roadmap band where your star-rated plays have a median accuracy of 90%+ "
                              f"({max(solid):g}*, {measures[max(solid)]['n']} plays).")
    else:
        est, basis = K.estimate_stars(ctx)
        if n_rated and isinstance(basis, str):   # star data exists, it just shows no solid band yet
            basis = basis.replace("no star-rating data was available (start tosu so the coach can rate maps)",
                                  f"none of your {n_rated} star-rated plays shows a band you pass solidly yet "
                                  "(90%+ median accuracy)")
        ctx["current_stars"], ctx["stars_basis"] = est, basis
        # the skills estimate reads low when practice maps are above the player's level: anchor it on the
        # star ratings of the player's recent good API top plays (osu_api.py) when those exist
        anc = recent_top_stars(ppd, now)
        if anc and (est is None or anc[0] > est):
            ctx["current_stars"] = round(anc[0], 1)
            ctx["stars_basis"] = (f"Median star rating of your {anc[1]} top plays from the last 180 days with 88%+ "
                                  f"accuracy (osu! API; {anc[0]:.2f}*). Your replay skills alone would put you at "
                                  f"~{est}*, which reads low because many practice maps are above your level.")
    ctx["acc_model"] = {b["stars"]: _skill_acc_at(model, b["stars"]) for b in K.BANDS
                        if _skill_acc_at(model, b["stars"]) is not None}
    if n_rated:
        notes.append(f"Roadmap uses star ratings for {n_rated} of your plays (from tosu via pp_live).")
    roadmap = K.build_roadmap(ctx, measures)

    # phase
    ts = [rc[p]["hdr"]["ts"] for p in keys if rc[p].get("hdr")]
    rts, rwhy = K.detect_return(ts, now)
    saved = load_json(PHASE_FILE, None)
    tops = [t for t in ((ppd or {}).get("top_plays") or []) if isinstance(t, dict)] if isinstance(ppd, dict) else []
    peak = [K._num(t.get("stars")) for t in tops if (K._num(t.get("acc")) or 0) >= 0.9 and K._num(t.get("stars"))]
    former_peak = max(peak) if peak else None
    phase = K.detect_phase(ctx, roadmap, rts, rwhy, saved, now, returning=K.return_gap(ts), former_peak=former_peak)
    ctx["phase"] = phase["name"]
    changed = isinstance(saved, dict) and saved.get("name") and saved.get("name") != phase["name"]
    if not isinstance(saved, dict) or saved.get("name") != phase["name"] or saved.get("since") != phase["since"]:
        try:
            atomic_write_json(PHASE_FILE, {"name": phase["name"], "since": phase["since"]})
        except Exception:
            pass

    tosu_ok = False
    if pp_live is not None and hasattr(pp_live, "tosu_available"):
        tosu_ok = bool(call_timeout(pp_live.tosu_available, 3))
    return {"ctx": ctx, "setup": setup, "game": game, "fw": fw, "inp": inp, "roadmap": roadmap, "phase": phase,
            "phase_changed": bool(changed), "sr_idx": sr_idx, "ppd": ppd, "tosu": tosu_ok,
            "former_peak": former_peak, "notes": notes}


def finish_recs(recs, v2, lib, osu_index, history, seed):
    """Phase push picks by real SR, pp picks, a mod/rate sentence on every pick, SR where known."""
    ctx, phase = v2["ctx"], v2["phase"]["name"]
    fp = v2.get("former_peak")
    cur = ctx.get("current_stars") or (fp - 0.5 if fp else None) or 4.0
    sr_idx = v2["sr_idx"]
    used = {r["id"] for r in recs}
    extra_push = []
    if sr_idx:
        # relative to the player: re-sharpen pushes towards levels passed before, rebuild up to ~0.3* past
        # the former peak (best 90%+ API top play) when known
        lo, hi = {"re-sharpen": (cur + 0.1, min(fp or cur + 0.6, cur + 0.8)),
                  "rebuild": (cur + 0.3, min(fp + 0.3, cur + 0.8) if fp else cur + 0.8)}.get(
            phase, (cur + 0.2, cur + 0.6))
        if hi > lo:
            extra_push = sr_push_recs(lib, sr_idx, history, used, round(lo, 1), round(hi, 1), 2, seed)
    pps = pp_recs(v2["ppd"], lib, osu_index, history, ctx, phase, v2.get("former_peak"))
    pps = [p for p in pps if p["id"] not in used]
    room = max(0, 25 - len(extra_push) - len(pps))
    base = recs[:room]
    if extra_push:  # real-SR push picks go first in the push group
        others = [r for r in base if r["purpose"] != "push"]
        base = others + extra_push + [r for r in base if r["purpose"] == "push"]
    out = base + pps
    for r in out:
        if r["purpose"] != "pp":
            if r.get("stars") is None and sr_idx:
                v = sr_of(sr_idx, r["id"], r["mods"], r["rate"])
                if v is not None:
                    r["stars"] = round(v, 2)
            r["reason"] = r["reason"].rstrip() + " " + K.mod_note(r, ctx)
    return out


def finish_v2(v2, recs=()):
    ctx = v2["ctx"]
    return {
        "phase": v2["phase"],
        "roadmap": v2["roadmap"],
        "techniques": K.rank_techniques(ctx),
        "setup_checks": K.setup_checks(ctx, v2["game"], v2["fw"], v2["inp"]),
        "pp": build_pp(v2["ppd"], v2["tosu"], [r for r in recs if r.get("purpose") == "pp"]),
        "player_setup": v2["setup"],
    }


def pick_player(rc, keys, player=None, root=None):
    """Whose replays these are (see coach_identity.py): --player NAME, else osu_api_config.json "user_id"/"user",
    else the account logged in to osu! (game.ini, read only), else whoever played most recently; names sharing
    that user id count too (renames). Accounts in "exclude_users" are never used.
    -> {"keys": [replay paths], "names": set, "me": display name, "uid": int|None}"""
    hdrs = [rc[p]["hdr"] for p in keys]
    names = Counter(h["player"] for h in hdrs)
    uid = None
    if player:
        me = player
        mine = {n for n in names if n.lower() == me.lower()}
        uids = Counter(h["uid"] for h in hdrs if h["player"] in mine and h["uid"])
        if uids:
            uid = uids.most_common(1)[0][0]
            mine |= {h["player"] for h in hdrs if h["uid"] == uid}
    else:
        if not names:
            raise RuntimeError("No osu!standard replays found yet. Play a few maps in osu!lazer (it saves a replay "
                               "of every pass), then run the coach again. (osu!stable only keeps replays you save "
                               "to its Replays folder, so osu!lazer is recommended.)")
        res = identity.resolve(hdrs, game_ini_username(root) if root else None)
        mine, uid = res["names"], res["uid"]
        me = next(iter(mine), "")
    out = [p for p in keys if rc[p]["hdr"]["player"] in mine]
    if player and not out:
        raise RuntimeError(f"No osu!standard replays by '{player}' found")
    if out and not player:  # show the account's current name (renames keep the uid)
        me = rc[max(out, key=lambda p: rc[p]["hdr"]["ts"])]["hdr"]["player"]
    return {"keys": out, "names": mine, "me": me, "uid": uid}


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def run(args):
    t_start = time.perf_counter()
    now = time.time()
    root = Path(args.osu_dir) if args.osu_dir else (oc.LAZER_DIR if oc.LAZER_DIR.exists() else oc.STABLE_DIR)
    if not root.exists():
        raise RuntimeError("Couldn't find your osu! folder. osu!lazer is recommended (it keeps a replay of every "
                           "play, which the coach learns from); it lives in %APPDATA%/osu. For another location "
                           "pass --osu-dir \"D:/path/to/osu\".")
    files_root = root / "files" if (root / "files").exists() else root / "Songs" if (root / "Songs").exists() else root
    status("running", "Indexing beatmaps...")
    cache = oc.load_cache()
    osu_index, found = oc.build_index(files_root, cache, want_replays=True)
    for sub in ("Replays", "exports"):
        if (root / sub).exists():
            found += [str(p) for p in (root / sub).glob("*.osr")]
    oc.save_cache(cache)
    found = list(dict.fromkeys(found))
    log(f"index: {len(osu_index)} beatmaps, {len(found)} replays ({time.perf_counter() - t_start:.1f}s)")

    status("running", "Reading replay headers...")
    rcache, new_hdr, _ = load_replays(found, osu_index)
    rc = rcache["replays"]
    # dedupe + pick player
    seen, keys = set(), []
    for p, e in rc.items():
        h = e.get("hdr")
        if not h or h["mode"] != 0:
            continue
        sig = (h["md5"], h["ts"], h["score"])
        if sig in seen:
            continue
        seen.add(sig)
        keys.append(p)
    pick = pick_player(rc, keys, args.player, root)
    keys, mine, me = pick["keys"], pick["names"], pick["me"]
    # Relax / Autopilot / Auto / Cinema: the game plays part of the map, so the stats would lie
    keys = [p for p in keys if not (rc[p]["hdr"].get("mbits", 0) & ASSISTED_BITS)]
    n_judged = judge_needed(rcache, keys, osu_index)
    atomic_write_json(REPLAY_CACHE, rcache, only_if_changed=True)
    log(f"replays: {len(keys)} by {sorted(mine)}, {new_hdr} new headers, {n_judged} judged "
        f"({time.perf_counter() - t_start:.1f}s)")

    status("running", "Reading the beatmap library...")
    lib, n_lib = build_library(osu_index, None)
    log(f"library: {sum(1 for f in lib.values() if f.get('mode') == 0 and not f.get('skip'))} std maps, "
        f"{n_lib} new ({time.perf_counter() - t_start:.1f}s)")

    plays_all = sorted((rc[p]["sum"] for p in keys if rc[p].get("sum")), key=lambda s: -s["ts"])
    plays_all = [s for s in plays_all if s["n"] >= 50]
    recent = plays_all[:args.last]
    history = {}
    for p in keys:
        h = rc[p]["hdr"]
        e = history.setdefault(h["md5"], {"plays": 0, "best": None})
        e["plays"] += 1
        e["best"] = h["acc"] if e["best"] is None else max(e["best"], h["acc"])

    status("running", "Building your coaching plan...")
    st, ex = profile(recent, plays_all, lib)
    latest = plays_all[0] if plays_all else None
    seed = int(datetime.now().strftime("%Y%m%d"))
    recs, ncands, focus, plan, spots = [], {}, [], [], []
    v2, v2_out, v2_err = None, {}, None
    try:
        v2 = prepare_v2(root, rc, keys, plays_all, recent, st, ex, now)
    except Exception:
        v2_err = traceback.format_exc()
        log(v2_err)
    phase_name = v2["phase"]["name"] if v2 else None
    if st:
        recs, ncands = recommend(lib, st, ex, history, seed)
        if v2:
            try:
                recs = finish_recs(recs, v2, lib, osu_index, history, seed)
            except Exception:
                v2_err = traceback.format_exc()
                log(v2_err)
        focus = make_focus(st, ex, phase_name, v2["roadmap"] if v2 else None)
        plan = make_plan(recs, st, ex, datetime.now().toordinal(), focus, phase_name)
        spots = trouble_spots(recent, st, ex)
    if v2:
        try:
            v2_out = finish_v2(v2, recs)
        except Exception:
            v2_err = traceback.format_exc()
            log(v2_err)
    trends = make_trends(plays_all)
    if st:
        mood, headline = mood_and_headline(trends, latest, st, now)
        if v2 and v2.get("phase_changed") and mood != "sleepy":
            mood, headline = "happy", f"New training phase: {phase_name}! Click me to see what changes."
    else:
        mood, headline = ("sleepy", "Play a few more maps so I can get to know you!")
    latest_ts_all = latest["ts"] if latest else None  # must match latest_play / recent_plays[0]
    has_sr = bool(v2 and v2.get("sr_idx"))
    notes = ["Slider bodies are not judged; "
             + ("star ratings come from tosu via pp_live where available."
                if has_sr else "star rating not computed (difficulty shown as BPM/AR/OD/spacing)."),
             "bpm/ar/od/length in recommendations are the values at the recommended rate.",
             "Speed changes (HT/DT custom speed) also change effective AR and OD.",
             "Rates come from lazer's custom speed; in lazer HT goes 0.5-0.99x and DT 1.01-2.0x.",
             "Aim speed is cursor travel per ms between notes (px/ms, osu! pixels)."]
    if ex.get("n_stream_plays", 0) > len(recent):
        notes.append(f"Stream stats use your last {ex['n_stream_plays']} plays (few streams in the recent ones).")
    if st and ex.get("stream_unmeasurable"):
        notes.append("Stream limit not measurable yet: your stream miss rate is high at every BPM with enough data "
                     "(fitted as a curve that can only rise with BPM, needing 2+ maps per 10-BPM band), so there is "
                     f"no clean band to call 'comfort'. Speed picks are a test at {STREAM_TEST_BPM[0]}-"
                     f"{STREAM_TEST_BPM[1]} BPM (1.0x where a map fits, never below 0.8x) instead of maps slowed far "
                     "below that."
                     + (f" {ex['stream_aim_share']:.0%} of stream misses were the cursor off the circle."
                        if ex.get("stream_aim_share") is not None else ""))
    elif st and st.get("stream_break_bpm") and not ex.get("stream_measured", True):
        notes.append(f"Your streams already break at the slowest BPM with enough data ({st['stream_break_bpm']}), "
                     f"so speed picks start at ~{comfort_bpm(st, ex)} BPM, an estimate below it.")
    if st and ex.get("aim_floor"):
        notes.append("Jump misses already climb below 0.75 px/ms (the 'comfort aim' number is the measured bin, "
                     "below the speeds the coach's aim picks use); aim picks use 0.75 px/ms as their floor.")
    if len(mine) > 1:
        notes.append("Combined replays from names: " + ", ".join(sorted(mine)) + " (same account).")
    # other accounts on this PC with many replays: maybe the same person on a second account - ask, never merge
    others = Counter((rc[p]["hdr"]["player"], rc[p]["hdr"]["uid"]) for p in rc
                     if rc[p].get("hdr") and rc[p]["hdr"].get("mode") == 0 and rc[p]["hdr"]["player"] not in mine
                     and rc[p]["hdr"].get("player"))
    for (nm, uid), n in others.most_common(2):
        if n >= 20:
            notes.append(f"Also found {n} replays by '{nm}' (a different account{f', id {uid}' if uid else ''}). "
                         "They are NOT used in this advice. If that account is also you, the advice is based on only "
                         f"part of your play: 'python coach_engine.py --player {nm} --out coach_data_other.json' "
                         "writes that account's numbers to a separate file. The coach never merges accounts by itself.")
    if v2:
        notes += v2.get("notes", [])
        if v2["ctx"].get("me_ns") is not None and v2["ctx"].get("me_s") is not None:
            notes.append(f"Mean hit error: {v2['ctx']['me_ns']:+.1f} ms on regular notes, "
                         f"{v2['ctx']['me_s']:+.1f} ms in streams/bursts.")
        notes.append("Techniques are labelled 'research' only where a motor-learning study supports the principle; "
                     "roadmap numbers are coach heuristics.")
    if v2_err:
        notes.append("Part of the new roadmap/pp section failed this run; the rest of the advice is unaffected.")
    # adaptive loop: tracks, today's session, a debrief for every attempt (replay or live)
    adapt = None
    if coach_adapt is not None and st and not ((HERE / ".adapt_disabled").exists() and not os.environ.get("OSU_COACH_STATE_DIR")):
        try:
            status("running", "Judging your latest attempt...")
            actx = {"stats": st, "ex": ex, "phase": phase_name, "typ_den": ex.get("typ_den"),
                    "current_stars": (v2["ctx"].get("current_stars") if v2 else None),
                    "focus_ids": [f.get("id") for f in focus]}
            adapt = coach_adapt.update(plays_all, lib, osu_index, history, actx, now=now)
        except Exception:
            log("coach_adapt failed:\n" + traceback.format_exc())
            notes.append("The adaptive session part failed this run; showing the classic plan instead.")
    if adapt:
        ad_recs = adapt.get("recommendations") or []
        ids = {r["id"] for r in ad_recs}
        recs = (ad_recs + [r for r in recs if r["id"] not in ids])[:30]
        if adapt.get("session_plan"):
            plan = adapt["session_plan"]
        ld = adapt.get("last_debrief")
        if ld and now - ld["ts"] <= 20 * 60:
            headline = ld["headline"]
            lc = ld.get("level_change")
            mood = ("happy" if ld["verdict"] == "pass" or (lc and lc["to"] > lc["from"]) else
                    "worried" if ld["verdict"] == "fail" else "neutral")
            if (adapt.get("session") or {}).get("fatigue", {}).get("flag"):
                mood = "sleepy"
        notes.append("Levels: each skill has a difficulty level that moves with your results (2 passes in a row = "
                     "up one step, a miss of the goal = down one step, at most one step down per session). Only "
                     "attempts on the plan's maps move levels during a session; free plays are reviewed the next "
                     "morning (max one step).")
    data = {
        "version": 1, "generated_at": int(now), "player": me, "n_plays": len(recent),
        "latest_play_ts": latest_ts_all,
        "mood": mood, "headline": headline[:90],
        "latest_play": ({"map": latest["name"], "mods": latest["mods"], "acc": r3(latest["acc"]),
                         "misses": latest["misses"], "ur": round(latest["ur"] or 0),
                         "comment": play_comment(latest, st["avg_acc"] if st else None)} if latest else None),
        "stats": st,
        "focus": focus,
        "session_plan": plan,
        "recommendations": recs,
        "trouble_spots": spots,
        "recent_plays": [{"ts": s["ts"], "map": s["name"], "id": s["md5"], "mods": s["mods"], "acc": r3(s["acc"]),
                          "misses": s["misses"], "ur": round(s["ur"] or 0), "max_combo": s["combo"],
                          "verdict": verdict(s), "main_issue": main_issue(s)} for s in recent[:40]],
        "trends": trends,
        "notes": notes,
    }
    data.update(v2_out)
    if adapt:
        for k in ("session", "last_debrief", "debriefs_today", "changes", "tracks"):
            data[k] = adapt.get(k)
    out = Path(args.out)
    if not out.is_absolute():
        out = HERE / out
    atomic_write_json(out, data, indent=1)
    dt = time.perf_counter() - t_start
    log(f"wrote {out} in {dt:.1f}s; candidates per purpose: {ncands}")
    status("done", f"Updated in {dt:.0f}s: {len(recent)} plays, {len(recs)} picks.")
    # after the write (never delays a debrief): real star ratings for newly downloaded maps (1 API request max)
    if coach_adapt is not None:
        call_timeout(coach_adapt.api_sr_fill, 8, lib, 1, log)


def main():
    ap = argparse.ArgumentParser(description="osu! coach engine (writes coach_data.json)")
    ap.add_argument("--last", type=int, default=40)
    ap.add_argument("--out", default="coach_data.json")
    ap.add_argument("--player")
    ap.add_argument("--osu-dir")
    args = ap.parse_args()
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        run(args)
    except RuntimeError as e:   # expected setup problems (no osu! folder, no replays yet): a message, no traceback
        log(f"osu coach: {e}")
        status("error", str(e))
        sys.exit(1)
    except Exception as e:
        log(traceback.format_exc())
        status("error", f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
