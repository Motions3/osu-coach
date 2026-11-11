# Contract v2 addendum: PP, live tosu, roadmap, techniques

Everything in CONTRACT.md stays valid. This adds modules and keys. All modules: Python stdlib only,
never write inside %APPDATA%\osu or the game folder, never touch the osu! process directly.

## Module ownership
| file | owner | role |
|---|---|---|
| `pp_live.py` | live builder | tosu client + star-rating cache + personal skill model + PP prediction |
| `osu_api.py` | api builder | osu! API v2 (client credentials): profile, PP history, top plays, climb list → `pp_data.json` |
| `coach_knowledge.py` | knowledge builder | static, fact-checked coaching content (techniques, roadmap bands, setup checks) |
| `coach_engine.py` | knowledge builder | merges everything into `coach_data.json` (new keys below) |
| `pet.pyw`, `pet_panel.py`, `pet_util.py` | pet builder | UI for all of it |

## pp_live.py public API
```python
TOSU = "http://127.0.0.1:24050"   # override with env OSU_COACH_TOSU_URL (tests use a mock server)

def tosu_available(timeout=0.4) -> bool
def calc_pp(osu_path: str|None, mods: list[dict]|int|None = None, clock_rate: float|None = None,
            acc: float|None = None, misses: int|None = None, combo: int|None = None,
            lazer: bool = True) -> dict|None
    # wraps GET /api/calculate/pp; returns {"pp","stars","aim","speed","max_combo","n_objects","ar","od","cs","bpm"} or None
def star_rating(md5: str, osu_path: str, mods_key: str, clock_rate: float) -> float|None
    # cached in .coach_sr_cache.json keyed "md5|mods_key|rate"; uses calc_pp
def skill_model(refresh=False) -> dict
    # fitted from .coach_replays.json (+ SR cache): {"n", "acc_at": {stars: acc}, "miss_rate_at": {...},
    #  "comfort_stars", "fit": {...}, "updated"}; cached in .coach_skill.json
def predict(osu_path: str, mods, clock_rate: float, stars: float|None = None) -> dict|None
    # {"stars","pp_ss","pp_fc_pred_acc","pp_expected","pp_good_run","acc_expected","misses_expected",
    #  "acc_good","misses_good","confidence": "low"|"medium"|"high","basis": str}
    # pp_expected = PP at your typical result on maps of this SR (median acc, median misses);
    # pp_good_run = your ~80th-percentile result; pp_ss = 100% FC.
class LiveWatcher:   # background thread, polls /json/v2 ~2x/s, never raises
    def __init__(self, on_change: callable)   # on_change(state: dict) called on Tk thread via pet's after()
    def start(self); def stop(self)
    # state dict: {"tosu_ok": bool, "screen": "song_select"|"playing"|"results"|"menu"|"other",
    #   "md5","beatmap_id","artist","title","version","mods": "HDDT","rate": 1.2,"stars": 4.62,
    #   "prediction": predict(...) | None, "results": {"pp","acc","misses","rank_gain_pp"} | None}
```

## pp_data.json (written by `python osu_api.py`, atomic; also `osu_api.refresh(force=False) -> dict`)
Config: `osu_api_config.json` `{"client_id": 12345, "client_secret": "...", "user": "YourName"}` (user fills it;
template `osu_api_config.example.json`). Base URL override env `OSU_COACH_API_URL` for tests.
Refresh at most every 30 min for profile, 24 h for climb list. Respect rate limits (<= 60 req/min).
```jsonc
{ "version": 1, "generated_at": 0, "configured": true, "error": null,
  "user": {"id": 1234567, "username": "PlayerName", "pp": 2000.0, "global_rank": 500000, "country_rank": 20000,
           "accuracy": 95.0, "play_count": 10000, "peak_rank": 200000, "peak_rank_date": "2020-01-01"},
  "history": [{"date": "2026-11-08", "pp": 2000.0, "global_rank": 500000, "accuracy": 95.0}],
  "top_plays": [{"beatmap_id": 0, "md5": "", "artist": "", "title": "", "version": "", "stars": 4.64,
                 "mods": "HD", "pp": 100.0, "acc": 0.98, "date": "2020-01-01"}],
  "pp_threshold": {"top1": 103.0, "top50": 49.0, "top100": 40.0},
  "climb": [{"beatmap_id": 0, "set_id": 0, "md5": "", "artist": "", "title": "", "version": "",
             "stars": 4.9, "bpm": 180, "ar": 9.3, "od": 8.5, "cs": 4.0, "length_s": 90, "mods": "HD",
             "typical_pp": 85.0, "typical_acc": 0.96, "source_players": 6, "owned": true,
             "pp_predicted": 70.0, "est_gain_pp": 14.2, "reason": "6 players ranked 400k-450k have this as a top play"}] }
```
`est_gain_pp` = exact weighted-sum gain (0.95^i weighting over top 100) if a play of that PP were added.

## New coach_data.json keys (written by coach_engine.py; pet must tolerate absence)
```jsonc
"phase": {"name": "re-sharpen" | "rebuild" | "plateau-break", "since": "2026-10-12", "why": "..."},
"roadmap": {"current_stars": 4.3, "basis": "...", "bands": [
   {"stars": 4.5, "status": "done"|"current"|"next"|"later",
    "checkpoints": [{"metric": "UR on 4.5* maps", "target": "<= 250", "current": "388", "met": false}],
    "focus": "...", "typical_maps": "..."}]},
"techniques": [   // ranked for THIS player, all entries from coach_knowledge
   {"id": "full-alt", "category": "tapping"|"timing"|"aim"|"reading"|"stamina"|"setup"|"mindset",
    "title": "...", "for_you": "why this matters given your numbers", "steps": ["..."],
    "mistakes": ["..."], "drill": "...", "evidence": "research"|"community consensus"|"coach heuristic",
    "source": "short citation or null"}],
"setup_checks": [{"id": "offset", "title": "...", "status": "ok"|"check"|"warn", "detail": "...", "how": "..."}],
"pp": {"configured": true, "pp": 2000.0, "rank": 500000, "gain_7d": 12.3, "next_goal": "...",
       "top50_floor": 49.0, "tosu": true},
"player_setup": {"device": "mouse"|"tablet"|null, "keys": "keyboard"|"keypad"|"mouse buttons"|null,
                 "style": "alternate"|"singletap"|"mixed"|null}   // copied from pet_settings.json "player_setup"
```
Recommendations gain `"purpose": "pp"` entries (from climb list, owned first) with extra optional fields
`"pp_expected"`, `"pp_good_run"`, `"pp_ss"`, `"est_gain_pp"`, `"stars"`.
