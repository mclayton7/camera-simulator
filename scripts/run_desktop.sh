#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_desktop.sh — Launch CamSim on a desktop workstation (GPU available).
#
# Usage:
#   export CESIUM_ION_TOKEN="your_token_here"
#   ./scripts/run_desktop.sh [--no-sidecar] [--software-encode]
#
# Environment variables:
#   CESIUM_ION_TOKEN   — required: Cesium ion API token (never commit!)
#   UE5_BIN            — path to UE5 editor binary (auto-detected if omitted)
#   CAMSIM_HOST        — UDP destination (default: 239.1.1.1)
#   CAMSIM_PORT        — UDP port (default: 5004)
#   CAMSIM_BITRATE     — H.264 bitrate kbps (default: 4000)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_FILE="${REPO_ROOT}/UnrealProject/CameraSimulator.uproject"

CAMSIM_HOST="${CAMSIM_HOST:-239.1.1.1}"
CAMSIM_PORT="${CAMSIM_PORT:-5004}"
CAMSIM_BITRATE="${CAMSIM_BITRATE:-4000}"

START_SIDECAR=true
SOFTWARE_ENCODE=false

# Parse flags
for arg in "$@"; do
  case $arg in
    --no-sidecar)       START_SIDECAR=false ;;
    --software-encode)  SOFTWARE_ENCODE=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------
if [[ -z "${CESIUM_ION_TOKEN:-}" ]]; then
  echo "ERROR: CESIUM_ION_TOKEN is not set."
  echo "  Get a free token at https://ion.cesium.com"
  echo "  Then: export CESIUM_ION_TOKEN=your_token"
  exit 1
fi

# ---------------------------------------------------------------------------
# Find Unreal Engine binary
# ---------------------------------------------------------------------------
if [[ -z "${UE5_BIN:-}" ]]; then
  for candidate in \
    "${HOME}/UnrealEngine/Engine/Binaries/Linux/UnrealEditor" \
    "/opt/UnrealEngine/Engine/Binaries/Linux/UnrealEditor" \
    "/Applications/Epic Games/UE_5.3/Engine/Binaries/Mac/UnrealEditor.app/Contents/MacOS/UnrealEditor" \
    "C:/Program Files/Epic Games/UE_5.3/Engine/Binaries/Win64/UnrealEditor.exe" \
  ; do
    if [[ -f "${candidate}" || -f "${candidate}.exe" ]]; then
      UE5_BIN="${candidate}"
      break
    fi
  done
fi

if [[ -z "${UE5_BIN:-}" ]]; then
  echo "ERROR: Cannot find UnrealEditor binary."
  echo "  Set UE5_BIN=/path/to/UnrealEditor"
  exit 1
fi

echo "=== CamSim Desktop Run ==="
echo "  Project:  ${PROJECT_FILE}"
echo "  UE5 bin:  ${UE5_BIN}"
echo "  Output:   udp://${CAMSIM_HOST}:${CAMSIM_PORT}"
echo ""

# ---------------------------------------------------------------------------
# Launch sidecar in background
# ---------------------------------------------------------------------------
SIDECAR_PID=""
if $START_SIDECAR; then
  SW_FLAG=""
  $SOFTWARE_ENCODE && SW_FLAG="--software"

  echo "[sidecar] Starting GStreamer sidecar..."
  python3 -m camsim_sidecar \
    --host "${CAMSIM_HOST}" \
    --port "${CAMSIM_PORT}" \
    --bitrate "${CAMSIM_BITRATE}" \
    ${SW_FLAG} \
    --wait-shm 60 \
    --log-level INFO \
    &
  SIDECAR_PID=$!
  echo "[sidecar] PID ${SIDECAR_PID}"
fi

# Cleanup on exit
cleanup() {
  echo ""
  echo "[cleanup] Stopping..."
  [[ -n "${SIDECAR_PID}" ]] && kill "${SIDECAR_PID}" 2>/dev/null || true
  [[ -n "${UE_PID:-}" ]]    && kill "${UE_PID}"    2>/dev/null || true
  # Remove stale shm regions
  rm -f /dev/shm/camsim_frames /dev/shm/camsim_telemetry 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Launch Unreal Engine
# ---------------------------------------------------------------------------
echo "[ue5] Launching Unreal Engine..."
export CESIUM_ION_TOKEN
"${UE5_BIN}" "${PROJECT_FILE}" \
  -log \
  -windowed \
  -ResX=1280 -ResY=720 \
  &
UE_PID=$!
echo "[ue5] PID ${UE_PID}"

# Wait for either process to exit
wait "${UE_PID}"
echo "[ue5] Unreal Engine exited."
