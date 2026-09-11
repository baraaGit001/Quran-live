#!/usr/bin/env bash
# Installs a self-contained static ffmpeg into vendor/ffmpeg.
#
# Oracle Linux 9 ships no ffmpeg, and enabling EPEL/RPM Fusion would add
# third-party package sources to a server that also runs PriceLens and
# AradoBot. A static binary under this project touches no system package and
# can be deleted by removing one directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/ffmpeg"
ARCH="$(uname -m)"

case "$ARCH" in
  aarch64|arm64) BUILD="arm64" ;;
  x86_64)        BUILD="amd64" ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${BUILD}-static.tar.xz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading static ffmpeg for $BUILD ..."
curl -fL --retry 3 -o "$TMP/ffmpeg.tar.xz" "$URL"

echo "Extracting ..."
tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP"
SRC="$(find "$TMP" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
[ -n "$SRC" ] || { echo "unexpected archive layout" >&2; exit 1; }

mkdir -p "$VENDOR"
install -m 0755 "$SRC/ffmpeg" "$SRC/ffprobe" "$VENDOR/"

echo
"$VENDOR/ffmpeg" -version | head -2
echo
echo "Installed to $VENDOR"
