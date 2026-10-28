# Installing

## Any platform, from a checkout

```
python -m venv .venv
.venv/bin/pip install -e .           # Windows: .venv\Scripts\pip install -e .
```

That installs two commands:

| | |
|---|---|
| `flow-bulk-generator` | the GUI (no console window on Windows) |
| `imgen` | the CLI — `imgen doctor`, `imgen run`, `imgen projects` |

`playwright install` is **not** needed: the app attaches to the real Chrome
already on the machine instead of downloading its own browser.

## Where things go

Run from a checkout (there is a `config/settings.toml` beside the code) and
everything stays beside the code — config, projects, output. That is dev mode.

Installed with pip, nothing is written into site-packages:

| | Linux | Windows | macOS |
|---|---|---|---|
| Config | `~/.config/flow-bulk-generator` | `%APPDATA%\FlowBulkGenerator` | `~/Library/Application Support/FlowBulkGenerator` |
| Projects, output | `~/.local/share/flow-bulk-generator` | `%LOCALAPPDATA%\FlowBulkGenerator` | as config |
| Chrome profile | the flatpak's own dir, else the data dir | `%LOCALAPPDATA%\FlowBulkGenerator\chrome-automation` | data dir |

`settings.toml` and `selectors.toml` are copied into the config directory on
first run, so they can be edited without touching the installation.

## Linux desktop entry

```
cp packaging/flow-bulk-generator.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications
```

On Ubuntu/Pop!_OS, Qt also needs one system library:

```
sudo apt install -y libxcb-cursor0
```

## Windows

- Chrome is found automatically under Program Files or LocalAppData; Edge is a
  fallback. To force one, set `chrome.binary` in `settings.toml` to its full
  path, e.g. `'C:\Program Files\Google\Chrome\Application\chrome.exe'`.
- The automation profile is a **separate** Chrome profile, never your everyday
  one — since Chrome 136 the DevTools port is refused on the default profile.
  You sign in to it once and it persists.
- No admin rights needed.

## macOS

Chrome is found in `/Applications`. The first launch of a downloaded Chrome may
need to be approved by Gatekeeper before automation can attach to it.
