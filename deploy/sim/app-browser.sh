#!/bin/bash
# Studio app bookmark manager.
#
# Publishes the Studio /app URL into app.physicar, which the browser-preview
# extension opens as the Studio bookmark:
# - Cloud/self-hosted sim: the operator's external URL (PHYSICAR_EXTERNAL_URL).
# - Local sim: http://localhost/app.
set -uo pipefail

APP_FILE="$HOME/physicar_ws/app.physicar"

# Write $2 into the (immutable) bookmark file $1.
write_file() {
  # chattr direct first (supervisord runs this as root; sudo can fail where
  # plain chattr works), sudo as fallback for other callers. A silently failed
  # chattr once let a bookmark baked into an image survive every boot
  # (validation.invalid incident) — so verify the write took and shout if not.
  chattr -i "$1" 2>/dev/null || sudo chattr -i "$1" 2>/dev/null || true
  chmod u+w "$1" 2>/dev/null || true
  printf '%s\n' "$2" > "$1" 2>/dev/null || true
  chmod 444 "$1" 2>/dev/null || true
  chattr +i "$1" 2>/dev/null || sudo chattr +i "$1" 2>/dev/null || true
  if [ "$(cat "$1" 2>/dev/null)" != "$2" ]; then
    echo "[app-browser] WARNING: bookmark write failed ($1); stale content remains: $(head -c 120 "$1" 2>/dev/null)" >&2
  fi
}

# Publish $1 (the /app URLs) into app.physicar.
write_bookmark() {
  write_file "$APP_FILE" "$1"
}

# Start from a clean slate on every boot — a URL from a previous environment
# (or one baked into an image) must never survive into this session. An empty
# bookmark is a valid display state; a stale foreign URL is not.
write_bookmark ""

# A leftover golden-bake validation env (validation.invalid) baked into the container — writing
# this dead URL to the bookmark blocks the extension's "empty bookmark → current domain/app"
# fallback and the app screen goes blank (residue of the 2026-08-11 CreateImage incremental-bake
# incident; real case 2026-08-22). Leave the bookmark empty and exit — the extension opens
# <the domain the student's browser connected to (sim.physicar.ai)>/app.
case "${PHYSICAR_EXTERNAL_URL:-}" in
  *validation.invalid*)
    echo "[app-browser] placeholder PHYSICAR_EXTERNAL_URL (${PHYSICAR_EXTERNAL_URL}) — leaving bookmark empty (extension falls back to <origin>/app)" >&2
    exit 0 ;;
esac

# Cloud/self-hosted sim: the operator hands us the external URL (e.g.
# https://xxxxx.physicar.dev) — the bookmark must use it, not localhost,
# because students reach this host through that domain.
if [ -n "${PHYSICAR_EXTERNAL_URL:-}" ]; then
  write_bookmark "${PHYSICAR_EXTERNAL_URL%/}/app"
  exit 0
fi

# Local sim: static localhost bookmark.
write_bookmark "http://localhost/app"
exit 0
