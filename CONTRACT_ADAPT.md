# Contract v3: adaptive training loop

Problem: the coach didn't feel reactive or adaptive enough - the same type of goal day after day with a
different song, and no adaptive advice on the spot. Example: accuracy step goal 95%, the player got 77% -
the coach said nothing and nothing changed.

Goal: a closed loop. **Every attempt is judged against the step's goal, the user gets a debrief within seconds,
and the next action (same map again / slower / faster / next map / next step / break) changes because of it.**
Day-to-day plans change because each skill has a difficulty level that moves with results.

## Files
| file | owner | role |
|---|---|---|
| `coach_adapt.py` (new) | adapt builder | skill tracks + staircases, session state, attempt judging, debriefs, adaptive plan inputs |
| `coach_engine.py` | adapt builder | calls coach_adapt on every run; builds session_plan/recommendations from track levels |
| `test_adapt_sim.py` (new) | adapt builder | replays the player's real history day by day + synthetic scenarios |
| `pp_live.py` | live builder | live play telemetry from tosu -> `live_attempts.jsonl` (incl. fails/quits, which have no replay) |
| `pet.pyw`, `pet_panel.py`, `pet_util.py`, `Play osu with Coach.bat` | pet builder | debrief UI, card follows next_action, "what changed", tosu path outside OneDrive |

## Skill tracks (`training_state.json`, written only by coach_adapt, atomic)
Tracks: `accuracy`, `speed`, `aim`, `stamina`, `reading`, `push`. Each:
```jsonc
{"level": 0.85,              // the difficulty knob: speed = playback rate on the chosen stream maps OR effective BPM;
                             // accuracy = map difficulty band (stars or density/aim-velocity ratio) at a fixed acc goal;
                             // aim = jump velocity ratio vs comfort; stamina = drain minutes; reading = AR; push = stars
 "unit": "x" | "BPM" | "★" | "min" | "AR",
 "step": 0.05, "min": 0.6, "max": 1.3,
 "streak": {"pass": 1, "fail": 0},
 "rule": "2-pass-up/1-fail-down",   // transformed up-down staircase (Levitt 1971): converges near ~71% success,
                                     // which matches the challenge-point idea - hard but mostly doable
 "history": [{"ts": 0, "md5": "", "outcome": "pass"|"close"|"fail", "level": 0.85, "metrics": {}}],
 "last_change": {"ts": 0, "from": 0.80, "to": 0.85, "why": "Cleared streams twice in a row at 0.80x"}}
```
Pass criteria per track must be concrete and measurable from a replay or live attempt, e.g.
speed: stream-note miss rate <= 3% AND stream drift <= 15 ms; accuracy: acc >= goal AND UR <= step target;
aim: jump miss rate <= 2-3%; stamina: last-third miss rate not > 1.3x first third; push: pass with <= N misses
(fail = failed/quit or >> N). "close" = within ~half the margin (no level change, streak kept).
Levels move only on the track's own attempts; a bad day cannot drop a level by more than one step per session.

## Session (`session.json`, coach_adapt): today's run-time state
```jsonc
{"date": "2026-11-09", "started": 0, "step_index": 2, "steps": [ {"block": "Speed", "track": "speed",
  "minutes": 12, "state": "active"|"done"|"skipped", "attempts": 3, "passes": 1, "pick_md5": "...",
  "rate": 0.85, "mods": "HT", "goal": "...", "exit_rule": "2 passes or 12 min"} ],
 "fatigue": {"ur_trend": +25, "flag": false}, "tilt": {"fails_in_row": 0, "flag": false}}
```
Steps auto-advance when their exit rule is met (pass target or time budget). The pet's Done/Skip buttons write
`session_cmd.json` `{"cmd": "done"|"skip"|"back"|"restart", "ts": 0}` which coach_adapt consumes on its next run
(pet triggers an engine run right after writing it). Old pet_settings "checks"/"card_pick" become a fallback only.

## New coach_data.json keys
```jsonc
"session": { ...session.json as above... ,
  "current": {"block": "Speed", "track": "speed", "map": {recommendation object}, "rate": 0.85, "mods": "HT",
              "goal": "No stream misses and no late drift", "attempt_n": 3, "passes": 1, "needed": 2}},
"last_debrief": {            // ALWAYS about the newest attempt (replay or live), null if none today
  "ts": 0, "source": "replay"|"live", "map": "...", "md5": "...", "mods": "HT", "rate": 0.85,
  "block": "Speed", "track": "speed",
  "verdict": "pass"|"close"|"fail"|"free-play",
  "headline": "Streams clean at 0.85x - that's 2 in a row!",          // <= 80 chars
  "observations": ["You drifted 22 ms late in the 1:12 stream (last try: 35 ms - better)",
                   "6 of 8 misses were at 1:40-1:52: wide jumps after the stream"],   // 1-3, specific, with m:ss
  "compare": {"vs": "last attempt on this map" | "your average for this step", "acc": "+3.1%", "ur": "-22", "misses": "-4"},
  "next_action": {"type": "retry"|"faster"|"slower"|"next_map"|"next_step"|"break"|"stop",
                  "text": "Go up to 0.90x on the same map", "rate": 0.90, "md5": "...", "why": "..."},
  "level_change": {"track": "speed", "from": 0.85, "to": 0.90} | null},
"debriefs_today": [ ...last_debrief objects, newest first, max 30 ],
"changes": ["Speed level 0.80x -> 0.85x (cleared twice yesterday)", "Accuracy: dropped to easier maps - 77% on Artist - Title"],
"tracks": {"speed": {"level": 0.85, "unit": "x", "label": "Stream practice speed", "trend": "up"|"flat"|"down",
                     "next": "Clear twice more to reach 0.90x"}, ...}
```
`session_plan` stays for compatibility but is now derived from `session.steps`; block minutes are weighted by
each track's gap and trend (more time on what's lagging or slipping, maintenance on what's improving),
and every block goal is generated from the track level - never fixed boilerplate.

## On-the-spot rules (coach_adapt, evaluated after every attempt)
- Debrief every attempt within one engine run (target < 3 s after the replay/live attempt lands).
- Fatigue: UR of the last 3 attempts >= 15% worse than the session's first 3, or session > plan length -> next_action break/stop.
- Tilt: 3 fails/quits in a row on any track -> next_action slower/easier map with a calm note.
- Same map played as the card's pick -> judged against the step; any other map -> "free-play" debrief with the
  same observations, plus "Want this to count? It's not today's step" (never silently ignored).
- Section-level: name the m:ss ranges where misses/late taps clustered and the pattern type.
- Weekly benchmark: once a week (or after 7 sessions) the plan includes a fixed "check-up" map set (same maps
  each time) to measure progress objectively; results go into tracks + trends.

## live_attempts.jsonl (pp_live.LiveWatcher, append-only, one JSON per attempt)
`{"ts", "md5", "beatmap_id", "artist","title","version", "mods", "rate", "stars", "outcome": "passed"|"failed"|"quit",
  "time_ms": 71234, "first_ms", "last_ms", "progress": 0.27, "acc", "hits": {"300","100","50","0"}, "max_combo",
  "ur", "hit_errors": [ms...] (cap 2000), "miss_times_ms": [..] (from hits['0'] increments vs beatmap.time.live)}`
Written when tosu's state leaves `play` (results = passed; back to song select with play.failed or mid-map = failed/quit).
Real tosu v2 snapshot (captured during the user's play): state {"number": 2, "name": "play"};
play keys: failed, playerName, mode, score, accuracy, healthBar, hits, hitErrorArray, combo, mods {checksum, number,
name, array, rate}, rank, pp, unstableRate; beatmap.time {live, firstObject, lastObject, mp3Length};
beatmap.status {"number": 4, "name": "ranked"}. Full sample: see scratchpad tosu_v2_sample.json path in your prompt.
coach_adapt treats a live attempt and a replay of the same play (same md5, ts within ~30 s) as one attempt,
preferring the replay's richer data.

## Extensions (v3.1, after the adaptivity / live-runtime / UX reviews)
- **Accuracy knob is OD, not stars** (`training_state.json` v2; a v1 state is re-bootstrapped, the weekly check-up
  maps and results are kept). Unit `OD`, step 0.5, min 2.0, max 10.0. 95% with no misses needs
  UR <= 10 x (80 - 6 x OD) / 1.78 (`ur95`). Pass = acc >= 95% (no separate UR bar), close = >= 93%. Maps: OD as
  played within +-0.35 of the level (NoMod, else 0.95/0.9/0.85x), stars in a band below the push level
  (`acc_band`: push - 1.8 .. push - 0.7). Start: the OD where the player's median UR on maps up to the push level x 1.05
  gives 95%.
- **Level-move limits**: per track and day at most one step up and one down, session and morning moves counted
  together (the morning seeds its caps from the day's recorded changes). A pass clearly above the level moves
  one step, never two. Bootstrap uses the same caps.
- **Free plays (morning review only)**: a map above the push level only counts for push; accuracy only on maps
  in the accuracy band (+0.3*); speed only on streams >= 110 BPM; stamina only up to band + 0.5*. A free fail
  far below the level (< L - 1.5 steps) says nothing. Free plays need 2 fails at/near the level on one day to drop
  and 2 passes at/above the level to rise (no instant rise); one map counts once per day (its last try); a pass on
  a map played 3+ times before (memorised) doesn't count.
- **Warm-up** is never judged and always ends up in `counted`; any map up to the push level counts as a warm-up
  play; the warm-up is marked done after 2 plays or once 8 active minutes were played (note "warmed up on your
  own maps").
- **Session step states**: `pending`, `active`, `done`, `skipped`. Playing a later step's map jumps there and
  marks the steps jumped over `skipped` with note `"jumped ahead"`; `back` skips those.
- **Tilt** counts step attempts (not the warm-up / check-up) and free fails up to the push level. On tilt:
  `fails_in_row` restarts, the next 2 plays are easier (target `min(L, morning level - 2 steps)`), then a map at
  the level comes back (`tilt_left`).
- **Below-level pass**: headline says "below your L level: a good rep, not a level-up"; next action returns to a
  map/speed at the level.
- **Fatigue** compares UR relative to the player's usual UR for the map's star rating (linear fit over their history), push
  and above-push plays excluded. Over plan time with less than half the steps done and < 1.5x plan length:
  "Short on time: do X and Y only, then stop" instead of stop.
- **Check-up** is a measurement: verdict `"done"` (pet: "Recorded"), no level move, no tilt count, up to 2 tries
  per map (a clean first try is enough), best try recorded with a per-track `metric` (acc/UR; stream-miss % and
  drift for the speed map; jump-miss % for the aim map). `tracks[t].checkup` lists recent results;
  next_action may carry `bench_n` / `bench_of`.
- **Live attempts**: records carry `end` and `joined_late`. Not judged: a quit with < 6 s played, or `end ==
  "retry"` before 15% of the map / 25 s of play (dropped, no debrief); `end` in `tosu_lost`, `stopped`,
  `map_change` -> verdict `"not-counted"` (debrief, no level/tilt/try). A passed live attempt whose result (md5,
  misses, max combo, acc to 0.1%) repeats an earlier play's exactly, more than 45 s later, is a replay being
  watched and is dropped. pp_live: an empty checksum, a different map for a single poll, an unknown state or a
  single song-select read mid-map no longer split an attempt; a clock reset with stale counters starts the new
  try from a stale baseline.
- **Texts**: free-play note is `"Not today's step, so it won't change your levels now."`; names drop feat./TV
  Size/Cut Ver. and are cut at a word with "…"; " at 1.0x" is omitted. Contract wording correction: the 70.7%
  convergence holds for the plain 2-up/1-down rule; this coach uses a rule *based on* it (off-level and free-play
  extensions), so the exact figure is not claimed.
