#!/usr/bin/env bash
# Install Flow Bulk Generator for the current user. No root except for one
# optional apt package. Safe to re-run: it upgrades in place.
set -euo pipefail

APP_NAME="Flow Bulk Generator"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/flow-bulk-generator"
VENV="$PREFIX/venv"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

say()  { printf '\033[32m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!  \033[0m %s\n' "$1"; }

# Returns yes on --yes, and on a non-interactive stdin (a piped installer must
# not hang waiting for an answer nobody can give).
confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 0
  local reply
  read -r -p "    $1 [Y/n] " reply
  [[ ! "${reply:-y}" =~ ^[Nn] ]]
}
die()  { printf '\033[31m✗  \033[0m %s\n' "$1" >&2; exit 1; }

# ---- prerequisites ------------------------------------------------------

command -v python3 >/dev/null || die "python3 is not installed."
python3 - <<'PY' || die "Python 3.10 or newer is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
say "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

python3 -c 'import venv' 2>/dev/null || die \
  "The venv module is missing. Install it with:  sudo apt install python3-venv"

# Qt's xcb platform plugin needs this and it is not installed by default on
# Ubuntu or Pop!_OS. Without it the CLI works but no window can open.
# NOTE: not `ldconfig | grep -q`. Under `set -o pipefail`, grep -q exits at the
# first match, ldconfig dies of SIGPIPE, and the pipeline reports failure even
# though the library was found — which reported an installed library missing.
xcb_hits=$(ldconfig -p 2>/dev/null | grep -c 'libxcb-cursor\.so\.0' || true)
if [ "${xcb_hits:-0}" -eq 0 ]; then
  warn "libxcb-cursor0 is missing — the GUI cannot open a window without it."
  if command -v apt-get >/dev/null; then
    if confirm "Install it now with sudo?"; then
      sudo apt-get install -y libxcb-cursor0
    else
      warn "Skipped. Run later:  sudo apt install libxcb-cursor0"
    fi
  else
    warn "Install your distribution's libxcb-cursor package."
  fi
fi

# ---- install ------------------------------------------------------------

say "Installing into $PREFIX"
mkdir -p "$PREFIX" "$BIN_DIR" "$DESKTOP_DIR"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
# A non-editable install, so the app uses the per-user config and data
# directories rather than writing beside the source checkout.
"$VENV/bin/pip" install --upgrade "$SOURCE_DIR" --quiet

for cmd in flow-bulk-generator imgen; do
  ln -sf "$VENV/bin/$cmd" "$BIN_DIR/$cmd"
done
say "Commands linked into $BIN_DIR"

sed "s|^Exec=.*|Exec=$BIN_DIR/flow-bulk-generator|" \
  "$SOURCE_DIR/packaging/flow-bulk-generator.desktop" \
  > "$DESKTOP_DIR/flow-bulk-generator.desktop"
if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi
say "Added to the application menu"

# ---- carry over existing work ------------------------------------------

DATA_PROJECTS="$PREFIX/projects"
SOURCE_PROJECTS="$SOURCE_DIR/projects"
if [ -d "$SOURCE_PROJECTS" ] && [ ! -d "$DATA_PROJECTS" ]; then
  echo
  warn "This checkout already has projects (queues and generated images)."
  warn "An installed copy reads from $PREFIX instead."
  if confirm "Copy them across so nothing is regenerated?"; then
    cp -r "$SOURCE_PROJECTS" "$DATA_PROJECTS"
    say "Copied $(find "$DATA_PROJECTS" -maxdepth 1 -mindepth 1 -type d | wc -l) project(s)"
  fi
fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/flow-bulk-generator"
if [ -f "$SOURCE_DIR/config/settings.toml" ] && [ ! -f "$CONFIG_DIR/settings.toml" ]; then
  mkdir -p "$CONFIG_DIR"
  cp "$SOURCE_DIR/config/settings.toml" "$SOURCE_DIR/config/selectors.toml" "$CONFIG_DIR/"
  say "Copied your settings to $CONFIG_DIR"
fi

# ---- done ---------------------------------------------------------------

echo
say "$APP_NAME installed."
echo "    GUI :  flow-bulk-generator      (also in the application menu)"
echo "    CLI :  imgen doctor"
echo "    Data:  $PREFIX"
echo "    Conf:  $CONFIG_DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH — add it to ~/.profile to use the commands." ;;
esac
