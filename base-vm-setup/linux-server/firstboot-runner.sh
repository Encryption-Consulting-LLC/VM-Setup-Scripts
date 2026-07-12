#!/usr/bin/env bash
#
# First-boot config runner. Shipped as a real file next to firstboot-setup.sh
# (which installs it to /usr/local/sbin) and invoked once by firstboot.service
# on a deployed VM. Locates the config disc by its firstboot.manifest, stages
# any payload files the manifest lists, runs the listed scripts in order as
# root, and owns the single reboot. Fail-fast: the first non-zero exit aborts
# the run (no reboot). Produces no console output (runs before any login);
# progress and errors go to /var/log/firstboot.log (fatal errors additionally
# to /var/log/firstboot-error.log).
#
# FIRSTBOOT-RUNNER-MARKER (firstboot-setup.sh verifies this before installing)
#
# Manifest support:
#   v1: {"version": 1, "scripts": [...]}
#   v2: {"version": 2, "scripts": [...], "files": [...]}
# "files" are payload files (binaries welcome) that are never executed. They
# are staged to $WORKDIR/files before any script runs; scripts reach them via
# $FIRSTBOOT_FILES_DIR or the relative path files/<name> (scripts run with the
# work dir as their working directory). Staging is TRANSIENT ($WORKDIR lives
# on tmpfs): a script that needs a payload to persist must install it (copy it
# to its final location) during first boot.
#
# The FIRSTBOOT_* environment overrides below exist for unit tests only; the
# defaults are production behavior. FIRSTBOOT_MANIFEST_DIR uses a directory as
# the disc root instead of scanning/mounting CD devices. FIRSTBOOT_NO_REBOOT=1
# skips the final service-disable and reboot.

# NB: intentionally no `set -e` -- failures are handled explicitly so the runner
# controls whether a reboot happens.
set -uo pipefail

LOG=${FIRSTBOOT_LOG:-/var/log/firstboot.log}
ERRLOG=${FIRSTBOOT_ERRLOG:-/var/log/firstboot-error.log}
SENTINEL=${FIRSTBOOT_SENTINEL:-/var/lib/firstboot/done}
MOUNTPOINT=${FIRSTBOOT_MOUNTPOINT:-/run/firstboot-cd}
WORKDIR=${FIRSTBOOT_WORKDIR:-/run/firstboot-scripts}
MANIFEST_NAME=firstboot.manifest

mounted_dev=""

log()  { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"; }
fail() {
    log "ERROR: $1"
    printf '%s  ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" > "$ERRLOG"
    log "Machine NOT rebooted. See $ERRLOG."
    [ -n "$mounted_dev" ] && umount "$MOUNTPOINT" 2>/dev/null || true
    exit 1
}

mkdir -p "$(dirname "$SENTINEL")"
log "===== firstboot runner starting (as $(id -un) on $(hostname)) ====="

# -- 1. Locate the config disc by its firstboot.manifest marker --------------
disc_root=""
if [ -n "${FIRSTBOOT_MANIFEST_DIR:-}" ]; then
    disc_root="$FIRSTBOOT_MANIFEST_DIR"
    log "Using FIRSTBOOT_MANIFEST_DIR as disc root: $disc_root"
else
    mkdir -p "$MOUNTPOINT"
    for dev in /dev/sr* /dev/cdrom; do
        [ -b "$dev" ] || continue
        umount "$MOUNTPOINT" 2>/dev/null || true
        log "Scanning $dev for $MANIFEST_NAME ..."
        if mount -o ro "$dev" "$MOUNTPOINT" 2>/dev/null; then
            if [ -f "$MOUNTPOINT/$MANIFEST_NAME" ]; then
                disc_root="$MOUNTPOINT"
                mounted_dev="$dev"
                break
            fi
            umount "$MOUNTPOINT" 2>/dev/null || true
        fi
    done
fi
manifest="$disc_root/$MANIFEST_NAME"
[ -n "$disc_root" ] && [ -f "$manifest" ] || fail "No config disc found. Ensure $MANIFEST_NAME is at the root of a mounted ISO."
log "Config disc: ${mounted_dev:-$disc_root} (manifest: $manifest)"

# -- 2. Parse the manifest (real JSON parse; the old sed pipeline broke on ----
#       anything but the exact v1 layout) ------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 is required to parse $MANIFEST_NAME but was not found."

python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$manifest" 2>/dev/null \
    || fail "Manifest '$manifest' is not valid JSON."

manifest_list() {
    # Print each entry of the named top-level array, one per line.
    python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    m = json.load(fh)
for name in (m.get(sys.argv[2]) or []):
    print(name)
' "$manifest" "$1"
}

version="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", "(unset)"))' "$manifest")"
mapfile -t scripts < <(manifest_list scripts)
mapfile -t files < <(manifest_list files)

[ "${#scripts[@]}" -gt 0 ] || fail "Manifest '$manifest' has no non-empty 'scripts' list."
case "$version" in
    1|2|"(unset)") : ;;
    *) log "WARN: manifest version $version is newer than supported (2); proceeding with the known keys." ;;
esac
log "Manifest version $version; ${#scripts[@]} script(s) in order: ${scripts[*]}"
[ "${#files[@]}" -gt 0 ] && log "${#files[@]} payload file(s): ${files[*]}"

# -- 3. Stage payload files, then scripts, locally ----------------------------
rm -rf "$WORKDIR"; mkdir -p "$WORKDIR"

FILESDIR="$WORKDIR/files"
if [ "${#files[@]}" -gt 0 ]; then
    mkdir -p "$FILESDIR"
    for name in "${files[@]}"; do
        src="$disc_root/$name"
        [ -f "$src" ] || fail "Manifest lists file '$name' but it is not present on the disc ($src)."
        cp -f "$src" "$FILESDIR/$name"
        log "Staged payload file '$name'."
    done
fi
export FIRSTBOOT_FILES_DIR="$FILESDIR"   # inherited by every script below

# -- 4. Run each script in order ----------------------------------------------
cd "$WORKDIR" || fail "Cannot change into work dir $WORKDIR."
for name in "${scripts[@]}"; do
    src="$disc_root/$name"
    [ -f "$src" ] || fail "Manifest lists '$name' but it is not present on the disc ($src)."
    dst="$WORKDIR/$name"
    cp -f "$src" "$dst"
    chmod +x "$dst"

    log "----- Running '$name' -----"
    if bash "$dst" >"$dst.out" 2>"$dst.err"; then
        while IFS= read -r line; do [ -n "$line" ] && log "  [out] $line"; done < "$dst.out"
        log "----- '$name' completed (exit 0) -----"
    else
        rc=$?
        while IFS= read -r line; do [ -n "$line" ] && log "  [out] $line"; done < "$dst.out"
        while IFS= read -r line; do [ -n "$line" ] && log "  [err] $line"; done < "$dst.err"
        fail "Script '$name' exited with code $rc. Aborting first-boot run -- NOT rebooting."
    fi
done
log "All ${#scripts[@]} script(s) completed successfully."

# -- 5. Finalize: mark done, disable self, reboot once -----------------------
# Linux applies hostname/network live, so no Windows-style second "finalize"
# reboot is needed -- one reboot lands the VM in a clean steady state.
[ -n "$mounted_dev" ] && umount "$MOUNTPOINT" 2>/dev/null || true
touch "$SENTINEL"                                  # ConditionPathExists guard
if [ "${FIRSTBOOT_NO_REBOOT:-0}" = "1" ]; then
    log "FIRSTBOOT_NO_REBOOT=1 -- skipping service disable and reboot."
    exit 0
fi
systemctl disable firstboot.service >/dev/null 2>&1 || true
log "Configuration complete -- rebooting."
sync
systemctl reboot
