# Keeping the other laptops up to date

You publish a release on GitHub. Every other machine notices it, shows a
strip across the top of its window, and installs it when the person there
presses **Update**.

```
you                                     the other laptops
───────────────────────────────────     ─────────────────────────────────
./tools/release.sh 1.2.0 "notes"
  bumps the version
  runs the tests
  builds the .deb
  tags and pushes
  attaches the .deb to the release
                                        on launch (and Help > Check for
                                        updates), compares its own
                                        version to the newest tag
                                        ↓
                                        "Version 1.2.0 is available"
                                        [What's new] [Update] [Later]
                                        ↓ Update
                                        downloads, asks for the password
                                        once, installs, offers a restart
```

---

## One-time setup

### 1. Put the project on GitHub

It is not a git repository yet.

```bash
cd ~/code/automations/images_generator
git init
git add -A
git commit -m "initial commit"
gh repo create flow-bulk-generator --private --source=. --push
```

`.gitignore` excludes `secrets/`, `projects/`, `output/`, `*.db`,
`.venv/`, `build/` and `dist/`, so no images, credentials, generated
video or build output are committed. A first commit is about 800 KB of
source — if yours is much larger, stop and check what got staged.

**`config/settings.toml` holds your spreadsheet id and is tracked.**
Either keep the repository private, or remove that file from it and let
each machine seed its own from the shipped defaults.

### 2. Decide public or private

| | |
|---|---|
| **Public releases** | nothing more to do. Each app reads the release list anonymously. |
| **Private repository** | each laptop needs a token, because GitHub answers *404* to an anonymous request for a private repo. Put it in `~/.config/flow-bulk-generator/settings.toml` as `update.token`, or in the environment as `FLOW_UPDATE_TOKEN`. |

A token must never be put in a build. Anyone holding the app would then
hold the token. `tools/sync_defaults.py` blanks it on the way into the
shipped defaults for exactly that reason.

A middle road, if the code should stay private but the updates simple:
keep this repo private and make a second, empty, public repo that holds
only the releases. Point `update.repo` at that one.

### 3. Tell the app where to look

On **every** machine, in `~/.config/flow-bulk-generator/settings.toml`:

```toml
[update]
repo = "your-github-name/flow-bulk-generator"
check_on_launch = true
token = ""          # only for a private repo
```

Easiest is to set it once here, before building the .deb the laptops are
installed from — then it ships already configured and nobody has to edit
anything.

---

## Releasing

```bash
./tools/release.sh 1.2.0 "faster thumbnails, A/V sync fixed"
```

The notes are what the other machines show under **What's new**, so write
them for the person reading them, not for yourself.

The script refuses to go backwards. An older tag becomes `latest` on
GitHub, and every machine would then be told it is up to date — forever,
silently, which is the worst kind of broken.

---

## What the other machines do

**They check at most once every six hours**, not on every launch. GitHub
allows 60 anonymous requests an hour from one address, and several
laptops in one office share that address.

**The check is silent when it fails.** A laptop opened on a train should
not be told off about the network. Help → Check for updates reports
either way, because then someone asked.

**Update is refused while a run is in progress.** Stop the run first.

**The password is asked once, by the desktop itself.** The app lives in
`/opt`, which is root-owned, so it cannot replace itself; the install is
handed to `pkexec apt-get install`. Anything that avoided the prompt
would mean a helper running as root in the background, which is a much
bigger thing to trust for a much smaller convenience.

**Restarting is offered, not forced.** The running copy keeps the files
it already opened, so it carries on working until it is closed.

---

## When something goes wrong

| What they see | What it means |
|---|---|
| `no update source is set` | `update.repo` is empty in that machine's settings. |
| `no releases found … A private repository also answers 404 without a token` | either the repo name is wrong, or it is private and that machine has no token. |
| `GitHub refused the request` | the hourly limit, or a token that is wrong or expired. |
| `release v1.2.0 has no .deb attached` | the tag was pushed but the file was not uploaded. Attach it to the release. |
| `the password prompt was dismissed` | nothing was changed; press Update again. |
| `apt failed: …` | the message is apt's own. Usually another package manager is open, or the disk is full. |

Nothing above leaves a half-installed app: apt either completes or
refuses, and the copy already running is untouched either way.
