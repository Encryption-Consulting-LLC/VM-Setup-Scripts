#!/usr/bin/env bash
#
# remaster-iso.sh -- Add `autoinstall` to an Ubuntu Server ISO's boot entries.
#
# Optional. When the autoinstall config arrives on a seed disc (build-seed.sh),
# Ubuntu 24.04 still stops once to ask "Continue with autoinstall? (yes|no)"
# unless `autoinstall` is on the kernel command line. This writes a copy of the
# ISO with that argument added to every /casper/{,hwe-}vmlinuz boot entry and
# the GRUB menu timeout shortened, so an install needs no console input at all.
# Do it once per Ubuntu point release and upload the result to the datastore.
#
# WARNING: the result installs WITHOUT confirmation onto any VM it boots with a
# seed disc attached, wiping that VM's disk. Name and store it accordingly.
#
# Runs on the workstation. Needs xorriso. The original ISO is not modified.
#
# Usage:
#   remaster-iso.sh ubuntu-24.04.3-live-server-amd64.iso isos/ubuntu-24.04.3-autoinstall.iso
set -euo pipefail

[ $# -eq 2 ] || { sed -n '/^# Usage:/,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }
IN=$1
OUT=$2

[ -f "$IN" ] || { echo "ERROR: input ISO not found: $IN" >&2; exit 1; }
[ "$(realpath -m "$IN")" != "$(realpath -m "$OUT")" ] || { echo "ERROR: output must differ from input." >&2; exit 1; }
command -v xorriso >/dev/null 2>&1 || { echo "ERROR: xorriso is required." >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

GRUB_CFG=/boot/grub/grub.cfg
xorriso -osirrox on -indev "$IN" -extract "$GRUB_CFG" "$work/grub.cfg" >/dev/null 2>&1 \
    || { echo "ERROR: $IN has no $GRUB_CFG -- not an Ubuntu 20.04+ live-server ISO?" >&2; exit 1; }
chmod u+w "$work/grub.cfg"

if grep -qE '^[[:space:]]*linux[[:space:]]+/casper/(hwe-)?vmlinuz.*[[:space:]]autoinstall([[:space:]]|$)' "$work/grub.cfg"; then
    echo "ERROR: $IN already has 'autoinstall' on its boot entries." >&2; exit 1
fi

sed -i -E \
    -e 's|^([[:space:]]*linux[[:space:]]+/casper/(hwe-)?vmlinuz)([[:space:]])|\1 autoinstall\3|' \
    -e 's|^([[:space:]]*set[[:space:]]+timeout=).*|\15|' \
    "$work/grub.cfg"

grep -qE '^[[:space:]]*linux[[:space:]]+/casper/(hwe-)?vmlinuz autoinstall' "$work/grub.cfg" \
    || { echo "ERROR: found no '/casper/vmlinuz' boot entry to patch in $GRUB_CFG." >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
# `-boot_image any replay` re-creates the source ISO's El Torito + hybrid
# MBR/GPT + EFI boot setup on the output, so it stays BIOS- and UEFI-bootable.
xorriso -indev "$IN" -outdev "$OUT" \
    -map "$work/grub.cfg" "$GRUB_CFG" \
    -boot_image any replay >/dev/null 2>&1 \
    || { echo "ERROR: xorriso failed to write $OUT." >&2; exit 1; }

echo "Remastered : $OUT"
echo "Boot lines :"
grep -E '^[[:space:]]*linux[[:space:]]+/casper/(hwe-)?vmlinuz' "$work/grub.cfg" | sed 's/^[[:space:]]*/  /'
