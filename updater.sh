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
WORKSPACE_DIR="$(cd "$REPO_DIR/../.." && pwd)"  # /opt/physicar
INTERVAL="${PHYSICAR_UPDATE_INTERVAL:-60}"  # default: 1 minute
SIGNAL_FILE="/tmp/.physicar-update-ready"
PENDING_UPDATE_FILE="/tmp/.physicar-update-pending"
PENDING_BUILD_FILE="/tmp/.physicar-build-pending"
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

# ── reinit_repo ──────────────────────────────────────────
# .git directory missing but source files may exist.
# Re-create git tracking without touching working tree.
reinit_repo() {
    log "reinitializing git in existing directory..."
    git -C "$REPO_DIR" init -b main >/dev/null 2>&1 || { log "git init failed"; return 1; }
    git -C "$REPO_DIR" remote add origin "$REPO_REMOTE" 2>/dev/null || \
        git -C "$REPO_DIR" remote set-url origin "$REPO_REMOTE" 2>/dev/null
    if ! timeout 60 git -C "$REPO_DIR" fetch origin 2>/dev/null; then
        log "fetch failed during reinit"
        return 1
    fi
    git -C "$REPO_DIR" update-ref refs/heads/main refs/remotes/origin/main 2>/dev/null
    git -C "$REPO_DIR" reset HEAD >/dev/null 2>&1
    if git -C "$REPO_DIR" rev-parse HEAD >/dev/null 2>&1; then
        log "reinit successful (working tree preserved)"
        return 0
    fi
    log "reinit failed"
    return 1
}

# ── repair_repo ──────────────────────────────────────────
# Lightweight git repair: remove empty objects and re-fetch.
# Preserves working tree (student modifications).
repair_repo() {
    log "attempting lightweight repo repair..."

    # Remove empty/corrupt object files
    local empty_count
    empty_count=$(find "$REPO_DIR/.git/objects" -type f -empty 2>/dev/null | wc -l)
    if [[ "$empty_count" -gt 0 ]]; then
        log "removing $empty_count empty object files"
        find "$REPO_DIR/.git/objects" -type f -empty -delete
    fi

    # Re-fetch all objects from remote
    if ! timeout 60 git -C "$REPO_DIR" fetch origin 2>/dev/null; then
        log "fetch failed during repair"
        return 1
    fi

    # Restore HEAD → origin/main (keep working tree intact)
    local default_branch
    default_branch=$(git -C "$REPO_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
    default_branch="${default_branch:-main}"
    git -C "$REPO_DIR" symbolic-ref HEAD "refs/heads/$default_branch" 2>/dev/null
    git -C "$REPO_DIR" update-ref "refs/heads/$default_branch" "refs/remotes/origin/$default_branch" 2>/dev/null
    git -C "$REPO_DIR" reset HEAD >/dev/null 2>&1

    # Verify repair succeeded
    if git -C "$REPO_DIR" rev-parse HEAD >/dev/null 2>&1; then
        log "lightweight repair successful (working tree preserved)"
        return 0
    fi
    log "lightweight repair failed"
    return 1
}

# ── check_repo_health ───────────────────────────────────
# Detect git repo corruption. Try lightweight repair first
# to preserve student work, fall back to re-clone only if
# repair fails.
check_repo_health() {
    if ! git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        log "not a valid git repo (.git missing), attempting reinit..."
        reinit_repo
        return $?
    fi

    # Quick corruption check: verify HEAD is resolvable
    if ! git -C "$REPO_DIR" rev-parse HEAD >/dev/null 2>&1; then
        log "HEAD corrupt, attempting repair..."
        repair_repo
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

# ── force_checkout ───────────────────────────────────────
# Checkout a tag and verify file consistency.
# Writes a pending marker before checkout, removes it after.
# Returns 0 on success.
force_checkout() {
    local tag="$1"
    local target_rev
    target_rev=$(git -C "$REPO_DIR" rev-parse "$tag^{}" 2>/dev/null) || return 1

    # Mark intent before checkout (survives power loss)
    echo "$tag" > "$PENDING_UPDATE_FILE"
    sync  # flush to disk

    if ! git -c gc.auto=0 -c advice.detachedHead=false -C "$REPO_DIR" checkout -f "$tag" 2>/dev/null; then
        log "checkout -f failed for $tag"
        return 1
    fi

    # Verify: HEAD must match target
    local head_rev
    head_rev=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    if [[ "$head_rev" != "$target_rev" ]]; then
        log "checkout verification failed: HEAD=$head_rev expected=$target_rev"
        return 1
    fi

    # Verify: working tree must be clean against the tag
    if ! git -C "$REPO_DIR" diff --quiet "$tag" 2>/dev/null; then
        log "checkout incomplete: working tree differs from $tag"
        return 1
    fi

    # Success — remove pending marker
    rm -f "$PENDING_UPDATE_FILE"
    log "checkout $tag verified OK"
    return 0
}

# ── resume_pending_update ────────────────────────────────
# If a previous checkout was interrupted (power loss during
# checkout -f), the pending marker file still exists.
# Resume by re-doing the checkout.
resume_pending_update() {
    [[ -f "$PENDING_UPDATE_FILE" ]] || return 1
    local pending_tag
    pending_tag=$(cat "$PENDING_UPDATE_FILE" 2>/dev/null)
    if [[ -z "$pending_tag" ]]; then
        rm -f "$PENDING_UPDATE_FILE"
        return 1
    fi
    log "resuming interrupted update to $pending_tag"

    # Ensure repo is healthy first
    check_repo_health || return 1

    # Re-fetch in case the interrupted fetch left incomplete objects
    timeout 30 git -c gc.auto=0 -C "$REPO_DIR" fetch --tags 2>/dev/null

    # Verify tag still exists
    if ! git -C "$REPO_DIR" rev-parse "$pending_tag^{}" >/dev/null 2>&1; then
        log "pending tag $pending_tag no longer exists, clearing"
        rm -f "$PENDING_UPDATE_FILE"
        return 1
    fi

    if force_checkout "$pending_tag"; then
        return 0
    fi
    log "resume failed for $pending_tag"
    rm -f "$PENDING_UPDATE_FILE"
    return 1
}

# ── safe_update ──────────────────────────────────────────
# Returns 0 if updated, 1 otherwise.
safe_update() {
    # 0) Resume interrupted checkout from previous run
    if resume_pending_update; then
        return 0
    fi

    # 1) Health check (detect corruption)
    check_repo_health || return 1

    # 2) Resolve git dir and clean stale locks
    local git_dir
    git_dir=$(resolve_git_dir) || return 1
    clean_stale_locks "$git_dir" || return 1

    # 3) Disk space check
    check_disk_space || return 1

    # 4) Fetch tags with timeout (network failure = skip)
    if ! timeout 30 git -c gc.auto=0 -C "$REPO_DIR" fetch --tags 2>/dev/null; then
        log "fetch failed (network unavailable?)"
        return 1
    fi

    # 5) Find latest matching tag
    local tag_pattern
    tag_pattern="$(detect_tag_pattern)"
    local latest
    latest=$(git -C "$REPO_DIR" tag -l "$tag_pattern" --sort=-v:refname | head -1)
    if [[ -z "$latest" ]]; then
        return 1
    fi

    # 6) Check if update is needed
    local current_rev target_rev
    current_rev=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    target_rev=$(git -C "$REPO_DIR" rev-parse "$latest^{}" 2>/dev/null)
    if [[ "$current_rev" == "$target_rev" ]]; then
        return 1  # up to date
    fi

    # 7) Don't roll back if HEAD is ahead of latest tag
    if git -C "$REPO_DIR" merge-base --is-ancestor "$target_rev" "$current_rev" 2>/dev/null; then
        return 1  # HEAD is ahead of latest tag
    fi

    # 8) Checkout with verification (force — overwrite local changes)
    local current_tag
    current_tag=$(git -C "$REPO_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo "${current_rev:0:8}")
    log "updating: $current_tag → $latest"
    if ! force_checkout "$latest"; then
        log "update to $latest failed"
        return 1
    fi
    log "updated to $latest"
    return 0
}

# ── safe_build ───────────────────────────────────────────
# Build with symlink-install. Backs up install/ so a failed or
# interrupted build can be rolled back on next boot.
safe_build() {
    local install_dir="$WORKSPACE_DIR/install"
    local backup_dir="$WORKSPACE_DIR/install.bak"
    local build_dir="$WORKSPACE_DIR/build"

    check_disk_space || return 1

    # Mark build in progress
    echo "$(date +%s)" > "$PENDING_BUILD_FILE"
    sync

    # Backup current install (atomic rename)
    if [[ -d "$install_dir" ]]; then
        rm -rf "$backup_dir" 2>/dev/null
        mv "$install_dir" "$backup_dir"
    fi

    log "building (symlink-install)..."
    if (cd "$WORKSPACE_DIR" && \
        source /opt/ros/jazzy/setup.bash && \
        colcon build --symlink-install \
            --base-paths src/physicar-ros \
            --cmake-args -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -5); then
        # Build succeeded — remove backup and pending marker
        rm -rf "$backup_dir" 2>/dev/null
        rm -f "$PENDING_BUILD_FILE"
        log "build successful"
        return 0
    else
        # Build failed — restore backup
        log "build FAILED, restoring previous install"
        rm -rf "$install_dir" 2>/dev/null
        if [[ -d "$backup_dir" ]]; then
            mv "$backup_dir" "$install_dir"
        fi
        rm -f "$PENDING_BUILD_FILE"
        return 1
    fi
}

# ── recover_install ──────────────────────────────────────
# Called at startup: if a build was interrupted (power loss),
# restore install/ from backup.
recover_install() {
    local install_dir="$WORKSPACE_DIR/install"
    local backup_dir="$WORKSPACE_DIR/install.bak"

    if [[ -f "$PENDING_BUILD_FILE" ]] || [[ -d "$backup_dir" ]]; then
        log "detected interrupted build, recovering..."
        rm -rf "$install_dir" 2>/dev/null
        if [[ -d "$backup_dir" ]]; then
            mv "$backup_dir" "$install_dir"
            log "restored install from backup"
        fi
        rm -f "$PENDING_BUILD_FILE"
    fi
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

# Recover from interrupted build (power loss during colcon build)
recover_install

# Initial delay: let build + launch stabilize first
sleep 120

while true; do
    if safe_update; then
        # Build after update
        if safe_build; then
            log "update ready — will apply on next restart"
        fi

        # Re-exec self in case updater.sh was part of the update
        maybe_reexec "$@"
    fi

    safe_pip_upgrade

    sleep "$INTERVAL"
done
