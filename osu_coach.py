#!/usr/bin/env python3
"""
osu! coach — replay analyzer + adaptive practice planner (osu!standard).

Reads your replays (.osr), finds the matching beatmap (.osu) in your osu!lazer
or osu!stable install, re-judges every hit object, and tells you *why* you lose
accuracy and combo: aim vs timing misses, overshoot/undershoot on jumps, stream
speed ceiling, tapping drift, fatigue, single-tapping in streams, etc.
Then it writes a focused practice plan for your next sessions.

No third-party packages needed (Python 3.9+).

Usage:
  python osu_coach.py                       # scan lazer for your recent replays
  python osu_coach.py --last 20             # analyse the 20 most recent
  python osu_coach.py replay.osr other.osr  # specific replay files
  python osu_coach.py --osu-dir "D:/osu!"   # stable install (uses Songs/ + Replays/)
  python osu_coach.py --player YourName     # only that player's replays

Limitations (approximations, stated honestly):
  * Slider heads are judged; slider bodies/ticks/ends are not (no slider breaks).
  * Stacking offsets are ignored (stacked notes may look a few px off-centre).
  * No star-rating calculation; difficulty is described by BPM, spacing, AR/OD/CS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import math
import os
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median, pstdev

# ----------------------------------------------------------------------------
# Binary helpers for .osr
# ----------------------------------------------------------------------------


class Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def _unpack(self, fmt, n):
        v = struct.unpack_from(fmt, self.d, self.p)[0]
        self.p += n
        return v

    def byte(self):
        return self._unpack("<B", 1)

    def short(self):
        return self._unpack("<H", 2)

    def int(self):
        return self._unpack("<i", 4)

    def long(self):
        return self._unpack("<q", 8)

    def uleb(self):
        result = shift = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def string(self):
        flag = self.byte()
        if flag == 0x00:
            return ""
        if flag != 0x0B:
            raise ValueError("bad string flag")
        n = self.uleb()
        s = self.d[self.p:self.p + n].decode("utf-8", "replace")
        self.p += n
        return s

    def raw(self, n):
        b = self.d[self.p:self.p + n]
        self.p += n
        return b


MODS = {1: "NF", 2: "EZ", 8: "HD", 16: "HR", 32: "SD", 64: "DT", 256: "HT",
        512: "NC", 1024: "FL", 4096: "SO", 16384: "PF"}
K_LEFT, K_RIGHT = 1, 2  # bits 1 and 2 cover M1/K1 and M2/K2


@dataclass
class Replay:
    path: str
    mode: int
    version: int
    beatmap_md5: str
    player: str
    n300: int
    n100: int
    n50: int
    misses: int
    score: int
    max_combo: int
    mods: int
    timestamp: int  # .NET ticks
    frames: list  # (t, x, y, keys)

    @property
    def mod_str(self):
        s = [n for b, n in MODS.items() if self.mods & b]
        if "NC" in s and "DT" in s:
            s.remove("DT")
        if "PF" in s and "SD" in s:
            s.remove("SD")
        return "".join(s) or "NM"

    @property
    def rate(self):
        r = getattr(self, "rate_override", None)  # lazer custom speed (set by coach_engine)
        if r:
            return r
        if self.mods & (64 | 512):
            return 1.5
        if self.mods & 256:
            return 0.75
        return 1.0

    @property
    def accuracy(self):
        tot = self.n300 + self.n100 + self.n50 + self.misses
        if not tot:
            return 0.0
        return (300 * self.n300 + 100 * self.n100 + 50 * self.n50) / (300 * tot)


def parse_osr(path, frames=True) -> Replay:
    data = Path(path).read_bytes()
    r = Reader(data)
    mode = r.byte()
    version = r.int()
    bmd5 = r.string()
    player = r.string()
    r.string()  # replay md5
    n300, n100, n50, _geki, _katu, miss = (r.short() for _ in range(6))
    score = r.int()
    combo = r.short()
    r.byte()  # perfect
    mods = r.int()
    r.string()  # life bar
    ts = r.long()
    clen = r.int()
    fr = []
    if frames:
        raw = lzma.decompress(r.raw(clen)).decode("ascii", "replace")
        t = 0
        for chunk in raw.split(","):
            if not chunk:
                continue
            parts = chunk.split("|")
            if len(parts) < 4:
                continue
            w = int(parts[0])
            if w == -12345:
                continue  # RNG seed frame
            t += w
            fr.append((t, float(parts[1]), float(parts[2]), int(float(parts[3]))))
    return Replay(str(path), mode, version, bmd5, player, n300, n100, n50, miss,
                  score, combo, mods, ts, fr)


def looks_like_osr(head: bytes) -> bool:
    """Signature check for content-addressed lazer files (no extension)."""
    if len(head) < 40 or head[0] > 3:
        return False
    ver = struct.unpack_from("<i", head, 1)[0]
    if not (20000000 <= ver <= 40000000):
        return False
    if head[5] != 0x0B or head[6] != 32:
        return False
    try:
        int(head[7:39].decode("ascii"), 16)
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# .osu beatmap parsing
# ----------------------------------------------------------------------------


@dataclass
class HitObject:
    x: float
    y: float
    time: float
    kind: str  # circle | slider | spinner
    end_time: float = 0.0
    end_x: float = 0.0
    end_y: float = 0.0
    new_combo: bool = False


@dataclass
class Beatmap:
    title: str = ""
    artist: str = ""
    version: str = ""
    creator: str = ""
    cs: float = 5
    od: float = 5
    ar: float = 5
    hp: float = 5
    slider_mult: float = 1.4
    timing: list = field(default_factory=list)  # (time, beat_len, uninherited)
    objects: list = field(default_factory=list)

    @property
    def name(self):
        return f"{self.artist} - {self.title} [{self.version}]"

    def beat_at(self, t):
        bl = 500.0
        for tp_t, tp_bl, unin in self.timing:
            if tp_t > t:
                break
            if unin:
                bl = tp_bl
        return bl

    def sv_at(self, t):
        sv = 1.0
        for tp_t, tp_bl, unin in self.timing:
            if tp_t > t:
                break
            if unin:
                sv = 1.0
            elif tp_bl < 0:
                sv = max(0.1, min(10.0, -100.0 / tp_bl))
        return sv


def parse_osu(text: str) -> Beatmap:
    bm = Beatmap()
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section in ("Metadata", "Difficulty") and ":" in line:
            k, v = (s.strip() for s in line.split(":", 1))
            try:
                if k == "Title": bm.title = v
                elif k == "Artist": bm.artist = v
                elif k == "Version": bm.version = v
                elif k == "Creator": bm.creator = v
                elif k == "CircleSize": bm.cs = float(v)
                elif k == "OverallDifficulty": bm.od = float(v); bm.ar = bm.ar if "ApproachRate" in text else float(v)
                elif k == "ApproachRate": bm.ar = float(v)
                elif k == "HPDrainRate": bm.hp = float(v)
                elif k == "SliderMultiplier": bm.slider_mult = float(v)
            except ValueError:
                pass
        elif section == "TimingPoints":
            p = line.split(",")
            if len(p) < 2:
                continue
            unin = True if len(p) < 7 else p[6].strip() == "1"
            try:
                bm.timing.append((float(p[0]), float(p[1]), unin))
            except ValueError:
                pass
        elif section == "HitObjects":
            p = line.split(",")
            if len(p) < 4:
                continue
            x, y, t, typ = float(p[0]), float(p[1]), float(p[2]), int(p[3])
            nc = bool(typ & 4)
            if typ & 1:
                bm.objects.append(HitObject(x, y, t, "circle", t, x, y, nc))
            elif typ & 2 and len(p) >= 8:
                pts = p[5].split("|")[1:]
                ctrl = [(float(a), float(b)) for a, b in (q.split(":") for q in pts if ":" in q)]
                slides = int(p[6])
                length = float(p[7])
                bm.objects.append(HitObject(x, y, t, "slider", 0, 0, 0, nc))
                bm.objects[-1]._ctrl = ctrl
                bm.objects[-1]._slides = slides
                bm.objects[-1]._len = length
            elif typ & 8:
                end = float(p[5]) if len(p) > 5 else t
                bm.objects.append(HitObject(256, 192, t, "spinner", end, 256, 192, nc))
    bm.timing.sort(key=lambda a: a[0])
    # slider durations / approximate end positions
    for o in bm.objects:
        if o.kind != "slider":
            continue
        beat = bm.beat_at(o.time)
        sv = bm.sv_at(o.time)
        dur = o._len / (bm.slider_mult * 100 * sv) * beat * o._slides
        o.end_time = o.time + dur
        if o._slides % 2 == 1 and o._ctrl:
            o.end_x, o.end_y = o._ctrl[-1]  # approximation: last control point
        else:
            o.end_x, o.end_y = o.x, o.y
    return bm


# ----------------------------------------------------------------------------
# Locating beatmaps & replays
# ----------------------------------------------------------------------------

try:
    from coach_identity import lazer_dir as _lazer_dir  # honours a moved lazer folder (storage.ini)
    LAZER_DIR = _lazer_dir()
except Exception:
    LAZER_DIR = Path(os.environ.get("APPDATA", "")) / "osu"
STABLE_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "osu!"
CACHE = Path(__file__).with_name(".osu_coach_cache.json")


_CACHE_TEXT = None   # the cache file's text as last read/written (skip rewriting an unchanged 2.5 MB file)


def load_cache():
    global _CACHE_TEXT
    try:
        t = CACHE.read_text()
        c = json.loads(t)
        _CACHE_TEXT = t
        return c
    except Exception:
        return {"osu": {}, "osr": {}}


def save_cache(c):
    """Atomic (a crash mid-write must not corrupt the cache and force a full re-hash) and only when changed."""
    global _CACHE_TEXT
    try:
        t = json.dumps(c)
        if t == _CACHE_TEXT and CACHE.exists():
            return
        tmp = CACHE.with_name(CACHE.name + ".tmp")
        tmp.write_text(t)
        for _ in range(20):
            try:
                os.replace(tmp, CACHE)
                break
            except PermissionError:
                import time as _t
                _t.sleep(0.1)
        _CACHE_TEXT = t
    except Exception:
        pass


def build_index(root: Path, cache, want_replays: bool):
    """Return md5->path for .osu files, and a list of replay paths."""
    osu_index = {}
    replays = []
    oc = cache.setdefault("osu", {})
    rc = cache.setdefault("osr", {})
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            key = fp
            if key in oc:
                if oc[key]:
                    osu_index[oc[key]] = fp
                continue
            if key in rc:
                if rc[key]:
                    replays.append(fp)
                continue
            low = fn.lower()
            if low.endswith((".mp3", ".ogg", ".wav", ".png", ".jpg", ".jpeg", ".mp4", ".avi", ".flv", ".osb", ".realm")):
                oc[key] = ""
                continue
            try:
                with open(fp, "rb") as f:
                    head = f.read(48)
                    if head.lstrip(b"\xef\xbb\xbf").startswith(b"osu file format"):
                        f.seek(0)
                        md5 = hashlib.md5(f.read()).hexdigest()
                        oc[key] = md5
                        osu_index[md5] = fp
                    elif want_replays and (low.endswith(".osr") or "." not in fn) and looks_like_osr(head):
                        rc[key] = True
                        replays.append(fp)
                    else:
                        oc[key] = ""
            except OSError:
                continue
    return osu_index, replays


# ----------------------------------------------------------------------------
# Judging
# ----------------------------------------------------------------------------


@dataclass
class Judged:
    obj: HitObject
    idx: int
    category: str = "other"
    result: str = "miss"  # 300 | 100 | 50 | miss
    error: float | None = None  # ms (real time), + = late
    off_x: float = 0.0
    off_y: float = 0.0
    along: float | None = None  # +overshoot / -undershoot, in radii
    across: float | None = None
    miss_reason: str = ""
    dist_in: float = 0.0  # px from previous object end
    dt_in: float = 0.0  # ms real time from previous object end
    run_len: int = 0
    run_pos: int = 0
    key: int = 0
    path_eff: float | None = None
    bpm_stream: float = 0.0


def classify(bm: Beatmap, objs, radius, rate):
    """Attach movement + pattern info to each object."""
    js = [Judged(o, i) for i, o in enumerate(objs)]
    prev = None
    for j in js:
        o = j.obj
        if prev is not None:
            j.dist_in = math.hypot(o.x - prev.end_x, o.y - prev.end_y)
            j.dt_in = max(1.0, (o.time - prev.end_time) / rate)
        else:
            j.dt_in = 99999
        prev = o
    # stream runs: consecutive objects with ~1/4 rhythm
    i = 0
    n = len(js)
    while i < n:
        def is_q(k):
            o = js[k].obj
            if k == 0 or o.kind == "spinner" or js[k - 1].obj.kind == "spinner":
                return False
            beat = bm.beat_at(o.time)
            gap = o.time - js[k - 1].obj.time
            return gap <= beat * 0.27 and gap > 0
        if i + 1 < n and is_q(i + 1):
            k = i + 1
            while k + 1 < n and is_q(k + 1):
                k += 1
            run = js[i:k + 1]
            for p, jj in enumerate(run):
                jj.run_len = len(run)
                jj.run_pos = p
                gap = (js[i + 1].obj.time - js[i].obj.time) / rate
                jj.bpm_stream = 15000 / gap if gap else 0
            i = k + 1
        else:
            i += 1
    for j in js:
        o = j.obj
        if o.kind == "spinner":
            j.category = "spinner"
        elif j.run_len >= 7:
            j.category = "stream"
        elif j.run_len >= 3:
            j.category = "burst"
        elif j.dist_in >= radius * 3.0 and j.dt_in < 400:
            j.category = "jump"
        elif o.kind == "slider":
            j.category = "slider"
        elif j.dt_in >= 900:
            j.category = "after break"
        else:
            j.category = "other"
    return js


def cursor_at(frames, t, start=0):
    """Return (x, y, index) of the frame at or just before t (linear interp)."""
    i = start
    n = len(frames)
    while i + 1 < n and frames[i + 1][0] <= t:
        i += 1
    f = frames[i]
    if i + 1 < n and frames[i + 1][0] > f[0]:
        g = frames[i + 1]
        a = (t - f[0]) / (g[0] - f[0])
        a = max(0.0, min(1.0, a))
        return f[1] + (g[1] - f[1]) * a, f[2] + (g[2] - f[2]) * a, i
    return f[1], f[2], i


def judge(rep: Replay, bm: Beatmap):
    mods = rep.mods
    cs, od = bm.cs, bm.od
    if mods & 16:
        cs, od = min(10, cs * 1.3), min(10, od * 1.4)
    if mods & 2:
        cs, od = cs * 0.5, od * 0.5
    radius = 54.4 - 4.48 * cs
    w300, w100, w50 = 80 - 6 * od, 140 - 8 * od, 200 - 10 * od
    objs = []
    for o in bm.objects:
        o2 = HitObject(o.x, o.y, o.time, o.kind, o.end_time, o.end_x, o.end_y, o.new_combo)
        if mods & 16:
            o2.y, o2.end_y = 384 - o2.y, 384 - o2.end_y
        objs.append(o2)
    js = classify(bm, objs, radius, rep.rate)
    frames = rep.frames
    if not frames:
        return js, radius, (w300, w100, w50)

    # key presses (rising edges)
    presses = []
    prev_keys = 0
    for t, x, y, k in frames:
        new = k & ~prev_keys & 3
        for bit in (K_LEFT, K_RIGHT):
            if new & bit:
                presses.append((t, x, y, bit))
        prev_keys = k

    targets = [j for j in js if j.obj.kind != "spinner"]
    ptr = 0
    attempts = defaultdict(list)  # target idx -> list of (dt, dist/r)

    def expire(t):
        nonlocal ptr
        while ptr < len(targets) and t > targets[ptr].obj.time + w50:
            targets[ptr].result = "miss"
            ptr += 1

    for t, x, y, key in presses:
        expire(t)
        if ptr >= len(targets):
            break
        cur = targets[ptr]
        o = cur.obj
        dt = t - o.time
        if dt < -400:
            continue  # too early to matter
        d = math.hypot(x - o.x, y - o.y)
        if d <= radius:
            if abs(dt) <= w50:
                cur.error = dt / rep.rate
                cur.result = "300" if abs(dt) <= w300 else "100" if abs(dt) <= w100 else "50"
                cur.off_x, cur.off_y = x - o.x, y - o.y
                cur.key = key
                ptr += 1
            else:  # early press on the note -> miss (shake) in stable/classic
                cur.result = "miss"
                cur.miss_reason = "timing (tapped too early)"
                ptr += 1
        else:
            attempts[ptr].append((dt, d / radius))

    expire(float("inf"))

    # miss reasons + aim vectors
    prev = None
    for ti, j in enumerate(targets):
        o = j.obj
        if j.result == "miss" and not j.miss_reason:
            at = [a for a in attempts.get(ti, []) if abs(a[0]) <= w50]
            if at:
                nearest = min(a[1] for a in at)
                j.miss_reason = "aim (tapped in time, cursor off the circle)" if nearest > 1.0 else "aim"
            else:
                cx, cy, _ = cursor_at(frames, o.time)
                d = math.hypot(cx - o.x, cy - o.y) / radius
                if d <= 1.5:
                    j.miss_reason = "timing / no tap (cursor was on it)"
                else:
                    j.miss_reason = "lost / no tap (cursor elsewhere)"
        if j.result != "miss" and prev is not None:
            vx, vy = o.x - prev.end_x, o.y - prev.end_y
            L = math.hypot(vx, vy)
            if L > radius:
                ux, uy = vx / L, vy / L
                j.along = (j.off_x * ux + j.off_y * uy) / radius
                j.across = (-j.off_x * uy + j.off_y * ux) / radius
        prev = o

    # cursor path efficiency for jumps (path length / straight distance)
    fi = 0
    for ti, j in enumerate(targets):
        if j.category != "jump" or j.result == "miss" or ti == 0:
            continue
        p = targets[ti - 1].obj
        if p.kind != "circle" or targets[ti - 1].result == "miss":
            continue  # slider end positions are approximate; only circle->circle
        t0, t1 = p.end_time, j.obj.time + (j.error or 0) * rep.rate
        # resample at ~60 Hz so high-polling-rate jitter doesn't inflate the path
        while fi + 1 < len(frames) and frames[fi + 1][0] <= t0:
            fi += 1
        length = 0.0
        px, py, k = cursor_at(frames, t0, fi)
        t = t0 + 16
        while t < t1:
            x, y, k = cursor_at(frames, t, k)
            length += math.hypot(x - px, y - py)
            px, py = x, y
            t += 16
        x, y, k = cursor_at(frames, t1, k)
        length += math.hypot(x - px, y - py)
        if j.dist_in > 0 and length > 0:
            j.path_eff = length / j.dist_in
    return js, radius, (w300, w100, w50)


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------


def ur(errors):
    return pstdev(errors) * 10 if len(errors) >= 2 else 0.0


def summarize(rep: Replay, bm: Beatmap):
    js, radius, windows = judge(rep, bm)
    tgt = [j for j in js if j.obj.kind != "spinner"]
    hits = [j for j in tgt if j.result != "miss"]
    errs = [j.error for j in hits if j.error is not None]
    s = {
        "replay": rep, "map": bm, "radius": radius, "windows": windows, "js": tgt,
        "n": len(tgt), "misses": sum(1 for j in tgt if j.result == "miss"),
        "n300": sum(1 for j in tgt if j.result == "300"),
        "ur": ur(errs), "mean_err": mean(errs) if errs else 0.0,
    }
    # per category
    cats = {}
    for c in ("stream", "burst", "jump", "slider", "other", "after break"):
        g = [j for j in tgt if j.category == c]
        if not g:
            continue
        ge = [j.error for j in g if j.error is not None]
        al = [j.along for j in g if j.along is not None]
        cats[c] = {
            "n": len(g), "miss": sum(1 for j in g if j.result == "miss"),
            "p300": sum(1 for j in g if j.result == "300") / len(g),
            "ur": ur(ge), "mean_err": mean(ge) if ge else 0.0,
            "along": mean(al) if al else 0.0,
        }
    s["cats"] = cats
    s["miss_reasons"] = Counter(j.miss_reason for j in tgt if j.result == "miss")

    # fatigue: thirds
    thirds = []
    for k in range(3):
        g = tgt[k * len(tgt) // 3:(k + 1) * len(tgt) // 3]
        ge = [j.error for j in g if j.error is not None]
        thirds.append({"miss": sum(1 for j in g if j.result == "miss"), "ur": ur(ge), "n": len(g)})
    s["thirds"] = thirds

    # streams: drift (first half vs second half of long runs), single-tap ratio, bpm buckets
    early, late, same_key, alt_key = [], [], 0, 0
    bpm_b = defaultdict(lambda: {"n": 0, "miss": 0, "err": []})
    for j in tgt:
        if j.run_len >= 7:
            if j.error is not None:
                (early if j.run_pos < j.run_len / 2 else late).append(j.error)
            b = int(j.bpm_stream // 10 * 10)
            bpm_b[b]["n"] += 1
            bpm_b[b]["miss"] += j.result == "miss"
            if j.error is not None:
                bpm_b[b]["err"].append(j.error)
    for a, b in zip(tgt, tgt[1:]):
        if b.run_len >= 3 and b.run_pos > 0 and a.key and b.key:
            if a.key == b.key:
                same_key += 1
            else:
                alt_key += 1
    s["stream_drift"] = (mean(late) - mean(early)) if early and late else None
    s["single_tap_ratio"] = same_key / (same_key + alt_key) if same_key + alt_key >= 10 else None
    s["bpm_buckets"] = {k: {"n": v["n"], "miss": v["miss"], "ur": ur(v["err"])} for k, v in sorted(bpm_b.items())}

    # jumps by spacing (in circle diameters)
    sp = defaultdict(lambda: {"n": 0, "miss": 0, "along": []})
    for j in tgt:
        if j.category == "jump":
            b = round(j.dist_in / (2 * radius))
            sp[b]["n"] += 1
            sp[b]["miss"] += j.result == "miss"
            if j.along is not None:
                sp[b]["along"].append(j.along)
    s["spacing_buckets"] = {k: {"n": v["n"], "miss": v["miss"],
                                "along": mean(v["along"]) if v["along"] else 0.0}
                            for k, v in sorted(sp.items())}
    pe = [j.path_eff for j in tgt if j.path_eff]
    s["path_eff"] = median(pe) if len(pe) >= 10 else None
    # map descriptors
    beats = [bp for _, bp, u in bm.timing if u and bp > 0]
    s["bpm"] = (60000 / Counter(round(b, 3) for b in beats).most_common(1)[0][0] * rep.rate) if beats else 0
    s["ar"] = ar_eff(bm.ar, rep.mods, rep.rate)
    return s


def ar_eff(ar, mods, rate=None):
    """Effective AR; `rate` = the replay's real speed (lazer custom DT/HT rates), else derived from the mods."""
    if mods & 16: ar = min(10, ar * 1.4)
    if mods & 2: ar *= 0.5
    ms = 1800 - 120 * ar if ar < 5 else 1200 - 150 * (ar - 5)
    if not rate:
        rate = 1.5 if mods & (64 | 512) else 0.75 if mods & 256 else 1.0
    ms /= rate
    return (1800 - ms) / 120 if ms > 1200 else 5 + (1200 - ms) / 150


# ----------------------------------------------------------------------------
# Coaching rules -> practice plan
# ----------------------------------------------------------------------------


def coach(stats_list):
    """Aggregate several plays and produce prioritised advice."""
    agg = defaultdict(lambda: {"n": 0, "miss": 0, "err": [], "along": []})
    reasons = Counter()
    drift, single, eff, mean_errs = [], [], [], []
    fat = [0, 0]
    bpm_all = defaultdict(lambda: [0, 0])
    sp_all = defaultdict(lambda: [0, 0, []])
    for s in stats_list:
        for j in s["js"]:
            a = agg[j.category]
            a["n"] += 1
            a["miss"] += j.result == "miss"
            if j.error is not None:
                a["err"].append(j.error)
            if j.along is not None and j.category == "jump":
                a["along"].append(j.along)
        reasons.update(s["miss_reasons"])
        if s["stream_drift"] is not None: drift.append(s["stream_drift"])
        if s["single_tap_ratio"] is not None: single.append(s["single_tap_ratio"])
        if s["path_eff"]: eff.append(s["path_eff"])
        if s["n"] > 50: mean_errs.append(s["mean_err"])
        t = s["thirds"]
        if t[0]["n"] and t[2]["n"]:
            fat[0] += t[0]["miss"]; fat[1] += t[2]["miss"]
        for b, v in s["bpm_buckets"].items():
            bpm_all[b][0] += v["n"]; bpm_all[b][1] += v["miss"]
        for b, v in s["spacing_buckets"].items():
            sp_all[b][0] += v["n"]; sp_all[b][1] += v["miss"]

    tips = []  # (priority, title, detail)
    tot_miss = sum(reasons.values()) or 1
    aim_m = sum(v for k, v in reasons.items() if k.startswith("aim"))
    lost_m = reasons.get("lost / no tap (cursor elsewhere)", 0)
    tim_m = sum(v for k, v in reasons.items() if k.startswith("timing"))

    def rate(c):
        a = agg.get(c)
        return (a["miss"] / a["n"]) if a and a["n"] >= 15 else None

    jr, sr, br = rate("jump"), rate("stream"), rate("burst")
    all_err = [e for c, a in agg.items() if c != "spinner" for e in a["err"]]
    tot_ur = ur(all_err)
    if tot_ur > 220:
        tips.append((tot_ur / 8, "Accuracy / rhythm consistency (UR)",
                     f"Your overall UR is {tot_ur:.0f}. Players who comfortably pass 6–7★ typically sit around "
                     "100–180 UR on maps at their level; until this drops, harder maps mostly add misses.\n"
                     "  Drill: 15 min per session on maps 1–1.5★ BELOW your level with the goal of 95%+ accuracy, "
                     "not passing. Watch the hit-error bar; tap to the music, not to the approach circle. "
                     "Track UR here weekly — it should trend down."))
    if jr is not None and jr > 0.04:
        al = mean(agg["jump"]["along"]) if agg["jump"]["along"] else 0
        kind = ("you OVERSHOOT (land past the circle)" if al > 0.15 else
                "you UNDERSHOOT (stop short of the circle)" if al < -0.15 else
                "your landing error is mostly sideways, not distance")
        worst = [b for b, v in sp_all.items() if v[0] >= 8 and v[1] / v[0] > jr]
        sp_txt = f" Misses spike at spacing ≈ {min(worst)}+ circle-diameters." if worst else ""
        tips.append((jr * 100 + aim_m / tot_miss * 5, "Jump aim",
                     f"{jr:.1%} of jumps missed; {kind} (mean {al:+.2f} radii).{sp_txt}\n"
                     "  Drill: 1/2 jump maps at a BPM ~20 below the maps you're failing, same spacing, "
                     "until you FC-ish (≥95% hits); then raise BPM by 10. "
                     + ("Overshoot fix: lower sensitivity slightly or consciously 'snap and stop'. "
                        if al > 0.15 else "Undershoot fix: aim *through* the circle; try slightly higher sens. "
                        if al < -0.15 else "")))
    if sr is not None and sr > 0.03 or (drift and mean(drift) > 4):
        ceiling = None
        for b in sorted(bpm_all):
            n, m = bpm_all[b]
            if n >= 20 and m / n > 0.05:
                ceiling = b
                break
        d = mean(drift) if drift else 0
        tips.append(((sr or 0) * 100 + max(0, d), "Streams / speed",
                     f"Stream miss rate {sr or 0:.1%}; "
                     + (f"timing drifts {d:+.1f} ms from start to end of long streams "
                        f"({'falling behind → speed stamina' if d > 0 else 'rushing → you tap faster than the music'}). "
                        if drift else "")
                     + (f"Streams start breaking around {ceiling} BPM. " if ceiling else "")
                     + "\n  Drill: tapping-only practice at "
                     + (f"{max(100, ceiling - 20)}–{max(105, ceiling - 10)} BPM " if ceiling else "a BPM you can hold cleanly ")
                     + "(~10 BPM below your break point), 30–60 s bursts, rest equal time; "
                       "+5 BPM once UR stays stable to the end."))
    if single and mean(single) > 0.35:
        tips.append((30, "Alternate in streams",
                     f"{mean(single):.0%} of consecutive stream/burst taps use the same key — that caps your speed.\n"
                     "  Drill: slow streams (150–170 BPM) strictly alternating, start each stream on alternating keys too."))
    if mean_errs:
        me = mean(mean_errs)
        if abs(me) > 6:
            tips.append((abs(me), "Timing bias",
                         f"Average hit error {me:+.1f} ms ({'late' if me > 0 else 'early'}). "
                         + ("This average includes streams, where lateness usually means tapping speed, "
                            "not offset - don't move your global offset by this number. Check the pet's "
                            "Setup tab: it uses regular (non-stream) notes only and explains a careful calibration."
                            )))
    if fat[1] > fat[0] * 1.6 and fat[1] >= 5:
        tips.append((20, "Stamina / fatigue",
                     f"Last third of maps has {fat[1]} misses vs {fat[0]} in the first third.\n"
                     "  Drill: longer maps (3–5 min) one notch below your level; deathstream practice in short sets."))
    if eff and mean(eff) > 1.2:
        tips.append((15, "Cursor control",
                     f"On a typical jump your cursor travels {mean(eff):.2f}× the straight-line distance (wobble/curving).\n"
                     "  Drill: low-BPM, big-spacing maps focusing on straight, decisive movements."))
    if lost_m / tot_miss > 0.3 and lost_m >= 5:
        tips.append((25, "Reading",
                     f"{lost_m} misses where your cursor wasn't near the note and you didn't tap — usually a reading issue.\n"
                     "  Drill: play maps at your star level with AR +0.5 above normal, and replay the sections where you got lost."))
    if tim_m / tot_miss > 0.3 and tim_m >= 5:
        tips.append((18, "Rhythm / timing misses",
                     f"{tim_m} misses with the cursor on the circle — wrong timing, not aim.\n"
                     "  Drill: practise the confusing rhythms slowly (HT or a rate-changer), then at full speed."))
    tips.sort(key=lambda t: -t[0])
    return tips, agg, reasons, bpm_all, sp_all


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def fmt_play(s):
    r, bm = s["replay"], s["map"]
    w = s["windows"]
    lines = [
        f"\n=== {bm.name} +{r.mod_str}  ({r.player})",
        f"  game: {r.accuracy:.2%} acc, {r.misses} miss, {r.max_combo}x  |  "
        f"BPM {s['bpm']:.0f}, AR {s['ar']:.1f}, CS {bm.cs}, OD {bm.od}",
        f"  re-judged: {s['misses']} miss / {s['n']} objects (slider heads only), "
        f"UR {s['ur']:.0f}, mean error {s['mean_err']:+.1f} ms  (300 window ±{w[0]/r.rate:.0f} ms)",
    ]
    if s["cats"]:
        lines.append("  pattern      objs  miss  300%    UR   mean err  jump landing")
        for c, v in s["cats"].items():
            al = f"{v['along']:+.2f}r" if c == "jump" else ""
            lines.append(f"  {c:<11}{v['n']:>6}{v['miss']:>6}{v['p300']:>6.0%}{v['ur']:>6.0f}"
                         f"{v['mean_err']:>+9.1f}   {al}")
    if s["miss_reasons"]:
        lines.append("  misses: " + "; ".join(f"{n}× {k}" for k, n in s["miss_reasons"].most_common()))
    t = s["thirds"]
    lines.append(f"  by section (start/mid/end): misses {t[0]['miss']}/{t[1]['miss']}/{t[2]['miss']}, "
                 f"UR {t[0]['ur']:.0f}/{t[1]['ur']:.0f}/{t[2]['ur']:.0f}")
    if s["stream_drift"] is not None:
        lines.append(f"  stream drift (end − start of long streams): {s['stream_drift']:+.1f} ms")
    if s["single_tap_ratio"] is not None:
        lines.append(f"  same-key taps in streams/bursts: {s['single_tap_ratio']:.0%}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="osu! replay coach")
    ap.add_argument("replays", nargs="*", help=".osr files or folders (default: scan your osu! install)")
    ap.add_argument("--osu-dir", help="osu! data folder (lazer: %%APPDATA%%/osu, stable: install folder)")
    ap.add_argument("--player", help="only analyse replays by this player name")
    ap.add_argument("--last", type=int, default=15, help="how many most-recent replays to analyse")
    ap.add_argument("--min-objects", type=int, default=100)
    ap.add_argument("--out", default="osu_coach_report.txt", help="report file to write")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = Path(args.osu_dir) if args.osu_dir else (LAZER_DIR if LAZER_DIR.exists() else STABLE_DIR)
    if not root.exists():
        sys.exit("Couldn't find your osu! folder. osu!lazer is recommended (%APPDATA%/osu); for another "
                 "location pass --osu-dir \"D:/path/to/osu\".")
    files_root = root / "files" if (root / "files").exists() else root / "Songs" if (root / "Songs").exists() else root
    print(f"Indexing beatmaps in {files_root} (first run can take a minute)...", file=sys.stderr)
    cache = load_cache()
    want_scan = not args.replays
    osu_index, found = build_index(files_root, cache, want_replays=want_scan)
    if want_scan and (root / "Replays").exists():
        found += [str(p) for p in (root / "Replays").glob("*.osr")]
    if want_scan and (root / "exports").exists():
        found += [str(p) for p in (root / "exports").glob("*.osr")]
    save_cache(cache)

    paths = []
    for r in args.replays:
        p = Path(r)
        paths += [str(q) for q in p.rglob("*.osr")] if p.is_dir() else [str(p)]
    if want_scan:
        paths = found

    headers = []
    seen = set()
    for p in paths:
        try:
            h = parse_osr(p, frames=False)
        except Exception:
            continue
        if h.mode != 0:
            continue
        sig = (h.beatmap_md5, h.timestamp, h.score)
        if sig in seen:
            continue
        seen.add(sig)
        headers.append(h)
    if args.player:
        headers = [h for h in headers if h.player.lower() == args.player.lower()]
    elif want_scan and headers:
        # respect exclude_users / the configured or logged-in account; else the most common remaining name
        try:
            import coach_identity as _ci
            _ident = _ci.load_identity()
            _ex = set(_ident.get("exclude_names") or ())
            _want = (_ident.get("username") or _ci.game_username() or "").lower()
        except Exception:
            _ex, _want = set(), ""
        _ok = [h for h in headers if h.player.lower() not in _ex] or headers
        _names = {h.player.lower() for h in _ok}
        if _want and _want in _names:
            headers = [h for h in _ok if h.player.lower() == _want]
        else:
            me = Counter(h.player for h in _ok).most_common(1)[0][0]
            headers = [h for h in _ok if h.player == me]
    headers.sort(key=lambda h: -h.timestamp)

    stats = []
    skipped = 0
    for h in headers:
        if len(stats) >= args.last:
            break
        bp = osu_index.get(h.beatmap_md5)
        if not bp:
            skipped += 1
            continue
        try:
            bm = parse_osu(Path(bp).read_text("utf-8", "replace"))
            if len([o for o in bm.objects if o.kind != "spinner"]) < args.min_objects:
                continue
            rep = parse_osr(h.path)
            stats.append(summarize(rep, bm))
        except Exception as e:  # keep going on odd files
            print(f"  ! skipped {h.path}: {e}", file=sys.stderr)
    if not stats:
        sys.exit(f"No analysable replays found ({len(headers)} replays seen, {skipped} without a local beatmap). "
                 "Play some maps (or export replays) and try again.")

    out = [f"osu! coach — {len(stats)} replays analysed (most recent first)"]
    out += [fmt_play(s) for s in stats]
    tips, agg, reasons, bpm_all, sp_all = coach(stats)
    out.append("\n" + "=" * 72 + "\nOVERALL")
    for c, a in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        if c == "spinner":
            continue
        out.append(f"  {c:<11} {a['n']:>6} objs  miss {a['miss'] / max(1, a['n']):6.1%}  UR {ur(a['err']):4.0f}")
    if bpm_all:
        out.append("  stream BPM → miss%: " + ", ".join(
            f"{b}:{m / n:.0%}" for b, (n, m) in sorted(bpm_all.items()) if n >= 10))
    if sp_all:
        out.append("  jump spacing (circle diameters) → miss%: " + ", ".join(
            f"{b}:{v[1] / v[0]:.0%}" for b, v in sorted(sp_all.items()) if v[0] >= 10))
    out.append("\nWHAT TO PRACTISE NEXT (highest impact first)")
    if not tips:
        out.append("  Nothing stands out — you're consistent at this level. Move up ~0.3★ "
                   "(the challenge point: you should be passing ~70–85% of attempts).")
    for i, (_, title, detail) in enumerate(tips[:4], 1):
        out.append(f"  {i}. {title}: {detail}")
    best_acc = [s for s in stats if s["misses"] <= 3]
    out.append("\nDIFFICULTY TARGET")
    out.append(f"  You near-FC'd {len(best_acc)}/{len(stats)} of these plays. "
               + ("Too easy on average → push harder maps for most of your session."
                  if len(best_acc) / len(stats) > 0.6 else
                  "Mostly failing/choking → spend 60% of time ~0.5★ lower, 40% at your target level."
                  if len(best_acc) / len(stats) < 0.2 else
                  "Good mix — you're in the productive zone. Keep this level, rotate focus skills."))
    text = "\n".join(out)
    print(text)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"\n(report saved to {args.out})", file=sys.stderr)


if __name__ == "__main__":
    main()
