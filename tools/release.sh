#!/usr/bin/env bash
# Cut a release the other laptops will be offered.
#
#     ./tools/release.sh 1.2.0 "faster thumbnails, sync fixed"
#
# Bumps the version, runs the tests, builds the .deb, tags, and publishes
# the tag with the .deb attached. The app on every other machine compares
# its own version against that tag, so a release without the .deb attached
# is worse than no release: it is offered and then cannot be installed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
NOTES="${2:-}"
if [ -z "$VERSION" ]; then
  echo "usage: ./tools/release.sh <version> [notes]" >&2
  echo "   eg: ./tools/release.sh 1.2.0 \"faster thumbnails\"" >&2
  exit 2
fi
if ! printf '%s' "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "version must look like 1.2.3, got '$VERSION'" >&2
  exit 2
fi

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3

CURRENT="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' imgen/__init__.py | head -1)"
if [ "$VERSION" = "$CURRENT" ]; then
  echo "already at $VERSION - nothing to release" >&2
  exit 2
fi
# Refuse to go backwards: an older tag would be offered to nobody and
# would quietly become 'latest' on GitHub, so every machine would then be
# told it is up to date forever.
if [ "$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -1)" != "$VERSION" ]; then
  echo "$VERSION is older than the current $CURRENT" >&2
  exit 2
fi

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "the working tree has uncommitted changes; commit them first" >&2
  exit 2
fi

echo "==> $CURRENT -> $VERSION"
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" imgen/__init__.py

echo "==> tests"
"$PY" -m pytest tests/ -q --deselect tests/test_ui_threads.py

echo "==> building the package"
./packaging/build_deb.sh --rebuild

DEB="dist/flow-bulk-generator_${VERSION}_$(dpkg --print-architecture).deb"
[ -f "$DEB" ] || { echo "expected $DEB and it is not there" >&2; exit 1; }

echo "==> committing and tagging"
git add imgen/__init__.py
git commit -m "release $VERSION${NOTES:+ - $NOTES}"
git tag -a "v$VERSION" -m "${NOTES:-release $VERSION}"
git push
git push --tags

if command -v gh >/dev/null 2>&1; then
  echo "==> publishing"
  gh release create "v$VERSION" "$DEB" \
    --title "v$VERSION" --notes "${NOTES:-No notes were published.}"
  echo
  echo "released. Every other machine will offer v$VERSION within a few hours,"
  echo "or immediately from Help > Check for updates."
else
  cat <<MANUAL

The tag is pushed, but the GitHub CLI is not installed, so the release
itself was not created - and a tag without the .deb attached is what the
app will refuse to install.

Either install it:
    sudo apt install gh && gh auth login
then:
    gh release create v$VERSION "$DEB" --title "v$VERSION" --notes "${NOTES:-}"

or do it in the browser: Releases > Draft a new release > pick tag
v$VERSION, and attach $DEB.
MANUAL
fi
