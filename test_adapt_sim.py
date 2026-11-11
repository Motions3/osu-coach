#!/usr/bin/env python3
"""
test_adapt_sim.py - exercises coach_adapt.py (CONTRACT_ADAPT.md) without touching the real state files.

  python test_adapt_sim.py            # real history day by day + synthetic scenarios
  python test_adapt_sim.py --quiet    # less output

Everything runs in temp folders (OSU_COACH_STATE_DIR / OSU_COACH_PP_DIR). The real caches next to this
script (.coach_replays.json, .coach_library.json, .osu_coach_cache.json, .coach_api_sr.json) are only read.

Part 1 replays the player's REAL replay history chronologically, day by day: a morning plan, then every
play of the day through update() (all of them are free play - the player wasn't following this coach yet),
printing plans (blocks, minutes, goals), level changes and a few debriefs, and asserting that plans and
goals change across days when results change.
Part 2 runs synthetic scenarios against the real library: improving player, struggling player (tilt),
fatigue, free play, a live fail without replay, live+replay dedupe, session_cmd done/skip/back.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

QUIET = "--quiet" in sys.argv
FAILS = []
ACC_PICKS = []


def say(*a):
    print(*a, flush=True)


def check(cond, msg):
    say(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def fresh_dirs():
    d = Path(tempfile.mkdtemp(prefix="adapt_sim_"))
    os.environ["OSU_COACH_STATE_DIR"] = str(d)
    os.environ["OSU_COACH_PP_DIR"] = str(d)
    if (HERE / ".coach_api_sr.json").exists():
        shutil.copy(HERE / ".coach_api_sr.json", d / ".coach_api_sr.json")
    return d


fresh_dirs()   # before the import, so nothing can resolve to the real folder
import coach_adapt as ca  # noqa: E402
import coach_engine as ce  # noqa: E402


# ----------------------------------------------------------------------------
# real data (read only)
# ----------------------------------------------------------------------------

def load_real():
    rc_all = json.loads((HERE / ".coach_replays.json").read_text("utf-8"))
    lib = json.loads((HERE / ".coach_library.json").read_text("utf-8"))["maps"]
    osu = json.loads((HERE / ".osu_coach_cache.json").read_text("utf-8"))["osu"]
    idx = {v: k for k, v in osu.items() if v}
    rc = rc_all["replays"]
    hdrs = [(p, e) for p, e in rc.items() if e.get("hdr") and e["hdr"]["mode"] == 0]
    mine = set()
    try:  # same player choice as the coach (config user / logged-in account / exclude_users)
        import coach_identity
        mine = set(coach_identity.resolve([e["hdr"] for _, e in hdrs], coach_identity.game_username())["names"])
    except Exception:
        mine = set()
    if not mine:
        newest = max(hdrs, key=lambda x: x[1]["hdr"]["ts"])[1]["hdr"]
        uid = newest.get("uid")
        mine = {e["hdr"]["player"] for _, e in hdrs if (uid and e["hdr"].get("uid") == uid)} or {newest["player"]}
    plays, hist = [], {}
    stale = rc_all.get("v") != ce.ENGINE_VER
    for p, e in hdrs:
        h = e["hdr"]
        if h["player"] not in mine or h.get("mbits", 0) & ce.ASSISTED_BITS:
            continue
        x = hist.setdefault(h["md5"], {"plays": 0, "best": None})
        x["plays"] += 1
        x["best"] = h["acc"] if x["best"] is None else max(x["best"], h["acc"])
        s = e.get("sum")
        if (stale or (s and "runs" not in s)) and h["md5"] in idx:
            try:  # cache from an older engine: re-judge in memory (the real cache is not written)
                s = ce.compact_summary(p, h, idx[h["md5"]])
            except Exception:
                s = None
        if s and s["n"] >= 50:
            plays.append(s)
    plays.sort(key=lambda s: s["ts"])
    return plays, hist, lib, idx, sorted(mine)


def history_until(plays, ts):
    h = {}
    for s in plays:
        if s["ts"] >= ts:
            break
        x = h.setdefault(s["md5"], {"plays": 0, "best": None})
        x["plays"] += 1
        x["best"] = s["acc"] if x["best"] is None else max(x["best"], s["acc"])
    return h


def ctx_for(plays_before, lib):
    recent = sorted(plays_before, key=lambda s: -s["ts"])[:40]
    st, ex = ce.profile(recent, sorted(plays_before, key=lambda s: -s["ts"]), lib) if len(recent) >= 5 else (None, {})
    focus = [f["id"] for f in ce.make_focus(st, ex)] if st else []
    return {"stats": st or {}, "ex": ex or {}, "current_stars": 4.1, "typ_den": (ex or {}).get("typ_den") or 3.5,
            "focus_ids": focus}


def plan_sig(out):
    return [(s["block"], s["minutes"], s["goal"]) for s in out["session"]["steps"]]


# ----------------------------------------------------------------------------
# part 1: real history, day by day
# ----------------------------------------------------------------------------

def part1(plays, lib, idx):
    say("\n=== PART 1: your real replay history, day by day (all of it was free play) ===")
    fresh_dirs()
    days = sorted({ca.day_of(s["ts"]) for s in plays})
    boot_days = 2
    say(f"{len(plays)} plays on {len(days)} days ({days[0]} .. {days[-1]}); first {boot_days} days = bootstrap history")
    sigs, levels_by_day, goals_by_day = [], [], []
    shown = 0
    t_max = 0.0
    for di, day in enumerate(days[boot_days:]):
        t0 = ca.day_start_ts(day)
        before = [s for s in plays if s["ts"] < t0]
        ctx = ctx_for(before, lib)
        hist = history_until(plays, t0 + 1)
        out = ca.update(before, lib, idx, hist, ctx, now=t0 + 60, live=[])
        sig = plan_sig(out)
        lv = {t: out["tracks"][t]["level"] for t in ca.TRACKS}
        sigs.append(sig)
        levels_by_day.append(lv)
        acc_st = next((s for s in out["session"]["steps"] if s["track"] == "accuracy" and s.get("pick_md5")), None)
        ACC_PICKS.append((day, next((r["od"] for r in out["recommendations"]
                                     if acc_st and r["id"] == acc_st["pick_md5"]), None)))
        goals_by_day.append([g for _, _, g in sig])
        say(f"\n--- {day} morning plan ---")
        say("  levels: " + ", ".join(f"{t} {out['tracks'][t]['level_txt']} ({out['tracks'][t]['trend']})" for t in ca.TRACKS))
        for c in out["changes"][:6]:
            say("  change: " + c)
        for s in out["session"]["steps"]:
            pick = ""
            if s.get("pick_md5"):
                f = lib.get(s["pick_md5"]) or {}
                pick = f" [{ca.short_name(ca.map_name(f), 28)} {ca.rate_txt(s['rate'] or 1)}]"
            say(f"  {s['minutes']:>3} min  {s['block']:<18}{pick}" + ("" if QUIET else f"  goal: {s['goal'][:110]}"))
        todays = [s for s in plays if t0 <= s["ts"] < t0 + 86400]
        last = None
        for s in todays:
            now = s["ts"] + 2
            t = time.perf_counter()
            last = ca.update([p for p in plays if p["ts"] <= now], lib, idx, history_until(plays, now + 1), ctx,
                             now=now, live=[])
            t_max = max(t_max, time.perf_counter() - t)
        if last and last["debriefs_today"]:
            say(f"  {len(todays)} plays today; sample debriefs:")
            for d in last["debriefs_today"][:2 if QUIET else 3]:
                say(f"    [{d['verdict']}] {d['headline']}")
                for o in d["observations"]:
                    say(f"       - {o}")
                if d.get("compare"):
                    say(f"       vs {d['compare']['vs']}: " + ", ".join(f"{k} {v}" for k, v in d["compare"].items() if k != "vs"))
                say(f"       next: {d['next_action']['text']}")
                shown += 1
    say("\nPart 1 checks:")
    distinct = len({json.dumps(s) for s in sigs})
    check(distinct >= max(2, len(sigs) // 2), f"plans differ across days ({distinct} distinct plans in {len(sigs)} days)")
    moved = sum(1 for a, b in zip(levels_by_day, levels_by_day[1:]) if a != b)
    check(moved >= 1, f"levels moved on {moved} of {len(levels_by_day) - 1} day changes")
    # where levels changed, the goal text changed with them
    ok = True
    for (la, lb), (ga, gb) in zip(zip(levels_by_day, levels_by_day[1:]), zip(goals_by_day, goals_by_day[1:])):
        if la["accuracy"] != lb["accuracy"] and not any(f"OD {lb['accuracy']:.1f}" in g for g in gb):
            ok = False
    check(ok, "accuracy goals are rewritten from the new level whenever it moves")
    # no track moves more than one step from one morning to the next (session + morning moves together)
    big = [(t, a[t], b[t]) for a, b in zip(levels_by_day, levels_by_day[1:]) for t in ca.TRACKS
           if abs(b[t] - a[t]) > ca.TRACKS[t]["step"] + 1e-6]
    check(not big, f"no track moves more than one step per day {big[:4]}")
    # no flip-flopping: up-then-down (or down-then-up) within 3 days at most twice over the whole history
    rev = 0
    for t in ca.TRACKS:
        mv = [(i, (b[t] > a[t]) - (b[t] < a[t])) for i, (a, b) in enumerate(zip(levels_by_day, levels_by_day[1:]))
              if abs(b[t] - a[t]) > 1e-9]
        rev += sum(1 for (i, x), (j, y) in zip(mv, mv[1:]) if x != y and j - i <= 3)
    check(rev <= 2, f"levels don't bounce (reversals within 3 days: {rev})")
    # the accuracy picks are hard but doable: with his own recent URs, a good share of the picks should be
    # passable by the UR model (95% needs UR <= ur95(OD))
    probs = []
    for day, od in ACC_PICKS:
        t0 = ca.day_start_ts(day)
        urs = [s["ur"] for s in plays if t0 - 14 * 86400 <= s["ts"] < t0 and s.get("ur")][-60:]
        if not urs or od is None:
            continue
        probs.append(sum(1 for u in urs if u <= ca.ur95(od)) / len(urs))
    pr = sum(probs) / len(probs) if probs else 0
    say(f"  accuracy picks (day, OD): {ACC_PICKS}")
    check(pr >= 0.3, f"accuracy picks passable by the UR model on {pr:.0%} of tries (>= 30%)")
    check(t_max < 3.0, f"slowest update() for a new attempt: {t_max:.2f}s (< 3 s)")
    return levels_by_day


# ----------------------------------------------------------------------------
# synthetic attempts
# ----------------------------------------------------------------------------

def synth_summary(lib, md5, rate, ts, acc=0.95, stream_miss=0.0, drift=5.0, jump_miss=0.0, ur=250.0,
                  mean_err=3.0, last_third=1.0, other_miss=0, seed=0):
    """A replay summary (coach_engine.compact_summary shape) with chosen quality."""
    rng = random.Random(seed)
    f = lib[md5]
    n = f["n"]
    L = f["len"] * 1000
    sn = round(n * f["sf"]) if f.get("sf", 0) >= 0.02 and f.get("sb", 0) >= 100 else 0
    jn = round(n * f.get("jf", 0))
    on = max(1, n - sn - jn)
    sm, jm = round(sn * stream_miss), round(jn * jump_miss)
    ml = []
    runs = []
    if sn:
        k = max(1, min(6, f.get("sc") or 1))
        per = max(7, sn // k)
        for i in range(k):
            st = int(L * (i + 1) / (k + 1))
            m = sm // k + (1 if i < sm % k else 0)
            runs.append([st, st + per * 80, per, round(f["sb"] * rate), m, drift, mean_err + drift / 2])
            for q in range(m):
                ml.append([st + 80 * (per // 2 + q), "s", "T", round(f["sb"] * rate, 1)])
    for q in range(jm):
        ml.append([int(L * (0.55 + 0.1 * q / max(1, jm))), "j", "A", round((f.get("jv75") or 1) * rate, 2)])
    for q in range(other_miss):
        ml.append([int(L * rng.random()), "o", "T", 0.0])
    ml.sort()
    third = on // 3
    first_m = max(0, round(other_miss / 3))
    thc = {"o": [third, first_m, third, round(max(first_m, 1) * last_third) if last_third > 1.01 else first_m]}
    seg = []
    nseg = max(3, int(L // 10000))
    sd = ur / 10
    for i in range(nseg):
        c = n // nseg
        bad = round(c * (1 - acc) * 2.2)
        seg.append([i * 10, c, 0, bad, c, round(c * mean_err, 1), round(c * (sd * sd + mean_err ** 2))])
    bb = {str(int(f["sb"] * rate // 10 * 10)): [sn, sm]} if sn else {}
    cats = {"o": [on, other_miss, on, 0.0, 0.0]}
    if sn:
        cats["s"] = [sn, sm, sn - sm, 0.0, 0.0]
    if jn:
        cats["j"] = [jn, jm, jn - jm, 0.0, 0.0]
    mis = sm + jm + other_miss
    return {"ts": int(ts), "md5": md5, "mods": ca.rate_mods(rate), "rate": rate, "acc": acc, "misses": mis,
            "combo": 100, "rj_misses": mis, "n": n, "ur": ur, "mean_err": mean_err, "e": [n, 0, 0],
            "cats": cats, "thirds": [0, 0, 0, n // 3, n // 3, n // 3], "thc": thc, "drift": drift if sn else None,
            "same": 0, "alt": 10, "bpm_b": bb, "sp_b": {}, "vel_b": {}, "jal": [0, 0], "reasons": {},
            "miss_list": ml, "runs": runs, "seg": seg, "n300": int(n * acc),
            "ar": ca.ar_at(f["ar"], rate), "od": ca.od_at(f["od"], rate), "cs": f["cs"], "bpm": f["bpm"] * rate,
            "len": round(f["len"] / rate), "nobj": n, "name": ca.map_name(f)}


GOOD = {"accuracy": dict(acc=0.965, ur=210), "speed": dict(acc=0.93, stream_miss=0.01, drift=4, ur=240),
        "aim": dict(acc=0.93, jump_miss=0.01, ur=240), "push": dict(acc=0.9, other_miss=2, ur=260),
        "stamina": dict(acc=0.93, ur=240), "reading": dict(acc=0.93, ur=240), "warmup": dict(acc=0.96, ur=200),
        "benchmark": dict(acc=0.94, ur=230)}
BAD = {"accuracy": dict(acc=0.86, ur=380, mean_err=14), "speed": dict(acc=0.8, stream_miss=0.15, drift=30, ur=390),
       "aim": dict(acc=0.8, jump_miss=0.09, ur=380), "push": dict(acc=0.7, other_miss=40, ur=400),
       "stamina": dict(acc=0.82, last_third=3.0, other_miss=12, ur=380),
       "reading": dict(acc=0.85, ur=360), "warmup": dict(acc=0.9, ur=300), "benchmark": dict(acc=0.85, ur=360)}


class Sim:
    """A player following (or not) the card, in a temp state dir, on top of the real history."""

    def __init__(self, plays, lib, idx, start_day):
        self.dir = fresh_dirs()
        self.lib, self.idx = lib, idx
        self.base = [s for s in plays if s["ts"] < ca.day_start_ts(start_day)]
        self.extra = []
        self.live = []
        self.day = start_day
        self.now = ca.day_start_ts(start_day) + 3600 * 14
        self.ctx = ctx_for(self.base, lib)
        self.out = None

    def hist(self):
        return history_until(sorted(self.base + self.extra, key=lambda s: s["ts"]), self.now + 1)

    def run(self):
        t = time.perf_counter()
        self.out = ca.update(self.base + self.extra, self.lib, self.idx, self.hist(), self.ctx, now=self.now,
                             live=self.live)
        self.dt = time.perf_counter() - t
        return self.out

    def current(self):
        c = (self.out or self.run())["session"]["current"]
        return c

    def play_pick(self, quality, gap=150, rate=None, md5=None):
        c = self.current()
        if c and c["track"] == "break":
            self.now += 330          # take the break
            c = self.run()["session"]["current"]
        if c and not c.get("map") and c["track"] not in ("review",):
            self.now += 5     # a step without a local map: press Skip like the player would
            (self.dir / "session_cmd.json").write_text(json.dumps({"cmd": "skip", "ts": self.now}), "utf-8")
            c = self.run()["session"]["current"]
        if not c or not c.get("map"):
            return None
        md5 = md5 or c["map"]["id"]
        r = rate or c["rate"] or 1.0
        f = self.lib[md5]
        self.now += f["len"] / r + gap
        q = dict(quality.get(c["track"], quality.get("accuracy")))
        self.extra.append(synth_summary(self.lib, md5, r, self.now, seed=len(self.extra), **q))
        return self.run()

    def next_day(self):
        self.day = (datetime.fromisoformat(self.day) + timedelta(days=1)).date().isoformat()
        self.now = ca.day_start_ts(self.day) + 3600 * 14
        return self.run()


def show_debrief(d, indent="    "):
    say(f"{indent}[{d['verdict']}] {d['headline']}")
    for o in d["observations"]:
        say(f"{indent}   - {o}")
    na = d["next_action"]
    say(f"{indent}   next ({na['type']}): {na['text']}")
    if d.get("level_change"):
        lc = d["level_change"]
        say(f"{indent}   level: {lc['track']} {lc['from']} -> {lc['to']}")


def skip_to(sim, track):
    """Press 'skip' until the current step is `track` (session_cmd.json, consumed by update())."""
    for _ in range(12):
        c = sim.current()
        if not c or c["track"] == track:
            return c
        sim.now += 5
        (sim.dir / "session_cmd.json").write_text(json.dumps({"cmd": "skip", "ts": sim.now}), "utf-8")
        sim.run()
    return sim.current()


# ----------------------------------------------------------------------------
# part 2: scenarios
# ----------------------------------------------------------------------------

def part2(plays, lib, idx):
    say("\n=== PART 2: synthetic scenarios (real library, real history as background) ===")
    start = (datetime.fromtimestamp(max(s["ts"] for s in plays)) + timedelta(days=2)).date().isoformat()

    # --- improving player
    say("\n[improving player: passes every step attempt]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    lv0 = {t: o["tracks"][t]["level"] for t in ca.TRACKS}
    plan0 = plan_sig(o)
    for i in range(30):
        o = sim.play_pick(GOOD)
        if o is None or o["session"].get("stopped"):
            break
        if i < 8 or (o["last_debrief"] or {}).get("level_change"):
            show_debrief(o["last_debrief"])
        if o["last_debrief"]["next_action"]["type"] == "break":
            sim.now += 330
    lv_end = {t: o["tracks"][t]["level"] for t in ca.TRACKS}
    o2 = sim.next_day()
    lv1 = {t: o2["tracks"][t]["level"] for t in ca.TRACKS}
    say("  day 1 levels: " + ", ".join(f"{t} {lv0[t]}" for t in ca.TRACKS))
    say("  day 2 levels: " + ", ".join(f"{t} {lv1[t]}" for t in ca.TRACKS))
    for c in o2["changes"]:
        say("  change: " + c)
    ups = [t for t in ca.TRACKS if lv1[t] > lv0[t]]
    check(len(ups) >= 2, f"levels rose on {ups}")
    check(plan_sig(o2) != plan0, "day-2 plan differs from day 1")
    check(any("->" in c for c in o2["changes"]), "'changes' explains the level moves")
    acc_goal = next((s["goal"] for s in o2["session"]["steps"] if s["track"] == "accuracy"), "")
    check(f"OD {lv1['accuracy']:.1f}" in acc_goal, f"accuracy goal uses the new level: {acc_goal[:70]}")

    # --- struggling player
    say("\n[struggling player: misses the goal every time]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    lv0 = {t: o["tracks"][t]["level"] for t in ca.TRACKS}
    skip_to(sim, "accuracy")
    types = []
    tilt_seen = False
    for i in range(5):
        o = sim.play_pick(BAD)
        d = o["last_debrief"]
        show_debrief(d)
        types.append(d["next_action"]["type"])
        if o["session"]["tilt"]["flag"] and not tilt_seen:
            tilt_seen = True
            tilt_text, tilt_type = d["next_action"]["text"], d["next_action"]["type"]
    check(tilt_seen, "tilt flagged after 3 misses of the goal in a row")
    check(tilt_seen and ("Easier" in tilt_text or tilt_type == "slower") and "frustration" in tilt_text,
          "tilt -> easier map with a calm note")
    acc_now = o["tracks"]["accuracy"]["level"]
    check(abs((lv0["accuracy"] - acc_now) - ca.TRACKS["accuracy"]["step"]) < 1e-6,
          f"bad session drops accuracy by exactly one step ({lv0['accuracy']} -> {acc_now})")
    o2 = sim.next_day()
    lv1 = {t: o2["tracks"][t]["level"] for t in ca.TRACKS}
    check(lv1["accuracy"] < lv0["accuracy"] and lv1["speed"] < lv0["speed"], "next day: accuracy and speed levels lower")
    for c in o2["changes"][:4]:
        say("  change: " + c)

    # --- fatigue
    say("\n[fatigue: UR climbs through the session]")
    sim = Sim(plays, lib, idx, start)
    sim.run()
    got_break = False
    for i in range(9):
        c = sim.current()
        q = {k: dict(v, ur=230 + 25 * i, acc=0.95 - 0.004 * i) for k, v in GOOD.items()}
        o = sim.play_pick(q, gap=30)
        if o["last_debrief"]["next_action"]["type"] in ("break", "stop") and o["session"]["fatigue"]["flag"]:
            got_break = True
            show_debrief(o["last_debrief"])
            break
    check(got_break, "fatigue -> break suggested")

    # --- free play
    say("\n[free play: a map that isn't the step's pick]")
    sim = Sim(plays, lib, idx, start)
    c = sim.current()
    other = next(m for m, f in lib.items() if ca._std(f) and f["n"] >= 200 and m != c["map"]["id"]
                 and all(m not in (s.get("pick_md5") or "") for s in sim.out["session"]["steps"]))
    sim.now += 200
    sim.extra.append(synth_summary(lib, other, 1.0, sim.now, acc=0.88, other_miss=6, ur=320))
    o = sim.run()
    d = o["last_debrief"]
    show_debrief(d)
    check(d["verdict"] == "free-play" and ca.FREE_NOTE in d["next_action"]["text"], "free play debriefed, never ignored")
    check(o["session"]["steps"][o["session"]["step_index"]]["attempts"] == 0, "free play doesn't count for the step")

    # --- live fail without a replay
    say("\n[live fail on the pick, no replay]")
    sim = Sim(plays, lib, idx, start)
    c = skip_to(sim, "speed") or sim.current()
    md5, r = c["map"]["id"], c["rate"] or 1.0
    f = lib[md5]
    sim.now += 70
    sim.live.append({"ts": sim.now, "md5": md5, "mods": ca.rate_mods(r), "rate": r, "stars": None,
                     "artist": f["a"], "title": f["t"], "version": f["v"], "outcome": "failed",
                     "time_ms": int(f["len"] * 1000 * 0.4), "first_ms": 0, "last_ms": int(f["len"] * 1000),
                     "progress": 0.4, "acc": 81.5, "hits": {"300": 200, "100": 40, "50": 5, "0": 18}, "max_combo": 90,
                     "ur": 360.0, "hit_errors": [12, -3, 25, 30, 8] * 20,
                     "miss_times_ms": [int(f["len"] * 1000 * x) for x in (0.2, 0.21, 0.22, 0.3, 0.35, 0.38, 0.39, 0.4)]})
    o = sim.run()
    d = o["last_debrief"]
    show_debrief(d)
    check(d["source"] == "live" and d["verdict"] == "fail", "live fail judged as a fail of the step")
    check(any("Failed at" in x for x in d["observations"]), "debrief says where it failed (m:ss)")

    # --- live + replay dedupe
    say("\n[live 'passed' + replay of the same play -> one attempt]")
    sim = Sim(plays, lib, idx, start)
    c = skip_to(sim, "accuracy")
    md5 = c["map"]["id"]
    f = lib[md5]
    sim.now += f["len"] + 60
    sim.live.append({"ts": sim.now, "md5": md5, "mods": "NM", "rate": 1.0, "stars": 3.33, "artist": f["a"],
                     "title": f["t"], "version": f["v"], "outcome": "passed", "time_ms": f["len"] * 1000,
                     "first_ms": 0, "last_ms": f["len"] * 1000, "progress": 1.0, "acc": 96.4,
                     "hits": {"300": f["n"] - 20, "100": 20, "50": 0, "0": 0}, "ur": 205, "hit_errors": [1, 2] * 50,
                     "miss_times_ms": []})
    o1 = sim.run()
    d1 = o1["last_debrief"]
    say(f"    before the replay lands: source={d1['source']} verdict={d1['verdict']}")
    sim.extra.append(synth_summary(lib, md5, 1.0, sim.now + 3, **GOOD["accuracy"]))
    sim.now += 4
    o2 = sim.run()
    d2 = o2["last_debrief"]
    stp = next(s for s in o2["session"]["steps"] if s["track"] == "accuracy")
    say(f"    after: source={d2['source']} verdict={d2['verdict']} step attempts={stp['attempts']} "
        f"debriefs today={len(o2['debriefs_today'])}")
    check(d2["source"] == "replay" and stp["attempts"] == 1, "deduped: one attempt, replay data preferred")

    # --- session_cmd
    say("\n[session_cmd.json done / skip / back]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    i0 = o["session"]["step_index"]
    for cmd, want in (("done", i0 + 1), ("skip", i0 + 2), ("back", i0 + 1)):
        sim.now += 5
        (sim.dir / "session_cmd.json").write_text(json.dumps({"cmd": cmd, "ts": sim.now}), "utf-8")
        o = sim.run()
        st = o["session"]["steps"]
        say(f"    {cmd}: step_index {o['session']['step_index']} ({st[o['session']['step_index']]['block']}), states "
            + " ".join(s["state"][0] for s in st))
        check(o["session"]["step_index"] == want, f"'{cmd}' -> step {want}")
        check(not (sim.dir / "session_cmd.json").exists(), f"'{cmd}' consumed")
    o = sim.run()
    check(o["session"]["step_index"] == i0 + 1, "re-running does not re-apply consumed commands")
    check(o["session"]["steps"][i0]["state"] == "done" and o["session"]["steps"][i0 + 1]["state"] == "active",
          "states after done/skip/back: first done, second active again")

    # --- weekly check-up
    say("\n[weekly check-up]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    has = any(s["track"] == "benchmark" for s in o["session"]["steps"])
    check(has, "first plan includes the check-up block")
    days_with = 0
    for k in range(8):
        c = sim.current()
        if c and c["track"] == "benchmark":
            lv_b = {t: sim.out["tracks"][t]["level"] for t in ca.TRACKS}
            for k2 in range(6):
                if (sim.current() or {}).get("track") != "benchmark":
                    break
                o = sim.play_pick(BAD if k2 % 2 else GOOD)
                d = o["last_debrief"]
                if k2 < 2:
                    show_debrief(d)
                check(d["verdict"] == "done" and not d.get("level_change"), "check-up attempt is recorded, not judged")
            check({t: o["tracks"][t]["level"] for t in ca.TRACKS} == lv_b, "check-up never moves a level")
        o = sim.next_day()
        if any(s["track"] == "benchmark" for s in o["session"]["steps"]):
            days_with += 1
            say(f"    check-up again on {o['session']['date']}")
    check(days_with == 1, f"check-up returns once a week ({days_with} times in the next 8 days)")

    # --- warm-up is never judged
    say("\n[warm-up attempts never move a level]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    lv0 = {t: o["tracks"][t]["level"] for t in ca.TRACKS}
    c = sim.current()
    check(c["track"] == "warmup", "first step is the warm-up")
    o = sim.play_pick({"warmup": dict(acc=0.7, other_miss=25, ur=420)})
    d = o["last_debrief"]
    show_debrief(d)
    check(not d.get("level_change") and {t: o["tracks"][t]["level"] for t in ca.TRACKS} == lv0,
          "bad warm-up play: no level moved")
    check(o["session"]["tilt"]["fails_in_row"] == 0, "bad warm-up play: no tilt count")
    o2 = sim.next_day()
    check(o2["tracks"]["accuracy"]["level"] >= lv0["accuracy"], "the morning review doesn't re-judge the warm-up play")

    # --- free-play fails on hard maps never cause tilt
    say("\n[free-play fails far above the push level: no tilt]")
    sim = Sim(plays, lib, idx, start)
    o = sim.run()
    pl = o["tracks"]["push"]["level"]
    stars = ca.Stars(lib)
    hard = [m for m, f in lib.items() if ca._std(f) and f["n"] >= 200
            and (stars.get(m, "NM", 1.0)[0] or 0) > pl + 0.8][:4]
    for m in hard:
        f = lib[m]
        sim.now += 80
        sim.live.append({"ts": sim.now, "md5": m, "mods": "NM", "rate": 1.0, "stars": None, "artist": f["a"],
                         "title": f["t"], "version": f["v"], "outcome": "failed", "end": "fail_quit",
                         "time_ms": int(f["len"] * 400), "first_ms": 0, "last_ms": int(f["len"] * 1000),
                         "progress": 0.4, "acc": 70.0, "hits": {"300": 150, "100": 40, "50": 5, "0": 30},
                         "max_combo": 40, "ur": 400.0, "hit_errors": [], "miss_times_ms": []})
    o = sim.run()
    check(len(hard) >= 3 and o["session"]["tilt"]["fails_in_row"] == 0 and not o["session"]["tilt"]["flag"],
          f"{len(hard)} free fails above the push level -> fails_in_row {o['session']['tilt']['fails_in_row']}")

    # --- a pass on the easier map returns him to his level
    say("\n[after an easier map: back to the level]")
    sim = Sim(plays, lib, idx, start)
    sim.run()
    skip_to(sim, "accuracy")
    o = sim.play_pick(BAD, gap=20)            # level drops one step, easier map
    show_debrief(o["last_debrief"])
    o = sim.play_pick(BAD, gap=20)            # drop capped: easier map below the (new) level
    show_debrief(o["last_debrief"])
    o = sim.play_pick(GOOD, gap=20)           # clean on the easier map
    d = o["last_debrief"]
    show_debrief(d)
    na = d["next_action"]
    check("below your" in d["headline"], f"headline is honest about a pass below the level: {d['headline']}")
    check(na["type"] in ("next_map", "faster") and "level" in na["text"],
          f"after a pass below the level the next action goes back up: {na['text'][:70]}")

    # --- replay watched right after the play is not a second pass
    say("\n[own replay watched after a pass: not counted twice]")
    sim = Sim(plays, lib, idx, start)
    c = skip_to(sim, "accuracy")
    md5 = c["map"]["id"]
    f = lib[md5]
    rec = {"md5": md5, "mods": "NM", "rate": 1.0, "stars": None, "artist": f["a"], "title": f["t"],
           "version": f["v"], "outcome": "passed", "end": "results", "time_ms": f["len"] * 1000, "first_ms": 0,
           "last_ms": f["len"] * 1000, "progress": 1.0, "acc": 95.6,
           "hits": {"300": f["n"] - 18, "100": 18, "50": 0, "0": 0}, "max_combo": f["n"], "ur": 230,
           "hit_errors": [1, 2] * 50, "miss_times_ms": []}
    sim.now += f["len"] + 30
    sim.live.append(dict(rec, ts=sim.now))
    sim.now += f["len"] + 20
    sim.live.append(dict(rec, ts=sim.now))
    o = sim.run()
    stp = next(s for s in o["session"]["steps"] if s["track"] == "accuracy")
    check(stp["attempts"] == 1, f"replay watch dropped (step attempts {stp['attempts']})")

    # --- early retry / cut-off attempts are not misses of the goal
    say("\n[early retry and tosu lost: not judged]")
    sim = Sim(plays, lib, idx, start)
    c = skip_to(sim, "accuracy")
    md5 = c["map"]["id"]
    f = lib[md5]
    base = {"md5": md5, "mods": "NM", "rate": 1.0, "stars": None, "artist": f["a"], "title": f["t"],
            "version": f["v"], "outcome": "quit", "first_ms": 0, "last_ms": f["len"] * 1000, "acc": 90.0,
            "hits": {"300": 30, "100": 5, "50": 0, "0": 2}, "max_combo": 20, "ur": 300, "hit_errors": [],
            "miss_times_ms": [3000, 5000]}
    sim.now += 40
    sim.live.append(dict(base, ts=sim.now, end="retry", time_ms=12000, progress=0.08))
    sim.now += 120
    sim.live.append(dict(base, ts=sim.now, end="tosu_lost", time_ms=60000, progress=0.5))
    o = sim.run()
    stp = next(s for s in o["session"]["steps"] if s["track"] == "accuracy")
    d = o["last_debrief"]
    show_debrief(d)
    check(stp["attempts"] == 0 and o["session"]["tilt"]["fails_in_row"] == 0 and d["verdict"] == "not-counted",
          "early retry dropped, tosu-lost attempt recorded as not counted")
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    need = [n for n in (".coach_replays.json", ".coach_library.json", ".osu_coach_cache.json") if not (HERE / n).exists()]
    if need:
        say("skipped: this test replays your own coach history and needs " + ", ".join(need)
            + " (run coach_engine.py once after playing a few maps)")
        sys.exit(0)
    plays, hist, lib, idx, mine = load_real()
    say(f"player: {mine}, {len(plays)} judged plays")
    part1(plays, lib, idx)
    part2(plays, lib, idx)
    say("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED:\n  " + "\n  ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
