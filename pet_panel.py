"""Mochi's UI: the quick "Next up" card (the main thing you use) and the full coach panel.

QuickCard  - small always-on-top card beside the pet. Default (CONTRACT_PP2.md) is the PP card: total pp + rank,
             what the map hovered in song select is worth for you (expected pp, range, pass chance, +gain, best
             mod), how accurate the predictions have been, and the top rank-up picks with Copy search. The
             trainer's "Next up" step and the debrief of a try are the card's other modes. It never takes
             keyboard focus, so after Copy you can paste straight into osu! song select (Ctrl+V).
CoachPanel - normal window with 5 tabs: PP, Today, Progress, Tips, Settings. Big fonts, short cards, details
             behind "More" expanders.

Everything treats coach_data.json as untrusted: any key may be missing, null or oddly typed. A failure in one
tab is logged and shown inline instead of breaking the others.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import re
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import ttk

import pet_util as U
from pet_util import g, dicts, num, txt

C = {
    "bg": "#1b1726", "card": "#262036", "card2": "#30294a", "hi": "#3a2f5c", "border": "#3b3254",
    "fg": "#f7f3ff", "sub": "#d2c9ea", "dim": "#a39bc0",
    "pink": "#ff9ec7", "mint": "#7fe3c8", "lav": "#b6a4ff", "yellow": "#ffd873",
    "peach": "#ffb98a", "red": "#ff8f8f", "blue": "#8ccaff", "ink": "#221a30",
}
KEY = "#0a0810"   # transparent key colour for the rounded card

MOOD_FACE = {"happy": "(◕‿◕)", "neutral": "(•ᴗ•)", "worried": "(•_•;)", "sleepy": "(－_－) zzZ"}
MOOD_LINE = {"happy": "You're doing great - keep it up!", "worried": "Let's slow down and fix a few things.",
             "sleepy": "It's been a while - welcome back!", "neutral": "Here's your plan for today."}
EVIDENCE = {"research": ("Backed by research", C["mint"]),
            "community consensus": ("Common advice", C["blue"]),
            "coach heuristic": ("Coach's rule of thumb", C["lav"])}
EVIDENCE_HELP = ("Badges:  Backed by research = studies support it  ·  Common advice = what strong players agree "
                 "on  ·  Coach's rule of thumb = my best guess from your data, keep it if it helps")
TECH_CATS = [("tapping", "Tapping"), ("timing", "Timing & accuracy"), ("aim", "Aim"), ("reading", "Reading"),
             ("stamina", "Stamina"), ("setup", "Setup & gear"), ("mindset", "Health & mindset")]
CHECK_STATUS = {"ok": ("OK", C["mint"]), "check": ("Worth a look", C["yellow"]), "warn": ("Fix this", C["red"])}
BAND_STATUS = {"done": ("Done", C["mint"]), "current": ("You are here", C["pink"]),
               "next": ("Next", C["yellow"]), "later": ("Later", C["border"])}
PHASE_INFO = {"re-sharpen": ("Re-sharpen", "getting your old level back"),
              "rebuild": ("Rebuild", "building a solid base before harder maps"),
              "plateau-break": ("Plateau break", "pushing past where you've been stuck")}
VERDICT_PLAIN = {"too hard": ("too hard for now", C["red"]), "good practice": ("good practice", C["mint"]),
                 "too easy": ("easy for you", C["blue"])}
SETUP_Q = [("device", "You aim with a...", ["mouse", "tablet"]),
           ("keys", "You tap with...", ["keyboard", "keypad", "mouse buttons"]),
           ("style", "Your tapping style", ["alternate", "singletap", "mixed"])]
PURPOSE_ORDER = ["warmup", "accuracy", "speed", "aim", "reading", "stamina", "push", "pp"]

TAB_NAMES = ["PP", "Today", "Progress", "Tips", "Settings"]
TAB_ALIASES = {"Setup": "Settings", "About": "Settings", "Roadmap": "Progress", "Recent plays": "Progress",
               "Trouble spots": "Progress", "PP & Climb": "Progress", "Techniques": "Tips", "Focus": "Tips",
               "Map picks": "Today", "Rank-up": "PP", "Picks": "PP", "Training": "Today"}
CONF_COL = {"high": C["mint"], "medium": C["yellow"], "low": C["peach"]}


def F(size=12, weight="normal"):
    return ("Segoe UI", size, weight) if weight != "normal" else ("Segoe UI", size)


def mod_color(mods, rate=None):
    ms = U.split_mods(mods)
    r = num(rate)
    if not ms:
        return C["blue"] if r and abs(r - 1) > 0.004 else C["mint"]
    if any(m in ("HT", "DC") for m in ms):
        return C["blue"]
    if any(m in ("DT", "NC") for m in ms):
        return C["peach"]
    if "HR" in ms:
        return C["red"]
    return C["lav"]


def block_title(name):
    """'Skill rotation: stamina' -> 'Stamina practice', 'Speed (stream test)' -> 'Speed practice (stream test)'."""
    n = txt(name, "Step")
    low = n.lower()
    if low.startswith("skill rotation:"):
        rest = n.split(":", 1)[1].strip()
        return (rest[:1].upper() + rest[1:] + " practice") if rest else n
    if low == "push":
        return "Push yourself"
    first = low.split(" ")[0]
    if first in ("accuracy", "aim", "speed", "reading", "stamina") and "practice" not in low:
        return n.split(" ")[0] + " practice" + n[len(n.split(" ")[0]):]
    return n


def goal_words(t):
    """'>= 94%' -> '94% or more', '<= 220' -> '220 or less' (units like px/ms dropped)."""
    s = txt(t).replace("px/ms", "").strip()
    m = re.match(r"^(>=|<=|>|<)\s*(.+)$", s)
    if m:
        op, v = m.groups()
        return f"{v.strip()} or {'more' if op.startswith('>') else 'less'}"
    return s


def cp_current(t):
    s = txt(t, "?").replace("px/ms", "").strip()
    if s.lower().startswith(("unknown", "not measurable")):
        return "not measured yet"
    return s


PHASE_DO = {"re-sharpen": "Lots of maps you already know, at a relaxed level, to get your feel back. Keep sessions "
                          "short.",
            "rebuild": "Clean, relaxed plays that train your weakest numbers (see Tips) before harder maps. Keep "
                       "sessions moderate - your hands are still getting used to it again.",
            "plateau-break": "Harder maps and faster speeds on purpose, a little at a time, with easy maps in between."}


def _f(s):
    m = re.search(r"-?\d+(?:\.\d+)?", txt(s))
    return float(m.group(0)) if m else None


def _more_pct(ratio):
    return f"{round((ratio - 1) * 100)}% more" if ratio > 1 else "no more"


def checkpoint_line(cp):
    """One roadmap goal in plain words: 'Timing spread (UR): 385 now → goal 220 or less'."""
    metric = txt(g(cp, "metric"), "goal")
    low = metric.lower()
    cur_raw, tgt_raw = txt(g(cp, "current")), txt(g(cp, "target"))
    cur = cp_current(cur_raw)
    unmeasured = cur == "not measured yet"
    if "px/ms" in tgt_raw or low.startswith("jump speed"):
        t, c = _f(tgt_raw), _f(cur_raw)
        if t and c and t > c:
            return (f"Faster clean jumps (under 5% misses): goal = jumps {round((t / c - 1) * 100)}% faster than "
                    "you're comfortable with now")
        return "Faster clean jumps (under 5% misses): " + ("goal reached" if t and c else "not measured yet")
    if "vs expected" in low:
        t, c = _f(tgt_raw), _f(cur_raw)
        now = f"{_more_pct(c)} than expected now" if c else "not measured yet"
        return f"Misses late in maps: {now}  →  goal at most {_more_pct(t) if t else '?'}"
    if low.startswith("ur"):
        name = "Timing spread (UR)"
    elif "near-fc" in low:
        name = "Plays with 3 misses or fewer" + (" on " + metric.split(" on ", 1)[1] if " on " in metric else "")
    else:
        name = re.sub(r"\s*\(no star ratings yet\)", "", metric).replace("1/4 ", "")
    name = U.plain(name)
    now = cur if unmeasured else f"{U.plain(cur)} now"
    return f"{name}:  {now}  →  goal {goal_words(tgt_raw)}"


def focus_line(it):
    """'65% of your misses are aim misses (goal: under 45%)' for one focus item (None if it has no numbers)."""
    cur, tgt = num(g(it, "current")), num(g(it, "target"))
    if cur is None or tgt is None:
        return None
    m = txt(g(it, "metric")).lower()
    c, t = f"{cur:g}", f"{tgt:g}"
    if m == "ur":
        return f"Timing spread (UR): {c} now, next goal under {t}"
    if m.startswith("aim misses"):
        return f"{c}% of your misses are aim misses (goal: under {t}%)"
    if m.startswith("jump miss"):
        return f"You miss {c}% of jumps (goal: under {t}%)"
    if m.startswith("stream miss"):
        return f"You miss {c}% of stream notes (goal: under {t}%)"
    if m.startswith("stream break"):
        return f"Your streams break at {c} BPM (goal: {t} BPM)"
    if m.startswith("near-fc"):
        return f"{c}% of your plays have 3 misses or fewer (goal: {t}%)"
    if "vs expected" in m:
        return f"Misses in the last third of maps: {_more_pct(cur)} than expected (goal: under {_more_pct(tgt)})"
    if m.startswith("lost misses"):
        return f"{c}% of your misses are 'lost' (cursor far from the note) (goal: under {t}%)"
    if m.startswith("same-key"):
        return f"{c}% of your stream taps repeat the same key (goal: under {t}%)"
    if m.startswith("mean hit error"):
        return f"You hit {abs(cur):g} ms {'late' if cur > 0 else 'early'} on average (goal: 0 ms)"
    unit = "%" if m.endswith("%") else ""
    name = U.plain(txt(g(it, "metric")).rstrip("% ").strip())
    return f"{name + ': ' if name else ''}{c}{unit} now, goal {t}{unit}"


def same_gist(a, b):
    """True when most words of the step goal `a` already appear in the map goal `b` (don't say it twice)."""
    wa = [w for w in re.findall(r"[a-z0-9]+", txt(a).lower()) if len(w) >= 2]
    bl = txt(b).lower()
    if not wa or not bl:
        return False
    return sum(1 for w in wa if w in bl) / len(wa) >= 0.6


def bpm_note(rec):
    """'  ·  about 155 BPM at this speed' when the mods change the speed (the plan talks in effective BPM)."""
    bpm, r = num(g(rec, "bpm")), U._rate_of(g(rec, "rate"))
    if not bpm:
        return ""
    if r or any(m in ("HT", "DC", "DT", "NC") for m in U.split_mods(g(rec, "mods"))):
        return f"\n≈{bpm:.0f} BPM at this speed"
    return f"\n{bpm:.0f} BPM" if txt(g(rec, "purpose")).lower() == "speed" else ""


def _open_url(url):
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001
        return False


def first_sentence(t, n=170):
    s = " ".join(txt(t).split())
    m = re.search(r"(?<=[.!?])\s", s)
    if m and m.start() <= n:
        return s[:m.start()]
    return U.shorten(s, n)


# ============================================================================
# shared little widget helpers
# ============================================================================

class _Widgets:
    s = 1.0

    def px(self, v):
        return int(round(v * self.s))

    def wrap(self, lbl, pad=2):
        """Make a label re-wrap to whatever width it gets (no guessing margins)."""
        def on(e, l=lbl):
            w = e.width - pad
            if w > 40:
                try:
                    if abs(int(float(str(l.cget("wraplength")) or 0)) - w) > 3:
                        l.configure(wraplength=w)
                except (tk.TclError, ValueError):
                    pass
        lbl.bind("<Configure>", on, add="+")
        return lbl

    def lbl(self, parent, text, size=12, weight="normal", fg=None, pady=0, padx=0, side=None, wrap=True,
            anchor="w", **kw):
        l = tk.Label(parent, text=text, bg=kw.pop("bg", None) or parent["bg"], fg=fg or C["fg"],
                     font=F(size, weight), justify="left", anchor=anchor, **kw)
        if side:
            l.pack(side=side, padx=padx, pady=pady, fill="x", expand=True)
        else:
            l.pack(fill="x", anchor="w", padx=padx, pady=pady)
        if wrap:
            self.wrap(l)
        return l

    def btn(self, parent, text, cmd, kind="normal", size=None):
        bg, fg, sz, padx, pady = {
            "primary": (C["pink"], C["ink"], 13, 18, 8),
            "go": (C["mint"], C["ink"], 12, 14, 6),
            "normal": (C["card2"], C["fg"], 11, 12, 5),
            "small": (C["card2"], C["fg"], 10, 9, 3),
        }[kind]
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=C["lav"],
                      activeforeground=C["ink"], relief="flat", bd=0, padx=self.px(padx), pady=self.px(pady),
                      font=F(size or sz, "bold"), cursor="hand2", highlightthickness=0)
        return b

    def chip(self, parent, text, color, side="left", size=11, fg=None):
        l = tk.Label(parent, text=text, bg=color, fg=fg or C["ink"], font=F(size, "bold"),
                     padx=self.px(9), pady=self.px(2))
        l.pack(side=side, padx=(0, self.px(8)) if side == "left" else (self.px(8), 0), pady=self.px(2))
        return l

    def link(self, parent, text, cmd, fg=None, size=11, side="left"):
        l = tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["lav"], font=F(size, "bold"), cursor="hand2")
        l.pack(side=side)
        l.bind("<Button-1>", lambda e: cmd())
        l.bind("<Enter>", lambda e: l.configure(fg=C["pink"]))
        l.bind("<Leave>", lambda e: l.configure(fg=fg or C["lav"]))
        return l

    def copy(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except tk.TclError:
            return False


# ============================================================================
# scroll container
# ============================================================================

class ScrollFrame(tk.Frame):
    def __init__(self, parent, s):
        super().__init__(parent, bg=C["bg"])
        self.s = s
        self.cv = tk.Canvas(self, bg=C["bg"], highlightthickness=0, bd=0, yscrollincrement=max(1, int(20 * s)))
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style="Pet.Vertical.TScrollbar")
        self.inner = tk.Frame(self.cv, bg=C["bg"], padx=int(18 * s), pady=int(14 * s))
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.cv.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.update_region())
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self.cv._pet_scroll = self
        self.inner._pet_scroll = self

    def update_region(self):
        try:
            self.cv.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        except tk.TclError:
            pass

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self.cv.yview_moveto(0)

    def wheel(self, delta):
        first, last = self.cv.yview()
        if first <= 0 and last >= 1:
            return
        self.cv.yview_scroll(int(-delta / 120) * 3 or (-1 if delta > 0 else 1), "units")


# ============================================================================
# quick card
# ============================================================================

class QuickCard(_Widgets):
    """The 'Next up' card. hooks: open_panel(), refresh(), changed(), noactivate(top), topmost(top),
    touched() (user interacted)."""
    W = 430

    def __init__(self, root, s, settings, hooks=None):
        self.root, self.s, self.settings = root, s, settings
        self.hooks = hooks or {}
        self.data = {}
        self.top = None
        self.visible = False
        self.why_open = False
        self.anchor = None
        self.errors = []
        self.state = {}          # for --selftest: what is on screen
        self.fb = None
        # PP card (CONTRACT_PP2.md) is the default; the trainer's "Next up" and the debrief of a try are the others
        self.mode = "pp"         # "pp" | "next" | "debrief"
        self.pp_data = {}        # pp_data.json (osu_api)
        self.recs = {}           # pp_recs.json (pp_recommend)
        self.report = None       # pp_model.model_report() (None: use pp_recs.json's copy)
        self.rows = []           # U.read_pred_log() rows
        self.live = None         # newest pp_live state with a map (song select / results)
        self.alts = []           # extra mod variants for the hovered map [{mods, expected, gain, p_pass}]
        self.tosu_note = None    # why there is no hovered map (tosu not running ...)
        self.debrief = None      # the debrief shown in "debrief" mode
        self.level = None        # U.level_change(...) of that debrief (banner), or None
        self.pending_since = None   # time.time() when a try ended and its debrief isn't in yet

    def _hook(self, name, *a):
        fn = self.hooks.get(name)
        if fn:
            try:
                return fn(*a)
            except Exception:
                U.log.error("card hook %s failed:\n%s", name, traceback.format_exc())
        return None

    def _build(self):
        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=KEY)
        try:
            top.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        top.attributes("-topmost", True)
        self.cv = tk.Canvas(top, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.body = tk.Frame(self.cv, bg=C["card"])
        self.cv.create_window(self.px(14), self.px(12), window=self.body, anchor="nw", tags="body")
        if self.hooks.get("premap"):
            self._hook("premap", top)    # real window gets WS_EX_NOACTIVATE before it is ever shown
        else:
            top.update_idletasks()
            self._hook("noactivate", top)   # never take keyboard focus from the game
        top.bind("<Enter>", lambda e: self._hook("touched"), add="+")   # hovering = using it (no auto-hide)

    # ------------------------------------------------------------ public
    def set_data(self, d):
        self.data = d if isinstance(d, dict) else {}
        U.set_aim_comfort(self.data)
        if self.visible:
            self.render()

    def toggle(self, anchor):
        if self.visible:
            self.hide()
        else:
            self.show(anchor)

    def show(self, anchor=None):
        if self.top is None:
            self._build()
        if anchor:
            self.anchor = anchor
        self.render()
        prev = U.foreign_foreground()
        self.top.deiconify()
        self._hook("noactivate", self.top)
        self._hook("topmost", self.top)
        self._hook("guard", prev)        # hand the keyboard straight back to the game if Windows moved it
        self.visible = True

    def hide(self):
        self.visible = False
        if self.top is not None:
            try:
                self.top.withdraw()
            except tk.TclError:
                pass

    def show_debrief(self, deb, anchor=None, level=None):
        """Switch to the debrief of the try that just ended and show the card."""
        self.mode, self.debrief, self.level = "debrief", deb, level
        self.pending_since = None
        self.why_open = False
        self.show(anchor)

    def show_next(self):
        self.mode = "next"
        self._hook("touched")
        if self.visible:
            self.render()

    def show_pp(self):
        self.mode = "pp"
        self._hook("touched")
        if self.visible:
            self.render()

    _UNSET = object()

    def set_pp(self, pp_data=_UNSET, recs=_UNSET, report=_UNSET, rows=_UNSET, live=_UNSET, alts=_UNSET,
               tosu_note=_UNSET):
        """New PP inputs; the card redraws only when it shows the PP view and something it shows changed."""
        changed = False
        for k, v in (("pp_data", pp_data), ("recs", recs), ("report", report), ("rows", rows), ("live", live),
                     ("alts", alts), ("tosu_note", tosu_note)):
            if v is QuickCard._UNSET:
                continue
            if k in ("pp_data", "recs") and not isinstance(v, dict):
                v = {}
            if k in ("rows", "alts") and not isinstance(v, list):
                v = []
            if getattr(self, k) != v:
                setattr(self, k, v)
                changed = True
        if changed and self.visible and self.mode == "pp":
            self.render()
        return changed

    def pp_view(self):
        return U.pp_card_view(self.pp_data, self.recs, self.report, self.rows, self.live, self.data, self.alts,
                              tosu_note=self.tosu_note)
    def set_pending(self, on):
        """A try just ended: say 'Looking at that try...' until its debrief arrives."""
        import time as _t
        new = _t.time() if on else None
        if (new is None) != (self.pending_since is None):
            self.pending_since = new
            if self.visible:
                self.render()

    # ------------------------------------------------------------ state
    def session(self):
        return U.session_of(self.data)

    def steps(self):
        return U.plan_steps(self.data)

    def cur(self):
        st = self.steps()
        return st, U.current_step(st, self.settings)

    def pick(self, step):
        picks = step["picks"]
        if not picks:
            return None, 0
        i = self.settings.pick_get(U.today(), step["key"]) % len(picks)
        return picks[i], i

    def _cmd(self, cmd):
        """Adaptive session: Done/Skip/back/restart go to the coach (session_cmd.json + engine run, via the
        pet's 'cmd' hook). -> False when there is no session (then the local ticks below are used)."""
        if not self.session():
            return False
        if self.hooks.get("cmd"):
            self._hook("cmd", cmd)
        else:   # no pet around (tests): just apply it locally
            self.data = U.session_apply_cmd(self.data, cmd)
        self.mode = "next"
        self.why_open = False
        self._after_change()
        return True

    def done(self):
        if self._cmd("done"):
            return
        st, i = self.cur()
        if i < len(st):
            self.settings.check_set(U.today(), st[i]["key"], True)
        self.why_open = False
        self._after_change()

    def back(self):
        if self._cmd("back"):
            return
        st, i = self.cur()
        if i > 0:
            self.settings.check_set(U.today(), st[i - 1]["key"], False)
        self._after_change()

    def skip(self):
        """Old plan: next map of the block. Adaptive session: skip this step."""
        if self._cmd("skip"):
            return
        st, i = self.cur()
        if i < len(st) and st[i]["picks"]:
            k = st[i]["key"]
            self.settings.pick_set(U.today(), k, (self.settings.pick_get(U.today(), k) + 1) % len(st[i]["picks"]))
        self._after_change()

    def restart(self):
        if self._cmd("restart"):
            return
        for s in self.steps():
            self.settings.check_set(U.today(), s["key"], False)
        self._after_change()

    def _rec_for_copy(self):
        """The map the Copy button copies: the debrief's next map (debrief view), else the step's map."""
        if self.session() or self.mode == "debrief":
            if self.mode == "debrief" and isinstance(self.debrief, dict):
                na = g(self.debrief, "next_action")
                md5 = txt(g(na, "md5")) if isinstance(na, dict) else ""
                typ = txt(g(na, "type")).lower() if isinstance(na, dict) else ""
                recs = {txt(g(r, "id")): r for r in dicts(g(self.data, "recommendations"))}
                if md5 and md5 in recs:
                    return recs[md5]
                cur = U.session_current(self.data)
                if typ in ("next_map", "next_step") and cur and cur.get("map"):
                    return cur["map"]
                if md5 and md5 == txt(g(self.debrief, "md5")) and txt(g(self.debrief, "map")):
                    return {"search": re.sub(r"\s+", " ", re.sub(r"[\[\]]| - ", " ", txt(g(self.debrief, "map")))).strip()}
                if cur and cur.get("map"):
                    return cur["map"]
                return None
            cur = U.session_current(self.data)
            return cur["map"] if cur else None
        st, i = self.cur()
        if i >= len(st):
            return None
        return self.pick(st[i])[0]

    def copy_search(self):
        rec = self._rec_for_copy()
        if rec is None or not U.search_text(rec):
            return False
        ok = self.copy(U.search_text(rec))
        if ok and self.fb is not None:
            try:
                self.fb.configure(text="Copied! Paste it in song select (Ctrl+V)", fg=C["mint"])
            except tk.TclError:
                pass
        self.state["copied"] = U.search_text(rec)
        self._hook("touched")
        return ok

    def toggle_why(self):
        self.why_open = not self.why_open
        self.render()

    def _after_change(self):
        self._hook("touched")
        self._hook("changed")
        if self.visible:
            self.render()

    # ------------------------------------------------------------ drawing
    def render(self):
        if self.top is None:
            self._build()
        try:
            for c in self.body.winfo_children():
                c.destroy()
            self.fb = None
            self._content(self.body)
        except Exception:
            tb = traceback.format_exc()
            self.errors.append(tb)
            U.log.error("quick card failed:\n%s", tb)
            for c in self.body.winfo_children():
                c.destroy()
            self.lbl(self.body, "Oops - I couldn't draw this card. Details are in pet_log.txt.", 12, fg=C["red"],
                     wraplength=self.px(self.W - 40), wrap=False)
        self._shape()

    def _shape(self):
        self.body.update_idletasks()
        bw, bh = self.body.winfo_reqwidth(), self.body.winfo_reqheight()
        p = self.px
        w, h = bw + p(28), bh + p(26)
        cv = self.cv
        cv.delete("bg")
        r = p(18)
        pts = [r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h, w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
        cv.create_polygon([v + 2 for v in pts], fill=C["card"], outline=C["lav"], width=p(2), smooth=True,
                          splinesteps=16, tags="bg")
        cv.tag_lower("bg")
        cw, ch = int(w + 5), int(h + 5)
        cv.configure(width=cw, height=ch)
        x, y = self._place(cw, ch)
        self.top.geometry(f"{cw}x{ch}+{x}+{y}")

    def _place(self, cw, ch):
        if not self.anchor:
            return 100, 100
        ax, ay, aw, ah, wa = self.anchor
        l, t, r, b = wa
        p = self.px
        x = ax - cw + p(18)                 # left of the pet
        if x < l + 6:
            x = ax + aw - p(18)             # no room: right of the pet
        y = ay + ah - ch - p(8)             # bottom aligned with the pet's feet
        x = max(l + 6, min(int(x), r - cw - 6))
        y = max(t + 6, min(int(y), b - ch - 6))
        return x, y

    def _content(self, f):
        tk.Frame(f, bg=C["card"], width=self.px(self.W + 40), height=1).pack(anchor="w")   # same width every time
        if self.mode == "debrief" and isinstance(self.debrief, dict):
            return self._debrief_content(f)
        if self.mode == "pp":
            return self._pp_content(f)
        self.mode = "next"
        if self.session():
            return self._session_content(f)
        p = self.px
        wl = p(self.W - 4)
        st, i = self.cur()
        n = len(st)
        self.state = {"step": i, "n": n}
        # header
        head = tk.Frame(f, bg=C["card"])
        head.pack(fill="x")
        close = tk.Label(head, text="✕", bg=C["card"], fg=C["dim"], font=F(14, "bold"), cursor="hand2",
                         padx=p(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: (self._hook("touched"), self.hide()))
        close.bind("<Enter>", lambda e: close.configure(fg=C["pink"]))
        close.bind("<Leave>", lambda e: close.configure(fg=C["dim"]))
        if n and i < n:
            title = "Break time" if not st[i]["picks"] and "break" in st[i]["block"].lower() else "Next up"
        elif n:
            title = "Today's plan"
        else:
            title = f"Hi! I'm {U.PET_NAME}"
        tk.Label(head, text=title, bg=C["card"], fg=C["fg"], font=F(15, "bold")).pack(side="left")
        if n:
            tk.Label(head, text=f"Step {i + 1} of {n}" if i < n else f"{n} of {n} done", bg=C["card"], fg=C["sub"],
                     font=F(12)).pack(
                side="right", padx=(0, p(10)))
            if i > 0:
                self.link(head, "‹ back", self.back, fg=C["dim"], size=10, side="right").pack_configure(padx=(0, p(8)))
            self._dots(f, n, i)
        if not n:
            self._no_plan(f, wl)
            return
        if i >= n:
            self._all_done(f, wl)
            return
        step = st[i]
        tr = tk.Frame(f, bg=C["card"])
        tr.pack(fill="x", pady=(p(8), 0))
        tk.Label(tr, text=block_title(step["block"]), bg=C["card"], fg=C["pink"], font=F(16, "bold"),
                 anchor="w").pack(side="left")
        if step["minutes"] and "min" not in step["block"].lower():
            tk.Label(tr, text=f"  ·  {step['minutes']:.0f} min", bg=C["card"], fg=C["sub"], font=F(12)).pack(
                side="left")
        rec, pi = self.pick(step)
        self.state.update(block=step["block"], pick=pi, map=U.map_name(rec) if rec else None)
        if rec is None:
            self.lbl(f, U.plain(step["goal"]) or "Take it easy for a few minutes.", 12, fg=C["fg"], wrap=False,
                     wraplength=wl, pady=(p(8), p(4)))
            if step["id"] == "review":
                self.btn(f, "Open Tips", lambda: (self._hook("touched"), self._hook("open_panel", "Tips")),
                         "primary").pack(fill="x", pady=(p(8), p(2)))
            self._buttons(f, step, None, 0)
            return
        # the step's how-to (unless the map's own goal already says the same)
        how = U.plain(step["goal"])
        if how and not same_gist(step["goal"], txt(g(rec, "goal"))):
            self.lbl(f, how, 11, fg=C["sub"], wrap=False, wraplength=wl, pady=(p(2), 0))
        self.lbl(f, U.map_name(rec), 13, "bold", wrap=False, wraplength=wl, pady=(p(8), p(4)))
        mr = tk.Frame(f, bg=C["card"])
        mr.pack(fill="x", pady=(p(2), p(2)))
        tk.Label(mr, text=U.mod_pill(g(rec, "mods"), g(rec, "rate")), bg=mod_color(g(rec, "mods"), g(rec, "rate")),
                 fg=C["ink"], font=F(14, "bold"), padx=p(12), pady=p(3)).pack(side="left", anchor="n")
        tk.Label(mr, text=U.mod_howto(g(rec, "mods"), g(rec, "rate")) + bpm_note(rec), bg=C["card"], fg=C["sub"],
                 font=F(11), justify="left", anchor="w", wraplength=p(self.W - 150)).pack(side="left",
                                                                                       padx=(p(10), 0))
        goal = txt(g(rec, "goal")) or step["goal"]
        if goal:
            self.lbl(f, "Goal on this map: " + U.plain(goal), 12, fg=C["fg"], wrap=False, wraplength=wl,
                     pady=(p(6), 0))
        self.btn(f, "Copy song search", self.copy_search, "primary").pack(fill="x", pady=(p(10), p(2)))
        self.fb = tk.Label(f, text="Then paste it in osu! song select (Ctrl+V)", bg=C["card"],
                           fg=C["sub"], font=F(11), anchor="w")
        self.fb.pack(fill="x")
        self._buttons(f, step, rec, pi)

    def _buttons(self, f, step, rec, pi):
        p = self.px
        row = tk.Frame(f, bg=C["card"])
        row.pack(fill="x", pady=(p(8), 0))
        self.btn(row, "Done - next step  ›", self.done, "go").pack(side="left")
        if rec is not None and len(step["picks"]) > 1:
            self.btn(row, "Skip map", self.skip, "small").pack(side="left", padx=(p(8), 0))
            tk.Label(row, text=f"map {pi + 1} of {len(step['picks'])}", bg=C["card"], fg=C["sub"],
                     font=F(11)).pack(side="left", padx=(p(6), 0))
        foot = tk.Frame(f, bg=C["card"])
        foot.pack(fill="x", pady=(p(10), 0))
        why = U.plain(txt(g(rec, "reason"))) if rec is not None else ""
        if why:
            self.link(foot, ("▾ Why this map?" if self.why_open else "▸ Why this map?"), self.toggle_why, size=11)
        self.link(foot, "Open full coach ›", lambda: (self._hook("touched"), self._hook("open_panel")),
                  size=11, side="right")
        if why and self.why_open:
            self.lbl(f, why, 11, fg=C["sub"], wrap=False, wraplength=p(self.W - 4), pady=(p(6), 0))

    def _dots(self, f, n, i):
        p = self.px
        if n > 16:
            return
        cv = tk.Canvas(f, height=p(16), width=p(self.W), bg=C["card"], highlightthickness=0, bd=0)
        cv.pack(fill="x", pady=(p(4), 0))
        x = p(7)
        for k in range(n):
            r = p(6) if k == i else p(4.5)
            col = C["mint"] if k < i else (C["pink"] if k == i else "")
            cv.create_oval(x - r, p(8) - r, x + r, p(8) + r, fill=col, outline=C["mint"] if k < i else
                           (C["pink"] if k == i else C["dim"]), width=p(1.5))
            x += p(20)

    def _no_plan(self, f, wl):
        p = self.px
        self.state["empty"] = True
        self.lbl(f, "No plan yet", 16, "bold", fg=C["pink"], wrap=False, wraplength=wl, pady=(p(8), 0))
        self.lbl(f, "I need to look at your replays first. Play a map in osu!, or press Refresh - "
                    "the first look takes a minute or two.", 12, wrap=False, wraplength=wl, pady=(p(6), 0))
        row = tk.Frame(f, bg=C["card"])
        row.pack(fill="x", pady=(p(12), 0))
        self.btn(row, "Refresh", lambda: (self._hook("touched"), self._hook("refresh")), "primary").pack(side="left")
        self.link(row, "Open full coach ›", lambda: self._hook("open_panel"), side="right")

    def _all_done(self, f, wl):
        p = self.px
        self.state["all_done"] = True
        self.lbl(f, "All done for today!", 16, "bold", fg=C["mint"], wrap=False, wraplength=wl, pady=(p(8), 0))
        self.lbl(f, "Great session. Rest your hands - more isn't better when they're tired. "
                    "Stop for the day if your wrist or hand hurts.", 12, wrap=False, wraplength=wl, pady=(p(6), 0))
        row = tk.Frame(f, bg=C["card"])
        row.pack(fill="x", pady=(p(12), 0))
        self.btn(row, "Start over", self.restart, "small").pack(side="left")
        self.link(row, "Open full coach ›", lambda: self._hook("open_panel"), side="right")

    # ------------------------------------------------------------ PP card (CONTRACT_PP2.md)
    def do_pick(self, pv):
        """A pick's button: Copy search (owned map) or open its page on the osu! website."""
        self._hook("touched")
        act = pv.get("action") if isinstance(pv, dict) else None
        if not act:
            return False
        kind, val = act
        if kind == "copy":
            ok = self.copy(val)
            self.state["copied"] = val
            msg = "Copied! Paste it in osu! song select (Ctrl+V)" if ok else "Couldn't copy - try again"
        else:
            ok = bool(self._hook("open_url", val)) if self.hooks.get("open_url") else _open_url(val)
            self.state["opened"] = val
            msg = "Opened on the osu! website - download it there" if ok else "Couldn't open the browser"
        if self.fb is not None:
            try:
                self.fb.configure(text=msg, fg=C["mint"] if ok else C["yellow"])
            except tk.TclError:
                pass
        return ok

    def _pp_content(self, f):
        p = self.px
        wl = p(self.W - 4)
        v = self.pp_view()
        hdr, lv, ms = v["header"], v["map"], v["model"]
        self.state = {"mode": "pp", "total": hdr["total_txt"], "rank": hdr["rank_txt"], "today": hdr["today_txt"],
                      "map": lv["full_name"] if lv else None, "big": lv["big"] if lv else None,
                      "range": lv["range"] if lv else None, "gain": lv["gain"] if lv else None,
                      "pass": lv["pass"] if lv else None, "tip": lv["tip"] if lv else None,
                      "model": ms["line"], "picks": [pv["name"] for pv in v["picks"]], "n_picks": v["n_picks"]}
        self._head(f, "Your PP")
        # total + rank + today
        hr = tk.Frame(f, bg=C["card"])
        hr.pack(fill="x", pady=(p(4), 0))
        if hdr["total_txt"]:
            tk.Label(hr, text=hdr["total_txt"], bg=C["card"], fg=C["mint"], font=F(24, "bold")).pack(side="left")
            side = tk.Frame(hr, bg=C["card"])
            side.pack(side="left", padx=(p(14), 0), fill="y")
            if hdr["rank_txt"]:
                tk.Label(side, text="rank " + hdr["rank_txt"], bg=C["card"], fg=C["fg"], font=F(13, "bold"),
                         anchor="w").pack(fill="x")
            if hdr["today_txt"]:
                up = (hdr["change"] or 0) > 0.05
                tk.Label(side, text=hdr["today_txt"], bg=C["card"], fg=C["mint"] if up else C["sub"],
                         font=F(11, "bold" if up else "normal"), anchor="w").pack(fill="x")
        else:
            self.lbl(f, "Connect your osu! account (Open full coach > Settings) to see your total pp and rank.", 11,
                     fg=C["sub"], wrap=False, wraplength=wl)
        # the map selected in osu! right now
        box = tk.Frame(f, bg=C["hi"], padx=p(14), pady=p(10), highlightthickness=max(1, p(2)),
                       highlightbackground=C["lav"], highlightcolor=C["lav"])
        box.pack(fill="x", pady=(p(10), 0))
        bw = wl - p(30)
        if lv is None:
            tk.Label(box, text="SELECTED MAP", bg=C["hi"], fg=C["lav"], font=F(10, "bold"), anchor="w").pack(fill="x")
            tk.Label(box, text=v["map_hint"], bg=C["hi"], fg=C["fg"], font=F(12), anchor="w", justify="left",
                     wraplength=bw).pack(fill="x", pady=(p(2), 0))
        else:
            top = tk.Frame(box, bg=C["hi"])
            top.pack(fill="x")
            tk.Label(top, text="SELECTED IN OSU!", bg=C["hi"], fg=C["lav"], font=F(10, "bold")).pack(side="left")
            if lv["confidence"]:
                tk.Label(top, text=f"{lv['confidence']} confidence", bg=CONF_COL.get(lv["confidence"], C["border"]),
                         fg=C["ink"], font=F(9, "bold"), padx=p(6)).pack(side="right")
            tk.Label(box, text=U.shorten(lv["full_name"], 70) or "this map", bg=C["hi"], fg=C["fg"],
                     font=F(12, "bold"), anchor="w", justify="left", wraplength=bw).pack(fill="x", pady=(p(2), 0))
            tk.Label(box, text=lv["title"], bg=C["hi"], fg=C["sub"], font=F(11), anchor="w").pack(fill="x")
            if lv["big"] and not (lv["status"] or "").startswith("not ranked"):
                br = tk.Frame(box, bg=C["hi"])
                br.pack(fill="x", pady=(p(4), 0))
                tk.Label(br, text=lv["big"], bg=C["hi"], fg=C["mint"], font=F(28, "bold")).pack(side="left")
                rs = tk.Frame(br, bg=C["hi"])
                rs.pack(side="left", padx=(p(12), 0))
                if lv["range"]:
                    tk.Label(rs, text=lv["range"], bg=C["hi"], fg=C["fg"], font=F(12, "bold"), anchor="w").pack(fill="x")
                if lv["fcss"]:
                    tk.Label(rs, text=lv["fcss"], bg=C["hi"], fg=C["sub"], font=F(11), anchor="w").pack(fill="x")
            else:
                tk.Label(box, text=lv["status"] or "working it out...", bg=C["hi"], fg=C["yellow"], font=F(13, "bold"),
                         anchor="w", justify="left", wraplength=bw).pack(fill="x", pady=(p(4), 0))
            if lv["pass"]:
                tk.Label(box, text=lv["pass"], bg=C["hi"], fg=C["fg"], font=F(12, "bold"), anchor="w").pack(fill="x")
            if lv["gain"]:
                good = (lv.get("gain_pass") or 0) >= 0.05
                tk.Label(box, text=lv["gain"], bg=C["hi"], fg=C["mint"] if good else C["sub"],
                         font=F(12, "bold" if good else "normal"), anchor="w", justify="left",
                         wraplength=bw).pack(fill="x")
            if lv["tip"]:
                tk.Label(box, text=lv["tip"], bg=C["hi"], fg=C["yellow"], font=F(12, "bold"), anchor="w",
                         justify="left", wraplength=bw).pack(fill="x", pady=(p(4), 0))
            if lv["basis"]:
                tk.Label(box, text=lv["basis"], bg=C["hi"], fg=C["dim"], font=F(10), anchor="w", justify="left",
                         wraplength=bw).pack(fill="x", pady=(p(2), 0))
        self.lbl(f, ms["line"], 11, fg=C["dim"], wrap=False, wraplength=wl, pady=(p(6), 0))
        # rank-up picks
        ph = tk.Frame(f, bg=C["card"])
        ph.pack(fill="x", pady=(p(10), p(2)))
        tk.Label(ph, text="Rank-up picks", bg=C["card"], fg=C["pink"], font=F(15, "bold")).pack(side="left")
        tk.Label(ph, text="most pp for you", bg=C["card"], fg=C["sub"], font=F(11)).pack(side="right")
        for pv in v["picks"]:
            row = tk.Frame(f, bg=C["card2"], padx=p(10), pady=p(5))
            row.pack(fill="x", pady=(p(4), 0))
            if pv["action"]:
                lab = "Copy search" if pv["action"][0] == "copy" else "Get map"
                self.btn(row, lab, lambda pv=pv: self.do_pick(pv), "small").pack(side="right", anchor="n",
                                                                                 padx=(p(8), 0))
            tx = tk.Frame(row, bg=C["card2"])
            tx.pack(side="left", fill="x", expand=True)
            tk.Label(tx, text=("★ " if pv["smash"] else "") + pv["short"], bg=C["card2"],
                     fg=C["yellow"] if pv["smash"] else C["fg"], font=F(11, "bold"), anchor="w",
                     justify="left", wraplength=wl - p(140)).pack(fill="x")
            tk.Label(tx, text=pv["line"], bg=C["card2"], fg=C["mint"] if (pv["gain"] or 0) >= 1 else C["sub"],
                     font=F(11), anchor="w", justify="left", wraplength=wl - p(140)).pack(fill="x")
        if any(pv["smash"] for pv in v["picks"]):
            self.lbl(f, "★ = good bet: better than even chance to pass, and among your biggest gains", 10, fg=C["dim"],
                     wrap=False, wraplength=wl, pady=(p(4), 0))
        if v["picks_note"]:
            self.lbl(f, v["picks_note"], 11, fg=C["sub"], wrap=False, wraplength=wl, pady=(p(4), 0))
        self.fb = tk.Label(f, text="Copy search, then paste it in osu! song select (Ctrl+V)" if v["picks"] else "",
                           bg=C["card"], fg=C["dim"], font=F(10), anchor="w")
        self.fb.pack(fill="x", pady=(p(4), 0))
        foot = tk.Frame(f, bg=C["card"])
        foot.pack(fill="x", pady=(p(8), 0))
        more = f"More picks ({v['n_picks']}) ›" if v["n_picks"] > len(v["picks"]) else "More about my picks ›"
        self.link(foot, more, lambda: (self._hook("touched"), self._hook("open_panel", "PP")), size=12)
        self.link(foot, "Open full coach (training) ›",
                  lambda: (self._hook("touched"), self._hook("open_panel", "Today")), fg=C["dim"], size=10,
                  side="right")

    # ------------------------------------------------------------ adaptive: header / debrief / session step
    def _head(self, f, title, right=None, back=False):
        p = self.px
        head = tk.Frame(f, bg=C["card"])
        head.pack(fill="x")
        close = tk.Label(head, text="✕", bg=C["card"], fg=C["dim"], font=F(14, "bold"), cursor="hand2", padx=p(4))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: (self._hook("touched"), self.hide()))
        close.bind("<Enter>", lambda e: close.configure(fg=C["pink"]))
        close.bind("<Leave>", lambda e: close.configure(fg=C["dim"]))
        tk.Label(head, text=title, bg=C["card"], fg=C["fg"], font=F(15, "bold")).pack(side="left")
        if right:
            tk.Label(head, text=right, bg=C["card"], fg=C["sub"], font=F(12)).pack(side="right", padx=(0, p(10)))
        if back:
            self.link(head, "‹ back", self.back, fg=C["dim"], size=10, side="right").pack_configure(padx=(0, p(8)))
        return head

    def _pending_line(self, f, wl):
        if self.pending_since and time.time() - self.pending_since < 60:
            self.lbl(f, "Looking at that try...", 12, "bold", fg=C["yellow"], wrap=False, wraplength=wl,
                     pady=(self.px(6), 0))
            self.state["pending"] = True

    NEXT_COLOR = {"faster": "mint", "next_step": "mint", "next_map": "mint", "retry": "pink", "slower": "blue",
                  "break": "yellow", "stop": "yellow"}

    def _debrief_content(self, f):
        p = self.px
        wl = p(self.W - 4)
        deb = self.debrief
        label, ck = U.verdict_of(deb)
        col = C.get(ck, C["lav"])
        verdict = txt(g(deb, "verdict")).lower()
        na = g(deb, "next_action") if isinstance(g(deb, "next_action"), dict) else {}
        typ = txt(g(na, "type")).lower()
        latest = U.is_latest_debrief(self.data, deb)
        big = (U.next_action_big(g(deb, "next_action"), self.data, deb) if latest else
               U.history_action(na) or U.next_action_big(g(deb, "next_action"), None, deb))
        headline = U.level_text(g(deb, "headline"))
        self.state = {"mode": "debrief", "verdict": verdict or None, "headline": headline, "next_big": big,
                      "next_type": typ or None, "level": self.level["text"] if self.level else None,
                      "free": verdict == "free-play"}
        when = U.ago(g(deb, "ts"))
        self._head(f, "How that went", right=when or None)
        if self.level:
            up = self.level["up"]
            box = tk.Frame(f, bg=C["mint"] if up else C["card2"], padx=p(12), pady=p(8))
            box.pack(fill="x", pady=(p(8), 0))
            tk.Label(box, text=("★  " if up else "") + self.level["text"], bg=box["bg"], fg=C["ink"] if up else C["fg"],
                     font=F(15, "bold"), anchor="w", justify="left", wraplength=wl - p(24)).pack(fill="x")
            tk.Label(box, text="Nice work - the coach just made it a bit harder." if up else
                     "A small step back so you can build it clean. That's normal - it moves up again soon.",
                     bg=box["bg"], fg=C["ink"] if up else C["sub"], font=F(11), anchor="w", justify="left",
                     wraplength=wl - p(24)).pack(fill="x")
        vr = tk.Frame(f, bg=C["card"])
        vr.pack(fill="x", pady=(p(10), 0))
        tk.Label(vr, text=label, bg=col, fg=C["ink"], font=F(14, "bold"), padx=p(12), pady=p(2)).pack(side="left")
        mp = txt(g(deb, "map"))
        if mp:
            pill = U.mod_pill(g(deb, "mods"), g(deb, "rate"))
            tk.Label(vr, text=U.shorten(mp, 60) + (f"\n{pill}" if pill else ""), bg=C["card"], fg=C["sub"],
                     font=F(10), anchor="w", justify="left", wraplength=wl - p(110)).pack(side="left", padx=(p(10), 0))
        if headline:
            self.lbl(f, headline, 15, "bold", wrap=False, wraplength=wl, pady=(p(8), 0))
        for o in U.strs(g(deb, "observations"))[:3]:
            self.lbl(f, "•  " + U.level_text(o), 12, fg=C["fg"], wrap=False, wraplength=wl, pady=(p(4), 0))
        vs, parts = U.compare_parts(g(deb, "compare"))
        if parts:
            cr = tk.Frame(f, bg=C["card"])
            cr.pack(fill="x", pady=(p(6), 0))
            tk.Label(cr, text=(f"vs {vs}:" if vs else "Compared with before:"), bg=C["card"], fg=C["dim"],
                     font=F(11)).pack(side="left")
            for lab, s, good in parts:
                tk.Label(cr, text=f"{lab} {s}", bg=C["card"], font=F(11, "bold"),
                         fg=C["mint"] if good else (C["peach"] if good is False else C["sub"])).pack(
                    side="left", padx=(p(8), 0))
        if verdict == "free-play":
            self.lbl(f, U.FREE_NOTE, 12, "bold", fg=C["lav"], wrap=False,
                     wraplength=wl, pady=(p(8), 0))
            cur = U.session_current(self.data)
            if cur and cur.get("map"):
                self.lbl(f, f"Today's step: {block_title(cur['block'])} on {U.map_name(cur['map'])}", 11,
                         fg=C["sub"], wrap=False, wraplength=wl)
        # the NEXT action, big
        bc = C.get(self.NEXT_COLOR.get(typ, "pink"), C["pink"])
        box = tk.Frame(f, bg=C["hi"], padx=p(14), pady=p(10), highlightthickness=max(1, p(2)), highlightbackground=bc,
                       highlightcolor=bc)
        box.pack(fill="x", pady=(p(12), 0))
        tk.Label(box, text="NEXT" if latest else "BACK THEN", bg=C["hi"], fg=bc, font=F(10, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(box, text=big or "Keep going", bg=C["hi"], fg=C["fg"], font=F(20 if latest else 14, "bold"),
                 anchor="w", justify="left", wraplength=wl - p(30)).pack(fill="x")
        detail = U.action_detail(na, deb, level=self.level) if latest else ""
        if detail and detail.lower() != big.lower():
            tk.Label(box, text=detail, bg=C["hi"], fg=C["fg"], font=F(12), anchor="w", justify="left",
                     wraplength=wl - p(30)).pack(fill="x", pady=(p(2), 0))
        if latest and typ in ("retry", "faster", "slower", "next_map", "next_step") and num(g(na, "rate")):
            target = self._rec_for_copy()
            same = txt(g(na, "md5")) in ("", txt(g(deb, "md5")))
            mods = txt(g(na, "mods")) or (txt(g(deb, "mods")) if same else txt(g(target, "mods")))
            r = num(g(na, "rate"))
            other = "".join(m for m in U.split_mods(mods) if m not in ("HT", "DC", "DT", "NC"))
            speed = "" if abs(r - 1.0) < 0.004 else ("HT" if r < 1 else "DT")   # lazer: custom speed on HT / DT
            mods = (speed + other) or "NM"
            tk.Label(box, text=U.mod_howto(mods, r), bg=C["hi"], fg=C["sub"], font=F(11), anchor="w",
                     justify="left", wraplength=wl - p(30)).pack(fill="x", pady=(p(4), 0))
            self.state["howto"] = U.mod_howto(mods, r)
        why = U.level_text(g(na, "why"))
        if why:
            tk.Label(box, text="Why: " + why, bg=C["hi"], fg=C["dim"], font=F(10), anchor="w", justify="left",
                     wraplength=wl - p(30)).pack(fill="x", pady=(p(4), 0))
        # buttons
        if typ in ("break", "stop"):
            self.lbl(f, "Stop for today if your wrist or hand hurts.", 11, fg=C["sub"], wrap=False, wraplength=wl,
                     pady=(p(8), 0))
            row = tk.Frame(f, bg=C["card"])
            row.pack(fill="x", pady=(p(10), 0))
            self.btn(row, "OK", lambda: (self._hook("touched"), self.show_next(), self.hide()), "primary").pack(
                side="left")
            self.link(row, "Next up ›", self.show_next, size=12, side="right")
            return
        rec = self._rec_for_copy()
        if rec is not None and U.search_text(rec):
            self.btn(f, "Copy song search", self.copy_search, "primary").pack(fill="x", pady=(p(10), p(2)))
            self.fb = tk.Label(f, text="Then paste it in osu! song select (Ctrl+V)", bg=C["card"], fg=C["sub"],
                               font=F(11), anchor="w")
            self.fb.pack(fill="x")
        row = tk.Frame(f, bg=C["card"])
        row.pack(fill="x", pady=(p(10), 0))
        self.btn(row, "Next up  ›", self.show_next, "go").pack(side="left")
        self.link(row, "Open full coach ›", lambda: (self._hook("touched"), self._hook("open_panel")), size=11,
                  side="right")

    def _sdots(self, f, steps, i):
        p = self.px
        n = len(steps)
        if not n or n > 16:
            return
        cv = tk.Canvas(f, height=p(16), width=p(self.W), bg=C["card"], highlightthickness=0, bd=0)
        cv.pack(fill="x", pady=(p(4), 0))
        x = p(7)
        for k, s in enumerate(steps):
            st = s["state"]
            r = p(6) if k == i else p(4.5)
            fill = {"done": C["mint"], "skipped": C["dim"], "now": C["pink"]}.get(st, "")
            out = {"done": C["mint"], "skipped": C["dim"], "now": C["pink"]}.get(st, C["dim"])
            cv.create_oval(x - r, p(8) - r, x + r, p(8) + r, fill=fill, outline=out, width=p(1.5))
            x += p(20)

    def _session_content(self, f):
        p = self.px
        wl = p(self.W - 4)
        steps = U.session_steps(self.data)
        n = len(steps)
        i = U.session_index(self.data)
        cur = U.session_current(self.data)
        self.state = {"mode": "next", "session": True, "step": i, "n": n}
        title = "Next up" if cur else "Today's plan"
        if cur and cur["map"] is None and "break" in cur["block"].lower():
            title = "Break time"
        self._head(f, title, right=(f"Step {i + 1} of {n}" if i < n else f"{n} of {n} done"), back=i > 0)
        self._sdots(f, steps, i)
        self._pending_line(f, wl)
        if cur is None:
            self._all_done(f, wl)
            return
        tr = tk.Frame(f, bg=C["card"])
        tr.pack(fill="x", pady=(p(8), 0))
        tk.Label(tr, text=block_title(cur["block"]), bg=C["card"], fg=C["pink"], font=F(16, "bold"),
                 anchor="w").pack(side="left")
        if cur["minutes"] and "min" not in cur["block"].lower():
            tk.Label(tr, text=f"  ·  {cur['minutes']:.0f} min", bg=C["card"], fg=C["sub"], font=F(12)).pack(
                side="left")
        rec = cur["map"]
        self.state.update(block=cur["block"], map=U.map_name(rec) if rec else None, rate=cur["rate"],
                          mods=cur["mods"], goal=cur["goal"], passes=cur["passes"], needed=cur["needed"],
                          attempt_n=cur["attempt_n"])
        if cur["goal"]:
            self.lbl(f, "Goal: " + U.plain(cur["goal"]), 13, "bold", wrap=False, wraplength=wl, pady=(p(4), 0))
        prog = U.progress_words(cur)
        if prog:
            self.lbl(f, prog, 12, "bold", fg=C["mint"] if cur["passes"] else C["sub"], wrap=False, wraplength=wl,
                     pady=(p(2), 0))
            self.state["progress"] = prog
        if cur["exit_rule"] and cur["exit_rule"] != "-":
            self.lbl(f, "This step: " + U.plain(cur["exit_rule"]), 10, fg=C["dim"], wrap=False, wraplength=wl)
        if rec is None:
            self.lbl(f, "Take it easy for a few minutes." if not cur["goal"] else "", 12, fg=C["fg"], wrap=False,
                     wraplength=wl, pady=(p(4), 0))
        else:
            self.lbl(f, U.map_name(rec), 13, "bold", wrap=False, wraplength=wl, pady=(p(8), p(4)))
            mr = tk.Frame(f, bg=C["card"])
            mr.pack(fill="x", pady=(p(2), p(2)))
            mods, rate = cur["mods"] or txt(g(rec, "mods")), cur["rate"] or num(g(rec, "rate"))
            tk.Label(mr, text=U.mod_pill(mods, rate), bg=mod_color(mods, rate), fg=C["ink"], font=F(14, "bold"),
                     padx=p(12), pady=p(3)).pack(side="left", anchor="n")
            tk.Label(mr, text=U.mod_howto(mods, rate) + bpm_note(dict(rec, mods=mods, rate=rate)), bg=C["card"],
                     fg=C["sub"], font=F(11), justify="left", anchor="w", wraplength=p(self.W - 150)).pack(
                side="left", padx=(p(10), 0))
            self.btn(f, "Copy song search", self.copy_search, "primary").pack(fill="x", pady=(p(10), p(2)))
            self.fb = tk.Label(f, text="Then paste it in osu! song select (Ctrl+V)", bg=C["card"], fg=C["sub"],
                               font=F(11), anchor="w")
            self.fb.pack(fill="x")
        last = U.step_debrief(self.data, cur)
        ts = num(g(last, "ts"))
        if last and (ts is None or time.time() - ts < 3 * 3600):
            lab, ck = U.verdict_of(last)
            strip = tk.Frame(f, bg=C["card2"], padx=p(10), pady=p(6))
            strip.pack(fill="x", pady=(p(10), 0))
            tk.Label(strip, text=lab, bg=C.get(ck, C["lav"]), fg=C["ink"], font=F(10, "bold"), padx=p(6)).pack(
                side="left", anchor="n")
            hl = U.shorten(U.plain(txt(g(last, "headline"), "Last try")), 70)
            tk.Label(strip, text="Last try: " + hl, bg=C["card2"], fg=C["fg"], font=F(11), anchor="w",
                     justify="left", wraplength=p(self.W - 170)).pack(side="left", padx=(p(8), 0))
            lk = tk.Label(strip, text="details ›", bg=C["card2"], fg=C["lav"], font=F(11, "bold"), cursor="hand2")
            lk.pack(side="right", anchor="n")
            lk.bind("<Button-1>", lambda e, d=last: (self._hook("touched"), self.show_debrief(
                d, None, U.level_change(d, g(self.data, "tracks")))))
            self.state["last_try"] = hl
        row = tk.Frame(f, bg=C["card"])
        row.pack(fill="x", pady=(p(8), 0))
        self.btn(row, "Done - next step  ›", self.done, "go").pack(side="left")
        self.btn(row, "Skip step", self.skip, "small").pack(side="left", padx=(p(8), 0))
        foot = tk.Frame(f, bg=C["card"])
        foot.pack(fill="x", pady=(p(10), 0))
        why = U.plain(txt(g(rec, "reason"))) if rec is not None else ""
        if why:
            self.link(foot, ("▾ Why this map?" if self.why_open else "▸ Why this map?"), self.toggle_why, size=11)
        self.link(foot, "Open full coach ›", lambda: (self._hook("touched"), self._hook("open_panel")),
                  size=11, side="right")
        if why and self.why_open:
            self.lbl(f, why, 11, fg=C["sub"], wrap=False, wraplength=wl, pady=(p(6), 0))


# ============================================================================
# the full panel
# ============================================================================

class CoachPanel(_Widgets):
    def __init__(self, root, scale, settings, on_refresh=None, name=U.PET_NAME):
        self.root = root
        self.s = scale
        self.settings = settings
        self.on_refresh = on_refresh
        self.name = name
        self.data = {}
        self.top = None
        self.dirty = True
        self.errors = []          # (tab, traceback) - read by --selftest
        self.status_text = ""
        self._style_done = False
        self.game_in_front = None   # callable set by the pet
        self.prev_fg = None         # window that had the foreground before the panel took it
        self.pp_data = {}
        self.recs = {}              # pp_recs.json (CONTRACT_PP2.md)
        self.report = None          # pp_model.model_report() (None: pp_recs.json's copy)
        self.rows = []              # pp_predictions.jsonl rows (U.read_pred_log)
        self.pp_sort = txt(settings.get("pp_sort")) if settings is not None else ""
        if self.pp_sort not in U.PICK_SORTS:
            self.pp_sort = "gain"
        self.options = []           # [(label, help, BooleanVar, command)] set by the pet
        self.expanders = {}         # tab -> [ {"open": bool, "toggle": fn} ]
        # callbacks set by the pet (all optional): setup_saved(), api_refresh(), tosu_check(cb), live_note(),
        # plan_changed(), topmost(top), before_show(top)
        self.hooks = {}
        self._cur_tab = None
        self.never_focus = False    # --selftest: never take focus from whatever is in front
        self.anchor = None
        self.frame_h = self.frame_w = None   # measured window frame (title bar + borders), physical px

    def _hook(self, name, *a):
        fn = self.hooks.get(name)
        if fn:
            try:
                return fn(*a)
            except Exception:
                U.log.error("panel hook %s failed:\n%s", name, traceback.format_exc())
        return None

    # ------------------------------------------------------------------ window
    def _style(self):
        if self._style_done:
            return
        self._style_done = True
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        p = self.px
        st.configure("Pet.TNotebook", background=C["bg"], borderwidth=0, tabmargins=(p(14), p(4), p(14), 0),
                     bordercolor=C["bg"], lightcolor=C["bg"], darkcolor=C["bg"])
        st.configure("Pet.TNotebook.Tab", background=C["card"], foreground=C["sub"], borderwidth=0,
                     padding=(p(22), p(9)), font=F(13, "bold"), lightcolor=C["card"], bordercolor=C["bg"])
        st.map("Pet.TNotebook.Tab", background=[("selected", C["pink"]), ("active", C["card2"])],
               foreground=[("selected", C["ink"]), ("active", C["fg"])], lightcolor=[("selected", C["pink"])],
               padding=[("selected", (p(22), p(9)))], expand=[("selected", (0, 0, 0, 0))])
        st.configure("Pet.Vertical.TScrollbar", background=C["card2"], troughcolor=C["bg"], bordercolor=C["bg"],
                     arrowcolor=C["sub"], lightcolor=C["card2"], darkcolor=C["card2"], gripcount=0,
                     arrowsize=p(14))
        st.map("Pet.Vertical.TScrollbar", background=[("active", C["border"])])

    def _build(self):
        self._style()
        top = self.top = tk.Toplevel(self.root)
        top.withdraw()
        top.title(f"{self.name} - your osu! coach")
        top.configure(bg=C["bg"])
        top.minsize(self.px(640), self.px(480))
        top.protocol("WM_DELETE_WINDOW", self.hide)
        top.bind("<Escape>", lambda e: self.hide())
        top.bind("<Enter>", lambda e: self._hook("touched"), add="+")   # using the panel: don't auto-close it
        top.bind("<ButtonPress>", lambda e: self._hook("touched"), add="+")
        top.bind_all("<MouseWheel>", self._wheel, add="+")
        p = self.px
        head = tk.Frame(top, bg=C["bg"])
        head.pack(fill="x", padx=p(18), pady=(p(14), p(6)))
        self.face_lbl = tk.Label(head, text=MOOD_FACE["neutral"], bg=C["bg"], fg=C["pink"], font=F(20, "bold"))
        self.face_lbl.pack(side="left")
        tb = tk.Frame(head, bg=C["bg"])
        tb.pack(side="left", fill="x", expand=True, padx=(p(14), 0))
        tk.Label(tb, text=f"{self.name} - your osu! coach", bg=C["bg"], fg=C["fg"], font=F(16, "bold"),
                 anchor="w").pack(fill="x")
        self.status_lbl = tk.Label(tb, text="", bg=C["bg"], fg=C["dim"], font=F(10), anchor="w", justify="left")
        self.status_lbl.pack(fill="x")
        self.btn(head, "Close", self.hide, "normal").pack(side="right", padx=(p(8), 0))
        self.btn(head, "Refresh", self._refresh_clicked, "normal").pack(side="right")
        self.nb = ttk.Notebook(top, style="Pet.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=p(8), pady=(p(4), p(10)))
        self.tabs = {}
        for name in TAB_NAMES:
            fr = ScrollFrame(self.nb, self.s)
            self.nb.add(fr, text=name)
            self.tabs[name] = fr
        self._set_status_lbl()

    def _wheel(self, e):
        w = e.widget
        try:
            while w is not None and not isinstance(w, str):
                sf = getattr(w, "_pet_scroll", None)
                if sf is not None:
                    sf.wheel(e.delta)
                    return
                w = w.master
        except Exception:
            pass

    def _refresh_clicked(self):
        if self.on_refresh:
            self.on_refresh()

    # ------------------------------------------------------------ public API
    def is_open(self):
        try:
            return self.top is not None and self.top.winfo_exists() and self.top.state() in ("normal", "zoomed")
        except tk.TclError:
            return False

    def set_data(self, data):
        self.data = data if isinstance(data, dict) else {}
        U.set_aim_comfort(self.data)
        self.dirty = True
        if self.is_open():
            self.refresh()

    def set_pp_data(self, ppd):
        ppd = ppd if isinstance(ppd, dict) else {}
        if ppd == self.pp_data:
            return
        self.pp_data = ppd
        self.dirty = True
        if self.is_open():
            self.refresh()

    def set_pp_extras(self, recs=QuickCard._UNSET, report=QuickCard._UNSET, rows=QuickCard._UNSET):
        """pp_recs.json / model report / prediction log changed: rebuild just the PP tab (if it changed)."""
        changed = False
        for k, v in (("recs", recs), ("report", report), ("rows", rows)):
            if v is QuickCard._UNSET:
                continue
            if k == "recs" and not isinstance(v, dict):
                v = {}
            if k == "rows" and not isinstance(v, list):
                v = []
            if getattr(self, k) != v:
                setattr(self, k, v)
                changed = True
        if changed:
            self.refresh_tab("PP")
        return changed

    def set_pp_sort(self, key):
        if key not in U.PICK_SORTS:
            return
        self.pp_sort = key
        try:
            self.settings.set("pp_sort", key)
        except Exception:  # noqa: BLE001
            pass
        self.refresh_tab("PP", keep_pos=False)

    def select_tab_name(self, name):
        name = TAB_ALIASES.get(name, name)
        if self.top is not None and name in TAB_NAMES:
            self.nb.select(TAB_NAMES.index(name))

    def select_tab(self, i):
        if self.top is not None:
            self.nb.select(i)

    def set_status(self, text):
        self.status_text = text or ""
        self._set_status_lbl()

    def _set_status_lbl(self):
        if self.top is None:
            return
        d = self.data
        bits = []
        if U.to_dt(g(d, "generated_at")):
            bits.append(f"Updated {U.ago(g(d, 'generated_at'))}")
        if num(g(d, "n_plays")) is not None:
            bits.append(f"looked at your last {int(num(g(d, 'n_plays')))} plays")
        if self.status_text:
            bits.append(self.status_text)
        try:
            self.status_lbl.configure(text="  ·  ".join(bits) or "No analysis yet")
        except tk.TclError:
            pass

    def _game_in_front(self):
        try:
            return bool(self.game_in_front and self.game_in_front())
        except Exception:
            return False

    def toggle(self, anchor=None):
        if self.is_open():
            self.hide()
        else:
            self.show(anchor)

    def show(self, anchor=None, focus=True):
        if self.top is None or not self.top.winfo_exists():
            self._build()
            self.dirty = True
        if self.dirty:
            self.refresh()
        if anchor:
            self.anchor = anchor
            self._place(anchor)
        elif not self.top.winfo_ismapped():
            self.top.geometry(f"{self.px(980)}x{self.px(840)}")
        fg = U.foreign_foreground()
        if fg:
            self.prev_fg = fg  # the game (or whatever had focus) gets it back when the panel closes
        self._hook("before_show", self.top)
        self.top.deiconify()
        self.top.attributes("-topmost", True)  # stays above the game while open (keep_on_top re-asserts it)
        self.top.lift()
        if anchor:
            self._fit_frame()
        self._hook("after_show")
        if focus and not self.never_focus:
            try:
                self.top.focus_force()
            except tk.TclError:
                pass

    def _place(self, anchor):
        """Beside the pet (left of it, else right of it), bottom edge level with the pet's feet, never over the
        pet itself, and inside the work area including the title bar and frame."""
        p = self.px
        ax, ay, aw, ah, wa = anchor
        l, t, r, b = wa
        fh = self.frame_h or p(31) + p(8)      # title bar + borders (measured after the first show)
        fw = self.frame_w or p(8)
        w = min(p(980), r - l - 16 - fw)
        h = min(p(840), b - t - 16 - fh)
        gap = p(10)
        x = ax - w - fw - gap
        if x < l + 8:
            x = ax + aw + gap                   # no room on the left: right of the pet
            if x + w + fw > r - 8:
                x = max(l + 8, min(ax - w - fw - gap, r - w - fw - 8))   # neither fits: as far left as possible
        y = ay + ah - h - fh
        x = max(l + 8, min(int(x), r - w - fw - 8))
        y = max(t + 8, min(int(y), b - h - fh - 8))
        self.top.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def _fit_frame(self):
        """Measure the real title bar / frame once and re-place if the first guess was off."""
        try:
            self.top.update_idletasks()
            fh = (self.top.winfo_rooty() - self.top.winfo_y()) + (self.top.winfo_rootx() - self.top.winfo_x())
            fw = 2 * (self.top.winfo_rootx() - self.top.winfo_x())
        except tk.TclError:
            return
        if fh > 0 and (abs(fh - (self.frame_h or 0)) > 2 or abs(fw - (self.frame_w or 0)) > 2):
            self.frame_h, self.frame_w = fh, max(0, fw)
            if self.anchor:
                self._place(self.anchor)

    def hide(self):
        if self.top is not None:
            try:
                had_fg = self._is_foreground()
                self.top.attributes("-topmost", False)
                self.top.withdraw()
                if had_fg:
                    U.restore_foreground(self.prev_fg)
            except tk.TclError:
                pass

    def _is_foreground(self):
        try:
            h = U.foreground_hwnd()
            w = self.top.focus_displayof()
            return bool(h) and U.hwnd_pid(h) == os.getpid() and w is not None and w.winfo_toplevel() == self.top
        except (tk.TclError, KeyError):
            return False

    def keep_on_top(self):
        """Called every 2 s: while open, the panel stays above osu! (never steals focus)."""
        if self.is_open():
            self._hook("topmost", self.top)

    # ------------------------------------------------------------ build tabs
    def refresh(self):
        if self.top is None:
            return
        self.dirty = False
        mood = txt(g(self.data, "mood"), "neutral")
        self.face_lbl.configure(text=MOOD_FACE.get(mood, MOOD_FACE["neutral"]))
        self._set_status_lbl()
        for name in TAB_NAMES:
            self._build_tab(name)

    def refresh_today(self):
        """Rebuild just the Today tab (after ticking a step), keeping the scroll position."""
        self.refresh_tab("Today")

    def refresh_tab(self, name, keep_pos=True):
        """Rebuild one tab now if the window is open (keeping the scroll position), else on the next show."""
        if self.top is None:
            return
        if not self.is_open():
            self.dirty = True
            return
        sf = self.tabs[name]
        pos = sf.cv.yview()[0] if keep_pos else 0.0
        self._build_tab(name)
        sf.update_idletasks()
        sf.update_region()
        sf.cv.yview_moveto(pos)

    def _build_tab(self, name):
        fr = self.tabs[name]
        fn = {"PP": self._pp_tab, "Today": self._today, "Progress": self._progress, "Tips": self._tips,
              "Settings": self._settings}[name]
        self._cur_tab = name
        self.expanders[name] = []
        try:
            fr.clear()
            fn(fr.inner)
        except Exception:
            tb = traceback.format_exc()
            self.errors.append((name, tb))
            U.log.error("panel tab %s failed:\n%s", name, tb)
            try:
                tk.Label(fr.inner, text=f"Oops - I couldn't draw this tab ({name}). Details are in pet_log.txt.",
                         bg=C["bg"], fg=C["red"], font=F(12)).pack(anchor="w", pady=self.px(16))
            except Exception:
                pass

    def expand_all(self):
        """--selftest: open every expander (nested ones appear as their parents open). -> how many."""
        n = 0
        for _ in range(8):
            todo = [e for lst in self.expanders.values() for e in lst if not e["open"]]
            if not todo:
                break
            for e in todo:
                e["toggle"]()
                n += 1
            self.root.update_idletasks()
        return n

    # ------------------------------------------------------------ widgets
    def card(self, parent, stripe=None, bg=None, pady=None):
        bg = bg or C["card"]
        outer = tk.Frame(parent, bg=C["border"])
        outer.pack(fill="x", pady=(0, self.px(12) if pady is None else pady))
        if stripe:
            tk.Frame(outer, bg=stripe, width=self.px(7)).pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=bg, padx=self.px(16), pady=self.px(12))
        inner.pack(side="left", fill="both", expand=True, padx=(0 if stripe else 1, 1), pady=1)
        return inner

    def heading(self, parent, text, sub=None, color=None, top=18):
        f = tk.Frame(parent, bg=parent["bg"])
        f.pack(fill="x", pady=(self.px(top), self.px(8)))
        self.lbl(f, text, 16, "bold", fg=color or C["pink"])
        if sub:
            self.lbl(f, sub, 11, fg=C["dim"], pady=(self.px(2), 0))
        return f

    def empty(self, parent, text, button=None):
        c = self.card(parent, C["border"])
        self.lbl(c, text, 12, fg=C["sub"])
        if button:
            self.btn(c, button[0], button[1], "go").pack(anchor="w", pady=(self.px(8), 0))

    def expander(self, parent, title, build, opened=False, size=12, fg=None, pady=(4, 0)):
        """'▸ title' toggles a body built on first open."""
        wrap = tk.Frame(parent, bg=parent["bg"])
        wrap.pack(fill="x", pady=(self.px(pady[0]), self.px(pady[1])))
        head = tk.Label(wrap, text="▸  " + title, bg=parent["bg"], fg=fg or C["lav"], font=F(size, "bold"),
                        cursor="hand2", anchor="w", justify="left")
        head.pack(fill="x", anchor="w")
        self.wrap(head)
        body = tk.Frame(wrap, bg=parent["bg"])
        st = {"open": False, "built": False}

        def toggle(_e=None):
            if not st["built"]:
                st["built"] = True
                try:
                    build(body)
                except Exception:
                    tb = traceback.format_exc()
                    self.errors.append((f"{self._cur_tab}/{title}", tb))
                    U.log.error("expander %s failed:\n%s", title, tb)
                    self.lbl(body, "Oops - couldn't show this. Details are in pet_log.txt.", 11, fg=C["red"])
            st["open"] = not st["open"]
            if st["open"]:
                body.pack(fill="x", pady=(self.px(6), 0))
            else:
                body.pack_forget()
            head.configure(text=("▾  " if st["open"] else "▸  ") + title)
        head.bind("<Button-1>", toggle)
        head.bind("<Enter>", lambda e: head.configure(fg=C["pink"]))
        head.bind("<Leave>", lambda e: head.configure(fg=fg or C["lav"]))
        st["toggle"] = toggle
        self.expanders.setdefault(self._cur_tab, []).append(st)
        if opened:
            toggle()
        return body

    def more_text(self, parent, text, size=12, fg=None, limit=180, pady=0):
        """Short version + 'More' link that shows the full text."""
        text = " ".join(txt(text).split())
        if len(text) <= limit:
            return self.lbl(parent, text, size, fg=fg, pady=pady)
        short = U.shorten(text, limit)
        l = self.lbl(parent, short, size, fg=fg, pady=pady)
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill="x")
        st = {"open": False}
        lk = self.link(row, "More ▸", lambda: toggle(), size=max(10, size - 1))

        def toggle():
            st["open"] = not st["open"]
            l.configure(text=text if st["open"] else short)
            lk.configure(text="Less ▴" if st["open"] else "More ▸")
        st["toggle"] = toggle
        self.expanders.setdefault(self._cur_tab, []).append(st)
        return l

    def copy_button(self, parent, text, label="Copy song search", kind="normal"):
        b = self.btn(parent, label, None, kind)

        def do():
            if self.copy(text):
                b.configure(text="Copied! Paste it in song select", bg=C["mint"], fg=C["ink"])
                b.after(2500, lambda: b.winfo_exists() and b.configure(
                    text=label, bg=C["pink"] if kind == "primary" else C["card2"],
                    fg=C["ink"] if kind == "primary" else C["fg"]))
        b.configure(command=do)
        return b

    def bar(self, parent, frac, color, height=10):
        cv = tk.Canvas(parent, height=self.px(height + 4), bg=parent["bg"], highlightthickness=0, bd=0)
        cv.pack(fill="x", pady=(self.px(6), self.px(2)))

        def draw(_e=None):
            cv.delete("all")
            w = cv.winfo_width()
            h = self.px(height)
            y = self.px(height / 2 + 2)
            r = h / 2
            if w < 20:
                return
            cv.create_line(r + 1, y, w - r - 1, y, width=h, fill=C["card2"], capstyle="round")
            if frac > 0.005:
                cv.create_line(r + 1, y, r + 1 + max(0.5, (w - 2 * r - 2) * min(1, frac)), y, width=h, fill=color,
                               capstyle="round")
        cv.bind("<Configure>", draw)
        return cv

    def map_row(self, parent, rec, why=True):
        """♪ Artist - Title [Version]  [Copy search] / [HT 0.80x] how-to / goal / Why this map?"""
        p = self.px
        bg = parent["bg"]
        box = tk.Frame(parent, bg=bg)
        box.pack(fill="x", pady=(p(6), p(2)))
        self.lbl(box, "♪  " + U.map_name(rec), 12, "bold")
        mr = tk.Frame(box, bg=bg)
        mr.pack(fill="x", pady=(p(3), 0))
        search = U.search_text(rec)
        if search:
            self.copy_button(mr, search).pack(side="right", padx=(p(10), 0))
        self.chip(mr,U.mod_pill(g(rec, "mods"), g(rec, "rate")), mod_color(g(rec, "mods"), g(rec, "rate")), size=11)
        extra = []
        bpm, rate = num(g(rec, "bpm")), num(g(rec, "rate"))
        if bpm:
            extra.append(f"≈{bpm:.0f} BPM at this speed" if U._rate_of(rate) else f"{bpm:.0f} BPM")
        if num(g(rec, "length_s")):
            extra.append(U.fmt_mmss(sec=g(rec, "length_s")) + " long")
        if num(g(rec, "stars")):
            extra.append(f"{num(g(rec, 'stars')):.2f}★")
        self.lbl(mr, U.mod_howto(g(rec, "mods"), rate) + ("   ·   " + ", ".join(extra) if extra else ""), 11,
                 fg=C["sub"], side="left")
        if txt(g(rec, "goal")):
            self.lbl(box, "Goal on this map: " + U.plain(g(rec, "goal")), 11, fg=C["mint"], pady=(p(3), 0))
        if why and (txt(g(rec, "reason")) or U.pos_int(g(rec, "beatmap_id"))):
            def build(b, r=rec):
                if txt(g(r, "reason")):
                    self.lbl(b, U.plain(g(r, "reason")), 11, fg=C["sub"])
                bid = U.pos_int(g(r, "beatmap_id"))
                if bid:
                    self.btn(b, "Open on osu! website", lambda: webbrowser.open(f"https://osu.ppy.sh/b/{bid}"),
                             "small").pack(anchor="w", pady=(p(4), 0))
            self.expander(box, "Why this map?", build, size=10, pady=(2, 0))
        return box

    # ============================================================ TODAY
    def _scmd(self, cmd):
        """Today tab buttons in an adaptive session -> the pet (session_cmd.json + engine run)."""
        if self.hooks.get("session_cmd"):
            self._hook("session_cmd", cmd)
        else:
            self.data = U.session_apply_cmd(self.data, cmd)
            self.refresh_today()

    def _debrief_card(self, parent, deb, big=False):
        p = self.px
        lab, ck = U.verdict_of(deb)
        col = C.get(ck, C["lav"])
        c = self.card(parent, col, pady=p(10 if big else 6))
        top = tk.Frame(c, bg=C["card"])
        top.pack(fill="x")
        self.chip(top, lab, col, size=12 if big else 10)
        when = U.ago(g(deb, "ts"))
        if when:
            tk.Label(top, text=when, bg=C["card"], fg=C["dim"], font=F(10)).pack(side="right")
        hl = U.level_text(g(deb, "headline"))
        self.lbl(top, hl or "Try looked at", 14 if big else 12, "bold", side="left")
        mp = txt(g(deb, "map"))
        if mp:
            self.lbl(c, mp + "   " + U.mod_pill(g(deb, "mods"), g(deb, "rate")), 10, fg=C["dim"])
        lc = U.level_change(deb, g(self.data, "tracks"))
        if lc:
            self.lbl(c, ("★ " if lc["up"] else "") + lc["text"], 12 if big else 11, "bold",
                     fg=C["mint"] if lc["up"] else C["sub"], pady=(p(2), 0))
        if big:
            for o in U.strs(g(deb, "observations"))[:3]:
                self.lbl(c, "•  " + U.level_text(o), 12, pady=(p(3), 0))
            vs, parts = U.compare_parts(g(deb, "compare"))
            if parts:
                self.lbl(c, (f"vs {vs}:  " if vs else "") + "   ".join(f"{a} {s}" for a, s, _ in parts), 11,
                         fg=C["sub"], pady=(p(4), 0))
            if txt(g(deb, "verdict")).lower() == "free-play":
                self.lbl(c, U.FREE_NOTE, 11, "bold", fg=C["lav"], pady=(p(4), 0))
        na = g(deb, "next_action")
        if not U.is_latest_debrief(self.data, deb):
            line = U.history_action(na)
            if line:
                self.lbl(c, line, 13 if big else 11, fg=C["sub"], pady=(p(4), 0))
            return c
        big_next = U.next_action_big(na, self.data, deb)
        if big_next:
            detail = U.action_detail(na, deb, level=lc)
            line = "Next: " + big_next + (f"  -  {detail}" if big and detail and detail.lower() != big_next.lower()
                                          else "")
            self.lbl(c, line, 13 if big else 11, "bold", fg=C["pink"], pady=(p(4), 0))
        return c

    def _today_session(self, f):
        p = self.px
        d = self.data
        steps = U.session_steps(d)
        n = len(steps)
        i = U.session_index(d)
        cur = U.session_current(d)
        mood = txt(g(d, "mood"), "neutral")
        hc = self.card(f, C["pink"])
        self.lbl(hc, MOOD_LINE.get(mood, MOOD_LINE["neutral"]), 11, fg=C["dim"])
        hero = U.today_hero(d)
        if hero:
            self.lbl(hc, hero["text"], 17, "bold", fg=C["pink"], pady=(p(2), 0))
            if hero.get("rec") is not None and U.search_text(hero["rec"]):
                self.copy_button(hc, U.search_text(hero["rec"]), kind="primary").pack(anchor="w", pady=(p(6), 0))
            self.lbl(hc, U.plain(txt(g(d, "headline"), "")), 12, fg=C["sub"], pady=(p(4), 0))
        else:
            self.lbl(hc, U.plain(txt(g(d, "headline"), "Here's your plan for today.")), 16, "bold", pady=(p(2), 0))
        total = sum(s["minutes"] or 0 for s in steps)
        done = sum(1 for s in steps if s["state"] in ("done", "skipped"))
        msg = (f"All {n} steps done - great session!" if i >= n else
               f"Step {i + 1} of {n}  ·  {done} done" + (f"  ·  about {total:.0f} min in total" if total else ""))
        self.lbl(hc, msg, 12, fg=C["mint"] if i >= n else C["sub"], pady=(p(8), 0))
        self.bar(hc, done / n if n else 0, C["mint"])

        changes = U.strs(g(d, "changes"))[:6]
        if changes:
            self.heading(f, "Since last time", "What I changed because of your last plays.")
            c = self.card(f, C["lav"])
            for ch in changes:
                self.lbl(c, "•  " + U.level_text(ch), 12, pady=(p(2), 0))

        last = U.step_debrief(d, cur) if cur else None
        if last is None and isinstance(g(d, "last_debrief"), dict):
            last = g(d, "last_debrief")
        if last is not None:
            self.heading(f, "Your last try" + (" on this step" if cur and last is U.step_debrief(d, cur) else ""))
            self._debrief_card(f, last, big=True)

        self.heading(f, "Today's plan", "Each step adapts to you: pass it and it gets a bit harder, miss the goal "
                     "and it gets easier. Press Done when you want to move on.")
        for s in steps:
            self._sstep_card(f, s, s["i"] == i, cur if s["i"] == i else None, s["i"] == i - 1)
        self._more_maps(f, [{"picks": [s["map"]] if s["map"] else []} for s in steps])

    def _sstep_card(self, f, s, is_cur, cur, is_prev):
        p = self.px
        st = s["state"]
        done = st in ("done", "skipped")
        bg = C["hi"] if is_cur else C["card"]
        stripe = C["pink"] if is_cur else (C["mint"] if st == "done" else (C["dim"] if st == "skipped" else
                                                                          C["border"]))
        c = self.card(f, stripe, bg=bg)
        top = tk.Frame(c, bg=bg)
        top.pack(fill="x")
        if is_cur:
            self.btn(top, "Skip", lambda: self._scmd("skip"), "small").pack(side="right", padx=(p(8), 0))
            self.btn(top, "Done ✓", lambda: self._scmd("done"), "go").pack(side="right")
            self.chip(top, "NOW", C["pink"], size=11)
        elif is_prev and done:
            self.btn(top, "Undo", lambda: self._scmd("back"), "small").pack(side="right")
        mark = "✓  " if st == "done" else ("–  " if st == "skipped" else f"{s['i'] + 1}.  ")
        mins = f"  ·  {s['minutes']:.0f} min" if s["minutes"] and "min" not in s["block"].lower() else ""
        self.lbl(top, mark + block_title(s["block"]) + mins, 14, "bold",
                 fg=C["dim"] if done else (C["pink"] if is_cur else C["fg"]), side="left", wrap=False)
        info = cur if cur else s
        prog = U.progress_words(info, with_level=bool(cur))
        bits = [prog] if prog else []
        if not done and info.get("exit_rule") and info["exit_rule"] != "-":
            bits.append(U.plain(info["exit_rule"]))
        if st == "skipped":
            bits.append("skipped")
        elif st == "done":
            bits.append("done")
        if bits:
            self.lbl(c, "  ·  ".join(bits), 11, "bold", fg=C["mint"] if st == "done" else C["sub"], pady=(p(2), 0))
        if done:
            return
        goal = info.get("goal")
        if goal:
            self.lbl(c, "Goal: " + U.plain(goal), 12 if is_cur else 11, fg=C["fg"] if is_cur else C["sub"],
                     pady=(p(4), 0))
        rec = info.get("map")
        if not isinstance(rec, dict):
            return
        shown = dict(rec, mods=info.get("mods") or g(rec, "mods"), rate=info.get("rate") or g(rec, "rate"), goal="")
        if is_cur:
            self.map_row(c, shown)
        else:
            body = self.expander(c, "Show the map", lambda b, r=shown: self.map_row(b, r), size=11, pady=(6, 0))
            body.configure(bg=bg)

    def _today(self, f):
        if U.session_of(self.data):
            return self._today_session(f)
        p = self.px
        d = self.data
        steps = U.plan_steps(d)
        day = U.today()
        cur = U.current_step(steps, self.settings, day)
        n = len(steps)
        mood = txt(g(d, "mood"), "neutral")
        hc = self.card(f, C["pink"])
        if not n:
            self.lbl(hc, "No plan yet", 16, "bold")
            self.lbl(hc, "I need to look at your replays first. Play a map in osu!, or press the button below - the "
                         "first look takes a minute or two.", 12, fg=C["sub"], pady=(p(4), 0))
            self.btn(hc, "Look at my replays now", self._refresh_clicked, "go").pack(anchor="w", pady=(p(10), 0))
            self._more_maps(f, steps)
            return
        self.lbl(hc, MOOD_LINE.get(mood, MOOD_LINE["neutral"]), 11, fg=C["dim"])
        self.lbl(hc, U.plain(txt(g(d, "headline"), "Here's your plan for today.")), 16, "bold", pady=(p(2), 0))
        if n:
            total = sum(s["minutes"] or 0 for s in steps)
            done = sum(1 for s in steps if self.settings.check_get(day, s["key"]))
            msg = (f"All {n} steps done - great session!" if done >= n else
                   f"Step {cur + 1} of {n}  ·  {done} done" + (f"  ·  about {total:.0f} min in total" if total else ""))
            self.lbl(hc, msg, 12, fg=C["mint"] if done >= n else C["sub"], pady=(p(8), 0))
            self.bar(hc, done / n, C["mint"])
        lp = g(d, "latest_play")
        if isinstance(lp, dict) and txt(g(lp, "map")):
            bits = []
            if num(g(lp, "acc")) is not None:
                bits.append(U.fmt_acc(g(lp, "acc")))
            if num(g(lp, "misses")) is not None:
                bits.append(f"{int(num(g(lp, 'misses')))} misses")
            self.lbl(hc, f"Last play: {txt(g(lp, 'map'))}" + (f"  -  {', '.join(bits)}" if bits else ""), 11,
                     fg=C["dim"], pady=(p(6), 0))

        self.heading(f, "Today's plan", "For each step: press Copy song search, click osu! and paste it in song select "
                     "(Ctrl+V), set the mods shown, play. Then press Done.")
        for s in steps:
            self._step_card(f, s, day, s["i"] == cur, self.settings.check_get(day, s["key"]))
        self._more_maps(f, steps)

    def _set_step(self, day, key, val):
        self.settings.check_set(day, key, val)
        self._hook("plan_changed")
        self.refresh_today()

    def _step_card(self, f, s, day, is_cur, done):
        p = self.px
        bg = C["hi"] if is_cur else C["card"]
        c = self.card(f, C["pink"] if is_cur else (C["mint"] if done else C["border"]), bg=bg)
        top = tk.Frame(c, bg=bg)
        top.pack(fill="x")
        if done:
            self.btn(top, "Undo", lambda: self._set_step(day, s["key"], False), "small").pack(side="right")
        else:
            self.btn(top, "Done ✓", lambda: self._set_step(day, s["key"], True),
                     "go" if is_cur else "normal").pack(side="right")
        if is_cur:
            self.chip(top, "NOW", C["pink"], size=11)
        mins = f"  ·  {s['minutes']:.0f} min" if s["minutes"] and "min" not in s["block"].lower() else ""
        self.lbl(top, ("✓  " if done else f"{s['i'] + 1}.  ") + block_title(s["block"]) + mins, 14, "bold",
                 fg=C["dim"] if done else (C["pink"] if is_cur else C["fg"]), side="left", wrap=False)
        if done:
            return
        if s["goal"]:
            self.more_text(c, ("This step: " if s["picks"] else "") + U.plain(s["goal"]), 12 if is_cur else 11,
                           fg=C["fg"] if is_cur else C["sub"], limit=170, pady=(p(4), 0))
        picks = s["picks"]
        if not picks:
            if is_cur and s["id"] == "review":
                self.btn(c, "Open Tips", lambda: self.select_tab_name("Tips"), "go").pack(anchor="w", pady=(p(8), 0))
            return
        if is_cur:
            if len(picks) > 1:
                self.lbl(c, f"Maps for this step ({len(picks)}) - play them in order, or pick the one you like:",
                         10, fg=C["dim"], pady=(p(6), 0))
            for rec in picks:
                self.map_row(c, rec)
        else:
            body = self.expander(c, f"Show the map{'s' if len(picks) > 1 else ''} ({len(picks)})",
                                 lambda b, ps=picks: [self.map_row(b, r) for r in ps], size=11, pady=(6, 0))
            body.configure(bg=bg)

    def _more_maps(self, f, steps):
        used = {txt(g(r, "id")) for s in steps for r in s["picks"]}
        rest = [r for r in dicts(g(self.data, "recommendations")) if txt(g(r, "id")) not in used or not txt(g(r, "id"))]
        if not dicts(g(self.data, "recommendations")):
            return
        groups = {}
        for r in rest:
            groups.setdefault(txt(g(r, "purpose"), "other").lower(), []).append(r)

        def build(b):
            if not rest:
                self.lbl(b, "All my map picks are already in today's plan above.", 12, fg=C["sub"])
            order = [k for k in PURPOSE_ORDER if k in groups] + sorted(k for k in groups if k not in PURPOSE_ORDER)
            for k in order:
                self.lbl(b, f"{U.PURPOSE_PLAIN.get(k, k.title())}  ({len(groups[k])})", 13, "bold", fg=C["pink"],
                         pady=(self.px(10), 0))
                for r in groups[k]:
                    cc = self.card(b, None, pady=self.px(6))
                    self.map_row(cc, r)
        self.expander(f, f"More map ideas ({len(rest)})", build, size=13, pady=(14, 0))

    # ============================================================ PROGRESS
    def _band_cp(self, rx):
        for b in dicts(g(g(self.data, "roadmap"), "bands")):
            if txt(g(b, "status")).lower() == "current":
                for cp in dicts(g(b, "checkpoints")):
                    if re.search(rx, txt(g(cp, "metric")).lower()):
                        return cp
        return None

    def _week_change(self, key, conv=num):
        tr = [conv(g(t, key)) for t in dicts(g(self.data, "trends"))]
        tr = [v for v in tr if v is not None]
        if len(tr) < 2:
            return None
        return tr[-1] - tr[-2]

    def _cur_band_stars(self):
        for b in dicts(g(g(self.data, "roadmap"), "bands")):
            if txt(g(b, "status")).lower() == "current":
                return num(g(b, "stars"))
        return None

    def _level_goal(self, cp):
        if not cp:
            return None
        st = self._cur_band_stars()
        return (f"Goal for {st:.1f}★: " if st is not None else "Goal for your level: ") + goal_words(g(cp, "target"))

    def _focus_target(self, fid):
        for it in dicts(g(self.data, "focus")):
            if txt(g(it, "id")) == fid and num(g(it, "target")) is not None:
                return num(g(it, "target"))
        return None

    def _pp_week_change(self):
        hist = [(U.to_dt(g(h, "date")) or self._date_of(g(h, "date")), num(g(h, "pp")))
                for h in dicts(g(self.pp_data, "history"))]
        hist = [(d, v) for d, v in hist if d is not None and v is not None]
        if len(hist) < 2:
            return None
        hist.sort(key=lambda t: t[0])
        last_d, last_v = hist[-1]
        older = [v for d, v in hist if (last_d - d).days >= 7]
        return last_v - older[-1] if older else None

    @staticmethod
    def _date_of(s):
        try:
            return _dt.datetime.fromisoformat(txt(s)[:10])
        except ValueError:
            return None

    def _tiles(self, f):
        d = self.data
        st = g(d, "stats") if isinstance(g(d, "stats"), dict) else {}
        tiles = []
        # timing consistency (UR)
        ur = num(g(st, "ur"))
        goals = []
        nxt = self._focus_target("ur")
        if nxt is not None and ur is not None and nxt < ur:
            goals.append(f"Next goal: under {nxt:.0f}")
        lg = self._level_goal(self._band_cp(r"\bur\b"))
        if lg:
            goals.append(lg)
        tiles.append(("Timing consistency (UR)", f"{ur:.0f}" if ur is not None else "-", goals,
                      self._week_change("ur"), True, "{:.0f}", "How evenly you hit the beat. Lower is better.", None))
        sb = num(g(st, "stream_break_bpm"))
        sc = num(g(st, "stream_comfort_bpm"))
        sv = sc if sc is not None else sb
        lg = self._level_goal(self._band_cp(r"stream"))
        tiles.append(("Stream speed", f"{sv:.0f} BPM" if sv is not None else "Not measured yet", [lg] if lg else [],
                      self._week_change("stream_break_bpm"), False,
                      "{:.0f} BPM", "Fastest streams you tap cleanly." if sv is not None else
                      ("Too many stream misses at every speed to measure yet - the Speed step in your plan tests it."
                       if st else "Shows up once I've looked at some of your plays."),
                      None if sv is not None else C["sub"]))
        acc = num(g(st, "avg_acc"))
        lg = self._level_goal(self._band_cp(r"accuracy"))
        tiles.append(("Accuracy", U.fmt_acc(acc) if acc is not None else "-", [lg] if lg else [],
                      self._week_change("acc", self._acc_val), False, "{:.1f}%", "Average over your recent plays.",
                      None))
        user = g(self.pp_data, "user") if isinstance(g(self.pp_data, "user"), dict) else {}
        pp = num(g(user, "pp"))
        if pp is None:
            pp = num(g(g(d, "pp"), "pp"))
        if pp is not None:
            gain = num(g(g(d, "pp"), "gain_7d"))
            if gain is None:
                gain = self._pp_week_change()
            tiles.append(("PP", f"{pp:,.0f}pp", [], gain, False, "{:.1f}pp", "Your total on the osu! website.", None))
        else:
            tiles.append(("Misses per 100 notes", (lambda v: f"{v:.1f}" if v is not None else "-")(
                num(g((dicts(g(d, "trends")) or [{}])[-1], "misses_per_100"))), [],
                self._week_change("misses_per_100"), True, "{:.1f}", "This week's average. Lower is better.", None))
        grid = tk.Frame(f, bg=C["bg"])
        grid.pack(fill="x")
        for c in range(2):
            grid.columnconfigure(c, weight=1, uniform="tile")
        p = self.px
        for i, (title, value, goals, chg, lower_better, fmt, expl, vcol) in enumerate(tiles):
            t = tk.Frame(grid, bg=C["card"], padx=p(16), pady=p(12), highlightthickness=1,
                         highlightbackground=C["border"])
            t.grid(row=i // 2, column=i % 2, sticky="nsew", padx=p(4), pady=p(4))
            self.lbl(t, title, 12, "bold", fg=C["sub"])
            self.lbl(t, value, 22 if len(value) < 12 else 16, "bold", fg=vcol or C["mint"])
            for gl in goals:
                self.lbl(t, gl, 11, fg=C["fg"])
            if chg is not None and abs(chg) > 1e-9:
                better = (chg < 0) == lower_better
                arrow = "▲" if chg > 0 else "▼"
                self.lbl(t, f"{arrow} {fmt.format(abs(chg))} vs last week  ({'better' if better else 'worse'})", 11,
                         "bold", fg=C["mint"] if better else C["peach"])
            elif chg is not None:
                self.lbl(t, "same as last week", 11, fg=C["dim"])
            self.lbl(t, expl, 10, fg=C["dim"], pady=(p(4), 0))

    TREND = {"up": ("▲ going up", "mint"), "down": ("▼ slipping", "peach"), "flat": ("► steady", "dim")}

    def _levels(self, f):
        """'Your training levels': one tile per skill track (coach_data.tracks)."""
        tr = g(self.data, "tracks")
        if not isinstance(tr, dict):
            return False
        keys = [k for k in U.TRACK_ORDER if isinstance(tr.get(k), dict)] + \
            sorted(k for k, v in tr.items() if k not in U.TRACK_ORDER and isinstance(v, dict))
        if not keys:
            return False
        p = self.px
        self.heading(f, "Your training levels", "How hard each kind of practice is right now. Pass and it goes up, "
                     "struggle and it eases off.", top=4)
        grid = tk.Frame(f, bg=C["bg"])
        grid.pack(fill="x")
        cols = 3
        for c in range(cols):
            grid.columnconfigure(c, weight=1, uniform="lvl")
        for i, k in enumerate(keys):
            t = tr[k]
            tile = tk.Frame(grid, bg=C["card"], padx=p(14), pady=p(10), highlightthickness=1,
                            highlightbackground=C["border"])
            tile.grid(row=i // cols, column=i % cols, sticky="nsew", padx=p(4), pady=p(4))
            self.lbl(tile, U.track_name(k), 13, "bold", fg=C["pink"])
            if txt(g(t, "label")):
                self.lbl(tile, U.plain(g(t, "label")) + (" (100% = your comfortable speed)"
                                                         if txt(g(t, "unit")) == "px/ms" and U.AIM_COMFORT else ""),
                         10, fg=C["dim"])
            self.lbl(tile, U.fmt_level(g(t, "level"), g(t, "unit")), 22, "bold", fg=C["mint"])
            tw, tc = self.TREND.get(txt(g(t, "trend")).lower(), ("", "dim"))
            if tw:
                self.lbl(tile, tw, 11, "bold", fg=C[tc])
            if txt(g(t, "next")):
                self.lbl(tile, U.level_text(g(t, "next")), 11, fg=C["fg"], pady=(p(4), 0))
        return True

    def _attempts_today(self, f):
        debs = U.debriefs(self.data)
        if not debs:
            return
        self.heading(f, f"Today's attempts ({len(debs)})", "Newest first - what happened and what I said to do next.")
        for deb in debs[:6]:
            self._debrief_card(f, deb)
        if len(debs) > 6:
            self.expander(f, f"Earlier today ({len(debs) - 6})",
                          lambda b, rest=debs[6:]: [self._debrief_card(b, x) for x in rest], size=12, pady=(2, 0))

    def _progress(self, f):
        p = self.px
        d = self.data
        had = self._levels(f)
        self._attempts_today(f)
        self.heading(f, "How you're doing", "Your main numbers - with the next goal and the change since last week "
                     "where I have them.", top=18 if had else 4)
        self._tiles(f)
        ph = g(d, "phase")
        if isinstance(ph, str):
            ph = {"name": ph}
        if isinstance(ph, dict) and txt(g(ph, "name")):
            key = txt(g(ph, "name")).lower()
            label, blurb = PHASE_INFO.get(key, (txt(g(ph, "name")).title(), ""))
            c = self.card(f, C["lav"], pady=0)
            c.master.pack_configure(pady=(p(12), 0))
            self.lbl(c, f"Right now: {label}" + (f" - {blurb}" if blurb else ""), 13, "bold", fg=C["lav"])
            if PHASE_DO.get(key):
                self.lbl(c, PHASE_DO[key], 12, fg=C["fg"], pady=(p(4), 0))
            if txt(g(ph, "why")):
                self.expander(c, "Why this phase?", lambda bd, w=U.plain(g(ph, "why")): self.lbl(
                    bd, w, 11, fg=C["sub"], bg=C["card"]), size=11, pady=(4, 0)).configure(bg=C["card"])

        rm = g(d, "roadmap")
        bands = [(num(g(b, "stars")), b) for b in dicts(g(rm, "bands"))]
        bands.sort(key=lambda t: t[0] if t[0] is not None else 99)
        cur = num(g(rm, "current_stars"))
        self.heading(f, "Your road to 7★", ("You're around " + f"{cur:.1f}★" + " right now (rough estimate). "
                                            if cur is not None else "") +
                     "Meet most goals of a step and the next one opens up. Click a step to see its goals.")
        if not bands:
            self.empty(f, "No roadmap yet - it appears after the next analysis.")
        stat = lambda b: txt(g(b, "status")).lower()  # noqa: E731
        main = [t for t in bands if stat(t[1]) in ("current", "next")]
        later = [t for t in bands if stat(t[1]) not in ("current", "next", "done")]
        done = [t for t in bands if stat(t[1]) == "done"]
        for st, b in main:
            self._band_row(f, st, b)
        if later:
            rng = f"{later[0][0]:.1f}★ to {later[-1][0]:.1f}★" if later[0][0] is not None and later[-1][0] is not None \
                else ""
            self.expander(f, f"Later steps ({len(later)})" + (f":  {rng}" if rng else ""),
                          lambda bd: [self._band_row(bd, st, b) for st, b in later], size=12, pady=(2, 0))
        if done:
            self.expander(f, f"Steps you've already done ({len(done)})",
                          lambda bd: [self._band_row(bd, st, b) for st, b in reversed(done)], size=12, pady=(6, 0))
        if txt(g(rm, "basis")):
            self.expander(f, "How I estimate this", lambda bd: self.lbl(bd, U.plain(g(rm, "basis")), 11,
                                                                       fg=C["sub"]), size=11)

        self.heading(f, "More details")
        self.expander(f, "Charts (week by week)", self._charts, size=13)
        plays = dicts(g(d, "recent_plays"))
        self.expander(f, f"Recent plays ({len(plays)})", self._recent, size=13, pady=(8, 0))
        spots = dicts(g(d, "trouble_spots"))
        self.expander(f, f"Trouble spots ({len(spots)}) - parts of maps where misses pile up", self._trouble,
                      size=13, pady=(8, 0))
        self.expander(f, "PP and your osu! profile", self._pp, size=13, pady=(8, 0))

    def _band_row(self, f, st, b):
        p = self.px
        status = txt(g(b, "status"), "later").lower()
        label, col = BAND_STATUS.get(status, (status.title() or "?", C["border"]))
        is_cur = status == "current"
        bg = C["hi"] if is_cur else C["card"]
        c = self.card(f, col if status != "later" else C["border"], bg=bg, pady=p(6))
        top = tk.Frame(c, bg=bg)
        top.pack(fill="x")
        tk.Label(top, text=f"{st:.1f}★" if st is not None else "?★", bg=bg,
                 fg=col if status != "later" else C["sub"], font=F(16 if is_cur else 14, "bold")).pack(side="left")
        self.chip(top, label, col if status != "later" else C["card2"], side="left",
                  fg=C["ink"] if status != "later" else C["sub"]).pack_configure(padx=(p(12), p(8)))
        cps = dicts(g(b, "checkpoints"))
        met = sum(1 for cp in cps if g(cp, "met") is True)
        if cps:
            tk.Label(top, text=f"{met} of {len(cps)} goals met", bg=bg, fg=C["fg"] if is_cur else C["dim"],
                     font=F(12)).pack(side="right")
        if is_cur and txt(g(b, "focus")):
            self.more_text(c, U.plain(g(b, "focus")), 12, fg=C["fg"], limit=130, pady=(p(4), 0))
        if cps and status in ("current", "next"):
            self.bar(c, met / len(cps), col)

        def build(bd, b=b, cps=cps, bg=bg):
            bd.configure(bg=bg)
            if not is_cur and txt(g(b, "focus")):
                self.lbl(bd, U.plain(g(b, "focus")), 11, fg=C["sub"], bg=bg)
            if txt(g(b, "typical_maps")):
                self.lbl(bd, "Typical maps: " + U.plain(g(b, "typical_maps")), 11, fg=C["dim"], bg=bg,
                         pady=(p(2), p(4)))
            for cp in cps:
                ok = g(cp, "met") is True
                row = tk.Frame(bd, bg=bg)
                row.pack(fill="x", pady=(p(2), 0))
                tk.Label(row, text="✓" if ok else "○", bg=bg, fg=C["mint"] if ok else C["peach"],
                         font=F(13, "bold"), width=2).pack(side="left", anchor="n")
                self.lbl(row, checkpoint_line(cp), 11, fg=C["sub"] if ok else C["fg"], bg=bg, side="left")
            if not cps:
                self.lbl(bd, "Goals for this step appear once you get closer.", 11, fg=C["dim"], bg=bg)
        if cps or txt(g(b, "typical_maps")) or txt(g(b, "focus")):
            self.expander(c, "See the goals" if cps else "Details", build, size=11, pady=(4, 0))

    def _charts(self, b):
        tr = dicts(g(self.data, "trends"))
        if not tr:
            self.lbl(b, "No weekly history yet.", 12, fg=C["sub"])
            return
        labels = [self._week_label(g(t, "week")) for t in tr]
        series = [("Timing consistency (UR)", [num(g(t, "ur")) for t in tr], lambda v: f"{v:.0f}", "lower is better",
                   C["pink"]),
                  ("Accuracy", [self._acc_val(g(t, "acc")) for t in tr], lambda v: f"{v:.0f}%", "higher is better",
                   C["mint"])]
        sbpm = [num(g(t, "stream_break_bpm")) for t in tr]
        if any(v is not None for v in sbpm):
            series.append(("Stream speed (BPM)", sbpm, lambda v: f"{v:.0f}", "higher is better", C["yellow"]))
        mp = [num(g(t, "misses_per_100")) for t in tr]
        if any(v is not None for v in mp):
            series.append(("Misses per 100 notes", mp, lambda v: f"{v:.1f}", "lower is better", C["peach"]))
        self.lbl(b, "From all your replays on this PC. Small dips are normal - look at the overall direction.", 11,
                 fg=C["dim"], pady=(0, self.px(6)))
        for title, ys, fmt, better, col in series:
            c = self.card(b, None)
            cv = tk.Canvas(c, height=self.px(210), bg=C["card"], highlightthickness=0, bd=0)
            cv.pack(fill="x")
            cv.bind("<Configure>", lambda e, cv=cv, a=(title, labels, ys, fmt, better, col): self._chart(cv, *a))

    @staticmethod
    def _acc_val(x):
        v = num(x)
        if v is None:
            return None
        return v if v > 1.5 else v * 100

    @staticmethod
    def _week_label(w):
        s = txt(w, "?")
        try:
            return _dt.date.fromisoformat(s[:10]).strftime("%b %d")
        except ValueError:
            return s[:8]

    def _chart(self, cv, title, labels, ys, fmt, better, col, xlabel="week starting"):
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 80 or H < 60:
            return
        p = self.px
        x0, x1, y0, y1 = p(62), W - p(18), p(36), H - p(42)
        cv.create_text(p(4), p(12), text=title, anchor="w", fill=C["fg"], font=F(12, "bold"))
        cv.create_text(W - p(4), p(12), text=better, anchor="e", fill=C["dim"], font=F(10))
        pts = [(i, v) for i, v in enumerate(ys) if v is not None]
        if not pts:
            cv.create_text(W / 2, H / 2, text="Not enough data yet", fill=C["dim"], font=F(11))
            return
        lo, hi = min(v for _, v in pts), max(v for _, v in pts)
        span = hi - lo
        if span < 1e-9:
            span = max(abs(hi) * 0.1, 1.0)
            lo, hi = lo - span / 2, hi + span / 2
        else:
            lo, hi = lo - span * 0.12, hi + span * 0.12
        n = len(ys)

        def X(i):
            return (x0 + x1) / 2 if n <= 1 else x0 + (x1 - x0) * i / (n - 1)

        def Y(v):
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        for k in range(5):
            v = lo + (hi - lo) * k / 4
            y = Y(v)
            cv.create_line(x0, y, x1, y, fill=C["border"], dash=(2, 4) if k else None)
            cv.create_text(x0 - p(8), y, text=fmt(v), anchor="e", fill=C["dim"], font=F(9))
        cv.create_line(x0, y0, x0, y1, fill=C["border"])
        step = max(1, math.ceil(n / 7))
        for i in range(0, n, step):
            cv.create_text(X(i), y1 + p(12), text=labels[i] if i < len(labels) else "", fill=C["dim"], font=F(9))
        cv.create_text((x0 + x1) / 2, H - p(8), text=xlabel, fill=C["dim"], font=F(9))
        for (ia, va), (ib, vb) in zip(pts, pts[1:]):
            cv.create_line(X(ia), Y(va), X(ib), Y(vb), fill=col, width=p(3), capstyle="round",
                           dash=(4, 4) if ib - ia > 1 else None)
        r = p(4)
        for i, v in pts:
            cv.create_oval(X(i) - r, Y(v) - r, X(i) + r, Y(v) + r, fill=C["card"], outline=col, width=p(2))
        li, lv = pts[-1]
        cv.create_text(X(li), Y(lv) - p(14), text=fmt(lv), fill=col, font=F(10, "bold"),
                       anchor="e" if li == n - 1 and n > 1 else "center")

    def _recent(self, b):
        plays = dicts(g(self.data, "recent_plays"))
        if not plays:
            self.lbl(b, "No plays looked at yet.", 12, fg=C["sub"])
            return
        p = self.px
        for pl in plays[:20]:
            c = self.card(b, None, pady=p(6))
            top = tk.Frame(c, bg=C["card"])
            top.pack(fill="x")
            v = txt(g(pl, "verdict")).lower()
            vt, vc = VERDICT_PLAIN.get(v, (v, C["card2"]))
            if vt:
                self.chip(top, vt, vc, side="right", size=10)
            self.lbl(top, txt(g(pl, "map"), "Unknown map"), 12, "bold", side="left")
            bits = [U.fmt_mods(g(pl, "mods")).replace("NM", "No mods"), U.fmt_acc(g(pl, "acc"))]
            if num(g(pl, "misses")) is not None:
                bits.append(f"{int(num(g(pl, 'misses')))} misses")
            if txt(g(pl, "main_issue")):
                bits.append(f"main problem: {txt(g(pl, 'main_issue'))}")
            when = U.ago(g(pl, "ts"))
            if when:
                bits.append(when)
            self.lbl(c, "  ·  ".join(bits), 11, fg=C["sub"])

    def _trouble(self, b):
        spots = dicts(g(self.data, "trouble_spots"))
        if not spots:
            self.lbl(b, "No trouble spots found - nice and even!", 12, fg=C["sub"])
            return
        p = self.px
        self.lbl(b, "Practise just that part: slow it down until it's clean, then speed back up.", 11, fg=C["dim"],
                 pady=(0, p(6)))
        for s in spots:
            c = self.card(b, C["peach"], pady=p(8))
            top = tk.Frame(c, bg=C["card"])
            top.pack(fill="x")
            if txt(g(s, "map")):
                self.copy_button(top, txt(g(s, "map")), "Copy map name", "small").pack(side="right", anchor="n")
            self.lbl(top, txt(g(s, "map"), "Unknown map"), 12, "bold", side="left")
            a = txt(g(s, "start")) or U.fmt_mmss(ms=g(s, "start_ms"))
            e = txt(g(s, "end")) or U.fmt_mmss(ms=g(s, "end_ms"))
            bits = []
            if a != "?" or e != "?":
                bits.append(f"{a} - {e}")
            if num(g(s, "misses")) is not None:
                bits.append(f"{int(num(g(s, 'misses')))} misses")
            if txt(g(s, "kind")):
                bits.append(txt(g(s, "kind")))
            pr = num(g(s, "practice_rate"))
            if pr:
                bits.append(f"practise at {pr:.2f}x speed")
            if bits:
                self.lbl(c, "  ·  ".join(bits), 11, "bold", fg=C["yellow"], pady=(p(2), 0))
            if txt(g(s, "reason")):
                self.lbl(c, "What went wrong: " + U.plain(g(s, "reason")), 11, fg=C["sub"], pady=(p(2), 0))
            if txt(g(s, "advice")):
                self.more_text(c, "What to do: " + U.plain(g(s, "advice")), 11, limit=150, pady=(p(2), 0))

    def _pp(self, b):
        p = self.px
        ppd = self.pp_data if isinstance(self.pp_data, dict) else {}
        summ = g(self.data, "pp") if isinstance(g(self.data, "pp"), dict) else {}
        user = g(ppd, "user") if isinstance(g(ppd, "user"), dict) else {}
        total = num(g(user, "pp")) if num(g(user, "pp")) is not None else num(g(summ, "pp"))
        rank = num(g(user, "global_rank")) if num(g(user, "global_rank")) is not None else num(g(summ, "rank"))
        if total is None and rank is None:
            c = self.card(b, C["yellow"])
            self.lbl(c, "I can't see your osu! profile yet.", 13, "bold")
            self.lbl(c, "Connect your osu! account (free, about 2 minutes) and I'll show your PP, rank, best plays and "
                        "maps that would raise your PP.", 11, fg=C["sub"], pady=(p(2), 0))
            self.btn(c, "Show me how (Settings)", lambda: self.select_tab_name("Settings"), "go").pack(
                anchor="w", pady=(p(8), 0))
            return
        bits = []
        if total is not None:
            bits.append(f"{total:,.0f}pp")
        if rank is not None:
            bits.append(f"rank #{rank:,.0f}")
        gain = num(g(summ, "gain_7d"))
        if gain is not None:
            bits.append(f"{gain:+.1f}pp in the last 7 days")
        c = self.card(b, C["mint"])
        self.lbl(c, "  ·  ".join(bits), 14, "bold", fg=C["mint"])
        if txt(g(summ, "next_goal")):
            self.lbl(c, "Next goal: " + txt(g(summ, "next_goal")), 12, pady=(p(4), 0))
        if txt(g(ppd, "error")):
            self.lbl(c, f"Last update had a problem: {txt(g(ppd, 'error'))} (showing the last good data)", 10,
                     fg=C["yellow"], pady=(p(4), 0))
        if self.hooks.get("api_refresh"):
            fb = tk.Label(c, text="", bg=C["card"], fg=C["dim"], font=F(10))
            self.btn(c, "Refresh PP data", lambda: fb.configure(text=self.hooks["api_refresh"]() or "Fetching..."),
                     "small").pack(anchor="w", pady=(p(6), 0))
            fb.pack(anchor="w")
        hist = dicts(g(ppd, "history"))
        pts = [(self._fmt_day(g(h, "date"), "%b %d"), num(g(h, "pp"))) for h in hist]
        if sum(1 for q in pts if q[1] is not None) >= 2:
            cc = self.card(b, None)
            cv = tk.Canvas(cc, height=p(200), bg=C["card"], highlightthickness=0, bd=0)
            cv.pack(fill="x")
            cv.bind("<Configure>", lambda e, a=("Total PP", [q[0] for q in pts], [q[1] for q in pts],
                                                 lambda v: f"{v:,.0f}", "higher is better", C["mint"]):
                    self._chart(cv, *a, xlabel="date"))
        plays = dicts(g(ppd, "top_plays"))
        if plays:
            self.lbl(b, "Your best plays (they make up your PP)", 13, "bold", fg=C["pink"], pady=(p(8), p(4)))
            for i, pl in enumerate(plays[:10]):
                self.lbl(b, f"{i + 1}.  {U.map_name(pl)}   -   {U.fmt_num(g(pl, 'pp'))}pp, {U.fmt_acc(g(pl, 'acc'))}, "
                            f"{U.fmt_mods(g(pl, 'mods')).replace('NM', 'No mods')}", 11, fg=C["sub"])
        climb = dicts(g(ppd, "climb"))
        if climb:
            self.lbl(b, "Maps that could raise your PP", 13, "bold", fg=C["pink"], pady=(p(10), p(2)))
            self.lbl(b, "Played by people a little above you. PP numbers are estimates; this list leans towards "
                        "popular 'farm' maps.", 10, fg=C["dim"])
            for it in climb[:12]:
                cc = self.card(b, C["mint"] if g(it, "owned") is True else C["lav"], pady=p(6))
                top = tk.Frame(cc, bg=C["card"])
                top.pack(fill="x")
                if g(it, "owned") is True and U.search_text(it):
                    self.copy_button(top, U.search_text(it)).pack(side="right", anchor="n")
                elif U.pos_int(g(it, "beatmap_id")):
                    bid = U.pos_int(g(it, "beatmap_id"))
                    self.btn(top, "Open on website", lambda bid=bid: webbrowser.open(f"https://osu.ppy.sh/b/{bid}"),
                             "small").pack(side="right", anchor="n")
                self.lbl(top, U.map_name(it), 12, "bold", side="left")
                bits = [U.mod_pill(g(it, "mods"), g(it, "rate"))]
                gain = num(g(it, "est_gain_pp"))
                if gain is not None and gain > 0:
                    bits.append(f"about +{gain:.0f}pp to your total with a good run")
                bits.append("in your library" if g(it, "owned") is True else "not downloaded yet")
                self.lbl(cc, "  ·  ".join(bits), 11, fg=C["sub"])

    # ============================================================ PP (CONTRACT_PP2.md)
    def _pp_tab(self, f):
        p = self.px
        hdr = U.pp_header(self.pp_data, self.recs, self.data)
        self.heading(f, "Your PP", "What each map is worth for you, and the maps that would rank you up fastest.",
                     top=4)
        c = self.card(f, C["mint"])
        if hdr["total_txt"]:
            row = tk.Frame(c, bg=C["card"])
            row.pack(fill="x")
            tk.Label(row, text=hdr["total_txt"], bg=C["card"], fg=C["mint"], font=F(28, "bold")).pack(side="left")
            side = tk.Frame(row, bg=C["card"])
            side.pack(side="left", padx=(p(18), 0))
            if hdr["rank_txt"]:
                tk.Label(side, text="rank " + hdr["rank_txt"], bg=C["card"], fg=C["fg"], font=F(15, "bold"),
                         anchor="w").pack(fill="x")
            if hdr["today_txt"]:
                up = (hdr["change"] or 0) > 0.05
                tk.Label(side, text=hdr["today_txt"], bg=C["card"], fg=C["mint"] if up else C["sub"],
                         font=F(12, "bold" if up else "normal"), anchor="w").pack(fill="x")
        else:
            self.lbl(c, "I can't see your osu! profile yet.", 13, "bold")
            if not U.api_configured():
                why = ("Connect your osu! account in the Settings tab (\"osu! website account\" - the README "
                       "section \"Set up the osu! API\" walks you through it) and I'll show your total pp and "
                       "rank here.")
            else:
                why = "Your account details are filled in; waiting for the first update from the osu! website."
                if txt(g(self.pp_data, "error")):
                    why += "  Problem: " + txt(g(self.pp_data, "error"))
            self.lbl(c, why, 11, fg=C["sub"], pady=(p(2), 0))
        bt = tk.Frame(c, bg=C["card"])
        bt.pack(fill="x", pady=(p(8), 0))
        fb = tk.Label(bt, text="", bg=C["card"], fg=C["dim"], font=F(10))
        if self.hooks.get("api_refresh"):
            self.btn(bt, "Refresh PP data", lambda: fb.configure(text=self._hook("api_refresh") or "Fetching..."),
                     "small").pack(side="left")
        if self.hooks.get("pp_refresh"):
            self.btn(bt, "Find new picks now",
                     lambda: fb.configure(text=self._hook("pp_refresh") or "Looking for new picks..."),
                     "small").pack(side="left", padx=(p(8), 0))
        fb.pack(side="left", padx=(p(10), 0))

        # rank-up picks
        if self.pp_sort == "smash":
            picks, src = U.rec_picks(self.recs, self.pp_data, "smash")
            picks = U.sort_picks(picks, "gain")
        else:
            picks, src = U.rec_picks(self.recs, self.pp_data)
            picks = U.sort_picks(picks, self.pp_sort)
        sub = "Maps I think you could smash for big pp. Expected pp is for a normal run of yours, not your best one."
        age = U.ago(g(self.recs, "generated_at"))
        if age:
            sub += f" (picked {age})"
        self.heading(f, "Rank-up picks", sub)
        sr = tk.Frame(f, bg=C["bg"])
        sr.pack(fill="x", pady=(0, p(8)))
        tk.Label(sr, text="Sort:", bg=C["bg"], fg=C["sub"], font=F(11)).pack(side="left", padx=(0, p(8)))
        for key, lab in U.PICK_SORTS.items():
            on = key == self.pp_sort
            b = self.btn(sr, lab, lambda k=key: self.set_pp_sort(k), "small")
            b.configure(bg=C["pink"] if on else C["card2"], fg=C["ink"] if on else C["fg"])
            b.pack(side="left", padx=(0, p(6)))
        tip = U.plain(txt(g(self.recs, "by_mods_tip")))
        if tip:
            self.lbl(f, "Mods: " + tip, 12, fg=C["lav"], pady=(0, p(8)))
        if src == "climb":
            self.lbl(f, "Rough picks from your osu! profile for now - my own picks show up after my next look at "
                        "your plays.", 11, fg=C["yellow"], pady=(0, p(6)))
        if self.pp_sort == "smash":
            self.lbl(f, "Smash picks = maps you'll most likely pass that are still worth a lot of pp for you.", 11,
                     fg=C["sub"], pady=(0, p(6)))
        if not picks and self.pp_sort == "smash" and U.rec_picks(self.recs, self.pp_data)[0]:
            self.empty(f, "No smash picks right now - the maps that would rank you up are all a bit risky. Try "
                          "'Safest first'.")
        elif not picks:
            why = ("pp_recommend.py isn't in my folder yet, so I can't pick maps." if not U.PP_RECOMMEND_SCRIPT.exists()
                   else "No picks yet - press 'Find new picks now', or play a few maps and they'll appear after "
                        "your session.")
            self.empty(f, why)
        self.pp_rows_shown = [pv["name"] for pv in picks[:30]]
        for pv in picks[:30]:
            self._pick_card(f, pv)
        if len(picks) > 30:
            self.lbl(f, f"... and {len(picks) - 30} more", 11, fg=C["dim"])

        # how good the predictions are
        self.heading(f, "How good are my guesses?",
                     "Before every map I write down what I expect, then check it against what you really got and "
                     "learn from it.")
        rep = self.report if isinstance(self.report, dict) else g(self.recs, "model")
        ms = U.model_summary(rep, self.rows)
        c = self.card(f, C["lav"])
        self.lbl(c, ms["line"], 14, "bold")
        if ms["detail"]:
            self.lbl(c, ms["detail"], 11, fg=C["sub"], pady=(p(4), 0))
        if ms["meta"]:
            self.lbl(c, ms["meta"], 10, fg=C["dim"], pady=(p(2), 0))
        sc = U.scored_rows(self.rows)
        if sc:
            cv = tk.Canvas(c, height=p(210), bg=C["card"], highlightthickness=0, bd=0)
            cv.pack(fill="x", pady=(p(10), 0))
            cv.bind("<Configure>", lambda e, cv=cv, rows=sc[-20:]: self._pred_chart(cv, rows))
            self.lbl(c, "Each column is one play (oldest left).  Bar = my likely range,  ○ = my guess,  "
                        "● = what you got (green = inside my range).", 10, fg=C["dim"], pady=(p(4), 0))
        else:
            self.lbl(c, "Nothing to compare yet - play a few maps with tosu running and I'll show my guesses next "
                        "to your real results here.", 11, fg=C["sub"], pady=(p(6), 0))

    def _pick_card(self, f, pv):
        p = self.px
        cc = self.card(f, CONF_COL.get(pv["confidence"], C["border"]), pady=p(8))
        top = tk.Frame(cc, bg=C["card"])
        top.pack(fill="x")
        if pv["action"]:
            kind, val = pv["action"]
            if kind == "copy":
                self.copy_button(top, val, "Copy search", "primary" if pv["owned"] else "normal").pack(
                    side="right", anchor="n", padx=(p(10), 0))
            else:
                self.btn(top, "Open on osu! website", lambda u=val: self._open_url(u), "normal").pack(
                    side="right", anchor="n", padx=(p(10), 0))
        if pv["smash"]:
            self.chip(top, "SMASH IT", C["mint"], side="right", size=10)
        self.lbl(top, pv["name"], 13, "bold", side="left")
        mr = tk.Frame(cc, bg=C["card"])
        mr.pack(fill="x", pady=(p(4), 0))
        self.chip(mr, pv["mods"], mod_color(pv["raw_mods"], pv["rate"]), size=11)
        meta = [x for x in (pv["meta"], "in your library" if pv["owned"] else "not downloaded yet") if x]
        self.lbl(mr, "  ·  ".join(meta), 11, fg=C["sub"], side="left")
        br = tk.Frame(cc, bg=C["card"])
        br.pack(fill="x", pady=(p(4), 0))
        if pv["expected"] is not None:
            tk.Label(br, text=f"~{U.fmt_pp(pv['expected'])}", bg=C["card"], fg=C["mint"], font=F(18, "bold")).pack(
                side="left")
        bits = [x for x in (pv["range"],
                            (f"{U.fmt_gain(pv['gain'])} to your total (expected)" if pv["gain"] is not None else None),
                            (f"up to {U.fmt_gain(pv['gain_high'])} on a great run" if pv["gain_high"] else None),
                            (f"{U.fmt_pass(pv['p_pass'])} pass chance" if pv["p_pass"] is not None else None)) if x]
        if bits:
            self.lbl(br, "  ·  ".join(bits), 12, side="left").pack_configure(padx=(p(12), 0))
        if pv["why"]:
            self.lbl(cc, pv["why"], 11, fg=C["dim"], pady=(p(4), 0))
        if pv["alts"]:
            self.lbl(cc, "Other mods: " + ",  ".join(pv["alts"]), 10, fg=C["sub"], pady=(p(2), 0))
        if pv["confidence"]:
            self.lbl(cc, f"{pv['confidence']} confidence", 10, fg=CONF_COL.get(pv["confidence"], C["dim"]))

    def _open_url(self, url):
        if self.hooks.get("open_url"):
            return self._hook("open_url", url)
        return _open_url(url)

    def _pred_chart(self, cv, rows):
        """Predicted range + guess vs the real pp, one column per play (pp_predictions.jsonl)."""
        p = self.px
        try:
            cv.delete("all")
            w, h = cv.winfo_width(), cv.winfo_height()
        except tk.TclError:
            return
        if w < 80 or h < 60 or not rows:
            return
        vals = []
        for r in rows:
            pr = r["pred"]
            vals += [x for x in (r["pp"], pr["expected"], pr["low"], pr["high"]) if x is not None]
        lo_v, hi_v = min(vals), max(vals)
        if hi_v - lo_v < 10:
            hi_v += 5
            lo_v -= 5
        lo_v = max(0.0, lo_v - (hi_v - lo_v) * 0.08)
        hi_v = hi_v + (hi_v - lo_v) * 0.08
        left, right, top, bot = p(48), p(10), p(10), p(22)

        def y(v):
            return top + (h - top - bot) * (1 - (v - lo_v) / (hi_v - lo_v))
        for k in range(4):
            v = lo_v + (hi_v - lo_v) * k / 3
            cv.create_line(left, y(v), w - right, y(v), fill=C["border"], width=1)
            cv.create_text(left - p(6), y(v), text=f"{v:.0f}pp", anchor="e", fill=C["dim"], font=F(9))
        n = len(rows)
        step = (w - left - right) / max(1, n)
        rad = max(p(3), min(p(6), step * 0.22))
        for i, r in enumerate(rows):
            x = left + step * (i + 0.5)
            pr = r["pred"]
            inside = None
            if pr["low"] is not None and pr["high"] is not None:
                cv.create_line(x, y(pr["low"]), x, y(pr["high"]), fill=C["hi"], width=max(2, int(step * 0.5)),
                               capstyle="round")
                inside = pr["low"] - 0.5 <= r["pp"] <= pr["high"] + 0.5
            if pr["expected"] is not None:
                ye = y(pr["expected"])
                cv.create_oval(x - rad, ye - rad, x + rad, ye + rad, outline=C["lav"], width=max(1, p(2)))
                if inside is None:
                    inside = abs(r["pp"] - pr["expected"]) <= max(5.0, 0.12 * pr["expected"])
            ya = y(r["pp"])
            col = C["mint"] if inside else C["peach"]
            cv.create_oval(x - rad * 0.8, ya - rad * 0.8, x + rad * 0.8, ya + rad * 0.8, fill=col, outline=col)
        cv.create_text(left, h - p(4), text="older", anchor="sw", fill=C["dim"], font=F(9))
        cv.create_text(w - right, h - p(4), text="newest", anchor="se", fill=C["dim"], font=F(9))

    @staticmethod
    def _fmt_day(s, fmt):
        s = txt(s)
        try:
            return _dt.date.fromisoformat(s[:10]).strftime(fmt)
        except ValueError:
            return s[:10]

    # ============================================================ TIPS
    def _tips(self, f):
        p = self.px
        items = dicts(g(self.data, "focus"))
        items.sort(key=lambda x: num(g(x, "priority")) if num(g(x, "priority")) is not None else 99)
        self.heading(f, "What to work on", "Most important first. Your plan on the Today tab already trains these.",
                     top=4)
        if not items:
            self.empty(f, "Nothing stands out yet - play a few maps and press Refresh.")
        else:
            c = self.card(f, C["pink"])
            for i, it in enumerate(items[:5]):
                line = f"{i + 1}.  {txt(g(it, 'title'), 'Focus area')}"
                fl = focus_line(it)
                if fl:
                    line += f"   -   {fl}"
                def build(bd, it=it):
                    if txt(g(it, "why")):
                        self.lbl(bd, U.plain(g(it, "why")), 11, fg=C["sub"])
                    if txt(g(it, "drill")):
                        self.lbl(bd, "Try this: " + U.plain(g(it, "drill")), 11, pady=(p(4), p(4)))
                if txt(g(it, "why")) or txt(g(it, "drill")):
                    self.expander(c, line, build, size=12, fg=C["fg"], pady=(4 if i else 0, 2))
                else:
                    self.lbl(c, line, 12, pady=(p(4) if i else 0, 0))
            self.lbl(c, "Click a line to see why and what to do.", 10, fg=C["dim"], pady=(p(6), 0))

        techs = dicts(g(self.data, "techniques"))
        self.heading(f, "Tips for you right now", "Picked from your replays - start with the first one.")
        if not techs:
            self.empty(f, "No tips yet - they appear after the next analysis.")
            return
        for t in techs[:3]:
            self._tech_card(f, t, big=True)
        self.heading(f, "All tips", "Grouped by topic. Click a topic to open it.")
        self.lbl(f, EVIDENCE_HELP, 10, fg=C["dim"], pady=(0, p(6)))
        rest = techs[3:]
        groups = {}
        for t in rest:
            groups.setdefault(txt(g(t, "category")).lower() or "other", []).append(t)
        names = dict(TECH_CATS)
        order = [k for k, _ in TECH_CATS if k in groups] + sorted(k for k in groups if k not in names)
        for k in order:
            self.expander(f, f"{names.get(k, k.title())}  ({len(groups[k])})",
                          lambda bd, ts=groups[k]: [self._tech_card(bd, t) for t in ts], size=13, pady=(6, 0))

    def _tech_card(self, parent, t, big=False):
        p = self.px
        ev = txt(g(t, "evidence")).lower()
        ev_lab, ev_col = EVIDENCE.get(ev, ("", C["dim"]))
        c = self.card(parent, C["pink"] if big else C["border"], pady=p(10 if big else 6))
        top = tk.Frame(c, bg=C["card"])
        top.pack(fill="x")
        if ev_lab:
            self.chip(top, ev_lab, ev_col, side="right", size=10)
        self.lbl(top, txt(g(t, "title"), "Tip"), 14 if big else 12, "bold", side="left")
        if txt(g(t, "for_you")):
            self.lbl(c, "Why for you: " + U.plain(first_sentence(g(t, "for_you"))), 12 if big else 11, fg=C["sub"],
                     pady=(p(4), 0))
        if txt(g(t, "drill")):
            self.lbl(c, "Try this: " + U.plain(g(t, "drill")), 12 if big else 11, pady=(p(4), 0))
        steps, mistakes = U.strs(g(t, "steps")), U.strs(g(t, "mistakes"))
        full_why = txt(g(t, "for_you"))
        if steps or mistakes or (full_why and full_why != first_sentence(full_why)) or txt(g(t, "source")):
            def build(bd, t=t, steps=steps, mistakes=mistakes, full_why=full_why):
                for i, s in enumerate(steps):
                    self.lbl(bd, f"{i + 1}.  {U.plain(s)}", 11, pady=(p(2), 0))
                if mistakes:
                    self.lbl(bd, "Avoid:", 11, "bold", fg=C["peach"], pady=(p(6), 0))
                    for s in mistakes:
                        self.lbl(bd, f"•  {U.plain(s)}", 11, fg=C["sub"])
                if full_why and full_why != first_sentence(full_why):
                    self.lbl(bd, "More about why: " + U.plain(full_why), 10, fg=C["dim"], pady=(p(6), 0))
                if txt(g(t, "source")):
                    self.lbl(bd, f"Source: {txt(g(t, 'source'))}", 10, fg=C["dim"], pady=(p(4), 0))
            self.expander(c, "Show steps", build, size=11, pady=(6, 0))

    # ============================================================ SETTINGS
    def _settings(self, f):
        p = self.px
        self.heading(f, "About you", "So I can pick the right tips. Saved on this PC only.", top=4)
        cur = self.settings.get("player_setup")
        if not isinstance(cur, dict):
            cur = g(self.data, "player_setup") if isinstance(g(self.data, "player_setup"), dict) else {}
        c = self.card(f, C["pink"])
        vars_ = {}
        for key, q, opts in SETUP_Q:
            self.lbl(c, q, 12, "bold", pady=(p(6), p(4)))
            row = tk.Frame(c, bg=C["card"])
            row.pack(fill="x")
            v = tk.StringVar(value=txt(g(cur, key)) if txt(g(cur, key)) in opts else "")
            vars_[key] = v
            btns = {}

            def paint(*_a, v=v, btns=btns):
                for o, b in btns.items():
                    on = v.get() == o
                    try:
                        b.configure(bg=C["pink"] if on else C["card2"], fg=C["ink"] if on else C["fg"],
                                    text=("●  " if on else "○  ") + o)
                    except tk.TclError:
                        pass
            for o in opts:
                b = tk.Button(row, text=o, command=lambda o=o, v=v: v.set(o), relief="flat", bd=0,
                              font=F(12, "bold"), padx=p(16), pady=p(6), cursor="hand2", highlightthickness=0,
                              activebackground=C["lav"], activeforeground=C["ink"])
                b.pack(side="left", padx=(0, p(8)))
                btns[o] = b
            v.trace_add("write", paint)
            paint()
        bt = tk.Frame(c, bg=C["card"])
        bt.pack(fill="x", pady=(p(12), 0))
        fb = tk.Label(bt, text="", bg=C["card"], fg=C["mint"], font=F(11))

        def save():
            ps = {k: (v.get() or None) for k, v in vars_.items()}
            if self.settings.set("player_setup", ps) is False:
                fb.configure(text="Couldn't save - try again in a moment (is OneDrive syncing the folder?).",
                             fg=C["yellow"])
                return
            fb.configure(text="Saved! I'll look at your plays again with this.", fg=C["mint"])
            self._hook("setup_saved")
        self.setup_save = save  # for --selftest
        self.setup_vars = vars_
        self.btn(bt, "Save", save, "go").pack(side="left")
        fb.pack(side="left", padx=p(10))

        # Mochi options
        self.heading(f, f"{self.name} options")
        c = self.card(f, C["lav"])
        if not self.options:
            self.lbl(c, f"Right-click {self.name} for options.", 12, fg=C["sub"])
        for label, help_, var, cmd in self.options:
            row = tk.Frame(c, bg=C["card"])
            row.pack(fill="x", pady=(p(4), p(4)))
            tb = self.btn(row, "", None, "normal", size=11)

            def flip(var=var, cmd=cmd, tb=tb):
                var.set(not var.get())
                if cmd:
                    cmd()
                paint(var, tb)

            def paint(var=var, tb=tb):
                on = bool(var.get())
                tb.configure(text="On" if on else "Off", bg=C["mint"] if on else C["card2"],
                             fg=C["ink"] if on else C["fg"], width=4)
            tb.configure(command=flip)
            paint()
            tb.pack(side="left", anchor="n")
            tx = tk.Frame(row, bg=C["card"])
            tx.pack(side="left", fill="x", expand=True, padx=(p(12), 0))
            self.lbl(tx, label, 12, "bold")
            if help_:
                self.lbl(tx, help_, 10, fg=C["dim"])

        # setup checks
        checks = dicts(g(self.data, "setup_checks"))
        self.heading(f, "Game setup checks", "Read from your osu! settings and replays - I never change them.")
        if not checks:
            self.empty(f, "No setup checks yet - they appear after the next analysis.")
        else:
            c = self.card(f, None)
            for ch in checks:
                st = txt(g(ch, "status")).lower()
                lab, col = CHECK_STATUS.get(st, (st.title() or "?", C["dim"]))
                row = tk.Frame(c, bg=C["card"])
                row.pack(fill="x", pady=(p(3), 0))
                tk.Label(row, text="●", bg=C["card"], fg=col, font=F(14)).pack(side="left")
                tk.Label(row, text=lab, bg=C["card"], fg=col, font=F(11, "bold")).pack(side="right")
                self.lbl(row, txt(g(ch, "title"), "Check"), 12, side="left").pack_configure(padx=(p(8), 0))
                if st in ("check", "warn") and (txt(g(ch, "detail")) or txt(g(ch, "how"))):
                    def build(bd, ch=ch):
                        if txt(g(ch, "detail")):
                            self.lbl(bd, U.plain(g(ch, "detail")), 11, fg=C["sub"])
                        if txt(g(ch, "how")):
                            self.lbl(bd, "How to fix: " + U.plain(g(ch, "how")), 11, pady=(p(4), 0))
                    body = self.expander(c, "How to fix", build, size=10, pady=(0, 4))
                    body.master.pack_configure(padx=(p(26), 0))

        # connections
        self.heading(f, "Connections (optional)", "Everything above works without these.")
        self._conn_api(f)
        self._conn_tosu(f)

        # about
        self.heading(f, f"About {self.name}")
        self.expander(f, f"How {self.name} works", self._about, size=13)

    def _conn_api(self, f):
        p = self.px
        c = self.card(f, C["blue"])
        self.lbl(c, "osu! website account  (shows your PP and rank)", 13, "bold")
        configured = U.api_configured()
        ppd = self.pp_data if isinstance(self.pp_data, dict) else {}
        if configured:
            gen = U.ago(g(ppd, "generated_at"))
            status = "● Connected." + (f" Last update {gen}." if gen else " Waiting for the first update...")
            if txt(g(ppd, "error")):
                status += f"  Problem: {txt(g(ppd, 'error'))}"
        else:
            status = "● Not connected yet."
        if not U.API_SCRIPT.exists():
            status += "  (osu_api.py is missing from my folder.)"
        self.lbl(c, status, 12, "bold", fg=C["mint"] if configured else C["yellow"], pady=(p(4), p(4)))
        if not configured:
            for i, line in enumerate([
                "Press \"Open osu! account settings\", scroll to OAuth, click \"New OAuth Application\". Any name; "
                "for the callback URL type http://localhost. Click Register.",
                "Press \"Open the account file\". Paste the Client ID after \"client_id\" and the Client Secret between the "
                "quotes after \"client_secret\". Optional: put your osu! username between the quotes after \"user\" "
                "(otherwise I use the account logged in to osu!). Save and close Notepad.",
                "Done - I fetch your profile within a minute, then every 30 minutes. Keep the secret private.",
            ]):
                self.lbl(c, f"{i + 1}.  {line}", 11, pady=(p(2), 0))
        if "onedrive" in str(U.BASE).lower():
            self.lbl(c, "Note: this folder is in OneDrive, so the secret gets uploaded to your OneDrive too. Move "
                        "the coach folder out of OneDrive first if you don't want that.", 10, fg=C["dim"],
                     pady=(p(4), 0))
        bt = tk.Frame(c, bg=C["card"])
        bt.pack(fill="x", pady=(p(8), 0))
        afb = tk.Label(c, text="", bg=C["card"], fg=C["dim"], font=F(10), anchor="w")
        self.btn(bt, "Open osu! account settings", lambda: webbrowser.open("https://osu.ppy.sh/home/account/edit"),
                 "normal").pack(side="left")
        self.btn(bt, "Change account details (advanced)" if configured else "Open the account file",
                 lambda: self._open_api_config(afb), "normal").pack(side="left", padx=p(8))
        if self.hooks.get("api_refresh") and configured:
            self.btn(bt, "Update PP now", lambda: afb.configure(text=self.hooks["api_refresh"]() or "Fetching..."),
                     "normal").pack(side="left")
        afb.pack(fill="x", pady=(p(4), 0))

    def _conn_tosu(self, f):
        p = self.px
        c = self.card(f, C["lav"])
        self.lbl(c, "tosu  (live: judges every try, even fails and quits, plus the PP bubble)", 13, "bold")
        ts = U.tosu_status()
        exe = ts["exe"]
        run_lbl = self.lbl(c, "● Checking if tosu is running...", 12, "bold", fg=C["dim"], pady=(p(4), 0))
        self.tosu_run_lbl = run_lbl   # --selftest
        stat = self.lbl(c, ("Installed: " + str(exe)) if exe else "Not installed.", 10,
                        fg=C["dim"] if exe else C["yellow"], pady=(0, p(4)))
        if ts["ignored"]:
            self.lbl(c, f"Not using the copy in OneDrive ({ts['ignored']}) - tosu won't start from there.", 10,
                     fg=C["dim"])
        if ts["onedrive"] and ts["movable"] is not None:
            wc = tk.Frame(c, bg=C["card2"], padx=p(12), pady=p(8))
            wc.pack(fill="x", pady=(p(6), p(4)))
            self.lbl(wc, "tosu can't run from inside OneDrive", 12, "bold", fg=C["yellow"], bg=C["card2"])
            self.lbl(wc, "It refuses to start there. Press the button once: I copy it to your local app folder "
                         f"({U.tosu_appdata_exe().parent if U.tosu_appdata_exe() else 'LOCALAPPDATA'}), without its "
                         "logs, and use that copy from now on.", 11, fg=C["fg"], bg=C["card2"])
            mfb = tk.Label(wc, text="", bg=C["card2"], fg=C["dim"], font=F(11), anchor="w", justify="left")

            def move(src=ts["movable"]):
                ok, msg, new = U.move_tosu_out(src)
                if ok and new is not None:
                    self.settings.set("tosu_path", str(new))
                    msg += " Start osu! with \"Play osu with Coach\" and tosu starts with it."
                try:
                    mfb.configure(text=msg, fg=C["mint"] if ok else C["yellow"])
                except tk.TclError:
                    pass
            self.tosu_move = move   # --selftest (only ever called there with a temp LOCALAPPDATA)
            self.btn(wc, "Move tosu out of OneDrive", move, "go").pack(anchor="w", pady=(p(6), 0))
            mfb.pack(fill="x", pady=(p(4), 0))
            self.wrap(mfb)
        note = self._hook("live_note")
        if note:
            self.lbl(c, note, 10, fg=C["dim"])
        tfb_box = {}

        def check(auto=False):
            tfb = tfb_box.get("lbl")
            hook = self.hooks.get("tosu_check")
            if not hook:
                if tfb is not None and not auto:
                    tfb.configure(text="Live features are off (pp_live.py missing).")
                return
            if tfb is not None and not auto:
                tfb.configure(text="Checking...")

            def done(ok):
                ok = {True: "ok", False: "down"}.get(ok, ok)
                try:
                    if not run_lbl.winfo_exists():
                        return
                    run_lbl.configure(text={"ok": "● tosu is running",
                                            "waiting": "● tosu is running, but it can't see osu! right now",
                                            }.get(ok, "● tosu is not running"),
                                      fg=C["mint"] if ok == "ok" else C["yellow"])
                    self.tosu_seen = ok
                    if tfb is not None and not auto:
                        tfb.configure(text={"ok": "tosu is running and sees osu! - live features are on!",
                                            "waiting": "tosu answers but says osu! isn't ready. Start osu! (or "
                                                       "close tosu and start osu! with \"Play osu with Coach\").",
                                            "down": "tosu isn't answering. Start osu! with \"Play osu with Coach\" "
                                                    "(it starts tosu), or run tosu.exe yourself.",
                                            None: "Live features are off."}.get(ok, "?"),
                                      fg=C["mint"] if ok == "ok" else C["yellow"])
                except tk.TclError:
                    pass
            hook(done)
        c.after(150, lambda: check(auto=True))   # the real HTTP check (one read-only request), every tab build

        def install_steps(parent):
            for i, line in enumerate([
                "Press \"Open tosu download page\" and download the latest Windows zip (only from that page).",
                f"Extract it so this file exists: {U.tosu_appdata_exe() or 'LOCALAPPDATA/tosu/tosu.exe'}  (not "
                "inside OneDrive - tosu won't start there; or use \"Choose tosu.exe\").",
                "Start osu! with \"Play osu with Coach\" - it starts tosu first.",
            ]):
                self.lbl(parent, f"{i + 1}.  {line}", 11, pady=(p(2), 0), bg=C["card"])
            bt2 = tk.Frame(parent, bg=C["card"])
            bt2.pack(fill="x", pady=(p(8), 0))
            self.btn(bt2, "Open tosu download page", lambda: webbrowser.open(U.TOSU_RELEASES), "normal").pack(
                side="left")
            self.btn(bt2, "Choose tosu.exe...", lambda: self._choose_tosu(stat), "normal").pack(side="left",
                                                                                               padx=p(8))

        if exe:
            self.lbl(c, "Start osu! with \"Play osu with Coach\" so tosu runs too.", 12, pady=(p(2), 0))
            bt = tk.Frame(c, bg=C["card"])
            bt.pack(fill="x", pady=(p(8), 0))
            self.btn(bt, "Is tosu running?", check, "normal").pack(side="left")
            tfb_box["lbl"] = tk.Label(c, text="", bg=C["card"], fg=C["dim"], font=F(11), anchor="w", justify="left")
            tfb_box["lbl"].pack(fill="x", pady=(p(4), 0))
            self.wrap(tfb_box["lbl"])
            self.expander(c, "Reinstall / help", install_steps, size=11, pady=(6, 0)).configure(bg=C["card"])
        else:
            install_steps(c)
            bt = tk.Frame(c, bg=C["card"])
            bt.pack(fill="x", pady=(p(6), 0))
            self.btn(bt, "Is tosu running?", check, "normal").pack(side="left")
            tfb_box["lbl"] = tk.Label(c, text="", bg=C["card"], fg=C["dim"], font=F(11), anchor="w", justify="left")
            tfb_box["lbl"].pack(fill="x", pady=(p(4), 0))
            self.wrap(tfb_box["lbl"])
        self.lbl(c, "Honest note: tosu is a third-party program that reads osu!'s memory (read-only) to see which map "
                    "you're hovering. I can't vouch for it. Without it I still judge every passed map from its replay - but "
                    "fails and quits leave no replay, so only tosu lets me see those.",
                 10, fg=C["dim"], pady=(p(6), 0))

    def _open_api_config(self, fb=None):
        p = U.ensure_api_config()
        try:
            import subprocess
            subprocess.Popen(["notepad.exe", str(p)], creationflags=0x08000000)
            if fb is not None:
                fb.configure(text="Opened in Notepad - save it when you're done.")
        except Exception as e:
            U.log.warning("could not open notepad: %s", e)
            try:
                os.startfile(str(p))  # type: ignore[attr-defined]
            except Exception:
                if fb is not None:
                    fb.configure(text=f"Open this file yourself: {p}")

    def _choose_tosu(self, stat_lbl):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.top, title="Find tosu.exe",
                                          filetypes=[("tosu", "tosu.exe"), ("Programs", "*.exe")])
        if not path or not path.lower().endswith(".exe"):
            return
        self.settings.set("tosu_path", path)
        if U.in_onedrive(path):
            stat_lbl.configure(text=f"Set: {path} - but that's inside OneDrive, where tosu won't start. Use "
                                    "\"Move tosu out of OneDrive\" (reopen this tab).", fg=C["yellow"])
        else:
            stat_lbl.configure(text=f"Set: {path}", fg=C["mint"])

    def _about(self, b):
        d = self.data
        for line in [
            f"Hi! I'm {self.name}, your osu! coach. I sit on top of osu! while you play.",
            "Click me for the \"Next up\" card: it tells you which map to play next and how. \"Copy song search\", "
            "then click osu! and paste it in song select (Ctrl+V). Press \"Done - next step\" when you've played it.",
            "After every try I tell you how it went and what to do next: the same map again, slower, faster, the next "
            "map or a break. With tosu running I also see fails and quits. Each skill has a level that moves up when "
            "you pass and eases off when you struggle, so the plan changes with you.",
            "Drag me to move me. Right-click me for options (refresh, sleep, quit).",
            "I never touch the osu! program: I'm a separate little window. I only read files from your osu! folder "
            "and never change anything there.",
            "Every ~30 minutes of play I'll remind you to take a 5-minute break. Stop for the day if your wrist or "
            "hand hurts.",
            "PP numbers are estimates - treat them as a guide, not a promise.",
        ]:
            self.lbl(b, "•  " + line, 11, fg=C["sub"], pady=(self.px(2), 0))
        notes = U.strs(g(d, "notes"))
        if notes:
            def build(bd):
                for n in notes:
                    self.lbl(bd, "•  " + U.plain(n), 10, fg=C["dim"], pady=(self.px(2), 0))
            self.expander(b, "Technical notes from the analysis", build, size=11, pady=(8, 0))
        gen = U.fmt_date(g(d, "generated_at"), "%Y-%m-%d %H:%M", "never")
        self.lbl(b, f"Analysis made: {gen}  ·  folder: {U.BASE}", 10, fg=C["dim"], pady=(self.px(8), 0))
        self.lbl(b, "Files: pet_log.txt (log)  ·  pet_settings.json (settings)  ·  coach_data.json (analysis)", 10,
                 fg=C["dim"])
