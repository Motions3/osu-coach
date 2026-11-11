# Mochi, your osu! coach: start here

**New here? Read [README.md](README.md) first.** It covers what you need, installing tosu,
setting up the osu! API, and troubleshooting. This page is the short version.

## Every day
Double-click **Play osu with Coach.bat**. It starts tosu (if you've installed it),
then osu! (lazer, or stable if lazer isn't installed), then Mochi. Mochi closes when osu! closes.
Click Mochi for the PP card and rank-up picks. Right-click for the menu (refresh, hide, quit,
Open full coach).

Nothing is injected into osu!, so osu! updates never break the coach.

## One-time setup (about 10 minutes)
1. **Check your setup.** Double-click `Check setup.bat` (or run `python setup_check.py` / `py setup_check.py`). It lists anything missing.
2. **Connect the osu! website (PP tracking and rank-up picks).**
   - osu! website → your avatar → Settings → OAuth → New OAuth Application.
     Use any name; the callback URL can be `http://localhost`. Click Register.
   - Copy `osu_api_config.example.json` to `osu_api_config.json` (or in Mochi: Open full coach →
     Settings → **Open the account file**). Fill in the Client ID (a number), the Client Secret and your own
     osu! username, then save. Keep that file to yourself.
   - If this folder syncs to OneDrive, the secret syncs too. If you'd rather it didn't,
     move the whole folder somewhere like `C:\osu coach` first.
3. **Install tosu for the live PP bubble in song select.**
   - tosu is a third-party program that reads osu!'s memory. It's read-only and
     widely used, but osu! doesn't officially approve it.
   - Download the Windows zip only from https://github.com/tosuapp/tosu/releases
     and extract it to `%LOCALAPPDATA%\tosu` so you have `%LOCALAPPDATA%\tosu\tosu.exe`.
     Not inside OneDrive: tosu won't start from there. (Or use Open full coach →
     Settings → Choose tosu.exe.)
   - With tosu running, the coach also sees fails and quits and gets real star ratings
     for your plays, which makes PP predictions much more accurate.
4. **Answer the setup questions.** Open full coach → **Settings** tab: mouse or tablet,
   keyboard, and tapping style, so the training advice fits how you actually play.

## How to follow the training coach (optional)
- Do the **Today** plan in order. It's about 75 minutes including a 5-minute break.
  Mochi reminds you to rest after about 30 minutes.
- **Stop if your wrist, hand or forearm hurts.** Rest days count as training.
- Check the **Progress** tab (roadmap) weekly. You move up a star band when your replays
  show you've met its checkpoints, not on a schedule.
- Labels in **Tips** tell you how strong each tip's evidence is: research,
  community consensus, or coach heuristic.
- More background: [TRAINING.md](TRAINING.md).
