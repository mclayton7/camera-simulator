#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_headless.sh — Launch CamSim on a headless Linux server (Xvfb + Vulkan).
#
# Requires:
#   - NVIDIA GPU with Vulkan support (nvidia-driver, vulkan-utils)
#   - Xvfb: apt install xvfb
#   - GStreamer + Python sidecar deps installed
#
# Usage:
#   export CESIUM_ION_TOKEN="your_token_here"
#   ./scripts/run_headless.sh
#
# Environment variables: same as run_desktop.sh plus:
#   DISPLAY_NUM    — Xvfb display number (default: 99)
#   XVFB_RES      — Xvfb resolution (default: 1920x1080x24)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_FILE="${REPO_ROOT}/UnrealProject/CameraSimulator.uproject"

CAMSIM_HOST="${CAMSIM_HOST:-239.1.1.1}"
CAMSIM_PORT="${CAMSIM_PORT:-5004}"
CAMSIM_BITRATE="${CAMSIM_BITRATE:-4000}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
XVFB_RES="${XVFB_RES:-1920x1080x24}"

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------
if [[ -z "${CESIUM_ION_TOKEN:-}" ]]; then
  echo "ERROR: CESIUM_ION_TOKEN is not set."
  exit 1
fi

if [[ -z "${UE5_BIN:-}" ]]; then
  for candidate in \
    "${HOME}/UnrealEngine/Engine/Binaries/Linux/UnrealEditor" \
    "/opt/UnrealEngine/Engine/Binaries/Linux/UnrealEditor" \
  ; do
    if [[ -f "${candidate}" ]]; then
      UE5_BIN="${candidate}"
      break
    fi
  done
fi

if [[ -z "${UE5_BIN:-}" ]]; then
  echo "ERROR: Cannot find UnrealEditor. Set UE5_BIN=/path/to/UnrealEditor"
  exit 1
fi

echo "=== CamSim Headless Run ==="
echo "  Project:  ${PROJECT_FILE}"
echo "  Display:  :${DISPLAY_NUM} (${XVFB_RES})"
echo "  Output:   udp://${CAMSIM_HOST}:${CAMSIM_PORT}"
echo ""

# ---------------------------------------------------------------------------
# Start Xvfb
# ---------------------------------------------------------------------------
XVFB_PID=""
echo "[xvfb] Starting Xvfb on :${DISPLAY_NUM}..."
Xvfb ":${DISPLAY_NUM}" -screen 0 "${XVFB_RES}" -ac -noreset &
XVFB_PID=$!
export DISPLAY=":${DISPLAY_NUM}"

# Give Xvfb a moment to initialize
sleep 2
echo "[xvfb] PID ${XVFB_PID}, DISPLAY=${DISPLAY}"

# ---------------------------------------------------------------------------
# Start sidecar
# ---------------------------------------------------------------------------
echo "[sidecar] Starting GStreamer sidecar..."
python3 -m camsim_sidecar \
  --host "${CAMSIM_HOST}" \
  --port "${CAMSIM_PORT}" \
  --bitrate "${CAMSIM_BITRATE}" \
  --wait-shm 120 \
  --log-level INFO \
  &
SIDECAR_PID=$!
echo "[sidecar] PID ${SIDECAR_PID}"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  echo ""
  echo "[cleanup] Stopping..."
  [[ -n "${SIDECAR_PID}" ]] && kill "${SIDECAR_PID}" 2>/dev/null || true
  [[ -n "${UE_PID:-}" ]]    && kill "${UE_PID}"    2>/dev/null || true
  [[ -n "${XVFB_PID}" ]]   && kill "${XVFB_PID}"  2>/dev/null || true
  rm -f /dev/shm/camsim_frames /dev/shm/camsim_telemetry 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Launch Unreal Engine (Vulkan, no editor UI)
# ---------------------------------------------------------------------------
echo "[ue5] Launching Unreal Engine (headless/Vulkan)..."
export CESIUM_ION_TOKEN
"${UE5_BIN}" "${PROJECT_FILE}" \
  -log \
  -nullrhi \
  -RHI=Vulkan \
  -unattended \
  -windowed \
  -ResX=1920 -ResY=1080 \
  -nosplash \
  &
UE_PID=$!
echo "[ue5] PID ${UE_PID}"

wait "${UE_PID}"
echo "[ue5] Unreal Engine exited."
