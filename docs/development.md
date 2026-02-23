# Development Guide

## Prerequisites

### Required

| Dependency | Version | Install |
|-----------|---------|---------|
| Unreal Engine | 5.3 or 5.4 | [Epic Games Launcher](https://www.unrealengine.com) |
| Cesium for Unreal | ≥ 2.0 | Fab Marketplace / Cesium plugin page |
| Python | ≥ 3.11 | System package or [pyenv](https://github.com/pyenv/pyenv) |
| GStreamer | ≥ 1.22 | See GStreamer section below |
| posix-ipc | ≥ 1.1.1 | `pip install posix-ipc` |

### Optional (for GPU encoding)

| Dependency | Notes |
|-----------|-------|
| NVIDIA GPU (Turing or later) | Required for `nvh264enc` (NVENC) |
| nvidia-gstreamer | `gstreamer1.0-plugins-bad-nvidia` on Ubuntu |
| CUDA | Not required by CamSim but often co-installed with NVIDIA drivers |

---

## Setting Up the UE5 Project

### 1. Install Cesium for Unreal

**From Fab (recommended):**
1. Go to https://www.fab.com and search for "Cesium for Unreal".
2. Add to your account, then install through the Epic Games Launcher →
   Library → Plugins for your UE5.3 installation.

**From source (for engine versions not yet on Fab):**
```bash
git clone https://github.com/CesiumGS/cesium-unreal
# Follow CesiumGS build instructions for your platform
```

### 2. Generate Project Files

```bash
# macOS / Linux
cd UnrealProject
/path/to/UnrealEngine/Engine/Build/BatchFiles/Mac/GenerateProjectFiles.sh \
    CameraSimulator.uproject

# Windows (run from Developer Command Prompt)
"C:\Program Files\Epic Games\UE_5.3\Engine\Build\BatchFiles\GenerateProjectFiles.bat" \
    CameraSimulator.uproject
```

### 3. Build the Plugin

Open `CameraSimulator.uproject` in the Unreal Editor. On first open, the editor
will prompt to build missing modules — click **Yes**. If compilation fails,
open the Output Log (Window → Developer Tools → Output Log) for details.

To build from the command line:
```bash
# Linux / macOS
/path/to/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh \
    CameraSimulatorEditor Linux Development \
    -Project="$(pwd)/UnrealProject/CameraSimulator.uproject" \
    -WaitMutex

# Windows
"C:\Program Files\Epic Games\UE_5.3\Engine\Build\BatchFiles\Build.bat" ^
    CameraSimulatorEditor Win64 Development ^
    -Project="CameraSimulator.uproject"
```

### 4. Set Up the Level

Open `Content/Maps/SimMain.umap` (or create a new level if it does not exist yet):

1. **Add a CesiumGeoreference actor:**
   Place → All Classes → CesiumGeoreference. Set the origin near your intended
   flight area (e.g. Death Valley: lat 36.5, lon −117.5).

2. **Add a Cesium World Terrain tileset:**
   CesiumGeoreference details → Quick Add → Cesium World Terrain + Bing Maps.

3. **Set your Cesium ion token:**
   CesiumGeoreference details → Cesium Ion → Access Token.
   Or set the `CESIUM_ION_TOKEN` environment variable before launching (see
   `DefaultEngine.ini` → `[CesiumIonToken]`).

4. **Place an AircraftKinematicActor:**
   Place → All Classes → AircraftKinematicActor.
   Set InitialLat/Lon/Alt to your desired starting position.

5. **Save the level** as `Content/Maps/SimMain.umap`.

---

## Installing GStreamer

### Ubuntu / Debian
```bash
sudo apt install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0

# Optional NVENC support
sudo apt install gstreamer1.0-plugins-bad-nvidia
```

### macOS (Homebrew)
```bash
brew install gstreamer gst-plugins-base gst-plugins-good \
             gst-plugins-bad gst-plugins-ugly gst-libav
pip install PyGObject
```

### Verify GStreamer installation
```bash
# Check nvh264enc is available
gst-inspect-1.0 nvh264enc
# Output: "Element not found" means software fallback will be used

# Verify mpegtsmux and meta/x-klv support
gst-inspect-1.0 mpegtsmux

# Test a minimal pipeline (no UE5 required)
gst-launch-1.0 videotestsrc num-buffers=90 \
    ! videoconvert ! x264enc ! h264parse \
    ! mpegtsmux \
    ! udpsink host=127.0.0.1 port=5004
```

---

## Installing the Python Sidecar

```bash
# From repo root
pip install -e sidecar/

# Verify
python -m camsim_sidecar --help
```

---

## Running Tests

```bash
# KLV encoder unit tests (no UE5 required)
cd sidecar
pytest test_klv_encoder.py -v

# With paretech/klvdata for reference cross-check
pip install klvdata
pytest test_klv_encoder.py -v -k "round_trip"
```

Expected output: all tests pass within quantisation tolerance. See
`docs/klv-reference.md` for the resolution of each tag.

---

## Running the Simulator

### Desktop (with GPU)

```bash
export CESIUM_ION_TOKEN="your_token_here"
pip install -e sidecar/
./scripts/run_desktop.sh
```

This script:
1. Validates `CESIUM_ION_TOKEN` is set.
2. Finds the `UnrealEditor` binary.
3. Starts the Python sidecar in the background (waits up to 60 s for shm).
4. Launches Unreal Engine with the project.
5. On exit, kills the sidecar and removes stale shm regions.

### Headless Linux Server

```bash
sudo apt install xvfb
export CESIUM_ION_TOKEN="your_token_here"
./scripts/run_headless.sh
```

Uses Xvfb display `:99` with a 1920×1080×24 virtual framebuffer. Unreal
Engine runs with `-RHI=Vulkan` for offscreen rendering.

### Starting Components Manually

```bash
# Terminal 1: UE5 (in editor or packaged)
export CESIUM_ION_TOKEN="your_token_here"
/path/to/UnrealEditor UnrealProject/CameraSimulator.uproject -log

# Terminal 2: Sidecar
python -m camsim_sidecar \
    --host 239.1.1.1 --port 5004 \
    --width 1920 --height 1080 --fps 30 \
    --bitrate 4000 \
    --wait-shm 60 \
    --log-level DEBUG
```

---

## Receiving the Stream

```bash
# VLC (with KLV metadata display if plugin installed)
vlc --demux=ts "udp://@239.1.1.1:5004"

# mpv (video only)
mpv udp://239.1.1.1:5004

# Unicast (replace with your receiver IP)
python -m camsim_sidecar --host 192.168.1.100 --port 5004 --no-multicast

# Inspect MPEG-TS packets and KLV tags
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004
```

---

## Sending Commands

```bash
# Slew pan at +10 deg/s for 5 seconds
python tools/inject_commands.py slew-pan --rate 10 --duration 5

# Slew tilt at -5 deg/s for 3 seconds
python tools/inject_commands.py slew-tilt --rate -5 --duration 3

# Both axes simultaneously
python tools/inject_commands.py slew-both --pan-rate 10 --tilt-rate -5 --duration 3

# Teleport aircraft
python tools/inject_commands.py set-position --lat 36.5 --lon -117.5 --alt 2000

# Set heading
python tools/inject_commands.py set-heading --heading 270

# Set airspeed
python tools/inject_commands.py set-speed --speed 150

# Absolute gimbal position
python tools/inject_commands.py gimbal-abs --pan 0 --tilt -45

# Set full aircraft state (position + attitude + speed)
python tools/inject_commands.py set-flight-state \
    --lat 36.5 --lon -117.5 --alt 1500 \
    --heading 90 --pitch 3 --roll 25 --speed 100

# Ping (verify CommandReceiver is alive)
python tools/inject_commands.py ping
```

To target a remote machine:
```bash
python tools/inject_commands.py --host 192.168.1.50 --port 5005 ping
```

---

## Decoding a KLV Packet

```bash
# Decode a hex KLV packet on the command line
python tools/klv_decoder.py 060e2b34020b01010e0103010100000082011f02...

# Use as a library
python3 -c "
import sys; sys.path.insert(0, '.')
from tools.klv_decoder import decode_klv_packet
raw = bytes.fromhex('060e2b34...')
tags = decode_klv_packet(raw)
print(tags)
"
```

---

## Common Issues

### "No CesiumGeoreference found in level"

Logged by `AircraftKinematicActor::BeginPlay`. The level does not contain a
`CesiumGeoreference` actor. Add one via the Cesium menu or Place mode and save
the level.

### "shm_open failed errno=2" (Linux)

The POSIX shared memory region doesn't exist yet. This is normal if the
sidecar starts before Unreal has reached `BeginPlay`. Use `--wait-shm 120` to
wait up to 2 minutes.

### "nvh264enc not found — falling back to x264enc"

The `gstreamer1.0-plugins-bad-nvidia` package is not installed, or the NVIDIA
container runtime is not active (Docker). The sidecar will silently fall back
to software encoding. Output quality and bitrate are unchanged; latency
increases by ~15–25 ms.

### MPEG-TS stream visible in recv_and_inspect but VLC shows no video

Check that the H.264 stream is getting key frames at the expected interval.
Increase `gop-size` (NVENC) or `key-int-max` (x264) if GOP is too long. VLC
requires an IDR frame to begin decoding.

### Tag 18 (sensor azimuth) not changing after slew command

1. Confirm the command is being received: run `inject_commands.py ping` and
   watch the Unreal log for "Ping received".
2. Check that `CommandReceiver` bound to port 5005: look for "listening on UDP
   port 5005" in the Unreal log at startup.
3. Verify the gimbal is not at its pan limit (±170°): send
   `gimbal-abs --pan 0 --tilt -45` to reset, then slew again.

### ReadPixels stalls (low FPS)

`ReadPixels` blocks until the GPU finishes rendering. On low-end hardware
this can drop below 30 fps. Consider:
- Reducing capture resolution (`CaptureWidth` / `CaptureHeight`).
- Disabling Lumen and Nanite for the simulation scene.
- Using `-RenderOffscreen` to avoid window compositing overhead.

---

## Code Organisation Notes

### Adding a New Unreal Component

1. Add `MyComponent.h` to `Source/CamSimPlugin/Public/`.
2. Add `MyComponent.cpp` to `Source/CamSimPlugin/Private/`.
3. Declare the component as `UPROPERTY()` in `AircraftKinematicActor.h`.
4. Create it with `CreateDefaultSubobject<UMyComponent>` in the constructor.
5. Wire it via a `SetXxx()` method called from `AircraftKinematicActor::BeginPlay`.

### Keeping SharedMemoryTypes.h and shm_reader.py in Sync

Both files define the same binary layout. If you change any field in a struct:
- Update the C++ `#pragma pack(1)` struct in `SharedMemoryTypes.h`.
- Update the matching `ctypes.Structure` in `shm_reader.py`.
- Update the offset table in `docs/ipc-protocol.md`.
- Run `pytest sidecar/test_klv_encoder.py` to catch any layout changes that
  affect telemetry encoding.

A mismatch in struct layout will produce silently wrong telemetry values, not a
crash, so always verify with `recv_and_inspect.py` after layout changes.
