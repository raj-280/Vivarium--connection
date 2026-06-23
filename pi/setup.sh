#!/usr/bin/env bash
# =============================================================================
# Vivarium Gantry System — Pi Setup Script
# =============================================================================
#
# Usage:
#   sudo bash setup.sh
#
# What this script does (in order):
#   1.  Print banner + check prerequisites
#   2.  Prompt for server URL, provisioning secret, MQTT broker host
#   3.  Assert mediamtx/mediamtx.example.yaml exists (provisioner needs it)
#   4.  Install Python dependencies (pip3 install -r requirements.txt)
#   5.  Download and install the MediaMTX binary
#   6.  Write a temporary provisioner.env file with the credentials
#   7.  Run provisioner.py  (writes /etc/vivarium/device.conf +
#           mediamtx/mediamtx.yaml, then exits)
#   8.  Verify mediamtx/mediamtx.yaml was written
#   9.  Create capture directory /var/vivarium/{device_id}/captures
#       (written to device.conf [capture] capture_dir by provisioner.py)
#  10.  Delete provisioner.env (credentials no longer needed on disk)
#  11.  Patch + install the three systemd service files
#  12.  Enable + start all services
#  13.  Print summary + log viewing hints
#
# After this script completes, the Pi will:
#   • Run provisioner.py automatically on every boot (no-op if already done)
#   • Keep bridge.py running (auto-restart on crash)
#   • Keep MediaMTX running for live video streaming
#   • Appear in the server DB and frontend within seconds
#
# Key paths (single source of truth — must match provisioner.py + settings.py):
#   Config file : /etc/vivarium/device.conf     (written by provisioner.py)
#   MediaMTX bin: /usr/local/bin/mediamtx       (installed in step 5)
#   MediaMTX cfg: {INSTALL_DIR}/mediamtx/mediamtx.yaml  (written by provisioner.py)
#   Capture dir : /var/vivarium/{device_id}/captures     (created in step 9)
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLD='\033[1m'
RST='\033[0m'

ok()   { echo -e "${GRN}  ✓  ${RST}$*"; }
info() { echo -e "${YLW}  →  ${RST}$*"; }
err()  { echo -e "${RED}  ✗  ${RST}$*" >&2; }
die()  { err "$*"; exit 1; }

# ── Resolve install directory (absolute path of this script's parent) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Canonical system paths — must stay in sync with provisioner.py and ────────
# ── settings.py (DEVICE_CONF_PATH) ────────────────────────────────────────────
DEVICE_CONF_PATH="/etc/vivarium/device.conf"
MEDIAMTX_BIN="/usr/local/bin/mediamtx"

# BUG FIX (Bug 6 — Unpinned MediaMTX version):
# Previously setup.sh fetched the GitHub "latest" release, meaning any future
# MediaMTX major release could silently change the YAML schema and crash
# vivarium-camera.service on the next fresh install.  This is exactly how the
# api: struct vs bool mismatch came about in the first place.
#
# We now pin to a specific tested version.  To upgrade MediaMTX:
#   1. Read the MediaMTX changelog for any YAML-breaking changes.
#   2. Update mediamtx.example.yaml if the schema changed.
#   3. Bump MEDIAMTX_VERSION below.
#   4. Test on a real Pi before rolling out to all units.
MEDIAMTX_VERSION="v1.9.3"   # pinned — only change after reading the changelog

MEDIAMTX_TEMPLATE="${SCRIPT_DIR}/mediamtx/mediamtx.example.yaml"
MEDIAMTX_YAML="${SCRIPT_DIR}/mediamtx/mediamtx.yaml"
PROVISIONER_ENV="${SCRIPT_DIR}/provisioner.env"

# ── Service user / group detection ───────────────────────────────────────────
# $SUDO_USER is set by sudo to the name of the original (non-root) caller.
# We use it to stamp User= / Group= into the service files and to chown
# device.conf and mediamtx.yaml so the bridge and camera services can read
# them. Must be resolved before the banner so any error message is clean.
SERVICE_USER="${SUDO_USER:-}"
if [[ -z "${SERVICE_USER}" || "${SERVICE_USER}" == "root" ]]; then
    # Fallback: logname works in some su / CI environments.
    SERVICE_USER="$(logname 2>/dev/null || true)"
fi
if [[ -z "${SERVICE_USER}" || "${SERVICE_USER}" == "root" ]]; then
    echo -e "${RED}  ✗  ${RST}Cannot determine the non-root service user." >&2
    echo -e "       Run this script with sudo from your normal account:" >&2
    echo -e "           sudo bash setup.sh" >&2
    echo -e "       Do NOT run it directly as root (e.g. via  su -)." >&2
    exit 1
fi
# Primary group of the service user.
SERVICE_GROUP="$(id -gn "${SERVICE_USER}" 2>/dev/null || echo "${SERVICE_USER}")"

# =============================================================================
# 1. Banner + prerequisites check
# =============================================================================

echo ""
echo -e "${BLD}╔══════════════════════════════════════════════════════════╗${RST}"
echo -e "${BLD}║        Vivarium Gantry System — Pi Setup Script          ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════════════════════════╝${RST}"
echo ""
info "Install directory: ${SCRIPT_DIR}"
info "Service user/group: ${SERVICE_USER}:${SERVICE_GROUP}"
echo ""

# Must run as root to create /etc/vivarium/ and write systemd unit files.
if [[ $EUID -ne 0 ]]; then
    die "Please run with sudo:  sudo bash setup.sh"
fi

# Check for Python 3
if ! command -v python3 &>/dev/null; then
    die "python3 not found. Install it with: sudo apt install python3 python3-pip"
fi
ok "python3 found: $(python3 --version)"

# Check for pip3
if ! command -v pip3 &>/dev/null; then
    die "pip3 not found. Install it with: sudo apt install python3-pip"
fi
ok "pip3 found"

# Check for curl (needed to download MediaMTX)
if ! command -v curl &>/dev/null; then
    die "curl not found. Install it with: sudo apt install curl"
fi
ok "curl found"

echo ""

# =============================================================================
# 2. Interactive prompts — typed in, never written to a permanent file
# =============================================================================

echo -e "${BLD}── Configuration ─────────────────────────────────────────────────${RST}"
echo ""
echo "  These values are used ONCE to provision this Pi."
echo "  They will NOT be stored on disk after provisioning completes."
echo ""

# Server URL
read -r -p "  Server URL         [http://localhost:8000] : " INPUT_SERVER_URL
GANTRY_SERVER_URL="${INPUT_SERVER_URL:-http://localhost:8000}"
ok "Server URL: ${GANTRY_SERVER_URL}"

# MQTT broker host (default: same host as server URL, port stripped).
# Stored as env var GANTRY_MQTT_BROKER → provisioner.py reads it as
# os.environ.get("GANTRY_MQTT_BROKER") and writes it to device.conf [mqtt] broker_host.
DEFAULT_MQTT_HOST="$(echo "${GANTRY_SERVER_URL}" | sed 's|http[s]*://||' | sed 's|:.*||')"
read -r -p "  MQTT broker host   [${DEFAULT_MQTT_HOST}] : " INPUT_MQTT_HOST
GANTRY_MQTT_BROKER="${INPUT_MQTT_HOST:-${DEFAULT_MQTT_HOST}}"
ok "MQTT broker host: ${GANTRY_MQTT_BROKER}"

# Provisioning secret (hidden input).
# Passed to POST /provision as json field "provisioning_secret".
echo -n "  Provisioning secret (hidden): "
read -r -s GANTRY_PROVISIONING_SECRET
echo ""  # newline after hidden input
if [[ -z "${GANTRY_PROVISIONING_SECRET}" ]]; then
    die "Provisioning secret cannot be empty."
fi
ok "Provisioning secret: [set]"

echo ""

# =============================================================================
# 3. Assert MediaMTX template exists before doing anything irreversible
#    provisioner.py writes mediamtx/mediamtx.yaml by substituting placeholders
#    in this template.  If the template is missing, provisioner.py exits 0
#    (warning only) but mediamtx.yaml is never written → vivarium-camera.service
#    fails on start.  Catch it early here so the error is obvious.
# =============================================================================

echo -e "${BLD}── Pre-flight checks ──────────────────────────────────────────────${RST}"

if [[ ! -f "${MEDIAMTX_TEMPLATE}" ]]; then
    die "MediaMTX template not found: ${MEDIAMTX_TEMPLATE}
       This file must exist for provisioner.py to generate mediamtx/mediamtx.yaml.
       Ensure the full pi/ directory was cloned from the repository."
fi
ok "MediaMTX template found: ${MEDIAMTX_TEMPLATE}"

if [[ ! -f "${SCRIPT_DIR}/provisioner.py" ]]; then
    die "provisioner.py not found in ${SCRIPT_DIR}"
fi
ok "provisioner.py found"

if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    die "requirements.txt not found in ${SCRIPT_DIR}"
fi
ok "requirements.txt found"

echo ""

# =============================================================================
# 4. Install Python dependencies
# =============================================================================

echo -e "${BLD}── Python dependencies ────────────────────────────────────────────${RST}"
info "Running: pip3 install -r ${SCRIPT_DIR}/requirements.txt"
pip3 install --break-system-packages -q -r "${SCRIPT_DIR}/requirements.txt"
ok "Python dependencies installed"
echo ""

# =============================================================================
# 5. Download and install MediaMTX
# =============================================================================

echo -e "${BLD}── MediaMTX ────────────────────────────────────────────────────────${RST}"

if [[ -f "${MEDIAMTX_BIN}" ]]; then
    ok "MediaMTX already installed at ${MEDIAMTX_BIN} — skipping download"
else
    info "Detecting system architecture..."
    ARCH="$(uname -m)"
    case "${ARCH}" in
        aarch64 | arm64) MEDIAMTX_ARCH="linux_arm64v8" ;;
        armv7l)          MEDIAMTX_ARCH="linux_armv7"   ;;
        armv6l)          MEDIAMTX_ARCH="linux_armv6"   ;;
        x86_64)          MEDIAMTX_ARCH="linux_amd64"   ;;
        *) die "Unsupported architecture: ${ARCH}" ;;
    esac
    ok "Architecture: ${ARCH} → MediaMTX variant: ${MEDIAMTX_ARCH}"

    info "Fetching latest MediaMTX release..."
    MEDIAMTX_LATEST_URL="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
        | python3 -c "import sys,json; data=json.load(sys.stdin); \
          print(next(a['browser_download_url'] for a in data['assets'] \
          if '${MEDIAMTX_ARCH}' in a['name'] and a['name'].endswith('.tar.gz')))")"

    if [[ -z "${MEDIAMTX_LATEST_URL}" ]]; then
        die "Could not find MediaMTX download URL for ${MEDIAMTX_ARCH}"
    fi
    ok "Download URL: ${MEDIAMTX_LATEST_URL}"

    TMP_DIR="$(mktemp -d)"
    info "Downloading to ${TMP_DIR}..."
    curl -fsSL "${MEDIAMTX_LATEST_URL}" -o "${TMP_DIR}/mediamtx.tar.gz"
    tar -xzf "${TMP_DIR}/mediamtx.tar.gz" -C "${TMP_DIR}"
    cp "${TMP_DIR}/mediamtx" "${MEDIAMTX_BIN}"
    chmod +x "${MEDIAMTX_BIN}"
    rm -rf "${TMP_DIR}"
    ok "MediaMTX installed to ${MEDIAMTX_BIN}"
fi

echo ""

# =============================================================================
# 6. Write temporary provisioner.env
#    Contains the env vars provisioner.py reads (via os.environ.get):
#      GANTRY_SERVER_URL          → SERVER_URL constant in provisioner.py
#      GANTRY_PROVISIONING_SECRET → PROVISIONING_SECRET_ENV in provisioner.py
#      GANTRY_MQTT_BROKER         → written to device.conf [mqtt] broker_host
#      GANTRY_SERVICE_USER        → used by provisioner.py for chown of
#                                   device.conf and mediamtx.yaml
#    File is mode 600 (root-only) and deleted immediately after step 10.
#    The vivarium-provisioner.service EnvironmentFile line references this
#    path with a leading "-" (optional) — it is only needed if the service
#    re-runs after device.conf is deleted for re-provisioning.
# =============================================================================

echo -e "${BLD}── Provisioning ───────────────────────────────────────────────────${RST}"

cat > "${PROVISIONER_ENV}" << EOF
GANTRY_SERVER_URL=${GANTRY_SERVER_URL}
GANTRY_PROVISIONING_SECRET=${GANTRY_PROVISIONING_SECRET}
GANTRY_MQTT_BROKER=${GANTRY_MQTT_BROKER}
GANTRY_SERVICE_USER=${SERVICE_USER}
EOF
chmod 600 "${PROVISIONER_ENV}"
info "Temporary provisioner.env written (mode 600)"

# =============================================================================
# 7. Run provisioner.py
#    Writes two files:
#      /etc/vivarium/device.conf      (mode 600, owner SERVICE_USER)
#      mediamtx/mediamtx.yaml         (mode 600, owner SERVICE_USER)
#    Env vars are exported directly into the subshell so they are available
#    even if provisioner.env is somehow unreadable.
# =============================================================================

info "Running provisioner.py..."
(
    export GANTRY_SERVER_URL="${GANTRY_SERVER_URL}"
    export GANTRY_PROVISIONING_SECRET="${GANTRY_PROVISIONING_SECRET}"
    export GANTRY_MQTT_BROKER="${GANTRY_MQTT_BROKER}"
    export GANTRY_SERVICE_USER="${SERVICE_USER}"
    python3 "${SCRIPT_DIR}/provisioner.py"
)
ok "provisioner.py exited successfully"

# =============================================================================
# 8. Verify both output files were actually written
#    provisioner.py logs a warning and exits 0 if mediamtx.example.yaml is
#    missing — step 3 already guards against this, but we double-check here
#    so a regression can't silently leave the camera service broken.
# =============================================================================

if [[ ! -f "${DEVICE_CONF_PATH}" ]]; then
    die "provisioner.py did not write ${DEVICE_CONF_PATH} — check provisioner logs above."
fi
ok "${DEVICE_CONF_PATH} written"

if [[ ! -f "${MEDIAMTX_YAML}" ]]; then
    die "provisioner.py did not write ${MEDIAMTX_YAML}
       Check that ${MEDIAMTX_TEMPLATE} exists and is readable."
fi
ok "${MEDIAMTX_YAML} written"

# =============================================================================
# 9. Create capture directory
#    provisioner.py writes:
#        [capture] capture_dir = /var/vivarium/{device_id}/captures
#    to device.conf.  settings.py reads this path; camera_handler.py writes
#    captures there.  Neither provisioner.py nor bridge.py creates the
#    directory itself, so first-capture would fail with FileNotFoundError
#    unless we create it here.
# =============================================================================

DEVICE_ID="$(python3 -c "
import configparser
p = configparser.ConfigParser()
p.read('${DEVICE_CONF_PATH}')
print(p.get('identity', 'device_id', fallback=''))
" 2>/dev/null || true)"

if [[ -z "${DEVICE_ID}" ]]; then
    err "Could not read device_id from ${DEVICE_CONF_PATH} — skipping capture directory creation."
else
    CAPTURE_DIR="/var/vivarium/${DEVICE_ID}/captures"
    mkdir -p "${CAPTURE_DIR}"
    # bridge.py and camera_handler.py run as SERVICE_USER — ensure they own the dir.
    if id -u "${SERVICE_USER}" &>/dev/null; then
        chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "/var/vivarium/${DEVICE_ID}"
    fi
    chmod 750 "/var/vivarium/${DEVICE_ID}"
    ok "Capture directory created: ${CAPTURE_DIR}"
fi

# =============================================================================
# 10. Delete provisioner.env — credentials no longer needed on disk
# =============================================================================

rm -f "${PROVISIONER_ENV}"
ok "provisioner.env deleted (credentials cleared from disk)"

echo ""

# =============================================================================
# 11. Install systemd service files
#     Three placeholders are substituted into each installed unit file:
#       {{INSTALL_DIR}}   — absolute path to the pi/ directory
#       {{SERVICE_USER}}  — non-root user detected from $SUDO_USER
#       {{SERVICE_GROUP}} — primary group of that user
#     vivarium-bridge and vivarium-camera use all three.
#     vivarium-provisioner keeps User=root intentionally (needs root to
#     create /etc/vivarium/) but {{INSTALL_DIR}} is still substituted.
# =============================================================================

echo -e "${BLD}── systemd services ───────────────────────────────────────────────${RST}"

SYSTEMD_SRC="${SCRIPT_DIR}/systemd"
SYSTEMD_DEST="/etc/systemd/system"

for SERVICE in vivarium-provisioner vivarium-bridge vivarium-camera; do
    SRC_FILE="${SYSTEMD_SRC}/${SERVICE}.service"
    DEST_FILE="${SYSTEMD_DEST}/${SERVICE}.service"

    if [[ ! -f "${SRC_FILE}" ]]; then
        err "Service file not found: ${SRC_FILE} — skipping"
        continue
    fi

    # Substitute all placeholders written by setup.sh into the installed unit file:
    #   {{INSTALL_DIR}}    — absolute path to the pi/ directory
    #   {{SERVICE_USER}}   — non-root user detected from $SUDO_USER
    #   {{SERVICE_GROUP}}  — primary group of that user
    sed \
        -e "s|{{INSTALL_DIR}}|${SCRIPT_DIR}|g" \
        -e "s|{{SERVICE_USER}}|${SERVICE_USER}|g" \
        -e "s|{{SERVICE_GROUP}}|${SERVICE_GROUP}|g" \
        "${SRC_FILE}" > "${DEST_FILE}"
    chmod 644 "${DEST_FILE}"
    ok "Installed ${SERVICE}.service → ${DEST_FILE}"
done
# Add SERVICE_USER to the "video" group — required by vivarium-camera.service
# which sets SupplementaryGroups=video so MediaMTX can access /dev/video* and
# /dev/dma_heap (Pi Camera). Without this the camera service gets permission
# denied on the camera device even though it starts successfully.
if id -u "${SERVICE_USER}" &>/dev/null; then
    usermod -aG video "${SERVICE_USER}"
    ok "Added ${SERVICE_USER} to video group (camera access)"
else
    err "Could not add ${SERVICE_USER} to video group — user not found"
fi
# Reload systemd to pick up new unit files
systemctl daemon-reload
ok "systemd daemon reloaded"

echo ""

# =============================================================================
# 12. Enable + start services
# =============================================================================

echo -e "${BLD}── Starting services ──────────────────────────────────────────────${RST}"

# Enable all three on boot.
systemctl enable vivarium-provisioner vivarium-bridge vivarium-camera
ok "Services enabled on boot"

# vivarium-provisioner is oneshot — it already ran in step 7 and
# device.conf now exists, so ConditionPathExists=!/etc/vivarium/device.conf
# causes it to exit 0 immediately (no-op).  We start it explicitly so that
# systemd marks it "active" and vivarium-bridge's Requires= is satisfied.
systemctl start vivarium-provisioner
ok "vivarium-provisioner started (no-op — device.conf already written)"

# Start camera first so the RTSP stream is ready before the bridge connects.
systemctl start vivarium-camera
ok "vivarium-camera started (MediaMTX live stream)"

systemctl start vivarium-bridge
ok "vivarium-bridge started (MQTT ↔ Serial ↔ Arduino)"

echo ""

# =============================================================================
# 13. Summary
# =============================================================================

echo -e "${BLD}╔══════════════════════════════════════════════════════════╗${RST}"
echo -e "${BLD}║                    Setup Complete! ✓                     ║${RST}"
echo -e "${BLD}╚══════════════════════════════════════════════════════════╝${RST}"
echo ""
echo -e "  ${BLD}Device ID:${RST}        ${GRN}${DEVICE_ID:-unknown}${RST}"
echo -e "  ${BLD}Service user:${RST}     ${SERVICE_USER}:${SERVICE_GROUP}"
echo -e "  ${BLD}Server:${RST}           ${GANTRY_SERVER_URL}"
echo -e "  ${BLD}MQTT broker:${RST}      ${GANTRY_MQTT_BROKER}"
echo -e "  ${BLD}Config file:${RST}      ${DEVICE_CONF_PATH}"
echo -e "  ${BLD}MediaMTX config:${RST}  ${MEDIAMTX_YAML}"
if [[ -n "${DEVICE_ID}" ]]; then
    echo -e "  ${BLD}Capture dir:${RST}      /var/vivarium/${DEVICE_ID}/captures"
fi
echo ""
echo -e "  ${BLD}This Pi will now appear in the frontend as:  ${GRN}${DEVICE_ID:-unknown}${RST}"
echo ""
echo -e "  ${BLD}View logs:${RST}"
echo -e "    journalctl -u vivarium-bridge  -f   # MQTT/serial logs"
echo -e "    journalctl -u vivarium-camera  -f   # MediaMTX logs"
echo ""
echo -e "  ${BLD}Service status:${RST}"
echo -e "    systemctl status vivarium-bridge"
echo -e "    systemctl status vivarium-camera"
echo ""
