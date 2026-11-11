#!/usr/bin/env python3
"""Tests for pp_model.py (CONTRACT_PP2.md) and its pp_live integration.  Hermetic: every file goes to a temp
folder (OSU_COACH_PP_DIR / OSU_COACH_ATTEMPTS_FILE), the osu! API is stubbed out, no tosu, no GUI.

    python test_pp_model.py          (runs at below-normal priority)
"""

import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import ctypes
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="pp_model_test_"))
os.environ["OSU_COACH_PP_DIR"] = str(TMP)
os.environ["OSU_COACH_ATTEMPTS_FILE"] = str(TMP / "live_attempts.jsonl")
os.environ["OSU_COACH_TOSU_URL"] = "http://127.0.0.1:9"      # nothing listens: tosu "down"
sys.path.insert(0, str(HERE))

import pp_model as pm  # noqa: E402
import pp_live as pl   # noqa: E402

pm._api = lambda: None    # never the network in tests
pm.UID = 12345            # the fixture player (never the real config's)
DAY = 86400.0
NOW = time.time()


def mk_attrs(stars, n=400, od=8.0, ar=9.0, diff=(), rate=1.0, ns=None):
    """Synthetic but formula-consistent attribute dict."""
    nS = ns if ns is not None else n // 3
    nC = n - nS - 1
    m = {"od": od, "ar": ar, "cs": 4.0, "hp": 5.0}
    e = pm.eff_stats(m, list(diff), rate)
    aim = 0.0675 * (stars / 2.0) * 1.0
    return {"src": "api", "stars": stars, "aim": stars * 0.55, "speed": stars * 0.4, "sf": 0.95,
            "adsc": nS * 0.5, "snc": n * 0.3, "adst": 60.0, "sdst": 45.0, "max_combo": nC + 2 * nS + 1 + 20,
            "nC": nC, "nS": nS, "nSp": 1, "nLT": 20, **e}


def stats_for(A, acc_hit, misses, combo_frac, tails_drop=0.0):
    n = A["nC"] + A["nS"] + A["nSp"]
    H = n - misses
    k = 6 * H * (1 - acc_hit) / 4.1
    meh = int(round(k * 0.1))
    ok = int(round(k - meh))
    g = H - ok - meh
    tails = int(round(A["nS"] * (1 - tails_drop)))
    return {"great": g, "ok": ok, "meh": meh, "miss": misses, "combo": max(1, int(A["max_combo"] * combo_frac)),
            "acc": pm.lazer_acc(g, ok, meh, misses, tails, A["nS"], A["nLT"], A["nLT"]), "tails": tails}


class T1Formula(unittest.TestCase):
    def test_hit_windows_match_lazer(self):
        e = pm.eff_stats({"od": 7, "ar": 8}, [], 1.0)
        self.assertAlmostEqual(e["gw"], 37.5)
        self.assertAlmostEqual(e["okw"], 83.5)
        self.assertAlmostEqual(e["mw"], 129.5)
        e = pm.eff_stats({"od": 8, "ar": 9}, ["HR"], 1.0)
        self.assertAlmostEqual(e["ar"], 10.0)
        dt = pm.eff_stats({"od": 8, "ar": 9}, ["DT"], 1.5)
        self.assertAlmostEqual(dt["ar"], 10.33, places=2)
        self.assertGreater(dt["od"], 8)

    def test_lazer_accuracy_formula(self):
        # Black Rover (TV Size) [Shunao's Hard], player1 2026-11-09: server accuracy 0.879242
        acc = pm.lazer_acc(183, 44, 6, 3, 150, 151, 18, 18)
        self.assertAlmostEqual(acc, 0.879242, places=5)
        self.assertEqual(pm.tails_from_acc(0.879242, 183, 44, 6, 3, 151, 18), 150)

    def test_server_reference_score(self):
        # same score: server pp 32.50 (osu! API).  The raw port is within 10%, i.e. the right formula family.
        A = {"aim": 2.16288, "speed": 1.35469, "sf": 0.91996, "adsc": 88.5857, "snc": 60.709, "adst": 53.5774,
             "sdst": 35.2055, "max_combo": 405, "nC": 85, "nS": 151, "nSp": 0, "nLT": 18,
             **pm.eff_stats({"od": 6, "ar": 8}, [], 1.0)}
        S = {"great": 183, "ok": 44, "meh": 6, "miss": 3, "combo": 327, "acc": 0.879242, "tails": 150}
        r = pm.pp_raw(A, S, [])
        self.assertLess(abs(r["pp"] / 32.5033 - 1), 0.10)

    def test_monotone(self):
        A = mk_attrs(4.8)
        base = pm.pp_raw(A, stats_for(A, 0.95, 3, 0.4))["pp"]
        self.assertGreater(pm.pp_raw(A, stats_for(A, 0.97, 3, 0.4))["pp"], base)     # more acc
        self.assertLess(pm.pp_raw(A, stats_for(A, 0.95, 10, 0.4))["pp"], base)       # more misses
        # lazer scores (slider tails/ticks known): combo itself doesn't count, missed slider parts do
        self.assertAlmostEqual(pm.pp_raw(A, stats_for(A, 0.95, 3, 0.8))["pp"], base)
        broken = dict(stats_for(A, 0.95, 3, 0.4), tick_miss=8)
        self.assertLess(pm.pp_raw(A, broken)["pp"], base)
        ss = pm.pp_raw(A, stats_for(A, 1.0, 0, 1.0))["pp"]
        self.assertGreater(ss, base)
        self.assertGreater(pm.pp_raw(A, stats_for(A, 0.95, 3, 0.4), ["HD"])["pp"], base)
        self.assertGreater(pm.pp_raw(mk_attrs(5.5), stats_for(mk_attrs(5.5), 0.95, 3, 0.4))["pp"], base)

    def test_calibration_fit_reduces_error(self):
        rnd = random.Random(3)
        pairs = []
        for _ in range(40):
            A = mk_attrs(rnd.uniform(3.5, 5.5))
            S = stats_for(A, rnd.uniform(0.85, 0.98), rnd.randint(0, 15), rnd.uniform(0.1, 1.0))
            hd = ["HD"] if rnd.random() < 0.4 else []
            srv = pm.pp_raw(A, S, hd)["pp"] * (1.06 if hd else 1.03)   # "server" a bit higher, more with HD
            pairs.append((A, S, hd, srv, 1.0))
        cal = pm.fit_calibration(pairs)
        self.assertLess(cal["mae_pct_cal"], cal["mae_pct_raw"])
        self.assertLess(cal["mae_pct_cal"], 1.0)

    def test_outcome_roundtrip(self):
        A = mk_attrs(4.6)
        row = {"g": 0, "ok": 0, "meh": 0, "miss": 6, "combo": 200}
        S = stats_for(A, 0.93, 6, 0.35)
        row.update({"g": S["great"], "ok": S["ok"], "meh": S["meh"]})
        y = pm.targets(row, A)
        o = pm.outcome_from_targets(y, A, {"meh_share": 0.1, "tail_drop": 0.0})
        self.assertEqual(o["miss"], 6)
        self.assertLessEqual(abs(o["great"] - S["great"]), 3)
        self.assertLessEqual(abs(o["combo"] - 200), 3)

    def test_parse_osu_text(self):
        txt = ("osu file format v14\n[General]\nMode: 0\n[Metadata]\nTitle:X\nVersion:Y\nBeatmapID:123\n"
               "BeatmapSetID:45\n[Difficulty]\nHPDrainRate:5\nCircleSize:4\nOverallDifficulty:8\nApproachRate:9\n"
               "[HitObjects]\n256,192,1000,1,0\n256,192,1500,2,0,L|300:200,1,100\n256,192,2000,12,0,4000\n")
        d = pm.parse_osu_text(txt)
        self.assertEqual((d["nC"], d["nS"], d["nSp"], d["bid"], d["od"], d["ar"]), (1, 1, 1, 123, 8.0, 9.0))
        self.assertAlmostEqual(d["len_s"], 3.0)


# ----------------------------------------------------------------------------
# synthetic player: improves over time, worse at higher stars, HD a bit worse, same-map practice helps
# ----------------------------------------------------------------------------

def synth_world(n_maps=40, n_plays=260, seed=7):
    rnd = random.Random(seed)
    maps = []
    for i in range(n_maps):
        md5 = f"{i:032x}"
        stars = rnd.uniform(3.4, 5.6)
        maps.append((md5, stars, rnd.randint(250, 700)))
    rows, attrs, minfo = [], {}, {}
    t0 = NOW - 30 * DAY
    plays_on = {}
    for k in range(n_plays):
        md5, stars, n = rnd.choice(maps)
        diff = ["HD"] if rnd.random() < 0.3 else []
        key = pm.attr_key(md5, diff, 1.0)
        if key not in attrs:
            attrs[key] = mk_attrs(stars + (0.05 if diff else 0), n=n, diff=diff)
            minfo[md5] = {"bid": 1000 + int(md5, 16), "status": "ranked", "od": 8.0, "ar": 9.0, "cs": 4.0,
                          "hp": 5.0, "nC": attrs[key]["nC"], "nS": attrs[key]["nS"], "nSp": 1, "len_s": n / 4.0,
                          "lib": {"sf": 0.1, "jv": 1.5, "den": 4.0}}
        A = attrs[key]
        ts = t0 + k * (30 * DAY / n_plays)
        skill = 4.2 + 0.6 * k / n_plays + 0.1 * plays_on.get(md5, 0) ** 0.5 - (0.15 if diff else 0)
        d = stars - skill
        miss_rate = math.exp(-4.5 + 1.6 * d + rnd.gauss(0, 0.35))
        misses = min(n - 5, int(miss_rate * n))
        hacc = max(0.6, min(0.995, 0.93 - 0.04 * d + rnd.gauss(0, 0.012)))
        cf = min(1.0, pm.exp_combo_frac(misses) * math.exp(rnd.gauss(0.2, 0.3)))
        S = stats_for(A, hacc, misses, cf, tails_drop=0.03)
        rows.append({"ts": ts, "md5": md5, "diff": diff, "rate": 1.0, "flags": sorted(diff), "src": "replay",
                     "passed": True, "progress": 1.0, "g": S["great"], "ok": S["ok"], "meh": S["meh"],
                     "miss": misses, "combo": S["combo"], "acc": S["acc"]})
        plays_on[md5] = plays_on.get(md5, 0) + 1
        # live attempts: fails get likelier with difficulty
        if rnd.random() < 0.5:
            p_fail = 1 / (1 + math.exp(-(d * 2.5 - 0.5)))
            failed = rnd.random() < p_fail
            prog = rnd.uniform(0.2, 0.9)
            rows.append({"ts": ts + 30, "md5": md5, "diff": diff, "rate": 1.0, "flags": sorted(diff), "src": "live", "live": True,
                         "passed": not failed, "outcome": "failed" if failed else "passed",
                         "progress": prog if failed else 1.0, "g": S["great"], "ok": S["ok"], "meh": S["meh"],
                         "miss": misses, "combo": S["combo"], "acc": S["acc"]})
    rows.sort(key=lambda r: r["ts"])
    return rows, attrs, minfo


def dataset_from(rows, attrs, minfo):
    out = []
    for r in rows:
        A = attrs[pm.attr_key(r["md5"], r["diff"], r["rate"])]
        rr = dict(r)
        rr["A"] = A
        rr["x"] = pm.feats(A, minfo[r["md5"]], r["diff"], r["rate"])
        if r["passed"]:
            rr["y"] = pm.targets(r, A)
            rr["S"] = pm._row_stats(r, A)
            rr["tail_drop"] = (A["nS"] - rr["S"]["tails"]) / A["nS"]
            rr["meh_share"] = r["meh"] / max(1, r["ok"] + r["meh"])
        out.append(rr)
    return out


class T1bReviewFixes(unittest.TestCase):
    """2026-11-10 review: custom-rate API attributes, practice feature, trainable passes, mod-family confidence."""

    def test_custom_rate(self):
        self.assertFalse(pm.custom_rate(["DT"], 1.5))
        self.assertTrue(pm.custom_rate(["DT"], 1.14))
        self.assertTrue(pm.custom_rate(["DT", "HD"], 1.15))
        self.assertFalse(pm.custom_rate(["HT"], 0.75))
        self.assertTrue(pm.custom_rate([], 1.2))
        self.assertFalse(pm.custom_rate(["HD"], 1.0))
        # the API ignores speed_change: never asked for a custom rate, and such cache entries are dropped
        self.assertIsNone(pm.api_attrs(1, ["DT"], 1.14))
        self.assertTrue(pm._bad_attr_entry("x|DT|1.14", {"src": "api"}))
        self.assertFalse(pm._bad_attr_entry("x|DT|1.14", {"src": "tosu"}))
        self.assertFalse(pm._bad_attr_entry("x|DTHD|1.50", {"src": "api"}))
        self.assertFalse(pm.seed_api_attrs("x", "DT", 1.14, {"aim_difficulty": 2.0}))

    def test_lrep_counts_earlier_attempts_only(self):
        rows = [{"ts": t, "md5": m, "x": {}} for t, m in ((10, "a"), (20, "a"), (20.5, "a"), (30, "b"), (40, "a"))]
        pm.set_lrep(rows)
        self.assertEqual([round(math.expm1(r["x"]["lrep"])) for r in rows], [0, 1, 1, 0, 3])
        self.assertIn("lrep", pm.Ridge.FEATS)

    def test_trainable_pass(self):
        A = mk_attrs(4.5)
        n = A["nC"] + A["nS"] + A["nSp"]
        good = {"g": n - 20, "ok": 15, "meh": 0, "miss": 5, "flags": []}
        self.assertTrue(pm.pp_trainable(good, A))
        self.assertFalse(pm.pp_trainable(dict(good, flags=["NF"]), A))
        self.assertFalse(pm.pp_trainable(dict(good, g=10, ok=n // 2, meh=n // 3, miss=n // 10), A))  # < 50% hit acc
        self.assertFalse(pm.pp_trainable(dict(good, g=n // 2, ok=0, miss=n // 2), A))              # > 25% misses

    def test_mod_family(self):
        self.assertEqual(pm.mod_family({"lrate": 0.0, "hr": 0.0}), "")
        self.assertEqual(pm.mod_family({"lrate": math.log(1.5), "hr": 0.0}), "DT")
        self.assertEqual(pm.mod_family({"lrate": 0.0, "hr": 1.0}), "HR")
        self.assertEqual(pm.mod_family({"lrate": math.log(1.5), "hr": 1.0}), "DTHR")


class T2Learners(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, attrs, minfo = synth_world()
        cls.rows = dataset_from(rows, attrs, minfo)
        cls.attrs, cls.minfo = attrs, minfo

    def test_each_learner_backtests(self):
        for name in ("knn", "ridge", "gbm", "curve"):
            s, det = pm.backtest(self.rows, name, 7.0, None, collect=True)
            self.assertGreater(s["n"], 100, name)
            for k in ("pp_mae", "pp_medae", "acc_mae", "miss_mae", "range_coverage"):
                self.assertTrue(math.isfinite(s[k]), (name, k))
            self.assertLess(s["pp_mape"], 60, name)

    def test_feature_models_beat_star_curve_on_same_map_effects(self):
        g, _ = pm.backtest(self.rows, "gbm", 7.0, None)
        c, _ = pm.backtest(self.rows, "curve", 21.0, None)
        self.assertLess(g["miss_mae"], c["miss_mae"] * 1.1)

    def test_no_leakage(self):
        """Predictions for early test points must not change when LATER rows change."""
        s1, d1 = pm.backtest(self.rows, "ridge", 7.0, None, collect=True)
        cut = d1[10]["ts"]
        mutated = []
        for r in self.rows:
            rr = dict(r)
            if r["ts"] > cut + 1 and r.get("y"):
                rr["y"] = {t: v + 3.0 for t, v in r["y"].items()}
            mutated.append(rr)
        s2, d2 = pm.backtest(mutated, "ridge", 7.0, None, collect=True)
        for a, b in zip(d1[:11], d2[:11]):
            self.assertAlmostEqual(a["pp_pred"], b["pp_pred"], places=6)

    def test_predictions_harder_map_fewer_pp_per_star_more_misses(self):
        pr = pm.Predictor("gbm", 7.0).fit(self.rows, NOW)
        lo = mk_attrs(3.8)
        hi = mk_attrs(5.6)
        m = {"len_s": 100, "lib": {"sf": 0.1, "jv": 1.5, "den": 4.0}, "od": 8, "ar": 9}
        p_lo = pr.predict_full(lo, pm.feats(lo, m, [], 1.0), None, [], [])
        p_hi = pr.predict_full(hi, pm.feats(hi, m, [], 1.0), None, [], [])
        self.assertGreater(p_hi["miss_q"][1], p_lo["miss_q"][1])
        self.assertLessEqual(p_hi["pp_low"], p_hi["pp_expected"])
        self.assertLessEqual(p_hi["pp_expected"], p_hi["pp_high"])
        self.assertLess(p_hi["p_pass"], p_lo["p_pass"])

    def test_pass_model_learns_difficulty(self):
        pmod = pm.PassModel().fit(self.rows, NOW)
        self.assertIsNotNone(pmod.w)
        m = {"len_s": 100, "lib": {}, "od": 8, "ar": 9}
        a = pmod.p_pass(pm.feats(mk_attrs(3.6), m, [], 1.0))
        b = pmod.p_pass(pm.feats(mk_attrs(5.6), m, [], 1.0))
        self.assertGreater(a, b)
        bt = pm.pass_backtest(self.rows)
        self.assertGreater(bt["n"], 20)

    def test_state_roundtrip(self):
        for name in ("knn", "ridge", "gbm", "blend"):
            pr = pm.Predictor(name, 7.0).fit(self.rows, NOW)
            st = json.loads(json.dumps(pr.state()))
            pr2 = pm.Predictor.from_state(st, self.rows, None)
            A = mk_attrs(4.7)
            m = {"len_s": 100, "lib": {}, "od": 8, "ar": 9}
            x = pm.feats(A, m, ["HD"], 1.0)
            a = pr.predict_full(A, x, None, ["HD"], ["HD"], now=NOW)
            b = pr2.predict_full(A, x, None, ["HD"], ["HD"], now=NOW)
            self.assertAlmostEqual(a["pp_expected"], b["pp_expected"], places=4, msg=name)


# ----------------------------------------------------------------------------
# end to end in the temp folder: files -> train -> predict -> log -> update
# ----------------------------------------------------------------------------

class T3EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for f in TMP.iterdir():
            if f.is_file():
                f.unlink()
        rows, attrs, minfo = synth_world(n_maps=25, n_plays=160, seed=11)
        cls.attrs, cls.minfo = attrs, minfo
        replays = {}
        live = []
        for i, r in enumerate(rows):
            n = r["g"] + r["ok"] + r["meh"] + r["miss"]
            if r["src"] == "replay":
                replays[f"C:/nonexistent/replay_{i}"] = {
                    "hdr": {"mode": 0, "md5": r["md5"], "player": "player1", "uid": pm.UID, "ts": r["ts"],
                            "score": i, "mods": "".join(r["diff"]) or "NM", "mbits": 8 if r["diff"] else 0,
                            "rate": 1.0, "acc": r["acc"], "misses": r["miss"], "combo": r["combo"]},
                    "sum": {"n": n, "n300": r["g"]}}
            else:
                live.append({"v": 1, "ts": r["ts"], "started_ts": r["ts"] - 90, "source": "live", "md5": r["md5"],
                             "mods": "".join(r["diff"]) or "NM", "rate": 1.0, "outcome": r["outcome"],
                             "progress": r["progress"], "acc": r["acc"], "misses": r["miss"],
                             "max_combo": r["combo"], "own": True,
                             "hits": {"300": r["g"], "100": r["ok"], "50": r["meh"], "0": r["miss"]}})
        # someone else's replay on the same machine must be ignored
        replays["C:/nonexistent/other"] = {"hdr": {"mode": 0, "md5": rows[0]["md5"], "player": "altacct",
                                                    "uid": 99999, "ts": NOW - DAY, "mods": "NM", "acc": 0.99,
                                                    "misses": 0, "combo": 999}, "sum": {"n": 300, "n300": 300}}
        (TMP / ".coach_replays.json").write_text(json.dumps({"v": 1, "replays": replays}), "utf-8")
        with open(TMP / "live_attempts.jsonl", "w", encoding="utf-8") as f:
            for a in live:
                f.write(json.dumps(a) + "\n")
        (TMP / ".pp_attr_cache.json").write_text(json.dumps({"v": 1, "maps": minfo, "attrs": attrs, "scores": {},
                                                             "lookup_fail": {}}), "utf-8")
        top = [{"beatmap_id": 9_000_000 + i, "md5": "f" * 32, "pp": 100 - i * 0.6} for i in range(100)]
        (TMP / "pp_data.json").write_text(json.dumps({"top_plays": top}), "utf-8")
        pm._cache = None
        pm._mem.update({"pred": None, "mtime": None, "meta": None})
        cls.meta = pm.train(full_backtest=True, learners=("knn", "ridge", "gbm"))

    def test_trained_and_reported(self):
        rep = pm.model_report()
        self.assertGreater(rep["trained_on"], 100)
        self.assertIn(rep["learner"].split(" ")[0], ("knn", "ridge", "gbm"))
        for k in ("pp_mae", "acc_mae", "miss_mae", "range_coverage", "n"):
            self.assertIsNotNone(rep["backtest"][k], k)
        self.assertEqual(len(self.meta["backtest_table"]), 6)    # 3 learners x half-lives 7, 21 d
        self.assertLess(abs(self.meta["backtest"]["range_coverage"] - 0.6), 0.2)

    def test_other_player_excluded(self):
        rows = pm.raw_rows()
        self.assertFalse(any(r.get("player") == "altacct" for r in rows))

    def test_predict_pp_contract_and_speed(self):
        md5 = next(iter(self.minfo))
        p = pm.predict_pp(md5, "HD", 1.0, network=False)
        self.assertIsNotNone(p)
        for k in ("pp_expected", "pp_low", "pp_high", "pp_fc", "pp_ss", "p_pass", "stars", "gain_expected",
                  "gain_high", "confidence", "basis", "model_error_pp"):
            self.assertIn(k, p)
        self.assertLessEqual(p["pp_low"], p["pp_expected"])
        self.assertLessEqual(p["pp_expected"], p["pp_high"])
        self.assertLessEqual(p["pp_high"], p["pp_ss"] + 1e-6)
        self.assertIsNotNone(p["gain_expected"])
        o = pm.predict_outcome(md5, "HD", 1.0, network=False)
        self.assertLessEqual(o["acc"]["p20"], o["acc"]["p80"])
        self.assertLessEqual(o["misses"]["p20"], o["misses"]["p80"])
        t = time.perf_counter()
        for _ in range(5):
            pm.predict_pp(md5, "NM", 1.0, network=False)
        warm = (time.perf_counter() - t) / 5
        self.assertLess(warm, 0.3)
        # cold load from disk (another process wrote the model)
        pm._mem.update({"pred": None, "mtime": None})
        t = time.perf_counter()
        self.assertIsNotNone(pm.predict_pp(md5, "NM", 1.0, network=False))
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_unknown_map_and_assisted(self):
        self.assertIsNone(pm.predict_pp("e" * 32, "NM", 1.0, network=False))
        md5 = next(iter(self.minfo))
        self.assertIsNone(pm.predict_pp(md5, "RX", 1.0, network=False))

    def test_zz_log_and_update(self):
        md5 = next(iter(self.minfo))
        p = pm.predict_pp(md5, "NM", 1.0, network=False)
        start = time.time()
        self.assertTrue(pm.log_pre(md5, "NM", 1.0, p, when=start, source="hover"))
        A = self.attrs[pm.attr_key(md5, [], 1.0)]
        S = stats_for(A, 0.94, 4, 0.4)
        recs = [{"v": 1, "ts": start + 95, "started_ts": start + 2, "md5": md5, "mods": "NM", "rate": 1.0,
                 "outcome": "passed", "progress": 1.0, "acc": S["acc"], "misses": 4, "max_combo": S["combo"],
                 "own": True, "hits": {"300": S["great"], "100": S["ok"], "50": S["meh"], "0": 4}},
                # an attempt nobody hovered (no snapshot): scored with the model as it was before
                {"v": 1, "ts": start + 300, "started_ts": start + 200, "md5": list(self.minfo)[1], "mods": "NM",
                 "rate": 1.0, "outcome": "failed", "progress": 0.4, "acc": 0.9, "misses": 9, "max_combo": 50,
                 "own": True, "hits": {"300": 100, "100": 10, "50": 1, "0": 9}}]
        with open(TMP / "live_attempts.jsonl", "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        before = pm.model_report()["trained_on"]
        res = pm.update()
        self.assertEqual(res["status"], "updated")
        self.assertEqual(res["scored"], 2)
        log = [r for r in pm.read_log() if r.get("kind") == "scored"]
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["prediction_source"], "hover")
        self.assertEqual(log[0]["predicted"]["pp_expected"], p["pp_expected"])    # the snapshot, not a refit
        self.assertTrue(log[0]["actual"]["passed"])
        self.assertIsNotNone(log[0]["actual"]["pp"])
        self.assertEqual(log[1]["prediction_source"], "model_before_attempt")
        self.assertFalse(log[1]["actual"]["passed"])
        self.assertEqual(pm.model_report()["trained_on"], before + 1)
        self.assertEqual(pm.update()["status"], "up to date")
        rec = pm.recent_accuracy()
        self.assertEqual(rec["n"], 1)


class T4LiveWatcher(unittest.TestCase):
    def test_pre_attempt_log_uses_hover_prediction(self):
        calls = []

        class Stub:
            @staticmethod
            def predict_pp(*a, **k):
                calls.append(k)
                return {"pp_expected": 50.0, "pp_low": 40.0, "pp_high": 60.0, "source": "pp_model"}

            @staticmethod
            def log_pre(md5, acs, rate, p, when=None, source=None, beatmap_id=None):
                calls.append(("log", md5, source, p["pp_expected"]))
                return True
        old = pl._pm["mod"]
        pl._pm["mod"] = Stub
        try:
            w = pl.LiveWatcher(lambda s: None, record=True)
            w._pred_cache[w._pred_key("a" * 32, ["HD"], 1.0)] = {"pp_expected": 42.0, "source": "pp_model"}
            w._log_pre_attempt({"md5": "a" * 32, "mods": "HD", "rate": 1.0, "started_ts": 123.0})
            self.assertEqual(calls[-1], ("log", "a" * 32, "hover", 42.0))
            w._log_pre_attempt({"md5": "b" * 32, "mods": "NM", "rate": 1.0, "started_ts": 124.0})
            self.assertEqual(calls[-1], ("log", "b" * 32, "play_start", 50.0))
            self.assertFalse(calls[-2]["tosu"])          # no tosu calculation while playing
        finally:
            pl._pm["mod"] = old

    def test_server_attributes_fetched_in_background(self):
        class Stub:
            norm_mods = staticmethod(pm.norm_mods)

            @staticmethod
            def get_attrs(md5, diff, rate, beatmap_id=None, network=True):
                return {"src": "api"}

            @staticmethod
            def cache():
                class C:
                    def flush(self):
                        pass
                return C()
        w = pl.LiveWatcher(lambda s: None, record=False)
        key = w._pred_key("c" * 32, [], 1.0)
        w._pred_cache[key] = {"pp_expected": 1.0, "attr_source": "tosu"}
        os.environ.pop("OSU_COACH_NO_NETWORK", None)
        w._server_attrs_later(Stub, {"md5": "c" * 32, "_acs": [], "rate": 1.0, "beatmap_id": 5}, key)
        for _ in range(50):
            if w._repredict == key:
                break
            time.sleep(0.02)
        self.assertEqual(w._repredict, key)
        self.assertNotIn(key, w._pred_cache)

    def test_results_carry_prediction(self):
        r = pl.LiveWatcher._with_prediction({"pp": 55.0}, {"pp_expected": 50.0, "pp_low": 40.0, "pp_high": 60.0})
        self.assertEqual(r["prediction_error_pp"], 5.0)
        self.assertTrue(r["within_range"])
        self.assertEqual(pl.LiveWatcher._with_prediction(None, {"pp_expected": 1}), None)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class T5Identity(unittest.TestCase):
    """coach_identity: config id/name, logged-in name, newest replay; renames merged; exclude_users honoured."""
    H = [{"player": "player1", "uid": 12345, "ts": 10}, {"player": "oldname", "uid": 12345, "ts": 5},
         {"player": "altacct", "uid": 99999, "ts": 30}, {"player": "stableguy", "uid": None, "ts": 1}]

    @staticmethod
    def ident(**kw):
        d = {"user_id": None, "username": None, "exclude_names": set(), "exclude_ids": set()}
        d.update(kw)
        return d

    def test_order_and_renames(self):
        import coach_identity as ci
        r = ci.resolve(self.H, "altacct", self.ident(user_id=12345))
        self.assertEqual((r["uid"], r["names"], r["source"]), (12345, {"player1", "oldname"}, "config"))
        r = ci.resolve(self.H, "altacct", self.ident(username="OLDNAME"))
        self.assertEqual((r["uid"], r["names"]), (12345, {"player1", "oldname"}))
        r = ci.resolve(self.H, "player1", self.ident())
        self.assertEqual((r["uid"], r["source"]), (12345, "game"))
        r = ci.resolve(self.H, None, self.ident())
        self.assertEqual((r["uid"], r["source"]), (99999, "newest replay"))
        r = ci.resolve(self.H, "stableguy", self.ident())
        self.assertEqual((r["uid"], r["names"]), (None, {"stableguy"}))

    def test_exclude(self):
        import coach_identity as ci
        r = ci.resolve(self.H, None, self.ident(exclude_names={"altacct"}))
        self.assertEqual((r["uid"], r["names"]), (12345, {"player1", "oldname"}))
        r = ci.resolve(self.H, "altacct", self.ident(exclude_ids={99999}))
        self.assertEqual(r["uid"], 12345)
        self.assertIn(99999, r["exclude_ids"])

    def test_config_parsing(self):
        import coach_identity as ci
        f = TMP / "ident_cfg.json"
        f.write_text(json.dumps({"client_id": 1, "client_secret": "x", "user": "", "user_id": 0,
                                 "exclude_users": ["AltAcct", 99999, ""]}), "utf-8")
        i = ci.load_identity(f)
        self.assertEqual((i["user_id"], i["username"], i["exclude_names"], i["exclude_ids"]),
                         (None, None, {"altacct"}, {99999}))
        f.write_text(json.dumps({"user": "12345"}), "utf-8")
        self.assertEqual(ci.load_identity(f)["user_id"], 12345)
        f.write_text(json.dumps({"user": "Some Name"}), "utf-8")
        self.assertEqual(ci.load_identity(f)["username"], "Some Name")
        self.assertIsNone(ci.load_identity(TMP / "missing.json")["user_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
