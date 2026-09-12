#!/usr/bin/env bash
# Restart the renderer on the rotation change and clear what it superseded.
#
# The running render_all.py still has the pre-rotation track.py in memory, and
# every track already on disk was keyed without the rotation settings, so the
# streamer cannot see any of them. Both have to go.
set -u
QL="$HOME/quran-live"

say() { printf '\n== %s\n' "$1"; }

say "1/5  stopping the old renderer"
pkill -f 'render_all.py' && echo "  render_all stopped" || echo "  not running"
sleep 2
pkill -f 'ffmpeg.*partial\.ts' && echo "  in-flight renders stopped" || echo "  none"
sleep 2
pgrep -f 'render_all|prime_and_render|partial\.ts' >/dev/null \
  && echo "  WARNING: something is still running" || echo "  clean"

say "2/5  protecting the track on air"
SPID=$(pgrep -f 'engine.stream channel01' | head -1)
ONAIR=""
if [ -n "${SPID:-}" ]; then
  ONAIR=$(ls -l /proc/"$SPID"/fd 2>/dev/null | grep -o '[a-f0-9]\{8,\}\.ts' | head -1)
fi
[ -n "$ONAIR" ] && echo "  on air: $ONAIR" || echo "  nothing held open"

say "3/5  deleting tracks the new cache key can no longer reach"
cd "$QL/cache/tracks" 2>/dev/null || exit 1
rm -f ./*.partial.ts 2>/dev/null
for f in *.ts; do
  [ -e "$f" ] || continue
  if [ "$f" = "$ONAIR" ]; then
    echo "    kept $f (on air)"
  else
    rm -f "$f" && echo "    deleted $f"
  fi
done

say "4/5  disk"
df -h / | tail -1
du -sh "$QL/cache" 2>/dev/null

say "5/5  starting the rebuild (shortest surahs first)"
cd "$QL" || exit 1
nohup python3 prime_and_render.py > logs/render.log 2>&1 &
sleep 8
if pgrep -f prime_and_render >/dev/null; then
  echo "  started (pid $(pgrep -f prime_and_render | head -1))"
  echo "  --- first lines ---"
  head -5 logs/render.log
else
  echo "  FAILED to start:"
  tail -20 logs/render.log
fi

say "stream"
systemctl --user is-active quran-live@channel01.service
tail -2 "$QL/logs/channel01.log"
