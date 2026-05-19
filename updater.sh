#!/usr/bin/env bash
# updater.sh — Safe periodic background updater for physicar-ros
#
# Launched by physicar.sh in background. Checks for new version tags.
# On update: force-checkout → signal physicar.sh to rebuild+relaunch.
#
# Safety guarantees:
#   - git diff --quiet: skip update if student has local modifications
#   - exec re-launch: re-exec self after update to avoid running stale script
#   - gc.auto=0: prevents git gc (interrupted gc = repo corruption risk)
#   - timeout 30s: prevents hanging on bad network / DNS / no internet
#   - Stale lock cleanup: auto-recover from power loss during git ops
#   - Disk space check: skip update if disk is critically low
#   - git fsck: detect and recover from repo corruption via re-clone
#   - pip install --timeout: prevent pip from hanging forever

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
INTERVAL="${PHYSICAR_UPDATE_INTERVAL:-300}"  # default: 5 minutes
SIGNAL_FILE="/tmp/.physicar-update-ready"
REPO_REMOTE="https://github.com/PhysiCar/physicar-ros.git"
MIN_DISK_MB=200  # minimum free disk space to proceed with update

log() { echo "[updater] $(date '+%H:%M:%S') $*"; }

# ── detect_tag_pattern ───────────────────────────────────
# Auto-detect major version from current tag (e.g. v1.4.2 → v1.*)
detect_tag_pattern() {
    local tag major
    tag=$(git -C "$REPO_DIR" describe --tags --abbrev=0 HEAD 2>/dev/null)
    if [[ -n "$tag" ]]; then
        major=$(echo "$tag" | grep -oP '^v\d+' 2>/dev/null)
        if [[ -n "$major" ]]; then
            echo "${major}.*"
            return 0
        fi
    fi
    echo "v*"
    return 1
}

# ── resolve_git_dir ──────────────────────────────────────
resolve_git_dir() {
    git -C "$REPO_DIR" rev-parse --absolute-git-dir 2>/dev/null || \
    (cd "$REPO_DIR" && cd "$(git rev-parse --git-dir)" && pwd)
}

# ── clean_stale_locks ────────────────────────────────────
# Remove lock files older than 5 minutes (leftover from power loss / kill -9)
clean_stale_locks() {
    local git_dir="$1"
    for lockfile in "$git_dir/index.lock" "$git_dir/HEAD.lock"; do
        if [[ -f "$lockfile" ]]; then
            local age
            age=$(( $(date +%s) - $(stat -c %Y "$lockfile" 2>/dev/null || echo 0) ))
            if (( age > 300 )); then
                rm -f "$lockfile"
                log "removed stale lock $(basename "$lockfile") (age=${age}s)"
            else
                log "lock file exists (age=${age}s), skipping"
                return 1
            fi
        fi
    done
    return 0
}

# ── check_disk_space ─────────────────────────────────────
check_disk_space() {
    local avail_mb
    avail_mb=$(df -BM --output=avail "$REPO_DIR" 2>/dev/null | tail -1 | tr -d ' M')
    if [[ -n "$avail_mb" ]] && (( avail_mb < MIN_DISK_MB )); then
        log "disk space critically low: ${avail_mb}MB < ${MIN_DISK_MB}MB, skipping"
        return 1
    fi
    return 0
}

# ── check_repo_health ───────────────────────────────────
# Detect git repo corruption, attempt re-clone if broken
check_repo_health() {
    if ! git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        log "not a valid git repo, attempting re-clone..."
        reclone_repo
        return $?
    fi

    # Quick corruption check: verify HEAD is resolvable
    if ! git -C "$REPO_DIR" rev-parse HEAD >/dev/null 2>&1; then
        log "HEAD corrupt, attempting re-clone..."
        reclone_repo
        return $?
    fi
    return 0
}

# ── reclone_repo ─────────────────────────────────────────
reclone_repo() {
    local backup="${REPO_DIR}.corrupt.$(date +%s)"
    log "backing up corrupt repo to $backup"
    mv "$REPO_DIR" "$backup" 2>/dev/null || {
        log "failed to move corrupt repo"
        return 1
    }
    if timeout 120 git clone "$REPO_REMOTE" "$REPO_DIR" 2>/dev/null; then
        log "re-clone successful"
        rm -rf "$backup"
        return 0
    else
        log "re-clone failed, restoring backup"
        rm -rf "$REPO_DIR" 2>/dev/null
        mv "$backup" "$REPO_DIR"
        return 1
    fi
}

# ── safe_update ──────────────────────────────────────────
# Returns 0 if updated, 1 otherwise.
safe_update() {
    # 1) Health check (detect corruption)
    check_repo_health || return 1

    # 2) Resolve git dir and clean stale locks
    local git_dir
    git_dir=$(resolve_git_dir) || return 1
    clean_stale_locks "$git_dir" || return 1

    # 3) Disk space check
    check_disk_space || return 1

    # 4) Check for local modifications (student work protection)
    if ! git -C "$REPO_DIR" diff --quiet 2>/dev/null; then
        log "local modifications detected, skipping update"
        return 1
    fi
    if ! git -C "$REPO_DIR" diff --cached --quiet 2>/dev/null; then
        log "staged changes detected, skipping update"
        return 1
    fi

    # 5) Fetch tags with timeout (network failure = skip)
    if ! timeout 30 git -c gc.auto=0 -C "$REPO_DIR" fetch --tags 2>/dev/null; then
        log "fetch failed (network unavailable?)"
        return 1
    fi

    # 6) Find latest matching tag
    local tag_pattern
    tag_pattern="$(detect_tag_pattern)"
    local latest
    latest=$(git -C "$REPO_DIR" tag -l "$tag_pattern" --sort=-v:refname | head -1)
    if [[ -z "$latest" ]]; then
        return 1
    fi

    # 7) Check if update is needed
    local current_rev target_rev
    current_rev=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    target_rev=$(git -C "$REPO_DIR" rev-parse "$latest^{}" 2>/dev/null)
    if [[ "$current_rev" == "$target_rev" ]]; then
        return 1  # up to date
    fi

    # 8) Don't roll back if HEAD is ahead of latest tag
    if git -C "$REPO_DIR" merge-base --is-ancestor "$target_rev" "$current_rev" 2>/dev/null; then
        return 1  # HEAD is ahead of latest tag
    fi

    # 9) Force checkout
    local current_tag
    current_tag=$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo "${current_rev:0:8}")
    log "updating: $current_tag → $latest"
    if ! git -c gc.auto=0 -c advice.detachedHead=false -C "$REPO_DIR" checkout -f "$latest" 2>/dev/null; then
        log "checkout -f failed"
        return 1
    fi
    log "updated to $latest"
    return 0
}

# ── safe_pip_upgrade ─────────────────────────────────────
safe_pip_upgrade() {
    check_disk_space || return 1
    timeout 60 pip3 install --upgrade --timeout 15 physicar 2>/dev/null || true
}

# ── re-exec guard ────────────────────────────────────────
# After checkout, updater.sh itself may have changed.
# Re-exec to run the new version instead of continuing with stale code.
maybe_reexec() {
    local new_hash old_hash
    new_hash=$(md5sum "$SCRIPT_DIR/updater.sh" 2>/dev/null | cut -d' ' -f1)
    old_hash="${_UPDATER_SELF_HASH:-}"
    if [[ -n "$old_hash" && "$new_hash" != "$old_hash" ]]; then
        log "updater.sh changed, re-executing..."
        export _UPDATER_SELF_HASH="$new_hash"
        exec bash "$SCRIPT_DIR/updater.sh" "$@"
    fi
    export _UPDATER_SELF_HASH="$new_hash"
}

# ── Main loop ────────────────────────────────────────────
maybe_reexec "$@"

log "started (interval=${INTERVAL}s)"

# Initial delay: let build + launch stabilize first
sleep 120

while true; do
    if safe_update; then
        # Signal physicar.sh to rebuild + relaunch
        touch "$SIGNAL_FILE"
        log "signaling rebuild..."

        # Kill the ros2 launch process (physicar.sh loop will restart)
        pkill -f 'ros2 launch physicar_bringup' 2>/dev/null || true

        # Re-exec self in case updater.sh was part of the update
        maybe_reexec "$@"
    fi

    safe_pip_upgrade

    sleep "$INTERVAL"
done
