#!/usr/bin/env bash
# Builds dist/chione-display_<version>_all.deb without root or fakeroot.
#
#   packaging/build-deb.sh             output in <repo>/dist
#   CHIONE_DIST=/some/dir packaging/build-deb.sh   output elsewhere
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${CHIONE_DIST:-$REPO_DIR/dist}"
GUI_ID="io.github.jonanthann.ChioneDisplay"
PKG="chione-display"
SHARE="usr/share/$PKG"

command -v dpkg-deb >/dev/null 2>&1 || { echo "error: dpkg-deb not found" >&2; exit 1; }

VERSION="$(cd "$REPO_DIR" && python3 -c 'import chione_core; print(chione_core.__version__)')"
[ -n "$VERSION" ] || { echo "error: cannot read __version__" >&2; exit 1; }

# Reproducible timestamps: last commit, or the environment's value.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH="$(git -C "$REPO_DIR" log -1 --format=%ct 2>/dev/null || date +%s)"
fi
export SOURCE_DATE_EPOCH

# The active rule must be uaccess, never a world-writable node.
grep -q '^SUBSYSTEM=="hidraw".*TAG+="uaccess"' "$REPO_DIR/udev/70-chione.rules" \
    || { echo "error: active udev rule is not TAG+=\"uaccess\"" >&2; exit 1; }
if grep -v '^[[:space:]]*#' "$REPO_DIR/udev/70-chione.rules" | grep -q 'MODE="0666"'; then
    echo "error: active udev rule grants MODE 0666" >&2; exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/$PKG"

mkdir -p "$ROOT/DEBIAN" "$ROOT/$SHARE" "$ROOT/usr/bin" \
    "$ROOT/usr/share/applications" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$ROOT/usr/lib/udev/rules.d" "$ROOT/usr/lib/systemd/user" \
    "$ROOT/usr/share/doc/$PKG"

# Application files
for f in chione_core.py chione_temp_display.py chione_gui.py chione_web.py; do
    if head -c 2 "$REPO_DIR/$f" | grep -q '^#!'; then
        install -m 0755 "$REPO_DIR/$f" "$ROOT/$SHARE/$f"
    else
        install -m 0644 "$REPO_DIR/$f" "$ROOT/$SHARE/$f"
    fi
done

# Command wrappers
write_wrapper() {
    printf '#!/bin/sh\nexec python3 /usr/share/%s/%s "$@"\n' "$PKG" "$2" > "$ROOT/usr/bin/$1"
    chmod 0755 "$ROOT/usr/bin/$1"
}
write_wrapper chione-display chione_temp_display.py
write_wrapper chione-display-gui chione_gui.py
write_wrapper chione-web chione_web.py

# Desktop entry (same as install.sh gui, except Exec)
cat > "$ROOT/usr/share/applications/$GUI_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Chione Display
Comment=Control the Chione cooler display
Exec=chione-display-gui
Icon=$GUI_ID
Terminal=false
Categories=Settings;HardwareSettings;
StartupNotify=true
DESKTOP
chmod 0644 "$ROOT/usr/share/applications/$GUI_ID.desktop"
if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$ROOT/usr/share/applications/$GUI_ID.desktop"
fi

install -m 0644 "$REPO_DIR/data/$GUI_ID.svg" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps/$GUI_ID.svg"
install -m 0644 "$REPO_DIR/udev/70-chione.rules" "$ROOT/usr/lib/udev/rules.d/70-chione.rules"

# User unit: same as the repo's, with the packaged paths
UNIT="$ROOT/usr/lib/systemd/user/chione-temp.service"
# Drop the repo unit's comments: they describe the git checkout and
# install.sh, which do not apply inside the package.
sed -e '/^#/d' -e 's|^ExecStart=.*|ExecStart=/usr/bin/python3 /usr/share/chione-display/chione_temp_display.py $CHIONE_ARGS|' \
    "$REPO_DIR/systemd/chione-temp.service" > "$UNIT"
grep -q '^ExecStart=/usr/bin/python3 /usr/share/chione-display/' "$UNIT" \
    || { echo "error: failed to rewrite ExecStart" >&2; exit 1; }
chmod 0644 "$UNIT"

# Docs
DOC="$ROOT/usr/share/doc/$PKG"
# Policy: documentation over 4 KB is shipped compressed.
gzip -9n < "$REPO_DIR/README.md" > "$DOC/README.md.gz"
chmod 0644 "$DOC/README.md.gz"
cat > "$DOC/copyright" <<COPYRIGHT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: chione-display-linux
Source: https://github.com/jonanthann/chione-display-linux

Files: *
Copyright: 2026 Jonanthan
License: MIT

License: MIT
$(sed -e '1,/^$/d' -e 's/^$/./' -e 's/^/ /' "$REPO_DIR/LICENSE")
COPYRIGHT
chmod 0644 "$DOC/copyright"
{
    printf '%s (%s) unstable; urgency=low\n\n' "$PKG" "$VERSION"
    printf '  * Release %s. See https://github.com/jonanthann/chione-display-linux/releases\n\n' "$VERSION"
    printf ' -- jonanthann <163675706+jonanthann@users.noreply.github.com>  %s\n' "$(date -u -R -d "@$SOURCE_DATE_EPOCH")"
# A native package (no Debian revision) ships changelog.gz.
} | gzip -9n > "$DOC/changelog.gz"
chmod 0644 "$DOC/changelog.gz"

# Maintainer scripts
cat > "$ROOT/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
    configure)
        if command -v udevadm >/dev/null 2>&1; then
            udevadm control --reload-rules || true
            udevadm trigger --subsystem-match=hidraw || true
        fi
        ;;
    abort-upgrade|abort-remove|abort-deconfigure) ;;
    *) echo "postinst called with unknown argument '$1'" >&2; exit 1 ;;
esac
exit 0
POSTINST
cat > "$ROOT/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
case "$1" in
    remove|purge)
        if command -v udevadm >/dev/null 2>&1; then
            udevadm control --reload-rules || true
        fi
        ;;
    upgrade|failed-upgrade|abort-install|abort-upgrade|disappear) ;;
    *) echo "postrm called with unknown argument '$1'" >&2; exit 1 ;;
esac
exit 0
POSTRM
chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm"

# Permissions and timestamps
find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

SIZE="$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: jonanthann <163675706+jonanthann@users.noreply.github.com>
Depends: python3 (>= 3.9)
Recommends: python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Section: utils
Priority: optional
Homepage: https://github.com/jonanthann/chione-display-linux
Installed-Size: $SIZE
Description: Driver and app for the Gamdias Chione E4 display (unofficial)
 Shows the real CPU temperature, or fan speed, on the digital display of
 the Gamdias Chione E4 cooler, without the Windows-only ZEUS CAST software.
 Includes a command line driver, a systemd user service, a udev rule that
 grants the logged-in user access to the device, and a GNOME settings app.
 .
 This is an unofficial project, not affiliated with Gamdias.
CONTROL
chmod 0644 "$ROOT/DEBIAN/control"
touch -d "@$SOURCE_DATE_EPOCH" "$ROOT/DEBIAN"/*

mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/${PKG}_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null

echo "$DEB"
dpkg-deb -I "$DEB" | sed -n '1,/^ Description/p'
dpkg-deb -c "$DEB" | awk '$NF !~ /\/$/ {print $1, $3, $NF}'
