#!/usr/bin/env python3
"""Tests for pp_recommend.py (stdlib unittest, hermetic: temp dirs, fake API / tosu / predictor / downloader).

    python test_pp_recommend.py

Nothing here talks to the osu! servers or tosu, opens a window, or touches the project's data files.
"""

import hashlib
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TMP_ROOT = Path(tempfile.mkdtemp(prefix="pp_rec_test_"))
os.environ["OSU_COACH_API_DIR"] = str(TMP_ROOT)       # osu_api import must not see the real config
(TMP_ROOT / "_default").mkdir()
os.environ["OSU_COACH_RECS_DIR"] = str(TMP_ROOT / "_default")   # pp_recommend_log.txt goes here, not the project
os.environ.pop("OSU_COACH_ATTEMPTS_FILE", None)

import pp_recommend as R  # noqa: E402

COMFORT = 4.6
NOW = 1_794_300_000.0


def osu_text(bid):
    return ("osu file format v14\r\n\r\n[General]\r\nMode: 0\r\n\r\n[Metadata]\r\nBeatmapID:%d\r\n" % bid).encode()


# ----------------------------------------------------------------------------
# fake world
# ----------------------------------------------------------------------------

class World:
    def __init__(self, d: Path, n_owned=160, n_search=60, seed=3):
        rng = random.Random(seed)
        self.d = d
        self.owned = {}      # md5 -> info
        self.online = {}     # bid -> api beatmap
        lib, osu_cache, api_sr = {}, {}, {}
        (d / "maps").mkdir(parents=True, exist_ok=True)
        for i in range(n_owned):
            bid, sid = 2_000_000 + i, 900_000 + i // 3
            md5 = hashlib.md5(b"own%d" % i).hexdigest()
            stars = round(rng.uniform(2.6, 6.2), 2)
            style = ["jump", "jump", "stream", "mixed"][i % 4]
            feats = {"mode": 0, "a": "Artist%d" % (i % 17), "t": "Song %d" % (i // 3), "v": "Diff %d" % (i % 3),
                     "c": "mapper%d" % (i % 9), "bid": bid, "sid": sid, "bpm": 180, "len": 90 + i % 60, "n": 450,
                     "ar": 9, "od": 8, "cs": 4, "hp": 5,
                     "jf": 0.5 if style == "jump" else 0.1, "sf": 0.2 if style == "stream" else 0.0,
                     "bf": 0.1}
            lib[md5] = feats
            p = d / "maps" / ("%s.osu" % md5)
            p.write_bytes(osu_text(bid))
            osu_cache[str(p)] = md5
            api_sr[md5] = stars
            status = "ranked"
            if i % 11 == 5:
                status = "loved"
            elif i % 13 == 7:
                status = "graveyard"
            checksum = md5 if i % 29 != 3 else hashlib.md5(b"newer%d" % i).hexdigest()   # outdated local copy
            self.owned[md5] = {"bid": bid, "sid": sid, "stars": stars, "status": status, "style": style,
                               "outdated": checksum != md5}
            self.online[bid] = self._bm(bid, sid, checksum, stars, status, feats)
        # one owned map the API doesn't know (deleted)
        self.search_bids = []
        for i in range(n_search):
            bid, sid = 3_000_000 + i, 950_000 + i // 2
            stars = round(rng.uniform(4.2, 5.5) if i % 3 else rng.uniform(3.0, 3.9), 2)
            checksum = hashlib.md5(osu_text(bid)).hexdigest()
            self.online[bid] = self._bm(bid, sid, checksum, stars, "ranked",
                                        {"a": "NetArtist%d" % i, "t": "Net Song %d" % (i // 2), "v": "X%d" % i,
                                         "c": "netmapper"})
            self.search_bids.append(bid)
        (d / ".coach_library.json").write_text(json.dumps({"v": 1, "maps": lib}), encoding="utf-8")
        (d / ".osu_coach_cache.json").write_text(json.dumps({"osu": osu_cache, "osr": {}}), encoding="utf-8")
        (d / ".coach_api_sr.json").write_text(json.dumps({"v": 1, "sr": api_sr, "miss": {}}), encoding="utf-8")
        # top 100: 40..103 pp, one of them on an owned map
        top = []
        for k in range(100):
            top.append({"beatmap_id": 1_000_000 + k, "md5": hashlib.md5(b"top%d" % k).hexdigest(),
                        "pp": round(42 + 61 * 0.955 ** k, 1), "stars": 4.5, "mods": "NM", "date": "2017-01-01"})
        self.top = top
        self.write_top(top)
        # live passes (history for "you average X% on similar maps")
        lines = []
        for j, (md5, o) in enumerate(list(self.owned.items())[:60]):
            acc = {"jump": 0.955, "stream": 0.89, "mixed": 0.93}[o["style"]]
            lines.append(json.dumps({"v": 1, "ts": NOW - 3600 * j, "md5": md5, "beatmap_id": o["bid"], "mods": "NM",
                                     "rate": 1.0, "stars": round(COMFORT + (j % 5 - 2) * 0.15, 2),
                                     "ranked": True, "outcome": "passed", "acc": acc, "misses": 2}))
        (d / "live_attempts.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_top(self, top):
        (self.d / "pp_data.json").write_text(json.dumps({"user": {"pp": 2001.5, "global_rank": 500000},
                                                         "top_plays": top, "climb": []}), encoding="utf-8")

    @staticmethod
    def _bm(bid, sid, checksum, stars, status, f):
        return {"id": bid, "beatmapset_id": sid, "checksum": checksum, "difficulty_rating": stars,
                "status": status, "mode": "osu", "mode_int": 0, "version": f.get("v"), "bpm": 180, "ar": 9,
                "accuracy": 8, "cs": 4, "hit_length": 100, "count_circles": 300, "count_sliders": 140,
                "count_spinners": 1, "beatmapset": {"id": sid, "artist": f.get("a"), "title": f.get("t"),
                                                    "creator": f.get("c")}}


class FakeApi:
    def __init__(self, world):
        self.w = world
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/beatmaps":
            ids = params["ids[]"]
            assert len(ids) <= 50
            return {"beatmaps": [self.w.online[i] for i in ids if i in self.w.online]}
        if path == "/beatmaps/lookup":
            return {}
        if path == "/beatmapsets/search":
            sets = {}
            for bid in self.w.search_bids:
                bm = self.w.online[bid]
                sets.setdefault(bm["beatmapset_id"], dict(bm["beatmapset"], beatmaps=[]))["beatmaps"].append(
                    {k: v for k, v in bm.items() if k != "beatmapset"})
            return {"beatmapsets": list(sets.values()), "cursor_string": None}
        raise AssertionError(path)


def fake_calc_factory(world, log=None):
    by_path = {}
    for md5, o in world.owned.items():
        by_path[str(world.d / "maps" / ("%s.osu" % md5))] = o["stars"]
    for bid in world.search_bids:
        by_path[str(world.d / R.OSU_DIR_NAME / ("%d.osu" % bid))] = world.online[bid]["difficulty_rating"]

    def calc(path, mods, rate, acc=None, misses=None, combo=None):
        if log is not None:
            log.append((path, mods, rate))
        nm = by_path.get(str(path))
        if nm is None:
            return None
        s = R.rough_stars(nm, mods, rate)
        a = 1.0 if acc is None else (acc / 100 if acc > 1 else acc)
        pp = 1.1 * s ** 3 * a ** 5 * 0.97 ** (misses or 0) * (1.04 if "HD" in mods else 1.0)
        return {"pp": pp, "stars": s, "aim": s * 0.5, "speed": s * 0.4, "max_combo": 600, "n_objects": 441,
                "ar": 9, "od": 8, "cs": 4, "bpm": 180 * rate}
    return calc


class StubPredictor:
    """Deterministic stand-in for pp_model (contract API)."""
    name = "stub"

    def __init__(self, calc, sig="s1"):
        self.calc, self.sig, self.calls, self.outcome_calls = calc, sig, 0, 0

    def report(self):
        return {"trained_on": 300, "updated": self.sig, "learner": "stub", "comfort_stars": COMFORT,
                "backtest": {"pp_mae": 6.0, "n": 100}, "recent": {"n": 20, "pp_mae": 5.5}}

    def signature(self):
        return self.sig

    def predict_outcome(self, md5, mods, rate, osu_path=None):
        self.outcome_calls += 1
        ss = self.calc(osu_path, mods, rate)
        p = 1 / (1 + math.exp((ss["stars"] - COMFORT - 0.3) / 0.25))
        return {"p_pass": p, "acc": {"p20": 0.93, "p50": 0.95, "p80": 0.965}, "misses": {"p20": 1, "p50": 3,
                                                                                       "p80": 8},
                "n_similar": 17, "confidence": "medium", "basis": "stub"}

    def predict_pp(self, md5, mods, rate, osu_path=None):
        self.calls += 1
        ss = self.calc(osu_path, mods, rate)
        if not ss:
            return None
        s = ss["stars"]
        p = 1 / (1 + math.exp((s - COMFORT - 0.3) / 0.25))
        if "HR" in mods:
            p *= 0.8
        mid = ss["pp"] * 0.95 ** 5 * 0.97 ** 3
        return {"pp_expected": mid, "pp_low": mid * 0.8, "pp_high": mid * 1.15, "pp_fc": mid * 1.2,
                "pp_ss": ss["pp"], "p_pass": p, "stars": s, "gain_expected": 0, "gain_high": 0,
                "confidence": "medium", "basis": "stub", "model_error_pp": 6.0}


class FakeFetcher:
    def __init__(self, world, bad=()):
        self.w, self.bad, self.calls = world, set(bad), []

    def __call__(self, bid):
        self.calls.append(bid)
        if bid in self.bad:
            return b"<html>not found</html>"
        return osu_text(bid)


class FakeClock:
    def __init__(self, step=0.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


FAST = R.Throttle(rate=1e9)


def make(d, world, mode="full", pred=None, api=True, fetcher=None, clock=None, deadline=None, calc=None):
    calc = calc or fake_calc_factory(world)
    pred = pred or StubPredictor(calc)
    return R.Recommender(d=d, mode=mode, api=R.ApiSource(FakeApi(world)) if api is True else api, predictor=pred,
                         calc=calc, fetcher=fetcher if fetcher is not None else FakeFetcher(world),
                         clock=clock or time.monotonic, now=NOW, deadline_s=deadline, throttle=FAST)


def tmpdir():
    return Path(tempfile.mkdtemp(dir=TMP_ROOT))


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------

class TestMath(unittest.TestCase):
    def test_weighted_gain_matches_osu_api_and_pp_live(self):
        import osu_api
        import pp_live
        rng = random.Random(1)
        top = [{"beatmap_id": i, "pp": round(rng.uniform(40, 103), 1)} for i in range(100)]
        pps = [t["pp"] for t in top]
        for new in (30, 41, 55.5, 80, 120):
            self.assertAlmostEqual(R.weighted_gain(pps, new), osu_api.weighted_gain(pps, new), places=6)
            self.assertAlmostEqual(R.weighted_gain(pps, new), pp_live.rank_gain_pp(new, top) or 0, places=1)
        ex = top[10]
        self.assertEqual(R.weighted_gain(pps, ex["pp"] - 1, ex["pp"]), 0.0)          # worse than own score
        g = R.weighted_gain(pps, ex["pp"] + 10, ex["pp"])
        self.assertAlmostEqual(g, pp_live.rank_gain_pp(ex["pp"] + 10, top, beatmap_id=ex["beatmap_id"]), places=1)
        self.assertLess(g, R.weighted_gain(pps, ex["pp"] + 10))                       # replacing < adding
        self.assertEqual(R.weighted_gain(pps, 10), 0.0)                               # below the top-100 floor

    def test_throttle_paces(self):
        clk = FakeClock()
        slept = []

        def sleep(s):
            slept.append(s)
            clk.t += s
        t = R.Throttle(rate=5, clock=lambda: clk.t, sleep=sleep)
        for _ in range(11):
            t.wait()
        self.assertAlmostEqual(sum(slept), 2.0, places=6)

    def test_tosu_patch_only_slows_own_thread(self):
        import pp_live
        orig = pp_live._http_json
        seen = []
        pp_live._http_json = lambda *a, **kw: seen.append(threading.get_ident()) or {"ok": 1}
        base = pp_live._http_json
        try:
            th = R.Throttle(rate=1e9)
            with R._TosuThrottle(th):
                pp_live._http_json("/x")
                other = threading.Thread(target=lambda: pp_live._http_json("/y"))
                other.start()
                other.join()
                self.assertEqual(th.count, 1)
            self.assertIs(pp_live._http_json, base)
            self.assertEqual(len(seen), 2)
        finally:
            pp_live._http_json = orig


class TestFullRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tmpdir()
        cls.w = World(cls.d)
        cls.fetch = FakeFetcher(cls.w, bad={3_000_004})
        cls.calc = fake_calc_factory(cls.w)
        cls.pred = StubPredictor(cls.calc)
        cls.api = FakeApi(cls.w)
        t0 = time.monotonic()
        cls.out = R.Recommender(d=cls.d, mode="full", api=R.ApiSource(cls.api), predictor=cls.pred, calc=cls.calc,
                                fetcher=cls.fetch, now=NOW, throttle=FAST).run()
        cls.took = time.monotonic() - t0
        cls.picks = cls.out["picks"]

    def test_file_written_and_shape(self):
        on_disk = json.loads((self.d / R.OUT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["picks"], self.picks)
        self.assertGreaterEqual(len(self.picks), 10)
        self.assertEqual(self.out["total_pp"], 2001.5)
        self.assertEqual(self.out["rank"], 500000)
        self.assertFalse(self.out["partial"], self.out["notes"])
        for k in ("md5", "beatmap_id", "set_id", "artist", "title", "version", "mapper", "owned", "status", "mods",
                  "rate", "stars", "length_s", "bpm", "ar", "od", "cs", "pp_expected", "pp_low", "pp_high", "pp_fc",
                  "p_pass", "gain_expected", "gain_high", "confidence", "why", "search", "smash"):
            self.assertIn(k, self.picks[0], k)
        self.assertEqual(self.out["model"]["learner"], "stub")
        self.assertLess(self.took, 20)

    def test_sorted_by_expected_gain_mostly(self):
        g = [p["gain_expected"] for p in self.picks]
        # diversity may swap neighbours, but the head of the list is the best gain overall; stretch picks
        # (< 25% pass chance) come after the passable ones
        st = [p["stretch"] for p in self.picks]
        self.assertEqual(st, sorted(st))
        ok = [p["gain_expected"] for p in self.picks if not p["stretch"]]
        if ok:
            self.assertEqual(g[0], max(ok))
        self.assertTrue(all(p["gain_high"] >= p["gain_expected"] - 1e-9 for p in self.picks))

    def test_expected_gain_math(self):
        top_pps = [t["pp"] for t in self.w.top]
        tail = R.tail_pp(self.out["total_pp"], top_pps)
        for p in self.picks[:5]:
            # no pp_model here: E[gain | pass] over a split-lognormal through pp_low / pp_expected / pp_high
            g = R.dist_gain(top_pps, p["pp_low"], p["pp_expected"], p["pp_high"], p.get("existing_pp"), tail)
            self.assertAlmostEqual(p["gain_expected"], p["p_pass"] * g, delta=0.05 + 0.005 * g)   # pp rounded to 0.1
            self.assertAlmostEqual(p["gain_if_pass"], g, delta=0.05 + 0.005 * g)
            # ... which is close to (and, the gain being convex above the floor, usually above) a 3-point mean
            g3 = [R.weighted_gain(top_pps, x, p.get("existing_pp"), tail) for x in (p["pp_low"], p["pp_expected"],
                                                                                     p["pp_high"])]
            self.assertLess(abs(g - sum(g3) / 3), 0.25 * max(1.0, g))

    def test_tail_correction(self):
        # entering a full top 100 keeps the old #100 at 0.95^100 and shifts the scores below by 5%
        pps = [100.0 - 0.5 * i for i in range(100)]
        base = R.weighted_gain(pps, 80.0)
        self.assertAlmostEqual(R.weighted_gain(pps, 80.0, tail=0.0) - base, pps[99] * 0.95 ** 100, places=6)
        self.assertAlmostEqual(R.weighted_gain(pps, 80.0, tail=2.0) - base, pps[99] * 0.95 ** 100 - 0.1, places=6)
        self.assertEqual(R.weighted_gain(pps, 40.0, tail=2.0), 0.0)                 # below #100: nothing
        import pp_live
        top = [{"beatmap_id": i, "pp": p} for i, p in enumerate(pps)]
        tot = R.weighted_total(pps) + 416.6667 * (1 - 0.995 ** 1000) + 2.0
        self.assertAlmostEqual(R.tail_pp(tot, pps), 2.0, places=3)
        self.assertAlmostEqual(pp_live.rank_gain_pp(80.0, top, total_pp=tot),
                               round(R.weighted_gain(pps, 80.0, tail=2.0), 2), places=2)

    def test_same_song_once(self):
        rows = [{"md5": "a", "set_id": 1, "owned": True, "gain_expected": 3.0, "title": "Harumachi Clover (Swing)"},
                {"md5": "b", "set_id": 2, "owned": True, "gain_expected": 2.9, "title": "harumachi clover (swing)"},
                {"md5": "c", "set_id": 1, "owned": True, "gain_expected": 2.8, "title": "Harumachi Clover (Swing)"},
                {"md5": "d", "set_id": 3, "owned": True, "gain_expected": 2.0, "title": "Other"}]
        out = R.diversify(rows, 10)
        self.assertEqual([r["md5"] for r in out], ["a", "d"])

    def test_only_ranked_and_current(self):
        for p in self.picks:
            self.assertIn(p["status"], R.GAIN_STATUSES)
            o = self.w.owned.get(p["md5"])
            if o:
                self.assertEqual(o["status"], "ranked")
                self.assertFalse(o["outdated"])

    def test_diversity(self):
        per_set, per_md5 = {}, {}
        for p in self.picks:
            per_set[p["set_id"]] = per_set.get(p["set_id"], 0) + 1
            per_md5[p["md5"]] = per_md5.get(p["md5"], 0) + 1
        self.assertLessEqual(max(per_set.values()), 2)
        self.assertEqual(max(per_md5.values()), 1)
        kinds = {p["owned"] for p in self.picks}
        self.assertEqual(kinds, {True, False})
        own = sum(1 for p in self.picks if p["owned"])
        self.assertLessEqual(own, math.ceil(R.OWN_SHARE_MAX * len(self.picks)) + 1)

    def test_mod_variants_considered(self):
        self.assertTrue(any(p.get("alt_mods") for p in self.picks))
        mods = {p["mods"] for p in self.picks} | {a["mods"] for p in self.picks for a in p["alt_mods"]}
        self.assertTrue({"HD", "NM"} <= mods, mods)
        self.assertTrue(mods & {"DT", "HDDT"}, mods)

    def test_smash_flag(self):
        top = max(p["gain_expected"] for p in self.picks)
        for p in self.picks:
            want = p["p_pass"] >= R.SMASH_P and p["gain_expected"] >= max(R.SMASH_MIN_GAIN, R.SMASH_REL * top)
            self.assertEqual(p["smash"], want)
        sm = self.out["smash_picks"]
        self.assertGreaterEqual(len(sm), 3)       # listed although riskier maps / mods gain more
        self.assertLessEqual(len(sm), R.SMASH_KEEP)
        self.assertTrue(all(p["smash"] and p["p_pass"] >= R.SMASH_P and p["why"] for p in sm))
        self.assertEqual(len({p["md5"] for p in sm}), len(sm))
        g = [p["gain_expected"] for p in sm]
        self.assertEqual(g, sorted(g, reverse=True))

    def test_explanations_grounded(self):
        joined = " ".join(p["why"] for p in self.picks)
        self.assertIn("you average", joined)
        self.assertIn("pass chance", joined)
        self.assertIn("17 similar plays", joined)
        jp = [p for p in self.picks if p["owned"] and "Jump-heavy" in p["why"]]
        self.assertTrue(jp)
        self.assertIn("strongest pattern", jp[0]["why"])
        self.assertTrue(any("not downloaded" in p["why"] for p in self.picks if not p["owned"]))
        self.assertLessEqual(self.pred.outcome_calls, R.N_PICKS + R.SMASH_KEEP)

    def test_downloads(self):
        files = list((self.d / R.OSU_DIR_NAME).glob("*.osu"))
        self.assertTrue(files)
        self.assertLessEqual(len(self.fetch.calls), R.FETCH_PER_RUN)
        self.assertNotIn(3_000_004, [p["beatmap_id"] for p in self.picks])   # invalid download -> not used
        for p in self.picks:
            if not p["owned"]:
                self.assertTrue((self.d / R.OSU_DIR_NAME / ("%d.osu" % p["beatmap_id"])).is_file())
        st = json.loads((self.d / R.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(st["fetch"]["n"], len(self.fetch.calls))

    def test_api_usage(self):
        self.assertLessEqual(len(self.api.calls), R.API_BUDGET)
        self.assertIn("/beatmapsets/search", self.api.calls)
        self.assertIn("/beatmaps", self.api.calls)

    def test_tip(self):
        self.assertIsInstance(self.out["by_mods_tip"], str)
        self.assertIn("HD adds", self.out["by_mods_tip"])


class TestBehaviour(unittest.TestCase):
    def test_existing_score_beating_pp_high_excluded_and_replacement(self):
        d = tmpdir()
        w = World(d, n_owned=60, n_search=0)
        out1 = make(d, w).run()
        p = out1["picks"][0]
        # give the player a score on that exact map above the model's high outcome -> must disappear
        top = [dict(t) for t in w.top]
        top[50] = {"beatmap_id": p["beatmap_id"], "md5": p["md5"], "pp": p["pp_high"] + 1, "mods": "NM"}
        w.write_top(top)
        out2 = make(d, w, mode="incremental", api=None).run()
        # that variant must disappear; another mod of the map may stay only if it can still beat the score
        for x in out2["picks"]:
            if x["md5"] == p["md5"]:
                self.assertNotEqual(x["mods"], p["mods"])
                self.assertGreater(x["pp_high"], p["pp_high"] + 1)
        # a score below pp_expected -> still recommended, gain counted as a replacement
        top[50]["pp"] = p["pp_expected"] - 8
        w.write_top(top)
        out3 = make(d, w, mode="incremental", api=None).run()
        q = [x for x in out3["picks"] if x["md5"] == p["md5"]]
        if not q:
            # one pick per song: with its gain cut by the replacement, another diff of the song may take the slot
            sib = [x for x in out3["picks"] if R.title_key(x) == R.title_key(p)]
            self.assertTrue(sib)
            self.assertGreaterEqual(sib[0]["gain_expected"], 0)
            return
        self.assertEqual(q[0]["existing_pp"], round(p["pp_expected"] - 8, 1))
        self.assertIn("beats your", q[0]["why"])
        self.assertLess(q[0]["gain_expected"], p["gain_expected"])

    def test_unknown_status_never_recommended_without_api(self):
        d = tmpdir()
        w = World(d, n_owned=40, n_search=0)
        (d / "live_attempts.jsonl").write_text("", encoding="utf-8")
        out = make(d, w, api=None).run()
        self.assertEqual(out["picks"], [])
        self.assertGreater(out["counts"]["unknown_status"], 0)
        self.assertTrue(any("API" in n for n in out["notes"]))

    def test_live_attempt_ranked_flag_counts_as_status(self):
        d = tmpdir()
        w = World(d, n_owned=40, n_search=0)
        out = make(d, w, api=None).run()
        live_md5 = {json.loads(x)["md5"] for x in (d / "live_attempts.jsonl").read_text().splitlines() if x}
        self.assertTrue(out["picks"])
        self.assertTrue(all(p["md5"] in live_md5 for p in out["picks"]))

    def test_resumable_with_deadline(self):
        d = tmpdir()
        w = World(d, n_owned=200, n_search=40)
        calc_log = []
        calc = fake_calc_factory(w, calc_log)
        clock = FakeClock(step=0.1)           # every clock read costs 100 ms of fake time
        r1 = make(d, w, clock=clock, deadline=12, calc=calc).run()
        self.assertTrue(r1["partial"])
        n1 = len(json.loads((d / R.CACHE_NAME).read_text())["attrs_custom"])
        for _ in range(15):
            out = make(d, w, clock=FakeClock(step=0.1), deadline=12, calc=calc).run()
            if not out["partial"]:
                break
        n2 = len(json.loads((d / R.CACHE_NAME).read_text())["attrs_custom"])
        self.assertGreater(n2, n1)
        self.assertFalse(out["partial"], out["notes"])
        self.assertTrue(out["picks"])
        # a later full run with everything cached makes (almost) no new tosu calls
        calc_log.clear()
        make(d, w, calc=calc).run()
        self.assertLess(len(calc_log), 5 + 3 * R.N_PICKS)

    def test_incremental_no_network_and_repredicts_after_model_change(self):
        d = tmpdir()
        w = World(d, n_owned=120, n_search=30)
        calc = fake_calc_factory(w)
        p1 = StubPredictor(calc, sig="a")
        make(d, w, pred=p1, calc=calc).run()
        api = FakeApi(w)
        fetch = FakeFetcher(w)
        p2 = StubPredictor(calc, sig="b")
        t0 = time.monotonic()
        out = make(d, w, mode="incremental", pred=p2, api=R.ApiSource(api), fetcher=fetch, calc=calc).run()
        self.assertLess(time.monotonic() - t0, R.INC_DEADLINE_S)
        self.assertEqual(api.calls, [])
        self.assertEqual(fetch.calls, [])
        self.assertGreater(p2.calls, 0)
        self.assertLessEqual(p2.calls, R.PRED_MAX_INC)
        self.assertEqual(out["mode"], "incremental")
        # the previous picks were re-predicted first
        cache = json.loads((d / R.CACHE_NAME).read_text())["preds"]
        for p in out["picks"][:10]:
            self.assertEqual(cache[R.vkey(p["md5"], p["mods"], p["rate"])]["sig"], "b|custom")
        # same model again: later incremental runs work through the rest, then have nothing left to do
        calls = []
        for _ in range(12):
            p3 = StubPredictor(calc, sig="b")
            make(d, w, mode="incremental", pred=p3, api=None, calc=calc).run()
            calls.append(p3.calls)
            if p3.calls == 0:
                break
        self.assertEqual(calls[-1], 0, calls)

    def test_auto_mode(self):
        d = tmpdir()
        w = World(d, n_owned=30, n_search=0)
        r = make(d, w, mode="auto")
        self.assertEqual(r.mode, "full")
        r.run()
        self.assertEqual(make(d, w, mode="auto").mode, "incremental")

    def test_daily_fetch_cap(self):
        d = tmpdir()
        w = World(d, n_owned=10, n_search=60)
        st = {"v": 1, "bid": {}, "md5": {}, "search": {}, "fetch": {"day": time.strftime(
            "%Y-%m-%d", time.localtime(NOW)), "n": R.FETCH_PER_DAY - 3}}
        (d / R.STATUS_NAME).write_text(json.dumps(st), encoding="utf-8")
        fetch = FakeFetcher(w)
        make(d, w, fetcher=fetch).run()
        self.assertEqual(len(fetch.calls), 3)

    def test_fallback_without_pp_model(self):
        d = tmpdir()
        w = World(d, n_owned=60, n_search=0)
        calc = fake_calc_factory(w)
        saved = sys.modules.get("pp_model")
        sys.modules["pp_model"] = None            # import pp_model -> ImportError
        try:
            r = R.Recommender(d=d, mode="full", api=R.ApiSource(FakeApi(w)), predictor=None, calc=calc,
                              fetcher=FakeFetcher(w), now=NOW, throttle=FAST)
            out = r.run()
        finally:
            if saved is None:
                sys.modules.pop("pp_model", None)
            else:
                sys.modules["pp_model"] = saved
        self.assertEqual(out["predictor"], "fallback")
        self.assertTrue(out["picks"])
        self.assertTrue(all(p["confidence"] == "low" for p in out["picks"]))

    def test_no_tosu_uses_cache_only(self):
        d = tmpdir()
        w = World(d, n_owned=40, n_search=0)
        out = R.Recommender(d=d, mode="full", api=R.ApiSource(FakeApi(w)), predictor=StubPredictor(
            fake_calc_factory(w)), calc=None, fetcher=FakeFetcher(w), now=NOW, throttle=FAST).run()
        self.assertTrue(any("tosu" in n for n in out["notes"]))

    def test_diversify_unit(self):
        rows = [{"md5": str(i), "set_id": i // 4, "owned": True, "gain_expected": 10 - i * 0.1} for i in range(20)]
        rows += [{"md5": "n%d" % i, "set_id": 100 + i, "owned": False, "gain_expected": 6 - i * 0.1}
                 for i in range(5)]
        out = R.diversify(sorted(rows, key=lambda r: -r["gain_expected"]), 10)
        sets = [r["set_id"] for r in out]
        self.assertTrue(all(sets.count(s) <= 2 for s in sets))
        self.assertGreaterEqual(sum(1 for r in out if not r["owned"]), 3)


class TestModelAdapter(unittest.TestCase):
    def test_kwargs_filtered_and_untrained_falls_back(self):
        import types
        seen = {}
        mod = types.ModuleType("fake_pp_model")

        def predict_pp(md5, mods="NM", rate=None, osu_path=None, beatmap_id=None, network=True):
            seen.update(md5=md5, mods=mods, osu_path=osu_path, beatmap_id=beatmap_id, network=network)
            return {"pp_expected": 50}
        mod.predict_pp = predict_pp
        mod.model_report = lambda: {"trained_on": 0, "updated": None}
        mp = R.ModelPredictor(mod)
        self.assertEqual(mp.predict_pp("m", "HD", 1.0, osu_path="p", beatmap_id=7, network=False,
                                       top_plays=[1])["pp_expected"], 50)
        self.assertEqual(seen, {"md5": "m", "mods": "HD", "osu_path": "p", "beatmap_id": 7, "network": False})
        self.assertFalse(mp.trained())
        d = tmpdir()
        w = World(d, n_owned=40, n_search=0)
        saved = sys.modules.get("pp_model")
        sys.modules["pp_model"] = mod
        try:
            out = R.Recommender(d=d, mode="full", api=R.ApiSource(FakeApi(w)), predictor=None,
                                calc=fake_calc_factory(w), fetcher=None, now=NOW, throttle=FAST).run()
        finally:
            if saved is None:
                sys.modules.pop("pp_model", None)
            else:
                sys.modules["pp_model"] = saved
        self.assertEqual(out["predictor"], "fallback")
        self.assertTrue(any("not trained" in n for n in out["notes"]))


class FakeAttrApi(FakeApi):
    """FakeApi + POST /beatmaps/{id}/attributes with attributes derived from the world's star ratings."""

    def __init__(self, world):
        super().__init__(world)
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        bid = int(path.split("/")[2])
        bm = self.w.online[bid]
        mods = [m["acronym"] for m in body["mods"]]
        s = R.rough_stars(bm["difficulty_rating"], mods, 1.5 if "DT" in mods else 1.0)
        return {"attributes": {"star_rating": s, "max_combo": 600, "aim_difficulty": s * 0.56,
                               "speed_difficulty": s * 0.42, "speed_note_count": 200.0,
                               "aim_difficult_strain_count": 90.0, "speed_difficult_strain_count": 80.0,
                               "slider_factor": 0.97, "aim_difficult_slider_count": 40.0}}


# captured from tosu v4.26.3 /api/calculate/pp (lazer=true) for one map: 105 circles, 35 sliders, 1 spinner,
# AR9 OD8; difficulty attributes from the same response
TOSU_ATTRS = {"aim_difficulty": 2.5967810919085608, "speed_difficulty": 1.7825714269606447,
              "speed_note_count": 101.43842202598788, "aim_difficult_strain_count": 47.71487447833467,
              "speed_difficult_strain_count": 51.18367980571999, "max_combo": 181, "slider_factor": 0.978}
TOSU_PP = [("NM", 1.0, 0, None, 136.398), ("NM", 0.97, 0, None, 105.915), ("NM", 0.94, 0, None, 91.197),
           ("NM", 0.94, 3, 90, 73.953), ("NM", 0.90, 8, 40, 50.445), ("HD", 1.0, 0, None, 151.166),
           ("HD", 0.94, 3, 90, 82.708)]


class TestApiCalc(unittest.TestCase):
    def test_formula_matches_tosu(self):
        for mods, acc, miss, combo, want in TOSU_PP:
            got = R.pp_formula(TOSU_ATTRS, 105, 35, 1, 9.0, 8.0, mods, acc, miss, combo)["pp"]
            self.assertAlmostEqual(got / want, 1.0, delta=0.045, msg="%s %s %s" % (mods, acc, miss))
        ss = R.pp_formula(TOSU_ATTRS, 105, 35, 1, 9.0, 8.0, "NM")["pp"]
        self.assertAlmostEqual(ss, 136.398, delta=0.7)

    def test_mod_adjustments(self):
        ar, od = R.adj_ar_od(9.0, 8.0, "HR", 1.0)
        self.assertEqual((ar, od), (10.0, 10.0))
        ar, od = R.adj_ar_od(9.0, 8.0, "DT", 1.5)
        self.assertAlmostEqual(ar, 10.33, places=1)
        self.assertAlmostEqual(od, 9.78, places=1)
        n300, n100, n50 = R.hit_counts(500, 0.95, 3)
        self.assertAlmostEqual((300 * n300 + 100 * n100 + 50 * n50) / (300 * 500), 0.95, places=6)

    def test_api_calc_and_calibration(self):
        d = tmpdir()
        w = World(d, n_owned=5, n_search=0)
        api = FakeAttrApi(w)
        rec = R.Recommender(d=d, mode="full", api=R.ApiSource(api), predictor=StubPredictor(lambda *a, **k: None),
                            calc="api", fetcher=None, now=NOW, throttle=FAST)
        ac = rec.api_calc
        bid = next(iter(w.owned.values()))["bid"]
        rec.status["bid"][str(bid)] = R.status_entry(w.online[bid], NOW)
        r1 = ac("bid:%d" % bid, "NM", 1.0)
        r2 = ac("bid:%d" % bid, "HD", 1.0, acc=95, misses=2, combo=300)
        self.assertGreater(r1["pp"], r2["pp"] * 0.9)
        self.assertEqual(len(api.posts), 1)            # HD reuses the NM attributes (+ formula HD bonus)
        self.assertGreater(ac("bid:%d" % bid, "HD", 1.0)["pp"], r1["pp"])
        ac("bid:%d" % bid, "HR", 1.0, acc=97)
        self.assertEqual(len(api.posts), 2)
        self.assertEqual(api.posts[1][1]["mods"], [{"acronym": "HR"}])
        ac("bid:%d" % bid, "HDHR", 1.0, acc=97)        # cached
        self.assertEqual(len(api.posts), 2)
        # calibration: top plays scored at 1.07x the formula -> k ~ 1.07
        top = []
        for o in list(w.owned.values())[:5]:
            rec.status["bid"][str(o["bid"])] = R.status_entry(w.online[o["bid"]], NOW)
            raw, _, _ = ac.raw(o["bid"], "NM", 1.0, 0.97, 0, None, classic=True)
            top.append({"beatmap_id": o["bid"], "pp": raw["pp"] * 1.07, "acc": 0.97, "misses": 0, "mods": "NM"})
        k = ac.calibrate(top)
        self.assertAlmostEqual(k, 1.07, places=3)
        self.assertAlmostEqual(ac("bid:%d" % bid, "NM", 1.0)["pp"], r1["pp"] * 1.07, places=3)

    def test_broken_tosu_detected_and_api_used(self):
        d = tmpdir()
        w = World(d, n_owned=80, n_search=20)

        def stuck_calc(path, mods, rate, acc=None, misses=None, combo=None):   # always the selected map
            return {"pp": 136.4, "stars": 4.665, "max_combo": 181, "n_objects": 141}
        api = FakeAttrApi(w)
        rec = R.Recommender(d=d, mode="full", api=R.ApiSource(api), predictor=None, calc=stuck_calc,
                            fetcher=FakeFetcher(w), now=NOW, throttle=FAST)
        rec.calc_kind = "tosu"                         # as if default_calc() had found tosu
        saved = sys.modules.get("pp_model")
        sys.modules["pp_model"] = None
        try:
            out = rec.run()
        finally:
            if saved is None:
                sys.modules.pop("pp_model", None)
            else:
                sys.modules["pp_model"] = saved
        self.assertEqual(out["pp_calc"]["kind"], "api")
        self.assertFalse(out["pp_calc"]["tosu_check"]["ok"])
        self.assertTrue(any("ignores ?path=" in n for n in out["notes"]))
        self.assertTrue(api.posts)
        self.assertTrue(out["picks"])
        pps = {p["pp_ss"] for p in out["picks"]}
        self.assertGreater(len(pps), 3)                # different maps -> different pp (not the stuck 136.4)
        self.assertTrue(any(not p["owned"] for p in out["picks"]))

    def test_model_numbers_for_wrong_map_rejected(self):
        d = tmpdir()
        w = World(d, n_owned=60, n_search=0)
        calc = fake_calc_factory(w)

        class WrongMap(StubPredictor):
            def predict_pp(self, md5, mods, rate, osu_path=None):
                self.calls += 1
                return {"pp_expected": 99.0, "pp_low": 80.0, "pp_high": 120.0, "pp_ss": 136.4, "stars": 4.665,
                        "p_pass": 0.7, "confidence": "medium"}
        rec = make(d, w, pred=WrongMap(calc), calc=calc)
        rec.calc_kind = "tosu"
        rec.cache["tosu_check"] = {"ok": True, "ts": NOW}
        out = rec.run()
        self.assertGreater(out["counts"]["preds_rejected"], 0)
        self.assertTrue(out["picks"])
        for p in out["picks"]:
            ss = calc(str(d / "maps" / ("%s.osu" % p["md5"])), R.CORE[p["mods"]], p["rate"])["pp"]
            if p["pp_expected"] == 99.0:            # kept only where the model's map really matches
                self.assertLessEqual(abs(136.4 / (ss * (1.06 if "HD" in p["mods"] else 1)) - 1), 0.3)
            else:
                self.assertAlmostEqual(p["pp_ss"], ss * (1.04 if "HD" in p["mods"] else 1), delta=0.2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
