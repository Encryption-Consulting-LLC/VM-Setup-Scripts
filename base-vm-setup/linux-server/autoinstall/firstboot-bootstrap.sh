#!/usr/bin/env bash
#
# firstboot-bootstrap.sh -- One-time bootstrap of an autoinstalled -base-pre VM.
#
# Staged into /var/lib/firstboot-bootstrap/ by the autoinstall late-commands
# (user-data) and run once by firstboot-bootstrap.service on the installed
# system's first boot. It replaces the hand-run "script" step on -base-pre:
#
#   1. Runs customize.sh, if build-seed.sh --customize put one on the seed.
#   2. Runs `firstboot-setup.sh --install-only`: installs the first-boot runner
#      and firstboot.service, enabled -- but does NOT generalize.
#   3. Removes itself (unit + staging dir), so nothing of it reaches the base.
#   4. Powers off. A powered-off VM is the signal that -base-pre is ready.
#
# Deliberately a self-deleting systemd unit rather than cloud-init runcmd /
# power_state: firstboot-setup.sh runs `cloud-init clean` when it generalizes,
# so cloud-init re-applies its user-data on every deployed clone -- anything put
# there would power off every clone on its first boot.
#
# Fail-safe: on any error it stops (set -e) with the VM left running and the
# unit still enabled, so it retries on the next boot. See
# /var/log/firstboot-bootstrap.log.
set -euo pipefail

DIR=/var/lib/firstboot-bootstrap
UNIT=/etc/systemd/system/firstboot-bootstrap.service
LOG=/var/log/firstboot-bootstrap.log

exec >>"$LOG" 2>&1
echo "===== firstboot-bootstrap starting $(date '+%Y-%m-%d %H:%M:%S') ====="

# Let cloud-init finish the install's first boot (user, SSH host keys, and the
# installer's own write of /etc/cloud/cloud-init.disabled) before customizing
# or powering off. The unit can't express this as ordering -- see its comments.
# Exit 2 is "degraded" (e.g. deprecation warnings): fine. A real error is
# logged but not fatal; the bake will surface anything that actually broke.
if command -v cloud-init >/dev/null 2>&1; then
    echo "--- waiting for cloud-init ---"
    rc=0; cloud-init status --wait >/dev/null || rc=$?
    case $rc in
        0|2) echo "--- cloud-init done (status rc=$rc) ---" ;;
        *)   echo "WARN: cloud-init finished with errors (rc=$rc); continuing. See /var/log/cloud-init.log." ;;
    esac
fi

if [ -f "$DIR/customize.sh" ]; then
    echo "--- customize.sh ---"
    bash "$DIR/customize.sh"
    echo "--- customize.sh done ---"
fi

bash "$DIR/firstboot-setup.sh" --install-only

systemctl disable firstboot-bootstrap.service
rm -f "$UNIT"
rm -rf "$DIR"
systemctl daemon-reload

echo "===== firstboot-bootstrap complete -- powering off ====="
sync
systemctl poweroff
