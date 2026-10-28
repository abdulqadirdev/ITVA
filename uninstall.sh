#!/usr/bin/env bash
# Remove the installed app. Projects and generated images are kept unless
# --purge is given, because they are the expensive part.
set -euo pipefail

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/flow-bulk-generator"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/flow-bulk-generator"
BIN_DIR="$HOME/.local/bin"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/flow-bulk-generator.desktop"

rm -f "$BIN_DIR/flow-bulk-generator" "$BIN_DIR/imgen" "$DESKTOP"
rm -rf "$PREFIX/venv"
echo "==> Removed the app."

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$PREFIX" "$CONFIG_DIR"
  echo "==> Purged projects, generated images and settings."
else
  echo "    Kept projects and images in $PREFIX"
  echo "    Kept settings in $CONFIG_DIR"
  echo "    Use --purge to delete those too."
fi
