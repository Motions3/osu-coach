# Contract v4: PP-first Mochi (self-learning PP predictor + rank-up recommender)

Goal: make the trainer a secondary feature (only shown in the full coach). The primary feature is an accurate PP
predictor - like the song-select PP readout, but self-learning on the player's own performance to estimate the PP
they would actually get - plus recommendations of maps they could realistically do well on to gain PP and rank up.

## Principles
- Predict the **player's outcome distribution** (pass chance, accuracy, misses, combo) for a map + mods + rate from
  their own history, then convert to PP with tosu's calculator (rosu-pp, same as the game). Never guess PP directly.
- **Self-learning**: every new attempt (replay or live, incl. fails/quits) is (1) scored against what the model
  predicted *before* the play (logged), then (2) added to training data. The model is refit after each attempt.
- **Honest accuracy**: time-ordered backtest (train on plays before t, predict play t) over the player's real
  history; report MAE/median error for PP, acc, misses, and calibration of the ranges. Show "my last 20 predictions
  were within ±X pp" in the UI. Recent plays weigh most (skill can change week to week, e.g. for returning players).
- Read-only on osu!/tosu folders. Stdlib only. GAME-SAFE: no GUI while osu!.exe runs.

## Files
| file | owner | role |
|---|---|---|
| `pp_model.py` (new) | model builder | dataset assembly, features, learner(s), backtest, calibration log, `predict_outcome`, `predict_pp` |
| `pp_recommend.py` (new) | recommender builder | candidate maps, mod variants, gain-to-total ranking -> `pp_recs.json` |
| `pp_live.py` | model builder | LiveWatcher hover/results use pp_model (replaces old skill_model path), logs predictions per attempt |
| `pet.pyw`, `pet_panel.py`, `pet_util.py` | pet builder | PP-first UI; trainer moved behind "Open full coach" |
| `coach_engine.py` | integrator only (minimal) | call pp_model.update + pp_recommend refresh after new attempts (or pet schedules them) |

## Training data (pp_model)
1. Local replays (`.coach_replays.json` + replay files via osu_coach): acc (lazer formula), misses, max combo,
   mods + rate, judged per-object details, timestamp. Player = the configured user plus local names sharing that user's uid
   (exclude any accounts listed in `exclude_users`, and RX/AP/AT/CN).
2. `live_attempts.jsonl` (tosu): passes AND fails/quits with progress -> pass/survival model.
3. osu! API (optional, via osu_api helpers, cached): recent scores incl. fails; best-100 only as weak, old prior
   (old plays get a heavy recency down-weight).
Map difficulty attributes per (md5, mods, rate) from tosu `/api/calculate/pp` (stars, aim, speed, sliderFactor,
speedNoteCount, ar, od, cs, maxCombo, nCircles, nSliders, bpm) cached in `.pp_attr_cache.json`; plus coach library
features (stream fraction/BPM, jump velocity, density, drain length).

## Model (pp_model.py) - pick by backtest
Compare >= 3 learners with a time-ordered backtest and keep the best (document the comparison):
e.g. recency-weighted k-NN in standardized attribute space; ridge regression on logit(acc) and log(1+misses)
with per-feature terms (aim, speed, od, ar, length, rate); quantile/residual model for ranges; logistic model
for P(pass) from live attempts. Output per query:
```python
predict_outcome(md5, mods, rate) -> {
  "p_pass": 0.82, "acc": {"p20": 0.92, "p50": 0.945, "p80": 0.962}, "misses": {"p20": 1, "p50": 4, "p80": 11},
  "combo_frac": {"p50": 0.35}, "n_similar": 23, "confidence": "high"|"medium"|"low", "basis": "plain text"}
predict_pp(md5, mods, rate, osu_path=None) -> {
  "pp_expected": 74.2,        # PP at the median outcome, given a pass
  "pp_low": 55.0, "pp_high": 96.0,   # ~20th/80th percentile outcome
  "pp_fc": 112.0, "pp_ss": 131.0,
  "p_pass": 0.82, "stars": 4.71,
  "gain_expected": 9.4,       # expected change in total pp: E[weighted_gain(pp)] * p_pass (beats-own-score aware)
  "gain_high": 18.1,
  "confidence": "...", "basis": "...", "model_error_pp": 6.3}   # recent backtest MAE near this difficulty
model_report() -> {"trained_on": n, "updated": ts, "learner": "...", "backtest": {"pp_mae": .., "acc_mae": ..,
  "miss_mae": .., "range_coverage": 0.61, "n": ..}, "recent": {"n": 20, "pp_mae": ..}}
update(force=False)  # ingest new attempts, log prediction-vs-actual for each new attempt, refit
```
Prediction log: `pp_predictions.jsonl` {ts, md5, mods, rate, predicted (snapshot taken at hover/attempt start or,
for attempts nobody hovered, the model as it was before the attempt), actual {pp, acc, misses, passed}}.

## Recommender (pp_recommend.py) -> pp_recs.json (atomic)
Candidates: owned local std maps (library, ranked/approved/loved status from tosu/API where known; unranked give
no pp - exclude), plus API-sourced maps (climb list, popular maps in the player's star band via beatmapsets
search). For maps not owned, the .osu text may be fetched from https://osu.ppy.sh/osu/{beatmap_id} (official,
plain text), cached in `.osu_cache/` (outside the osu! folder), max ~200 files/day, to compute attributes with tosu.
Mod variants per map: NM, HD, HR, HDHR, DT, HDDT (and lazer rate variants only if the model supports them).
Rank by `gain_expected` (and show gain_high, p_pass, confidence). Diversity: max 2 diffs per set; exclude maps where
the player's existing score already exceeds pp_high; refresh daily and after each session.
```jsonc
{"generated_at": 0, "total_pp": 2000.0, "rank": 500000, "model": {model_report()},
 "picks": [{"md5","beatmap_id","set_id","artist","title","version","mapper","owned": true, "status": "ranked",
   "mods": "HD", "rate": 1.0, "stars": 4.7, "length_s": 95, "bpm": 180, "ar","od","cs",
   "pp_expected","pp_low","pp_high","pp_fc","p_pass","gain_expected","gain_high","confidence",
   "why": "Jump-heavy 4.7* - your strongest pattern; you average 95% on similar maps", "search": "..."}],
 "by_mods_tip": "HD adds ~+4pp on your typical map; HR costs you ~-12% pass chance"}
```

## Pet UI (primary = PP)
- Left-click Mochi -> **PP card** (replaces "Next up" as default): header with total pp + rank (+ change today);
  the currently hovered map (tosu) with expected PP big, range low-high, FC/SS, pass chance, "+X pp to your total
  if you get it", best-mod tip for this map ("try HD: +5pp expected"); model accuracy line
  ("predictions within ±6pp lately"); then **Rank-up picks** (top 5, each: song, mods, expected pp, +gain, Copy
  search) and "More picks" -> full list. Small link "Open full coach (training)" -> existing panel (trainer).
- Hover bubble in song select: expected PP big + range + gain + pass chance; updates on mods/rate change.
- Results: "You got 83pp - I predicted 74 (within range)" + total pp change; the model learns from it.
- Trainer debriefs no longer pop up automatically by default (setting "Show training debriefs after plays", off);
  the PP results bubble is the default after-play feedback.
- Full panel gets a new first tab "PP" (rank-up list with sorting by gain / pass chance / stars, model accuracy
  panel with recent predicted-vs-actual chart), then the existing Today/Progress/Tips/Settings.
