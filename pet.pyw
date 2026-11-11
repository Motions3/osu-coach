"""Mochi - a little desktop pet that is your osu! coach.

Double-click (or run with pythonw) to start. Mochi sits in the bottom-right
corner above the taskbar while osu! is running and stays on top of the game.
Click it for the PP card (your total pp + rank, what the map selected in song
select is worth for you, and maps that would rank you up), drag it to move it,
right-click for options. The full coach (with the trainer) opens from the card.

It is a completely separate window: it never injects into, attaches to or
reads the memory of the osu! process, and it never writes to the osu! folder.
It only stats %APPDATA%/osu/client.realm to notice that a new score was
saved, then runs coach_engine.py (which reads replays) and shows the result
from coach_data.json.

Flags:
  --exit-with-osu   quit when osu! closes (used by the launcher)
  --selftest        build everything from coach_data.json (or a built-in
                    sample), animate for ~4 s, no engine/subprocess, exit 0
  --data PATH       use another coach_data.json (testing)
  --sample / --sparse / --empty   selftest with the built-in sample / a broken sparse file / no data at all
  --hold N [--tab NAME | --card | --ppcard] [--expand] [--live] [--pos X,Y]
                    selftest helper: just show the panel tab (or the training card / the PP card) for N s
  --adapt           selftest with the built-in adaptive sample (session, debriefs, tracks, changes)
  --debrief KIND    --hold: show the debrief card (pass|close|fail|free-play|break|level-up|level-down|sparse,
                    or 'last' = the last_debrief of --data)

Adaptive loop (CONTRACT_ADAPT.md): when a try ends (tosu: playing -> anything else, pp_live's on_attempt or
live_attempts.jsonl growing; without tosu: a new replay file) the engine runs within ~2-3 s; a new
coach_data.last_debrief pops the debrief card (verdict, what happened, NEXT action in big text, level up/down).
The card's "Next up" follows coach_data.session.current; Done/Skip/back/restart write session_cmd.json (one at a
time - coach_adapt deletes it once read) and run the engine. Without a session the old local ticks are used.

PP-first (CONTRACT_PP2.md): left-click = PP card; song-select bubble = expected pp (big), range, +gain, pass chance;
results bubble = "You got 83pp - I predicted 74"; the trainer's debrief card only pops up when the setting
"Show training debriefs after plays" is on (off by default). After every try pp_model.update runs, after a session
(and at least daily) pp_recommend refreshes pp_recs.json - background subprocesses, one at a time, low priority,
never started during a map.

Optional live/online parts (CONTRACT_PP.md), each off when its module is missing:
  pp_live.LiveWatcher (tosu at 127.0.0.1) -> song-select PP card, hide while playing, results cheer;
  osu_api.py run as a subprocess at startup and every 30 min when osu_api_config.json is filled in.
"""
from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import random
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pet_util as U  # noqa: E402
from pet_util import g, num, txt  # noqa: E402
from pet_panel import CoachPanel, QuickCard, TAB_NAMES  # noqa: E402

try:  # optional: live song-select PP via tosu (CONTRACT_PP.md). Absent/broken -> feature off.
    import pp_live  # noqa: E402
    PP_LIVE_ERR = None
except Exception as _e:  # noqa: BLE001
    pp_live = None
    PP_LIVE_ERR = f"{type(_e).__name__}: {_e}" if not isinstance(_e, ModuleNotFoundError) else "pp_live.py not found"

KEY = "#0a0810"            # transparent key colour (never used for drawing)
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS: analysis never competes with the game for CPU
MOODS = ("happy", "neutral", "worried", "sleepy")

# palette for the pet
OUT = "#4b3a55"
BODY = "#fff3e3"
BELLY = "#fffaf3"
FOOT = "#f6dcc6"
EAR_IN = "#ffc2d2"
BAND = "#6fd8c1"
BAND_HI = "#c9f7ec"
STAR = "#ffd75e"
STAR_OUT = "#c9962a"
EYE = "#2c2142"
IRIS = "#6a58c9"
BLUSH = "#ffb6c8"
BLUSH_HOT = "#ff9dba"
MOUTH = "#5b2a3e"
TONGUE = "#ff8fa8"
SHADOW = "#2a2335"
SWEAT = "#9fd8ff"
ZZZ = "#c7b8ff"


# ----------------------------------------------------------------------------
# Win32 helpers (all optional - failures fall back to plain Tk behaviour)
# ----------------------------------------------------------------------------

def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


try:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
except Exception:  # not Windows
    user32 = kernel32 = None

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = wintypes.HWND(-1) if user32 else None


def hwnd_of(widget):
    if not user32:
        return None
    try:
        return user32.GetAncestor(widget.winfo_id(), 2) or widget.winfo_id()  # GA_ROOT
    except Exception:
        return None


def make_noactivate(widget):
    """Clicking the window must not take keyboard focus away from the game."""
    h = hwnd_of(widget)
    if not h:
        return
    try:
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        want = ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if ex != want:
            user32.SetWindowLongW(h, GWL_EXSTYLE, want)
    except Exception:
        pass


def premap(widget, full=False):
    """Map a window once, invisibly (alpha 0), so its real Win32 window exists and gets WS_EX_NOACTIVATE
    before it is ever shown for real (Tk only creates that window on the first map). Then hide it again."""
    try:
        widget.attributes("-alpha", 0.0)
        widget.deiconify()
        widget.update() if full else widget.update_idletasks()
        make_noactivate(widget)
        widget.withdraw()
        widget.update_idletasks()
        widget.attributes("-alpha", 1.0)
    except tk.TclError:
        pass


GIVE_BACK_FOCUS = True   # off in --selftest: tests never touch other windows


def give_back_focus(prev):
    """If showing one of our little windows took the foreground from the game, hand it straight back."""
    if not (GIVE_BACK_FOCUS and prev):
        return
    h = U.foreground_hwnd()
    if h and h != prev and U.hwnd_pid(h) == os.getpid():
        try:
            if user32.GetWindowTextLengthW(h) and "coach" in _wtext(h):
                return  # the coach panel itself: that one is allowed to have focus
        except Exception:
            pass
        U.restore_foreground(prev)


def _wtext(h):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, buf, 256)
    return buf.value


def force_topmost(widget):
    try:
        widget.attributes("-topmost", True)
    except tk.TclError:
        return
    h = hwnd_of(widget)
    if h:
        try:  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def work_area(root):
    """(left, top, right, bottom) of the work area (excludes taskbar) of the monitor the pet is on;
    the primary monitor's when that can't be found."""
    try:
        h = hwnd_of(root)
        if h and user32:
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            mon = user32.MonitorFromWindow(h, 2)  # MONITOR_DEFAULTTONEAREST
            if mon:
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
                if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    r = mi.rcWork
                    if r.right > r.left and r.bottom > r.top:
                        return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    try:
        r = wintypes.RECT()
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48


def virtual_screen(root):
    try:
        gm = user32.GetSystemMetrics
        x, y, w, h = gm(76), gm(77), gm(78), gm(79)
        if w > 0 and h > 0:
            return x, y, x + w, y + h
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def osu_pids():
    """PIDs of osu!.exe processes from the process list (never opened), or None if unavailable."""
    if not kernel32:
        return None
    try:
        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap and snap != wintypes.HANDLE(-1).value:
            try:
                pids = set()
                e = PROCESSENTRY32W()
                e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    if e.szExeFile.lower() == "osu!.exe":
                        pids.add(e.th32ProcessID)
                    ok = kernel32.Process32NextW(snap, ctypes.byref(e))
                return pids
            finally:
                kernel32.CloseHandle(snap)
    except Exception:
        pass
    return None


_pid_cache = {"t": -1e9, "pids": None}


def game_in_front():
    """True when the foreground window belongs to osu!.exe (process list cached for 3 s: called every second)."""
    pid = U.hwnd_pid(U.foreground_hwnd())
    if not pid:
        return False
    now = time.monotonic()
    if now - _pid_cache["t"] > 3:
        _pid_cache["pids"], _pid_cache["t"] = osu_pids(), now
    pids = _pid_cache["pids"]
    return bool(pids and pid in pids)


def osu_running():
    """True/False whether an osu!.exe process exists (process list only - never opened)."""
    target = "osu!.exe"
    pids = osu_pids()
    if pids is not None:
        return bool(pids)
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {target}", "/NH"], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW, timeout=10).stdout
        return target.lower() in out.lower()
    except Exception:
        return False


_mutex = None


def single_instance():
    global _mutex
    if not kernel32:
        return True
    try:
        _mutex = kernel32.CreateMutexW(None, False, "Local\\OsuCoachPet_Mochi_v1")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def pythonw_exe():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


# ----------------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------------

def ellipse_pts(cx, cy, rx, ry, ang=0.0, n=24):
    ca, sa = math.cos(ang), math.sin(ang)
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        x, y = rx * math.cos(a), ry * math.sin(a)
        pts += [cx + x * ca - y * sa, cy + x * sa + y * ca]
    return pts


def star_pts(cx, cy, ro, ri, n=5, rot=-math.pi / 2):
    pts = []
    for i in range(n * 2):
        r = ro if i % 2 == 0 else ri
        a = rot + math.pi * i / n
        pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


class PetView:
    """Draws the pet on a Canvas. All geometry is in logical px, scaled by s."""
    W, H = 180, 190

    def __init__(self, canvas, s):
        self.cv = canvas
        self.s = s
        # static fluffy outline (unit circle with soft bumps)
        self.fluff = []
        for i in range(44):
            a = 2 * math.pi * i / 44
            r = 1 + 0.022 * math.sin(a * 11)
            self.fluff.append((math.cos(a) * r, math.sin(a) * r))

    def _sc(self, pts):
        s = self.s
        return [v * s for v in pts]

    def poly(self, pts, **kw):
        return self.cv.create_polygon(self._sc(pts), **kw)

    def oval(self, x0, y0, x1, y1, **kw):
        return self.cv.create_oval(*self._sc([x0, y0, x1, y1]), **kw)

    def line(self, pts, w=2.0, **kw):
        kw.setdefault("capstyle", "round")
        kw.setdefault("joinstyle", "round")
        return self.cv.create_line(self._sc(pts), width=max(1, w * self.s), **kw)

    def draw(self, t, mood="neutral", eye_open=1.0, look=(0.0, 0.0), perk=0.0, hop=0.0, thinking=False,
             cheer=0.0, talking=False, squash=0.0):
        cv, s = self.cv, self.s
        cv.delete("all")
        cx = 90.0
        breathe = math.sin(t * 2 * math.pi / 3.4)
        bob = 1.4 * math.sin(t * 2 * math.pi / 3.4 + 0.7)
        oy = -hop + bob
        cy = 116 + oy + squash * 3
        rx = 60 * (1 + 0.012 * breathe + 0.06 * squash)
        ry = 51 * (1 - 0.012 * breathe - 0.06 * squash)
        sleepy = mood == "sleepy"
        ow = 2.4

        # shadow
        sh = max(0.45, 1 - hop / 28)
        self.oval(cx - 40 * sh, 177 - 4.5 * sh, cx + 40 * sh, 177 + 4.5 * sh, fill=SHADOW, outline="")

        # feet (peek out under the body)
        fy = 163 + oy * 0.85
        for dx in (-25, 25):
            self.oval(cx + dx - 14, fy - 8, cx + dx + 14, fy + 9, fill=FOOT, outline=OUT, width=ow * s)

        # ears (round, tilted outward; perk up on hover)
        lift = 5 * perk + (0.7 * math.sin(t * 1.7) if not sleepy else -3)
        tilt = 0.55 - 0.25 * perk + (0.2 if sleepy else 0)
        for side in (-1, 1):
            ex, ey = cx + side * 37, cy - 43 - lift
            ang = side * tilt
            self.poly(ellipse_pts(ex, ey, 13, 14.5, ang, 20), fill=BODY, outline=OUT, width=ow * s, smooth=True)
            self.poly(ellipse_pts(ex + side * 0.5, ey + 1, 7, 8, ang, 16), fill=EAR_IN, outline="", smooth=True)

        # body (fluffy bun, a bit flatter at the bottom)
        pts = []
        for ux, uy in self.fluff:
            pts += [cx + ux * rx, cy + uy * ry * (0.9 if uy > 0 else 1.0)]
        self.poly(pts, fill=BODY, outline=OUT, width=ow * s, smooth=True)
        self.oval(cx - 34, cy + 2, cx + 34, cy + 42, fill=BELLY, outline="")

        # little arms (cheer = arms up and waving)
        for side in (-1, 1):
            if cheer > 0:
                wave = math.sin(t * 14 + side) * 4
                ax, ay = cx + side * (58 + 2 * cheer), cy - 6 - 14 * cheer + wave
            else:
                ax, ay = cx + side * 55, cy + 17 + 0.8 * breathe
            self.poly(ellipse_pts(ax, ay, 8.5, 11.5, side * (0.5 + cheer * 0.4), 14),
                      fill=BODY, outline=OUT, width=ow * s, smooth=True)

        # headband with a knot and a tiny star badge
        band = [cx - 52, cy - 27, cx - 27, cy - 38, cx, cy - 41, cx + 27, cy - 38, cx + 52, cy - 27]
        self.line(band, 9.5, fill=BAND, smooth=True)
        self.line([v - (1.8 if i % 2 else 0) for i, v in enumerate(band)], 2.4, fill=BAND_HI, smooth=True)
        flap = math.sin(t * 3.1) * 2.5
        kx, ky = cx + 51, cy - 29
        self.poly([kx, ky - 2, kx + 17, ky - 11 + flap, kx + 15, ky - 1 + flap, kx + 2, ky + 2],
                  fill=BAND, outline=OUT, width=1.6 * s, smooth=True)
        self.poly([kx, ky + 1, kx + 14, ky + 7 + flap * 0.6, kx + 9, ky + 12 + flap * 0.6, kx - 1, ky + 5],
                  fill=BAND, outline=OUT, width=1.6 * s, smooth=True)
        self.oval(kx - 4, ky - 4, kx + 4, ky + 4, fill=BAND, outline=OUT, width=1.6 * s)
        self.poly(star_pts(cx, cy - 40, 6.5, 2.8), fill=STAR, outline=STAR_OUT, width=max(1, s))

        # blush
        bc = BLUSH_HOT if (perk > 0.5 or cheer > 0) else BLUSH
        for side in (-1, 1):
            self.oval(cx + side * 36 - 8.5, cy + 8, cx + side * 36 + 8.5, cy + 17, fill=bc, outline="")

        # eyes
        lx, ly = look
        ey0 = cy + 0
        for side in (-1, 1):
            ex = cx + side * 21 + lx
            ey = ey0 + ly
            if sleepy:
                self.line([ex - 8, ey + 1, ex, ey + 5, ex + 8, ey + 1], 2.6, fill=EYE, smooth=True)
            elif cheer > 0:
                self.line([ex - 8, ey + 3, ex, ey - 6, ex + 8, ey + 3], 3.0, fill=EYE, smooth=True)
            elif eye_open < 0.22:
                self.line([ex - 8, ey + 1, ex, ey + 4, ex + 8, ey + 1], 2.6, fill=EYE, smooth=True)
            else:
                erx, ery = 10.5 * (1 + 0.05 * perk), 13.2 * eye_open * (1 + 0.07 * perk)
                self.oval(ex - erx, ey - ery, ex + erx, ey + ery, fill=EYE, outline="")
                iry = min(6.0, ery * 0.5)
                self.oval(ex - 7, ey + ery - 2 * iry - 1.5, ex + 7, ey + ery - 1.5, fill=IRIS, outline="")
                hx, hy = ex - 3.6 - lx * 0.3, ey - ery * 0.4
                r1 = 4.2 * min(1.0, eye_open + 0.2)
                self.oval(hx - r1, hy - r1, hx + r1, hy + r1, fill="#ffffff", outline="")
                r2 = 1.9 * min(1.0, eye_open + 0.2)
                self.oval(ex + 3.8 - r2, ey + ery * 0.3 - r2, ex + 3.8 + r2, ey + ery * 0.3 + r2,
                          fill="#ffffff", outline="")
                if perk > 0.5:
                    self.poly(star_pts(ex + 4, ey - ery * 0.45, 2.6, 0.9, 4), fill="#ffffff", outline="")

        # brows when worried
        if mood == "worried" and cheer <= 0:
            for side in (-1, 1):
                bx = cx + side * 21
                self.line([bx + side * 9, cy - 16, bx - side * 6, cy - 21], 2.3, fill=OUT)
            sx, sy = cx + 47, cy - 16 + (math.sin(t * 2) * 1.5)
            self.poly([sx, sy - 9, sx + 5, sy + 1, sx, sy + 5, sx - 5, sy + 1], fill=SWEAT, outline=OUT,
                      width=1.4 * s, smooth=True)

        # mouth
        mx, my = cx + lx * 0.4, cy + 15
        talk_open = talking and math.sin(t * 16) > 0
        if sleepy:
            self.oval(mx - 3, my - 1, mx + 3, my + 5, fill=MOUTH, outline="")
        elif cheer > 0 or (mood == "happy" and not talking) or talk_open:
            big = 1.0 if (cheer > 0 or mood == "happy") else 0.7
            self.poly([mx - 8 * big, my - 1, mx + 8 * big, my - 1, mx + 6 * big, my + 6 * big, mx, my + 8.5 * big,
                       mx - 6 * big, my + 6 * big], fill=MOUTH, outline=MOUTH, width=max(1, s), smooth=True)
            self.oval(mx - 4 * big, my + 3 * big, mx + 4 * big, my + 8 * big, fill=TONGUE, outline="")
        elif mood == "worried":
            self.line([mx - 7, my + 3, mx - 3.5, my + 1, mx, my + 3, mx + 3.5, my + 1, mx + 7, my + 3], 2.2,
                      fill=MOUTH, smooth=True)
        else:
            self.line([mx - 7, my, mx - 3.5, my + 3.5, mx, my + 0.5, mx + 3.5, my + 3.5, mx + 7, my], 2.2,
                      fill=MOUTH, smooth=True)

        # sleepy Zzz
        if sleepy:
            for k in range(3):
                ph = (t * 0.35 + k / 3) % 1.0
                zx, zy = cx + 34 + ph * 30, 52 + oy - ph * 44
                size = int(round((8 + ph * 7) * 1))
                cv.create_text(zx * s, zy * s, text="z", fill=ZZZ, font=("Segoe UI", size, "bold"))

        # thinking: thought dots + a spinning sparkle
        if thinking:
            base = [(cx + 42, 42 + oy * 0.5, 3.6), (cx + 52, 30 + oy * 0.5, 4.8)]
            for i, (x, y, r) in enumerate(base):
                on = ((t * 2.0) % 3) >= i
                if on:
                    self.oval(x - r, y - r, x + r, y + r, fill="#ffffff", outline=OUT, width=1.2 * s)
            ang = t * 4.0
            pulse = 1 + 0.15 * math.sin(t * 8)
            self.poly(star_pts(cx + 64, 16 + oy * 0.5, 9 * pulse, 3.4 * pulse, 4, ang), fill=STAR, outline=STAR_OUT,
                      width=max(1, s))


# ----------------------------------------------------------------------------
# speech bubble
# ----------------------------------------------------------------------------

class Bubble:
    def __init__(self, root, s, on_click):
        self.s = s
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.configure(bg=KEY)
        try:
            self.top.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.top.attributes("-topmost", True)
        self.cv = tk.Canvas(self.top, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.cv.bind("<ButtonRelease-1>", lambda e: on_click())
        self.cv.configure(cursor="hand2")
        premap(self.top)   # before the first show, so even that one can't take focus from the game
        self.hide_job = None
        self.visible = False

    def show(self, title, body, tip_x, tip_y, secs=8.0, bounds=None, big=None):
        s = self.s
        cv = self.cv
        cv.delete("all")
        pad, wrap = 12 * s, int(270 * s)
        y = pad
        items = []
        if title:
            items.append(cv.create_text(pad, y, text=title, anchor="nw", width=wrap, fill="#d9569a",
                                        font=("Segoe UI", 10, "bold")))
            y = cv.bbox(items[-1])[3] + 2 * s
        if big:
            items.append(cv.create_text(pad, y, text=big, anchor="nw", width=wrap, fill="#1f9d7a",
                                        font=("Segoe UI", 18, "bold")))
            y = cv.bbox(items[-1])[3] + 2 * s
        if body:
            items.append(cv.create_text(pad, y, text=body, anchor="nw", width=wrap, fill="#3a2d45",
                                        font=("Segoe UI", 10)))
        items.append(cv.create_text(pad, (cv.bbox(items[-1])[3] if items else y) + 3 * s, text="click for your PP",
                                    anchor="nw", fill="#a596b8", font=("Segoe UI", 8)))
        x0, y0, x1, y1 = cv.bbox("all")
        w = max(x1 + pad, 150 * s)
        h = y1 + pad
        tail = 14 * s
        r = 14 * s
        tx = w - 44 * s  # tail position along the bottom edge
        pts = [r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h, w - r, h,
               tx + 9 * s, h, tx + 16 * s, h + tail, tx - 3 * s, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
        # sharp tail: duplicate its points so the spline keeps the corner
        bg = cv.create_polygon([p + 2 for p in pts], fill="#fffaf3", outline="#4b3a55", width=2 * s,
                               smooth=True, splinesteps=16)
        for it in items:
            cv.move(it, 2, 2)
        cv.tag_lower(bg)
        cw, ch = int(w + 5), int(h + tail + 5)
        cv.configure(width=cw, height=ch)
        x = int(tip_x - (tx + 16 * s) - 2)
        y = int(tip_y - ch)
        if bounds:
            l, t_, rr, b = bounds
            x = max(l + 4, min(x, rr - cw - 4))
            y = max(t_ + 4, y)
        self.top.geometry(f"{cw}x{ch}+{x}+{y}")
        prev = U.foreign_foreground()
        self.top.deiconify()
        make_noactivate(self.top)
        give_back_focus(prev)
        force_topmost(self.top)
        self.visible = True
        if self.hide_job:
            self.top.after_cancel(self.hide_job)
        self.hide_job = self.top.after(int(secs * 1000), self.hide)

    def hide(self):
        self.hide_job = None
        self.visible = False
        try:
            self.top.withdraw()
        except tk.TclError:
            pass


CONF_COLOR = {"low": "#ffb98a", "medium": "#ffd873", "high": "#7fe3c8"}


class LiveBubble:
    """Small song-select card beside the pet: what the hovered map is worth for you.
    It stays while you browse (no timer) and hides when you leave song select."""

    def __init__(self, root, s, on_click):
        self.s = s
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.configure(bg=KEY)
        try:
            self.top.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.top.attributes("-topmost", True)
        self.cv = tk.Canvas(self.top, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.cv.bind("<ButtonRelease-1>", lambda e: on_click())
        self.cv.configure(cursor="hand2")
        premap(self.top)
        self.visible = False
        self.lines = None

    def show(self, lines, pet_rect, bounds):
        s, cv = self.s, self.cv
        cv.delete("all")
        pad, wrap = 11 * s, int(285 * s)
        tail = 12 * s
        items = []
        y = pad

        def add(text, color, font, gap=2):
            nonlocal y
            if not text:
                return
            items.append(cv.create_text(pad, y, text=text, anchor="nw", width=wrap, fill=color, font=font))
            y = cv.bbox(items[-1])[3] + gap * s

        add(lines.get("title"), "#d9569a", ("Segoe UI", 11, "bold"), 0)
        add(lines.get("name"), "#a596b8", ("Segoe UI", 8))
        y += 2 * s
        if lines.get("big"):
            add(lines["big"], "#1f9d7a", ("Segoe UI", 22, "bold"), 0)
            add(lines.get("range"), "#3a2d45", ("Segoe UI", 10, "bold"))
        else:
            add(lines.get("status") or lines.get("main"), "#3a2d45", ("Segoe UI", 10, "bold"))
        gain = lines.get("gain")
        if gain:
            add(gain, "#1f9d7a" if gain.startswith("+") else "#6d5f80", ("Segoe UI", 10, "bold" if gain.startswith("+")
                                                                        else "normal"))
        add(lines.get("pass"), "#3a2d45", ("Segoe UI", 10))
        add(lines.get("tip"), "#b56a00", ("Segoe UI", 9, "bold"))
        if not lines.get("big"):
            add(lines.get("sub"), "#6d5f80", ("Segoe UI", 9))
        conf = lines.get("confidence")
        if conf:
            t = cv.create_text(pad + 7 * s, y + 2 * s, text=f"confidence: {conf}", anchor="nw", fill="#3a2d45",
                               font=("Segoe UI", 8, "bold"))
            x0, y0, x1, y1 = cv.bbox(t)
            chip = cv.create_rectangle(x0 - 6 * s, y0 - 1 * s, x1 + 6 * s, y1 + 1 * s,
                                       fill=CONF_COLOR.get(conf, "#e8def5"), outline="")
            cv.tag_lower(chip, t)
            items += [chip, t]
            y = y1 + 3 * s
        x0, y0, x1, y1 = cv.bbox("all")
        w = max(x1 + pad, 170 * s)
        h = y1 + pad
        r = 12 * s
        px_, py_, pw, ph = pet_rect
        l, t_, rr, b = bounds
        cw, ch = int(w + tail + 5), int(h + 5)
        left_side = px_ - cw + 10 * s >= l + 4   # room on the left of the pet?
        ty = min(h - r - 10 * s, max(r + 10 * s, h * 0.55))
        # outline with the tail on the right edge (pointing at the pet); mirrored when placed on the right
        pts = [r, 0, w - r, 0, w, 0, w, r, w, ty - 9 * s, w + tail, ty + 2 * s, w, ty + 7 * s, w, h - r, w, h,
               w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
        if not left_side:
            pts = [(w + tail - v) if i % 2 == 0 else v for i, v in enumerate(pts)]
            for it in items:
                cv.move(it, tail, 0)
        bg = cv.create_polygon([p + 2 for p in pts], fill="#fffaf3", outline="#4b3a55", width=2 * s,
                               smooth=True, splinesteps=16)
        for it in items:
            cv.move(it, 2, 2)
        cv.tag_lower(bg)
        cv.configure(width=cw, height=ch)
        tip_y = py_ + ph * 0.5
        x = int(px_ - cw + 14 * s) if left_side else int(px_ + pw - 14 * s)
        y = int(tip_y - ty - 2)
        x = max(l + 4, min(x, rr - cw - 4))
        y = max(t_ + 4, min(y, b - ch - 4))
        self.top.geometry(f"{cw}x{ch}+{x}+{y}")
        prev = U.foreign_foreground()
        self.top.deiconify()
        make_noactivate(self.top)
        give_back_focus(prev)
        force_topmost(self.top)
        self.visible = True
        self.lines = lines

    def hide(self):
        self.visible = False
        try:
            self.top.withdraw()
        except tk.TclError:
            pass


# ----------------------------------------------------------------------------
# the app
# ----------------------------------------------------------------------------

class PetApp:
    def __init__(self, args, selftest=False, data_override=None):
        self.args = args
        self.selftest = selftest
        self.root = root = tk.Tk()
        root.withdraw()
        self.errors = []
        root.report_callback_exception = self._tk_error
        self.s = s = max(0.75, root.winfo_fpixels("1i") / 96.0)
        self.settings = U.Settings(persist=not selftest)
        self.start = time.time()
        self.t0 = time.monotonic()

        root.title(U.PET_NAME)
        root.overrideredirect(True)
        root.configure(bg=KEY)
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        root.attributes("-topmost", True)
        self.Wpx, self.Hpx = int(PetView.W * s), int(PetView.H * s)
        self.cv = tk.Canvas(root, width=self.Wpx, height=self.Hpx, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        premap(root, full=True)   # Tk grabs the foreground on its first idle; this hands it back at once
        self.view = PetView(self.cv, s)
        self.bubble = Bubble(root, s, self._bubble_clicked)
        self.live_bubble = LiveBubble(root, s, self._live_clicked)
        self.panel = CoachPanel(root, s, self.settings, on_refresh=lambda: self.run_engine("manual"))
        self.panel.game_in_front = game_in_front
        self.panel.hooks = {"setup_saved": lambda: self.run_engine("setup"),
                            "api_refresh": lambda: self.run_api("manual"),
                            "tosu_check": self.tosu_check,
                            "live_note": lambda: self.live_note,
                            "plan_changed": lambda: self.card.visible and self.card.render(),
                            "session_cmd": self.send_cmd,
                            "topmost": force_topmost,
                            "touched": self._touched,
                            "after_show": self._raise_small}
        if selftest:  # never take focus from the game while testing
            self.panel.hooks["before_show"] = make_noactivate
            self.panel.never_focus = True
        self.panel.hooks["pp_refresh"] = self.pp_refresh_clicked
        self.panel.hooks["open_url"] = self.open_url
        self.card = QuickCard(root, s, self.settings, hooks={
            "open_panel": self.open_full_coach,
            "open_url": self.open_url,
            "refresh": lambda: self.run_engine("manual"),
            "changed": self.panel.refresh_today,
            "noactivate": make_noactivate,
            "premap": premap,
            "guard": give_back_focus,
            "topmost": force_topmost,
            "cmd": self.send_cmd,
            "touched": self._touched})
        self.card._build()   # now, not on the first click (its window must exist before it is ever shown)

        # live (tosu via pp_live) state - all touched on the Tk thread only
        self.live_q = queue.Queue()      # LiveWatcher states (any thread -> Tk)
        self.ui_q = queue.Queue()        # (callback, value) from worker threads
        self.watcher = None
        self.live_state = None
        self.live_screen = None
        self.live_last = 0.0             # monotonic time of the last state
        self.live_sig = None
        self.live_dismissed = None
        self.play_hidden = False
        self.results_done = False
        self.last_playing = -1e9         # monotonic time "playing" was last seen (results need a fresh play)
        self.celebrated = set()          # results already celebrated (never twice for the same score)
        # osu! focus handling + break reminders (all Tk thread)
        self.osu_focused = False
        self.faded = False               # 70% opacity while osu! has focus and nobody touched me for a minute
        self.focus_since = None          # monotonic time osu! became the foreground window (None = not in front)
        self.card_auto_hidden = False    # the card was put away for a map (tosu 'playing'): bring it back after
        self.pending_say = None          # (title, body, secs, created) held while osu! has focus
        self.last_interaction = -1e9
        self.active_s = 0.0              # seconds of play since the last break
        self.session_s = 0.0             # seconds of play in this session
        self.idle_s = 0.0                # seconds osu! has been unfocused / closed in a row
        self.break_due = False
        self.session_told = False
        self.score_seen_at = -1e9        # monotonic: a new score file just appeared (= results screen)
        self.unknown_screens = set()
        self.live_trace = []             # (screen, bubble visible, play_hidden, pet visible) for --selftest
        self.pp_celebrations = 0
        self.live_note = ("Live PP bubble: off - " + PP_LIVE_ERR) if PP_LIVE_ERR else "Live PP bubble: not started yet."
        self.hide_play_var = tk.BooleanVar(value=bool(self.settings.get("hide_while_playing", False)))
        self.breaks_var = tk.BooleanVar(value=bool(self.settings.get("break_reminders", True)))
        self.live_var = tk.BooleanVar(value=bool(self.settings.get("live_bubble", True)))
        # PP-first (CONTRACT_PP2.md): trainer debrief pop-ups are opt-in now
        self.debrief_var = tk.BooleanVar(value=bool(self.settings.get("show_debriefs", False)))
        self.recs = {}                   # pp_recs.json
        self.recs_mtime = None
        self.pred_rows = []              # pp_predictions.jsonl (tail)
        self.pred_mtime = None
        self.model_report = None         # pp_model.model_report() (worker thread)
        self.report_next = time.time() + 5
        self.report_busy = False
        self.pred_by_key = {}            # map_key -> the song-select prediction (for the results bubble)
        self.results_shown = []          # results bubbles shown (--selftest)
        self.last_results_at = -1e9      # monotonic: a results bubble was just shown
        self.alt_cache = {}              # (md5, rate) -> other mod choices for the hovered map
        self.alt_busy = None
        self.urls_opened = []            # --selftest: "Open on osu! website" never opens a browser there
        self.pp_job_log = None
        self.pp_jobs = U.PPJobs(self._spawn_pp_job, playing_flag=None if self.selftest else U.BASE / ".pp_playing")
        self.pp_jobs.on_done = self._pp_job_done

        # osu! API fetcher (osu_api.py -> pp_data.json)
        self.pp_data = {}
        self.pp_mtime = None
        self.api_proc = None
        self.api_log = None
        self.api_started = 0.0
        self.api_next = time.time() + 6
        self.api_manual = False

        # animation state
        self.mood = "neutral"
        self.blink_at = time.monotonic() + random.uniform(1.5, 4)
        self.blink_dur = 0.16
        self.look = [0.0, 0.0]
        self.look_target = (0.0, 0.0)
        self.look_at = time.monotonic() + random.uniform(3, 7)
        self.look_until = 0.0
        self.perk = 0.0
        self.hover = False
        self.hop_start = -10.0
        self.hop_amp = 0.0
        self.cheer_until = 0.0
        self.thinking = False
        self.mood_override = None

        # data / engine state
        self.data = {}
        self.data_mtime = None
        self.data_override = data_override
        self.proc = None
        self.proc_log = None
        self.proc_started = 0.0
        self.engine_queued = False
        self.manual_pending = False
        self.last_engine_end = 0.0
        self.realm_mtime = U.mtime(U.REALM_FILE)
        self.realm_due = None
        self.files_sig = U.files_signal()
        self.files_next = 0.0
        self.proc_quiet = False
        self.status = {}
        self.recheck_due = None          # second "new score" run in case the replay wasn't indexed yet
        self.play_ts_at_signal = None
        self.last_focus_wall = 0.0       # time.time() osu! was last the window in front (periodic runs)
        self.engine_requests = []        # reasons run_engine was asked for (--selftest)

        # adaptive loop (CONTRACT_ADAPT.md): debrief after every try, card follows next_action
        self.attempt_due = None          # time.time() to run the engine for a try that just ended
        self.live_attempts_mtime = U.mtime(U.LIVE_ATTEMPTS_FILE)
        self.debrief_seen = txt(self.settings.get("last_debrief_seen")) or None
        self.debrief_last = None
        self.level_seen = None
        self.pending_debrief = None      # (debrief, level) held while a map is playing / Mochi is hidden
        self.level_celebrations = 0
        self.debriefs_shown = []         # verdicts shown (--selftest)
        self.cmds_sent = []
        self.cmd_queue = []              # session commands waiting for coach_adapt to read the previous one

        # visibility state
        self.visible = False
        self.osu_up = False
        self.osu_seen = False
        self.osu_gone_polls = 0
        self.dormant_since = None        # osu! closed (with --exit-with-osu): hidden, watcher stopped, waiting
        self.sleep_until = 0.0
        self.always_var = tk.BooleanVar(value=bool(self.settings.get("always_show", False)))

        # input
        self.drag = None
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<ButtonPress-3>", self._menu)
        self.cv.bind("<Enter>", self._enter)
        self.cv.bind("<Leave>", lambda e: setattr(self, "hover", False))
        self.menu = tk.Menu(root, tearoff=0, bg="#262036", fg="#f4effc", activebackground="#ff9ec7",
                            activeforeground="#221a30", font=("Segoe UI", 10), bd=0)
        self.menu.add_command(label="Your PP + rank-up picks", command=self.toggle_card)
        self.menu.add_command(label="Open full coach (training)", command=self.toggle_panel)
        self.menu.add_command(label="Training: next step", command=self.show_training_card)
        self.menu.add_command(label="Refresh analysis", command=lambda: self.run_engine("manual"))
        self.menu.add_separator()
        self.menu.add_checkbutton(label="Always show (even without osu!)", variable=self.always_var,
                                  command=self._always_changed)
        self.menu.add_checkbutton(label="Hide while playing a map (needs tosu)", variable=self.hide_play_var,
                                  command=self._hide_play_changed)
        self.menu.add_checkbutton(label="Break reminders", variable=self.breaks_var, command=self._breaks_changed)
        self.menu.add_checkbutton(label="Live PP bubble (needs tosu)", variable=self.live_var,
                                  command=self._live_changed)
        self.menu.add_command(label="Sleep 1 hour (hide)", command=self.sleep_hour)
        self.panel.options = [
            ("Always show", "Stay on screen even when osu! isn't running.", self.always_var, self._always_changed),
            ("Break reminders", "A 5-minute break reminder every ~30 minutes of play.", self.breaks_var,
             self._breaks_changed),
            ("Live PP bubble", "In song select, show what the map is worth for you (needs tosu).", self.live_var,
             self._live_changed),
            ("Hide while playing a map", "Off = I stay visible on top of osu! all the time. On = I hide during a "
             "map and come back on the results screen (needs tosu).", self.hide_play_var, self._hide_play_changed),
            ("Show training debriefs after plays", "Off = after a map I just tell you the pp you got (and what I "
             "predicted). On = the trainer's 'How that went' card pops up too.", self.debrief_var,
             self._debrief_changed),
        ]
        self.menu.add_separator()
        self.menu.add_command(label="Reset position", command=self.reset_position)
        self.menu.add_command(label=f"Quit {U.PET_NAME}", command=self.quit)

        self.place_initial()
        self.load_pp_data(force=True)
        self.load_pp_files(force=True)
        self.load_data(force=True)

    # ------------------------------------------------------------ errors
    def _tk_error(self, exc, val, tb):
        msg = "".join(traceback.format_exception(exc, val, tb))
        self.errors.append(msg)
        U.log.error("callback error:\n%s", msg)

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            msg = traceback.format_exc()
            self.errors.append(msg)
            U.log.error("loop error:\n%s", msg)

    # ------------------------------------------------------------ position
    def default_pos(self):
        l, t, r, b = work_area(self.root)
        return int(r - self.Wpx - 14 * self.s), int(b - self.Hpx + 6 * self.s)

    def place_initial(self):
        pos = self.settings.get("pos")
        x = y = None
        if isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, int) for v in pos):
            l, t, r, b = virtual_screen(self.root)
            if l - 20 <= pos[0] <= r - self.Wpx // 2 and t - 20 <= pos[1] <= b - self.Hpx // 2:
                x, y = pos
        if x is None:
            x, y = self.default_pos()
        self.root.geometry(f"{self.Wpx}x{self.Hpx}+{x}+{y}")

    def reset_position(self):
        x, y = self.default_pos()
        self.root.geometry(f"+{x}+{y}")
        self.settings.set("pos", None)

    def pet_rect(self):
        return self.root.winfo_x(), self.root.winfo_y(), self.Wpx, self.Hpx

    # ------------------------------------------------------------ input
    def _enter(self, _e):
        if not self.hover and time.monotonic() - self.hop_start > 1.5:
            self.hop(5, n=1)
        self.hover = True
        self.update_fade()

    def _touched(self):
        self.last_interaction = time.monotonic()
        self.update_fade()

    FADE_AFTER_S = 60

    def update_fade(self):
        """While osu! has focus and nobody touched me for a minute: 70% opacity (full again on hover)."""
        faded = (self.visible and self.osu_focused and not self.hover and not self.card.visible
                 and time.monotonic() - self.last_interaction >= self.FADE_AFTER_S)
        if faded != self.faded:
            self.faded = faded
            try:
                self.root.attributes("-alpha", 0.7 if faded else 1.0)
            except tk.TclError:
                pass

    def _press(self, e):
        self.last_interaction = time.monotonic()
        self.drag = {"x": e.x_root, "y": e.y_root, "wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                     "moved": False}

    def _motion(self, e):
        d = self.drag
        if not d:
            return
        dx, dy = e.x_root - d["x"], e.y_root - d["y"]
        if not d["moved"] and math.hypot(dx, dy) < 5 * self.s:
            return
        if not d["moved"]:
            d["moved"] = True
            self.bubble.hide()
            self.live_bubble.hide()
        self.root.geometry(f"+{d['wx'] + dx}+{d['wy'] + dy}")

    def _release(self, e):
        d, self.drag = self.drag, None
        if not d:
            return
        if d["moved"]:
            self.settings.set("pos", [self.root.winfo_x(), self.root.winfo_y()])
            if self.live_state is not None:
                self.live_sig = None
                self.handle_live(self.live_state, replay=True)
            if self.card.visible:
                self.card.show(self.anchor())   # follow the pet
        else:
            self.pet_clicked()

    def pet_clicked(self):
        """Click on the pet: close whatever is open, else open the PP card (CONTRACT_PP2.md: PP is the primary
        view; a fresh debrief stays up only when training debriefs are on)."""
        self.bubble.hide()
        if self.panel.is_open():
            self.panel.hide()
        elif self.card.visible:
            self.card.hide()
        else:
            ts = num(g(self.card.debrief, "ts")) if self.card.mode == "debrief" else None
            if not (self.card.mode == "debrief" and self.debrief_var.get() and ts is not None
                    and time.time() - ts < 15 * 60):
                self.card.mode = "pp"
            self.card.show(self.anchor())
            self.request_alts()
        self.update_fade()

    def show_training_card(self):
        """The trainer's 'Next up' card (secondary: right-click menu / full coach)."""
        self.bubble.hide()
        self._touched()
        self.card.mode = "next"
        self.card.show(self.anchor())
        self.update_fade()

    def _menu(self, e):
        self.last_interaction = time.monotonic()
        prev = U.foreign_foreground()  # usually the game: it gets keyboard focus back afterwards
        if prev:
            self.panel.prev_fg = prev
        try:
            self.menu.entryconfigure(0, label="Hide the card" if self.card.visible else "Your PP + rank-up picks")
            self.menu.entryconfigure(1, label="Close full coach" if self.panel.is_open() else
                                     "Open full coach (training)")
            self.root.focus_force()  # lets the popup close normally when clicking elsewhere
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
        if prev:
            # after the chosen command has run; skip if it opened the coach panel (it has focus now)
            self.root.after(150, lambda: None if self.panel._is_foreground() else U.restore_foreground(prev))

    def _bubble_clicked(self):
        self.bubble.hide()
        self._touched()
        if not self.card.visible and not self.panel.is_open():
            if self.card.mode != "debrief":
                self.card.mode = "pp"
            self.card.show(self.anchor())
            self.request_alts()

    def anchor(self):
        x, y, w, h = self.pet_rect()
        return x, y, w, h, work_area(self.root)

    def toggle_card(self):
        self.bubble.hide()
        self._touched()
        if not self.card.visible:
            self.card.mode = "pp"
        self.card.toggle(self.anchor())
        self.request_alts()

    def toggle_panel(self):
        self.bubble.hide()
        self._touched()
        if not self.panel.is_open():
            self.card.hide()
        self.panel.toggle(self.anchor())

    def open_full_coach(self, tab="Today"):
        self.card.hide()
        self.bubble.hide()
        if not self.panel.is_open():
            self.panel.show(self.anchor())
        self.panel.select_tab_name(tab or "Today")

    def _raise_small(self):
        """After the coach window opened: Mochi, his bubbles and the card stay above it."""
        if not self.visible:
            return
        force_topmost(self.root)
        for b in (self.bubble, self.live_bubble):
            if b.visible:
                force_topmost(b.top)
        if self.card.visible:
            force_topmost(self.card.top)

    def _breaks_changed(self):
        self.settings.set("break_reminders", bool(self.breaks_var.get()))

    def _always_changed(self):
        self.settings.set("always_show", bool(self.always_var.get()))
        self.update_visibility()

    def _hide_play_changed(self):
        self.settings.set("hide_while_playing", bool(self.hide_play_var.get()))
        self.play_hidden = bool(self.hide_play_var.get()) and self.live_screen == "playing"
        self.update_visibility()

    def _debrief_changed(self):
        self.settings.set("show_debriefs", bool(self.debrief_var.get()))

    def open_url(self, url):
        """'Open on osu! website' for a pick that isn't downloaded (only osu.ppy.sh links)."""
        self._touched()
        url = txt(url)
        if not url.startswith("https://osu.ppy.sh/"):
            return False
        self.urls_opened.append(url)
        if self.selftest:
            return True
        try:
            import webbrowser
            return bool(webbrowser.open(url))
        except Exception:
            return False

    def _live_changed(self):
        self.settings.set("live_bubble", bool(self.live_var.get()))
        self.live_sig = None
        if not self.live_var.get():
            self.live_bubble.hide()
        elif self.live_state is not None:
            self.handle_live(self.live_state, replay=True)

    def _live_clicked(self):
        """Clicking the live card dismisses it until the map or mods change."""
        if self.live_state is not None:
            self.live_dismissed = U.map_key(self.live_state)
        self.live_bubble.hide()

    def sleep_hour(self):
        self.sleep_until = time.time() + 3600
        self.update_visibility()

    def quit(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ animation
    def hop(self, amp=15, n=3):
        self.hop_start = time.monotonic()
        self.hop_amp = amp
        self.hop_n = n

    def tosu_live(self):
        """tosu answered last time and the watcher is still polling it. The watcher only sends a state when
        something changes (nothing does during a map) and sends tosu_ok False as soon as tosu stops answering,
        so the age of the last state means nothing while its thread is alive."""
        if not (isinstance(self.live_state, dict) and self.live_state.get("tosu_ok") is True):
            return False
        w = self.watcher
        t = getattr(w, "_thread", None) if w is not None else None
        if t is not None:
            return t.is_alive()
        return time.monotonic() - self.live_last < 10   # no thread to ask (selftest): only a fresh state counts

    def should_hold(self):
        """Hold chatter while osu! has focus and there's no tosu to tell menus from gameplay (a bubble or a hop
        in the middle of a map is a distraction), unless he just clicked me."""
        if self.selftest or time.monotonic() - self.last_interaction < 90:
            return False
        return not self.visible or (self.osu_focused and not self.tosu_live())

    def say(self, title, body, secs=8.0, urgent=False, big=None):
        if not urgent and self.should_hold():
            self.pending_say = (title, body, secs, time.monotonic(), big)
            return
        if not self.visible and not self.selftest:
            return
        if self.card.visible and not urgent:
            return  # the open card already shows the up-to-date plan; don't cover it
        x, y, w, h = self.pet_rect()
        self.bubble.show(title, body, x + w * 0.46, y + 40 * self.s, secs, work_area(self.root), big=big)

    def frame(self):
        now = time.monotonic()
        t = now - self.t0
        # blink
        if now >= self.blink_at + self.blink_dur:
            self.blink_at = now + random.uniform(2.2, 6.0)
            if random.random() < 0.18:
                self.blink_at = now + 0.28  # double blink
        eye_open = 1.0
        if self.blink_at <= now < self.blink_at + self.blink_dur:
            p = (now - self.blink_at) / self.blink_dur
            eye_open = abs(1 - 2 * p)
        # look around
        if now >= self.look_at:
            self.look_target = (random.uniform(-3.5, 3.5), random.uniform(-2, 1.5))
            self.look_until = now + random.uniform(0.8, 2.0)
            self.look_at = now + random.uniform(4, 10)
        if now >= self.look_until:
            self.look_target = (0.0, 0.0)
        self.look[0] += (self.look_target[0] - self.look[0]) * 0.18
        self.look[1] += (self.look_target[1] - self.look[1]) * 0.18
        # hover perk
        self.perk += ((1.0 if self.hover else 0.0) - self.perk) * 0.2
        # hops
        hop = squash = 0.0
        dt = now - self.hop_start
        n = getattr(self, "hop_n", 3)
        if 0 <= dt < 0.42 * n:
            k = int(dt / 0.42)
            ph = (dt - k * 0.42) / 0.42
            hop = self.hop_amp * (0.62 ** k) * math.sin(math.pi * ph)
            if ph < 0.12 or ph > 0.88:
                squash = 0.5 * (0.62 ** k)
        cheer = 1.0 if now < self.cheer_until else 0.0
        mood = self.mood_override or self.mood
        self.view.draw(t, mood, eye_open, tuple(self.look), self.perk, hop, self.thinking, cheer,
                       self.bubble.visible, squash)

    def tick(self):
        if self.visible or self.selftest:
            self._safe(self.frame)
        now = time.monotonic()
        # ~10 fps while the game has focus and nothing is moving (no need for 30 fps redraws of an always-on-top
        # window over a borderless game); 30 fps while hovered, hopping, dragged or talking
        calm = (self.osu_focused and not self.hover and not self.bubble.visible and now - self.hop_start > 2
                and now >= self.cheer_until and self.drag is None)
        self.root.after(100 if calm else 33, self.tick)

    # ------------------------------------------------------------ data
    def load_data(self, force=False):
        if self.data_override is not None:
            if force:
                self.apply_data(self.data_override, first=True)
            return
        m = U.mtime(U.DATA_FILE)
        if m is None or (m == self.data_mtime and not force):
            return
        d = U.read_json(U.DATA_FILE)
        if d is None:
            return  # half-written or bad: keep the previous data, try again next poll
        self.data_mtime = m
        self.apply_data(d, first=force)

    @staticmethod
    def _content(d):
        return {k: v for k, v in d.items() if k != "generated_at"} if isinstance(d, dict) else d

    def apply_data(self, d, first=False):
        if isinstance(d, dict) and U.session_of(d) is not None:
            pend = list(self.cmd_queue)
            c = U.read_json(U.SESSION_CMD_FILE) if U.mtime(U.SESSION_CMD_FILE) is not None else None
            if isinstance(c, dict) and c.get("cmd") in U.SESSION_CMDS:
                pend.insert(0, c["cmd"])
            for cmd in pend:          # not consumed yet: show the session as it will be once they are
                d = U.session_apply_cmd(d, cmd)
        same = not first and self._content(d) == self._content(self.data)
        self.data = d
        mood = txt(g(d, "mood"), "neutral")
        self.mood = mood if mood in MOODS else "neutral"
        if same:  # e.g. a periodic re-run found nothing new: don't rebuild the panel under the user
            self.panel.data = d if isinstance(d, dict) else {}
            self.panel._set_status_lbl()
            self.card.data = self.panel.data
        else:
            self.panel.set_data(d)
            self.card.set_data(d)
        shown = self.check_debrief(d, first)
        ts =num(g(d, "latest_play_ts"))
        seen = num(self.settings.get("last_seen_play_ts"))
        if ts is not None and (seen is None or ts > seen):
            if shown:
                pass   # the debrief card already covers this play
            elif time.monotonic() - self.last_results_at < 120:
                pass   # the PP results bubble already covered this play
            elif seen is not None and not first:
                self.celebrate_new_play()
            elif seen is not None and first:
                self.root.after(2500, self.celebrate_new_play)  # played while the pet was closed
            self.settings.set("last_seen_play_ts", ts)
        elif not first and self.manual_pending:
            self.say("All caught up!", U.plain(txt(g(d, "headline"), "No new plays since last time.")))
        self.manual_pending = False

    def celebrate_new_play(self):
        d = self.data
        lp = g(d, "latest_play")
        if not self.should_hold():
            self.hop(16, 3)
        self.cheer_until = time.monotonic() + (2.2 if self.mood != "worried" else 1.0)
        if isinstance(lp, dict):
            bits = [U.fmt_mods(g(lp, "mods"))]
            if num(g(lp, "acc")) is not None:
                bits.append(f"{U.fmt_acc(g(lp, 'acc'))}")
            if num(g(lp, "misses")) is not None:
                bits.append(f"{int(num(g(lp, 'misses')))} misses")
            body = f"{txt(g(lp, 'map'), 'that map')} ({', '.join(bits)})"
            if txt(g(lp, "comment")):
                body += "\n" + txt(g(lp, "comment"))
            self.say("You just played:", body)
        else:
            self.say("New play!", U.plain(txt(g(d, "headline"), "I looked at your latest replay.")))

    # ------------------------------------------------------------ adaptive loop (CONTRACT_ADAPT.md)
    @staticmethod
    def adaptive(d):
        return isinstance(g(d, "last_debrief"), dict) or U.session_of(d) is not None

    def check_debrief(self, d, first=False):
        """A new coach_data.last_debrief -> pop the debrief card (and celebrate a level change). -> True if shown."""
        deb = g(d, "last_debrief")
        if not isinstance(deb, dict):
            return False
        sig = U.debrief_sig(deb)
        if not sig or sig == self.debrief_seen:
            return False
        prev, self.debrief_seen, self.debrief_last = self.debrief_last, sig, deb
        self.settings.set("last_debrief_seen", sig)
        ts = num(g(deb, "ts"))
        if ts is not None and ts > 1e11:
            ts /= 1000.0
        age = time.time() - ts if ts is not None else None
        if first and (self.selftest or age is None or age > 300):
            return False   # from before Mochi started: it's in the Today tab, no popup
        if age is not None and age > 3 * 3600:
            return False
        update = U.same_attempt(prev, deb)   # the replay's richer version of a live debrief already shown
        lvl = U.level_change(deb, g(d, "tracks"))
        core = lvl["sig"].rsplit("|", 1)[0] if lvl else None
        celebrate = bool(lvl) and not (update and core == self.level_seen)
        if lvl:
            self.level_seen = core
        if not self.debrief_var.get():
            # PP-first: no trainer pop-up after a play (the PP results bubble is the feedback); the debrief is
            # still in the full coach's Today tab, and the card can show it from there
            self.card.debrief, self.card.level = deb, lvl
            self.card.set_pending(False)
            return False
        self.show_debrief(deb, lvl, celebrate=celebrate, quiet=update)
        return True

    def show_debrief(self, deb, lvl=None, celebrate=False, quiet=False):
        verdict = txt(g(deb, "verdict")).lower()
        playing = self.live_screen == "playing" and self.tosu_live()
        if playing or (not self.visible and not self.selftest):
            self.pending_debrief = (deb, lvl, celebrate, quiet)   # never pop up / hop in the middle of a map
            return
        self.pending_debrief = None
        self.debriefs_shown.append(verdict)
        U.log.info("debrief: %s - %s", verdict or "?", txt(g(deb, "headline"))[:80])
        if celebrate:
            self.level_celebrations += 1
            U.log.info("level change: %s", lvl["text"])
            if lvl["up"]:
                self.hop(18, 3)
                self.cheer_until = time.monotonic() + 2.8
            else:
                self.hop(6, 1)
        elif not quiet and verdict == "pass":
            self.hop(12, 2)
            self.cheer_until = time.monotonic() + 1.6
        elif not quiet and verdict == "close":
            self.hop(6, 1)
        self.card_auto_hidden = False
        if self.panel.is_open():
            self.card.mode, self.card.debrief, self.card.level = "debrief", deb, lvl
            self.card.set_pending(False)
            return   # the Today tab already shows it; don't stack the card on the open coach window
        self.bubble.hide()
        self.live_bubble.hide()
        self.card.show_debrief(deb, self.anchor(), lvl)
        self.update_fade()

    def flush_pending_debrief(self):
        if not self.pending_debrief:
            return
        if self.live_screen == "playing" and self.tosu_live():
            return
        if not self.visible and not self.selftest:
            return
        deb, lvl, celebrate, quiet = self.pending_debrief
        self.pending_debrief = None
        self.show_debrief(deb, lvl, celebrate=celebrate, quiet=quiet)

    def attempt_ended(self, rec=None):
        """tosu: a map just ended (results, fail or quit) -> analyse it in ~2 s (pp_live writes the live attempt,
        the replay follows when lazer saves it). rec (from the watcher): after a fail/quit say what the model
        expected - about 2 of 3 tries end that way and would otherwise get no feedback at all."""
        if isinstance(rec, dict):
            try:
                self.fail_feedback(rec)
            except Exception:
                U.log.error("fail feedback failed", exc_info=True)
        self.attempt_due = min(self.attempt_due or 1e18, time.time() + 0.5)
        self.play_ts_at_signal = num(g(self.data, "latest_play_ts"))
        self.pp_jobs.attempt()           # the pp model learns from this try (pp_model.update, ~20 s later)
        if self.adaptive(self.data) and self.debrief_var.get():
            self.card.set_pending(True)
        U.log.info("live: attempt ended - debrief due")

    def fail_feedback(self, rec):
        md5 = txt(g(rec, "md5"))
        if not md5:
            return
        now = time.monotonic()
        seen = getattr(self, "_fail_said", {})
        self._fail_said = {k: t for k, t in seen.items() if now - t < 300}
        if md5 in self._fail_said:
            return                       # once per map per 5 minutes
        pred = self.pred_by_key.get(U.map_key(rec))
        if pred is None:
            same = [p for k, p in self.pred_by_key.items() if k and k[0] == md5]
            pred = same[-1] if same else None
        note = U.fail_note(rec, pred)
        if note is None:
            return
        self._fail_said[md5] = now
        U.log.info("live: fail feedback %s", note[1])
        self.say(note[0], note[1], 7)

    def send_cmd(self, cmd):
        """Done / Skip / back / restart in an adaptive session: session_cmd.json for coach_adapt, show the result
        at once (optimistic, also the fallback when the engine can't run), then run the engine."""
        self._touched()
        if U.session_of(self.data) is None:
            return False
        self.cmds_sent.append(cmd)
        ok = True
        if self.cmd_waiting():
            self.cmd_queue.append(cmd)   # coach_adapt reads one command per run: the next goes after it
            U.log.info("session cmd %s queued", cmd)
        else:
            ok = U.write_session_cmd(cmd, self.data)
            U.log.info("session cmd %s (%s)", cmd, "written" if ok else "NOT written")
        self.data = U.session_apply_cmd(self.data, cmd)
        self.card.mode = "next"
        self.panel.set_data(self.data)
        self.card.set_data(self.data)
        self.run_engine("cmd")
        return ok

    def cmd_waiting(self):
        """session_cmd.json is still there (coach_adapt deletes it once read) - no time cutoff, or a slow engine run
        would let the next command overwrite it."""
        return U.mtime(U.SESSION_CMD_FILE) is not None

    def flush_cmd_queue(self):
        if self.cmd_queue and not self.cmd_waiting():
            cmd = self.cmd_queue.pop(0)
            U.write_session_cmd(cmd, self.data)
            U.log.info("session cmd %s (from the queue)", cmd)
            self.run_engine("cmd")

    def periodic_due(self, now=None):
        """The safety-net engine run while osu! is open but not in front. Before: every 2 min all night with
        osu! idle. Now: every 2 min only if osu! was in front since the last run (something may have changed),
        otherwise at most every 10 min, and not at all after an hour without osu! in front (new score files and
        finished tries trigger their own runs)."""
        now = time.time() if now is None else now
        if not self.osu_up or self.osu_focused:
            return False
        last = max(self.last_engine_end, self.start)
        since = now - last
        if self.last_focus_wall > last:
            return since > 120
        return now - self.last_focus_wall < 3600 and since > 600

    def greet(self):
        if not U.ENGINE_FILE.exists() and not self.data:
            self.say(f"Hi! I'm {U.PET_NAME}.", "My brain (coach_engine.py) isn't next to me yet, so I can't "
                     "coach you. Put it in my folder and right-click me > Refresh analysis.", 12)
        elif not self.data:
            self.say(f"Hi! I'm {U.PET_NAME}, your coach.", "Let me look at your recent plays... the first time "
                     "can take a minute or two.", 9)
        else:
            hdr = U.pp_header(self.pp_data, self.recs, self.data)
            self.say(f"Hi! I'm {U.PET_NAME}.", (f"You're at {hdr['total_txt']}. " if hdr["total_txt"] else "") +
                     "Click me for what each map is worth for you, and maps that would rank you up.", 9)

    # ------------------------------------------------------------ engine
    def engine_running(self):
        return self.proc is not None and self.proc.poll() is None

    STATUS_STALE_S = 180  # the engine rewrites coach_status.json at every phase; older "running" = dead run

    def external_running(self):
        """An engine we did not start (launcher, or a previous pet) is running right now."""
        if self.engine_running():
            return False
        st = U.read_json(U.STATUS_FILE) or {}
        return g(st, "state") == "running" and time.time() - (num(g(st, "updated")) or 0) < self.STATUS_STALE_S

    def run_engine(self, reason="manual"):
        self.engine_requests.append(reason)
        del self.engine_requests[:-50]
        if self.selftest:
            return
        if reason == "manual":
            self.manual_pending = True
        if self.engine_running():
            self.engine_queued = True
            if reason == "manual":
                self.say("Still thinking...", "I'm already analysing - I'll update when I'm done.", 5)
            return
        if not U.ENGINE_FILE.exists():
            self.manual_pending = False
            self.panel.set_status("coach_engine.py not found")
            if reason == "manual" or (not self.data and reason != "periodic"):
                self.say("I can't think yet!", "coach_engine.py is missing from my folder, so I can only show "
                         "old results (if any).", 8)
            return
        if self.external_running():
            self.engine_queued = True  # someone else (e.g. the launcher) is running it; poll_files retries
            if reason == "manual":
                self.say("Still thinking...", "I'm already analysing - I'll update when I'm done.", 5)
            return
        try:
            self.proc_log = open(U.ENGINE_LOG, "w", encoding="utf-8", errors="replace")
            self.proc = subprocess.Popen([pythonw_exe(), str(U.ENGINE_FILE)], cwd=str(U.BASE),
                                         stdin=subprocess.DEVNULL, stdout=self.proc_log, stderr=self.proc_log,
                                         creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
            self.proc_started = time.time()
            self.engine_queued = False
            self.proc_quiet = reason in ("periodic", "api")  # background check: no thinking animation
            if not self.proc_quiet:
                self.panel.set_status("analysing your replays...")
            U.log.info("engine started (%s)", reason)
        except Exception as e:
            U.log.error("could not start engine: %s", e)
            self.proc = None
            self.say("Oops!", "I couldn't start my analysis. Details are in pet_log.txt.", 8)

    def check_engine(self):
        if self.proc is None:
            return
        rc = self.proc.poll()
        if rc is None:
            if time.time() - self.proc_started > 1800:
                U.log.warning("engine ran for 30 min - stopping it")
                try:
                    self.proc.kill()
                except Exception:
                    pass
            return
        self.proc = None
        try:
            self.proc_log.close()
        except Exception:
            pass
        self.last_engine_end = time.time()
        if rc != 0:
            tail = ""
            try:
                tail = U.ENGINE_LOG.read_text("utf-8", "replace").strip().splitlines()[-1][:160]
            except Exception:
                pass
            U.log.error("engine exited with %s: %s", rc, tail)
            self.panel.set_status("last analysis failed - see pet_engine_log.txt")
            if not self.proc_quiet or self.manual_pending:
                self.say("Hmm, my analysis tripped up.", "I'll keep showing the last results. "
                         + (f"({tail})" if tail else "Details are in pet_engine_log.txt."), 9)
            self.manual_pending = False
        else:
            self.panel.set_status("")
            self.load_data()
            if self.manual_pending:  # data unchanged on disk
                self.manual_pending = False
                self.say("All caught up!", txt(g(self.data, "headline"), "Nothing new since last time."), 6)
        if self.engine_queued:
            self.engine_queued = False
            self.root.after(2000, lambda: self.run_engine("queued"))

    def poll_files(self):
        def body():
            self.check_engine()
            self.load_data()
            self.check_api()
            self.load_pp_data()
            self.check_play_hidden()
            st = U.read_json(U.STATUS_FILE) or {}
            own = self.engine_running()
            running_ext = self.external_running()
            if self.engine_queued and not own and not running_ext:
                self.engine_queued = False  # the run we deferred for an external engine
                self.run_engine("queued")
                own = self.engine_running()
            self.thinking = (own and not self.proc_quiet) or running_ext
            if running_ext:
                self.panel.set_status(txt(g(st, "message"), "analysing..."))
            elif own and not self.proc_quiet and txt(g(st, "message")):
                self.panel.set_status(txt(g(st, "message")))
            now = time.time()
            # new score saved by lazer. client.realm is written through a memory map, so its mtime
            # rarely moves; the saved replay is a new file in files/?/??, which bumps that folder's
            # mtime. Both are stat() only.
            m = U.mtime(U.REALM_FILE)
            if m is not None and m != self.realm_mtime:
                self.realm_mtime = m
                self.realm_due = now + 15  # debounce
            if now >= self.files_next:
                self.files_next = now + 3
                fs = U.files_signal()
                if fs is not None and fs != self.files_sig:
                    if self.files_sig is not None:
                        self.pp_jobs.attempt(now)   # a new replay: the pp model learns from it too
                        # the debrief must land within seconds: run soon, and once more a bit later in case
                        # lazer hadn't finished indexing the replay
                        self.realm_due = now + 3
                        self.recheck_due = now + 25
                        self.play_ts_at_signal = num(g(self.data, "latest_play_ts"))
                        self.score_seen_at = time.monotonic()   # a score was just saved: results screen
                    self.files_sig = fs
            # pp_live appended a live attempt (fails and quits too): debrief it now
            lm = U.mtime(U.LIVE_ATTEMPTS_FILE)
            if lm != self.live_attempts_mtime:
                self.live_attempts_mtime = lm
                if lm is not None:
                    self.attempt_due = min(self.attempt_due or now + 0.5, now + 0.5)
                    self.pp_jobs.attempt(now)
            self.load_pp_files()
            self.pp_jobs.tick(now, busy=self.pp_busy(), busy_api=self.api_running())
            if now >= self.report_next:
                self.load_model_report()
            if self.attempt_due and now >= self.attempt_due:
                self.attempt_due = None
                self.run_engine("attempt")   # queued behind a run in progress
            # safety net while the game is open (see periodic_due: no more runs every 2 min all night)
            if not self.realm_due and not own and not running_ext and self.periodic_due(now):
                self.last_engine_end = now  # don't retry every second if it can't start
                self.run_engine("periodic")
            if self.realm_due and time.time() >= self.realm_due:
                if time.time() - self.last_engine_end < 8 and not self.engine_running():
                    self.realm_due = self.last_engine_end + 8
                else:
                    self.realm_due = None
                    self.run_engine("new score")
            if self.recheck_due and now >= self.recheck_due and not self.realm_due and not own:
                self.recheck_due = None
                if num(g(self.data, "latest_play_ts")) == self.play_ts_at_signal:
                    self.run_engine("new score (recheck)")
            if self.card.pending_since and now - self.card.pending_since > 45:
                self.card.set_pending(False)
            self.flush_pending_debrief()
            self.flush_cmd_queue()
        self._safe(body)
        self.root.after(1000, self.poll_files)

    # ------------------------------------------------------------ live (tosu)
    def start_live(self):
        if self.selftest:
            self.live_note = "Live PP bubble: selftest (fake tosu states)."
            return
        if pp_live is None:
            U.log.info("live PP off: %s", PP_LIVE_ERR)
            return
        try:
            # on_change may be called from the watcher's thread: only enqueue there, handle on the Tk thread
            # on_attempt (pp_live with live_attempts.jsonl): a try ended -> debrief run on the Tk thread
            on_attempt = lambda rec: self.ui_q.put((lambda r: self.attempt_ended(r), rec))  # noqa: E731
            try:
                self.watcher = pp_live.LiveWatcher(on_change=self.live_q.put, on_attempt=on_attempt)
            except TypeError:   # older pp_live without attempt telemetry
                self.watcher = pp_live.LiveWatcher(on_change=self.live_q.put)
            self.watcher.start()
            self.live_note = "Live PP bubble: on - waiting for tosu (start tosu.exe before osu!)."
            U.log.info("live watcher started")
        except Exception as e:
            self.watcher = None
            self.live_note = f"Live PP bubble: off - pp_live failed to start ({type(e).__name__}: {e})"
            U.log.error("live watcher failed: %s", traceback.format_exc())

    def stop_live(self):
        w, self.watcher = self.watcher, None
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

    def tosu_check(self, cb):
        """Panel tosu status: one real HTTP check in a worker thread, answered on the Tk thread."""
        def work():
            try:
                ok = U.tosu_probe(timeout=1.0)   # "ok" | "waiting" (tosu up, can't see osu!) | "down"
            except Exception:
                ok = "down"
            self.ui_q.put((cb, ok))
        threading.Thread(target=work, daemon=True).start()

    def _tosu_hint(self):
        """tosu only exists inside OneDrive (where it refuses to start): say once how to fix it."""
        st = U.tosu_status()
        if st["onedrive"] and st["movable"] is not None and not self.tosu_live():
            U.log.info("tosu only found inside OneDrive: %s", st["exe"])
            self.say("tosu can't start from OneDrive", "Right-click me > Open full coach > Settings and press "
                     "\"Move tosu out of OneDrive\". Then I can judge every try, even fails and quits.", 12)

    def poll_live(self):
        self._safe(self.drain_live)
        try:
            self.root.after(120, self.poll_live)
        except tk.TclError:
            pass

    def drain_live(self):
        """Run on the Tk thread: worker callbacks, then queued LiveWatcher states (only the newest state
        per screen is shown, but every screen change is processed in order)."""
        for _ in range(60):
            try:
                cb, val = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                cb(val)
            except Exception:
                U.log.error("ui callback failed:\n%s", traceback.format_exc())
        latest = None
        for _ in range(200):
            try:
                st = self.live_q.get_nowait()
            except queue.Empty:
                break
            if latest is not None and U.screen_of(st) != U.screen_of(latest):
                self.handle_live(latest)  # keep screen transitions (e.g. playing -> results)
            latest = st
        if latest is not None:
            self.handle_live(latest)

    def handle_live(self, st, replay=False):
        if not isinstance(st, dict):
            return
        if not replay:
            self.live_last = time.monotonic()
        self.live_state = st
        ok = st.get("tosu_ok") is True
        raw = txt(g(st, "screen")).lower()
        if raw and raw not in U.SCREENS and raw not in self.unknown_screens:
            self.unknown_screens.add(raw)
            U.log.info("live: unknown screen %r (treated as 'other')", raw)
        screen = U.screen_of(st) if ok else "other"
        prev, self.live_screen = self.live_screen, screen
        if prev == "playing" and screen != "playing" and not replay:
            self.attempt_ended()   # passed, failed or quit: the debrief follows within seconds
        if ok and self.watcher is not None and "waiting" in self.live_note:
            self.live_note = "Live PP bubble: on - tosu connected."
        if screen == "playing":
            self.last_playing = time.monotonic()
        # hide while playing (opt-in; only while the game is the window in front, like check_play_hidden)
        hide = screen == "playing" and bool(self.hide_play_var.get()) and self.osu_focused
        if hide != self.play_hidden:
            self.play_hidden = hide
            U.log.info("live: %s", "playing - hiding" if hide else f"{screen} - showing again")
            self.update_visibility()
        # the card and the coach window never cover a map: put them away while playing, card back afterwards
        if screen == "playing":
            if self.card.visible or self.panel.is_open():
                self.card_auto_hidden = True
                self.card.hide()
                self.panel.hide()
                self.update_fade()
        elif self.card_auto_hidden and screen in ("results", "song_select", "menu") and not (
                screen == "results" and U.results_view(st) is not None):
            # (on a results screen with a score the PP results bubble shows first; the card is back at song select)
            self.card_auto_hidden = False
            if self.visible and not self.panel.is_open():
                self.card.show(self.anchor())
        # while scrolling the watcher sends a "working it out..." state for every map it passes: render those at
        # most every 0.3 s (a finished prediction always renders)
        tnow = time.monotonic()
        skip_pending = (not replay and g(st, "prediction_pending") is True and
                        tnow - getattr(self, "_live_render_at", -1e9) < 0.3)
        if not skip_pending and screen in ("song_select", "results"):
            self._live_render_at = tnow
        # the PP card follows the map selected in osu! (and its mods / rate)
        if skip_pending:
            pass
        elif ok and screen in ("song_select", "results") and (txt(g(st, "md5")) or txt(g(st, "title"))):
            if screen == "song_select" and isinstance(g(st, "prediction"), dict):
                self.pred_by_key[U.map_key(st)] = st["prediction"]
                if len(self.pred_by_key) > 200:
                    self.pred_by_key.pop(next(iter(self.pred_by_key)))
            if self.card.set_pp(live=st, alts=self.alt_cache.get(self._alt_key(st), []), tosu_note=None):
                self.request_alts()
        elif not ok and self.card.live is not None and not replay:
            try:
                have_tosu = U.tosu_status()["exe"] is not None
            except Exception:
                have_tosu = True
            self.card.set_pp(live=None, tosu_note=(
                "tosu isn't answering, so I can't see the map you selected. Start tosu (Open full coach > "
                "Settings) to get live predictions." if have_tosu else
                "I need tosu to see the map you selected. Get it in Open full coach > Settings > \"Open tosu "
                "download page\" (free, from github.com/tosuapp/tosu), then start osu! with \"Play osu with "
                "Coach\"."))
        # song-select card
        if screen == "song_select" and self.live_var.get():
            if not skip_pending:
                self.show_live(st)
        elif self.live_bubble.visible:
            self.live_bubble.hide()
        if screen != "song_select":
            self.live_dismissed = None
            self.live_sig = None
        # results: celebrate PP gained - only for a results screen reached straight from playing (not a
        # leaderboard score, a replay or an old score re-opened), once per score, and only for his own play
        if screen != "results":
            self.results_done = False
        elif not self.results_done:
            gain = U.results_gain(st)
            sig = U.results_sig(st)
            fresh = prev == "playing" or time.monotonic() - self.last_playing < 30
            mine = self._results_mine(st)
            if gain is not None or num(g(g(st, "results"), "pp")) is not None:
                self.results_done = True
                if fresh and mine and sig not in self.celebrated:
                    self.celebrated.add(sig)
                    self.last_playing = -1e9
                    self.celebrate_pp(gain, g(st, "results"), st)
                else:
                    U.log.info("live: results screen not celebrated (fresh=%s, own=%s, seen=%s)",
                               fresh, mine, sig in self.celebrated)
        self.live_trace.append((screen, self.live_bubble.visible, self.play_hidden, self.visible))
        del self.live_trace[:-50]

    def _results_mine(self, st):
        """False only when tosu names a results player that isn't him (absent names = can't tell = mine)."""
        who = txt(g(g(st, "results"), "player")).lower()
        me = {txt(g(st, "profile_name")).lower(), txt(g(g(self.pp_data, "user"), "username")).lower()} - {""}
        return not (who and me and who not in me)

    def check_play_hidden(self, force=False):
        """Every second (not in selftest): only when the user turned on 'Hide while playing' AND tosu says
        'playing', stay hidden - and only while the game window is in front, so alt-tabbing (or a watcher that
        went quiet) never leaves the pet stuck hidden. Otherwise Mochi always stays visible above osu!
        (fading to 70% after a minute without interaction). Also: hold chatter while osu! has focus; breaks."""
        if self.selftest:
            return
        focused = bool(self.osu_up) and game_in_front()
        self.osu_focused = focused
        now = time.monotonic()
        if not focused:
            self.focus_since = None
        else:
            self.last_focus_wall = time.time()
            if self.focus_since is None:
                self.focus_since = now
        self._auto_put_away(now)
        want = self.live_screen == "playing" and bool(self.hide_play_var.get()) and focused and self.tosu_live()
        changed = want != self.play_hidden
        self.play_hidden = want
        if changed or force:
            self.update_visibility()
        self.update_fade()
        self._break_tick(focused)
        if self.pending_say and not self.should_hold():
            title, body, secs, t0 = self.pending_say[:4]
            big = self.pending_say[4] if len(self.pending_say) > 4 else None
            self.pending_say = None
            if time.monotonic() - t0 < 600:
                self.say(title, body, secs, big=big)

    PANEL_AWAY_S = 10      # coach window: closes after the game has been in front this long without use
    CARD_AWAY_S = 45       # card: put away after this long in the game without use when tosu can't tell song
                           # select from a map (with tosu it goes away exactly while 'playing')

    def _auto_put_away(self, now):
        """The coach window and the card must never sit over a map. Mochi himself stays (and fades)."""
        if self.focus_since is None:
            return
        in_game = now - self.focus_since
        idle = now - self.last_interaction
        if self.panel.is_open() and in_game >= self.PANEL_AWAY_S and idle >= self.PANEL_AWAY_S:
            U.log.info("osu! in front for %.0f s: closing the coach window, showing the card", in_game)
            self.panel.hide()
            if self.visible and U.has_plan(self.data):
                self.card.show(self.anchor())        # the small card keeps the next step in view
                self.focus_since = now                # and gets its own full time before it goes away
        if self.card.visible and not self.tosu_live() and in_game >= self.CARD_AWAY_S \
                and idle >= self.CARD_AWAY_S:
            U.log.info("osu! in front for %.0f s: putting the card away", in_game)
            self.card.hide()
            self.update_fade()

    BREAK_AFTER_S = 30 * 60      # health-breaks: 5 minutes off every 30-40 minutes of play
    BREAK_RESET_S = 5 * 60       # this long away from osu! counts as a break taken

    def _break_tick(self, focused):
        playing = self.osu_up and (focused or self.live_screen == "playing")
        if playing:
            self.active_s += 1
            self.session_s += 1
            self.idle_s = 0
        else:
            self.idle_s += 1
            if self.idle_s >= self.BREAK_RESET_S:
                self.active_s, self.break_due = 0.0, False
            if self.idle_s >= 30 * 60 or not self.osu_up and self.idle_s >= self.BREAK_RESET_S:
                self.session_s, self.session_told = 0.0, False
        if not self.breaks_var.get():
            return
        plan_min = sum(num(g(b, "minutes")) or 0 for b in U.dicts(g(self.data, "session_plan"))) or 75
        if self.active_s >= self.BREAK_AFTER_S:
            self.break_due = True
        # show it at a safe moment: tosu says menu / song select / results, or (no tosu) a score was just
        # saved (= results screen), or osu! isn't the focused window
        safe = (self.live_screen in ("song_select", "results", "menu") if self.tosu_live()
                else (time.monotonic() - self.score_seen_at < 20 or not focused))
        if not safe or self.play_hidden:
            return
        if not self.session_told and self.session_s >= plan_min * 60:
            self.session_told, self.break_due, self.active_s = True, False, 0.0
            self._remind(f"That's today's {plan_min:.0f}-minute plan!",
                         "Good place to stop for today. More isn't better when your hands are tired - "
                         "and stop straight away if your wrist or hand hurts.")
        elif self.break_due:
            self.break_due, self.active_s = False, 0.0
            self._remind("Break time (5 min)",
                         "Stand up, let your hands hang loose, look away from the screen. Stop for today if "
                         "your wrist or hand hurts, tingles or feels numb.")

    def _remind(self, title, body):
        self.say(title, body, 12, urgent=True)

    def _top100(self):
        th = g(self.pp_data, "pp_threshold")
        return num(g(th, "top100"))

    def show_live(self, st):
        lines = U.live_lines(st, self._top100(), self.pp_data, self.recs, self.alt_cache.get(self._alt_key(st)))
        key = U.map_key(st)
        if not lines or key == self.live_dismissed or not self.visible or self.card.visible:
            self.live_bubble.hide()
            return
        sig = (key, tuple(sorted((k, str(v)) for k, v in lines.items())))
        if sig == self.live_sig and self.live_bubble.visible:
            return
        self.live_sig = sig
        self.live_bubble.show(lines, self.pet_rect(), work_area(self.root))

    def celebrate_pp(self, gain, res, st=None):
        """Results bubble (the default after-play feedback): 'You got 83pp' / 'I predicted 74 - inside my range' /
        '+4.2pp to your total'. The pp model learns from this try (pp_model.update is scheduled)."""
        self.pp_celebrations += 1
        st = st if isinstance(st, dict) else {"results": res}
        rv = U.results_view(st, self.pred_by_key.get(U.map_key(st)), self.pp_data)
        if rv is None:   # no pp number: the old gain-only line
            g_txt = f"+{gain:.0f}pp" if (gain or 0) >= 1 else f"+{gain or 0:.1f}pp"
            rv = {"title": f"~{g_txt} to your total", "body": "Proud of you! Shake your hands out before the next one.",
                  "gain": gain, "within": None, "pp": None, "predicted": None}
        U.log.info("live: results bubble %s (gain %s, predicted %s, within %s)", rv["title"], rv["gain"],
                   rv["predicted"], rv["within"])
        self.results_shown.append(rv)
        del self.results_shown[:-20]
        self.last_results_at = time.monotonic()
        if (rv["gain"] or 0) >= 1 or rv["within"]:
            self.hop(18, 3)
            self.cheer_until = time.monotonic() + 2.6
        else:
            self.hop(8, 1)
        self.pp_jobs.attempt()
        if (rv["gain"] or 0) >= 0.5 and time.time() - self.api_started > 600:
            self.api_next = min(self.api_next, time.time() + 90)   # new total from osu! soon after it submits
        self.say("That run:", rv["body"], 11, big=rv["title"])

    # ------------------------------------------------------------ osu! API (osu_api.py -> pp_data.json)
    def api_running(self):
        return self.api_proc is not None and self.api_proc.poll() is None

    def run_api(self, reason="periodic"):
        if self.selftest:
            return "Selftest - not fetching."
        if self.api_running():
            return "Already fetching..."
        if not U.API_SCRIPT.exists():
            return "osu_api.py is missing from my folder."
        if not U.api_configured():
            return "Fill in osu_api_config.json first (Settings tab)."
        cmd = [pythonw_exe(), str(U.API_SCRIPT)]
        try:
            if reason == "manual" and "--force" in U.API_SCRIPT.read_text("utf-8", "replace"):
                cmd.append("--force")
        except OSError:
            pass
        try:
            self.api_log = open(U.API_LOG, "w", encoding="utf-8", errors="replace")
            self.api_proc = subprocess.Popen(cmd, cwd=str(U.BASE), stdin=subprocess.DEVNULL, stdout=self.api_log,
                                             stderr=self.api_log, creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
            self.api_started = time.time()
            self.api_next = time.time() + 1800
            self.api_manual = reason == "manual"
            U.log.info("osu_api started (%s)", reason)
            return "Fetching your profile from osu!..."
        except Exception as e:
            U.log.error("could not start osu_api.py: %s", e)
            self.api_proc = None
            self.api_next = time.time() + 300
            return "Couldn't start osu_api.py - see pet_log.txt."

    def check_api(self):
        now = time.time()
        if self.api_proc is None:
            if now >= self.api_next and not self.selftest:
                if U.API_SCRIPT.exists() and U.api_configured():
                    self.run_api("periodic")
                else:
                    self.api_next = now + 30  # cheap re-check: the user may fill in the config any time
            return
        rc = self.api_proc.poll()
        if rc is None:
            if now - self.api_started > 600:
                U.log.warning("osu_api ran for 10 min - stopping it")
                try:
                    self.api_proc.kill()
                except Exception:
                    pass
            return
        self.api_proc = None
        try:
            self.api_log.close()
        except Exception:
            pass
        if rc == 0:
            self.load_pp_data()
            self.run_engine("api")  # merges pp_data.json into coach_data.json
        else:
            tail = ""
            try:
                tail = U.API_LOG.read_text("utf-8", "replace").strip().splitlines()[-1][:160]
            except Exception:
                pass
            U.log.error("osu_api exited with %s: %s", rc, tail)
            if self.api_manual:
                self.say("I couldn't reach osu!", (tail or "Details are in pet_api_log.txt.")
                         + "\nCheck osu_api_config.json (Settings tab).", 9)
        self.api_manual = False

    def load_pp_data(self, force=False):
        if self.selftest:
            return
        m = U.mtime(U.PP_DATA_FILE)
        if m is None or (m == self.pp_mtime and not force):
            return
        d = U.read_json(U.PP_DATA_FILE)
        if d is None:
            return
        self.pp_mtime = m
        self.pp_data = d
        self.panel.set_pp_data(d)
        self.card.set_pp(pp_data=d)

    # ------------------------------------------------------------ PP-first (CONTRACT_PP2.md)
    def load_pp_files(self, force=False):
        """pp_recs.json (pp_recommend) and pp_predictions.jsonl (pp_model) - re-read only when they change."""
        if self.selftest:
            return
        m = U.mtime(U.PP_RECS_FILE)
        if m is not None and (m != self.recs_mtime or force):
            d = U.read_json(U.PP_RECS_FILE)
            if d is not None:
                self.recs_mtime = m
                self.set_pp_inputs(recs=d)
        m = U.mtime(U.PP_PRED_LOG)
        if m is not None and (m != self.pred_mtime or force):
            self.pred_mtime = m
            self.set_pp_inputs(rows=U.read_pred_log(U.PP_PRED_LOG))

    def set_pp_inputs(self, recs=None, rows=None, report=None):
        if recs is not None:
            self.recs = recs if isinstance(recs, dict) else {}
            self.card.set_pp(recs=self.recs)
            self.panel.set_pp_extras(recs=self.recs)
        if rows is not None:
            self.pred_rows = rows if isinstance(rows, list) else []
            self.card.set_pp(rows=self.pred_rows)
            self.panel.set_pp_extras(rows=self.pred_rows)
        if report is not None:
            self.model_report = report if isinstance(report, dict) else None
            self.card.set_pp(report=self.model_report)
            self.panel.set_pp_extras(report=self.model_report)

    @staticmethod
    def _low_thread():
        try:   # worker threads never compete with the game: THREAD_PRIORITY_BELOW_NORMAL
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:
            pass

    def load_model_report(self):
        """pp_model.model_report() in a worker thread (pp_recs.json's copy is used until it answers)."""
        self.report_next = time.time() + 900
        if self.selftest or self.report_busy or not U.PP_MODEL_SCRIPT.exists():
            return
        self.report_busy = True

        def work():
            self._low_thread()
            rep = None
            try:
                import pp_model   # noqa: F401  (pp_live imports it too)
                fn = getattr(pp_model, "model_report", None)
                rep = fn() if callable(fn) else None
            except Exception as e:
                U.log.info("model_report unavailable: %s: %s", type(e).__name__, e)
            self.ui_q.put((self._report_done, rep))
        threading.Thread(target=work, name="mochi-model-report", daemon=True).start()

    def _report_done(self, rep):
        self.report_busy = False
        if isinstance(rep, dict):
            self.set_pp_inputs(report=rep)

    @staticmethod
    def _alt_key(st):
        return (txt(g(st, "md5")), round(num(g(st, "rate")) or 1.0, 2))

    ALT_MODS = ("NM", "HD", "HR", "HDHR")

    def request_alts(self):
        """Best-mod tip for the map on the PP card: if its prediction doesn't already compare mods, ask pp_model
        for NM / HD / HR / HDHR in a worker thread (once per map, only while the card shows it, paced)."""
        c = self.card
        st = c.live
        if self.selftest or not (c.visible and c.mode == "pp") or not isinstance(st, dict):
            return
        pred = g(st, "prediction")
        md5 = txt(g(st, "md5"))
        if not md5 or not isinstance(pred, dict) or U.pred_view(pred) is None or g(st, "ranked") is False:
            return
        if U.mod_alternatives(pred, self.recs, md5):
            return   # the prediction / the picks already compare mods
        key = self._alt_key(st)
        if key in self.alt_cache:
            c.set_pp(alts=self.alt_cache[key])
            return
        if self.alt_busy or not U.PP_MODEL_SCRIPT.exists():
            return
        self.alt_busy = key
        cur = U.norm_mods(g(st, "mods"))
        speed = "DT" if "DT" in cur else ("HT" if "HT" in cur else "")
        rate = num(g(st, "rate")) or 1.0
        bid = U.pos_int(g(st, "beatmap_id"))
        variants = [U.norm_mods(m + speed) for m in self.ALT_MODS]

        def work():
            self._low_thread()
            out = []
            try:
                import pp_model
                fn = getattr(pp_model, "predict_pp", None)
                path = pp_live.osu_path_for_md5(md5) if pp_live is not None and \
                    hasattr(pp_live, "osu_path_for_md5") else None
                for mods in variants:
                    if not callable(fn) or mods == cur:
                        continue
                    try:
                        try:   # pp_model: no network here; tosu calculates the map selected in game (checked)
                            res = fn(md5, mods, rate, osu_path=path, beatmap_id=bid, network=False, tosu=True)
                        except TypeError:
                            res = fn(md5, mods, rate, osu_path=path)
                        v = U.pred_view(res)
                    except Exception as e:
                        U.log.info("alt prediction %s failed: %s: %s", mods, type(e).__name__, e)
                        v = None
                    if v and v["expected"] is not None:
                        out.append({"mods": mods, "expected": v["expected"], "gain": v["gain"], "p_pass": v["p_pass"]})
                    time.sleep(0.4)   # tosu: a few calculations per variant, keep it gentle
            except Exception as e:
                U.log.info("alt predictions unavailable: %s: %s", type(e).__name__, e)
            self.ui_q.put((self._alts_done, (key, out)))
        threading.Thread(target=work, name="mochi-mod-tips", daemon=True).start()

    def _alts_done(self, kv):
        key, out = kv
        self.alt_busy = None
        self.alt_cache[key] = out
        if len(self.alt_cache) > 100:
            self.alt_cache.pop(next(iter(self.alt_cache)))
        if isinstance(self.card.live, dict) and self._alt_key(self.card.live) == key:
            self.card.set_pp(alts=out)
        if self.live_bubble.visible and isinstance(self.live_state, dict) and self._alt_key(self.live_state) == key:
            self.live_sig = None
            self.show_live(self.live_state)

    def pp_busy(self):
        """No pp jobs start while a map is being played (or the trainer engine is analysing)."""
        return (self.live_screen == "playing" and self.tosu_live()) or self.engine_running()

    def _spawn_pp_job(self, kind, code):
        script = U.PP_MODEL_SCRIPT if kind == "update" else U.PP_RECOMMEND_SCRIPT
        if self.selftest or not script.exists():
            return None
        try:
            if U.PP_JOB_LOG.exists() and U.PP_JOB_LOG.stat().st_size > 1_000_000:
                U.PP_JOB_LOG.unlink()
        except OSError:
            pass
        lg = open(U.PP_JOB_LOG, "a", encoding="utf-8", errors="replace")
        lg.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {kind}\n")
        lg.flush()
        try:
            proc = subprocess.Popen([pythonw_exe(), "-c", code], cwd=str(U.BASE), stdin=subprocess.DEVNULL,
                                    stdout=lg, stderr=lg, creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
        except Exception:
            lg.close()
            raise
        self.pp_job_log = lg
        return proc

    def _pp_job_done(self, kind, rc):
        lg, self.pp_job_log = self.pp_job_log, None
        if lg is not None:
            try:
                lg.close()
            except Exception:
                pass
        self.load_pp_files()
        if kind == "update" and rc == 0:
            self.alt_cache.clear()          # the model changed: new tips
            self.report_next = 0            # fresh model_report on the next poll

    def pp_refresh_clicked(self):
        """Panel 'Find new picks now'."""
        if not U.PP_RECOMMEND_SCRIPT.exists():
            return "pp_recommend.py isn't in my folder yet."
        if self.pp_jobs.running():
            self.pp_jobs.request("recommend")
            return "Busy learning - I'll look for picks right after."
        self.pp_jobs.request("recommend")
        return "Looking for maps that would rank you up... (takes a minute or two)"

    # ------------------------------------------------------------ visibility
    def update_visibility(self):
        now = time.time()
        want = (self.always_var.get() or self.osu_up or now - self.start < 20) and now >= self.sleep_until \
            and not self.play_hidden
        if want and not self.visible:
            prev = U.foreign_foreground()
            self.root.deiconify()
            self.root.update_idletasks()
            make_noactivate(self.root)
            force_topmost(self.root)
            give_back_focus(prev)
            self.visible = True
        elif not want and self.visible:
            self.root.withdraw()
            self.bubble.hide()
            self.live_bubble.hide()
            self.card.hide()
            self.visible = False

    def poll_osu(self):
        def body():
            up = osu_running()
            if up and not self.osu_up and self.osu_seen is False and time.time() - self.start > 5:
                self.root.after(1500, lambda: self.say("osu! time!", "Hover a map in song select and I'll tell "
                                                                     "you the pp you'd get. Click me for rank-up "
                                                                     "picks.", 7))
            if up:
                self.osu_seen = True
                self.osu_gone_polls = 0
            else:
                self.osu_gone_polls += 1
                if self.live_screen is not None:  # game closed: forget any 'playing' / song-select state
                    self.live_screen = None
                    self.play_hidden = False
                    self.live_bubble.hide()
                self.osu_focused = False
            self.osu_up = up
            if self.args.exit_with_osu:
                # osu! closed: go dormant instead of exiting, so a game started again without the .bat (or a
                # lazer self-update restart) still gets coached. ~45 s grace before going dormant; exit only after
                # 30 min with no osu!.
                now = time.time()
                gone = (self.osu_seen and self.osu_gone_polls >= 15) or (not self.osu_seen and now - self.start > 120)
                if self.dormant_since is None and gone:
                    self.dormant_since = now
                    self.stop_live()
                    if self.pp_jobs.tries_since_rec:
                        self.pp_jobs.request("recommend")   # session over: fresh rank-up picks for next time
                    U.log.info("osu! closed - dormant (waiting up to 30 min for it to come back)")
                elif self.dormant_since is not None and up:
                    self.dormant_since = None
                    if self.watcher is None:
                        self.start_live()
                    U.log.info("osu! is back - awake again")
                elif self.dormant_since is not None and now - self.dormant_since > 1800:
                    U.log.info("osu! closed for 30 min - exiting")
                    self.quit()
                    return
            self.update_visibility()
        self._safe(body)
        try:
            self.root.after(3000, self.poll_osu)
        except tk.TclError:
            pass

    def keep_topmost_once(self):
        # the coach window first, then Mochi, his bubbles and the card on top of it (in that order)
        self.panel.keep_on_top()
        if self.visible:
            make_noactivate(self.root)
            force_topmost(self.root)
            if self.bubble.visible:
                force_topmost(self.bubble.top)
            if self.live_bubble.visible:
                force_topmost(self.live_bubble.top)
            if self.card.visible:
                make_noactivate(self.card.top)
                force_topmost(self.card.top)

    def keep_topmost(self):
        self._safe(self.keep_topmost_once)
        self.root.after(2000, self.keep_topmost)

    # ------------------------------------------------------------ run
    def run(self):
        self.osu_up = osu_running()
        self.osu_seen = self.osu_up
        self.update_visibility()
        self.tick()
        self.root.after(1000, self.poll_files)
        self.root.after(3000, self.poll_osu)
        self.root.after(2000, self.keep_topmost)
        self.root.after(1200, self.greet)
        self.root.after(12000, lambda: self._safe(self._tosu_hint))
        self.root.after(2500, lambda: self.run_engine("startup"))
        self.root.after(1500, lambda: self._safe(self.start_live))
        self.root.after(300, self.poll_live)
        try:
            self.root.mainloop()
        finally:
            self.stop_live()


# ----------------------------------------------------------------------------
# selftest
# ----------------------------------------------------------------------------

def z_order(*widgets):
    """Positions of the widgets' windows in the Win32 z-order (0 = top); None where unknown."""
    if not user32:
        return [None] * len(widgets)
    try:
        user32.GetTopWindow.argtypes = [wintypes.HWND]
        user32.GetTopWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetWindow.restype = wintypes.HWND
        want = [hwnd_of(w) for w in widgets]
        pos, h, i = {}, user32.GetTopWindow(None), 0
        while h and i < 5000:
            pos.setdefault(h, i)
            h = user32.GetWindow(h, 2)   # GW_HWNDNEXT
            i += 1
        return [pos.get(x) for x in want]
    except Exception:
        return [None] * len(widgets)


def _geom(name, w):
    try:
        w.update_idletasks()
        if w.winfo_ismapped():
            print(f"GEOM {name} {w.winfo_rootx()} {w.winfo_rooty()} {w.winfo_width()} {w.winfo_height()}", flush=True)
    except Exception:
        pass


def selftest(args):
    global GIVE_BACK_FOCUS
    GIVE_BACK_FOCUS = False
    import json
    import shutil
    import tempfile
    # never touch the real session_cmd.json / live_attempts.jsonl from a test
    st_tmp = Path(tempfile.mkdtemp(prefix="mochi_selftest_"))
    U.SESSION_CMD_FILE = st_tmp / "session_cmd.json"
    U.LIVE_ATTEMPTS_FILE = st_tmp / "live_attempts.jsonl"
    try:
        return _selftest(args, json, shutil, tempfile)
    finally:
        shutil.rmtree(st_tmp, ignore_errors=True)


def _widget_texts(w):
    out, stack = [], [w]
    while stack:
        x = stack.pop()
        try:
            out.append(str(x.cget("text")))
        except (tk.TclError, AttributeError):
            pass
        try:
            stack.extend(x.winfo_children())
        except tk.TclError:
            pass
    return "\n".join(out)


def _selftest(args, json, shutil, tempfile):
    if args.adapt or (args.debrief and args.debrief != "last"):
        data, src = U.sample_adapt_data(args.debrief or "pass"), "built-in adaptive sample"
    elif args.sparse:
        data, src = U.sparse_data(), "built-in sparse data"
    elif args.empty:
        data, src = {}, "empty data"
    elif args.sample:
        data, src = U.sample_data(), "built-in sample"
    elif args.data:
        data, src = U.read_json(args.data), args.data
        if data is None:
            raise SystemExit(f"could not read {args.data}")
    else:
        data = U.read_json(U.DATA_FILE)
        src = str(U.DATA_FILE)
        if data is None:
            data, src = U.sample_data(), "built-in sample (coach_data.json not found)"
    if args.sparse:
        ppd = U.sparse_pp_data()
    elif args.empty:
        ppd = {}
    elif args.sample or args.adapt or (args.debrief and args.debrief != "last"):
        ppd = U.sample_pp_data()
    else:
        ppd = U.read_json(U.PP_DATA_FILE) or U.sample_pp_data()
    fg_before = U.foreground_hwnd()
    app = PetApp(args, selftest=True, data_override=data)
    root = app.root
    if args.pos:
        try:
            px_, py_ = (int(v) for v in args.pos.split(","))
            root.geometry(f"+{px_}+{py_}")
        except ValueError:
            pass
    app.pp_data = ppd
    app.panel.pp_data = ppd
    app.card.set_pp(pp_data=ppd)
    # PP-first inputs (CONTRACT_PP2.md): samples, broken ones for --sparse, the real files otherwise
    if args.sparse:
        app.set_pp_inputs(recs={"picks": [{}, "x", {"title": 5, "pp_expected": "a", "owned": "yes"}], "model": 3,
                                "by_mods_tip": 7}, rows=[{"pred": None}], report={"recent": "x"})
    elif args.empty:
        pass
    elif args.sample or args.adapt or (args.debrief and args.debrief != "last"):
        app.set_pp_inputs(recs=U.sample_pp_recs(), rows=U.sample_pred_rows(), report=U.sample_model_report())
    else:
        app.set_pp_inputs(recs=U.read_json(U.PP_RECS_FILE) or U.sample_pp_recs(),
                          rows=U.read_pred_log(U.PP_PRED_LOG) or U.sample_pred_rows())
    app.start_live()
    make_noactivate(root)
    app.root.deiconify()
    app.visible = True
    make_noactivate(root)
    force_topmost(root)
    app.tick()
    app.poll_live()

    if args.hold:  # screenshot helper: show one tab or the quick card (optionally the live card) for N s, then exit
        root.update()
        if args.debrief:
            deb = data.get("last_debrief")
            app.card.show_debrief(deb, app.anchor(), U.level_change(deb, data.get("tracks")))
        elif args.ppcard:
            if args.live:
                app.handle_live(U.sample_live_states_pp()[1])
            app.pet_clicked()
        elif args.card:
            app.card.mode = "next"
            app.card.show(app.anchor())
            if args.step:
                for _ in range(args.step):
                    app.card.done()
            if args.expand:
                app.card.toggle_why()
        else:
            app.panel.show(app.anchor(), focus=False)
            root.update()
            app.panel.select_tab_name(args.tab or "Today")
            root.update()
            if args.expand:
                app.panel.expand_all()
            if args.scroll:
                root.update()
                sf = app.panel.tabs[app.panel.nb.tab(app.panel.nb.select(), "text")]
                sf.update_region()
                sf.cv.yview_moveto(args.scroll)
        if args.live and not args.ppcard:
            app.live_q.put(U.sample_live_states_pp()[1])
        root.update()

        def report():
            _geom("pet", root)
            _geom("card", app.card.top) if app.card.top is not None else None
            _geom("panel", app.panel.top) if app.panel.top is not None else None
            fg = U.foreground_hwnd()
            print(f"FOREGROUND same={fg == fg_before} ours={U.hwnd_pid(fg) == os.getpid()}", flush=True)
        root.after(1200, report)
        root.after(int(args.hold * 1000), root.quit)
        root.mainloop()
        try:
            root.destroy()
        except tk.TclError:
            pass
        return src + " (hold)"

    app.panel.show(app.anchor(), focus=False)
    root.update()
    for i in range(len(TAB_NAMES)):  # realise every tab so charts and wraps lay out
        app.panel.select_tab(i)
        root.update()
    app.panel.select_tab(0)
    app.panel.hide()
    root.update()

    problems = []
    steps_plan = U.plan_steps(data)

    def check_setup():
        app.panel.show(app.anchor(), focus=False)
        app.panel.select_tab_name("Settings")
        vars_ = getattr(app.panel, "setup_vars", None)
        if not vars_:
            problems.append("settings tab has no questionnaire")
            return
        vars_["device"].set("tablet")
        vars_["style"].set("alternate")
        app.panel.setup_save()
        ps = app.settings.get("player_setup")
        if not (isinstance(ps, dict) and ps.get("device") == "tablet" and ps.get("keys") in (None, "keyboard", "keypad", "mouse buttons")):
            problems.append(f"player_setup not saved: {ps!r}")

    last_cmd = {}

    def read_cmd():
        """Read session_cmd.json and take it away, like coach_adapt does (else the next one is queued)."""
        try:
            d_ = json.loads(U.SESSION_CMD_FILE.read_text("utf-8"))
            U.SESSION_CMD_FILE.unlink()
            last_cmd.clear()
            last_cmd.update(d_)
        except Exception:
            pass
        return dict(last_cmd)

    def restore_data():
        app.data = data
        app.panel.set_data(data)
        app.card.mode = "next"
        app.card.set_data(data)

    def check_card():
        if U.session_of(data):
            return check_card_session()
        return check_card_plan()

    def check_card_session():
        """The card follows coach_data.session: Done/back write session_cmd.json and ask for an engine run."""
        c = app.card
        app.show_training_card()
        root.update_idletasks()
        if not c.visible:
            problems.append("training card did not open")
            return
        st = dict(c.state)
        if st.get("mode") != "next" or not st.get("session"):
            problems.append(f"card not in session mode: {st!r}")
        i0, n = st.get("step"), st.get("n")
        if isinstance(i0, int) and n and i0 < n:
            app.engine_requests.clear()
            c.done()
            if read_cmd().get("cmd") != "done" or c.state.get("step") != i0 + 1:
                problems.append(f"session Done: cmd {read_cmd()!r}, card {c.state!r}")
            if "cmd" not in app.engine_requests:
                problems.append("session Done did not ask for an engine run")
            c.back()
            if read_cmd().get("cmd") != "back" or c.state.get("step") != i0:
                problems.append(f"session back: cmd {read_cmd()!r}, card {c.state!r}")
        if c.errors:
            problems.extend("card: " + e for e in c.errors)
        restore_data()
        app.pet_clicked()
        if c.visible:
            problems.append("second pet click did not close the card")
        app.pet_clicked()
        app.open_full_coach()
        root.update_idletasks()
        if c.visible or not app.panel.is_open():
            problems.append("Open full coach did not swap the card for the panel")
        app.pet_clicked()
        if app.panel.is_open():
            problems.append("pet click did not close the panel")

    def check_adaptive():
        """CONTRACT_ADAPT.md UI on the built-in adaptive sample: a debrief card for pass / close / fail /
        free play / break, the card following session.current, Done/Skip/back/restart -> session_cmd.json +
        engine run, level up / down celebrations (once per try), a debrief held back during a map, the Today and
        Progress tabs, and broken adaptive data."""
        c = app.card
        k = [0]
        app.debrief_var.set(True)   # the trainer's debrief pop-ups are opt-in now (PP-first)

        def feed(kind, **over):
            d = U.sample_adapt_data(kind)
            k[0] += 1
            d["last_debrief"]["ts"] = int(time.time()) - 40 + k[0]   # a new debrief each time
            d["last_debrief"].update(over)
            d["debriefs_today"][0] = d["last_debrief"]
            app.apply_data(d)
            root.update_idletasks()
            return d
        try:
            c.hide()
            app.panel.hide()
            cases = [("pass", "pass", "Again at 0.85x"), ("close", "close", "Again at 0.85x"),
                     ("fail", "fail", "Again at 0.90x"), ("free-play", "free-play", "Back to Speed at 0.85x"),
                     ("break", "close", "Take a 5 min break")]
            for kind, v, big in cases:
                n0 = len(app.debriefs_shown)
                d = feed(kind)
                st = dict(c.state)
                if not c.visible or st.get("mode") != "debrief":
                    problems.append(f"{kind}: no debrief card (visible={c.visible}, {st!r})")
                    continue
                if st.get("verdict") != v or st.get("next_big") != big or len(app.debriefs_shown) != n0 + 1:
                    problems.append(f"{kind}: debrief card shows {st!r}")
                texts = _widget_texts(c.body)
                if U.plain(d["last_debrief"]["headline"]) not in texts or "NEXT" not in texts:
                    problems.append(f"{kind}: headline / NEXT missing on the card")
                for o in d["last_debrief"]["observations"]:
                    if U.level_text(o) not in texts.replace("•  ", ""):
                        problems.append(f"{kind}: observation missing: {o}")
                if kind == "free-play" and (not st.get("free") or U.FREE_NOTE not in texts):
                    problems.append("free play: no 'Not today's step' note")
                if kind == "break" and (st.get("next_type") != "break" or "Copy song search" in texts):
                    problems.append(f"break: wrong card {st!r}")
                if kind == "pass" and "0.85x" not in txt(st.get("howto")):
                    problems.append(f"pass: no mod how-to for the speed: {st!r}")
            n0 = len(app.debriefs_shown)
            app.apply_data(dict(app.data, generated_at=1))   # a re-run that found nothing new
            if len(app.debriefs_shown) != n0:
                problems.append("the same debrief popped up twice")
            # the card follows session.current
            c.show_next()
            st = dict(c.state)
            if (st.get("mode"), st.get("map"), st.get("passes"), st.get("needed"), st.get("step"), st.get("rate")) != \
                    ("next", "Demo - Stream Practice [Insane]", 1, 1, 2, 0.85) or not st.get("last_try"):
                problems.append(f"Next up card doesn't follow session.current: {st!r}")
            app.engine_requests.clear()
            for cmd, fn, want_step in (("done", c.done, 3), ("skip", c.skip, 4), ("back", c.back, 3),
                                       ("restart", lambda: app.send_cmd("restart"), 0)):
                fn()
                got = read_cmd()
                if got.get("cmd") != cmd or c.state.get("step") != want_step or not num(got.get("ts")):
                    problems.append(f"{cmd}: session_cmd {got!r}, card step {c.state.get('step')} (want {want_step})")
            if app.engine_requests.count("cmd") != 4:
                problems.append(f"session commands did not each ask for an engine run: {app.engine_requests!r}")
            if c.state.get("map") != "Sample Artist - Gentle Morning [Normal]":
                problems.append(f"after restart the card shows {c.state.get('map')!r}")
            # two quick clicks: coach_adapt reads one session_cmd.json per run, so the second one waits its turn
            app.send_cmd("done")
            app.send_cmd("skip")
            if read_cmd().get("cmd") != "done" or app.cmd_queue != ["skip"] or c.state.get("step") != 2:
                problems.append(f"quick Done+Skip: {last_cmd!r}, queue {app.cmd_queue!r}, step {c.state.get('step')}")
            app.flush_cmd_queue()
            if read_cmd().get("cmd") != "skip" or app.cmd_queue:
                problems.append(f"queued Skip not written after Done was read: {last_cmd!r}")
            # level up: celebrated once, also when the replay's version of the same try arrives
            n0 = app.level_celebrations
            d = feed("level-up")
            if app.level_celebrations != n0 + 1 or c.state.get("level") != "Speed level up: 150 BPM → 155 BPM (play it at 0.90x)" \
                    or app.cheer_until <= time.monotonic():
                problems.append(f"level up not celebrated: {app.level_celebrations - n0}, {c.state!r}")
            deb2 = dict(d["last_debrief"], source="live", ts=d["last_debrief"]["ts"] + 8,
                        headline="Streams clean at 0.85x - checked the replay too")
            app.apply_data(dict(d, last_debrief=deb2))
            if app.level_celebrations != n0 + 1 or c.state.get("headline") != deb2["headline"]:
                problems.append("the replay version of the same try: celebrated twice or card not updated")
            feed("level-down")
            if app.level_celebrations != n0 + 2 or c.state.get("level") != "Speed level down: 155 BPM → 150 BPM (play it at 0.85x)":
                problems.append(f"level down not shown: {c.state!r}")
            # a debrief that arrives mid-map waits until the map is over; the end of a map asks for a run
            c.hide()
            app.live_state, app.live_screen, app.live_last = {"tosu_ok": True, "screen": "playing"}, "playing", \
                time.monotonic()
            feed("close")
            if c.visible or not app.pending_debrief:
                problems.append("a debrief popped up in the middle of a map")
            app.handle_live({"tosu_ok": True, "screen": "results"})
            if app.attempt_due is None:
                problems.append("the end of a map (tosu playing -> results) did not schedule a debrief run")
            app.flush_pending_debrief()
            if not c.visible or c.state.get("mode") != "debrief":
                problems.append("held debrief not shown after the map")
            c.show_next()
            app.attempt_ended()
            if not c.state.get("pending"):
                problems.append("no 'Looking at that try...' while the debrief is on its way")
            c.set_pending(False)
            # the full coach: Today + Progress show the adaptive parts; Today's Done sends a session command
            c.hide()
            app.panel.show(app.anchor(), focus=False)
            app.panel.set_data(app.data)
            root.update_idletasks()
            tt = _widget_texts(app.panel.tabs["Today"].inner)
            pt = _widget_texts(app.panel.tabs["Progress"].inner)
            for want in ("Since last time", "Your last try", "Today's plan", "145 BPM -> 150 BPM", "level up after 1 more clean pass"):
                if want not in tt:
                    problems.append(f"Today tab: {want!r} missing")
            for want in ("Your training levels", "Today's attempts", "150 BPM", "Stream practice speed", "▲ going up",
                         "Pass once more to reach 155 BPM", "105%"):
                if want not in pt:
                    problems.append(f"Progress tab: {want!r} missing")
            n0 = len(app.cmds_sent)
            app.panel._scmd("done")
            if app.cmds_sent[n0:] != ["done"] or read_cmd().get("cmd") != "done":
                problems.append("Today tab Done did not send a session command")
            app.panel.hide()
            # broken adaptive data: nothing may crash
            sp = U.sparse_data()
            app.apply_data(sp)
            c.show(app.anchor())
            for deb in (U.sample_debrief("sparse"), sp["last_debrief"], {"verdict": "pass"}):
                c.show_debrief(deb, app.anchor(), U.level_change(deb, sp.get("tracks")))
            c.show_next()
            c.done()
            c.back()
            c.copy_search()
            app.panel.show(app.anchor(), focus=False)
            app.panel.set_data(sp)
            for name in TAB_NAMES:
                app.panel.select_tab_name(name)
                root.update_idletasks()
            app.panel.hide()
            if c.errors:
                problems.extend("card: " + e for e in c.errors)
        finally:
            app.debrief_var.set(False)
            app.live_state = app.live_screen = None
            app.pending_debrief = app.attempt_due = None
            app.last_playing, app.results_done = -1e9, False
            app.live_trace.clear()
            app.card.set_pending(False)
            restore_data()
            app.card.hide()
            app.panel.hide()

    def check_pp():
        """CONTRACT_PP2.md: the PP card on left-click (total, rank, hovered map, best-mod tip, model accuracy,
        top-5 picks with Copy search / website), it follows the hovered map and its mods, More picks -> PP tab
        (sorting, model chart), the results bubble ('You got 61pp - I predicted 55'), no trainer pop-up by
        default, and the pp jobs (one at a time, never during a map)."""
        c = app.card
        saved = (app.recs, app.pred_rows, app.model_report)
        n_cel = app.pp_celebrations
        try:
            c.hide()
            app.panel.hide()
            app.set_pp_inputs(recs=U.sample_pp_recs(), rows=U.sample_pred_rows(), report=U.sample_model_report())
            c.set_pp(live=None)
            app.pet_clicked()
            root.update_idletasks()
            st = dict(c.state)
            if not c.visible or st.get("mode") != "pp":
                problems.append(f"pet click did not open the PP card: {st!r}")
                return
            hdr = U.pp_header(ppd)
            if hdr["total_txt"] and st.get("total") != hdr["total_txt"]:
                problems.append(f"PP card total {st.get('total')!r} != {hdr['total_txt']!r}")
            if len(st.get("picks") or []) != 5 or "±6pp" not in txt(st.get("model")):
                problems.append(f"PP card picks / model line: {st!r}")
            texts = _widget_texts(c.body)
            for want in ("Your PP", "Rank-up picks", "Open full coach (training) ›", "SELECTED MAP", "Copy search",
                         "Get map"):
                if want not in texts:
                    problems.append(f"PP card: {want!r} missing")
            states = U.sample_live_states_pp()
            app.handle_live(states[0])
            st = dict(c.state)
            if st.get("big") != "~34pp" or txt(st.get("tip")) != "Try HD: +6pp expected" or \
                    st.get("pass") != "82% pass chance" or st.get("range") != "likely 26-43pp":
                problems.append(f"PP card does not follow the hovered map: {st!r}")
            app.handle_live(states[1])     # mods changed to HD
            st = dict(c.state)
            if st.get("big") != "~55pp" or not txt(st.get("gain")).startswith("+7.9pp to your total"):
                problems.append(f"PP card did not react to the mod change: {st!r}")
            if "~55pp" not in _widget_texts(c.body):
                problems.append("PP card shows an old prediction")
            v = c.pp_view()
            own = [pv for pv in v["picks"] if pv["action"] and pv["action"][0] == "copy"]
            web = [pv for pv in v["picks"] if pv["action"] and pv["action"][0] == "web"]
            if not own or not web:
                problems.append("sample picks need owned and not-owned maps")
            else:
                c.do_pick(own[0])
                if c.state.get("copied") != own[0]["search"]:
                    problems.append(f"Copy search on a pick: {c.state.get('copied')!r}")
                c.do_pick(web[0])
                if not app.urls_opened or app.urls_opened[-1] != web[0]["action"][1] or \
                        not app.urls_opened[-1].startswith("https://osu.ppy.sh/b/"):
                    problems.append(f"Get map did not open the osu! page: {app.urls_opened!r}")
            # More picks -> full coach on the PP tab
            c._hook("open_panel", "PP")
            root.update_idletasks()
            if c.visible or not app.panel.is_open() or app.panel.nb.index(app.panel.nb.select()) != 0:
                problems.append("More picks did not open the full coach on the PP tab")
            app.panel.refresh_tab("PP")
            pt = _widget_texts(app.panel.tabs["PP"].inner)
            for want in ("Your PP", "Rank-up picks", "How good are my guesses?", "Biggest gain", "Safest first",
                         "Easiest first", "Smash picks", "Other mods: HR ~70pp (50% pass)", "Open on osu! website", "Copy search", "My last 20 guesses",
                         "Mods: HD adds"):
                if want not in pt:
                    problems.append(f"PP tab: {want!r} missing")
            o_gain = list(app.panel.pp_rows_shown)
            app.panel.set_pp_sort("pass")
            o_pass = list(app.panel.pp_rows_shown)
            app.panel.set_pp_sort("stars")
            o_stars = list(app.panel.pp_rows_shown)
            app.panel.set_pp_sort("smash")
            o_smash = list(app.panel.pp_rows_shown)
            if o_smash != ["Artist 0 - Song 0 [Insane]", "Artist 4 - Song 4 [Insane]", "Artist 30 - Safe Song [Insane]"]                     or "SMASH IT" not in _widget_texts(app.panel.tabs["PP"].inner):
                problems.append(f"PP tab smash picks: {o_smash}")
            app.panel.set_pp_sort("gain")
            if o_gain == o_pass or o_gain == o_stars or o_pass[0] != "Artist 0 - Song 0 [Insane]" \
                    or o_stars[0] != "Artist 0 - Song 0 [Insane]" or o_gain[0] != "Artist 0 - Song 0 [Insane]" \
                    or o_stars[-1] == o_gain[-1]:
                problems.append(f"PP tab sorting: {o_gain[:3]} / {o_pass[:3]} / {o_stars[:3]}")
            app.panel.hide()
            # play -> results: the PP results bubble, the model learns
            app.pp_jobs.update_at = None
            n0 = app.pp_celebrations
            app.handle_live(states[2])
            app.handle_live(states[3])
            rv = app.results_shown[-1] if app.results_shown else {}
            if app.pp_celebrations != n0 + 1 or rv.get("title") != "You got 61pp!" or \
                    "I predicted 55pp - inside my range (44-66)" not in txt(rv.get("body")) or \
                    "+7.4pp to your total" not in txt(rv.get("body")):
                problems.append(f"results bubble: {rv!r}")
            bt = [app.bubble.cv.itemcget(i, "text") for i in app.bubble.cv.find_all()
                  if app.bubble.cv.type(i) == "text"]
            if not app.bubble.visible or "You got 61pp!" not in bt:
                problems.append(f"results bubble not on screen: {bt!r}")
            if c.visible:
                problems.append("the card covered the results bubble")
            if app.pp_jobs.update_at is None:
                problems.append("a finished map did not schedule pp_model.update")
            # the trainer's debrief does not pop up by default any more
            n0 = len(app.debriefs_shown)
            d = U.sample_adapt_data("pass")
            d["last_debrief"]["ts"] = int(time.time()) - 5
            app.apply_data(d)
            if len(app.debriefs_shown) != n0 or (c.visible and c.mode == "debrief"):
                problems.append("a trainer debrief popped up with 'Show training debriefs' off")
            app.pet_clicked()
            if c.mode != "pp":
                problems.append(f"pet click after a play opened {c.mode!r}, not the PP card")
            c.hide()
            restore_data()
            # pp jobs: a fake spawner - one at a time, never during a map, after a session, daily
            spawned = []

            class _P:
                rc = None

                def poll(self):
                    return self.rc

                def kill(self):
                    self.rc = -9

            def sp(kind, code):
                if "pp_model" not in code and "pp_recommend" not in code:
                    return None
                spawned.append((kind, _P()))
                return spawned[-1][1]
            t0 = 1_000_000.0
            fresh = {"m": t0}
            j = U.PPJobs(sp, model_ok=lambda: True, rec_ok=lambda: True, recs_mtime=lambda: fresh["m"], now=t0)
            r = [j.tick(t0 + 10), j.tick(t0 + 61)]
            j.attempt(t0 + 62)
            r += [j.tick(t0 + 100)]
            spawned[0][1].rc = 0
            r += [j.tick(t0 + 101), j.tick(t0 + 102, busy=True), j.tick(t0 + 103)]
            spawned[-1][1].rc = 0
            r += [j.tick(t0 + 104), j.tick(t0 + 105 + 15 * 60)]     # session over -> recommend
            spawned[-1][1].rc = 0
            r += [j.tick(t0 + 2000), j.tick(t0 + 3000)]
            fresh["m"] = t0 - 21 * 3600                               # picks a day old -> daily refresh
            r += [j.tick(t0 + 2 * 3600 + 1200)]
            want = [None, "update", None, None, None, "update", None, "recommend", None, None, "recommend"]
            if r != want:
                problems.append(f"pp jobs: {r} (wanted {want})")
            if c.errors:
                problems.extend("card: " + e for e in c.errors)
        finally:
            app.recs, app.pred_rows, app.model_report = saved
            app.set_pp_inputs(recs=saved[0], rows=saved[1])
            app.pp_celebrations = n_cel   # the live-state run at the end counts its own
            app.live_state = app.live_screen = None
            app.last_playing, app.results_done, app.card_auto_hidden = -1e9, False, False
            app.live_trace.clear()
            app.bubble.hide()
            c.set_pp(live=None)
            c.hide()
            app.panel.hide()

    def check_periodic():
        """The periodic engine run: no more every-2-minutes all night while osu! sits idle."""
        saved = (app.osu_up, app.osu_focused, app.last_engine_end, app.start, app.last_focus_wall)
        try:
            now = time.time()
            app.osu_up, app.osu_focused, app.start = True, False, now - 9 * 3600
            app.last_focus_wall, app.last_engine_end = now - 150, now - 200    # played since the last run
            if not app.periodic_due(now):
                problems.append("periodic: no re-run 3 min after playing")
            app.last_focus_wall, app.last_engine_end = now - 1800, now - 200   # idle since the last run
            if app.periodic_due(now):
                problems.append("periodic: re-ran after 3 min with osu! idle")
            app.last_engine_end = now - 700
            if not app.periodic_due(now):
                problems.append("periodic: no 10-minute re-run after recent play")
            app.osu_focused = True
            if app.periodic_due(now):
                problems.append("periodic: ran while osu! is in front")
            app.osu_focused = False
            t0 = now
            app.last_focus_wall = app.last_engine_end = t0
            runs = 0
            for m in range(1, 8 * 60):      # a night with osu! open and idle, checked every minute
                t = t0 + m * 60
                if app.periodic_due(t):
                    runs += 1
                    app.last_engine_end = t
            if runs > 6:
                problems.append(f"periodic: {runs} runs in an idle night")
        finally:
            app.osu_up, app.osu_focused, app.last_engine_end, app.start, app.last_focus_wall = saved

    def check_tosu_paths():
        """tosu outside OneDrive: resolution order, launcher path, 'Move tosu out of OneDrive' (temp folders)."""
        d = Path(tempfile.mkdtemp(prefix="mochi_tosu_"))
        saved_env = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "OneDrive")}
        saved_u = (U.SETTINGS_FILE, U.TOSU_LOCAL)
        try:
            od = d / "OneDrive" / "coach" / "tosu"
            (od / "logs").mkdir(parents=True)
            (od / "static").mkdir()
            (od / "tosu.exe").write_bytes(b"MZ")
            (od / "tosu.env").write_text("A=1", "utf-8")
            (od / "logs" / "x.txt").write_text("log", "utf-8")
            (od / "run.log").write_text("log", "utf-8")
            (od / "static" / "a.js").write_text("1", "utf-8")
            la = d / "Local"
            la.mkdir()
            os.environ["LOCALAPPDATA"], os.environ["OneDrive"] = str(la), str(d / "OneDrive")
            sf = d / "pet_settings.json"
            sf.write_text(json.dumps({"tosu_path": str(od / "tosu.exe")}), "utf-8")
            U.SETTINGS_FILE, U.TOSU_LOCAL = sf, od / "tosu.exe"
            st = U.tosu_status()
            if not (st["onedrive"] and st["movable"] == od and U.launch_tosu_path() == ""):
                problems.append(f"tosu only in OneDrive: {st!r}, launcher {U.launch_tosu_path()!r}")
            if not U.in_onedrive(od) or U.in_onedrive(la):
                problems.append("in_onedrive is wrong")
            if U.move_tosu_out(od, d / "OneDrive" / "elsewhere")[0]:
                problems.append("move_tosu_out copied into OneDrive")
            # the Settings tab shows the warning and the button; the button copies (to the temp LOCALAPPDATA)
            app.panel.show(app.anchor(), focus=False)
            app.panel.tosu_move = None
            app.panel._build_tab("Settings")
            root.update_idletasks()
            if "can't run from inside OneDrive" not in _widget_texts(app.panel.tabs["Settings"].inner) \
                    or not app.panel.tosu_move:
                problems.append("Settings: no 'tosu can't run from OneDrive' warning / move button")
            else:
                app.panel.tosu_move()
            new = la / "tosu" / "tosu.exe"
            if not new.is_file() or (la / "tosu" / "logs").exists() or (la / "tosu" / "run.log").exists() \
                    or not (la / "tosu" / "static" / "a.js").is_file() or not (od / "tosu.exe").is_file():
                problems.append("Move tosu out of OneDrive: wrong copy")
            if app.settings.get("tosu_path") != str(new):
                problems.append(f"tosu_path not updated: {app.settings.get('tosu_path')!r}")
            st = U.tosu_status()   # the settings file still names the OneDrive copy: skipped
            if st["exe"] != new or st["source"] != "localappdata" or st["ignored"] != str(od / "tosu.exe") \
                    or U.launch_tosu_path() != str(new):
                problems.append(f"tosu after the move: {st!r}")
            own = d / "Tools" / "tosu.exe"
            own.parent.mkdir()
            own.write_bytes(b"MZ")
            sf.write_text(json.dumps({"tosu_path": str(own)}), "utf-8")
            if U.tosu_status()["exe"] != own:
                problems.append("a tosu_path outside OneDrive was not used first")
            app.panel._build_tab("Settings")
            root.update_idletasks()
            if "can't run from inside OneDrive" in _widget_texts(app.panel.tabs["Settings"].inner):
                problems.append("Settings still warns about OneDrive after the move")
        finally:
            for k_, v_ in saved_env.items():
                if v_ is None:
                    os.environ.pop(k_, None)
                else:
                    os.environ[k_] = v_
            U.SETTINGS_FILE, U.TOSU_LOCAL = saved_u
            app.settings.d["tosu_path"] = None
            app.panel.hide()
            app.panel.dirty = True
            shutil.rmtree(d, ignore_errors=True)

    def check_card_plan():
        c = app.card
        app.show_training_card()                # right-click > Training: next step
        root.update_idletasks()
        if not c.visible:
            problems.append("training card did not open")
            return
        st = dict(c.state)
        if not steps_plan:
            if not st.get("empty"):
                problems.append(f"empty plan: card should say 'no plan yet', state={st!r}")
        else:
            s0 = st.get("step")
            if s0 != 0 or st.get("n") != len(steps_plan):
                problems.append(f"card starts at step {s0} of {st.get('n')} (wanted 0 of {len(steps_plan)})")
            first = steps_plan[0]
            if first["picks"]:
                if not c.copy_search() or c.state.get("copied") != U.search_text(first["picks"][0]):
                    problems.append("Copy song search did not copy the first pick")
                if len(first["picks"]) > 1:
                    c.skip()
                    if c.state.get("pick") != 1:
                        problems.append(f"Skip map did not move to the next pick: {c.state!r}")
                c.toggle_why()
                c.toggle_why()
            c.done()
            if c.state.get("step") != 1:
                problems.append(f"Done did not advance: {c.state!r}")
            if not app.settings.check_get(U.today(), first["key"]):
                problems.append("Done did not tick the block for today")
            c.back()
            if c.state.get("step") != 0:
                problems.append(f"back did not go back: {c.state!r}")
            for _ in range(len(steps_plan)):
                c.done()
                root.update_idletasks()
            if not c.state.get("all_done"):
                problems.append(f"card not 'all done' after every step: {c.state!r}")
            c.restart()
            if c.state.get("step") != 0:
                problems.append("Start over did not reset the plan")
        if c.errors:
            problems.extend("card: " + e for e in c.errors)
        app.pet_clicked()                       # click again -> closes
        if c.visible:
            problems.append("second pet click did not close the card")
        app.pet_clicked()
        app.open_full_coach()                   # 'Open full coach' link
        root.update_idletasks()
        if c.visible or not app.panel.is_open():
            problems.append("Open full coach did not swap the card for the panel")
        app.pet_clicked()                       # clicking the pet closes the panel
        if app.panel.is_open():
            problems.append("pet click did not close the panel")

    def check_tabs():
        app.panel.show(app.anchor(), focus=False)
        root.update_idletasks()
        for name in TAB_NAMES:
            app.panel.select_tab_name(name)
            root.update_idletasks()
        n = app.panel.expand_all()
        root.update_idletasks()
        if data and n < 5:
            problems.append(f"only {n} expanders opened")
        for name in TAB_NAMES:
            app.panel.select_tab_name(name)
            root.update_idletasks()
        app.panel.hide()

    def check_fade():
        app.osu_focused, app.last_interaction = True, -1e9
        app.update_fade()
        if not app.faded or abs(float(root.attributes("-alpha")) - 0.7) > 0.02:
            problems.append("fade to 70% did not apply")
        app._enter(None)
        if app.faded:
            problems.append("hover did not restore full opacity")
        app.hover, app.osu_focused = False, False
        app.update_fade()

    def check_real_paths():
        """The code paths the selftest flag normally skips: the per-second focus check with a stubbed 'game in
        front', a quiet-but-alive tosu watcher, putting the panel / card away, and the window order."""
        mod = sys.modules[PetApp.__module__]
        saved_gif = mod.game_in_front
        saved = (app.osu_up, app.live_state, app.live_screen, app.live_last, app.watcher, app.hide_play_var.get(),
                 app.always_var.get(), app.breaks_var.get())
        try:
            mod.game_in_front = lambda: True
            app.selftest = False
            app.osu_up = True
            app.breaks_var.set(False)
            app.always_var.set(True)

            class _W:   # a LiveWatcher whose thread is alive but that sent nothing for 30 s (mid-map)
                _thread = threading.current_thread()
            app.watcher = _W()
            app.hide_play_var.set(True)
            app.live_state, app.live_screen = {"tosu_ok": True, "screen": "playing"}, "playing"
            app.live_last = time.monotonic() - 30
            app.check_play_hidden()
            if not app.play_hidden or app.visible:
                problems.append(f"hide-while-playing gave up on a quiet but alive tosu (hidden={app.play_hidden})")
            app.hide_play_var.set(False)
            app.check_play_hidden()
            if app.play_hidden or not app.visible:
                problems.append("pet did not come back with 'hide while playing' off")
            # with tosu: the card goes away for the map and comes back on the results screen
            app.card.show(app.anchor())
            app.handle_live({"tosu_ok": True, "screen": "playing"})
            if app.card.visible:
                problems.append("card stayed over the map while tosu said 'playing'")
            app.handle_live({"tosu_ok": True, "screen": "results"})
            if not app.card.visible:
                problems.append("card did not come back after the map")
            app.card.hide()
            app.card_auto_hidden = False
            # without tosu: the coach window closes after ~10 s in the game, the card after ~45 s
            app.watcher, app.live_state, app.live_screen = None, None, None
            app.panel.show(app.anchor(), focus=False)
            root.update_idletasks()
            # window order: Mochi above the coach window, also after a keep-on-top tick
            app.keep_topmost_once()
            zp, zc = z_order(root, app.panel.top)
            if zp is not None and zc is not None and zp > zc:
                problems.append(f"the coach window is above Mochi (z {zc} < {zp})")
            pr = (root.winfo_rootx(), root.winfo_rooty(), app.Wpx, app.Hpx)
            q = app.panel.top
            qr = (q.winfo_rootx(), q.winfo_rooty(), q.winfo_width(), q.winfo_height())
            if pr[0] < qr[0] + qr[2] and qr[0] < pr[0] + pr[2] and pr[1] < qr[1] + qr[3] and qr[1] < pr[1] + pr[3]:
                problems.append(f"the coach window covers Mochi: pet {pr}, panel {qr}")
            wl, wt, wr, wb = work_area(root)
            if q.winfo_y() < wt or q.winfo_rooty() + q.winfo_height() > wb + 2:
                problems.append(f"coach window leaves the work area: {qr} vs {(wl, wt, wr, wb)}")
            app.focus_since, app.last_interaction = None, -1e9
            app.check_play_hidden()
            app.focus_since = time.monotonic() - 11
            app.check_play_hidden()
            if app.panel.is_open():
                problems.append("coach window stayed open over the game")
            if steps_plan and not app.card.visible:
                problems.append("card did not replace the auto-closed coach window")
            app.focus_since, app.last_interaction = time.monotonic() - 50, -1e9
            app.check_play_hidden()
            if app.card.visible:
                problems.append("card stayed over the game for 50 s without use (no tosu)")
        finally:
            mod.game_in_front = saved_gif
            app.selftest = True
            (app.osu_up, app.live_state, app.live_screen, app.live_last, app.watcher) = saved[:5]
            app.hide_play_var.set(saved[5])
            app.always_var.set(saved[6])
            app.breaks_var.set(saved[7])
            app.osu_focused, app.focus_since = False, None
            app.live_trace.clear()
            app.last_playing, app.results_done, app.card_auto_hidden = -1e9, False, False
            app.panel.hide()
            app.card.hide()
            app.update_visibility()
            app.update_fade()

    def check_settings_file():
        """Migration to v2 + persistence + stable step keys, on a temporary settings file."""
        import json
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="mochi_st_"))
        f = d / "pet_settings.json"
        try:
            f.write_text(json.dumps({"hide_when_focused": True, "hide_while_playing": True}), "utf-8")
            st = U.Settings(path=f)
            disk = json.loads(f.read_text("utf-8"))
            if st.get("hide_while_playing") or st.get("hide_when_focused") or disk.get("settings_version") != 2:
                problems.append(f"settings migration failed: {disk!r}")
            st.set("hide_while_playing", True)
            if U.Settings(path=f).get("hide_while_playing") is not True:
                problems.append("a later 'hide while playing' choice did not survive a restart")
            f.write_text("[1, 2]", "utf-8")
            U.Settings(path=f)                       # must not crash on junk
        finally:
            for x in d.glob("*"):
                try:
                    x.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass
        plan = [{"block": "Warm-up"}, {"block": "Accuracy"}, {"block": "Speed (stream test)"}, {"block": "Aim"}]
        k1 = {s["block"]: s["key"] for s in U.plan_steps({"session_plan": plan})}
        plan2 = [plan[0], {"block": "Skill rotation: stamina"}, plan[1], {"block": "Break (5 min)"},
                 {"block": "Speed"}, plan[3]]
        k2 = {s["block"]: s["key"] for s in U.plan_steps({"session_plan": plan2})}
        if not (k1["Aim"] == k2["Aim"] and k1["Accuracy"] == k2["Accuracy"] and k1["Speed (stream test)"] == k2["Speed"]):
            problems.append(f"step keys move when the plan changes: {k1} vs {k2}")

    live_states = U.sample_live_states()

    shown = []

    def feed(st):
        app.live_q.put(st)
        app.drain_live()  # process now, so every fake state is checked on its own
        if app.live_bubble.visible:
            shown.append(dict(app.live_bubble.lines or {}))

    steps = []
    for i, m in enumerate(MOODS * 2):
        steps.append((300 + i * 420, lambda m=m: setattr(app, "mood_override", m)))
    steps += [
        (400, lambda: app.say("Selftest", txt(g(data, "headline"), "Hello from the selftest!"), 3)),
        (900, lambda: setattr(app, "thinking", True)),
        (1300, lambda: setattr(app, "hover", True)),
        (1800, lambda: setattr(app, "hover", False)),
        (2000, app.celebrate_new_play),
        (2300, check_card),
        (2600, lambda: app.panel.select_tab_name("Progress")),
        (2900, app.toggle_panel),
        (3200, app.toggle_panel),
        (3400, lambda: app.panel.set_data(data)),
        (3500, check_setup),
        (3550, check_tabs),
        (3580, check_fade),
        (3600, lambda: setattr(app, "thinking", False)),
        (3650, lambda: app.panel.set_pp_data(dict(ppd, generated_at=1))),  # pp_data.json changed -> rebuild
        (3620, check_settings_file),
        (3640, check_real_paths),
        (3660, check_adaptive),
        (3670, check_periodic),
        (3675, check_pp),
        (3680, check_tosu_paths),
        (3700, lambda: app.hide_play_var.set(True)),   # the opt-in 'hide while playing' (tosu) feature
        (3750, lambda: setattr(app, "osu_focused", True)),   # it only hides while the game is in front
    ]
    for i, st in enumerate(live_states):
        steps.append((3800 + i * 300, lambda st=st: feed(st)))
    for ms, fn in steps:
        root.after(ms, lambda fn=fn: app._safe(fn))
    root.after(3800 + len(live_states) * 300 + 500, root.quit)
    root.mainloop()

    problems = problems + list(app.errors) + [f"tab {n}:\n{tb}" for n, tb in app.panel.errors]
    # live bubble / hide-while-playing / results celebration, from the fake LiveWatcher states
    expect = [("menu", False, False, True), ("song_select", True, False, True), ("song_select", True, False, True),
              ("song_select", True, False, True), ("playing", False, True, False), ("results", False, False, True),
              ("song_select", True, False, True), ("results", False, False, True), ("other", False, False, True)]
    if app.live_trace != expect:
        problems.append("live trace mismatch:\n  got    %r\n  wanted %r" % (app.live_trace, expect))
    mains = [s.get("main", "") for s in shown]
    if len(mains) < 4 or not mains[0].startswith("~") or not shown[0].get("big") or mains[0] == mains[1] \
            or "no PP estimate" not in mains[2]:
        problems.append(f"live bubble texts look wrong: {mains!r}")
    if app.pp_celebrations != 1:
        problems.append(f"results celebration ran {app.pp_celebrations} times (wanted 1)")
    for name, fr in app.panel.tabs.items():
        if not fr.inner.winfo_children():
            problems.append(f"tab {name} is empty")
    if not app.cv.find_all():
        problems.append("pet canvas is empty")
    fg = U.foreground_hwnd()
    if fg_before and U.hwnd_pid(fg) == os.getpid():
        problems.append("the selftest took the keyboard focus")
    try:
        root.destroy()
    except tk.TclError:
        pass
    if problems:
        raise RuntimeError("selftest problems:\n" + "\n".join(problems))
    return src


def main():
    ap = argparse.ArgumentParser(description="osu! coach desktop pet")
    ap.add_argument("--exit-with-osu", action="store_true", help="quit when osu! closes")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data", help="coach_data.json to use for --selftest")
    ap.add_argument("--sample", action="store_true", help="selftest with the built-in sample")
    ap.add_argument("--sparse", action="store_true", help="selftest with deliberately sparse data")
    ap.add_argument("--empty", action="store_true", help="selftest with no data at all")
    ap.add_argument("--hold", type=float, default=0, help="selftest: just show the panel for N s (screenshots)")
    ap.add_argument("--tab", help="selftest --hold: tab to show")
    ap.add_argument("--live", action="store_true", help="selftest --hold: also show the live PP card")
    ap.add_argument("--card", action="store_true", help="selftest --hold: show the training Next up card instead")
    ap.add_argument("--ppcard", action="store_true", help="selftest --hold: show the PP card (with --live: hovered map)")
    ap.add_argument("--step", type=int, default=0, help="selftest --hold --card: press Done N times first")
    ap.add_argument("--expand", action="store_true", help="selftest --hold: open every expander / Why this?")
    ap.add_argument("--pos", help="selftest: put the pet at X,Y (physical px)")
    ap.add_argument("--scroll", type=float, default=0, help="selftest --hold: scroll the tab to 0..1")
    ap.add_argument("--adapt", action="store_true", help="selftest with the built-in adaptive sample (session etc.)")
    ap.add_argument("--debrief", help="selftest --hold: show the debrief card for pass|close|fail|free-play|break|"
                                      "level-up|level-down|sparse (adaptive sample)")
    args, _ = ap.parse_known_args()
    U.setup_logging(selftest=args.selftest)
    dpi_aware()
    if args.selftest:
        try:
            src = selftest(args)
        except BaseException:
            tb = traceback.format_exc()
            U.log.error("SELFTEST FAILED\n%s", tb)
            try:
                print(tb)
                print("SELFTEST FAILED")
            except Exception:
                pass
            sys.exit(1)
        try:
            print(f"SELFTEST OK ({src})")
        except Exception:
            pass
        sys.exit(0)
    if not single_instance():
        U.log.info("another pet is already running - exiting")
        return
    try:
        U.log.info("pet starting (exit_with_osu=%s)", args.exit_with_osu)
        PetApp(args).run()
    except Exception:
        U.log.error("fatal:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
