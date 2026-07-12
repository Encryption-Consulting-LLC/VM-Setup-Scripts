#!/usr/bin/env bash
#
# Tier-1 harness for base-vm-setup/linux-server/firstboot-runner.sh: drives the
# runner against fake disc directories via its FIRSTBOOT_* test hooks (no CD,
# no mount, no reboot, no systemd). Run from anywhere: ./tests/test-linux-runner.sh
set -euo pipefail

RUNNER="$(cd "$(dirname "$0")/.." && pwd)/base-vm-setup/linux-server/firstboot-runner.sh"
[ -f "$RUNNER" ] || { echo "runner not found: $RUNNER" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

check() { # $1 = description, $2 = condition result (0/1 via $?)
    if [ "$2" -eq 0 ]; then
        PASS=$((PASS + 1)); echo "  ok: $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL: $1"
    fi
}

run_runner() { # $1 = case dir (has disc/), returns runner exit code
    local dir="$1"
    mkdir -p "$dir/state"
    set +e
    FIRSTBOOT_MANIFEST_DIR="$dir/disc" \
    FIRSTBOOT_LOG="$dir/state/firstboot.log" \
    FIRSTBOOT_ERRLOG="$dir/state/firstboot-error.log" \
    FIRSTBOOT_SENTINEL="$dir/state/done" \
    FIRSTBOOT_WORKDIR="$dir/state/work" \
    FIRSTBOOT_NO_REBOOT=1 \
        bash "$RUNNER" >/dev/null 2>&1
    local rc=$?
    set -e
    return $rc
}

# ---------------------------------------------------------------------------
echo "case 1: v1 manifest, scripts run in manifest order"
C="$TMP/c1"; mkdir -p "$C/disc"
cat > "$C/disc/firstboot.manifest" <<'EOF'
{
  "version": 1,
  "scripts": [
    "20-second.sh",
    "10-first.sh"
  ]
}
EOF
printf '#!/bin/sh\necho 20 >> "%s/order"\n' "$C" > "$C/disc/20-second.sh"
printf '#!/bin/sh\necho 10 >> "%s/order"\n' "$C" > "$C/disc/10-first.sh"

rc=0; run_runner "$C" || rc=$?
check "exit 0" "$([ $rc -eq 0 ]; echo $?)"
check "manifest order respected (20 before 10)" "$([ "$(cat "$C/order" 2>/dev/null | tr '\n' ' ')" = "20 10 " ]; echo $?)"
check "sentinel written" "$([ -f "$C/state/done" ]; echo $?)"
check "no error log" "$([ ! -f "$C/state/firstboot-error.log" ]; echo $?)"

# ---------------------------------------------------------------------------
echo "case 2: v2 manifest, payload files staged and reachable"
C="$TMP/c2"; mkdir -p "$C/disc"
head -c 65536 /dev/urandom > "$C/disc/blob.bin"
BLOB_SHA="$(sha256sum "$C/disc/blob.bin" | cut -d' ' -f1)"
echo "hello" > "$C/disc/note.txt"
cat > "$C/disc/firstboot.manifest" <<'EOF'
{
  "version": 2,
  "scripts": [
    "10-verify.sh"
  ],
  "files": [
    "blob.bin",
    "note.txt"
  ]
}
EOF
cat > "$C/disc/10-verify.sh" <<EOF
#!/bin/sh
set -e
# via env var
got="\$(sha256sum "\$FIRSTBOOT_FILES_DIR/blob.bin" | cut -d' ' -f1)"
[ "\$got" = "$BLOB_SHA" ] || { echo "sha mismatch"; exit 1; }
# via relative path (runner sets cwd to the work dir)
[ -f files/note.txt ] || { echo "relative path missing"; exit 1; }
echo verified > "$C/verified"
EOF

rc=0; run_runner "$C" || rc=$?
check "exit 0" "$([ $rc -eq 0 ]; echo $?)"
check "verify script saw byte-identical payload + relative path" "$([ -f "$C/verified" ]; echo $?)"
check "manifest version 2 logged" "$(grep -q 'Manifest version 2' "$C/state/firstboot.log"; echo $?)"

# ---------------------------------------------------------------------------
echo "case 3: failing script aborts the run, later scripts never run"
C="$TMP/c3"; mkdir -p "$C/disc"
cat > "$C/disc/firstboot.manifest" <<'EOF'
{
  "version": 2,
  "scripts": [
    "10-ok.sh",
    "20-boom.sh",
    "30-never.sh"
  ],
  "files": []
}
EOF
printf '#!/bin/sh\ntrue\n' > "$C/disc/10-ok.sh"
printf '#!/bin/sh\necho boom >&2\nexit 3\n' > "$C/disc/20-boom.sh"
printf '#!/bin/sh\ntouch "%s/never"\n' "$C" > "$C/disc/30-never.sh"

rc=0; run_runner "$C" || rc=$?
check "exit 1" "$([ $rc -eq 1 ]; echo $?)"
check "error log written" "$(grep -q "exited with code 3" "$C/state/firstboot-error.log"; echo $?)"
check "third script never ran" "$([ ! -f "$C/never" ]; echo $?)"
check "no sentinel on failure" "$([ ! -f "$C/state/done" ]; echo $?)"

# ---------------------------------------------------------------------------
echo "case 4: invalid JSON manifest fails cleanly"
C="$TMP/c4"; mkdir -p "$C/disc"
echo '{not json' > "$C/disc/firstboot.manifest"

rc=0; run_runner "$C" || rc=$?
check "exit 1" "$([ $rc -eq 1 ]; echo $?)"
check "error log mentions JSON" "$(grep -q 'not valid JSON' "$C/state/firstboot-error.log"; echo $?)"

# ---------------------------------------------------------------------------
echo "case 5: missing payload file listed in manifest fails cleanly"
C="$TMP/c5"; mkdir -p "$C/disc"
cat > "$C/disc/firstboot.manifest" <<'EOF'
{
  "version": 2,
  "scripts": [
    "10-a.sh"
  ],
  "files": [
    "ghost.bin"
  ]
}
EOF
printf '#!/bin/sh\ntrue\n' > "$C/disc/10-a.sh"

rc=0; run_runner "$C" || rc=$?
check "exit 1" "$([ $rc -eq 1 ]; echo $?)"
check "error names the missing file" "$(grep -q "ghost.bin" "$C/state/firstboot-error.log"; echo $?)"

# ---------------------------------------------------------------------------
echo "case 6: newer manifest version proceeds with a warning"
C="$TMP/c6"; mkdir -p "$C/disc"
cat > "$C/disc/firstboot.manifest" <<'EOF'
{
  "version": 3,
  "scripts": [
    "10-a.sh"
  ],
  "files": [],
  "future": {"unknown": true}
}
EOF
printf '#!/bin/sh\ntrue\n' > "$C/disc/10-a.sh"

rc=0; run_runner "$C" || rc=$?
check "exit 0" "$([ $rc -eq 0 ]; echo $?)"
check "warning logged" "$(grep -q 'WARN: manifest version 3' "$C/state/firstboot.log"; echo $?)"

# ---------------------------------------------------------------------------
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
