#!/usr/bin/env python3
"""setup_check.py - is everything the osu! coach needs in place?  Run it once after downloading the coach:

    python setup_check.py            # local checks only (no network)
    python setup_check.py --online   # also asks the osu! website for a token once to test your API keys

Checks: Python version, tkinter (for the desktop pet), osu! (lazer / stable), tosu (live features),
osu_api_config.json (your osu! API client - the secret is never printed), and which osu! account the coach will
treat as you.  It only reads files; it never changes your osu! folder or the coach's data.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OK, WARN, BAD = "[ ok ]", "[warn]", "[FAIL]"
rows = []        # (mark, title, detail, next step)


def add(mark, title, detail="", step=""):
    rows.append((mark, title, detail, step))


def check_python():
    v = sys.version_info
    if v >= (3, 11):
        add(OK, "Python %d.%d.%d" % v[:3])
    elif v >= (3, 9):
        add(WARN, "Python %d.%d.%d" % v[:3], "the coach is tested on Python 3.11+",
            "Install Python 3.11 or newer from python.org (tick \"Add python.exe to PATH\").")
    else:
        add(BAD, "Python %d.%d.%d is too old" % v[:3], "", "Install Python 3.11 or newer from python.org.")


def check_tk():
    try:
        import tkinter  # noqa: F401  (import only - no window is opened)
        add(OK, "tkinter (needed for the desktop pet)")
    except Exception:
        add(BAD, "tkinter is missing", "the desktop pet needs it",
            "Re-run the Python installer > Modify > tick \"tcl/tk and IDLE\".")


def check_osu():
    import coach_identity as ci
    lazer, stable = ci.lazer_dir(), ci.stable_dir()
    lazer_files = (lazer / "files").is_dir() or (lazer / "client.realm").exists()
    if lazer_files:
        add(OK, "osu!lazer found", str(lazer))
    install = Path(os.environ.get("LOCALAPPDATA", "")) / "osulazer"
    if not lazer_files and install.is_dir():
        add(WARN, "osu!lazer is installed but has no data yet", str(install), "Start osu!lazer once and play a map.")
    if (stable / "osu!.exe").exists() or (stable / "Songs").is_dir():
        mark = OK if lazer_files else WARN
        add(mark, "osu!stable found", str(stable) + ("" if lazer_files else
            " - stable only keeps replays you save to its Replays folder, so the coach learns much less"),
            "" if lazer_files else "osu!lazer is recommended (it keeps a replay of every play).")
    if not lazer_files and not (stable / "Songs").is_dir() and not install.is_dir():
        add(BAD, "osu! not found", f"looked in {lazer} and {stable}",
            "Install osu!lazer from osu.ppy.sh and play a few maps (or pass --osu-dir to coach_engine.py).")


def check_tosu():
    try:
        import pet_util as U
        st = U.tosu_status()
    except Exception as e:
        add(WARN, "tosu: couldn't check", f"{type(e).__name__}: {e}")
        return
    if st["exe"] is None:
        add(WARN, "tosu not found (live features off)",
            "the coach still works from your replays; tosu adds live pp, fails and quits",
            "Download tosu from https://github.com/tosuapp/tosu/releases and extract it so "
            + str(U.tosu_appdata_exe() or r"%LOCALAPPDATA%\tosu\tosu.exe") + " exists (not inside OneDrive).")
    elif st["onedrive"]:
        add(WARN, "tosu is inside OneDrive", str(st["exe"]) + " - tosu refuses to start there",
            "Open the pet > Open full coach > Settings > \"Move tosu out of OneDrive\".")
    else:
        add(OK, "tosu found", str(st["exe"]))


def load_config():
    p = Path(os.environ.get("OSU_COACH_API_DIR") or HERE) / "osu_api_config.json"
    if not p.exists():
        add(WARN, "osu! API not set up (no osu_api_config.json)", "PP total, rank and rank-up picks need it",
            "Copy osu_api_config.example.json to osu_api_config.json and fill in client_id and client_secret "
            "(README: \"Set up the osu! API\"). The pet's Settings tab can do this for you.")
        return None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        add(BAD, "osu_api_config.json is not valid JSON", "", "Fix it or copy the example again.")
        return None
    if not isinstance(cfg, dict):
        add(BAD, "osu_api_config.json has the wrong shape", "", "Copy osu_api_config.example.json again.")
        return None
    cid = str(cfg.get("client_id") or "").strip()
    sec = cfg.get("client_secret")
    sec_ok = isinstance(sec, str) and len(sec) >= 8 and " " not in sec and "PASTE" not in sec.upper()
    problems = []
    if not cid.isdigit() or int(cid) <= 0 or cid == "12345":
        problems.append("client_id must be your application's number")
    if not sec_ok:
        problems.append("client_secret is not filled in")
    if problems:
        add(WARN, "osu_api_config.json is incomplete", "; ".join(problems),
            "osu! website > Settings > OAuth > New OAuth Application (README: \"Set up the osu! API\").")
        return None
    add(OK, "osu_api_config.json looks complete", "client_id set, client_secret set (not shown)")
    return cfg


def check_identity():
    import coach_identity as ci
    ident = ci.load_identity()
    game = ci.game_username()
    who = ident["user_id"] or ident["username"]
    if who:
        add(OK, "Player from osu_api_config.json", f"user {who}" + (f"; logged in to osu! as {game}" if game else ""))
    elif game:
        add(OK, "Player = the account logged in to osu!", game,
            "Optional: put your osu! username or id in osu_api_config.json \"user\" to pin it.")
    else:
        add(WARN, "Don't know your osu! account yet", "no \"user\" in the config and no logged-in account found",
            "Log in to osu! once, or set \"user\" in osu_api_config.json to your osu! username or id.")
    n_ex = len(ident["exclude_names"]) + len(ident["exclude_ids"])
    if n_ex:
        add(OK, f"{n_ex} other account(s) excluded", "from \"exclude_users\"")


def check_online(cfg):
    if cfg is None:
        add(WARN, "--online: skipped (API config incomplete)")
        return
    if os.environ.get("OSU_COACH_NO_NETWORK"):
        add(WARN, "--online: skipped (OSU_COACH_NO_NETWORK is set)")
        return
    try:
        import osu_api
        osu_api.Api(cfg).token(force=True)
        add(OK, "--online: osu! API login works")
    except Exception as e:
        code = getattr(e, "code", None) or getattr(e, "status", None)
        add(BAD, "--online: osu! API login failed", f"HTTP {code}" if isinstance(code, int) else type(e).__name__,
            "Check client_id / client_secret (osu! website > Settings > OAuth) and your internet connection.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for fn in (check_python, check_tk, check_osu, check_tosu):
        try:
            fn()
        except Exception as e:
            add(WARN, f"{fn.__name__}: couldn't check", f"{type(e).__name__}: {e}")
    cfg = None
    try:
        cfg = load_config()
        check_identity()
    except Exception as e:
        add(WARN, "config check failed", type(e).__name__)
    if "--online" in argv:
        check_online(cfg)
    print("osu! coach setup check\n")
    for mark, title, detail, _ in rows:
        print(f"{mark} {title}" + (f"  ({detail})" if detail else ""))
    steps = [s for m, _, _, s in rows if s and m != OK]
    print()
    if steps:
        print("Next steps:")
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {s}")
    else:
        print("All set! Start the coach with \"Play osu with Coach.bat\".")
    if "--online" not in argv:
        print("\n(Run 'python setup_check.py --online' to also test your osu! API keys.)")
    return 1 if any(m == BAD for m, *_ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
