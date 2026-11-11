# coach_data.json contract (engine → pet)

`coach_engine.py` (CLI: `python coach_engine.py [--last N] [--out coach_data.json]`) writes
`coach_data.json` next to itself, atomically (write `coach_data.json.tmp`, then `os.replace`).
`pet.pyw` only READS this file and launches the engine as a subprocess
(`pythonw coach_engine.py`, cwd = script folder). Neither imports the other.

While the engine runs it writes `coach_status.json`: `{"state": "running"|"done"|"error", "message": str, "updated": unix_ts}`.

All strings are plain text for the UI (no markdown). Times are in ms unless the name says otherwise.
Every list may be empty; every optional field may be null. The pet must tolerate missing keys.

```jsonc
{
  "version": 1,
  "generated_at": 1762560000,          // unix seconds
  "player": "PlayerName",
  "n_plays": 40,                        // recent plays analysed
  "latest_play_ts": 1762550000,         // unix seconds of newest replay (pet uses it to detect "new play")

  "mood": "happy" | "neutral" | "worried" | "sleepy",   // pet expression
  "headline": "UR down 30 since last week - nice!",     // one short line for the speech bubble (<= 90 chars)
  "latest_play": {                      // the newest play, for the "you just played X" bubble; null if none
    "map": "Artist - Title [Insane]", "mods": "NM", "acc": 0.584, "misses": 36, "ur": 384,
    "comment": "27 of 36 misses were aim on jumps in the second half."   // <= 140 chars
  },

  "stats": {                            // ability profile (null when not enough data)
    "ur": 466.0, "mean_error_ms": 14.1, "avg_acc": 0.66, "near_fc_rate": 0.05,
    "stream_comfort_bpm": 140, "stream_break_bpm": 150, "stream_drift_ms": 32.2,
    "single_tap_ratio": 0.10,
    "jump_miss_rate": 0.063, "jump_landing_radii": -0.05, "jump_spike_spacing": 5,  // circle diameters
    "comfort_aim_velocity": 1.9,        // px/ms where jump miss% is still < 5%
    "typical_ar": 8.0, "typical_od": 6.0, "typical_bpm": 180
  },

  "focus": [                            // prioritised, max 5
    {"id": "ur", "title": "Accuracy / rhythm consistency", "priority": 1,
     "why": "Your UR is 466; 6-7* players sit around 100-180.",
     "drill": "15 min on maps well below your level aiming for 95% accuracy.",
     "metric": "UR", "current": 466, "target": 300}
  ],

  "session_plan": [                     // today's session, in order; minutes sum ~60-75
    {"block": "Warm-up", "minutes": 10, "goal": "95% acc, relaxed tapping", "pick_ids": ["<md5>", "..."]}
  ],

  "recommendations": [                  // local maps the player owns, max ~25
    {"id": "<beatmap md5>", "purpose": "accuracy" | "speed" | "aim" | "reading" | "stamina" | "push" | "warmup",
     "artist": "...", "title": "...", "version": "...", "creator": "...",
     "beatmap_id": 123456, "set_id": 45678,   // may be null; pet links https://osu.ppy.sh/b/<beatmap_id>
     "mods": "NM" | "HT" | "DT" | "HR" | "HD" | "EZ" | ...,
     "rate": 0.85,                      // lazer speed (HT/DT custom speed or Rate Adjust); 1.0 = normal
     "bpm": 180, "ar": 8.0, "od": 7.0, "cs": 4.0, "length_s": 95,
     "reason": "Long 150-BPM streams; at 0.9x that's 135 BPM, just under your break point.",
     "goal": "Pass with no stream misses, then raise to 0.95x.",
     "search": "artist title version"   // text to paste into lazer song-select search
    }
  ],

  "trouble_spots": [                    // sections of recent maps where misses cluster, max ~15
    {"map": "Artist - Title [Insane]", "id": "<md5>", "start_ms": 83000, "end_ms": 95000,
     "start": "1:23", "end": "1:35", "misses": 9, "kind": "jumps" | "streams" | "bursts" | "sliders" | "rhythm" | "reading",
     "reason": "aim - tapped in time but cursor off the circle",
     "advice": "Replay this section at 0.8x (HT custom speed) until clean, then 0.9x, then 1.0x.",
     "practice_rate": 0.8}
  ],

  "recent_plays": [                     // newest first, max 40
    {"ts": 1762550000, "map": "...", "id": "<md5>", "mods": "NM", "acc": 0.66, "misses": 12,
     "ur": 450, "max_combo": 300, "verdict": "too hard" | "good practice" | "too easy",
     "main_issue": "streams"}
  ],

  "trends": [                           // weekly buckets, oldest first, from ALL local replays
    {"week": "2026-10-26", "plays": 31, "ur": 480, "acc": 0.65, "misses_per_100": 7.1,
     "stream_break_bpm": 150}
  ],

  "notes": ["Slider bodies are not judged; star rating not computed (difficulty shown as BPM/AR/OD/spacing)."]
}
```
