#!/bin/bash
# The VNC desktop's background (display :1): the PhysiCar wallpaper — a logo and one sentence,
# so the empty desktop reads as "this is where windows your code opens appear", not as a dead
# screen. feh paints the image; without feh (images baked before 2026-10) the root window gets
# the wallpaper's base colour instead of X's black. Run after openbox is up (sim: the
# supervisord `wallpaper` program; robot: physicar.sh right after it starts openbox).
export DISPLAY="${DISPLAY:-:1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
for _ in $(seq 1 20); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.5; done
if command -v feh >/dev/null 2>&1 && feh --no-fehbg --bg-fill "$HERE/wallpaper.png" 2>/dev/null; then
  exit 0
fi
xsetroot -solid '#15161c' 2>/dev/null || true
