#!/usr/bin/env python3
"""osu! API v2 client for the coach (stdlib only).  See CONTRACT_PP.md.

    python osu_api.py [--force] [--climb-only]

Writes pp_data.json (atomically) next to this file:
  * user        - profile from GET /users/{id}/osu
  * history     - one snapshot per calendar day, kept forever
  * top_plays   - GET /users/{id}/scores/best (limit 100)
  * pp_threshold, and weighted_gain() for exact "how much total pp would X pp add"
  * climb       - maps that should raise YOUR total pp, ranked for YOUR skill model

Credentials: osu_api_config.json {"client_id": 12345, "client_secret": "...", "user": "<your osu! name or id>"}
filled in by the user (template: osu_api_config.example.json; "user" may stay empty: then the account logged
in to osu! is used, see coach_identity.py).  Uses the client-credentials grant with scope "public"
only; nothing is ever posted on the user's behalf.  The secret is never printed, logged or written anywhere else.

Never writes the osu! data folder (reads only the coach's own caches and, when "user" is empty, the logged-in
username from game.ini) and never touches the osu! process.

Climb-list method (why it is built this way)
--------------------------------------------
/rankings only lists the top 10,000, so "players slightly better than me" (rank ~590k) cannot
be listed directly.  Instead we SAMPLE them:
  1. Activity sample: GET /scores (the global feed of recently submitted scores, one call
     returns up to 1000) -> user ids of people playing right now.
  2. Taste sample: GET /beatmaps/{id}/scores for a few of the player's own top-play maps
     -> users who play the same kind of maps.  Capped at ~1/3 of the source players.
  3. Fallback (if /scores is unavailable): random user ids, looked up 50 at a time.
  Ids are resolved with GET /users?ids[]=... (50 per call) and kept when their osu! pp is
  1.1-1.6x the player's.  Their top 100s (GET /users/{id}/scores/best) are pooled per
  beatmap+mods.  Each candidate is then re-scored for THIS player: star band around his
  current level, expected good-run accuracy/misses from his own data, aim/speed balance
  from POST /beatmaps/{id}/attributes, exact weighted pp gain, owned maps first.
Biases (also written to pp_data.json climb_meta.bias): the activity sample over-represents
people who play a lot; the taste sample over-represents his genre; random-id fallback
over-represents older accounts; top-100 lists over-represent "farm" maps (high pp per
difficulty) - which is the point for pp, but they are not necessarily the best TRAINING maps.
pp predictions without tosu are a coarse model (labelled "low" confidence).
"""

import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coach_identity as _identity  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OSU_COACH_API_DIR") or HERE)
CONFIG_FILE = DATA_DIR / "osu_api_config.json"
TOKEN_FILE = DATA_DIR / ".osu_api_token.json"
OUT_FILE = DATA_DIR / "pp_data.json"
LIB_FILE = DATA_DIR / ".coach_library.json"
OSU_CACHE_FILE = DATA_DIR / ".osu_coach_cache.json"
COACH_DATA = DATA_DIR / "coach_data.json"

BASE = (os.environ.get("OSU_COACH_API_URL") or "https://osu.ppy.sh").rstrip("/")
MIN_INTERVAL = float(os.environ.get("OSU_COACH_API_SLEEP", "1.1"))   # seconds between requests (<60/min)
MAX_REQUESTS = int(os.environ.get("OSU_COACH_API_BUDGET", "150"))    # per refresh
PROFILE_TTL = 30 * 60
CLIMB_TTL = 24 * 3600
UA = "osu-coach/1.0 (personal training helper)"

BAND_LO, BAND_HI = 1.10, 1.60          # source players: pp ratio vs the player
MAX_SOURCES = 36                       # players whose top 100 we fetch
MAX_TASTE_SOURCES = 12
FIREHOSE_USERS = 500                   # ids from /scores to resolve (10 lookups)
MAX_ATTR_CALLS = 45
MAX_CLIMB = 30
STAR_BELOW, STAR_ABOVE = 0.2, 0.6      # climb band around current stars
ACC_EXP = 4.5                          # pp ~ acc^4.5 (coarse fit of the pp formula, 90-100% acc)
MISS_F = 0.96                          # pp factor per miss (coarse)

IGNORE_MODS = {"NF", "SD", "PF", "TD", "CL", "MR", "SV2", "V2"}
MOD_ORDER = ["EZ", "HD", "HR", "DT", "HT", "FL", "SO", "BL", "AC", "RX", "AP"]
RATE_OF = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}


class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def log(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path, obj, indent=1):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False))
    for _ in range(20):            # the pet may have the file open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def r1(x):
    return None if x is None else round(float(x), 1)


def r2(x):
    return None if x is None else round(float(x), 2)


def today_str(now=None):
    return datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# ----------------------------------------------------------------------------
# pp math
# ----------------------------------------------------------------------------

def weighted_total(pps):
    """Weighted sum over the top 100: sum pp_i * 0.95^i (pps in any order)."""
    s = sorted((float(p) for p in pps if p is not None), reverse=True)[:100]
    return sum(p * 0.95 ** i for i, p in enumerate(s))


def weighted_gain(top_pp_list, new_pp, replaces=None, tail=None):
    """Exact change of the weighted top-100 sum if a play worth `new_pp` is set.

    replaces: pp of the player's existing score on the SAME beatmap (if any).  Only the best
    score per beatmap counts, so the gain is 0 unless new_pp beats it, and the old score
    is removed from the list when it does.  (Bonus pp from play count is unaffected.)
    """
    if new_pp is None:
        return 0.0
    old = sorted((float(p) for p in top_pp_list if p is not None), reverse=True)
    new = list(old)
    replaced = False
    if replaces is not None:
        if new_pp <= replaces:
            return 0.0
        for i, p in enumerate(new):            # remove one copy of the replaced score
            if abs(p - float(replaces)) < 1e-6:
                del new[i]
                replaced = True
                break
    new.append(float(new_pp))
    g = weighted_total(new) - weighted_total(old)
    # tail = weighted pp below #100 (total - bonus - weighted top 100): entering a full top 100 moves the old
    # #100 to #101 instead of dropping it, and shifts the scores below down one place (pp_recommend.weighted_gain)
    if tail is not None and len(old) >= 100 and not replaced and float(new_pp) > old[99]:
        g += old[99] * 0.95 ** 100 - 0.05 * float(tail)
    return max(0.0, g)


def mods_of(score):
    """-> (display list, rate).  Accepts legacy ['HD','DT'] or lazer [{'acronym':..,'settings':..}]."""
    out, rate = [], 1.0
    for m in score.get("mods") or []:
        if isinstance(m, dict):
            ac = str(m.get("acronym") or "").upper()
            sc = (m.get("settings") or {}).get("speed_change")
        else:
            ac, sc = str(m).upper(), None
        if not ac or ac in IGNORE_MODS:
            continue
        if ac in RATE_OF:
            rate = float(sc) if sc else RATE_OF[ac]
            ac = "DT" if rate > 1 else "HT"
        if ac not in out:
            out.append(ac)
    out.sort(key=lambda a: MOD_ORDER.index(a) if a in MOD_ORDER else 99)
    return out, rate


def mods_str(mods):
    return "".join(mods) or "NM"


def core_key(mods, rate):
    """Grouping key: HD/FL-free difficulty-changing mods + rate (HD doesn't change star rating)."""
    core = [m for m in mods if m in ("EZ", "HR", "DT", "HT")]
    return mods_str(core) + ("@%.2f" % rate if abs(rate - 1.0) > 1e-6 else "")


def preempt(ar):
    return 1200 + 600 * (5 - ar) / 5 if ar < 5 else 1200 - 750 * (ar - 5) / 5


def ar_from_preempt(ms):
    return 5 - (ms - 1200) / 120 if ms > 1200 else 5 + (1200 - ms) / 150


def adjust_stats(bpm, ar, od, cs, length, mods, rate):
    if "HR" in mods:
        cs, ar, od = min(10, cs * 1.3), min(10, ar * 1.4), min(10, od * 1.4)
    if "EZ" in mods:
        cs, ar, od = cs / 2, ar / 2, od / 2
    if abs(rate - 1) > 1e-6:
        ar = ar_from_preempt(preempt(ar) / rate)
        od = (80 - (80 - 6 * od) / rate) / 6
        bpm, length = bpm * rate, length / rate
    return bpm, ar, od, cs, length


def rough_stars(nm_stars, mods, rate):
    """Pre-filter estimate only; finalists get the real value from /attributes."""
    s = nm_stars
    if abs(rate - 1) > 1e-6:
        s *= 1 + (rate - 1) * 0.8 if rate > 1 else 1 - (1 - rate) * 1.0
    if "HR" in mods:
        s *= 1.08
    if "EZ" in mods:
        s *= 0.85
    return s


def ss_equiv(pp, acc, misses):
    """Undo the coarse acc/miss model: the pp an SS would roughly give."""
    acc = max(0.5, min(1.0, acc or 1.0))
    return pp / (acc ** ACC_EXP * MISS_F ** max(0, misses or 0))


def pp_at(ss_pp, acc, misses):
    return ss_pp * max(0.5, min(1.0, acc)) ** ACC_EXP * MISS_F ** max(0.0, misses)


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# ----------------------------------------------------------------------------
# HTTP client
# ----------------------------------------------------------------------------

def network_disabled():
    """OSU_COACH_NO_NETWORK=1 (tests, offline runs): no request may reach the osu! website."""
    return bool(os.environ.get("OSU_COACH_NO_NETWORK"))


def _is_local(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "") in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


class Api:
    def __init__(self, cfg, budget=MAX_REQUESTS, base=None, interval=MIN_INTERVAL):
        self.cid = str(cfg.get("client_id") or "").strip()
        self._secret = str(cfg.get("client_secret") or "")
        self.base = (base or BASE).rstrip("/")
        self.interval = interval
        self.budget = budget
        self.count = 0
        self._last = 0.0
        self._token = None
        self._exp = 0

    def __repr__(self):            # never show the secret
        return "<Api client_id=%s requests=%d>" % (self.cid, self.count)

    def _wait(self):
        dt = time.monotonic() - self._last
        if dt < self.interval:
            time.sleep(self.interval - dt)
        self._last = time.monotonic()

    def _raw(self, method, url, data=None, headers=None, timeout=20):
        if network_disabled() and not _is_local(url):
            raise ApiError("network disabled (OSU_COACH_NO_NETWORK)")
        if self.count >= self.budget:
            raise BudgetExceeded("request budget (%d) used up" % self.budget)
        self._wait()
        self.count += 1
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return r.status, (json.loads(body.decode("utf-8")) if body else None), r.headers
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                body = ""
            return e.code, {"_error": body}, e.headers

    # -- token ------------------------------------------------------------
    def token(self, force=False):
        now = time.time()
        if not force and self._token and self._exp > now + 60:
            return self._token
        if not force:
            t = load_json(TOKEN_FILE, {})
            if (t.get("client_id") == self.cid and t.get("base") == self.base
                    and t.get("access_token") and float(t.get("expires_at", 0)) > now + 60):
                self._token, self._exp = t["access_token"], float(t["expires_at"])
                return self._token
        form = urllib.parse.urlencode({"client_id": self.cid, "client_secret": self._secret,
                                       "grant_type": "client_credentials", "scope": "public"}).encode()
        code, js, _ = self._raw("POST", self.base + "/oauth/token", form,
                                {"Accept": "application/json", "User-Agent": UA,
                                 "Content-Type": "application/x-www-form-urlencoded"})
        if code != 200 or not isinstance(js, dict) or not js.get("access_token"):
            hint = ""
            if code in (400, 401):
                hint = (" - check client_id / client_secret in osu_api_config.json "
                        "(osu! website > Settings > OAuth > your application)")
            raise ApiError("token request failed (HTTP %s)%s" % (code, hint), code)
        self._token = js["access_token"]
        self._exp = now + float(js.get("expires_in") or 3600)
        try:
            atomic_write_json(TOKEN_FILE, {"client_id": self.cid, "base": self.base,
                                           "access_token": self._token, "expires_at": self._exp})
        except Exception:
            pass
        return self._token

    def call(self, method, path, params=None, body=None):
        url = self.base + "/api/v2" + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(3):
            # x-api-version: current score format (lazer mods with settings, e.g. DT speed_change)
            hdr = {"Accept": "application/json", "User-Agent": UA, "x-api-version": "20240529",
                   "Authorization": "Bearer " + self.token(force=attempt > 0 and last == 401)}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            code, js, headers = self._raw(method, url, data, hdr)
            last = code
            if code == 200:
                return js
            if code == 401 and attempt == 0:
                self._token = None
                continue
            if code == 429 and attempt < 2:
                try:
                    wait = float(headers.get("Retry-After") or 30)
                except Exception:
                    wait = 30
                time.sleep(min(max(wait, 0), 120))
                continue
            raise ApiError("%s %s -> HTTP %s" % (method, path.split("?")[0], code), code)
        raise ApiError("%s %s failed" % (method, path), last)

    def get(self, path, params=None):
        return self.call("GET", path, params)

    def post(self, path, body):
        return self.call("POST", path, body=body)


# ----------------------------------------------------------------------------
# local data (read-only)
# ----------------------------------------------------------------------------

def load_owned():
    """-> (md5 set, beatmap_id set, md5->.osu path).  Coach caches only, never the osu! folder."""
    md5s, bids, paths = set(), set(), {}
    lib = load_json(LIB_FILE, {}).get("maps") or {}
    for md5, f in lib.items():
        md5s.add(md5)
        if isinstance(f, dict) and f.get("bid"):
            try:
                bids.add(int(f["bid"]))
            except Exception:
                pass
    for path, md5 in (load_json(OSU_CACHE_FILE, {}).get("osu") or {}).items():
        if md5:
            md5s.add(md5)
            paths.setdefault(md5, path)
    return md5s, bids, paths


_tosu = {}


def tosu_ok(pl):
    """tosu availability, checked at most once a minute (each failed check costs ~0.4 s)."""
    if time.time() - _tosu.get("t", 0) > 60:
        try:
            _tosu["ok"] = bool(pl.tosu_available())
        except Exception:
            _tosu["ok"] = False
        _tosu["t"] = time.time()
    return _tosu["ok"]


def try_pp_live():
    if os.environ.get("OSU_COACH_NO_TOSU"):
        return None
    try:
        sys.path.insert(0, str(HERE))
        import pp_live  # noqa
        return pp_live
    except Exception:
        return None


# ----------------------------------------------------------------------------
# score normalisation
# ----------------------------------------------------------------------------

def norm_score(s):
    bm = s.get("beatmap") or {}
    bs = s.get("beatmapset") or bm.get("beatmapset") or {}
    st = s.get("statistics") or {}
    mods, rate = mods_of(s)
    misses = st.get("count_miss", st.get("miss", 0)) or 0
    return {
        "beatmap_id": int(s.get("beatmap_id") or bm.get("id") or 0),
        "set_id": int(bm.get("beatmapset_id") or bs.get("id") or 0),
        "md5": bm.get("checksum") or "",
        "artist": bs.get("artist") or "", "title": bs.get("title") or "",
        "creator": bs.get("creator") or "", "version": bm.get("version") or "",
        "nm_stars": bm.get("difficulty_rating"),
        "bpm": bm.get("bpm"), "ar": bm.get("ar"), "od": bm.get("accuracy"), "cs": bm.get("cs"),
        "length": bm.get("hit_length") or bm.get("total_length"),
        "n_objects": sum(int(bm.get(k) or 0) for k in ("count_circles", "count_sliders", "count_spinners")) or None,
        "mods": mods, "rate": rate, "pp": s.get("pp"), "acc": s.get("accuracy"),
        "misses": int(misses), "date": (s.get("ended_at") or s.get("created_at") or "")[:10],
        "user_id": s.get("user_id") or (s.get("user") or {}).get("id"),
        "mode": s.get("ruleset_id", s.get("mode_int", 0)),
    }


def user_osu_stats(u):
    st = (u.get("statistics_rulesets") or {}).get("osu") or u.get("statistics") or {}
    return st.get("pp"), st.get("global_rank")


# ----------------------------------------------------------------------------
# player model
# ----------------------------------------------------------------------------

def player_model(top, now=None):
    """Current level and good-run (~80th percentile) accuracy / miss rate for prediction.

    The good run comes from the player's CURRENT local replays (pp_live skill model: the fitted p80 accuracy and
    p20 miss rate at their level, else the p80/p20 over all recent plays). Old top plays are only used as an
    upper cap on accuracy (a years-old top play says what they could do then, not now), and alone only when no
    local model exists at all."""
    now = now or time.time()
    basis = []
    cutoff = datetime.fromtimestamp(now - 548 * 86400).strftime("%Y-%m-%d")
    recent = [t for t in top if t.get("date", "") >= cutoff and t.get("stars")]
    old = [t for t in top if t.get("stars")]
    cur, acc_good, miss_rate, local = None, None, None, False
    pl = try_pp_live()
    sm = None
    if pl is not None:
        try:
            sm = pl.skill_model()
        except Exception:
            sm = None
    if isinstance(sm, dict) and sm.get("comfort_stars"):
        cur = float(sm["comfort_stars"])
        basis.append("level from the skill model (%s star-rated local replays)" % sm.get("n", "?"))
    if cur is None:
        rm = (load_json(COACH_DATA, {}).get("roadmap") or {})
        if rm.get("current_stars"):
            cur = float(rm["current_stars"])
            basis.append("level from the coach roadmap")
    if cur is None:
        if len(recent) >= 3:
            cur = statistics.median(t["stars"] for t in recent)
            basis.append("level = median stars of %d top plays from the last 18 months" % len(recent))
        elif old:
            cur = statistics.median(t["stars"] for t in old[:20]) * 0.9
            basis.append("level = old top plays minus 10% (rust)")
        else:
            cur = 4.0
            basis.append("default level")
    if isinstance(sm, dict):
        fit = sm.get("fit") or {}
        try:
            if fit.get("x") and hasattr(pl, "_curve_at"):
                _am, a_g, _mm, m_g, _ov = pl._curve_at(fit, cur)
                acc_good, miss_rate, local = float(a_g), (float(m_g) if m_g is not None else None), True
                basis.append("good run = your fitted 80th-percentile result at %.1f* (local replays)" % cur)
        except Exception:
            pass
        ov = sm.get("overall") or {}
        if acc_good is None and ov.get("acc_p80") is not None:
            acc_good, local = float(ov["acc_p80"]), True
            miss_rate = float(ov["miss_p20"]) if ov.get("miss_p20") is not None else None
            basis.append("good run = your 80th-percentile result over recent plays at any difficulty "
                         "(no star ratings yet: coarse)")
    src = recent if len(recent) >= 3 else old
    accs = [t["acc"] for t in src if t.get("acc")]
    top_acc = pct(accs, 0.6) if accs else None
    if top_acc is not None and src is old:
        top_acc -= 0.02               # old accuracy, not yet re-earned
    if acc_good is None:
        acc_good = top_acc if top_acc is not None else 0.93
        basis.append("good-run acc from top plays (no local replay model)")
    elif top_acc is not None and acc_good > top_acc:
        acc_good = top_acc            # cap only: never above what his top plays show
        basis.append("capped at your top-play accuracy")
    acc_good = max(0.70, min(0.985, acc_good))
    # pp scale (SS-equivalent per star^3) from his own plays, for maps with no reference score
    ks = [ss_equiv(t["pp"], t["acc"], t.get("misses", 0)) / t["stars"] ** 3
          for t in top if t.get("pp") and t.get("stars") and t.get("acc")]
    k = statistics.median(ks) if ks else 1.6
    stats = load_json(COACH_DATA, {}).get("stats") or {}
    return {"current_stars": round(cur, 2), "acc_good": round(acc_good, 4), "k_ss": k,
            "miss_rate_good": round(miss_rate, 3) if miss_rate is not None else None, "local": local,
            "stream_break_bpm": stats.get("stream_break_bpm"), "basis": "; ".join(basis)}


def predict_good_run(stars, model, n_objects=None):
    """Coarse good-run (about your best 1 in 5 attempts) acc and misses at this star rating.
    Misses scale with the map's object count when the model has a per-100-objects miss rate."""
    d = stars - model["current_stars"]
    acc = model["acc_good"] - 0.035 * d if d > 0 else model["acc_good"] + 0.012 * (-d)
    acc = max(0.70, min(0.99, acc))
    mr = model.get("miss_rate_good")
    if mr is not None:
        n = n_objects or 400           # ~a TV-size Insane when the object count is unknown
        f = 2.0 ** d if d > 0 else max(0.5, 1.0 + 0.5 * d)   # coach heuristic: ~2x misses per +1*
        misses = max(0.0, mr * n / 100.0 * f)
    else:
        misses = max(0.0, 3.0 * (d + 0.2))
    return acc, misses


# ----------------------------------------------------------------------------
# refresh pieces
# ----------------------------------------------------------------------------

def fetch_user(api, user):
    u = str(user).strip()
    if u.isdigit():
        js = api.get("/users/%s/osu" % u, {"key": "id"})
    else:
        js = api.get("/users/%s/osu" % urllib.parse.quote(u), {"key": "username"})
    st = js.get("statistics") or {}
    rh = js.get("rank_highest") or {}
    return {
        "id": js.get("id"), "username": js.get("username"),
        "pp": r1(st.get("pp")), "global_rank": st.get("global_rank"),
        "country_rank": st.get("country_rank"), "country": js.get("country_code"),
        "accuracy": r2(st.get("hit_accuracy")), "play_count": st.get("play_count"),
        "peak_rank": rh.get("rank"), "peak_rank_date": (rh.get("updated_at") or "")[:10] or None,
    }


def fetch_top(api, uid, with_attrs=True):
    js = api.get("/users/%s/scores/best" % uid, {"mode": "osu", "limit": 100})
    out = []
    for s in js or []:
        n = norm_score(s)
        if n["pp"] is None:
            continue
        n["stars"] = rough_stars(n["nm_stars"], n["mods"], n["rate"]) if n["nm_stars"] else None
        out.append(n)
    if with_attrs:   # star rating with rate/HR/EZ mods differs from the NoMod value
        for n in [x for x in out if core_key(x["mods"], x["rate"]) != "NM"][:10]:
            try:
                a = beatmap_attrs(api, n["beatmap_id"], n["mods"], n["rate"])
                if a and a.get("star_rating"):
                    n["stars"] = a["star_rating"]
            except (ApiError, BudgetExceeded):
                break
    out.sort(key=lambda n: -n["pp"])
    return out


def top_public(n):
    return {"beatmap_id": n["beatmap_id"], "md5": n["md5"], "artist": n["artist"], "title": n["title"],
            "version": n["version"], "stars": r2(n["stars"]), "mods": mods_str(n["mods"]),
            "pp": r1(n["pp"]), "acc": round(n["acc"], 4) if n["acc"] is not None else None,
            "misses": n["misses"], "date": n["date"]}


def thresholds(pps):
    s = sorted(pps, reverse=True)
    pick = lambda i: r1(s[i]) if len(s) > i else (r1(s[-1]) if s else None)  # noqa
    return {"top1": pick(0), "top50": pick(49), "top100": pick(99)}


_attr_cache = {}


def beatmap_attrs(api, bid, mods, rate):
    key = (bid, core_key(mods, rate))
    if key in _attr_cache:
        return _attr_cache[key]
    ml = []
    for m in mods:
        if m in ("HD", "FL", "HR", "EZ", "DT", "HT"):
            if m in ("DT", "HT") and abs(rate - RATE_OF[m]) > 1e-6:
                ml.append({"acronym": m, "settings": {"speed_change": rate}})
            else:
                ml.append({"acronym": m})
    js = api.post("/beatmaps/%d/attributes" % bid, {"mods": ml, "ruleset": "osu"})
    a = (js or {}).get("attributes") or {}
    _attr_cache[key] = a
    return a


def lookup_users(api, ids, want, found, source):
    """Resolve ids 50 at a time, keep those in the pp band.  found: {uid: info}."""
    ids = [i for i in ids if i not in found]
    for i in range(0, len(ids), 50):
        if sum(1 for f in found.values() if f["source"] == source) >= want:
            return
        js = api.get("/users", [("ids[]", x) for x in ids[i:i + 50]])
        for u in (js or {}).get("users") or []:
            pp, rank = user_osu_stats(u)
            if pp and want_band[0] <= pp <= want_band[1] and u.get("id") not in found:
                found[u["id"]] = {"id": u["id"], "pp": pp, "rank": rank, "source": source}


want_band = (0, 0)


def sample_players(api, uid, player_pp, top, seed, meta):
    """-> list of {'id','pp','rank','source'} with pp in BAND_LO..BAND_HI x player's."""
    global want_band
    want_band = (player_pp * BAND_LO, player_pp * BAND_HI)
    rng = random.Random(seed)
    found = {}
    skip = {uid}
    # taste sample: leaderboards of his own best maps
    taste_ids = []
    for t in top[:3]:   # global top-50 leaderboards: these players are usually far above his band (low yield)
        try:
            js = api.get("/beatmaps/%d/scores" % t["beatmap_id"], {"mode": "osu"})
        except ApiError:
            continue
        for s in (js or {}).get("scores") or []:
            u = s.get("user_id") or (s.get("user") or {}).get("id")
            if u and u not in skip and u not in taste_ids:
                taste_ids.append(u)
    rng.shuffle(taste_ids)
    lookup_users(api, [i for i in taste_ids if i not in skip], MAX_TASTE_SOURCES, found, "taste")
    meta["sources"].append("taste: %d leaderboard users checked, %d in your pp band"
                           % (len(taste_ids), sum(1 for f in found.values() if f["source"] == "taste")))
    # activity sample: global recent-score feed
    feed_ids = []
    try:
        js = api.get("/scores", {"ruleset": "osu"})
        for s in (js or {}).get("scores") or []:
            if s.get("ruleset_id", 0) not in (0, None):
                continue
            u = s.get("user_id") or (s.get("user") or {}).get("id")
            if u and u not in skip and u not in feed_ids:
                feed_ids.append(u)
        meta["sources"].append("activity: %d recent-score users" % len(feed_ids))
    except ApiError as e:
        meta["sources"].append("activity feed unavailable (%s)" % e)
    rng.shuffle(feed_ids)
    lookup_users(api, feed_ids[:FIREHOSE_USERS], MAX_SOURCES - len(found), found, "activity")
    # fallback: random account ids (older-account bias)
    if len(found) < MAX_SOURCES // 2:
        tries = 0
        while len(found) < MAX_SOURCES // 2 and tries < 20:
            tries += 1
            ids = [rng.randint(2_000_000, 20_000_000) for _ in range(50)]
            lookup_users(api, ids, MAX_SOURCES, found, "random-id")
        meta["sources"].append("random-id fallback: %d lookups" % tries)
    players = list(found.values())
    rng.shuffle(players)
    taste = [p for p in players if p["source"] == "taste"][:MAX_TASTE_SOURCES]
    rest = [p for p in players if p["source"] != "taste"]
    return (taste + rest)[:MAX_SOURCES]


def style_of(a, bpm):
    aim, spd = a.get("aim_difficulty"), a.get("speed_difficulty")
    if not aim or not spd:
        return "unknown", 1.0
    r = spd / aim
    if r < 0.85:
        return "aim", 1.15
    if r > 0.97 or (bpm and bpm >= 210 and r > 0.9):
        return "speed", 0.85
    return "balanced", 1.0


def build_climb(api, user, top, model, seed, meta):
    uid, ppl = user["id"], user["pp"] or 1.0
    top_pps = [t["pp"] for t in top]
    mine = {t["beatmap_id"]: t["pp"] for t in top}
    lo, hi = model["current_stars"] - STAR_BELOW, model["current_stars"] + STAR_ABOVE
    meta["star_band"] = [r2(lo), r2(hi)]
    md5s, bids, paths = load_owned()
    groups = {}
    players = []
    try:
        players = sample_players(api, uid, ppl, top, seed, meta)
    except BudgetExceeded:
        meta["notes"].append("request budget ran out while sampling players")
    meta["source_players"] = len(players)
    if players:
        meta["source_pp_range"] = [r1(min(p["pp"] for p in players)), r1(max(p["pp"] for p in players))]
        rk = [p["rank"] for p in players if p.get("rank")]
        meta["source_rank_range"] = [min(rk), max(rk)] if rk else None
    for p in players:
        try:
            js = api.get("/users/%d/scores/best" % p["id"], {"mode": "osu", "limit": 100})
        except BudgetExceeded:
            meta["notes"].append("budget ran out after %d player top lists" % meta["players_fetched"])
            break
        except ApiError:
            continue
        meta["players_fetched"] += 1
        for s in js or []:
            n = norm_score(s)
            if not n["beatmap_id"] or n["pp"] is None or n["nm_stars"] is None:
                continue
            k = (n["beatmap_id"], core_key(n["mods"], n["rate"]))
            g = groups.setdefault(k, {"info": n, "scores": [], "users": set(), "modsets": {}})
            if p["id"] in g["users"]:
                continue
            g["users"].add(p["id"])
            g["scores"].append((n["pp"], n["acc"] or 0.9, n["misses"], p))
            ms = mods_str(n["mods"])
            g["modsets"][ms] = g["modsets"].get(ms, 0) + 1
    # pre-score
    cands = []
    for (bid, ck), g in groups.items():
        n = g["info"]
        est = rough_stars(n["nm_stars"], n["mods"], n["rate"])
        slack = 0.0 if ck == "NM" else 0.3        # NoMod/HD star ratings are exact already
        if not (lo - slack <= est <= hi + slack) or len(g["users"]) < 1:
            continue
        cands.append(score_candidate(g, est, None, model, top_pps, mine, md5s, bids, paths, ppl))
    # popular-map supplement when the sample is thin (no reference scores; star model only)
    if len(cands) < 15:
        try:
            cands += search_candidates(api, lo, hi, model, top_pps, mine, md5s, bids, paths, ppl, groups)
        except (ApiError, BudgetExceeded) as e:
            meta["notes"].append("map search skipped (%s)" % e)
    cands.sort(key=lambda c: -c["_prio"])
    seen, uniq = set(), []
    for c in cands:                      # one entry per beatmap (the best mod choice for you)
        if c["beatmap_id"] not in seen:
            seen.add(c["beatmap_id"])
            uniq.append(c)
    cands = uniq
    # exact star rating + aim/speed split for finalists (speed maps kept to a few)
    final, n_speed = [], 0
    for c in cands:
        if len(final) >= MAX_CLIMB or meta["attr_calls"] >= MAX_ATTR_CALLS:
            break
        try:
            a = beatmap_attrs(api, c["beatmap_id"], c["_mods"], c["_rate"])
            meta["attr_calls"] += 1
        except BudgetExceeded:
            meta["notes"].append("budget ran out during star-rating checks")
            break
        except ApiError:
            a = {}
        if a.get("star_rating"):
            c = rescore(c, a, model, top_pps, mine, ppl)
        if not (lo <= c["stars"] <= hi):
            continue
        if c["style"] == "speed":
            if n_speed >= 4:
                continue
            n_speed += 1
        if c["est_gain_pp"] < 0.1:
            continue
        final.append(c)
    # owned first, then by priority (gain x style fit x evidence)
    final.sort(key=lambda c: (not c["owned"], -c["_prio"]))
    for c in final:
        c["reason"] = make_reason(c, model, meta)
        c["rate"] = round(float(c.get("_rate") or 1.0), 3)   # lazer custom speed (DT 1.2x etc.), not just "DT"
        for k in [k for k in c if k.startswith("_")]:
            del c[k]
    return final


def _owned(n, md5s, bids):
    return bool((n.get("md5") and n["md5"] in md5s) or (n.get("beatmap_id") in bids))


def score_candidate(g, stars, attrs, model, top_pps, mine, md5s, bids, paths, ppl):
    n = g["info"]
    sc = g["scores"]
    mods = max(g["modsets"], key=g["modsets"].get) if g["modsets"] else mods_str(n["mods"])
    pps = [s[0] for s in sc]
    accs = [s[1] for s in sc]
    ref_ss = statistics.median(ss_equiv(s[0], s[1], s[2]) for s in sc)
    c = {
        "beatmap_id": n["beatmap_id"], "set_id": n["set_id"], "md5": n["md5"],
        "artist": n["artist"], "title": n["title"], "version": n["version"],
        "stars": r2(stars), "mods": mods, "typical_pp": r1(statistics.median(pps)),
        "typical_acc": round(statistics.median(accs), 4), "source_players": len(sc),
        "owned": _owned(n, md5s, bids),
        "_mods": n["mods"] if mods_str(n["mods"]) == mods else [m for m in _split(mods)],
        "_rate": n["rate"], "_ref_ss": ref_ss, "_n": n,
        "_src_pp": [s[3]["pp"] for s in sc], "_src_rank": [s[3].get("rank") for s in sc if s[3].get("rank")],
        "_path": paths.get(n["md5"]),
    }
    _fill_stats(c, n)
    return _predict(c, attrs or {}, model, top_pps, mine, ppl)


def _split(ms):
    return [] if ms == "NM" else [ms[i:i + 2] for i in range(0, len(ms), 2)]


def _fill_stats(c, n, attrs=None):
    bpm, ar, od, cs, ln = adjust_stats(float(n["bpm"] or 0), float(n["ar"] or 0), float(n["od"] or 0),
                                       float(n["cs"] or 0), float(n["length"] or 0), c["_mods"], c["_rate"])
    if attrs:
        ar = attrs.get("approach_rate", ar)
        od = attrs.get("overall_difficulty", od)
    c.update({"bpm": int(round(bpm)) if bpm else None, "ar": r1(ar), "od": r1(od), "cs": r1(cs),
              "length_s": int(round(ln)) if ln else None})


def _predict(c, attrs, model, top_pps, mine, ppl):
    stars = c["stars"]
    acc, miss = predict_good_run(stars, model, (c.get("_n") or {}).get("n_objects"))
    ss = c["_ref_ss"] if c["_ref_ss"] else model["k_ss"] * stars ** 3
    pred = pp_at(ss, acc, miss)
    basis, conf = ("coarse model: your good-run acc/misses at %.1f* (%s)"
                   % (stars, "from your local replays" if model.get("local") else "from your top plays")), "low"
    pl = try_pp_live() if c.get("owned") else None
    if pl is not None:
        try:
            if not c.get("_path") and c.get("md5"):
                c["_path"] = pl.osu_path_for_md5(c["md5"])
            if c.get("_path") and tosu_ok(pl):
                pr = pl.predict(c["_path"], [{"acronym": m} for m in c["_mods"]], c["_rate"], stars)
                if pr and pr.get("pp_good_run"):
                    pred, conf = float(pr["pp_good_run"]), pr.get("confidence") or "medium"
                    acc, miss = pr.get("acc_good") or acc, pr.get("misses_good") or miss
                    basis = "tosu pp calculation + your skill model"
        except Exception:
            pass
    gain = weighted_gain(top_pps, pred, mine.get(c["beatmap_id"]))
    style, sf = style_of(attrs, c.get("bpm"))
    support = min(1.0, 0.55 + 0.12 * c["source_players"]) if c["source_players"] else 0.5
    d = stars - model["current_stars"]
    feas = 1.0 / (1.0 + 1.5 * max(0.0, d))     # harder maps need many more attempts for that good run
    rate_f = 0.85 if c["_rate"] > 1.0 + 1e-6 else 1.0   # DT: faster tapping + AR10ish reading = his weak side
    c.update({"pp_predicted": r1(pred), "est_gain_pp": r1(gain), "pred_acc": round(acc, 4),
              "pred_misses": r1(miss), "confidence": conf, "pp_basis": basis, "style": style,
              "reach": "stretch" if d > 0.35 else ("at your level" if d > -0.1 else "comfortable"),
              "_prio": gain * sf * support * feas * rate_f})
    if attrs:
        c["aim"] = r2(attrs.get("aim_difficulty"))
        c["speed"] = r2(attrs.get("speed_difficulty"))
    return c


def rescore(c, a, model, top_pps, mine, ppl):
    c["stars"] = r2(a["star_rating"])
    if c.get("_n"):
        _fill_stats(c, c["_n"], a)
    return _predict(c, a, model, top_pps, mine, ppl)


def search_candidates(api, lo, hi, model, top_pps, mine, md5s, bids, paths, ppl, groups):
    out = []
    q = "stars>=%.1f stars<=%.1f" % (lo, hi)
    js = api.get("/beatmapsets/search", {"m": 0, "s": "ranked", "sort": "plays_desc", "q": q})
    for bs in (js or {}).get("beatmapsets") or []:
        for bm in bs.get("beatmaps") or []:
            if bm.get("mode_int", 0) != 0 or not bm.get("difficulty_rating"):
                continue
            if not (lo <= bm["difficulty_rating"] <= hi) or (bm["id"], "NM") in groups:
                continue
            n = norm_score({"beatmap": dict(bm, beatmapset_id=bs.get("id")), "beatmapset": bs, "mods": []})
            c = {"beatmap_id": n["beatmap_id"], "set_id": n["set_id"], "md5": n["md5"],
                 "artist": n["artist"], "title": n["title"], "version": n["version"],
                 "stars": r2(bm["difficulty_rating"]), "mods": "NM", "typical_pp": None,
                 "typical_acc": None, "source_players": 0, "owned": _owned(n, md5s, bids),
                 "_mods": [], "_rate": 1.0, "_ref_ss": None, "_n": n, "_src_pp": [], "_src_rank": [],
                 "_path": paths.get(n["md5"]), "_search": True}
            _fill_stats(c, n)
            out.append(_predict(c, {}, model, top_pps, mine, ppl))
    return out


def make_reason(c, model, meta):
    parts = []
    if c["source_players"]:
        rk = sorted(c.get("_src_rank") or [])
        rtxt = (" (ranks ~%dk-%dk)" % (rk[0] // 1000, rk[-1] // 1000)) if rk else ""
        parts.append("%d of %d sampled players at %d-%dpp%s have this in their top 100 (median %s pp at %.1f%%)"
                     % (c["source_players"], meta["source_players"], min(c["_src_pp"]), max(c["_src_pp"]),
                        rtxt, c["typical_pp"], 100 * c["typical_acc"]))
    else:
        parts.append("Popular ranked map in your %.1f-%.1f* band (no sampled-player scores; star-rating estimate only)"
                     % tuple(meta["star_band"]))
    if c["style"] == "aim":
        parts.append("aim-leaning - suits your jump aim")
    elif c["style"] == "speed":
        sb = model.get("stream_break_bpm")
        parts.append("speed-heavy - a speed-base target%s; practise sections at 0.8-0.9x first"
                     % ((", BPM %s vs your stream break ~%s" % (c.get("bpm"), sb)) if sb else ""))
    elif c["style"] == "balanced":
        parts.append("balanced aim/speed")
    if "DT" in c["mods"]:
        parts.append("DT means faster tapping and higher AR - expect more attempts")
    parts.append({"stretch": "a stretch for you right now", "at your level": "at your current level",
                  "comfortable": "comfortable level - aim for high accuracy"}[c["reach"]])
    parts.append("good run for you ~%.1f%% %d miss%s -> ~%s pp, +%s pp total (%s confidence)"
                 % (100 * c["pred_acc"], round(c["pred_misses"]), "" if round(c["pred_misses"]) == 1 else "es",
                    c["pp_predicted"], c["est_gain_pp"], c["confidence"]))
    parts.append("you own it" if c["owned"] else "not downloaded")
    return "; ".join(parts) + "."


# ----------------------------------------------------------------------------
# refresh
# ----------------------------------------------------------------------------

def empty(prev=None):
    prev = prev or {}
    return {"version": 1, "generated_at": int(time.time()), "configured": False, "error": None,
            "user": prev.get("user"), "history": prev.get("history") or [],
            "top_plays": prev.get("top_plays") or [], "pp_threshold": prev.get("pp_threshold") or {},
            "climb": prev.get("climb") or [], "climb_meta": prev.get("climb_meta")}


def load_config():
    cfg = load_json(CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        return None, ("osu! API not set up: copy osu_api_config.example.json to osu_api_config.json and fill in "
                      "client_id and client_secret (osu! website > Settings > OAuth > New OAuth Application; "
                      "callback URL can be http://localhost) - see README, \"Set up the osu! API\". Keep the "
                      "secret private." + (" Note: this folder is synced by OneDrive, so the secret and the "
                      "short-lived token file (.osu_api_token.json) are uploaded there too; move the coach folder "
                      "out of OneDrive if you don't want that." if "onedrive" in str(DATA_DIR).lower() else ""))
    if not str(cfg.get("client_id") or "").strip().isdigit() or not cfg.get("client_secret") \
            or "PASTE" in str(cfg.get("client_secret")).upper():
        return None, ("osu_api_config.json is incomplete: client_id must be a number and client_secret filled in "
                      "(see README, \"Set up the osu! API\").")
    return cfg, None


def snapshot_history(hist, user, now=None):
    d = today_str(now)
    snap = {"date": d, "pp": user.get("pp"), "global_rank": user.get("global_rank"),
            "country_rank": user.get("country_rank"), "accuracy": user.get("accuracy")}
    hist = [h for h in (hist or []) if isinstance(h, dict) and h.get("date")]
    if hist and hist[-1]["date"] == d:
        hist[-1] = snap            # one per day: keep the latest of today
    else:
        hist = [h for h in hist if h["date"] != d] + [snap]
    hist.sort(key=lambda h: h["date"])
    return hist


def refresh(force=False, climb_only=False):
    now = time.time()
    prev = load_json(OUT_FILE, {}) or {}
    cfg, err = load_config()
    if cfg is None:
        out = empty(prev)
        out["error"] = err
        atomic_write_json(OUT_FILE, out)
        return out
    pm = prev.get("climb_meta") or {}
    fresh_profile = (prev.get("configured") and not prev.get("error") and prev.get("user")
                     and now - float(prev.get("generated_at") or 0) < PROFILE_TTL)
    fresh_climb = now - float(pm.get("generated_at") or 0) < CLIMB_TTL and prev.get("climb") is not None
    if not force and not climb_only and fresh_profile:
        return prev
    if network_disabled() and not _is_local(BASE):
        # offline run: report it, and leave pp_data.json exactly as it is
        out = dict(prev)
        out["configured"] = True
        out["error"] = "network disabled (OSU_COACH_NO_NETWORK)"
        return out
    api = Api(cfg)
    out = empty(prev)
    out["configured"] = True
    try:
        who = _identity.api_user(_identity.load_identity(CONFIG_FILE))
        if not who:
            raise ApiError("I don't know which osu! account is yours yet: put your osu! username (or user id) in "
                           "osu_api_config.json as \"user\", or log in to osu! once (see README, "
                           "\"Set up the osu! API\").")
        user = fetch_user(api, who)
        out["user"] = user
        out["history"] = snapshot_history(prev.get("history"), user, now)
        top = fetch_top(api, user["id"])
        out["top_plays"] = [top_public(t) for t in top]
        out["pp_threshold"] = thresholds([t["pp"] for t in top])
        model = player_model(top, now)
        if climb_only or not fresh_climb:   # --force refreshes the profile; --climb-only rebuilds the list
            meta = {"generated_at": int(now), "method": "sampled players at %.1f-%.1fx your pp; their top 100s "
                    "re-scored for your level" % (BAND_LO, BAND_HI), "sources": [], "notes": [],
                    "players_fetched": 0, "attr_calls": 0, "model": model,
                    "bias": ("Activity sample favours people who play a lot right now; the taste sample (global "
                             "top-50 leaderboards of your best maps, <=%d players) usually finds almost nobody in "
                             "your pp band; random-id fallback favours older accounts; top-100 lists favour "
                             "'farm' maps (high pp for their difficulty). pp_predicted is a good run (~your best "
                             "1 in 5 attempts) from your current local replays, coarse unless tosu is running and "
                             "you own the map." % MAX_TASTE_SOURCES)}
            seed = int(hashlib.md5(today_str(now).encode()).hexdigest()[:8], 16)
            out["climb"] = build_climb(api, user, top, model, seed, meta)
            meta["requests"] = api.count
            out["climb_meta"] = meta
        else:
            # top plays changed -> recompute gains cheaply, no requests
            pps = [t["pp"] for t in top]
            mine = {t["beatmap_id"]: t["pp"] for t in top}
            for c in out["climb"]:
                c["est_gain_pp"] = r1(weighted_gain(pps, c.get("pp_predicted"), mine.get(c.get("beatmap_id"))))
            out["climb"] = [c for c in out["climb"] if (c.get("est_gain_pp") or 0) >= 0.1]
    except BudgetExceeded as e:
        out["error"] = "partial refresh: %s" % e
    except ApiError as e:
        out["error"] = str(e)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        out["error"] = "network error: %s" % getattr(e, "reason", e)
    except Exception as e:  # never crash the pet's refresh
        out["error"] = "unexpected error: %s: %s" % (type(e).__name__, e)
    out["generated_at"] = int(time.time())
    out["requests"] = api.count
    atomic_write_json(OUT_FILE, out)
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="osu! API v2 -> pp_data.json")
    ap.add_argument("--force", action="store_true", help="ignore the 30 min profile refresh limit")
    ap.add_argument("--climb-only", action="store_true", help="rebuild the climb list now (ignores its 24 h limit)")
    a = ap.parse_args(argv)
    d = refresh(force=a.force, climb_only=a.climb_only)
    if not d.get("configured"):
        print("not configured: %s" % d.get("error"))
        return 1
    if d.get("error") and not d.get("user"):
        print("error: %s" % d["error"])
        return 2
    u = d.get("user") or {}
    print("%s: %s pp, rank #%s, acc %s%% (%d requests)" % (u.get("username"), u.get("pp"), u.get("global_rank"),
                                                          u.get("accuracy"), d.get("requests", 0)))
    th = d.get("pp_threshold") or {}
    print("top play %s pp, #50 %s pp, #100 %s pp" % (th.get("top1"), th.get("top50"), th.get("top100")))
    for c in (d.get("climb") or [])[:8]:
        print("  +%5.1f pp  %-4s %.2f*  %s - %s [%s]%s" % (c["est_gain_pp"], c["mods"], c["stars"], c["artist"],
                                                           c["title"], c["version"], "  (owned)" if c["owned"] else ""))
    if d.get("error"):
        print("error: %s" % d["error"])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
