# Running this on Windows

Everything is per-user. No admin rights are needed at any point.

## Two ways in

**A. The standalone build** - a zip containing `FlowBulkGenerator.exe` with
Python already inside it. Unpack it, double-click, done. Nothing to install,
no Python, no PATH. This is the one to forward to other people.

Built by GitHub Actions (`.github/workflows/windows-build.yml`) on a tag, or
by hand on any Windows machine:

```powershell
pip install pyinstaller
pyinstaller --noconfirm packaging\flow-bulk-generator.spec
```

The result is `dist\FlowBulkGenerator\`, around 250-300 MB because Qt and
Playwright's driver are bundled. It is a folder rather than a single file on
purpose: a one-file build of this size unpacks itself to a temp directory on
every launch, which makes startup slow.

Unsigned, so Windows SmartScreen will warn on first run - *More info* ->
*Run anyway*. Removing that needs a code-signing certificate.

**B. Install from source** - the rest of this page. Needs Python on the
machine, but the download is 100 KB instead of 280 MB.

## 1. Get the files onto the Windows machine

There is no git remote, so copy the folder across — USB, Google Drive,
OneDrive, whatever is easiest. Use the zip that was built for this:

```
flow-bulk-generator-<version>.zip
```

It excludes the Linux virtual environment, generated images, project queues
and secrets. Unzip it anywhere you like, e.g. `C:\Users\<you>\flow-bulk-generator`.

## 2. Install Python

From [python.org](https://www.python.org/downloads/windows/), version **3.10
or newer, 64-bit**. On the first installer screen tick **"Add python.exe to
PATH"** — without it the commands below will not be found.

The Microsoft Store build of Python also works but sandboxes file access in
ways that can be confusing; the python.org installer is the simpler path.

## 3. Install Google Chrome

Normal Chrome from google.com/chrome. Edge works as a fallback but Chrome is
what this was built against.

## 4. One click

Double-click **`Install.bat`**. It checks the machine, offers to install
Python through winget if it is missing, and then runs the installer. Nothing
else is needed — the rest of this page is only useful if that fails.

## 4b. Pre-flight, by hand

`Install.bat` runs this for you. To run it on its own, open PowerShell in the
unzipped folder (Shift + right-click → *Open PowerShell window here*):

```powershell
py windows_preflight.py
```

This uses nothing but the standard library and checks Python's version and
architecture, that `venv` is present, that Chrome is findable, that the
AppData folders are writable, and that port 9222 is free. Fix anything it
marks `[FAIL]` before continuing.

## 5. Install, by hand

`Install.bat` does this for you. To do it yourself:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

`-ExecutionPolicy Bypass` applies to that one command only; it does not change
any machine setting. The script creates a private virtual environment under
`%LOCALAPPDATA%\FlowBulkGenerator`, installs the app into it, and adds a Start
Menu shortcut (and a Desktop one if you say yes).

## 6. Sign in to Flow, once

Launch **Flow Bulk Generator** from the Start Menu, then **Run ▸ Launch
Chrome**. A Chrome window opens on a *separate* profile — not your everyday
one. Sign in to Google there and open Flow.

> The separate profile is not optional. Since Chrome 136 the DevTools port is
> refused on the default profile, specifically so other local programs cannot
> read its cookies. The app has to drive a profile of its own.

Then **Run ▸ Check Flow**. It should say *Flow: ready*.

## 6b. For video (optional)

Building video needs **ffmpeg**. The standalone build already contains it.
Installing from source instead:

```powershell
winget install Gyan.FFmpeg
```

Then open a new PowerShell window so the PATH is picked up. The Video tab
says plainly if it cannot find ffmpeg.

## 7. Point it at your prompts

The app ships with no sheet configured — the first run starts empty.

**Source** tab → paste the Google Sheet link → **Check source** → **Load
prompts into queue**. Or choose a local `.xlsx` / `.csv`.

## If something goes wrong

| Symptom | Cause |
|---|---|
| `py` or `python` not recognised | Python installed without "Add to PATH". Re-run its installer → Modify → tick it. |
| `install.ps1 cannot be loaded` | The `-ExecutionPolicy Bypass` part was dropped from the command. |
| No window opens, no error | Launch `%LOCALAPPDATA%\FlowBulkGenerator\venv\Scripts\imgen.exe doctor` from PowerShell to see the real message. |
| "no Chrome, Chromium or Edge found" | Set `chrome.binary` in `%APPDATA%\FlowBulkGenerator\settings.toml` to the full `chrome.exe` path. |
| Devtools endpoint never appears | Another Chrome may already own the automation profile. Close all Chrome windows and retry. |
| Flow says signed out | Sign in again in the automation Chrome window; the session is per-profile. |

The CLI is the quickest way to see what is actually wrong:

```powershell
%LOCALAPPDATA%\FlowBulkGenerator\venv\Scripts\imgen.exe doctor
```

## What has and has not been tested

`install.ps1` and `uninstall.ps1` were run for real under **PowerShell 7.6**,
on Linux, against a sandboxed `%LOCALAPPDATA%` / `%APPDATA%`. That exercised
nearly all of it end to end: Python detection, virtual-environment creation,
`pip install`, entry-point creation, the confirmation prompts, copying
projects and settings across, the final summary, and both uninstall modes.
Three bugs were found and fixed that way — a hard-coded `Scripts\pip.exe`
path, an abort when shortcut creation fails, and a summary that announced a
Start Menu entry it had not managed to create.

What could **not** be exercised, because it is Windows-only:

- the `WScript.Shell` Start Menu and Desktop shortcuts
- the `py -3` launcher (the fallbacks `python` and `python3` were used here)
- real `C:\` paths and Windows file locking

Those are covered indirectly: tests assert that the locations the installer
writes to are exactly the ones the app reads from on Windows, and
`windows_preflight.py` checks the environment before you install.

If the first Windows run fails, send the exact error — it will be quick to fix.
