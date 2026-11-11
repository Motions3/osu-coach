#!/usr/bin/env python3
"""Stdlib mock of the osu! API v2 endpoints used by osu_api.py, plus end-to-end tests.

    python test_mock_api.py            # run the tests
    python test_mock_api.py --serve    # just run the mock on http://127.0.0.1:8765 (Ctrl+C)

Nothing here talks to the real osu! servers or touches the osu! folders; every file is
written to a temporary directory (OSU_COACH_API_DIR).
"""

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAYER = 12345
CID, SECRET = "4242", "mock-secret-DO-NOT-LEAK-7f3a9c"

# ----------------------------------------------------------------------------
# fake world
# ----------------------------------------------------------------------------

K_SS = 1.15        # mock pp of an SS ~ 1.15 * stars^3 (4.6* ~ 112 pp, 7* ~ 395 pp)
ARTISTS = ["TrySail", "ClariS", "fripSide", "LiSA", "Aimer", "ZAQ", "Kana Hanazawa", "nano", "GARNiDELiA",
           "Camellia", "xi", "DragonForce", "Halozy", "UNDEAD CORPORATION", "Yunomi", "MYTH & ROID"]
VERSIONS = ["Insane", "Extra", "Hard", "Expert", "Light Insane", "Another", "Extreme"]


def mock_pp(stars, acc, misses):
    return K_SS * stars ** 3 * acc ** 5 * 0.965 ** misses


class World:
    def __init__(self, seed=42):
        rng = random.Random(seed)
        self.maps = {}
        for i in range(450):
            bid = 1000001 + i
            stars = round(rng.uniform(2.5, 7.0), 2)
            style = rng.choice(["aim", "aim", "balanced", "speed"])
            aim = stars * 0.52
            spd = aim * {"aim": 0.78, "balanced": 0.9, "speed": 1.03}[style]
            self.maps[bid] = {
                "id": bid, "beatmapset_id": 500000 + i // 3, "mode": "osu", "mode_int": 0,
                "difficulty_rating": stars, "version": rng.choice(VERSIONS),
                "checksum": hashlib.md5(str(bid).encode()).hexdigest(),
                "bpm": rng.choice([150, 160, 170, 175, 180, 185, 190, 200, 210, 220, 230]),
                "ar": round(min(10, 7 + stars * 0.4), 1), "accuracy": round(min(10, 5 + stars * 0.6), 1),
                "cs": rng.choice([3.5, 4, 4, 4.2, 4.5]), "drain": 5, "total_length": rng.randint(85, 240),
                "hit_length": rng.randint(80, 230), "count_circles": rng.randint(200, 700),
                "count_sliders": rng.randint(50, 300), "count_spinners": 1, "status": "ranked",
                "_aim": aim, "_speed": spd, "_style": style, "_plays": rng.randint(1000, 2_000_000),
                "_artist": rng.choice(ARTISTS), "_title": "Song %d" % i,
            }
        self.users = {}
        for _ in range(4000):
            uid = rng.randint(2_000_000, 20_000_000)
            active = rng.random() < 0.6
            pp = round(math.exp(rng.gauss(math.log(2600), 0.6)), 2) if active else 0.0
            self.users[uid] = {"id": uid, "username": "player%d" % uid, "pp": pp}
        ranked = sorted((u for u in self.users.values() if u["pp"] > 0), key=lambda u: -u["pp"])
        for u in self.users.values():
            u["rank"] = None
        for i, u in enumerate(ranked):     # pretend each mock user stands for 300 real ones
            u["rank"] = (i + 1) * 300
        self.users[PLAYER] = {"id": PLAYER, "username": "player1", "pp": 2000.0, "rank": 500000}
        self.active = [u for u in self.users.values() if u["pp"] > 0 and u["id"] != PLAYER]
        self.rng = rng

    # -- scores --------------------------------------------------------------
    def best(self, uid):
        if uid == PLAYER:
            return self.player_best()
        u = self.users[uid]
        rng = random.Random(uid)
        comfort = 2.0 + 1.25 * math.log(max(u["pp"], 301) / 300)
        out, used = [], set()
        pool = list(self.maps.values())
        while len(out) < 100:
            m = rng.choice(pool)
            if m["id"] in used:
                continue
            mods = []
            stars = m["difficulty_rating"]
            r = rng.random()
            if r < 0.15:
                mods, stars = ["HD", "DT"], stars * 1.4
            elif r < 0.25:
                mods, stars = ["HD", "HR"], stars * 1.08
            elif r < 0.6:
                mods = ["HD"]
            if not (comfort - 0.9 <= stars <= comfort + 0.35):
                continue
            used.add(m["id"])
            acc = min(1.0, rng.uniform(0.93, 0.995))
            miss = rng.choice([0, 0, 0, 1, 2, 4])
            out.append(self.score(uid, m, mods, acc, miss, mock_pp(stars, acc, miss), "2025-0%d-1%dT12:00:00Z"
                                  % (rng.randint(1, 9), rng.randint(0, 9)), new_format=uid % 2 == 0))
        out.sort(key=lambda s: -s["pp"])
        return out

    def player_best(self):
        rng = random.Random(PLAYER)
        out = []
        pool = sorted(self.maps.values(), key=lambda m: m["id"])
        cands = [m for m in pool if 3.0 <= m["difficulty_rating"] <= 4.9]
        rng.shuffle(cands)
        for i, m in enumerate(cands[:100]):
            recent = i % 9 == 0
            acc = rng.uniform(0.89, 0.93) if recent else rng.uniform(0.90, 0.986)
            miss = rng.choice([0, 0, 1, 2]) if not recent else rng.choice([1, 2, 3])
            date = "2026-10-%02dT20:00:00Z" % rng.randint(10, 30) if recent else "201%d-0%d-06T10:00:00Z" % (
                rng.choice([6, 7]), rng.randint(1, 9))
            mods = ["HD"] if recent else []
            out.append(self.score(PLAYER, m, mods, acc, miss, mock_pp(m["difficulty_rating"], acc, miss), date))
        out.sort(key=lambda s: -s["pp"])
        out[0]["pp"] = 103.0          # match the real profile's top play
        return out

    def score(self, uid, m, mods, acc, miss, pp, date, new_format=False):
        bm = {k: v for k, v in m.items() if not k.startswith("_")}
        bs = {"id": m["beatmapset_id"], "artist": m["_artist"], "title": m["_title"], "creator": "mapper"}
        if new_format:
            return {"id": random.randint(1, 10**9), "user_id": uid, "beatmap_id": m["id"], "ruleset_id": 0,
                    "accuracy": round(acc, 4), "mods": [{"acronym": a} for a in mods] + [{"acronym": "CL"}],
                    "pp": round(pp, 3), "ended_at": date, "statistics": {"miss": miss, "great": 500},
                    "beatmap": bm, "beatmapset": bs}
        return {"id": random.randint(1, 10**9), "user_id": uid, "accuracy": round(acc, 4), "mods": mods,
                "pp": round(pp, 3), "created_at": date, "mode_int": 0,
                "statistics": {"count_miss": miss, "count_300": 500}, "beatmap": bm, "beatmapset": bs}

    def feed(self):
        rng = random.Random(7)
        out = []
        for _ in range(1000):
            u = rng.choice(self.active)
            m = rng.choice(list(self.maps.values()))
            out.append({"id": rng.randint(1, 10**9), "user_id": u["id"], "beatmap_id": m["id"],
                        "ruleset_id": rng.choice([0, 0, 0, 0, 1, 3]), "pp": None, "accuracy": 0.95,
                        "mods": [], "ended_at": "2026-11-08T10:00:00Z"})
        return {"scores": out, "cursor_string": "abc"}

    def leaderboard(self, bid):
        rng = random.Random(bid)
        users = rng.sample(self.active, 200)
        users.sort(key=lambda u: -u["pp"])
        return {"scores": [{"id": i, "user_id": u["id"], "user": {"id": u["id"], "username": u["username"]},
                            "pp": 200.0, "accuracy": 0.99, "mods": []} for i, u in enumerate(users[:50])]}

    def user_compact(self, u):
        return {"id": u["id"], "username": u["username"], "country_code": "GB",
                "statistics_rulesets": {"osu": {"pp": u["pp"], "global_rank": u["rank"]}}}

    def user_full(self, u):
        return {"id": u["id"], "username": u["username"], "country_code": "GB",
                "rank_highest": {"rank": 200000, "updated_at": "2020-01-01T00:00:00Z"} if u["id"] == PLAYER else None,
                "statistics": {"pp": u["pp"], "global_rank": u["rank"], "country_rank": 20000,
                               "hit_accuracy": 95.0, "play_count": 10000}}

    def attributes(self, bid, mods):
        m = self.maps[bid]
        ac = [x["acronym"] if isinstance(x, dict) else x for x in (mods or [])]
        f = 1.0
        if "DT" in ac or "NC" in ac:
            f *= 1.4
        if "HT" in ac:
            f *= 0.75
        if "HR" in ac:
            f *= 1.08
        return {"attributes": {"star_rating": round(m["difficulty_rating"] * f, 3),
                               "aim_difficulty": m["_aim"] * f, "speed_difficulty": m["_speed"] * f,
                               "max_combo": 800, "approach_rate": min(11, m["ar"] * (1.2 if f > 1.3 else 1)),
                               "overall_difficulty": m["accuracy"], "slider_factor": 0.98,
                               "speed_note_count": 300.0}}

    def search(self, q):
        lo, hi = 0, 99
        for part in q.split():
            if part.startswith("stars>="):
                lo = float(part[7:])
            elif part.startswith("stars<="):
                hi = float(part[7:])
        sets = {}
        for m in sorted(self.maps.values(), key=lambda m: -m["_plays"]):
            if lo <= m["difficulty_rating"] <= hi:
                s = sets.setdefault(m["beatmapset_id"], {"id": m["beatmapset_id"], "artist": m["_artist"],
                                                         "title": m["_title"], "creator": "mapper", "beatmaps": []})
                s["beatmaps"].append({k: v for k, v in m.items() if not k.startswith("_")})
            if len(sets) >= 50:
                break
        return {"beatmapsets": list(sets.values()), "cursor_string": None}


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class State:
    def __init__(self):
        self.world = World()
        self.tokens = set()
        self.log = []            # (time, method, path)
        self.token_posts = []
        self.disable_feed = False
        self.expire_once = False
        self.throttle_once = False
        self.bad_secret_seen = False


class Handler(BaseHTTPRequestHandler):
    S = None

    def log_message(self, *a):
        pass

    def send(self, code, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        S = self.S
        h = self.headers.get("Authorization", "")
        if self.headers.get("Accept") != "application/json":
            self.send(400, {"error": "Accept header missing"})
            return False
        if not h.startswith("Bearer ") or h[7:] not in S.tokens:
            self.send(401, {"authentication": "basic"})
            return False
        if S.expire_once:
            S.expire_once = False
            S.tokens.discard(h[7:])
            self.send(401, {"authentication": "basic"})
            return False
        if S.throttle_once:
            S.throttle_once = False
            self.send(429, {"error": "slow down"}, {"Retry-After": "0"})
            return False
        return True

    def do_POST(self):
        S = self.S
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode()
        u = urllib.parse.urlparse(self.path)
        S.log.append((time.monotonic(), "POST", u.path))
        if u.path == "/oauth/token":
            f = dict(urllib.parse.parse_qsl(body))
            S.token_posts.append(f.get("grant_type") + "|" + f.get("scope", ""))
            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded") is False:
                return self.send(400, {"error": "form expected"})
            if f.get("client_id") != CID or f.get("client_secret") != SECRET:
                S.bad_secret_seen = True
                return self.send(401, {"error": "invalid_client"})
            if f.get("grant_type") != "client_credentials" or f.get("scope") != "public":
                return self.send(400, {"error": "unsupported_grant_type"})
            t = hashlib.sha1(os.urandom(8)).hexdigest()
            S.tokens.add(t)
            return self.send(200, {"token_type": "Bearer", "expires_in": 86400, "access_token": t})
        if not self.authed():
            return
        parts = u.path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["api", "v2", "beatmaps"] and parts[4] == "attributes":
            js = json.loads(body or "{}")
            bid = int(parts[3])
            if bid not in S.world.maps:
                return self.send(404, {"error": "not found"})
            return self.send(200, S.world.attributes(bid, js.get("mods")))
        self.send(404, {"error": "not found"})

    def do_GET(self):
        S = self.S
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        S.log.append((time.monotonic(), "GET", u.path))
        if not self.authed():
            return
        W = S.world
        p = u.path.strip("/").split("/")
        if p[:2] != ["api", "v2"]:
            return self.send(404, {})
        p = p[2:]
        if p == ["users"]:
            ids = [int(x) for x in q.get("ids[]", [])][:50]
            return self.send(200, {"users": [W.user_compact(W.users[i]) for i in ids if i in W.users]})
        if len(p) == 3 and p[0] == "users" and p[2] == "osu":
            key = q.get("key", ["id"])[0]
            if key == "username":
                us = [x for x in W.users.values() if x["username"] == p[1]]
                return self.send(200, W.user_full(us[0])) if us else self.send(404, {})
            uid = int(p[1])
            return self.send(200, W.user_full(W.users[uid])) if uid in W.users else self.send(404, {})
        if len(p) == 4 and p[0] == "users" and p[2] == "scores" and p[3] == "best":
            uid = int(p[1])
            if uid not in W.users:
                return self.send(404, {})
            lim = min(100, int(q.get("limit", ["100"])[0]))
            return self.send(200, W.best(uid)[:lim])
        if p == ["scores"]:
            if S.disable_feed:
                return self.send(404, {"error": "not found"})
            return self.send(200, W.feed())
        if len(p) == 3 and p[0] == "beatmaps" and p[2] == "scores":
            return self.send(200, W.leaderboard(int(p[1])))
        if p == ["beatmapsets", "search"]:
            return self.send(200, W.search(q.get("q", [""])[0]))
        self.send(404, {"error": "not found"})


def start_mock(port=0):
    st = State()
    Handler.S = st
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, st


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------

SRV, ST = start_mock()
TMP = Path(tempfile.mkdtemp(prefix="osu_api_test_"))
import atexit, shutil  # noqa: E401,E402
atexit.register(shutil.rmtree, TMP, True)
os.environ.update({"OSU_COACH_API_URL": "http://127.0.0.1:%d" % SRV.server_address[1],
                   "OSU_COACH_API_DIR": str(TMP), "OSU_COACH_API_SLEEP": "0", "OSU_COACH_NO_TOSU": "1",
                   # pp_live / coach caches (.coach_skill.json ...) go to the temp dir, never the project folder
                   "OSU_COACH_PP_DIR": str(TMP), "OSU_COACH_STATE_DIR": str(TMP)})
sys.path.insert(0, str(HERE))
import osu_api  # noqa: E402


def write_config(secret=SECRET):
    (TMP / "osu_api_config.json").write_text(json.dumps({"client_id": int(CID), "client_secret": secret,
                                                         "user": PLAYER}), encoding="utf-8")


def write_library():
    W = ST.world
    owned = [m for m in W.maps.values() if m["id"] % 3 == 0]
    lib = {"v": 3, "maps": {m["checksum"]: {"bid": m["id"], "a": m["_artist"], "t": m["_title"]} for m in owned}}
    (TMP / ".coach_library.json").write_text(json.dumps(lib), encoding="utf-8")


def reset_files():
    for f in TMP.iterdir():
        f.unlink()
    osu_api._attr_cache.clear()


def all_written_text():
    return "".join(f.read_text(encoding="utf-8", errors="replace") for f in TMP.iterdir() if f.is_file()
                   and f.name != "osu_api_config.json")


class TestWeightedGain(unittest.TestCase):
    def brute(self, top, new, rep=None):
        lst = sorted(top, reverse=True)
        if rep is not None:
            if new <= rep:
                return 0.0
            lst.remove(rep)
        lst2 = sorted(lst + [new], reverse=True)[:100]
        f = lambda L: sum(p * 0.95 ** i for i, p in enumerate(sorted(L, reverse=True)[:100]))  # noqa
        return f(lst2) - f(top)

    def test_exact(self):
        rng = random.Random(1)
        top = sorted((rng.uniform(30, 103) for _ in range(100)), reverse=True)
        for new in (10, 35, 49, 60, 103, 150):
            self.assertAlmostEqual(osu_api.weighted_gain(top, new), max(0, self.brute(top, new)), places=6)
        self.assertEqual(osu_api.weighted_gain(top, 10), 0.0)           # below #100: nothing
        # inserting at #1 of an all-equal list: gain = new - last weight * p
        flat = [50.0] * 100
        # new #1 shifts everyone down one place and the #100 drops out: gain = 60 - 50
        self.assertAlmostEqual(osu_api.weighted_gain(flat, 60), 10.0, places=6)

    def test_replace_and_short_list(self):
        top = [103, 90, 80, 70, 60]
        self.assertEqual(osu_api.weighted_gain(top, 75, replaces=80), 0.0)   # not better than existing
        g = osu_api.weighted_gain(top, 85, replaces=80)
        self.assertAlmostEqual(g, (85 - 80) * 0.95 ** 2, places=6)
        self.assertAlmostEqual(osu_api.weighted_gain(top, 20), 20 * 0.95 ** 5, places=6)
        self.assertAlmostEqual(osu_api.weighted_gain([], 50), 50.0, places=6)
        self.assertEqual(osu_api.weighted_gain(top, None), 0.0)

    def test_mods(self):
        self.assertEqual(osu_api.mods_of({"mods": ["HD", "NC", "SD"]}), (["HD", "DT"], 1.5))
        self.assertEqual(osu_api.mods_of({"mods": [{"acronym": "DT", "settings": {"speed_change": 1.3}},
                                                   {"acronym": "CL"}]}), (["DT"], 1.3))
        self.assertEqual(osu_api.mods_str([]), "NM")
        self.assertEqual(osu_api.core_key(["HD"], 1.0), "NM")


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        reset_files()
        ST.log.clear()
        ST.disable_feed = ST.expire_once = ST.throttle_once = False

    def test_no_config(self):
        d = osu_api.refresh()
        self.assertFalse(d["configured"])
        self.assertIn("osu_api_config.example.json", d["error"])
        self.assertTrue((TMP / "pp_data.json").exists())
        self.assertEqual(ST.log, [])                       # no network without config

    def test_placeholder_config(self):
        (TMP / "osu_api_config.json").write_text((HERE / "osu_api_config.example.json").read_text(), "utf-8")
        d = osu_api.refresh()
        self.assertFalse(d["configured"])
        self.assertIn("incomplete", d["error"])

    def test_bad_secret(self):
        write_config("wrong")
        d = osu_api.refresh(force=True)
        self.assertTrue(d["configured"])
        self.assertIn("client_secret", d["error"])
        self.assertNotIn("wrong", json.dumps(d))

    def test_full_refresh(self):
        write_config()
        write_library()
        t0 = time.monotonic()
        d = osu_api.refresh(force=True)
        self.assertIsNone(d["error"], d["error"])
        u = d["user"]
        self.assertEqual((u["id"], u["username"], u["pp"], u["global_rank"]), (PLAYER, "player1", 2000.0, 500000))
        self.assertEqual((u["peak_rank"], u["peak_rank_date"]), (200000, "2020-01-01"))
        self.assertEqual(len(d["history"]), 1)
        self.assertEqual(len(d["top_plays"]), 100)
        self.assertEqual(d["pp_threshold"]["top1"], 103.0)
        self.assertGreaterEqual(d["pp_threshold"]["top50"], d["pp_threshold"]["top100"])
        # contract keys
        for k in ("version", "generated_at", "configured", "error", "user", "history", "top_plays",
                  "pp_threshold", "climb"):
            self.assertIn(k, d)
        climb = d["climb"]
        self.assertGreater(len(climb), 5)
        keys = ["beatmap_id", "set_id", "md5", "artist", "title", "version", "stars", "bpm", "ar", "od", "cs",
                "length_s", "mods", "typical_pp", "typical_acc", "source_players", "owned", "pp_predicted",
                "est_gain_pp", "reason"]
        for c in climb:
            for k in keys:
                self.assertIn(k, c)
            self.assertFalse(any(k.startswith("_") for k in c))
            self.assertGreater(c["est_gain_pp"], 0)
        band = d["climb_meta"]["star_band"]
        self.assertTrue(all(band[0] - 1e-6 <= c["stars"] <= band[1] + 1e-6 for c in climb))
        # owned first
        owned = [c["owned"] for c in climb]
        self.assertEqual(owned, sorted(owned, reverse=True))
        self.assertTrue(any(owned))
        self.assertTrue(all(c["owned"] == (c["beatmap_id"] % 3 == 0) for c in climb))
        # gains are exact w.r.t. the top plays
        pps = [t["pp"] for t in d["top_plays"]]
        mine = {t["beatmap_id"]: t["pp"] for t in d["top_plays"]}
        for c in climb:
            g = osu_api.weighted_gain(pps, c["pp_predicted"], mine.get(c["beatmap_id"]))
            self.assertLess(abs(g - c["est_gain_pp"]), 0.2)
        # source players really are 1.1-1.6x
        lo, hi = d["climb_meta"]["source_pp_range"]
        self.assertGreaterEqual(lo, 2000 * 1.1 - 0.1)
        self.assertLessEqual(hi, 2000 * 1.6 + 0.1)
        # budget and rate
        self.assertLessEqual(d["requests"], 150)
        self.assertEqual(d["requests"], len(ST.log))
        self.assertIn("client_credentials|public", ST.token_posts)
        # secret never written (config file excluded)
        self.assertNotIn(SECRET, all_written_text())
        # speed maps limited
        self.assertLessEqual(sum(1 for c in climb if c.get("style") == "speed"), 4)
        print("\n  full refresh: %d requests in %.1fs, %d climb maps, %d owned; top: %s"
              % (d["requests"], time.monotonic() - t0, len(climb), sum(owned), climb[0]["reason"][:160]))

    def test_cache_and_history(self):
        write_config()
        d1 = osu_api.refresh(force=True)
        n = len(ST.log)
        d2 = osu_api.refresh()                                # within 30 min -> cached, no requests
        self.assertEqual(len(ST.log), n)
        self.assertEqual(d1["generated_at"], d2["generated_at"])
        # profile refresh (forced) same day keeps a single snapshot; climb reused when not forced
        data = json.loads((TMP / "pp_data.json").read_text("utf-8"))
        data["generated_at"] -= 3600
        (TMP / "pp_data.json").write_text(json.dumps(data), "utf-8")
        ST.log.clear()
        d3 = osu_api.refresh()
        self.assertEqual(len(d3["history"]), 1)
        self.assertLessEqual(len(ST.log), 15)                  # profile + top plays only, climb reused
        self.assertEqual([c["beatmap_id"] for c in d3["climb"]], [c["beatmap_id"] for c in d1["climb"]])
        # old snapshots kept forever, new day appended
        data = json.loads((TMP / "pp_data.json").read_text("utf-8"))
        data["history"] = [{"date": "2020-01-01", "pp": 2900, "global_rank": 200000, "accuracy": 95.0}] + \
            data["history"]
        (TMP / "pp_data.json").write_text(json.dumps(data), "utf-8")
        d4 = osu_api.refresh(force=True)
        self.assertEqual([h["date"] for h in d4["history"]][0], "2020-01-01")
        self.assertEqual(len(d4["history"]), 2)
        # token reused from file across a fresh Api
        self.assertEqual(sum(1 for t in ST.log if t[2] == "/oauth/token"), 0)

    def test_expired_token_and_429(self):
        write_config()
        osu_api.refresh(force=True)
        ST.log.clear()
        ST.expire_once = True
        ST.throttle_once = False
        d = osu_api.refresh(force=True)
        self.assertIsNone(d["error"])
        self.assertEqual(sum(1 for t in ST.log if t[2] == "/oauth/token"), 1)
        ST.throttle_once = True
        d = osu_api.refresh(force=True)
        self.assertIsNone(d["error"])

    def test_feed_unavailable_fallback(self):
        write_config()
        ST.disable_feed = True
        d = osu_api.refresh(force=True)
        self.assertIsNone(d["error"], d["error"])
        self.assertTrue(any("unavailable" in s for s in d["climb_meta"]["sources"]))
        self.assertGreater(len(d["climb"]), 0)
        self.assertLessEqual(d["requests"], 150)

    def test_budget_partial(self):
        write_config()
        old = osu_api.MAX_REQUESTS
        try:
            api_init = osu_api.Api.__init__.__defaults__
            osu_api.Api.__init__.__defaults__ = (20,) + api_init[1:]
            d = osu_api.refresh(force=True)
        finally:
            osu_api.Api.__init__.__defaults__ = api_init
        self.assertLessEqual(d["requests"], 20)
        self.assertEqual(d["user"]["id"], PLAYER)
        self.assertEqual(len(d["top_plays"]), 100)
        self.assertIsInstance(d["climb"], list)
        self.assertEqual(old, osu_api.MAX_REQUESTS)

    def test_with_pp_live_tosu_down(self):
        """pp_live importable (if present) but tosu not running: must still work, fast."""
        write_config()
        write_library()
        old = os.environ.pop("OSU_COACH_NO_TOSU")
        os.environ["OSU_COACH_TOSU_URL"] = "http://127.0.0.1:9"
        osu_api._tosu.clear()
        try:
            t0 = time.monotonic()
            d = osu_api.refresh(force=True)
            self.assertIsNone(d["error"], d["error"])
            self.assertGreater(len(d["climb"]), 0)
            self.assertLess(time.monotonic() - t0, 60)
            self.assertTrue(all(c["pp_basis"].startswith("coarse") for c in d["climb"]))
        finally:
            os.environ["OSU_COACH_NO_TOSU"] = old
            os.environ.pop("OSU_COACH_TOSU_URL", None)

    def test_cli(self):
        write_config()
        write_library()
        r = subprocess.run([sys.executable, str(HERE / "osu_api.py"), "--force"], capture_output=True,
                           text=True, env=dict(os.environ), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("player1", r.stdout)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, str(HERE / "osu_api.py"), "--climb-only"], capture_output=True,
                           text=True, env=dict(os.environ), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(SECRET, r.stdout + r.stderr)


class TestRateLimit(unittest.TestCase):
    def test_interval(self):
        reset_files()
        write_config()
        api = osu_api.Api(json.loads((TMP / "osu_api_config.json").read_text()), interval=0.05)
        ST.log.clear()
        for _ in range(5):
            api.get("/users/%d/osu" % PLAYER)
        ts = [t[0] for t in ST.log]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        # the mock's arrival timestamps are quantised to the Windows clock tick (~15.6 ms: gaps show up as
        # 0.016 / 0.078 pairs), so single gaps can't be checked against 0.05 s; check the pacing as a whole
        self.assertGreaterEqual(ts[-1] - ts[0], 4 * 0.05 * 0.9, gaps)
        self.assertNotIn(SECRET, repr(api))
        self.assertEqual(osu_api.MIN_INTERVAL, 0.0)            # env override in tests; default 1.1 s


if __name__ == "__main__":
    if "--serve" in sys.argv:
        print("mock osu! API on http://127.0.0.1:8765 (client_id %s)" % CID)
        s, _ = start_mock(8765)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        unittest.main(verbosity=2)
