#!/usr/bin/env python3
"""
coach_knowledge.py - static coaching content + the logic that picks it for one player.

Owned by the knowledge builder (see CONTRACT_PP.md). Standard library only. Nothing here
touches the network, the osu! process or writes anywhere: the read_* helpers open the
osu! config files read-only and never return secrets (game.ini holds a login token, which
is never read into memory beyond the line filter).

Evidence labels used everywhere:
  "research"            a real motor-learning study supports the principle (cited in "source");
                        the osu!-specific drill built on it is still practice, not a lab result
  "community consensus" widely taught osu! practice, no formal study
  "coach heuristic"     this coach's rule of thumb; numbers are starting points, not facts

Public API (used by coach_engine.py):
  read_ini(path, keys)              -> {key: value} for the requested keys only
  read_input_config(path)           -> summary of lazer's input.json (mouse/tablet handlers)
  BANDS, band_targets(stars)
  estimate_stars(ctx)               -> (stars, basis) from stats when no SR model exists
  build_roadmap(ctx, measures)      -> CONTRACT_PP "roadmap"
  detect_return(timestamps)         -> (return_ts, why)
  detect_phase(ctx, roadmap, ...)   -> CONTRACT_PP "phase"
  rank_techniques(ctx)              -> CONTRACT_PP "techniques"
  setup_checks(ctx, game, fw, inp)  -> CONTRACT_PP "setup_checks"
  mod_note(rec, ctx)                -> one sentence explaining the mod/rate of a recommendation
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

EVIDENCE = ("research", "community consensus", "coach heuristic")
CATEGORIES = ("tapping", "timing", "aim", "reading", "stamina", "setup", "mindset")

RESHARPEN_DAYS = 21          # coach heuristic: returning players regain most "rust" in 2-4 weeks
RETURN_GAP_DAYS = 90         # a gap this long in the replay history counts as a break


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------


def _num(x):
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt(x, f="{:.0f}", none="?"):
    x = _num(x)
    return none if x is None else f.format(x)


def read_ini(path, keys=None):
    """Read 'Key = Value' lines from an osu! ini file, read-only. Only `keys` are returned
    (case-insensitive) so secrets such as the login Token never leave this function."""
    want = {k.lower(): k for k in keys} if keys else None
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                k, sep, v = line.partition("=")
                if not sep:
                    continue
                k = k.strip()
                kl = k.lower()
                if kl in ("token", "password") or "token" in kl:
                    continue
                if want is None or kl in want:
                    out[want[kl] if want else k] = v.strip()   # last occurrence wins (lazer does the same)
    except OSError:
        return None
    return out


def read_input_config(path):
    """Summarise lazer's input.json (read-only): mouse relative mode + sensitivity, tablet handler."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    out = {"mouse": None, "tablet": None, "pen": None}
    for h in (d.get("InputHandlers") or []) if isinstance(d, dict) else []:
        if not isinstance(h, dict):
            continue
        t = str(h.get("$type", ""))
        if "MouseHandler" in t:
            out["mouse"] = {"enabled": bool(h.get("Enabled")), "relative": h.get("UseRelativeMode"),
                            "sensitivity": _num(h.get("Sensitivity"))}
        elif "TabletDriverHandler" in t or ".Tablet." in t:
            tab = h.get("Tablet") or {}
            area = h.get("AreaSize") or {}
            out["tablet"] = {"enabled": bool(h.get("Enabled")), "name": tab.get("Name"),
                             "size": [_num((tab.get("Size") or {}).get("x")), _num((tab.get("Size") or {}).get("y"))],
                             "area": [_num(area.get("x")), _num(area.get("y"))],
                             "rotation": _num(h.get("Rotation"))}
        elif "PenHandler" in t:
            out["pen"] = {"enabled": bool(h.get("Enabled"))}
    return out


# ----------------------------------------------------------------------------
# roadmap bands (all numbers are coach heuristics, measured with THIS coach's replay judge)
# ----------------------------------------------------------------------------
# ur       pooled unstable rate on maps of that star rating (UR = 10 x std-dev of hit error, ms)
# acc      median accuracy on maps of that star rating
# stream   highest 1/4 stream BPM (runs of 7+ notes) held with a normal miss rate
# aim      jump speed (osu! px per ms) below which jump misses stay under ~5%
#          (for calibration: a typical 4.5* anime Insane peaks around 0.8-1.2 px/ms, a typical
#          Extra around 1.6; 7* jump maps commonly need 2+)
# nfc      share of plays at that level with 3 misses or fewer
# stamina  last-third misses vs what the note types there predict (1.0 = no fatigue), 5.0* and up
BANDS = [
    {"stars": 4.0, "ur": 220, "acc": 0.94, "stream": 150, "aim": 0.90, "nfc": 0.40, "stamina": None,
     "focus": "Timing first: relaxed, accurate tapping on maps you know you can read. Get the "
              "hit-error bar centred and your UR down before chasing harder maps.",
     "typical_maps": "TV-size anime Insanes and hard Hards: 160-180 BPM, AR 8.5-9, OD 7-8, short "
                     "3-5 note bursts, jumps of 2-3 circle spacings."},
    {"stars": 4.5, "ur": 200, "acc": 0.93, "stream": 160, "aim": 1.05, "nfc": 0.35, "stamina": None,
     "focus": "Accuracy first (93%+), then clean 5-9 note bursts at 170-180 BPM "
              "with strict alternation.",
     "typical_maps": "Anime/J-rock Insanes: 170-190 BPM, AR 9, OD 8, 5-9 note bursts, 3-4 spacing jumps."},
    {"stars": 5.0, "ur": 180, "acc": 0.93, "stream": 170, "aim": 1.20, "nfc": 0.30, "stamina": 1.30,
     "focus": "Back-to-back jumps without losing accuracy, and 9-16 note streams at "
              "170-180 BPM without drifting late.",
     "typical_maps": "Hard Insanes / light Extras: 180-200 BPM, AR 9-9.3, OD 8-8.5, 9-16 note streams, "
                     "longer jump sections."},
    {"stars": 5.5, "ur": 165, "acc": 0.93, "stream": 180, "aim": 1.40, "nfc": 0.25, "stamina": 1.25,
     "focus": "Build the speed base: Stream sets at 180+ BPM, wider 1/2 jumps, "
              "full-length (2-4 min) maps.",
     "typical_maps": "Extras: 180-200 BPM, AR 9.3-9.5, OD 8.5-9, 16-32 note streams, wide 1/2 jumps."},
    {"stars": 6.0, "ur": 150, "acc": 0.92, "stream": 190, "aim": 1.60, "nfc": 0.25, "stamina": 1.20,
     "focus": "Speed and stamina become the limit. Interval stream sets, rate overload (1.05-1.10x), "
              "and AR 9.5+ reading.",
     "typical_maps": "Extras at 190-210 BPM, AR 9.5, or DT on easier maps (DT often turns a ~4.2* map "
                     "into roughly 6*; check the star rating in song select)."},
    {"stars": 6.5, "ur": 140, "acc": 0.92, "stream": 200, "aim": 1.80, "nfc": 0.25, "stamina": 1.20,
     "focus": "Sustained 200 BPM streams, fast wide jumps, AR 10 reading (NM AR 10 or DT AR ~10.3).",
     "typical_maps": "Hard Extras 200-220 BPM, long 190-200 BPM streams, or DT on ~4.5* maps."},
    {"stars": 7.0, "ur": 130, "acc": 0.92, "stream": 210, "aim": 2.00, "nfc": 0.25, "stamina": 1.15,
     "focus": "Top-tier band. 200-230 BPM bursts and streams, fast wide (often cross-screen) jumps, "
              "high-AR reading. Aim players often reach it first on jump maps; stream maps need more speed.",
     "typical_maps": "Top-diff Extras 200-240 BPM with fast wide jumps, or DT on ~5* maps (AR 10.3+)."},
    {"stars": 7.5, "ur": 120, "acc": 0.92, "stream": 220, "aim": 2.20, "nfc": 0.25, "stamina": 1.15,
     "focus": "Consistency at 7*+ (near-FCs), 220+ BPM streams, DT reading at AR 10.5+.",
     "typical_maps": "7.5* jump/stream maps, DT on 5.3*+ maps."},
]

# bands below 4.0*: only shown on the roadmap of players whose current level is under 4*
LOW_BANDS = [
    {"stars": 3.0, "ur": 260, "acc": 0.94, "stream": 130, "aim": 0.60, "nfc": 0.45, "stamina": None,
     "focus": "Reading and rhythm basics: hit every note on time at a comfortable speed, keep the cursor "
              "relaxed, and pass full maps before chasing accuracy.",
     "typical_maps": "Hards and easy Insanes: 140-170 BPM, AR 7-8, OD 5-7, short 3-note bursts, small jumps."},
    {"stars": 3.5, "ur": 240, "acc": 0.94, "stream": 140, "aim": 0.75, "nfc": 0.45, "stamina": None,
     "focus": "Steady tapping: clean 3-5 note bursts, consistent jumps of 2-3 spacings, and accuracy above "
              "93% on maps you can read.",
     "typical_maps": "Light Insanes: 150-180 BPM, AR 8-8.5, OD 6-7, 3-5 note bursts, moderate jumps."},
]

BAND_BASIS = ("Checkpoint numbers are coach heuristics for what a player at each star level typically shows "
              "in this coach's measurements (UR, clean stream BPM, jump speed in px/ms, accuracy, near-FC "
              "rate); they are not official values. Players differ: aim players reach 6-7* on jump maps "
              "with a slower stream speed than listed.")


OFFSET_OK_MS = 10    # coach heuristic: within +-10 ms on regular notes = leave the offset alone (used everywhere)
UR_TARGETS_TXT = "UR <= " + ", ".join(f"{b['ur']} at {b['stars']:g}*" for b in BANDS if b["stars"] in (5.0, 6.0, 7.0))
UR_RANGE_TXT = "coach heuristic: " + UR_TARGETS_TXT


def band_targets(stars):
    for b in LOW_BANDS + BANDS:
        if abs(b["stars"] - stars) < 1e-6:
            return b
    return None


def _interp_stars(value, key, higher_better):
    """Star level implied by one metric (linear between bands, clamped to 3.5..8.0)."""
    v = _num(value)
    if v is None:
        return None
    pts = [(b["stars"], b[key]) for b in BANDS if b.get(key) is not None]
    if higher_better:
        if v <= pts[0][1]:
            s0, t0 = pts[0]; s1, t1 = pts[1]
            slope = (s1 - s0) / (t1 - t0)
            return max(3.5, s0 - (t0 - v) * slope)
        for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
            if t0 <= v <= t1:
                return s0 + (v - t0) / (t1 - t0) * (s1 - s0)
        return min(8.0, pts[-1][0] + 0.5)
    # lower is better (UR)
    if v >= pts[0][1]:
        s0, t0 = pts[0]; s1, t1 = pts[1]
        slope = (s1 - s0) / (t0 - t1)
        return max(3.5, s0 - (v - t0) * slope)
    for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
        if t1 <= v <= t0:
            return s0 + (t0 - v) / (t0 - t1) * (s1 - s0)
    return min(8.0, pts[-1][0] + 0.5)


def estimate_stars(ctx):
    """Rough star level from the player's measured skills (used only without a star-rating model)."""
    parts = []
    for key, val, hb, w, name in (("aim", ctx.get("V"), True, 0.35, "jump speed"),
                                  ("stream", ctx.get("C"), True, 0.25, "stream speed"),
                                  ("ur", ctx.get("ur_hi_od") or ctx.get("ur"), False, 0.30, "UR")):
        # (accuracy targets are deliberately flat across bands, so accuracy can't imply a star level)
        s = _interp_stars(val, key, hb)
        if s is not None:
            parts.append((s, w, name))
    if not parts:
        return None, "Not enough replay data to estimate a star level yet."
    tw = sum(w for _, w, _ in parts)
    est = sum(s * w for s, w, _ in parts) / tw
    txt = ", ".join((f"{n} below 4*" if s <= 3.5 else f"{n} ~{s:.1f}*") for s, _, n in parts)
    floor = est <= 3.6
    return round(est, 1), (f"Rough estimate from your skills in recent replays ({txt}); no star-rating data was "
                           "available (start tosu so the coach can rate maps). Treat it as +-0.5*. Because it is "
                           "measured on the maps you actually played - often above your level - it can read "
                           "lower than what you can pass"
                           + (". 3.5* is the bottom of this scale, so read it as 'at most 3.5* on these numbers', "
                              "not as your pass level: the star ratings of your recent passes on your osu! profile "
                              "are a better guide until tosu or the osu! API is set up." if floor else
                              " (the scale bottoms out at 3.5*)."))


def _ck(metric, target_txt, current, met):
    return {"metric": metric, "target": target_txt, "current": current, "met": bool(met)}


def build_roadmap(ctx, measures):
    """measures: {stars: {"ur","acc","nfc","n","source"}} per band from star-rated plays (may be empty),
    plus ctx global skills (C, V, stamina_ratio). Returns the CONTRACT_PP roadmap dict."""
    cur = ctx.get("current_stars")
    bands = []
    first_open = None
    cur_n = _num(cur)
    for b in ((LOW_BANDS + BANDS) if cur_n is not None and cur_n < 4.0 else BANDS):
        s = b["stars"]
        m = (measures or {}).get(s) or {}
        cks = []
        rated = (m.get("n") or 0) >= 3
        label = f"on {s:g}* maps" if rated else "(recent plays, any difficulty)"
        ur = m.get("ur") if rated else (ctx.get("ur_hi_od") or ctx.get("ur"))
        cks.append(_ck(f"UR {label}" if rated else
                       ("UR on OD 7+ maps (no star ratings yet)" if ctx.get("ur_hi_od") else "UR, recent plays"),
                       f"<= {b['ur']}", _fmt(ur), ur is not None and ur <= b["ur"]))
        acc = m.get("acc") if rated else (ctx.get("acc_model") or {}).get(s)
        acc_src = label if rated else (f"on {s:g}* maps (skill model)" if acc is not None else None)
        if acc is None:
            # without star ratings the median of ALL recent plays mixes push / stamina / HR plays into the
            # number, so it can't say whether this band is met: report it as unknown instead of "not met"
            cks.append(dict(_ck(f"Accuracy on {s:g}* maps", f">= {b['acc']:.0%}",
                                "unknown (no star-rated plays yet)", False), unknown=True))
        else:
            cks.append(_ck(f"Accuracy {acc_src}", f">= {b['acc']:.0%}",
                           _fmt(acc * 100, "{:.1f}%"), acc >= b["acc"]))
        C, Bk = ctx.get("C"), ctx.get("B")
        cur_s = (f"{C:.0f} BPM" if ctx.get("stream_measured") and C else
                 (f"breaks at {Bk:.0f} BPM" if Bk else
                  "not measurable yet (misses high at every BPM)" if ctx.get("stream_unmeasurable") else "?"))
        cks.append(_ck("Clean 1/4 streams (7+ notes)", f">= {b['stream']} BPM", cur_s,
                       ctx.get("stream_measured") and C is not None and C >= b["stream"]))
        V = ctx.get("V")
        cks.append(_ck("Jump speed with under ~5% misses", f">= {b['aim']:.2f} px/ms",
                       ("below 0.75 px/ms (not measured)" if ctx.get("V_floor") else _fmt(V, "{:.2f} px/ms"))
                       if V is not None else "?",
                       V is not None and V >= b["aim"] and not ctx.get("V_floor")))
        if rated:
            nfc = m.get("nfc")
            cks.append(_ck(f"Near-FC rate (<= 3 misses) {label}", f">= {b['nfc']:.0%}",
                           _fmt(nfc * 100 if nfc is not None else None, "{:.0f}%"),
                           nfc is not None and nfc >= b["nfc"]))
        else:
            cks.append(dict(_ck(f"Near-FC rate (<= 3 misses) on {s:g}* maps", f">= {b['nfc']:.0%}",
                                "unknown (no star-rated plays yet)", False), unknown=True))
        if b.get("stamina"):
            r = ctx.get("stamina_ratio")
            cks.append(_ck("Late-map misses vs expected (1.0 = no fatigue)", f"<= {b['stamina']:.2f}x",
                           _fmt(r, "{:.2f}x"), r is not None and r <= b["stamina"]))
        known = [c for c in cks if not c.get("unknown")]
        done = len(known) >= 3 and all(c["met"] for c in known)
        if not done and first_open is None:
            first_open = s
        bands.append({"stars": s, "checkpoints": cks, "focus": b["focus"], "typical_maps": b["typical_maps"],
                      "_done": done})
    for bd in bands:
        s = bd.pop("_done")
        if first_open is None or bd["stars"] < first_open:
            bd["status"] = "done" if s else "current"
        elif bd["stars"] == first_open:
            bd["status"] = "current"
        elif bd["stars"] == first_open + 0.5:
            bd["status"] = "next"
        else:
            bd["status"] = "later"
    # bands below the first open band that are not done: also "current" work (rare, keeps it honest)
    basis = (ctx.get("stars_basis") or "") + " " + BAND_BASIS
    return {"current_stars": cur, "basis": basis.strip(), "bands": bands}


def band_done(roadmap, stars):
    for b in (roadmap or {}).get("bands", []):
        if abs(b["stars"] - stars) < 1e-6:
            return b.get("status") == "done"
    return False


# ----------------------------------------------------------------------------
# phase
# ----------------------------------------------------------------------------


def return_gap(timestamps):
    """True when the local replay history has a break of RETURN_GAP_DAYS+ (a returning player)."""
    ts = sorted(t for t in (timestamps or []) if t)
    return any(b - a >= RETURN_GAP_DAYS * 86400 for a, b in zip(ts, ts[1:]))


def detect_return(timestamps, now=None):
    """Return date = first play after the last gap of RETURN_GAP_DAYS+ in the account's local
    replay history; if there is no gap, the oldest local replay (said so in `why`)."""
    ts = sorted(t for t in (timestamps or []) if t)
    if not ts:
        return None, "No local replays yet."
    gap_at = None
    for a, b in zip(ts, ts[1:]):
        if b - a >= RETURN_GAP_DAYS * 86400:
            gap_at = (a, b)
    if gap_at:
        a, b = gap_at
        return b, (f"your local replays have a {int((b - a) / 86400)}-day gap ending "
                   f"{date.fromtimestamp(b).isoformat()}")
    return ts[0], (f"return date = your oldest replay on this PC, {date.fromtimestamp(ts[0]).isoformat()}; "
                   "older plays aren't stored locally")


def _floor_half(x):
    return int(x * 2) / 2.0


def phase_checkpoints(ctx, former_peak=None):
    """(lo, hi) star bands that move the phase on, relative to the player: hi = their former peak (best
    90%+ API top play, rounded down to a band) when known, else one star above today's level; lo = hi - 0.5.
    Without any level data: 4.5 / 5.0."""
    peak = _num(former_peak)
    cur = _num((ctx or {}).get("current_stars"))
    if peak is not None and peak >= 1.0:
        hi = _floor_half(peak)
        if cur is not None:
            hi = max(hi, _floor_half(cur) + 0.5)
    elif cur is not None:
        hi = _floor_half(cur) + 1.0
    else:
        hi = 5.0
    return hi - 0.5, hi


def detect_phase(ctx, roadmap, return_ts, return_why, saved=None, now=None, returning=None, former_peak=None):
    """returning: True when the replay history shows a real break (return_gap); None = decide from
    return_why (it names a gap only when there is one). A former peak (best 90%+ API top play) 0.5*+ above
    today's level also counts as returning. Only returning players get the re-sharpen phase."""
    now = now or time.time()
    today = date.fromtimestamp(now)
    if returning is None:
        returning = bool(return_why) and "-day gap" in str(return_why)
    # also returning: the player's best API top plays are clearly above today's level (back after a break
    # whose replays aren't on this PC, e.g. a new install)
    _cur, _peak = _num((ctx or {}).get("current_stars")), _num(former_peak)
    if not returning and _cur is not None and _peak is not None and _peak >= _cur + 0.5:
        returning = True
    if not return_ts:
        return {"name": "rebuild", "since": today.isoformat(),
                "why": "No play history yet: play a few maps so the coach can measure your level."}
    days = (now - return_ts) / 86400
    lo, hi = phase_checkpoints(ctx, former_peak)
    d_lo = band_done(roadmap, lo)
    d_hi = band_done(roadmap, hi)
    rdate = date.fromtimestamp(return_ts)
    peak_txt = "your former peak" if _num(former_peak) else "the next level up"
    if d_hi:
        name = "plateau-break"
        why = (f"You meet the {hi:g}* checkpoints ({peak_txt}), so from here it's new ground: "
               "build the speed base with overload sets, longer maps and higher AR.")
    elif returning and days < RESHARPEN_DAYS and not d_lo:
        name = "re-sharpen"
        why = (f"Day {int(days) + 1} since your return ({return_why}). Old skills come back faster than new ones "
               "are built, so for about 3 weeks the plan is timing, relaxed tapping and maps at levels you've "
               f"passed before (around {lo:g}-{hi:g}*), with short sessions while your hands get used to it again.")
    else:
        name = "rebuild"
        if d_lo:
            why = (f"You meet the {lo:g}* checkpoints. Rebuild means getting to {peak_txt} (~{hi:g}*) with "
                   "targeted aim, accuracy and stream work.")
        elif returning:
            why = (f"It's been {int(days)} days since your return ({return_why}), so the ~3-week re-sharpen "
                   f"window is over, but the {lo:g}* checkpoints aren't met yet. Rebuild means structured work "
                   f"on your weakest numbers (see focus) until you're back at {peak_txt} (~{hi:g}*). Keep "
                   "sessions moderate: your hands are still re-adapting.")
        else:
            why = (f"Building up: the {lo:g}* checkpoints aren't met yet, so the plan is structured work on your "
                   f"weakest numbers (see focus) on the way to {peak_txt} (~{hi:g}*).")
    since = None
    if isinstance(saved, dict) and saved.get("name") == name and saved.get("since"):
        since = saved["since"]
    if not since:
        if name == "re-sharpen":
            since = rdate.isoformat()
        elif name == "rebuild" and not d_lo:
            since = (min(today, rdate + timedelta(days=RESHARPEN_DAYS)) if returning else today).isoformat()
        else:
            since = today.isoformat()
    return {"name": name, "since": since, "why": why}


# ----------------------------------------------------------------------------
# techniques
# ----------------------------------------------------------------------------
# Each entry: id, category, title, steps, mistakes, drill, evidence, source,
#   why(c) -> "for_you" text, score(c) -> relevance for this player (higher first)
# c = context dict built by coach_engine (see build_context there); every value may be None.


def _g(c, k, d=None):
    v = c.get(k)
    return d if v is None else v


def _share(c, k):
    return _g(c, k, 0.0)


def _ur_txt(c):
    return f"Your UR is about {_fmt(c.get('ur'))}" if c.get("ur") else "Your UR isn't measured yet"


T = []


def _t(**kw):
    T.append(kw)


# --- mindset / health (always first) ---------------------------------------
_t(id="health-breaks", category="mindset", title="Session length, breaks and pain rules (read first)",
   steps=["Play 45-90 minutes a day rather than one long weekend session; 5-6 days a week with at least one "
          "full rest day.",
          "Take a 5-minute break every 30-40 minutes: stand up, let your hands hang loose, look away from the screen.",
          "Set up so your wrists stay roughly straight: forearms supported on the desk, keyboard and mouse/tablet "
          "at elbow height, shoulders down.",
          "Stop for the day if you feel sharp pain, numbness, tingling, or an ache that gets worse while you play. "
          "Don't play through it, and don't come back until it's gone.",
          "If pain or tingling lasts more than a few days, or comes back every session, see a doctor or "
          "physiotherapist before continuing. No pp is worth a hand injury."],
   mistakes=["Grinding 3-4 hours on the first days back: your tendons have had years off, your skill memory hasn't.",
             "Squeezing the mouse/pen or pressing keys harder when tired.",
             "Treating soreness as 'part of training'."],
   drill="Set a timer for 35 minutes when you start (the pet also reminds you). When it rings, take the break "
         "even mid-session.",
   evidence="research",
   source="Lee & Genovese 1988 (meta-analysis of rest between practice trials: spacing helped performance, with a "
          "smaller effect on retention); Walker et al. 2002 (sleep after practice improved finger-tapping skill; "
          "Rickard et al. 2008 found part of that gain was a measurement artefact). The session lengths and days "
          "per week are coach heuristics, not study results. Pain rules are basic injury safety, not a study.",
   why=lambda c: ("You're coming back after years off: your skill memory returns faster than your hands' "
                  "tolerance for long sessions. Shorter daily sessions also learn better than long ones."
                  + (" Your replays already show more misses late in maps than the note types explain."
                     if c.get("fatigue_flag") else "")
                  + (" " + c["overload_txt"] if c.get("overload_txt") else "")),
   score=lambda c: 1000)

_t(id="warm-up", category="mindset", title="Warm up before anything hard",
   steps=["Run warm water over your hands or just move your fingers and wrists gently for a minute (no forced "
          "stretches).",
          "Play 2-3 easy maps (well below your level) aiming for relaxed, accurate taps, not score.",
          "Only then play the session's harder blocks. Your first hard map of the day is usually your worst; "
          "don't judge yourself on it."],
   mistakes=["Starting cold on your hardest map and getting frustrated.",
             "Making the warm-up a speed test: it should feel easy."],
   drill="The coach's 'warm-up' picks: 95%+ accuracy, loose grip, 8-10 minutes.",
   evidence="coach heuristic", source=None,
   why=lambda c: "Cold hands tap later and tenser; a warm-up makes the rest of the session's numbers meaningful.",
   score=lambda c: 95)

# --- timing -----------------------------------------------------------------
_t(id="challenge-point", category="timing", title="Accuracy before difficulty (practise at the right level)",
   steps=["Spend most of each session (about 60-70%) on maps where you get 90%+ and pass most attempts.",
          "Keep a smaller 'push' block (10-15 min) for maps above your level, where failing is fine.",
          "Move up about 0.3-0.5* once your accuracy block hits its goal on most maps for a week."],
   mistakes=["Playing mostly maps you pass at 60-75%: you practise missing, and the timing feedback is noise.",
             "Never pushing at all: you need some exposure to the next level."],
   drill="Pick 3 maps where your best is 88-94% and replay them aiming for +2% each, before any hard map.",
   evidence="research",
   source="Guadagnoli & Lee 2004 (challenge point framework - a theory with little direct testing: learning is "
          "predicted to be best when a task is hard but mostly doable); applied to osu! by this coach.",
   why=lambda c: ((f"Your recent average accuracy is {c['acc'] * 100:.0f}% and "
                   + ("none of your recent plays were near-FCs (3 misses or fewer)" if not c.get("nfc") else
                      f"only {c['nfc'] * 100:.0f}% of your recent plays were near-FCs")
                   + ": most of your practice is above your current level. " if c.get("acc") is not None else "")
                  + f"{_ur_txt(c)}"
                  + (f", well above the coach's roadmap targets ({UR_RANGE_TXT})." if _g(c, "ur", 0) > 200 else ".")),
   score=lambda c: 50 + (1 - _g(c, "nfc", 0.3)) * 30 + (20 if _g(c, "ur", 0) > 300 else 0)
                   + (12 if c.get("phase") == "re-sharpen" else 0))

_t(id="tap-to-music", category="timing", title="Tap to the music, not the approach circle",
   steps=["Turn the music up enough to hear the beat clearly (hitsounds on).",
          "Tap with the drum/vocal rhythm; use the approach circle only to know WHERE, the music tells you WHEN.",
          "On a map you know, try one run looking slightly ahead of the current note and listening for the beat."],
   mistakes=["Waiting for the approach circle to 'touch' the hit circle: that reaction makes you late.",
             "Playing with the music very quiet."],
   drill="Pick an easy map with a strong steady beat. Play once normally, once concentrating only on the beat. "
         "Compare the UR the coach reports.",
   evidence="community consensus", source=None,
   why=lambda c: (f"{_ur_txt(c)}." + (f" On regular notes (not streams) you're {c['me_ns']:+.0f} ms off on "
                                     "average, so the main issue is spread (consistency), not a fixed offset."
                                     if c.get("me_ns") is not None else "")),
   score=lambda c: 40 + _g(c, "ur", 200) / 20 + abs(_g(c, "me_ns", 0)) + (10 if c.get("phase") == "re-sharpen" else 0))

_t(id="late-bias", category="timing", title="Fix a late bias: offset, anticipation or speed?",
   steps=["First look where the lateness happens. The coach splits it: regular notes vs streams.",
          "Late on everything, by the same amount on every map: it's probably offset; see the offset check.",
          "Late mainly in streams and getting later through the stream: it's speed (your fingers can't keep up), "
          "not offset. Fix with slower stream sets, not by changing offset.",
          "Late on slow, simple notes: you're reacting instead of anticipating. Tap 'into' the beat."],
   mistakes=["Moving the offset to fix stream lateness: that makes every other note early.",
             "Changing the offset after every play."],
   drill="Play the speed picks at the coach's rate and watch the 'drift' number: aim for under 10 ms of drift "
         "before going +0.05x.",
   evidence="coach heuristic", source=None,
   why=lambda c: ((f"You hit {c['me']:+.0f} ms on average overall, but {c['me_ns']:+.0f} ms on regular notes and "
                   f"{c['me_s']:+.0f} ms in streams/bursts"
                   + (" - so your late bias comes from streams (speed), not from your offset."
                      if c.get("me_s") is not None and c.get("me_ns") is not None and c["me_s"] - c["me_ns"] > 10
                      else ".")) if c.get("me") is not None and c.get("me_ns") is not None and c.get("me_s") is not None
                  else "Your average hit error tells you whether to change offset or to work on speed."),
   score=lambda c: 25 + abs(_g(c, "me", 0)) * 3 + (15 if _g(c, "drift", 0) > 15 else 0))

_t(id="hit-error-meter", category="timing", title="Use the hit-error meter after the play, not on every note",
   steps=["Keep a hit-error meter on screen (lazer's default skins show one; if yours doesn't, add one with the "
          "skin layout editor, if your version has it).",
          "During play, glance at it only in breaks: you'll probably play better watching the notes than the meter.",
          "After the map, read the average and the spread: centred and narrow is the goal.",
          "Use the coach's after-session numbers (UR, mean error, drift) to choose tomorrow's focus."],
   mistakes=["Correcting each note based on the last one: you chase noise.",
             "Judging a whole session by one bad map."],
   drill="One check per map: after it ends, say 'early', 'late' or 'centred' and 'wide' or 'narrow'. "
         "That's all the feedback you need.",
   evidence="coach heuristic",
   source="Based on Salmoni, Schmidt & Walter 1984 (guidance hypothesis: constant feedback may help during practice "
          "but hurt retention). A 2022 meta-analysis (McKay et al.) found no reliable benefit of reduced feedback, so "
          "treat this as a sensible habit, not a proven effect.",
   why=lambda c: f"{_ur_txt(c)}: you need feedback, but in a form that builds your own timing sense.",
   score=lambda c: 33 + (10 if _g(c, "ur", 0) > 250 else 0))

_t(id="od-practice", category="timing", title="Train precision with higher OD (and HR, later)",
   steps=["Play maps at OD 8-9 that are otherwise easy for you; the tighter 300 window rewards precise timing.",
          "Optionally raise OD on an easy map with the Difficulty Adjust mod (lazer).",
          "Use HR on maps 1* or more below your level only once your UR is about 250 or lower: before that, "
          "HR's smaller circles and higher AR just add aim and reading problems."],
   mistakes=["HR on maps at your level: that's a different, harder map, not precision practice.",
             "Very low OD maps for accuracy practice: at OD 4-5, sloppy taps still get 300s."],
   drill="One accuracy pick per session at OD 8+; goal 93%+, then the same map with HR once your UR <= 250.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Your typical OD is {_fmt(c.get('typical_od'), '{:g}')}. " if c.get("typical_od") else "")
                  + ("Your UR is low enough now for HR precision work."
                     if _g(c, "ur", 999) <= 250 else
                     f"Your UR ({_fmt(c.get('ur'))}) is still above ~250, so stay NM: OD work without HR for now.")),
   score=lambda c: 20 + (25 if _g(c, "ur", 999) <= 250 else 5))

_t(id="offset-calibration", category="setup", title="Calibrate your offset once (then leave it)",
   steps=["Use simple, steady maps you know well (no streams, OD 7+, a clear beat), 3-4 of them.",
          "Look at the average hit error on those maps. If it's within about 10 ms of zero, don't touch anything.",
          "If you're off by the same amount on every map, adjust lazer's global audio offset (Settings > Audio). "
          "If your version suggests an offset from your recent plays, apply it only right after 2-3 steady, "
          "stream-free maps (it averages your last plays, so streams would skew it).",
          "If adjusting by hand: change it 5 ms, replay the same maps, and check the average moved toward zero. "
          "If it moved the wrong way, go the other direction.",
          "Re-check after changing audio device, WASAPI or headphones; per-beatmap offsets are only for single maps "
          "that are badly timed."],
   mistakes=["Calibrating on stream-heavy or hard maps (speed problems look like offset).",
             "Adjusting the offset every day.",
             "Using one map's lateness to change the global offset."],
   drill="Once: 3 steady maps, read the average, adjust if needed, confirm with 2 more maps.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Your regular (non-stream) notes average {c['me_ns']:+.0f} ms. "
                   + ("That's close enough to zero: leave your offset alone." if abs(c["me_ns"]) <= OFFSET_OK_MS else
                      "That's worth one careful calibration."))
                  if c.get("me_ns") is not None else "Do this once so every other timing number is trustworthy."),
   score=lambda c: 15 + max(0, abs(_g(c, "me_ns", 0)) - OFFSET_OK_MS) * 5)

# --- tapping ----------------------------------------------------------------
_t(id="alt-strategy", category="tapping", title="Alternate vs singletap: alternate when it's needed",
   steps=["Streams and bursts (3+ notes at 1/4): full alternate, always, starting on either finger.",
          "1/2 jumps and slow rhythms: singletapping with your main finger is fine and often more accurate.",
          "Fast 1/2 jump sections (roughly 220+ BPM, which 7* maps have): alternate them too, or one finger tires "
          "and goes late.",
          "Be consistent within a pattern; don't switch mid-burst."],
   mistakes=["Singletapping 1/4 triples and bursts because it feels safer at low BPM: it caps your speed later.",
             "Forcing alternation on every slow note and losing accuracy."],
   drill="Play a jump map at 1.0x alternating every note, then the same map singletapping the jumps; keep the one "
         "with better accuracy for that BPM.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Only {c['single'] * 100:.0f}% of your stream/burst taps repeat a key: you already alternate "
                   "streams properly. Keep it; the next step is alternating fast 1/2 jump sections too."
                   if _g(c, "single", 1) <= 0.15 else
                   f"{c['single'] * 100:.0f}% of your stream/burst taps repeat the same key; full alternation in "
                   "streams is the single biggest speed habit to fix.") if c.get("single") is not None
                  else "The coach couldn't measure your tapping style yet."),
   score=lambda c: 60 if _g(c, "single", 0) > 0.3 else 18)

_t(id="finger-motion", category="tapping", title="Tap from the fingers with small motions",
   steps=["Rest both fingers on the keys all the time; press only as deep as the key needs.",
          "Keep your wrist and forearm quiet for bursts; for long streams some wrist 'bounce' is normal - let it "
          "happen, don't force it.",
          "Lift each finger only just enough for the key to reset.",
          "If one finger lags in streams, practise slow streams starting with that finger."],
   mistakes=["Lifting fingers high off the keys (slow, tiring, late).",
             "Hammering the keys: louder isn't faster."],
   drill="On a keyboard outside the game: 16 even taps alternating, as quietly as you can, at a metronome speed "
         "you can hold. Then play the speed picks.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Your streams break around {c['B']:.0f} BPM and you fall ~{_fmt(c.get('drift'))} ms behind by "
                   "the end of long streams" if c.get("B") else
                   "Your stream misses are high at every BPM you've played, so your stream limit isn't measurable yet"
                   if c.get("stream_unmeasurable") else "Your stream speed isn't measured yet")
                  + "; efficient finger motion is the base for every speed gain."),
   score=lambda c: 25 + (20 if _g(c, "B", 999) < 170 or c.get("stream_unmeasurable") else 0) + _g(c, "drift", 0) / 2)

_t(id="low-tension", category="tapping", title="Low tension: loose grip, light keys",
   steps=["Check your shoulders, jaw and grip every break. Drop your shoulders and loosen your hand on the "
          "mouse/pen.",
          "Press keys with just enough force to actuate; let the key spring push your finger back up.",
          "When a stream feels like it's falling apart, relax instead of pushing harder.",
          "If a hand feels tight, take the break then - tension is the first step toward pain."],
   mistakes=["Tensing up for hard sections: speed drops and timing drifts late.",
             "White-knuckle grip on the mouse/pen in jump sections."],
   drill="Play one stream pick at 0.9x focusing only on staying loose; compare drift with your normal run.",
   evidence="community consensus", source=None,
   why=lambda c: (("Your late-map misses and your " if c.get("fatigue_flag") else "Your ")
                  + (f"{_fmt(c.get('drift'))} ms late drift in long streams" if c.get("drift") else "stream timing")
                  + " are typical of tension building up through a map."),
   score=lambda c: 40 + (15 if c.get("fatigue_flag") else 0) + (10 if _g(c, "drift", 0) > 20 else 0))

_t(id="key-actuation", category="tapping", title="Keys, actuation and rapid trigger (hardware is a small factor)",
   steps=["Any decent keyboard works; consistency matters more than hardware.",
          "If you have adjustable actuation or rapid trigger (Hall-effect/analog keys), start with moderate settings, "
          "not the most sensitive ones, and keep them fixed for weeks.",
          "Rapid trigger lets a key reset with a tiny lift, which helps fast streams - but very sensitive "
          "settings cause accidental double taps or releases, and it doesn't fix timing.",
          "Only change hardware when your current setup actually limits you (keys not resetting at your speed)."],
   mistakes=["Buying a rapid-trigger keyboard expecting a big jump: most gains still come from practice.",
             "Changing actuation settings every few days."],
   drill="None: set it once, then forget it.",
   evidence="coach heuristic", source=None,
   why=lambda c: ("You alternate cleanly, so hardware isn't your bottleneck right now; speed and stamina are."
                  if _g(c, "single", 1) <= 0.15 else "Worth a check, but technique comes first."),
   score=lambda c: 8)

_t(id="metronome-tapping", category="tapping", title="Stream speed: step up by BPM, not by guessing",
   steps=["Find the BPM where your streams are clean (the coach shows it).",
          "Practise 1/4 streams at about 5-10 BPM below that, in sets of 30-60 seconds with equal rest.",
          "When a set is clean with under ~10 ms drift, add +5 BPM (or +0.05x rate).",
          "If two sets in a row break, drop back 10 BPM for the rest of the day."],
   mistakes=["Practising streams 20+ BPM above your limit: you train mashing, not speed.",
             "Judging by feel: use the drift and miss numbers."],
   drill="The coach's 'speed' picks are already rated to your comfort BPM. 3-5 sets per session.",
   evidence="coach heuristic",
   source="Progression principle consistent with Guadagnoli & Lee 2004 (challenge point); the BPM steps are a "
          "coach heuristic.",
   why=lambda c: ((f"Your streams start breaking around {c['B']:.0f} BPM" if c.get("B") else
                   "Your stream limit can't be measured yet (misses are high at every BPM so far; start with the "
                   "coach's stream test at 150-160 BPM)" if c.get("stream_unmeasurable") else "Your stream speed")
                  + (f" (clean up to ~{c['C']:.0f})" if c.get("stream_measured") and c.get("C") else "")
                  + ". 5* maps need about 170 clean, 7* about 210+ (coach estimate): this is your longest road."),
   score=lambda c: 30 + (25 if _g(c, "B", 999) < 170 or c.get("stream_unmeasurable") else 5)
                   + (5 if c.get("phase") == "rebuild" else 0))

_t(id="burst-hand", category="tapping", title="Start bursts on either finger",
   steps=["With full alternate, odd-length bursts (3, 5, 7 notes) end on the same finger they started on; the "
          "next burst then starts on the other finger.",
          "Practise triples and 5-note bursts starting with your weaker finger.",
          "Don't 'reset' to your main finger before every burst: that's where late notes and dropped taps come from."],
   mistakes=["Inserting a double tap with the main finger to reset the hand.",
             "Only ever drilling bursts starting with the index finger."],
   drill="On a burst-heavy map at 0.8-0.9x, consciously start every burst with the finger that comes next.",
   evidence="community consensus", source=None,
   why=lambda c: ("Bursts are a big part of the 4.5-5.5* anime maps you played before; misses there often come from "
                  "the start finger, not raw speed."),
   score=lambda c: 18 + (10 if _g(c, "B", 999) < 170 else 0))

# --- aim --------------------------------------------------------------------
_t(id="snap-flow", category="aim", title="Snap aim vs flow aim: use the right one",
   steps=["Snap aim: sharp angles and back-and-forth jumps. Move fast, stop on the circle, tap, move again.",
          "Flow aim: curved or linear patterns. Keep the cursor moving smoothly through the notes without stopping.",
          "Read the pattern shape before it starts: angle sharp -> snap; smooth curve -> flow.",
          "Train both: most 5-7* maps mix them."],
   mistakes=["Flowing through a sharp back-and-forth jump (overshoot, miss).",
             "Stopping dead on every note of a smooth curve (slow, jerky, late)."],
   drill="Aim picks at the coach's rate: first run snap-only focus, second run reading each pattern's shape.",
   evidence="community consensus", source=None,
   why=lambda c: (f"{c['aim_share'] * 100:.0f}% of your misses were taps in time with the cursor off the circle: "
                  "aim is your biggest miss cause right now." if _g(c, "aim_share", 0) >= 0.2 else
                  "Aim misses aren't your main problem, but 5*+ maps need both snap and flow."),
   score=lambda c: 30 + _share(c, "aim_share") * 60)

_t(id="aim-through", category="aim", title="Aim through the circle, don't brake early",
   steps=["On jumps, aim for the circle's centre, not its near edge.",
          "Commit to the full distance in one motion; slow down ON the circle, not before it.",
          "For linear jumps, imagine the cursor passing through the circle towards the next one.",
          "Watch a replay of a jump section in slow motion: are you landing short?"],
   mistakes=["Decelerating halfway and landing on the near edge (shows as 'cursor off the circle' misses).",
             "Overcorrecting into big overshoots."],
   drill="Jump-heavy map at 0.8x: only goal is landing in the centre of every circle, ignore accuracy.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Your landings average {c['landing']:+.2f} radii along the jump direction"
                   + (" (slightly short): you tend to brake before the circle." if c["landing"] < -0.03 else
                      " (overshooting)." if c["landing"] > 0.05 else " (well centred), so focus on consistency."))
                  if c.get("landing") is not None else "Landing position isn't measured yet."),
   score=lambda c: 25 + _share(c, "aim_share") * 45 + (15 if _g(c, "landing", 0) < -0.03 else 0))

_t(id="sens-consistency", category="aim", title="One sensitivity / tablet area, kept for weeks",
   steps=["Pick a setting where a comfortable full-screen jump uses a comfortable wrist/arm motion.",
          "Mouse: set the in-game multiplier near 1.0 and set the speed with your mouse's DPI instead, if you can "
          "(large in-game multipliers can skip pixels).",
          "Tablet: one fixed area with the same aspect ratio as your screen, set in one place only (lazer's tablet "
          "settings OR the tablet driver, not both).",
          "Keep it for at least 2-4 weeks. Every change resets part of your aim memory."],
   mistakes=["Changing sensitivity after every bad session.",
             "Copying a top player's area without testing it on your desk."],
   drill="Change once if needed, then play only aim picks for 3 days before judging.",
   evidence="community consensus", source=None,
   why=lambda c: (("Your lazer input settings show a tablet configured but lazer's tablet handler off, and "
                   f"the mouse handler in relative mode at {_fmt(c.get('sens'), '{:g}')}x. Tell the coach which you play with "
                   "(pet settings) and keep that setup fixed.") if c.get("tablet_known") and not c.get("device") else
                  f"You play with a {c['device']}. Keep your setup fixed while you rebuild aim." if c.get("device")
                  else "Consistency of your aim setup matters more than the exact value."),
   score=lambda c: 30 + (15 if not c.get("device") else 0) + _share(c, "aim_share") * 15)

_t(id="jump-drills", category="aim", title="Jump drills: raise difficulty in small steps",
   steps=["Pick jump maps where the hardest jumps are just below where your misses climb (the coach's aim picks).",
          "Make one thing harder at a time (wider spacing, a faster rate, or smaller circles) so you can tell "
          "what caused new misses.",
          "Start at 0.8-0.9x if needed; +0.05x after a run with under ~3% jump misses.",
          "For cross-screen jumps: a few minutes on wide-spaced maps at lower BPM, then back to normal maps."],
   mistakes=["Jumping straight to the fastest, widest maps (practising flailing).",
             "Adding HR (smaller circles) and DT (more speed) at the same time."],
   drill="Aim block: 2-3 aim picks, 2 runs each, rate up 0.05x only after a clean run.",
   evidence="community consensus",
   source="Background: Fitts 1954 (difficulty grows with distance relative to target size; with the time fixed by "
          "the map, that shows up as more misses). The step-by-step drill format is community practice.",
   why=lambda c: (((f"Your jump misses are already above ~5% at the slowest jump speeds measured (under "
                    f"{c['V']:.2f} px/ms), so this is about precision more than speed for now"
                    if c.get("V_floor") else f"Your jump misses climb from about {c['V']:.2f} px/ms")
                   + (f"; {c['aim_share'] * 100:.0f}% of all your misses are the cursor off the circle, so "
                      "precise landing matters as much as speed" if _g(c, "aim_share", 0) >= 0.2 else "")
                   + "; 5* maps need about 1.2 px/ms clean, 7* about 2.0+ (coach estimate).") if c.get("V") else
                  "Jump speed isn't measured yet."),
   score=lambda c: 30 + _share(c, "aim_share") * 50 + (10 if _g(c, "V", 9) < 1.2 else 0))

_t(id="cursor-dance", category="aim", title="Cut extra cursor movement",
   steps=["Between notes, move the cursor on the shortest comfortable path.",
          "No wiggles, circles or 'waiting dances' on slow sections: they cost time when the next jump comes.",
          "On sliders, follow just enough to keep the slider ball; don't trace wide loops exactly if not needed."],
   mistakes=["Big overshoots that need a correction before the next note.",
             "Moving the cursor away and back during gaps."],
   drill="Watch one of your replays at 0.5x speed with the cursor trail: mark every unnecessary loop.",
   evidence="community consensus", source=None,
   why=lambda c: "Tidier paths give you more time per jump, which matters as jump speed goes up toward 7*.",
   score=lambda c: 14 + _share(c, "aim_share") * 15)

# --- reading ----------------------------------------------------------------
_t(id="ar-progression", category="reading", title="AR: step up slowly",
   steps=["Play most maps at the AR you can read comfortably; add +0.3 to +0.5 AR at a time (Difficulty Adjust "
          "mod, or a slight speed-up which raises AR too).",
          "7* NM maps are usually AR 9.3-10; with DT, AR goes above 10.",
          "If you lose track of patterns at a new AR, step back for a few days, then try again."],
   mistakes=["Jumping from AR 9 to AR 10.3 (DT) in one go.",
             "Lowering AR for practice so far that you learn bad timing habits (very low AR)."],
   drill="One reading pick per session at AR +0.5 above your usual, with easy patterns.",
   evidence="community consensus", source=None,
   why=lambda c: (f"Your typical effective AR is {_fmt(c.get('typical_ar'), '{:g}')}"
                  + (f" and {c['lost_share'] * 100:.0f}% of misses had your cursor far from the note (lost)."
                     if c.get("lost_share") else ".")
                  + " You'll need AR 9.5-10 comfortable for 6-7*."),
   score=lambda c: 15 + _share(c, "lost_share") * 60 + (8 if _g(c, "typical_ar", 9) < 9.3 else 0))

_t(id="hd-reading", category="reading", title="HD as reading training",
   steps=["Use HD on maps 0.5-1* below your level: no approach circles forces you to read timing from spacing "
          "and rhythm.",
          "Once a map is comfortable NM, an HD run shows whether you really read it or were reacting to circles.",
          "HD also gives a pp bonus, but only take it on score attempts once it doesn't cost you accuracy."],
   mistakes=["HD on maps at your limit: you'll just miss more.",
             "Playing HD all the time and never improving NM reading at high AR."],
   drill="Warm-up or accuracy maps with HD, 1-2 runs per session.",
   evidence="community consensus", source=None,
   why=lambda c: "You played a lot of HD before your break; re-learning HD reading on easier maps is a fast win.",
   score=lambda c: 18 + _share(c, "lost_share") * 30 + (8 if c.get("phase") == "re-sharpen" else 0))

_t(id="rhythm-reading", category="reading", title="Read unusual rhythms before you play them",
   steps=["When a section has odd snaps (1/3, 1/6, 1/8, triplets, polyrhythms), watch your replay of it or "
          "open the map in the editor to see the timeline.",
          "Play it at 0.75x until you can tap it in rhythm, then raise the rate.",
          "Count or hum the rhythm once before playing it."],
   mistakes=["Guessing the rhythm from spacing alone at full speed.",
             "Treating every odd-rhythm miss as an aim miss."],
   drill="The coach's trouble spots marked 'rhythm' or with 'early' taps: practise exactly those sections.",
   evidence="coach heuristic", source=None,
   why=lambda c: ((f"{(c.get('early_share') or 0) * 100:.0f}% of your misses were early taps and "
                   f"{(c.get('timing_share') or 0) * 100:.0f}% off-time taps with the cursor on the note.")
                  if c.get("timing_share") is not None else "Odd rhythms appear more from 5.5* up."),
   score=lambda c: 12 + (_share(c, "early_share") + _share(c, "timing_share")) * 40)

# --- stamina ----------------------------------------------------------------
_t(id="interval-streams", category="stamina", title="Interval stream sets",
   steps=["Pick a map with long streams at about 10 BPM below your break (the coach's speed picks).",
          "30-60 seconds of streaming, then the same time resting; 3-5 sets.",
          "When all sets are clean, make the sets longer before making them faster.",
          "Stop the set early if your hand tenses up or hurts."],
   mistakes=["One long grind until your hand burns.",
             "Doing stamina work at the end of a session when you're already tired and sloppy."],
   drill="2-3 times a week, 10-12 minutes, right after the warm-up.",
   evidence="coach heuristic",
   source="Work/rest structure borrowed from general sport training; no osu!-specific study.",
   why=lambda c: ((f"You drift ~{_fmt(c.get('drift'))} ms late by the end of long streams"
                   if c.get("drift") else "Your stream endurance")
                  + (" and your late-map misses are higher than the note types explain." if c.get("fatigue_flag")
                     else ".")),
   score=lambda c: 25 + (20 if c.get("fatigue_flag") else 0) + _g(c, "drift", 0) / 2
                   + (10 if c.get("phase") == "plateau-break" else 0))

_t(id="rate-overload", category="stamina", title="Overload with custom speed (1.05-1.15x)",
   steps=["Take a map you can already play cleanly at 1.0x.",
          "Play it at 1.05x, then 1.10x (lazer's DT/NC custom speed), up to 1.15x.",
          "Go back to 1.0x: it should feel slower and easier to control.",
          "Use this for speed and aim only; for accuracy work stay at 1.0x or below."],
   mistakes=["Overloading maps you can't play at 1.0x yet.",
             "Forgetting that a faster rate also raises AR and tightens OD."],
   drill="Plateau-break phase: 1 overload map per session (3 runs, rising rate), then the same map at 1.0x.",
   evidence="community consensus", source=None,
   why=lambda c: ("Not yet: during re-sharpen, get maps clean at 1.0x first." if c.get("phase") == "re-sharpen"
                  else "Useful once a map is clean at 1.0x; from the plateau-break phase it's a core tool."
                  if c.get("phase") == "rebuild" else
                  "You're in the plateau-break phase: overload is how you build speed past your old peak."),
   score=lambda c: {"re-sharpen": 5, "rebuild": 20, "plateau-break": 55}.get(c.get("phase"), 15))

_t(id="length-progression", category="stamina", title="Map length: TV size -> full songs",
   steps=["Keep the difficulty (BPM, stream length) fixed while you increase map length.",
          "TV size (~90 s) -> 2-3 minutes -> 4-5 minute full versions.",
          "Only increase length when your last minute is as accurate as your first."],
   mistakes=["Raising BPM and length at the same time.",
             "Only ever playing 90-second maps and then failing every full version."],
   drill="One 2.5-5 minute map per session (the coach's stamina picks), full run, no retries in the middle.",
   evidence="community consensus", source=None,
   why=lambda c: ("Your last thirds have more misses than the patterns there explain: build length tolerance."
                  if c.get("fatigue_flag") else "Your stamina flag is off for now, so this is maintenance."),
   score=lambda c: 15 + (25 if c.get("fatigue_flag") else 0))

# --- mindset ----------------------------------------------------------------
_t(id="external-focus", category="mindset", title="Focus on the target and the rhythm, not your hands",
   steps=["While playing, think about the circle you're going to and the beat, not your wrist or fingers.",
          "Give each block a one-line focus that is outside your body ('land in the centre', 'stay on the beat').",
          "Think about hand technique only in slow drills, then let it go when playing."],
   mistakes=["Thinking 'move my wrist faster' mid-jump.",
             "Watching your hands during streams."],
   drill="Before each block, say its focus out loud in 3-4 words.",
   evidence="research",
   source="Wulf 2013 (review: early studies found an external focus helped); McKay et al. 2024 (bias-corrected "
          "re-analysis) found the effects small or negligible, so the evidence is mixed.",
   why=lambda c: ("Evidence is mixed (early studies positive, a 2024 bias-corrected re-analysis found small or no "
                  "effects), but it's harmless to try and gives each block a clear goal."),
   score=lambda c: 28)

_t(id="practice-vs-performance", category="mindset", title="Separate practice from score attempts",
   steps=["Practice blocks: specific goal, slowed sections, failing is fine, no pp pressure.",
          "Performance block (5-10 minutes, at the end): a few full attempts at a score/pp map.",
          "Set an attempt limit (for example 5 tries), then stop even if it went badly."],
   mistakes=["Turning every map into a score attempt (you stop learning).",
             "Never doing full attempts (you never learn to finish under pressure)."],
   drill="The coach's plan already ends with a push or score block; keep it at the end.",
   evidence="community consensus",
   source="Background: Ericsson et al. 1993 (deliberate practice: specific goals and feedback). Splitting practice "
          "blocks from score attempts is common coaching practice, not a tested result.",
   why=lambda c: "You want pp and you want to improve: they need different kinds of sessions.",
   score=lambda c: 30)

_t(id="retry-discipline", category="mindset", title="Retry discipline: finish practice maps",
   steps=["On practice maps, don't quick-restart after an early miss: play to the end so the hard later "
          "sections get practice too.",
          "Quick restarts are fine on score attempts, within your attempt limit.",
          "If you restart the same map 5+ times in a row, switch maps for a while."],
   mistakes=["Retry-spamming the first 30 seconds of a map for an hour.",
             "Playing angry: if you're tilting, take the break."],
   drill="Rule for practice blocks: every map gets played to the end once before any restart.",
   evidence="community consensus", source=None,
   why=lambda c: ("Your miss data comes from whole maps, which is good; keep finishing maps so trouble spots in "
                  "the second half get practised too."),
   score=lambda c: 26 + (10 if _g(c, "nfc", 1) < 0.1 else 0))

_t(id="choke-management", category="mindset", title="Choke management on near-FCs",
   steps=["Expect nerves near the end of a good run; they're normal.",
          "Use breaks in the map to exhale slowly and loosen your grip.",
          "Keep tapping exactly as before: don't slow down or 'play safe' - that's what causes late misses.",
          "Practise full runs on maps you know, so the end of a map feels familiar."],
   mistakes=["Holding your breath through the last section.",
             "Blaming luck: chokes are usually tension, which is trainable."],
   drill="Once a week, pick a map you've near-FC'd and do 3 full runs treating each as a tournament attempt.",
   evidence="coach heuristic", source=None,
   why=lambda c: "Near-FCs will start happening as your accuracy comes back; this keeps them from becoming chokes.",
   score=lambda c: 14 + (10 if 0.05 <= _g(c, "nfc", 0) <= 0.3 else 0))

_t(id="plateau-habits", category="mindset", title="Plateau habits: variety, patience, weekly numbers",
   steps=["Judge progress weekly, not daily: daily numbers swing a lot (sleep, stress, warm-up).",
          "Mix skills within a session once they're decent (jumps, streams, reading), rather than one skill for "
          "an hour.",
          "Rotate map pools every 1-2 weeks so you don't just memorise maps.",
          "After 4-6 hard weeks, take a lighter week (half the time, easier maps): it often unlocks progress."],
   mistakes=["Changing everything (settings, keys, sens) when progress stalls.",
             "Grinding one map 50 times."],
   drill="Every Sunday: compare this week's UR, stream break and jump-miss numbers with last week in the coach.",
   evidence="research",
   source="Shea & Morgan 1979 (contextual interference: mixed practice learns slower in-session but retains "
          "better; effects are weaker in applied settings, Brady 2004).",
   why=lambda c: ("You're past your old peak: progress is slower now, so keep the weekly view."
                  if c.get("phase") == "plateau-break" else
                  "Your early weeks back will move fast; this matters more once gains slow down."),
   score=lambda c: 10 + (45 if c.get("phase") == "plateau-break" else 0))

# --- setup ------------------------------------------------------------------
_t(id="frame-latency", category="setup", title="Frame limiter and input latency",
   steps=["Avoid VSync for gameplay: it adds input delay.",
          "A frame limiter at 2x-8x your refresh rate is fine for most PCs; 'Unlimited' only if it stays stable.",
          "Make sure Windows runs your monitor at its highest refresh rate.",
          "Turn on the FPS counter once to check frame times are stable, then turn it off."],
   mistakes=["Chasing thousands of FPS while frame times stutter.",
             "Changing graphics settings mid-session."],
   drill="One-time check.",
   evidence="community consensus", source=None,
   why=lambda c: ((f"Your frame limiter is set to {c['framesync']}." if c.get("framesync") else "")
                  + (" That's fine." if c.get("framesync") in ("Limit2x", "Limit4x", "Limit8x", "Unlimited")
                     else " Consider a 2x-8x limiter instead." if c.get("framesync") else "")),
   score=lambda c: 25 if c.get("framesync") == "VSync" else 9)

_t(id="key-bindings", category="setup", title="Key bindings and hand position",
   steps=["Two adjacent keys you can rest both fingers on (for example Z/X), bound once, never moved.",
          "Disable mouse buttons in gameplay if you tap on the keyboard, so a stray click can't hit notes.",
          "Keep the keyboard where your wrist is straight, not bent sideways."],
   mistakes=["Switching key layouts between sessions.", "Keyboard at an angle that bends the wrist."],
   drill="One-time check.",
   evidence="community consensus", source=None,
   why=lambda c: ("Mouse buttons are disabled in gameplay in your settings, good for keyboard tapping."
                  if c.get("mouse_buttons_off") else "Quick check that your bindings are comfortable and fixed."),
   score=lambda c: 7)

_t(id="display", category="setup", title="Screen and visual settings",
   steps=["Use a consistent window mode and playfield size; changing them changes your aim distances.",
          "Background dim high (or full) and busy effects off help reading at high AR.",
          "Pick a skin with clear hit circles and a visible approach circle, then keep it."],
   mistakes=["Changing skins often: circle size perception changes.",
             "Very bright storyboards/videos at high AR."],
   drill="One-time check.",
   evidence="community consensus", source=None,
   why=lambda c: ("Your settings already have a full background dim and storyboards off: good for reading."
                  if c.get("dim_full") else "Quick check of your visual setup."),
   score=lambda c: 6)


TECHNIQUES = T


def rank_techniques(c):
    phase = c.get("phase")
    bonus = {"re-sharpen": {"timing": 10, "tapping": 6},
             "rebuild": {"aim": 5, "tapping": 5, "stamina": 5},
             "plateau-break": {"stamina": 10, "mindset": 5, "reading": 5}}.get(phase, {})
    out = []
    for t in TECHNIQUES:
        try:
            sc = float(t["score"](c)) + bonus.get(t["category"], 0)
        except Exception:
            sc = 0.0
        try:
            why = t["why"](c)
        except Exception:
            why = ""
        out.append((sc, {"id": t["id"], "category": t["category"], "title": t["title"], "for_you": why,
                         "steps": list(t["steps"]), "mistakes": list(t["mistakes"]), "drill": t["drill"],
                         "evidence": t["evidence"], "source": t["source"]}))
    out.sort(key=lambda x: -x[0])
    return [x[1] for x in out]


# ----------------------------------------------------------------------------
# setup checks
# ----------------------------------------------------------------------------


def setup_checks(c, game, fw, inp):
    """game/fw: read_ini() dicts (or None); inp: read_input_config() (or None). Read-only facts only."""
    out = []
    game = game or {}
    fw = fw or {}
    dev = c.get("device")

    # offset
    off = _num(game.get("AudioOffset"))
    me, me_ns, me_s = c.get("me"), c.get("me_ns"), c.get("me_s")
    if me_ns is not None:
        spread = c.get("me_spread")
        detail = (f"Global audio offset is {_fmt(off, '{:g}')} ms. " if off is not None else "") \
            + f"Your hit error on regular notes averages {me_ns:+.1f} ms" \
            + (f", in streams/bursts {me_s:+.1f} ms" if me_s is not None else "") \
            + (f" (overall {me:+.1f} ms)" if me is not None else "") + ". "
        if spread:
            detail += f"Per-map averages vary by about +-{spread:.0f} ms, so single maps say little. "
        if abs(me_ns) <= OFFSET_OK_MS:
            st = "ok"
            detail += (f"Within +-{OFFSET_OK_MS} ms on regular notes, so leave your offset alone"
                       + ("; the late bias is in streams (speed/drift), which offset can't fix."
                          if me_s is not None and me_s - me_ns > 10 else "."))
        elif abs(me_ns) <= 20:
            st = "check"
            detail += "That's a consistent bias on regular notes: worth one careful calibration."
        else:
            st = "warn"
            detail += "That's a large consistent bias: calibrate before trusting other timing numbers."
        if game.get("AutomaticallyAdjustBeatmapOffset", "").lower() == "true":
            detail += " Automatic per-beatmap offset adjustment is on; turn it off while calibrating."
        out.append({"id": "offset", "title": "Audio offset", "status": st, "detail": detail,
                    "how": (f"Only if regular notes are more than {OFFSET_OK_MS} ms off: calibrate on 3-4 steady maps "
                            "without streams (OD 7+). Change lazer's global audio offset (Settings > Audio) in 5 ms "
                            "steps and check the average moved toward zero. If your version suggests an offset from "
                            "recent plays, apply it only right after 2-3 steady, stream-free maps. Re-check after "
                            "changing audio device or WASAPI.")})
    elif off is not None:
        out.append({"id": "offset", "title": "Audio offset", "status": "check",
                    "detail": f"Global audio offset is {off:g} ms; not enough plays to check it yet.",
                    "how": "Play a few steady maps; the coach will compare your hit error with the offset."})

    # WASAPI
    w = fw.get("AudioUseExperimentalWasapi")
    if w is not None:
        on = w.lower() == "true"
        out.append({"id": "wasapi", "title": "Audio output (WASAPI)", "status": "ok",
                    "detail": ("Experimental WASAPI output is on: it lowers audio latency. "
                               + (f"If your {off:g} ms offset was set before you enabled it, re-check the offset. "
                                  if off is not None and abs(off) >= 20 else "")
                               + "If you ever hear crackling or sound drifting, turn it off and recalibrate.")
                    if on else "Standard audio output. Fine; WASAPI (experimental) may lower latency on some PCs.",
                    "how": "Settings > Audio in lazer. Recalibrate offset after any change."})

    # frame limiter
    fs = fw.get("FrameSync")
    if fs:
        good = fs in ("Limit2x", "Limit4x", "Limit8x", "Unlimited")
        out.append({"id": "frame-limiter", "title": "Frame limiter", "status": "ok" if good else "check",
                    "detail": f"Frame limiter: {fs}. " + ("Fine for low input latency." if good else
                                                          "VSync adds input delay; a 2x-8x limiter is usually better."),
                    "how": "Settings > Graphics > Frame limiter. Check Windows runs your monitor at its highest "
                           "refresh rate."})
    wm = fw.get("WindowMode")
    if wm:
        out.append({"id": "window-mode", "title": "Window mode", "status": "ok",
                    "detail": f"Window mode: {wm}"
                              + (f", fullscreen resolution {fw['SizeFullscreen']}" if fw.get("SizeFullscreen") else "")
                              + ". Borderless is fine on Windows 10/11 for most PCs; if input ever feels delayed, "
                                "try Fullscreen and compare." if wm == "Borderless" else
                              f"Window mode: {wm}.",
                    "how": "Settings > Graphics. Keep one mode so your aim distances stay the same."})

    # input device
    mouse = (inp or {}).get("mouse") or {}
    tab = (inp or {}).get("tablet") or {}
    sens = _num(fw.get("CursorSensitivity")) or mouse.get("sensitivity")
    rel = mouse.get("relative")
    tab_name = tab.get("name")
    if dev == "tablet":
        if tab and not tab.get("enabled"):
            st, det = "check", (f"You play on a tablet{' (' + tab_name + ')' if tab_name else ''}, but lazer's own "
                                "tablet handler is off, so the pen goes through the tablet driver / Windows Ink and "
                                "your area is whatever the driver sets. The tablet area saved in lazer "
                                + (f"({tab['area'][0]:g} x {tab['area'][1]:g} mm) " if tab.get("area") and tab["area"][0]
                                   else "") + "is not in use. "
                                + (f"The mouse handler is in relative (high precision) mode at {sens:g}x; on some "
                                   "setups that makes a tablet act like a mouse, so the cursor may not follow the "
                                   "pen 1:1. "
                                   if rel and sens else ""))
            how = ("Check that the same pen position always puts the cursor on the same screen spot. If it drifts, "
                   "either enable lazer's tablet handler (Settings > Input, if your version shows it) and set your "
                   "area there, or turn off high precision mouse in lazer. Set the area in ONE place only, and "
                   "keep it for weeks.")
        else:
            st, det = "ok", "Tablet handler in lazer is on." + (f" Area {tab['area'][0]:g} x {tab['area'][1]:g} mm."
                                                                 if tab.get("area") and tab["area"][0] else "")
            how = "Keep one area with your screen's aspect ratio; don't change it for at least 2-4 weeks."
        out.append({"id": "input-device", "title": "Tablet setup", "status": st, "detail": det, "how": how})
    elif dev == "mouse":
        st = "check" if (sens and sens >= 2.0) else "ok"
        out.append({"id": "input-device", "title": "Mouse sensitivity", "status": st,
                    "detail": (f"In-game sensitivity {sens:g}x" + (" with high precision (relative) mode" if rel else "")
                               + ". " if sens else "")
                              + ("A multiplier this high can skip pixels on precise aim; most mouse players keep the "
                                 "in-game value near 1.0 and set speed with DPI." if st == "check" else "Fine."),
                    "how": "If you change it: raise mouse DPI and lower the in-game multiplier so a full-screen "
                           "jump feels the same distance on your desk. Change once, then keep it for weeks."})
    else:
        bits = []
        if tab_name:
            bits.append(f"lazer knows a {tab_name} tablet, but its tablet handler is "
                        + ("on" if tab.get("enabled") else "off"))
        if sens:
            bits.append(f"mouse sensitivity is {sens:g}x" + (" in high precision (relative) mode" if rel else ""))
        out.append({"id": "input-device", "title": "Mouse or tablet?", "status": "check",
                    "detail": ("Your settings: " + "; ".join(bits) + ". " if bits else "")
                              + "The coach doesn't know which you play with, and the right advice differs "
                                "(sensitivity only matters for a mouse; a tablet needs a fixed area).",
                    "how": "Set your device, keys and tapping style in the pet's settings."})

    # tapping style
    s1 = c.get("single")
    if s1 is not None:
        out.append({"id": "alternation", "title": "Stream alternation", "status": "ok" if s1 <= 0.2 else "check",
                    "detail": f"{s1 * 100:.0f}% of stream/burst taps repeat a key"
                              + (": you alternate properly." if s1 <= 0.2 else ": alternate streams and bursts."),
                    "how": "Full alternate on anything 1/4; singletap 1/2 jumps if it's more accurate for you."})

    # mouse buttons / keys
    mb = game.get("MouseDisableButtons")
    if mb is not None and c.get("keys") != "mouse buttons":
        on = mb.lower() == "true"
        out.append({"id": "mouse-buttons", "title": "Mouse buttons in gameplay", "status": "ok" if on else "check",
                    "detail": "Mouse buttons are disabled during gameplay, so stray clicks can't hit notes." if on else
                              "Mouse buttons are active during gameplay; if you tap on the keyboard, a stray click "
                              "can hit or break notes.",
                    "how": "Settings > Input > Mouse: disable mouse buttons during gameplay (keyboard players)."})

    # visuals
    dim = _num(game.get("DimLevel"))
    if dim is not None:
        sb = game.get("ShowStoryboard", "").lower() == "true"
        good = dim >= 0.8 and not sb
        out.append({"id": "visuals", "title": "Background dim and storyboards", "status": "ok" if good else "check",
                    "detail": f"Background dim {dim:.0%}, storyboards {'on' if sb else 'off'}."
                              + (" Good for reading." if good else " High dim and no storyboard help at high AR."),
                    "how": "Settings > Gameplay > Background."})

    # hit error meter: can't be read from the ini (it's part of the skin layout)
    out.append({"id": "hit-error-meter", "title": "Hit-error meter visible", "status": "check",
                "detail": "The coach can't see your HUD layout. You need a hit-error meter for timing work.",
                "how": "Lazer's default skins include one. If yours doesn't show it, add it with the skin layout "
                       "editor (if your version has it). Read it after maps, not on every note."})
    return out


# ----------------------------------------------------------------------------
# mod / rate guidance for recommendations
# ----------------------------------------------------------------------------


def mod_note(rec, c):
    r = _num(rec.get("rate")) or 1.0
    p = rec.get("purpose")
    mods = str(rec.get("mods") or "NM").upper()
    ur = c.get("ur")
    if p == "pp":
        bits = []
        if "HD" in mods:
            bits.append("HD gives a pp bonus; take it only if you read this map comfortably NM.")
        if "DT" in mods or "NC" in mods:
            bits.append(f"DT is {r:.2f}x speed (AR and OD go up too); only if its streams and jumps are inside your "
                        "range.")
        if "HR" in mods:
            bits.append("HR: smaller circles, higher AR/OD - precision; best once your UR is about 250 or lower"
                        + (f" (now {ur:.0f})." if ur else "."))
        return " ".join(bits) or "NoMod: just a clean score attempt."
    if r < 0.999:
        return (f"HT custom speed {r:.2f}x: slowed for clean repetition, so you practise the right movements. "
                "Raise 0.05x after a clean run and finish at 1.0x.")
    if r > 1.001:
        return (f"DT custom speed {r:.2f}x: overload - it makes the 1.0x version feel easier. Speed/aim work only, "
                "not accuracy.")
    if p == "accuracy":
        return ("HR later: once your UR is about 250 or lower, replay these with HR for precision"
                + (f" (now {ur:.0f}, so NoMod for now)." if ur and ur > 250 else "."))
    if p == "reading":
        return "After a clean NM run, try it with HD: no approach circles makes you read rhythm from spacing."
    if p == "warmup":
        return "NoMod: warm-up is for relaxed, accurate tapping, not reading tricks."
    if p == "push":
        return "NoMod first; HD only after you can pass it NoMod."
    return "NoMod at 1.0x."
