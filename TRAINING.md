# osu! coach: training guide

## How to use the coach

```
python osu_coach.py               # analyses your 15 most recent lazer plays
python osu_coach.py --last 40     # more plays = more reliable stats
```

Run it **once after each session**, not after every play (see "Feedback" below).
It saves `osu_coach_report.txt`. Keep a copy each week so you can compare
UR, stream break-point and jump miss % over time.

## What the evidence actually says

There's basically no peer-reviewed research on osu! itself. What follows comes
from motor-learning research, plus a note on how each finding applies to osu!.
Effect sizes in real-world skills are often smaller than in lab studies.

| Finding | Source | What it means for osu! |
|---|---|---|
| Practise at the right difficulty. Learning is predicted to be best when a task is hard but still mostly doable. | Guadagnoli & Lee 2004, *challenge point framework* (a theory with little direct testing) | Passing with 60% acc is too hard to learn from efficiently. Practise on maps where you can get about 90%+ and pass most attempts, and spend a smaller share of time pushing. |
| Targeted practice beats just playing, but it isn't everything. | Ericsson et al. 1993; Macnamara et al. 2014 meta-analysis (accumulated practice explained ~26% of variance in games, less in sports) | Have a specific goal for each block ("streams at 150 with no drift"). Expect individual differences in how fast you improve. Splitting practice blocks from score attempts is common coaching practice, not something these studies tested. |
| Rest between practice trials helps. | Lee & Genovese 1988 meta-analysis (mostly continuous tasks; a bigger effect on performance than on retention) | Short daily sessions with breaks rather than a 6-hour weekend grind. The 45–90 min/day and 5–6 days/week numbers are coach heuristics, not study results. |
| Sleep helps lock in motor sequences. | Walker et al. 2002 (finger tapping); later work (Rickard et al. 2008) found part of the effect was a time-of-day artefact | Improvement often shows up the next day. Don't judge a new skill on the same night you started it. |
| Constant feedback during practice may hurt retention. | Salmoni, Schmidt & Walter 1984, *guidance hypothesis*; a 2022 meta-analysis (McKay et al.) found no reliable benefit of reduced feedback | A sensible habit rather than a proven effect: review the report after a session and pick a focus, and you'll probably play better watching the notes than the hit-error bar. |
| Mixed practice gives slower gains in the session but better retention. | Shea & Morgan 1979; weaker effects in applied studies (Brady 2004) | Once a skill is OK, rotate jump, stream and reading maps instead of grinding one map 50 times. |
| Focusing outside your body may help. | Wulf 2013 review; a 2024 bias-corrected re-analysis (McKay et al.) found small or negligible effects | Evidence is mixed, but it's harmless: focus on the circle, the rhythm or the cursor path, not on your wrist or fingers. |
| Aim difficulty grows with distance relative to target size. | Fitts 1954 | Wider spacing and smaller circles both make jumps harder (it's their ratio that matters); with the time fixed by the map that shows up as more misses. Raising one thing at a time is community practice, so you can tell what caused new misses. |

## What the coach looks at in your replays

The numbers below are examples of what the report shows; yours will differ.

* **UR and timing bias.** Your overall unstable rate (UR) and whether you hit early or late on
  average, split into regular notes and streams. UR is often the biggest gap; the coach's roadmap
  targets (coach heuristic) are UR <= 180 at 5★, 150 at 6★ and 130 at 7★.
* **Why you miss.** Each miss is classed as timing (tapped off-time) or aim (tapped on time, cursor
  off the circle), and jump landing error is measured in circle radii, so you can tell wild aim
  from being slightly off-target on dense sections.
* **Streams.** Miss rate per BPM, the BPM where streams start to break down (only reported once
  there's a clear break point), and how far behind you drift by the end of long streams.
* **Challenge point.** How many plays you nearly FC'd and at what accuracy. If you rarely pass
  cleanly at 60-70% accuracy, you're practising above your challenge point.

## Weekly plan (about 60–75 min/day, 5–6 days/week)

| Block | Time | What |
|---|---|---|
| Warm-up | 10 min | Easy maps, 1.5★ below your level. Goal: 95%+ accuracy. |
| **Accuracy** | 15 min | Maps 1–1.5★ below your level, OD 7–8. Goal: lower UR, not pass. Retry until you hit 93–95%. |
| **Speed** | 10 min | The coach's speed picks (a 1.0x test at 150-160 BPM until your limit is measurable, then about 10 BPM below your break point). 30–60 s on, equal rest. +5 BPM when the coach shows drift under 10 ms. |
| Break | 5 min | After about 30-35 minutes: stand up, hands loose. Stop for the day if your wrist or hand hurts, tingles or feels numb. |
| Skill rotation | 15 min | Rotate daily: jumps, reading (AR +0.5), technical rhythms. Stamina work goes right after the warm-up, never at the end when you're tired. |
| Push | 10–15 min | Maps at your target level (+0.5★). Fine to fail; it's for exposure. |
| Review | 2 min | Run `osu_coach.py`, write down one focus for tomorrow. |

**When to move up:** when your accuracy block hits 95% routinely and your "push"
maps pass about 70% of the time, raise everything by about 0.3★.

**Before anything else**, check your offset - but don't just copy the overall early/late average
into it. If most of the lateness is in streams rather than on regular notes, that's a
tapping-speed problem, not an offset problem. Only a bias that
shows on regular, comfortable notes on every map is an offset issue; follow the pet's Setup
tab (offset check) for one careful calibration, and use lazer's offset calibration tool if
your version has it.

## Honest expectations

Going up several stars (say 4★ → 7★) is a big jump. For most regular players it takes many months to a couple
of years, and progress comes in plateaus. Nobody can promise a timeline. What you
can do is spend more of your practice on the thing that's actually holding you
back, and this tool tells you what that is.
