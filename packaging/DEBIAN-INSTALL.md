# Installing on Pop!_OS / Ubuntu

```bash
sudo apt install ./flow-bulk-generator_1.1.1_amd64.deb
```

Use `apt`, not `dpkg -i`: apt pulls in ffmpeg if the machine does not
have it. With `dpkg -i` you would then need `sudo apt-get -f install`.

Afterwards the app is in the applications menu as **Flow Bulk Generator**,
and on the command line as `flow-bulk-generator`.

## After installing: open it, and do what the Setup tab says

The app opens on a **Setup** tab whenever something is missing, with one
row per thing and a button on each:

| | |
|---|---|
| **Google Chrome** | Installs it from Flathub. No password is asked - it is a per-user install. The app generates every image inside Chrome, so nothing works without one. |
| **Signed in to Google** | Opens Chrome so you can sign in. Asked once. This profile is kept separate from your everyday browsing. |
| **Video tools (ffmpeg)** | Normally already there, because the package depends on it. If you installed with `dpkg -i` instead of `apt`, this button puts it right. |
| **Thumbnail font** | Optional. Thumbnails build either way, just in a plainer face. |

Press **Check again** after each one. When nothing is left, the app opens
on Source instead and the tab stops getting in the way.

## What is and is not included

Python, Qt, the Playwright driver and every library are inside the
package, so nothing else needs installing and no network is used during
the install. Two things are deliberately left out:

* **Google Chrome**, because it is not in the Ubuntu archive and a
  flatpak install is both smaller and needs no password. The Setup tab
  does it for you.
* **piper, the offline speech engine** (~100 MB plus a 61 MB voice).
  Everything the app does needs the network anyway - it drives Google
  Flow - so the offline fallback was not worth the weight. Narration
  uses edge-tts.

Microsoft's fonts (Arial Black and friends) are not on a stock Pop!_OS.
The thumbnail presets pick the best heavy face the machine actually has,
so they work either way; for Arial Black specifically:

```bash
sudo apt install ttf-mscorefonts-installer
```

## Where your data lives

| | |
|---|---|
| settings | `~/.config/flow-bulk-generator/` |
| projects, images, video | `~/.local/share/flow-bulk-generator/projects/` |
| the app itself | `/opt/flow-bulk-generator/` |

Removing the package leaves your projects alone:

```bash
sudo apt remove flow-bulk-generator          # keeps projects and settings
rm -rf ~/.local/share/flow-bulk-generator    # and this removes them
```

## Moving an existing project to the new machine

Copy the project folder across; it holds the queue, the images and the
output together.

```bash
# on the old machine
tar czf myproject.tar.gz -C ~/.local/share/flow-bulk-generator/projects default
# on the new one
mkdir -p ~/.local/share/flow-bulk-generator/projects
tar xzf myproject.tar.gz -C ~/.local/share/flow-bulk-generator/projects
```

The queue stores absolute paths to its images; the app repairs them on
first launch, so a project copied from another machine or another home
directory still finds its files.

## Building the package again

```bash
./packaging/build_deb.sh --rebuild
```

It builds the bundle with PyInstaller, refuses to ship if
`imgen/defaults/settings.toml` has drifted from `config/settings.toml`,
and writes `dist/flow-bulk-generator_<version>_amd64.deb`.
