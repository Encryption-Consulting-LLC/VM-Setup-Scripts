#!/usr/bin/env bash
#
# build-seed.sh -- Build the NoCloud seed ISO that autoinstalls a -base-pre VM.
#
# Fills the user-data template next to this script and packs it with meta-data,
# the bootstrap unit/script, the first-boot runner + setup script (from the
# parent linux-server/ folder) and an optional customize.sh onto an ISO labelled
# CIDATA. Attach it as a second CD-ROM alongside the Ubuntu Server ISO; the
# installer picks it up by its label.
#
# Runs on the workstation (not the VM). Needs xorriso (or genisoimage/mkisofs)
# and openssl (or mkpasswd) to hash the password.
#
# The seed deliberately carries NO firstboot.manifest, so the first-boot runner
# never mistakes it for a config disc.
#
# Usage:
#   build-seed.sh -o isos/ub-24.04-seed.iso -u USER [-n HOSTNAME] [--customize FILE]
#
# The password is read from $SEED_PASSWORD, else prompted (twice). It becomes
# the install user's password on -base-pre and every clone, unless a per-VM
# gen-password script resets it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LINUX_DIR="$(dirname "$HERE")"

OUT=""
USERNAME=""
HOSTNAME_="ubuntu-base-pre"
CUSTOMIZE=""

usage() {
    sed -n '/^# Usage:/,/^# gen-password/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output)   OUT=${2:?}; shift 2 ;;
        -u|--user)     USERNAME=${2:?}; shift 2 ;;
        -n|--hostname) HOSTNAME_=${2:?}; shift 2 ;;
        --customize)   CUSTOMIZE=${2:?}; shift 2 ;;
        -h|--help)     usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

[ -n "$OUT" ] || { echo "ERROR: -o OUTPUT.iso is required." >&2; usage; }
[ -n "$USERNAME" ] || { echo "ERROR: -u USER is required (the install user)." >&2; usage; }

# Same rules the installer enforces, checked here so a bad value fails now and
# not twenty minutes into an install.
[[ $USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "ERROR: invalid username '$USERNAME'." >&2; exit 1; }
[[ $HOSTNAME_ =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || { echo "ERROR: invalid hostname '$HOSTNAME_' (single RFC 1123 label)." >&2; exit 1; }
if [ -n "$CUSTOMIZE" ] && [ ! -f "$CUSTOMIZE" ]; then
    echo "ERROR: --customize file not found: $CUSTOMIZE" >&2; exit 1
fi

for f in "$HERE/user-data" "$HERE/firstboot-bootstrap.sh" "$HERE/firstboot-bootstrap.service" \
         "$LINUX_DIR/firstboot-runner.sh" "$LINUX_DIR/firstboot-setup.sh"; do
    [ -f "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }
done

if command -v xorriso >/dev/null 2>&1; then
    mkiso=(xorriso -as mkisofs)
elif command -v genisoimage >/dev/null 2>&1; then
    mkiso=(genisoimage)
elif command -v mkisofs >/dev/null 2>&1; then
    mkiso=(mkisofs)
else
    echo "ERROR: need xorriso, genisoimage or mkisofs to build the ISO." >&2; exit 1
fi

# --- Password -> SHA-512 crypt hash ------------------------------------------
password=${SEED_PASSWORD:-}
if [ -z "$password" ]; then
    read -rsp "Password for '$USERNAME': " password; echo >&2
    read -rsp "Confirm: " confirm; echo >&2
    [ "$password" = "$confirm" ] || { echo "ERROR: passwords do not match." >&2; exit 1; }
fi
[ -n "$password" ] || { echo "ERROR: empty password." >&2; exit 1; }

if command -v openssl >/dev/null 2>&1; then
    hash=$(printf '%s' "$password" | openssl passwd -6 -stdin)
elif command -v mkpasswd >/dev/null 2>&1; then
    hash=$(printf '%s' "$password" | mkpasswd -m sha-512 -s)
else
    echo "ERROR: need openssl or mkpasswd to hash the password." >&2; exit 1
fi
unset password confirm

# --- Stage and pack ----------------------------------------------------------
stage=$(mktemp -d)
chmod 0755 "$stage"
trap 'rm -rf "$stage"' EXIT

# A crypt hash uses only [./0-9A-Za-z$], so '|' is a safe sed delimiter and
# nothing in it is special in a replacement.
sed -e "s|@@HOSTNAME@@|$HOSTNAME_|g" \
    -e "s|@@USERNAME@@|$USERNAME|g" \
    -e "s|@@PASSWORD_HASH@@|$hash|g" \
    "$HERE/user-data" > "$stage/user-data"
if grep -q '@@[A-Z_]*@@' "$stage/user-data"; then
    echo "ERROR: unfilled placeholder left in user-data:" >&2
    grep -n '@@[A-Z_]*@@' "$stage/user-data" >&2
    exit 1
fi

printf 'instance-id: iid-%s\nlocal-hostname: %s\n' "$HOSTNAME_" "$HOSTNAME_" > "$stage/meta-data"

install -m 0755 "$HERE/firstboot-bootstrap.sh" "$LINUX_DIR/firstboot-runner.sh" \
                "$LINUX_DIR/firstboot-setup.sh" "$stage/"
install -m 0644 "$HERE/firstboot-bootstrap.service" "$stage/"
[ -n "$CUSTOMIZE" ] && install -m 0755 "$CUSTOMIZE" "$stage/customize.sh"

mkdir -p "$(dirname "$OUT")"
"${mkiso[@]}" -quiet -volid CIDATA -joliet -rock -output "$OUT" "$stage"

echo "Seed ISO : $OUT (label CIDATA)"
echo "Contents : $(cd "$stage" && ls | tr '\n' ' ')"
echo "Install  : user '$USERNAME', hostname '$HOSTNAME_'"
echo "Next     : attach it as a 2nd CD-ROM next to the Ubuntu Server ISO and boot."
