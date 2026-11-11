#!/usr/bin/env python3
"""coach_identity.py - who "the player" is, in one place for every script (Python 3.11 stdlib only).

Nothing about a specific person is hard-coded anywhere in the coach.  The player is found, in this order:
  1. osu_api_config.json "user_id" (a number), or "user" (a number = user id, or a username),
  2. the account logged in to osu! (lazer game.ini "Username", or osu!stable's osu!.<you>.cfg "Username"),
  3. whoever played the newest replay on this PC (not counting excluded accounts).
Names that share the resolved osu! user id are the same player (renames keep the id), so their replays count.
osu_api_config.json "exclude_users" (names or ids, default empty) lists OTHER accounts that play on this PC:
their replays never count as the player's and they are never picked by step 3.

Only files are read (read-only); no network, and the client secret in the config is never looked at.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def config_path() -> Path:
    """osu_api_config.json (OSU_COACH_API_DIR moves it, like osu_api.py)."""
    return Path(os.environ.get("OSU_COACH_API_DIR") or HERE) / "osu_api_config.json"


def lazer_default_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "osu"


def lazer_dir() -> Path:
    """osu!lazer's data folder: %APPDATA%/osu, or the folder it was moved to (Settings > Maintenance >
    change folder location), which lazer records as FullPath in %APPDATA%/osu/storage.ini (read only)."""
    base = lazer_default_dir()
    try:
        for line in (base / "storage.ini").read_text("utf-8", "replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().lower() == "fullpath":
                d = Path(v.strip().strip('"'))
                if v.strip() and d.is_dir():
                    return d
    except OSError:
        pass
    return base


def stable_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "osu!"


def _placeholder(s: str) -> bool:
    u = s.strip().upper()
    return not u or u.startswith(("YOUR", "PASTE", "<")) or u in ("0", "NONE", "NULL")


def _as_id(v):
    """Positive int from an int / digit string, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    s = str(v or "").strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


def load_identity(path=None) -> dict:
    """{"user_id": int|None, "username": str|None, "exclude_names": set[str lower], "exclude_ids": set[int]}
    from osu_api_config.json.  Missing / unreadable / placeholder values -> None / empty."""
    out = {"user_id": None, "username": None, "exclude_names": set(), "exclude_ids": set()}
    try:
        with open(path or config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return out
    if not isinstance(cfg, dict):
        return out
    out["user_id"] = _as_id(cfg.get("user_id"))
    u = cfg.get("user")
    if out["user_id"] is None:
        out["user_id"] = _as_id(u)
    if _as_id(u) is None and isinstance(u, str) and not _placeholder(u):
        out["username"] = u.strip()
    ex = cfg.get("exclude_users")
    for v in (ex if isinstance(ex, list) else [ex] if isinstance(ex, (str, int)) and ex != "" else []):
        i = _as_id(v)
        if i is not None:
            out["exclude_ids"].add(i)
        elif isinstance(v, str) and v.strip():
            out["exclude_names"].add(v.strip().lower())
    return out


def _read_username(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                k, sep, v = line.partition("=")
                if sep and k.strip().lower() == "username":
                    return v.strip() or None
    except OSError:
        pass
    return None


def game_username(root=None):
    """The account logged in to osu! (read-only): <root>/game.ini (lazer), else osu!stable's osu!.*.cfg.
    root=None tries lazer (%APPDATA%\\osu) then stable (%LOCALAPPDATA%\\osu!).  -> name or None."""
    roots = [Path(root)] if root else list(dict.fromkeys([lazer_dir(), lazer_default_dir(), stable_dir()]))
    for r in roots:
        n = _read_username(r / "game.ini")
        if n:
            return n
        try:
            for cfg in sorted(r.glob("osu!.*.cfg")):
                n = _read_username(cfg)
                if n:
                    return n
        except OSError:
            pass
    return None


def resolve(headers, ini_name=None, ident=None) -> dict:
    """Pick the player among replay headers (dicts with "player", "uid", "ts").
    -> {"uid": int|None, "names": set[str], "source": "config"|"game"|"newest replay"|None,
        "exclude_names": set[str lower], "exclude_ids": set[int]}"""
    ident = ident if ident is not None else load_identity()
    hs = [h for h in headers if isinstance(h, dict) and h.get("player")]
    ex_names = set(ident["exclude_names"])
    ex_ids = set(ident["exclude_ids"])
    ex_ids |= {h["uid"] for h in hs if h.get("uid") and h["player"].lower() in ex_names}
    ex_names |= {h["player"].lower() for h in hs if h.get("uid") in ex_ids}

    def allowed(h):
        return h["player"].lower() not in ex_names and h.get("uid") not in ex_ids

    def uid_of(name):
        c = Counter(h["uid"] for h in hs if h.get("uid") and h["player"].lower() == name.lower())
        return c.most_common(1)[0][0] if c else None

    uid, seed, source = None, set(), None
    if ident["user_id"] is not None:
        uid, source = ident["user_id"], "config"
    elif ident["username"]:
        seed, source = {ident["username"]}, "config"
        uid = uid_of(ident["username"])
    if source is None and ini_name and ini_name.lower() not in ex_names:
        if any(h["player"].lower() == ini_name.lower() for h in hs) or not hs:
            seed, source = {ini_name}, "game"
            uid = uid_of(ini_name)
    if source is None:
        ok = [h for h in hs if allowed(h)]
        if ok:
            newest = max(ok, key=lambda h: h.get("ts") or 0)
            seed, source, uid = {newest["player"]}, "newest replay", newest.get("uid")
    names = {h["player"] for h in hs if uid is not None and h.get("uid") == uid}
    # the seed name (config / logged-in account) also counts for its replays without an id (osu!stable)
    for s in seed:
        names |= {h["player"] for h in hs if h["player"].lower() == s.lower() and h.get("uid") in (None, uid)}
    if not names and seed:
        names = set(seed)
    # the logged-in account counts too when it shares the configured id (same person, renamed)
    if ini_name and uid is not None and uid_of(ini_name) == uid:
        names |= {h["player"] for h in hs if h["player"].lower() == ini_name.lower()}
    names = {n for n in names if n.lower() not in ex_names}
    return {"uid": uid if uid not in ex_ids else None, "names": names, "source": source,
            "exclude_names": ex_names, "exclude_ids": ex_ids}


def api_user(ident=None):
    """What osu_api.py should look up: the configured id / username, else the logged-in osu! name, else None."""
    ident = ident if ident is not None else load_identity()
    return ident["user_id"] or ident["username"] or game_username()
