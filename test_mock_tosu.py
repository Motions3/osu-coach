"""test_mock_tosu.py - stdlib mock of tosu + tests for pp_live.py.

    python test_mock_tosu.py            run all tests (hermetic: temp folder, mock server on a free port)
    python test_mock_tosu.py --serve    just run the mock on 127.0.0.1:24051 (scenario via /mock/set?scenario=...)

The mock serves:
  /json/v2               websocket-v2-shaped snapshot for the current scenario (see SCENARIOS)
  /api/calculate/pp      plausible PP: monotone in acc (up), misses (down), combo (up), mods/rate (up)
  /mock/set?scenario=X[&fail_calc=1]   switch scenario / make calc return {"error": ...}
  /mock/stats            {"calc_calls": n, "v2_calls": n}
Mock .osu files are tiny text files: "osu file format v14" + "//mock stars=4.6 objects=400 combo=600".
Nothing here touches the osu! folders; pp_live is redirected to a temp folder via OSU_COACH_PP_DIR.
"""

import json
import math
import os
import random
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

MAP_A = {"checksum": "a" * 32, "id": 1001, "set": 501, "artist": "TrySail", "title": "whiz", "version": "Insane",
         "mapper": "m", "stars": 4.6, "objects": 400, "combo": 600}
MAP_B = {"checksum": "b" * 32, "id": 1002, "set": 502, "artist": "Demo", "title": "Reach", "version": "Extra",
         "mapper": "m", "stars": 5.4, "objects": 500, "combo": 800}


def _v2(state_num, state_name, bm, mods=None, results=None, path=None):
    mods = mods or {"number": 0, "name": ""}
    snap = {
        "state": {"number": state_num, "name": state_name},
        "profile": {"name": "tester", "pp": 2000.0, "globalRank": 500000},
        "beatmap": {"checksum": bm["checksum"], "id": bm["id"], "set": bm["set"], "artist": bm["artist"],
                    "title": bm["title"], "version": bm["version"], "mapper": bm["mapper"],
                    "stats": {"stars": {"live": 0, "total": bm["stars"]}, "ar": {"original": 9, "converted": 9},
                              "cs": {"original": 4, "converted": 4}, "od": {"original": 8, "converted": 8},
                              "hp": {"original": 5, "converted": 5}, "bpm": {"common": 180},
                              "objects": {"circles": 300, "sliders": 100, "spinners": 0, "total": bm["objects"]},
                              "maxCombo": bm["combo"]}},
        "play": {"mods": mods, "pp": {"current": 0, "fc": 0}},
        "performance": {"accuracy": {"95": 100, "100": 150}},
        "folders": {"game": "", "songs": "", "beatmap": ""},
        "files": {"beatmap": ""},
        "directPath": {"beatmapFile": path or ""},
        "resultsScreen": results or {},
    }
    if state_num is None:
        del snap["state"]["number"]
    return snap


HDDT = {"number": 72, "name": "HDDT", "array": [{"acronym": "HD"}, {"acronym": "DT", "settings": {"speed_change": 1.2}}],
        "rate": 1.2}
RES = {"accuracy": 96.5, "rank": "S", "maxCombo": 700, "hits": {"300": 470, "100": 25, "50": 3, "0": 2},
       "pp": {"current": 120.0, "fc": 140.0}, "mods": {"name": ""}, "createdAt": "2026-11-08T12:00:00Z"}

SCENARIOS = {
    "menu": lambda: _v2(0, "menu", MAP_A),
    "select_a": lambda: _v2(5, "selectPlay", MAP_A),
    "select_a_hddt": lambda: _v2(5, "selectPlay", MAP_A, HDDT),
    "select_b": lambda: _v2(5, "selectPlay", MAP_B),
    "playing_b": lambda: _v2(2, "play", MAP_B),
    "results_b": lambda: _v2(7, "resultScreen", MAP_B, results=RES),
    "results_b_nopp": lambda: _v2(7, "resultScreen", MAP_B, results=dict(RES, pp={"current": None})),
    "lazer_select": lambda: _v2(None, "PlaySongSelect", MAP_A),
    "lazer_play": lambda: _v2(None, "SoloPlayer", MAP_B),
    "lazer_results": lambda: _v2(None, "ResultsScreen", MAP_B, results=RES),
    "weird": lambda: _v2(99, "WeirdScreen", MAP_A),
    "error": lambda: {"error": "osu is not running"},
}


class Mock:
    scenario = "select_a"
    fail_calc = False
    calc_calls = 0
    v2_calls = 0
    last_calc = None
    int_mods_only = False    # like the user's real tosu: JSON mods -> HTTP 500 "expected GameMods"
    json_mods_only = False   # a hypothetical tosu that refuses integers
    dynamic = None           # callable -> snapshot; overrides `scenario` (attempt timelines)


# ----------------------------------------------------------------------------
# play simulation (attempt telemetry). Field names/shapes copied from a real lazer + tosu snapshot.
# ----------------------------------------------------------------------------

class PlaySim:
    """One map's play: an object every `gap` ms from first to last; misses at given map times; hit errors
    drift late by `drift` ms over the map. snap(state, live) -> a tosu v2 snapshot at map time `live`."""

    def __init__(self, bm=None, first=1000, last=61000, gap=150, misses=(), drift=0.0, player="tester",
                 mods=None):
        self.bm, self.first, self.last, self.gap = bm or MAP_B, first, last, gap
        self.misses = sorted(misses)
        self.drift, self.player, self.mods = drift, player, mods or {"number": 0, "name": "", "array": [], "rate": 1}
        self.times = list(range(first, last + 1, gap))
        rnd = random.Random(3)
        self.err = [rnd.gauss(0, 25) + drift * (t - first) / (last - first) for t in self.times]

    def counts(self, live):
        n = sum(1 for t in self.times if t <= live)
        m = sum(1 for t in self.misses if t <= live)
        m = min(m, n)
        n100 = (n - m) // 10
        return {"300": n - m - n100, "100": n100, "50": 0, "0": m}, n

    def snap(self, state, live, failed=False, hp=80.0, results=None, zero_play=False, player=None):
        num, name = {"play": (2, "play"), "select": (5, "selectPlay"), "results": (7, "resultScreen"),
                     "menu": (0, "menu"), "loader": (99, "loading")}[state]
        s = _v2(num, name, self.bm, self.mods, results=results)
        h, n = self.counts(live)
        if zero_play:
            h, n = {"300": 0, "100": 0, "50": 0, "0": 0}, 0
        errs = [round(e, 3) for e, t in zip(self.err, self.times) if t <= live][:max(0, n - h["0"])]
        tot = sum(h.values())
        acc = 100.0 * (300 * h["300"] + 100 * h["100"] + 50 * h["50"]) / (300 * tot) if tot else 0
        ur = 10 * (sum((e - sum(errs) / len(errs)) ** 2 for e in errs) / len(errs)) ** 0.5 if errs else 0
        h_full = dict(h, geki=0, katu=0, sliderBreaks=0, sliderEndHits=0, smallTickHits=0, largeTickHits=0)
        s["play"] = {"failed": failed, "playerName": player or self.player, "mode": {"number": 0, "name": "osu"},
                     "score": tot * 300, "accuracy": acc, "healthBar": {"normal": 0 if failed else hp, "smooth": hp},
                     "hits": h_full, "hitErrorArray": errs, "combo": {"current": 5, "max": min(n, 200)},
                     "mods": dict(self.mods, checksum=""), "rank": {"current": "A", "maxThisPlay": "A"},
                     "pp": {"current": 0, "fc": 0}, "unstableRate": ur}
        s["beatmap"]["time"] = {"live": live, "firstObject": self.first, "lastObject": self.last,
                                "mp3Length": self.last + 5000}
        s["beatmap"]["status"] = {"number": 4, "name": "ranked"}
        s["profile"]["name"] = "tester"
        s["game"] = {"focused": True, "paused": False}
        return s

    def results(self, live=None, acc=None):
        h, _ = self.counts(self.last if live is None else live)
        return {"scoreId": 1, "playerName": self.player, "accuracy": acc if acc is not None else 93.1,
                "hits": dict(h, geki=0, katu=0), "maxCombo": 321, "rank": "A", "mods": {"name": ""},
                "pp": {"current": 80.0, "fc": 120.0}, "createdAt": "2026-11-09T18:00:00Z"}


def mock_stars(base, acs, rate):
    s = base * (1 + 0.9 * (rate - 1))
    if "HR" in acs: s *= 1.07
    if "EZ" in acs: s *= 0.75
    if "FL" in acs: s *= 1.15
    return s


def mock_calc(q):
    path = q.get("path")
    base, objs, combo_max = MAP_A["stars"], MAP_A["objects"], MAP_A["combo"]
    if path:
        try:
            txt = Path(path).read_text("utf-8")
        except OSError:
            return {"error": "beatmap not found"}
        for tok in txt.split():
            k, _, v = tok.partition("=")
            if k == "stars": base = float(v)
            if k == "objects": objs = int(v)
            if k == "combo": combo_max = int(v)
    mods = q.get("mods", "[]")
    try:
        ml = json.loads(mods)
    except ValueError:
        ml = []
    acs, rate = set(), 1.0
    if isinstance(ml, int):
        for b, a in ((8, "HD"), (16, "HR"), (2, "EZ"), (64, "DT"), (256, "HT"), (1, "NF"), (1024, "FL")):
            if ml & b: acs.add(a)
        rate = 1.5 if "DT" in acs else 0.75 if "HT" in acs else 1.0
    else:
        for m in ml:
            a = m.get("acronym")
            acs.add(a)
            if a in ("DT", "NC", "HT", "DC"):
                rate = float((m.get("settings") or {}).get("speed_change", 1.5 if a in ("DT", "NC") else 0.75))
    if "clockRate" in q:
        rate = float(q["clockRate"])
    stars = mock_stars(base, acs, rate)
    acc = float(q.get("acc", 100))
    miss = int(q.get("nMisses", 0))
    combo = int(q.get("combo", combo_max - miss if miss else combo_max))
    pp_ss = 1.5 * stars ** 3 * (1.06 if "HD" in acs else 1) * (0.9 if "NF" in acs else 1)
    pp = pp_ss * 0.5 ** ((100 - acc) / 2.5) * 0.97 ** miss * min(1.0, combo / combo_max) ** 0.5
    n = max(1, objs)
    return {"attributes": {"mode": 0, "version": 14, "nCircles": n * 3 // 4, "nSliders": n - n * 3 // 4,
                           "nSpinners": 0, "ar": 9.0, "cs": 4.0, "hp": 5.0, "od": 8.0, "arHitWindow": 600,
                           "odHitWindow": 32, "clockRate": rate, "bpm": 180 * rate},
            "performance": {"mode": 0, "pp": pp, "ppAcc": pp * 0.3, "ppAim": pp * 0.45, "ppFlashlight": 0,
                            "ppSpeed": pp * 0.25, "effectiveMissCount": miss,
                            "difficulty": {"aim": stars / 2, "speed": stars / 2.2, "flashlight": 0, "sliderFactor": 1,
                                           "speedNoteCount": 100, "ar": 9.0, "od": 8.0, "nCircles": n * 3 // 4,
                                           "nSliders": n - n * 3 // 4, "nSpinners": 0, "stars": stars,
                                           "maxCombo": combo_max}}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/json/v2":
            Mock.v2_calls += 1
            dyn = Mock.dynamic
            if dyn is not None:
                return self._send(dyn())
            if Mock.scenario == "error500":      # what the real tosu answers while osu! is closed
                return self._send({"error": "osu is not ready/running"}, 500)
            return self._send(SCENARIOS[Mock.scenario]())
        if u.path == "/api/calculate/pp":
            Mock.calc_calls += 1
            Mock.last_calc = q
            if Mock.fail_calc:
                return self._send({"error": "calc failed"})
            is_int = q.get("mods", "0").lstrip("-").isdigit()
            if (Mock.int_mods_only and not is_int) or (Mock.json_mods_only and is_int):
                return self._send({"error": "invalid type: floating point `NaN`, expected GameMods"}, 500)
            return self._send(mock_calc(q))
        if u.path == "/mock/set":
            if "scenario" in q: Mock.scenario = q["scenario"]
            Mock.fail_calc = q.get("fail_calc") == "1"
            return self._send({"ok": True})
        if u.path == "/mock/stats":
            return self._send({"calc_calls": Mock.calc_calls, "v2_calls": Mock.v2_calls})
        self._send({"error": "not found"}, 404)


def start_mock(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ----------------------------------------------------------------------------
# fixture data (temp folder)
# ----------------------------------------------------------------------------

def write_fixture(d: Path, now=None, seed=7):
    now = now or time.time()
    rnd = random.Random(seed)
    maps = d / "maps"
    maps.mkdir(exist_ok=True)
    osu_idx, replays = {}, {}
    # 24 maps 3.0 .. 5.3 stars; this player's curve: acc 0.97 - 0.05/star, misses 0.4/100 at 3* rising x2/star
    for i in range(24):
        st = 3.0 + i * 0.1
        md5 = f"{i:032x}"
        fp = maps / f"m{i}.osu"
        fp.write_text(f"osu file format v14\n//mock stars={st:.2f} objects=400 combo=600\n", "utf-8")
        osu_idx[str(fp)] = md5
        for k in range(4):
            age = rnd.uniform(0, 20)
            acc = 0.97 - 0.05 * (st - 3) + rnd.gauss(0, 0.012)
            miss = max(0, round(400 * 0.004 * 2 ** (st - 3) * rnd.uniform(0.5, 1.5)))
            replays[f"r{i}_{k}"] = {"hdr": {"mode": 0, "md5": md5, "player": "tester", "uid": 42,
                                            "ts": int(now - age * 86400), "score": 1000 + k, "mods": "HD" if k % 2 else "NM",
                                            "mbits": 8 if k % 2 else 0, "rate": 1.0, "acc": round(acc, 4),
                                            "misses": miss, "combo": 300},
                                    "sum": {"nobj": 400}}
    # same uid under an old name (counted), another account (ignored), a relax play (ignored)
    replays["old_name"] = {"hdr": {"mode": 0, "md5": f"{5:032x}", "player": "tester_old", "uid": 42,
                                   "ts": int(now - 86400), "score": 5, "mods": "NM", "mbits": 0, "rate": 1.0,
                                   "acc": 0.93, "misses": 1, "combo": 500}, "sum": {"nobj": 400}}
    for j in range(30):
        replays[f"other{j}"] = {"hdr": {"mode": 0, "md5": f"{j % 24:032x}", "player": "someone", "uid": 99,
                                        "ts": int(now - 3600), "score": j, "mods": "NM", "mbits": 0, "rate": 1.0,
                                        "acc": 0.40, "misses": 80, "combo": 10}, "sum": {"nobj": 400}}
    replays["relax"] = {"hdr": {"mode": 0, "md5": f"{3:032x}", "player": "tester", "uid": 42, "ts": int(now),
                                "score": 9, "mods": "NFRX", "mbits": 129, "rate": 1.0, "acc": 0.30, "misses": 90,
                                "combo": 5}, "sum": {"nobj": 400}}
    # map A / B of the live scenarios are in the index too
    for bm in (MAP_A, MAP_B):
        fp = maps / f"{bm['checksum'][:4]}.osu"
        fp.write_text(f"osu file format v14\n//mock stars={bm['stars']} objects={bm['objects']} combo={bm['combo']}\n")
        osu_idx[str(fp)] = bm["checksum"]
    osu_idx[str(maps / "x.mp3")] = ""
    (d / ".osu_coach_cache.json").write_text(json.dumps({"osu": osu_idx, "osr": {}}))
    (d / ".coach_replays.json").write_text(json.dumps({"v": 5, "replays": replays}))
    (d / "coach_data.json").write_text(json.dumps({"player": "tester"}))
    top = [{"beatmap_id": 2000 + i, "md5": "", "pp": round(103 * 0.985 ** i, 2)} for i in range(100)]
    top[10]["beatmap_id"] = MAP_B["id"]   # an older, worse play on map B
    (d / "pp_data.json").write_text(json.dumps({"version": 1, "top_plays": top}))
    return top


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------

SRV = None
TMP = None
pp = None


def mock_url():
    return f"http://127.0.0.1:{SRV.server_address[1]}"


def set_scenario(name, fail_calc=False):
    Mock.scenario = name
    Mock.fail_calc = fail_calc


def reset_module():
    pp._sr.update({"loaded": False, "data": {}, "dirty": 0, "mtime": None, "checked": 0.0})
    pp._skill_mem["model"] = None
    pp._idx.update({"mtime": None, "map": {}})
    for f in (".coach_sr_cache.json", ".coach_skill.json"):
        try:
            (TMP / f).unlink()
        except OSError:
            pass


def wait_for(cond, timeout=8.0, step=0.05):
    t = time.time() + timeout
    while time.time() < t:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


class T1Basics(unittest.TestCase):
    def setUp(self):
        os.environ["OSU_COACH_TOSU_URL"] = mock_url()
        set_scenario("select_a")
        reset_module()

    def test_available(self):
        self.assertTrue(pp.tosu_available())
        os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
        t = time.time()
        self.assertFalse(pp.tosu_available())
        self.assertLess(time.time() - t, 2.0)
        os.environ["OSU_COACH_TOSU_URL"] = mock_url()
        set_scenario("error")
        self.assertFalse(pp.tosu_available())
        self.assertEqual({k: v for k, v in pp.tosu_status().items() if k in ("tosu", "osu")},
                         {"tosu": True, "osu": False})
        set_scenario("error500")
        self.assertFalse(pp.tosu_available())
        st = pp.tosu_status()
        self.assertEqual((st["tosu"], st["osu"], st["error"]), (True, False, "osu is not ready/running"))
        set_scenario("select_a")
        self.assertEqual((pp.tosu_status()["tosu"], pp.tosu_status()["osu"]), (True, True))
        os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
        self.assertEqual((pp.tosu_status()["tosu"], pp.tosu_status()["osu"]), (False, False))

    def test_mods_key(self):
        k = pp.mods_key
        self.assertEqual(k("HDDT"), "DTHD")
        self.assertEqual(k("DT,HD"), "DTHD")
        self.assertEqual(k([{"acronym": "NC", "settings": {"speed_change": 1.2}}, {"acronym": "HD"}]), "DTHD")
        self.assertEqual(k(72), "DTHD")
        self.assertEqual(k(None), "NM")
        self.assertEqual(k("NM"), "NM")
        self.assertEqual(k("CLSD"), "NM")
        self.assertEqual(k("NM", 1.2), "DT")
        self.assertEqual(k("DT", 1.0), "NM")
        self.assertEqual(pp.parse_mods([{"acronym": "DT", "settings": {"speed_change": 1.25}}])[1], 1.25)
        self.assertEqual(pp.parse_mods("HT")[1], 0.75)
        self.assertEqual(pp.parse_mods(576)[0:2], (["DT"], 1.5))   # NC bits
        self.assertEqual(pp.mods_display(["DT", "HD"]), "HDDT")

    def test_calc_pp_shape_and_monotone(self):
        p = str(TMP / "maps" / "m10.osu")
        r = pp.calc_pp(p, None, None, acc=100)
        for key in ("pp", "stars", "aim", "speed", "max_combo", "n_objects", "ar", "od", "cs", "bpm"):
            self.assertIn(key, r)
        self.assertAlmostEqual(r["stars"], 4.0, places=2)
        self.assertEqual(r["n_objects"], 400)
        self.assertEqual(r["max_combo"], 600)
        accs = [pp.calc_pp(p, None, None, acc=a)["pp"] for a in (90, 95, 98, 100)]
        self.assertEqual(accs, sorted(accs))
        mis = [pp.calc_pp(p, None, None, acc=97, misses=m)["pp"] for m in (0, 2, 8)]
        self.assertEqual(mis, sorted(mis, reverse=True))
        self.assertGreater(pp.calc_pp(p, "HDDT", 1.2)["pp"], pp.calc_pp(p, "HD", None)["pp"])
        self.assertAlmostEqual(pp.calc_pp(p, "DT", 1.2)["stars"], 4.0 * 1.18, places=2)
        self.assertEqual(Mock.last_calc.get("clockRate"), "1.2")
        # the real tosu only accepts integer mods (JSON -> HTTP 500 "expected GameMods"); rate via clockRate
        self.assertEqual(Mock.last_calc["mods"], "64")
        Mock.int_mods_only = True
        try:
            r_int = pp.calc_pp(p, "HDDT", 1.2)
            self.assertIsNotNone(r_int)
            self.assertEqual(Mock.last_calc["mods"], "72")
            # a tosu that refuses integers gets the lazer JSON form as a fallback
            Mock.int_mods_only, Mock.json_mods_only = False, True
            r_json = pp.calc_pp(p, "HDDT", 1.2)
            self.assertIsNotNone(r_json)
            self.assertIn('"speed_change":1.2', Mock.last_calc["mods"])
            self.assertAlmostEqual(r_json["pp"], r_int["pp"], places=3)
        finally:
            Mock.int_mods_only = Mock.json_mods_only = False
        self.assertAlmostEqual(pp.calc_pp(p, None, None, acc=0.95)["pp"], pp.calc_pp(p, None, None, acc=95)["pp"])
        set_scenario("select_a", fail_calc=True)
        self.assertIsNone(pp.calc_pp(p))
        set_scenario("select_a")
        self.assertIsNone(pp.calc_pp(str(TMP / "maps" / "missing.osu")))
        os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
        self.assertIsNone(pp.calc_pp(p))

    def test_star_rating_cache(self):
        md5 = f"{10:032x}"
        c0 = Mock.calc_calls
        s1 = pp.star_rating(md5, None, "HD", 1.0)
        self.assertAlmostEqual(s1, 4.0, places=2)
        self.assertEqual(Mock.calc_calls, c0 + 1)
        self.assertEqual(pp.star_rating(md5, None, "HD", 1.0), s1)
        self.assertEqual(Mock.calc_calls, c0 + 1)           # in-memory hit
        disk = json.loads((TMP / ".coach_sr_cache.json").read_text())
        self.assertIn(f"{md5}|HD|1.00", disk["sr"])
        reset_module_keep = dict(disk)
        pp._sr.update({"loaded": False, "data": {}, "dirty": 0})
        self.assertEqual(pp.star_rating(md5, None, "HD", None), s1)   # disk hit
        self.assertEqual(Mock.calc_calls, c0 + 1)
        s2 = pp.star_rating(md5, None, "DTHD", 1.2)
        self.assertGreater(s2, s1)
        self.assertEqual(pp.star_rating(md5, None, [{"acronym": "HD"}, {"acronym": "NC", "settings": {"speed_change": 1.2}}], 1.2), s2)
        self.assertEqual(Mock.calc_calls, c0 + 2)
        self.assertIsNone(pp.star_rating("f" * 32, None, "NM", 1.0))   # unknown map: no path, no call
        self.assertEqual(Mock.calc_calls, c0 + 2)
        self.assertTrue(reset_module_keep)

    def test_rank_gain_exact(self):
        top = [{"beatmap_id": i, "pp": 100 - i * 0.5} for i in range(100)]

        def brute(new, bid):
            old = sorted([t["pp"] for t in top], reverse=True)
            keep = [t["pp"] for t in top if t["beatmap_id"] != bid]
            prev = [t["pp"] for t in top if t["beatmap_id"] == bid]
            if prev and prev[0] >= new:
                return 0.0
            new_l = sorted(keep + [new], reverse=True)[:100]
            w = lambda l: sum(p * 0.95 ** i for i, p in enumerate(l))
            return round(w(new_l) - w(old[:100]), 2)
        for new, bid in ((120, None), (75.3, None), (40, None), (99.9, 5), (60, 5), (98.0, 50)):
            self.assertAlmostEqual(pp.rank_gain_pp(new, top, beatmap_id=bid), brute(new, bid), places=2)
        self.assertEqual(pp.rank_gain_pp(40, top), 0.0)     # below #100 -> nothing
        self.assertIsNone(pp.rank_gain_pp(None, top))
        self.assertIsNone(pp.rank_gain_pp(50, None))

    def test_predict_needs_path(self):
        # no .osu path -> no silent "whatever map is selected in game" calculation
        n0 = Mock.calc_calls
        self.assertIsNone(pp.predict(None, "HD", None))
        self.assertIsNone(pp.predict(pp.osu_path_for_md5("f" * 32), "HD", None))
        self.assertEqual(Mock.calc_calls, n0)

    def test_ranked_of(self):
        self.assertTrue(pp.ranked_of({"beatmap": {"status": {"number": 4, "name": "ranked"}}}))
        self.assertTrue(pp.ranked_of({"beatmap": {"status": {"name": "Approved"}}}))
        self.assertFalse(pp.ranked_of({"beatmap": {"status": {"number": 7, "name": "loved"}}}))
        self.assertFalse(pp.ranked_of({"beatmap": {"status": "Graveyard"}}))
        self.assertIsNone(pp.ranked_of({"beatmap": {"status": {"number": 4}}}))   # numbers differ stable/lazer
        self.assertIsNone(pp.ranked_of({"beatmap": {}}))

    def test_sr_cache_merges_other_process(self):
        f = TMP / ".coach_sr_cache.json"
        f.write_text(json.dumps({"v": pp.SR_CACHE_VERSION, "sr": {"x|NM|1.00": 4.0}}), "utf-8")
        pp._sr.update({"loaded": False, "data": {}, "dirty": 0, "mtime": None, "checked": 0.0})
        self.assertEqual(pp._sr_get("x", "NM", 1.0), 4.0)
        # another process adds a key after we loaded
        f.write_text(json.dumps({"v": pp.SR_CACHE_VERSION, "sr": {"x|NM|1.00": 4.0, "y|NM|1.00": 5.0}}), "utf-8")
        with pp._sr_lock:
            pp._sr["data"]["z|NM|1.00"] = 6.0
            pp._sr["dirty"] += 1
        pp._sr_flush()
        disk = json.loads(f.read_text("utf-8"))["sr"]
        self.assertEqual(set(disk), {"x|NM|1.00", "y|NM|1.00", "z|NM|1.00"})   # the other process's key survived
        # a v1 cache (written while tosu ignored path= and rated the selected map) is discarded
        f.write_text(json.dumps({"v": 1, "sr": {"q|NM|1.00": 5.0}}), "utf-8")
        pp._sr.update({"loaded": False, "data": {}, "dirty": 0, "mtime": None, "checked": 0.0})
        self.assertIsNone(pp._sr_get("q", "NM", 1.0))

    def test_est_combo(self):
        self.assertEqual(pp._est_combo(600, 0), 600)
        # expected longest stretch with random misses: max * H(m+1)/(m+1)
        self.assertEqual(pp._est_combo(600, 1), 450)
        self.assertEqual(pp._est_combo(600, 5), 245)
        self.assertLessEqual(pp._est_combo(10, 9), 1)
        self.assertIsNone(pp._est_combo(None, 3))


class T2Model(unittest.TestCase):
    def setUp(self):
        os.environ["OSU_COACH_TOSU_URL"] = mock_url()
        set_scenario("select_a")
        reset_module()

    def test_player_filter(self):
        recs = pp._player_records()
        self.assertEqual(len(recs), 24 * 4 + 1)     # own name + old name same uid; no other account / relax
        self.assertTrue(all(r["acc"] > 0.5 for r in recs))

    def test_fit_with_sr(self):
        m = pp.skill_model(refresh=True)
        self.assertEqual(m["n"], 97)
        self.assertEqual(m["fit"]["sr_missing"], 0)
        accs = [m["acc_at"][k] for k in sorted(m["acc_at"], key=float)]
        self.assertEqual(accs, sorted(accs, reverse=True), "acc must not rise with stars")
        mrs = [m["miss_rate_at"][k] for k in sorted(m["miss_rate_at"], key=float)]
        self.assertEqual(mrs, sorted(mrs), "miss rate must not fall with stars")
        self.assertAlmostEqual(m["acc_at"]["4.0"], 0.92, delta=0.015)
        self.assertGreater(m["miss_rate_at"]["5.0"], m["miss_rate_at"]["3.5"])
        self.assertIsNotNone(m["comfort_stars"])
        self.assertTrue(3.0 <= m["comfort_stars"] <= 4.5, m["comfort_stars"])
        self.assertTrue((TMP / ".coach_skill.json").exists())
        # cached: second call makes no calc calls
        c = Mock.calc_calls
        pp._skill_mem["model"] = None
        m2 = pp.skill_model()
        self.assertEqual(Mock.calc_calls, c)
        self.assertEqual(m2["updated"], m["updated"])
        a, mr, ag, mg, conf, basis = pp._model_at(m, 4.0)
        self.assertEqual(conf, "high")
        self.assertGreaterEqual(ag, a)
        self.assertLessEqual(mg, mr)
        a7, _, _, _, conf7, basis7 = pp._model_at(m, 6.5)
        self.assertEqual(conf7, "low")
        self.assertIn("extrapolated", basis7)
        self.assertLess(a7, m["acc_at"]["5.0"] - 0.02)

    def test_recency_weighting(self):
        # add many OLD bad plays at 4.0*: with a 21-day half-life they barely move the fit
        rc = json.loads((TMP / ".coach_replays.json").read_text())
        backup = json.dumps(rc)
        now = time.time()
        for k in range(30):
            rc["replays"][f"old{k}"] = {"hdr": {"mode": 0, "md5": f"{10:032x}", "player": "tester", "uid": 42,
                                                "ts": int(now - 120 * 86400), "score": 50000 + k, "mods": "NM",
                                                "mbits": 0, "rate": 1.0, "acc": 0.70, "misses": 40, "combo": 50},
                                        "sum": {"nobj": 400}}
        (TMP / ".coach_replays.json").write_text(json.dumps(rc))
        try:
            m = pp.skill_model(refresh=True)
            self.assertAlmostEqual(m["acc_at"]["4.0"], 0.92, delta=0.02)
        finally:
            (TMP / ".coach_replays.json").write_text(backup)

    def test_budget(self):
        old = pp.SR_BUDGET
        pp.SR_BUDGET = 5
        try:
            c = Mock.calc_calls
            m = pp.skill_model(refresh=True)
            self.assertEqual(Mock.calc_calls - c, 5)
            self.assertGreater(m["fit"]["sr_missing"], 0)
        finally:
            pp.SR_BUDGET = old

    def test_no_tosu(self):
        os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
        m = pp.skill_model(refresh=True)
        self.assertEqual(m["n"], 0)
        self.assertIn("tosu", m["basis"])
        self.assertIsNotNone(m["overall"]["acc_med"])
        a, mr, ag, mg, conf, basis = pp._model_at(m, 4.5)
        self.assertEqual(conf, "low")
        self.assertIsNone(pp.predict(str(TMP / "maps" / "m10.osu"), "NM", 1.0))

    def test_predict(self):
        pp.skill_model(refresh=True)
        path = str(TMP / "maps" / "m10.osu")
        r = pp.predict(path, "HD", 1.0)
        for k in ("stars", "pp_ss", "pp_fc_pred_acc", "pp_expected", "pp_good_run", "acc_expected",
                  "misses_expected", "acc_good", "misses_good", "confidence", "basis"):
            self.assertIn(k, r)
        self.assertGreaterEqual(r["pp_ss"], r["pp_good_run"])
        self.assertGreaterEqual(r["pp_good_run"], r["pp_expected"])
        self.assertGreaterEqual(r["pp_fc_pred_acc"], r["pp_expected"])
        self.assertEqual(r["confidence"], "high")
        self.assertAlmostEqual(r["acc_expected"], 0.92, delta=0.02)
        self.assertGreaterEqual(r["misses_expected"], r["misses_good"])
        hard = pp.predict(path, "HDDT", 1.5)       # ~5.8*: beyond the data
        self.assertEqual(hard["confidence"], "low")
        self.assertGreater(hard["pp_ss"], r["pp_ss"])
        self.assertLess(hard["acc_expected"], r["acc_expected"])
        set_scenario("select_a", fail_calc=True)
        self.assertIsNone(pp.predict(path, "HD", 1.0))


class T3Watcher(unittest.TestCase):
    def setUp(self):
        os.environ["OSU_COACH_TOSU_URL"] = mock_url()
        set_scenario("select_a")
        reset_module()
        pp.skill_model(refresh=True)
        self.states = []
        self.lock = threading.Lock()

    def cb(self, s):
        with self.lock:
            self.states.append(s)

    def last(self):
        with self.lock:
            return self.states[-1] if self.states else None

    def until(self, f, timeout=8.0):
        return wait_for(lambda: (lambda s: s if s and f(s) else None)(self.last()), timeout)

    def test_flow(self):
        threads_before = threading.active_count()
        w = pp.LiveWatcher(self.cb)        # real defaults: 0.5 s poll, 250 ms debounce
        w.start()
        try:
            s = self.until(lambda s: s["screen"] == "song_select" and s["prediction"])
            self.assertTrue(s, self.states[-3:])
            self.assertTrue(s["tosu_ok"])
            self.assertEqual((s["md5"], s["beatmap_id"], s["title"], s["mods"], s["rate"]),
                             (MAP_A["checksum"], 1001, "whiz", "NM", 1.0))
            self.assertAlmostEqual(s["stars"], 4.6, places=1)
            self.assertIn(s["prediction"]["confidence"], ("medium", "high"))
            for k in ("tosu_ok", "screen", "md5", "beatmap_id", "artist", "title", "version", "mods", "rate",
                      "stars", "prediction", "results"):
                self.assertIn(k, s)
            self.assertFalse(any(k.startswith("_") for k in s))
            json.dumps(s)
            nm_ss = s["prediction"]["pp_ss"]
            set_scenario("select_a_hddt")
            s = self.until(lambda s: s["mods"] == "HDDT" and s["prediction"])
            self.assertTrue(s)
            self.assertEqual(s["rate"], 1.2)
            self.assertGreater(s["prediction"]["pp_ss"], nm_ss)
            set_scenario("select_b")
            s = self.until(lambda s: s["md5"] == MAP_B["checksum"] and s["prediction"])
            self.assertTrue(s)
            set_scenario("playing_b")
            self.assertTrue(self.until(lambda s: s["screen"] == "playing"))
            set_scenario("results_b")
            s = self.until(lambda s: s["screen"] == "results" and s["results"])
            self.assertTrue(s)
            res = s["results"]
            self.assertEqual((res["pp"], res["acc"], res["misses"]), (120.0, 0.965, 2))
            top = json.loads((TMP / "pp_data.json").read_text())["top_plays"]
            self.assertAlmostEqual(res["rank_gain_pp"], pp.rank_gain_pp(120.0, top, beatmap_id=MAP_B["id"]), places=2)
            self.assertGreater(res["rank_gain_pp"], 0)
            # going back to the same map in song select reuses the cached prediction (no new calc)
            set_scenario("select_a")
            self.assertTrue(self.until(lambda s: s["md5"] == MAP_A["checksum"] and s["prediction"]))
            c = Mock.calc_calls
            set_scenario("select_b")
            self.assertTrue(self.until(lambda s: s["md5"] == MAP_B["checksum"] and s["prediction"]))
            self.assertEqual(Mock.calc_calls, c)
            set_scenario("menu")
            self.assertTrue(self.until(lambda s: s["screen"] == "menu"))
        finally:
            w.stop()
        self.assertFalse(w._thread.is_alive())
        self.assertLessEqual(threading.active_count(), threads_before + 1)

    def test_lazer_names_and_unknown(self):
        w = pp.LiveWatcher(self.cb, poll=0.1, debounce=0.05)
        w.start()
        try:
            set_scenario("lazer_select")
            self.assertTrue(self.until(lambda s: s["screen"] == "song_select" and s["prediction"]))
            set_scenario("lazer_play")
            self.assertTrue(self.until(lambda s: s["screen"] == "playing"))
            set_scenario("lazer_results")
            self.assertTrue(self.until(lambda s: s["screen"] == "results" and s["results"]))
            set_scenario("weird")
            self.assertTrue(self.until(lambda s: s["screen"] == "other" and s["tosu_ok"]))
            set_scenario("results_b_nopp")
            s = self.until(lambda s: s["screen"] == "results" and s["results"] and s["md5"] == MAP_B["checksum"])
            self.assertTrue(s)
            self.assertIsNotNone(s["results"]["pp"])   # calculated by us when tosu reports none
        finally:
            w.stop()
        log = (TMP / "pp_live_log.txt").read_text("utf-8")
        self.assertIn("WeirdScreen".lower(), log)

    def test_unreachable_and_recovery(self):
        os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
        w = pp.LiveWatcher(self.cb, poll=0.1, slow_poll=0.3, debounce=0.05)
        w.start()
        try:
            s = self.until(lambda s: s["tosu_ok"] is False, timeout=3)
            self.assertTrue(s)
            self.assertEqual(s["screen"], "other")
            n = len(self.states)
            time.sleep(1.0)
            self.assertEqual(len(self.states), n, "unchanged state must not be re-emitted")
            os.environ["OSU_COACH_TOSU_URL"] = mock_url()
            self.assertTrue(self.until(lambda s: s["tosu_ok"] and s["prediction"], timeout=5))
            set_scenario("error")      # tosu up but no game data
            self.assertTrue(self.until(lambda s: s["tosu_ok"] is False, timeout=5))
        finally:
            w.stop()

    def test_callback_errors_and_calc_failure(self):
        calls = []

        def bad(s):
            calls.append(s)
            raise RuntimeError("pet bug")
        set_scenario("select_b", fail_calc=True)
        w = pp.LiveWatcher(bad, poll=0.1, debounce=0.05)
        w.start()
        try:
            self.assertTrue(wait_for(lambda: len(calls) >= 1))
            time.sleep(0.5)
            self.assertTrue(w._thread.is_alive())
            self.assertIsNone(calls[-1]["prediction"])
            set_scenario("select_a")
            self.assertTrue(wait_for(lambda: calls[-1]["md5"] == MAP_A["checksum"] and calls[-1]["prediction"]))
        finally:
            w.stop()

    def test_debounce_scrolling(self):
        # flicking A<->B faster than the debounce must not compute predictions for every map
        w = pp.LiveWatcher(self.cb, poll=0.05, debounce=0.3)
        stop = threading.Event()

        def flick():
            i = 0
            while not stop.is_set():
                set_scenario("select_a" if i % 2 else "select_b")
                i += 1
                time.sleep(0.08)
        c = Mock.calc_calls
        t = threading.Thread(target=flick, daemon=True)
        t.start()
        w.start()
        try:
            time.sleep(1.5)
            self.assertEqual(Mock.calc_calls, c, "no prediction while scrolling")
            stop.set(); t.join()
            set_scenario("select_b")
            self.assertTrue(self.until(lambda s: s["md5"] == MAP_B["checksum"] and s["prediction"]))
        finally:
            stop.set()
            w.stop()


def drive(tr, steps, t0=1_800_000_000.0):
    """Feed (snapshot, seconds_since_start) pairs into an AttemptTracker -> finished attempts."""
    out = []
    for snap, sec in steps:
        out += tr.feed(snap, pp.screen_of(snap), now_wall=t0 + sec, now_mono=1000.0 + sec)
    if steps and pp.screen_of(steps[-1][0]) in ("song_select", "menu"):
        # the watcher keeps polling: song select is only final on its second read (one read can be a glitch)
        snap, sec = steps[-1]
        out += tr.feed(snap, pp.screen_of(snap), now_wall=t0 + sec + 0.5, now_mono=1000.0 + sec + 0.5)
    return out


def play_steps(sim, start_ms, end_ms, sec0=0.0, step_ms=500, **kw):
    """Play samples every 0.5 s of map time (the watcher's real poll) -> [(snap, sec)]."""
    out, live, sec = [], start_ms, sec0
    while live <= end_ms:
        out.append((sim.snap("play", live, **kw), sec))
        live += step_ms
        sec += step_ms / 1000.0
    return out


class T5Attempts(unittest.TestCase):
    """AttemptTracker end-of-attempt rules, deterministic (fake clocks)."""

    def setUp(self):
        self.tr = pp.AttemptTracker(poll_s=0.5)

    def test_pass_with_results(self):
        sim = PlaySim(misses=(12000, 30100, 30250, 52000), drift=20)
        steps = [(sim.snap("select", 40000), 0.0)] + play_steps(sim, 0, 62000, sec0=1.0)
        sec = steps[-1][1]
        # lazer: results state may show empty resultsScreen numbers for a moment, then the real ones
        empty = {"hits": {"0": 0, "50": 0, "100": 0, "300": 0}, "accuracy": 0, "playerName": ""}
        r = drive(self.tr, steps + [(sim.snap("results", 62000, results=empty, zero_play=True), sec + 0.5)])
        self.assertEqual(r, [], "must wait for the results numbers")
        r = drive(self.tr, [(sim.snap("results", 62000, results=sim.results(acc=93.1), zero_play=True), sec + 1.0)])
        self.assertEqual(len(r), 1)
        a = r[0]
        self.assertEqual((a["outcome"], a["end"], a["progress"]), ("passed", "results", 1.0))
        self.assertEqual(a["md5"], MAP_B["checksum"])
        self.assertEqual(a["hits"], {k: v for k, v in sim.counts(61000)[0].items()})
        self.assertEqual(a["misses"], 4)
        self.assertAlmostEqual(a["acc"], 0.931, places=4)
        self.assertEqual(a["max_combo"], 321)
        self.assertEqual(len(a["miss_times_ms"]), 4)
        for got, true in zip(sorted(a["miss_times_ms"]), sim.misses):
            self.assertLessEqual(abs(got - true), 250, (got, true))   # midpoint of a 0.5 s poll
        self.assertEqual(len(a["hit_errors"]), sum(sim.counts(61000)[0].values()) - 4)
        self.assertGreater(a["ur"], 100)
        self.assertGreater(a["mean_error_ms"], 5)    # the late drift shows up
        self.assertEqual((a["first_ms"], a["last_ms"]), (1000, 61000))
        self.assertTrue(a["own"])
        self.assertEqual(a["source"], "live")
        for k in ("ts", "md5", "beatmap_id", "artist", "title", "version", "mods", "rate", "stars", "outcome",
                  "time_ms", "first_ms", "last_ms", "progress", "acc", "hits", "max_combo", "ur", "hit_errors",
                  "miss_times_ms"):
            self.assertIn(k, a)
        json.dumps(a)

    def test_results_timeout_uses_live_numbers(self):
        sim = PlaySim(misses=(5000,))
        steps = play_steps(sim, 0, 62000)
        sec = steps[-1][1]
        empty = {"hits": {"0": 0, "50": 0, "100": 0, "300": 0}, "accuracy": 0}
        self.assertEqual(drive(self.tr, steps + [(sim.snap("results", 0, results=empty, zero_play=True), sec + 0.5)]), [])
        r = drive(self.tr, [(sim.snap("results", 0, results=empty, zero_play=True), sec + 0.5 + pp.RESULTS_WAIT_S + 0.1)])
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["outcome"], "passed")
        self.assertEqual(r[0]["misses"], 1)
        self.assertIsNotNone(r[0]["acc"])

    def test_fail_then_song_select(self):
        # real lazer: failed=true + hp 0 while state stays 2; song select then shows play.* zeroed
        sim = PlaySim(misses=(8000, 20000, 25000, 26000, 27000, 28000, 29000))
        steps = play_steps(sim, 0, 29000) + [(sim.snap("play", 29400, failed=True), 15.0),
                                              (sim.snap("play", 29400, failed=True), 15.5),
                                              (sim.snap("select", 44000, zero_play=True), 17.0)]
        r = drive(self.tr, steps)
        self.assertEqual(len(r), 1)
        a = r[0]
        self.assertEqual((a["outcome"], a["end"]), ("failed", "fail_quit"))
        self.assertEqual(a["misses"], 7, "numbers come from the last play sample, not the zeroed select")
        self.assertAlmostEqual(a["progress"], (29400 - 1000) / 60000, places=3)
        self.assertEqual(a["hp_min"], 0)
        self.assertTrue(a["failed_seen"])

    def test_quit_mid_map(self):
        sim = PlaySim(misses=(15000,))
        r = drive(self.tr, play_steps(sim, 0, 20000) + [(sim.snap("select", 50000, zero_play=True), 11.0)])
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["outcome"], r[0]["end"]), ("quit", "quit"))
        self.assertAlmostEqual(r[0]["progress"], 19000 / 60000, places=3)
        self.assertEqual(r[0]["time_ms"], 20000)

    def test_retry_in_play(self):
        sim = PlaySim(misses=(4000, 9000, 22000))
        a = play_steps(sim, 0, 25000)
        b = play_steps(sim, 0, 10000, sec0=a[-1][1] + 0.5)    # quick retry: clock + counters back to 0
        r = drive(self.tr, a + b)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["outcome"], r[0]["end"], r[0]["misses"]), ("quit", "retry", 3))
        r2 = drive(self.tr, [(sim.snap("select", 1000, zero_play=True), b[-1][1] + 1)])
        self.assertEqual(len(r2), 1)
        self.assertEqual((r2[0]["outcome"], r2[0]["misses"]), ("quit", 2))
        self.assertEqual(sorted(r2[0]["miss_times_ms"]), [3750, 8750])

    def test_fail_then_retry(self):
        sim = PlaySim(misses=tuple(range(10000, 20001, 1000)))
        a = play_steps(sim, 0, 20000) + [(sim.snap("play", 20200, failed=True), 11.0)]
        b = play_steps(sim, 0, 3000, sec0=13.0)
        r = drive(self.tr, a + b)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["outcome"], r[0]["end"]), ("failed", "retry"))

    def test_real_lazer_fail_screen_retry(self):
        # observed live 2026-11-09 (lazer + tosu, map "Emoji"): fail at live=22344 with 4 misses (failed=true,
        # hp 0, state 2); ~3 s later the player hits Retry: still state 2, failed=false, all hits 0, while
        # time.live stays at the fail position (22286) for a few polls before jumping to the map start
        sim = PlaySim(first=43, last=217773, misses=(9000, 15000, 20000, 22000))
        a = play_steps(sim, 0, 22000)
        s = a[-1][1]
        a += [(sim.snap("play", 22344, failed=True), s + 0.5), (sim.snap("play", 22344, failed=True), s + 2.0)]
        stale = [(sim.snap("play", 22286, zero_play=True), s + 3.0 + 0.5 * i) for i in range(6)]
        r = drive(self.tr, a + stale[:1])
        self.assertEqual(len(r), 1, "the retry is seen on the first zeroed not-failed poll")
        self.assertEqual((r[0]["outcome"], r[0]["end"], r[0]["misses"]), ("failed", "retry", 4))
        self.assertAlmostEqual(r[0]["progress"], (22344 - 43) / (217773 - 43), places=3)
        b = play_steps(sim, 0, 12000, sec0=s + 7.0)
        r = drive(self.tr, stale[1:] + b + [(sim.snap("select", 40000, zero_play=True), s + 40)])
        self.assertEqual(len(r), 1, [(x["outcome"], x["end"]) for x in r])
        self.assertEqual((r[0]["outcome"], r[0]["end"], r[0]["misses"]), ("quit", "quit", 1))
        self.assertFalse(r[0]["failed_seen"])
        self.assertLess(r[0]["progress"], 0.06, "stale fail-position clock must not count as progress")
        # fail screen -> retry -> straight back to song select during the stale phase: one failed attempt only
        tr = pp.AttemptTracker()
        r = drive(tr, a + stale[:3] + [(sim.snap("select", 40000, zero_play=True), s + 6)])
        self.assertEqual([(x["outcome"], x["end"]) for x in r], [("failed", "retry")])

    def test_loader_between_retries(self):
        sim = PlaySim()
        a = play_steps(sim, 0, 15000)
        s = a[-1][1]
        r = drive(self.tr, a + [(sim.snap("loader", 0, zero_play=True), s + 0.5)])
        self.assertEqual(r, [])
        r = drive(self.tr, [(sim.snap("play", 0, zero_play=True), s + 1.5)])
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["end"], "retry")
        # left to the loader and never came back: written as a quit after the grace period
        r = drive(self.tr, play_steps(sim, 500, 8000, sec0=s + 2.0))
        s2 = s + 2.0 + 16 * 0.5
        self.assertEqual(drive(self.tr, [(sim.snap("loader", 0, zero_play=True), s2)]), [])
        r = drive(self.tr, [(sim.snap("loader", 0, zero_play=True), s2 + pp.RETRY_GRACE_S + 0.1)])
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["outcome"], r[0]["end"]), ("quit", "quit"))

    def test_map_change_and_empty_attempts(self):
        a, b = PlaySim(bm=MAP_A), PlaySim(bm=MAP_B)
        r = drive(self.tr, play_steps(a, 0, 6000) + play_steps(b, 0, 3000, sec0=10))
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["md5"], r[0]["end"]), (MAP_A["checksum"], "map_change"))
        # restart before the first note: nothing judged -> not an attempt
        tr = pp.AttemptTracker()
        r = drive(tr, [(b.snap("play", 200), 0), (b.snap("play", 700), 0.5), (b.snap("select", 0, zero_play=True), 1)])
        self.assertEqual(r, [])
        self.assertEqual(tr.dropped, 1)

    def test_joined_late_and_glitch(self):
        sim = PlaySim(misses=(5000, 6000, 40000))
        # watcher starts at 30 s into the map: the two early misses are counted but not time-stamped "now"
        steps = play_steps(sim, 30000, 45000)
        glitch = sim.snap("play", 45500, zero_play=True)       # one bad read with zeros, clock moving on
        steps.append((glitch, 8.0))
        steps += play_steps(sim, 46000, 50000, sec0=8.5)
        steps.append((sim.snap("select", 0, zero_play=True), 20.0))
        r = drive(self.tr, steps)
        self.assertEqual(len(r), 1, r)
        a = r[0]
        self.assertTrue(a["joined_late"])
        self.assertEqual(a["misses"], 3)
        self.assertEqual(len(a["miss_times_ms"]), 1)
        self.assertLessEqual(abs(a["miss_times_ms"][0] - 40000), 250)

    def test_spectating_marked_not_own(self):
        sim = PlaySim(player="someone_else")
        r = drive(self.tr, play_steps(sim, 0, 10000) + [(sim.snap("select", 0, zero_play=True), 6)])
        self.assertFalse(r[0]["own"])

    def test_flush_and_lost(self):
        sim = PlaySim()
        drive(self.tr, play_steps(sim, 0, 10000))
        self.assertEqual(self.tr.lost(now_mono=2000.0), [])
        r = self.tr.lost(now_mono=2000.0 + pp.LOST_GIVEUP_S + 1)
        self.assertEqual((len(r), r[0]["end"]), (1, "tosu_lost"))
        drive(self.tr, play_steps(sim, 0, 5000, sec0=100))
        r = self.tr.flush()
        self.assertEqual((len(r), r[0]["end"]), (1, "stopped"))
        self.assertEqual(self.tr.flush(), [])

    def test_real_lazer_start_and_pass(self):
        # observed 2026-11-09 (lazer 2026.1005): state 2 begins while time.live still shows the song-select
        # preview position (48938) with zero hits for ~3 s, then the clock jumps to 558; at the end the
        # results state carries filled resultsScreen numbers on its first poll, song select 1 s later
        sim = PlaySim(first=2782, last=137782, misses=(40000, 90000))
        steps = [(sim.snap("select", 44897, zero_play=True), 0.0)]
        steps += [(sim.snap("play", 48938 + 1000 * i, zero_play=True), 1.0 + i) for i in range(3)]
        steps += play_steps(sim, 558, 138627, sec0=4.0)
        sec = steps[-1][1]
        steps.append((sim.snap("results", 139622, results=sim.results(acc=69.56)), sec + 1))
        steps.append((sim.snap("select", 140627, zero_play=True), sec + 2))
        r = drive(self.tr, steps)
        self.assertEqual(len(r), 1, [(x["outcome"], x["end"]) for x in r])
        a = r[0]
        self.assertEqual((a["outcome"], a["end"], a["progress"], a["misses"]), ("passed", "results", 1.0, 2))
        self.assertAlmostEqual(a["acc"], 0.6956, places=4)
        self.assertLessEqual(a["time_ms"], 138627)
        self.assertGreaterEqual(a["started_ts"], 1_800_000_000.0 + 4.0, "start = the real map start")
        self.assertFalse(a["joined_late"])
        # same, but the player skips the results screen (straight to song select after the last object)
        tr = pp.AttemptTracker()
        r = drive(tr, play_steps(sim, 558, 138627) + [(sim.snap("select", 0, zero_play=True), 300)])
        self.assertEqual((r[0]["outcome"], r[0]["end"], r[0]["progress"]), ("passed", "completed", 1.0))
        # preview-time phase then quit before the first note: nothing written
        tr = pp.AttemptTracker()
        r = drive(tr, [(sim.snap("play", 48938, zero_play=True), 0), (sim.snap("play", 49938, zero_play=True), 1),
                       (sim.snap("select", 0, zero_play=True), 2)])
        self.assertEqual(r, [])

    def test_hit_errors_cap(self):
        sim = PlaySim(first=0, last=400000, gap=100)
        r = drive(self.tr, play_steps(sim, 0, 400000, step_ms=5000) + [(sim.snap("select", 0, zero_play=True), 99)])
        self.assertEqual(len(r[0]["hit_errors"]), pp.HIT_ERRORS_CAP)


class T6AttemptFile(unittest.TestCase):
    def setUp(self):
        self.f = TMP / "att_test.jsonl"
        for p in (self.f, self.f.with_name("att_test.1.jsonl")):
            try:
                p.unlink()
            except OSError:
                pass

    def test_append_read_rotate(self):
        old = pp.ATTEMPTS_ROTATE_BYTES
        pp.ATTEMPTS_ROTATE_BYTES = 2000
        try:
            for i in range(30):
                self.assertTrue(pp.append_attempt({"ts": 1000 + i, "i": i, "pad": "x" * 100}, self.f))
            self.assertTrue(self.f.with_name("att_test.1.jsonl").exists())
            self.assertLessEqual(self.f.stat().st_size, 2000)
            with open(self.f, "a", encoding="utf-8") as fh:
                fh.write('{"ts": 5000, "broken": tr\n')       # torn line from a crash: skipped
            got = pp.read_attempts(0, self.f)
            idx = [r["i"] for r in got]
            self.assertEqual(idx, sorted(idx))
            self.assertEqual(idx[-1], 29)
            self.assertGreater(len(idx), 10)
            self.assertEqual([r["i"] for r in pp.read_attempts(1027, self.f)], [27, 28, 29])
            for ln in self.f.read_text("utf-8").splitlines()[:-1]:
                json.loads(ln)
        finally:
            pp.ATTEMPTS_ROTATE_BYTES = old

    def test_concurrent_appends_stay_whole_lines(self):
        def w(k):
            for i in range(40):
                pp.append_attempt({"ts": i, "k": k, "hit_errors": [1.5] * 300}, self.f)
        ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        lines = self.f.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 160)
        for ln in lines:
            self.assertEqual(len(json.loads(ln)["hit_errors"]), 300)

    def test_find_tosu_exe_skips_onedrive(self):
        d = TMP / "la"
        (d / "tosu").mkdir(parents=True, exist_ok=True)
        exe = d / "tosu" / "tosu.exe"
        exe.write_bytes(b"MZ")
        od = TMP / "OneDrive" / "tosu"
        od.mkdir(parents=True, exist_ok=True)
        (od / "tosu.exe").write_bytes(b"MZ")
        old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(d)
        try:
            self.assertEqual(pp.find_tosu_exe(str(od / "tosu.exe")), os.path.normpath(str(exe)))
            self.assertEqual(pp.find_tosu_exe(None), os.path.normpath(str(exe)))
        finally:
            if old is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old


class Timeline:
    """Thread-safe scripted tosu for the watcher: the map clock advances with real time x speed."""

    def __init__(self, speed=20.0):
        self.lock = threading.Lock()
        self.speed = speed
        self.set("select", PlaySim())

    def set(self, state, sim, live0=0, failed_at=None, results=None, until=None, player=None):
        with self.lock:
            self.state, self.sim, self.live0, self.t0 = state, sim, live0, time.monotonic()
            self.failed_at, self.res, self.until, self.player = failed_at, results, until, player

    def __call__(self):
        with self.lock:
            live = self.live0 + (time.monotonic() - self.t0) * 1000 * self.speed
            if self.until is not None:
                live = min(live, self.until)
            if self.state == "play":
                failed = self.failed_at is not None and live >= self.failed_at
                if failed:
                    live = self.failed_at
                return self.sim.snap("play", int(live), failed=failed, player=self.player)
            return self.sim.snap(self.state, 30000, results=self.res, zero_play=True)


class T7WatcherAttempts(unittest.TestCase):
    """End to end: LiveWatcher polling the mock through pass / fail / quit / retry -> live_attempts.jsonl."""

    def setUp(self):
        os.environ["OSU_COACH_TOSU_URL"] = mock_url()
        self.f = TMP / ATT_NAME
        os.environ["OSU_COACH_ATTEMPTS_FILE"] = str(self.f)
        try:
            self.f.unlink()
        except OSError:
            pass
        reset_module()
        pp.skill_model(refresh=True)       # so song-select predictions are quick (4 calcs) during the run
        self.states, self.attempts = [], []

    def tearDown(self):
        Mock.dynamic = None
        os.environ.pop("OSU_COACH_ATTEMPTS_FILE", None)

    def wait_n(self, n, timeout=8):
        return wait_for(lambda: len(self.attempts) >= n, timeout)

    def test_session(self):
        tl = Timeline(speed=20.0)          # 60 s map plays in ~3 s
        Mock.dynamic = tl
        w = pp.LiveWatcher(self.states.append, poll=0.05, debounce=0.05, on_attempt=self.attempts.append)
        w.start()
        try:
            time.sleep(0.4)
            # 1) pass
            sim = PlaySim(misses=(12000, 30000, 31000), drift=15)
            tl.set("play", sim, until=62000)
            time.sleep(3.4)
            tl.set("results", sim, results=sim.results(acc=94.2))
            self.assertTrue(self.wait_n(1))
            tl.set("select", sim)
            # 2) fail at 25 s, then back to song select
            sim2 = PlaySim(misses=tuple(range(15000, 25001, 1000)))
            time.sleep(0.3)
            tl.set("play", sim2, failed_at=25000)
            time.sleep(1.8)
            tl.set("select", sim2)
            self.assertTrue(self.wait_n(2))
            # 3) quick retry at 10 s, then 4) quit at ~20 s
            time.sleep(0.3)
            sim3 = PlaySim(misses=(5000,))
            tl.set("play", sim3)
            time.sleep(0.6)
            tl.set("play", sim3, live0=0)          # restart: clock back to 0, counters reset
            self.assertTrue(self.wait_n(3))
            time.sleep(0.9)
            tl.set("select", sim3)
            self.assertTrue(self.wait_n(4))
            # 5) someone else's play (spectating) is written with own=false but does not fire on_attempt
            time.sleep(0.3)
            tl.set("play", PlaySim(), player="other_guy")
            time.sleep(0.6)
            tl.set("select", sim3)
            time.sleep(0.6)
            self.assertEqual(len(self.attempts), 4)
            s = wait_for(lambda: self.states and self.states[-1].get("attempt_seq") == 5 and self.states[-1])
            self.assertTrue(s, self.states[-1] if self.states else None)
            self.assertEqual(s["last_attempt"]["own"], False)
        finally:
            w.stop()
        recs = pp.read_attempts(0)
        self.assertEqual([(r["outcome"], r["end"]) for r in recs],
                         [("passed", "results"), ("failed", "fail_quit"), ("quit", "retry"), ("quit", "quit"),
                          ("quit", "quit")])
        p, f, rq, q, other = recs
        self.assertEqual((p["misses"], p["acc"], p["progress"]), (3, 0.942, 1.0))
        for got, true in zip(sorted(p["miss_times_ms"]), (12000, 30000, 31000)):
            self.assertLessEqual(abs(got - true), 1100)            # 50 ms polls x 20x clock = 1 s of map time
        self.assertGreaterEqual(f["misses"], 10)
        self.assertAlmostEqual(f["progress"], 24000 / 60000, delta=0.02)
        self.assertTrue(f["failed_seen"])
        self.assertLess(rq["progress"], q["progress"] + 0.5)
        self.assertGreater(q["time_ms"], 8000)
        self.assertFalse(other["own"])
        self.assertTrue(all(r["md5"] == MAP_B["checksum"] for r in recs))
        self.assertEqual([a["ts"] for a in self.attempts], [r["ts"] for r in recs[:4]])

    def test_stop_mid_play_and_record_off(self):
        tl = Timeline(speed=5.0)
        Mock.dynamic = tl
        tl.set("play", PlaySim(misses=(2000,)))
        w = pp.LiveWatcher(self.states.append, poll=0.05, debounce=0.05, on_attempt=self.attempts.append)
        w.start()
        time.sleep(1.0)
        w.stop()
        recs = pp.read_attempts(0)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["end"], "stopped")
        # record=False: callback still fires, nothing written
        self.f.unlink()
        self.attempts.clear()
        tl.set("play", PlaySim())
        w = pp.LiveWatcher(self.states.append, poll=0.05, debounce=0.05, on_attempt=self.attempts.append,
                           record=False)
        w.start()
        time.sleep(0.6)
        tl.set("select", PlaySim())
        self.assertTrue(self.wait_n(1))
        w.stop()
        self.assertFalse(self.f.exists())

    def test_tosu_dies_mid_play(self):
        tl = Timeline(speed=5.0)
        Mock.dynamic = tl
        tl.set("play", PlaySim(misses=(1500,)))
        old = pp.LOST_GIVEUP_S
        pp.LOST_GIVEUP_S = 0.5
        w = pp.LiveWatcher(self.states.append, poll=0.05, slow_poll=0.2, debounce=0.05,
                           on_attempt=self.attempts.append)
        w.start()
        try:
            time.sleep(0.8)
            os.environ["OSU_COACH_TOSU_URL"] = f"http://127.0.0.1:{free_port()}"
            self.assertTrue(self.wait_n(1, timeout=5))
            self.assertEqual(self.attempts[0]["end"], "tosu_lost")
            s = wait_for(lambda: self.states[-1].get("attempt_seq") == 1 and self.states[-1])
            self.assertTrue(s)
            self.assertFalse(s["tosu_ok"])
        finally:
            pp.LOST_GIVEUP_S = old
            w.stop()


ATT_NAME = "live_attempts_test.jsonl"


class T9Hermetic(unittest.TestCase):
    def test_project_caches_untouched(self):
        for f in (".coach_sr_cache.json", ".coach_skill.json", "pp_live_log.txt", "live_attempts.jsonl"):
            p = HERE / f
            now = p.stat().st_mtime if p.exists() else None
            if now == PROJECT_MTIMES[f]:
                continue
            # the user's running Mochi may legitimately update these while the tests run (it did on
            # 2026-11-09 right after a live attempt). A changed file is only a test leak if it contains
            # the mock's data: the fake checksums, mock titles or the temp test folder.
            txt = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            for marker in (MAP_A["checksum"], MAP_B["checksum"], "TrySail", "ppl_test_", str(TMP)):
                self.assertNotIn(marker, txt, f"{f} was written by the tests ({marker!r})")


PROJECT_MTIMES = {}


def main():
    global SRV, TMP, pp
    if "--serve" in sys.argv:
        srv = start_mock(24051)
        print("mock tosu on http://127.0.0.1:24051 (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.shutdown()
        return 0
    for f in (".coach_sr_cache.json", ".coach_skill.json", "pp_live_log.txt", "live_attempts.jsonl"):
        p = HERE / f
        PROJECT_MTIMES[f] = p.stat().st_mtime if p.exists() else None
    SRV = start_mock()
    tmp = tempfile.TemporaryDirectory(prefix="ppl_test_")
    TMP = Path(tmp.name)
    os.environ["OSU_COACH_PP_DIR"] = str(TMP)
    os.environ["OSU_COACH_TOSU_URL"] = mock_url()
    write_fixture(TMP)
    sys.path.insert(0, str(HERE))
    import pp_live
    pp = pp_live
    prog = unittest.main(argv=[sys.argv[0], "-v"] + [a for a in sys.argv[1:]], exit=False)
    SRV.shutdown()
    try:
        tmp.cleanup()
    except Exception:
        pass
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
