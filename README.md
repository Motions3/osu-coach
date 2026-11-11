# Mochi, your osu! coach

A cute desktop pet that sits next to osu! and helps you gain PP.

- **Mochi the pet** lives in a small always-on-top window beside the game. Click it for your
  coaching card; right-click for the menu.
- **PP predictor that learns *your* play.** Hover a map in song select and Mochi tells you the PP
  you'd realistically get on it (with your mods and speed), not just the SS number. Every play you
  finish, fail or quit teaches it a bit more about you.
- **Rank-up picks.** A short list of maps that would actually raise your total PP, with the
  expected gain and your chance of passing.
- **Optional training coach.** "Open full coach" has a daily practice plan, a star-rating roadmap,
  your progress and trouble spots from replays, and technique tips (marked by how strong the evidence is).

> Screenshot goes here: add one to the repo (for example `docs/mochi.png`) and link it.

## Is it safe?

- Live game info comes from **tosu**, the same memory reader that stream overlays use. It only
  *reads*; nothing is injected into osu! and osu! itself is never modified.
- Replay files are read **read-only**. Mochi never writes into your osu! folders.
- The pet is its own window, so osu! updates don't break it.
- tosu is a third-party tool. It's widely used, but osu! doesn't officially endorse it.
  Everything except live PP in song select works without it.

## What you need

- **Windows 10 or 11**
- **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/). With the classic
  installer, tick **"Add python.exe to PATH"** (it's *off* by default) and keep **"tcl/tk and IDLE"**
  ticked. If python.org gives you the newer **Python install manager** instead, or you install Python
  from the **Microsoft Store**, there are no checkboxes: both include tkinter and the `py` command,
  and the coach's `.bat` files find that too. Nothing else to install: no `pip install` needed.
- **osu!lazer** (recommended) or osu!stable
- **tosu** (free) for live PP in song select: <https://github.com/tosuapp/tosu/releases>
- An **osu! account**, to connect the osu! website (free, 2 minutes, see below)

## Install (one time, about 10 minutes)

### 1. Get the files
Either click the green **Code** button on GitHub, then **Download ZIP**, and extract it somewhere
like `C:\osu coach`; or, if you use git:

```
git clone <this repo's URL> "C:\osu coach"
```

Any folder works. If you can, pick one that isn't synced by OneDrive, so your API secret stays
on your PC.

### 2. Install tosu
1. Download the Windows zip from <https://github.com/tosuapp/tosu/releases> (only from there).
2. Extract it to `%LOCALAPPDATA%\tosu`, so you end up with `%LOCALAPPDATA%\tosu\tosu.exe`.
   (Paste `%LOCALAPPDATA%` into the File Explorer address bar to open that folder.)
   If you see another folder inside `tosu` instead of `tosu.exe`, move that folder's contents up
   one level.
3. **Don't put tosu inside OneDrive.** It refuses to start from there.
4. Double-click `tosu.exe` once yourself and allow any Windows prompt (SmartScreen: **More info >
   Run anyway**; Firewall: **Allow**). The launcher starts tosu minimised, so a prompt it triggers
   is easy to miss.

The launcher starts tosu for you from then on.

### 3. Set up the osu! API
This lets Mochi see your profile, total PP and top plays. It only uses read-only access.

1. Go to <https://osu.ppy.sh/home/account/edit>, scroll to **OAuth**, and click
   **New OAuth Application**.
2. Use any name (for example "Mochi"). For the callback URL, enter `http://localhost`.
   Click **Register application**.
3. In the coach folder, copy `osu_api_config.example.json` and name the copy
   `osu_api_config.json`.
4. Open `osu_api_config.json` in Notepad and fill in:
   - `client_id`: the **Client ID** number from the osu! website
   - `client_secret`: the **Client Secret** from the osu! website
   - `user`: **your** osu! username (or your user id number)
5. Save it.

**Never share `osu_api_config.json` or post it anywhere.** The secret works like a password for
that OAuth app. (It's git-ignored, so it won't be uploaded if you use git. If it ever leaks, delete
the app on the osu! website and make a new one.)

### 4. Check everything
Double-click **`Check setup.bat`** in the coach folder. (Or open a command prompt there, by clicking
the folder's address bar, typing `cmd` and pressing Enter, and run `python setup_check.py`, or
`py setup_check.py` if `python` isn't found.)

It prints a checklist: Python, tkinter, osu!, tosu, your API config, and who the coach thinks you
are. Fix anything marked as a problem, then run it again.

### 5. Play
Double-click **`Play osu with Coach.bat`**. It starts tosu, then osu!, then Mochi.
Mochi closes when osu! closes. Use this every time you play.

## Using Mochi

- **Hover a map in song select:** Mochi shows the PP it expects for you, a range for a good run,
  and your pass chance. Change mods or speed and it updates.
- **After a play:** you'll see what you got versus what Mochi predicted, and how your total PP moved.
- **Click Mochi** for the PP card: your total PP and rank, how accurate the predictions have been
  lately, and your **Rank-up picks**. Use **Copy search** to paste a pick into song select.
- **Open full coach** (on the card, or right-click Mochi) for the training side, with the tabs
  PP, Today, Progress, Tips and Settings. Answer the setup questions in **Settings** once (mouse or
  tablet, keyboard, tapping style) so the advice fits you.
- **Right-click Mochi** for refresh, hide and quit.
- `Run osu coach.bat` writes a text report on your last 40 replays and opens it in Notepad.

## How the PP predictor learns

- It predicts your accuracy, misses and pass chance from your own recent plays, then turns that into
  PP with the same calculator the game uses.
- Before each play it logs its guess, and afterwards it checks itself and learns from the result,
  including fails and quits (those need tosu running).
- It needs roughly **10-20 plays** before the numbers get good. Until then it's cautious and says
  it's unsure.
- It tells you **how accurate it has been lately** ("my last 20 predictions were within ±X pp"),
  so you can see how far to trust it.
- Recent plays count most, so it keeps up as you improve.

## Troubleshooting

**Python wasn't found.** Re-run the python.org installer and tick "Add python.exe to PATH", or
install it fresh. Then close and reopen any command prompts.

**"Your Python is missing tkinter".** Run the python.org installer again, choose **Modify**, and
tick **tcl/tk and IDLE**.

**tosu is inside OneDrive / tosu won't start.** Move the tosu folder to `%LOCALAPPDATA%\tosu`.
Or use Mochi: right-click > Open full coach > Settings > **Move tosu out of OneDrive**.

**No PP shows when I hover maps.**
- Is tosu running? Look for `tosu.exe` in Task Manager. Start the game with
  `Play osu with Coach.bat` so tosu starts first.
- If you start osu! yourself, start tosu first too.
- Fresh install: play a few maps so the model has something to learn from.

**My profile / total PP doesn't show.** Check `osu_api_config.json` (client id, secret, your
username) and double-click `Check setup.bat`.

**Mochi isn't on top of the game.** Run osu! in **borderless** or windowed mode. Exclusive
fullscreen draws over every other window.

**The coach finds no replays, but I play osu!lazer.** If you moved lazer's data folder (lazer
Settings > Maintenance > change folder location), the coach follows the `storage.ini` lazer leaves
in `%APPDATA%\osu`. `Check setup.bat` shows which folder it found. For the text report you can also
run `python osu_coach.py --osu-dir "D:\path\to\osu"`.

**osu! wasn't found.** The launcher looks for osu!lazer in `%LOCALAPPDATA%\osulazer` and osu!stable
in `%LOCALAPPDATA%\osu!`. If yours is elsewhere, start osu! yourself; Mochi appears once it's running.

## Privacy

- All your data stays **on your PC**: plays, predictions, caches, logs and settings are saved as
  files in the coach folder.
- The only internet traffic is to the official osu! API (osu.ppy.sh), and only once you've set it up.
- Every file that holds your data (`*.json` apart from the example config, `*.jsonl`, logs, the
  tosu folder, caches, your API config and token) is **git-ignored**, so it's never uploaded if you
  share or fork the code.

## License

MIT. See [LICENSE](LICENSE).
