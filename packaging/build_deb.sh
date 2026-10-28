#!/usr/bin/env bash
# Build a .deb around the PyInstaller bundle.
#
# The bundle carries its own Python, Qt and Playwright driver, so the
# package depends on almost nothing: a target machine needs ffmpeg and a
# Chrome it can drive, and nothing else. Installing a venv from a postinst
# was the alternative and would need the network, several minutes and a
# compiler on a machine that may have none of them.
#
#     ./packaging/build_deb.sh            # builds dist/ first if needed
#     ./packaging/build_deb.sh --rebuild  # always rebuild the bundle
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PACKAGE="flow-bulk-generator"
# The version lives in imgen/__init__.py; pyproject reads it from there
# too, so there is only ever one copy of the number.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' imgen/__init__.py | head -1)"
[ -n "$VERSION" ] || { echo "cannot read the version from imgen/__init__.py" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"
BUNDLE="dist/FlowBulkGenerator"
STAGE="build/deb/${PACKAGE}_${VERSION}_${ARCH}"
OUT="dist/${PACKAGE}_${VERSION}_${ARCH}.deb"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3

if [ "${1:-}" = "--rebuild" ] || [ ! -d "$BUNDLE" ]; then
  echo "==> building the application bundle"
  rm -rf build/pyi dist/FlowBulkGenerator
  "$PY" -m PyInstaller --noconfirm \
    --distpath dist --workpath build/pyi \
    packaging/flow-bulk-generator.spec
fi

# The defaults seed every fresh machine, which is precisely what this
# package creates. Refuse to ship them out of step with the live config.
echo "==> checking the shipped defaults"
"$PY" tools/sync_defaults.py --check

echo "==> staging"
rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" \
           "$STAGE/opt/$PACKAGE" \
           "$STAGE/usr/bin" \
           "$STAGE/usr/share/applications" \
           "$STAGE/usr/share/icons/hicolor/scalable/apps" \
           "$STAGE/usr/share/doc/$PACKAGE"

cp -a "$BUNDLE/." "$STAGE/opt/$PACKAGE/"
cp packaging/flow-bulk-generator.svg \
   "$STAGE/usr/share/icons/hicolor/scalable/apps/$PACKAGE.svg"

# A wrapper rather than a symlink: the bundle resolves its own directory
# from argv[0], and a symlink from /usr/bin makes that the wrong place.
cat > "$STAGE/usr/bin/$PACKAGE" <<'LAUNCH'
#!/bin/sh
exec /opt/flow-bulk-generator/FlowBulkGenerator "$@"
LAUNCH
chmod 755 "$STAGE/usr/bin/$PACKAGE"

cat > "$STAGE/usr/share/applications/$PACKAGE.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Flow Bulk Generator
GenericName=Bulk image and video generator
Comment=Generate images with Google Flow from a sheet of prompts, then build the video
Exec=/usr/bin/$PACKAGE
Icon=$PACKAGE
Terminal=false
Categories=AudioVideo;Video;
Keywords=flow;images;video;thumbnail;narration;
StartupNotify=true
StartupWMClass=FlowBulkGenerator
DESKTOP

cp README.md "$STAGE/usr/share/doc/$PACKAGE/" 2>/dev/null || true
gzip -9 -n -c packaging/INSTALL.md > "$STAGE/usr/share/doc/$PACKAGE/INSTALL.md.gz" 2>/dev/null || true

INSTALLED_KB="$(du -sk "$STAGE" | cut -f1)"

# Recommends flatpak because that is how the Setup tab installs
# Chrome without asking for a password. The control format has no
# comments of its own - a "#" line in there is a parse error.
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PACKAGE
Version: $VERSION
Section: graphics
Priority: optional
Architecture: $ARCH
Depends: ffmpeg, fontconfig
Recommends: fonts-dejavu-core, flatpak
Installed-Size: $INSTALLED_KB
Maintainer: Flow Bulk Generator <noreply@localhost>
Description: Bulk image and video generator driven by Google Flow
 Reads a sheet of prompts, generates each image through Google Flow in a
 Chrome you are already signed in to, then narrates, captions and joins
 them into a finished video with a thumbnail.
 .
 Python, Qt and the browser driver are bundled, so nothing else needs
 installing. Open the app and its Setup tab lists anything still missing -
 Chrome, the video tools, the fonts - with a button for each.
CONTROL

# Remove the write bits group/other should not have, and make sure the
# tree is root-owned in the archive even though it is built unprivileged.
find "$STAGE" -type d -exec chmod 755 {} +
# Nothing in a package may be group-writable; the build runs under a
# umask that leaves 664 on ordinary files. Executable bits are kept.
find "$STAGE" -type f -exec chmod go-w {} +
chmod 755 "$STAGE/opt/$PACKAGE/FlowBulkGenerator"

echo "==> packing"
install -d dist
fakeroot dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$OUT" >/dev/null

echo
echo "built $OUT"
ls -lh "$OUT" | awk '{print "  size: " $5}'
echo "  installs to /opt/$PACKAGE, launcher at /usr/bin/$PACKAGE"
