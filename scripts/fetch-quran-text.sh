#!/usr/bin/env bash
# Fetches the Tanzil Uthmani Quran text into data/quran-uthmani.txt.
# Format is one verse per line: surah|ayah|text
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/data/quran-uthmani.txt"
mkdir -p "$ROOT/data"

URLS=(
  "https://tanzil.net/pub/download/index.php?quranType=uthmani&outType=txt-2&agree=true"
  "http://tanzil.net/pub/download/quran-uthmani.txt"
)
for url in "${URLS[@]}"; do
  if curl -fsSL --max-time 60 -o "$OUT" "$url" && [ -s "$OUT" ]; then
    echo "Fetched $(grep -c '^[0-9]' "$OUT") verses -> $OUT"
    exit 0
  fi
done
echo "Could not download the Quran text. Fetch it manually from tanzil.net" >&2
exit 1
