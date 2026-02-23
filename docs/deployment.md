# Deployment Guide

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CESIUM_ION_TOKEN` | **Yes** | — | Cesium ion API token. Get a free token at https://ion.cesium.com |
| `UE5_BIN` | No | auto-detected | Absolute path to the `UnrealEditor` binary |
| `CAMSIM_HOST` | No | `239.1.1.1` | UDP destination (multicast or unicast IP) |
| `CAMSIM_PORT` | No | `5004` | UDP destination port |
| `CAMSIM_BITRATE` | No | `4000` | H.264 bitrate in kbps |
| `GST_DEBUG` | No | `2` | GStreamer log level (0=none … 9=trace) |

> **Security:** `CESIUM_ION_TOKEN` is a credential. Set it in the shell
> environment or a `.env` file. **Never commit it to git** — `.gitignore`
> blocks `.env` files.

---

## Desktop (GPU Workstation)

### Quick Start

```bash
export CESIUM_ION_TOKEN="your_token_here"
pip install -e sidecar/
./scripts/run_desktop.sh
```

### Manual Start

```bash
# Terminal 1 — Sidecar
export CESIUM_ION_TOKEN="your_token_here"
python -m camsim_sidecar \
    --host 239.1.1.1 \
    --port 5004 \
    --bitrate 4000 \
    --wait-shm 60

# Terminal 2 — Unreal Engine
export CESIUM_ION_TOKEN="your_token_here"
/path/to/UnrealEditor UnrealProject/CameraSimulator.uproject \
    -log -windowed -ResX=1280 -ResY=720
```

### Script Options

```
./scripts/run_desktop.sh [--no-sidecar] [--software-encode]

  --no-sidecar       Launch UE5 only; start the sidecar separately
  --software-encode  Force x264enc even if NVENC is available
```

---

## Headless Linux Server

### Prerequisites

```bash
sudo apt install xvfb vulkan-tools nvidia-driver-535
# GStreamer
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav \
    python3-gst-1.0 gir1.2-gstreamer-1.0
```

Verify Vulkan is available:
```bash
vulkaninfo --summary
```

### Start Headless

```bash
export CESIUM_ION_TOKEN="your_token_here"
./scripts/run_headless.sh
```

The script:
1. Starts `Xvfb :99` at 1920×1080×24.
2. Starts the Python sidecar in the background.
3. Launches Unreal Engine with `-RHI=Vulkan -unattended`.

### Tuning for Headless

```bash
# Reduce virtual display resolution to save GPU VRAM (render target is unaffected)
XVFB_RES=1280x720x24 ./scripts/run_headless.sh

# Change display number if :99 is already in use
DISPLAY_NUM=100 ./scripts/run_headless.sh
```

---

## macOS (Docker, No Unreal Engine)

A self-contained Docker Compose stack that runs the full pipeline on macOS
without Unreal Engine or a GPU. A synthetic frame generator (`frame-gen`)
writes colour-bar BGRA frames and fake telemetry to shared memory; the
sidecar reads them, encodes with `x264enc`, and sends MPEG-TS + KLV over UDP
to the host.

### Prerequisites

- [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/) (4.x+)

That's it — no Unreal Engine, GStreamer, Python, or posix-ipc needed on the host.

### Quick Start

```bash
docker compose -f docker/docker-compose.mac.yml up --build
```

This builds two images and starts two containers:

| Service | Image | Role |
|---------|-------|------|
| `frame-gen` | `camsim-framegen` | Writes synthetic BGRA frames + telemetry to `/dev/shm` |
| `sidecar` | `camsim-sidecar` | Reads from `/dev/shm`, encodes H.264 + KLV, sends MPEG-TS/UDP |

Both containers share a tmpfs volume mounted at `/dev/shm` (stands in for
`--ipc=host`, which is not supported on macOS Docker Desktop).

### Receive the Stream

```bash
# Video player
vlc --demux=ts "udp://@:5004"

# Inspect TS packets + KLV tags
python tools/recv_and_inspect.py --host 0.0.0.0 --port 5004
```

The sidecar sends unicast UDP to `host.docker.internal` (the macOS host IP
inside Docker Desktop). Multicast is not available in this mode.

### Override Resolution / FPS

```bash
FRAME_WIDTH=1920 FRAME_HEIGHT=1080 FRAME_FPS=24 \
  docker compose -f docker/docker-compose.mac.yml up --build
```

### Override Destination / Bitrate

```bash
CAMSIM_PORT=6000 CAMSIM_BITRATE=8000 \
  docker compose -f docker/docker-compose.mac.yml up
```

### All Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRAME_WIDTH` | `1280` | Frame width in pixels |
| `FRAME_HEIGHT` | `720` | Frame height in pixels |
| `FRAME_FPS` | `30` | Target frame rate |
| `CAMSIM_HOST` | `host.docker.internal` | UDP destination host |
| `CAMSIM_PORT` | `5004` | UDP destination port |
| `CAMSIM_BITRATE` | `4000` | H.264 bitrate in kbps |
| `CAMSIM_LOG_LEVEL` | `INFO` | Sidecar log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `GST_DEBUG` | `2` | GStreamer log level (0–9) |

### Stopping

```bash
docker compose -f docker/docker-compose.mac.yml down
```

Add `-v` to also remove the shared tmpfs volume:

```bash
docker compose -f docker/docker-compose.mac.yml down -v
```

### Troubleshooting

**"Telemetry SHM magic mismatch"** — The sidecar mapped a stale or wrong
shared memory region. Run `docker compose down -v` to clear the tmpfs volume,
then `up` again.

**"Shared memory /camsim_frames did not appear within 30 s"** — The
`frame-gen` container failed to start. Check its logs:
```bash
docker compose -f docker/docker-compose.mac.yml logs frame-gen
```

**VLC shows nothing** — Ensure VLC is listening on the correct port
(`udp://@localhost:5004`). The `@` is required for VLC to bind as a listener.
Also verify Docker Desktop is running and the containers are healthy:
```bash
docker compose -f docker/docker-compose.mac.yml ps
```

**Low FPS / stuttering** — Docker Desktop runs inside a Linux VM with limited
CPU. Try reducing resolution (`FRAME_WIDTH=640 FRAME_HEIGHT=480`) or increasing
Docker Desktop's CPU/memory allocation in Settings → Resources.

---

## Docker (Sidecar Only — Linux with UE5)

The Dockerfile packages only the Python sidecar. The Unreal Engine process runs
on the host (or in a GPU-capable VM) and communicates via POSIX shared memory.

### Build the Image

```bash
docker build -f docker/Dockerfile.sidecar -t camsim-sidecar:latest .
```

### Run with Docker Compose

```bash
# Default: multicast 239.1.1.1:5004, NVENC if available
docker compose -f docker/docker-compose.yml up

# Override via environment
CAMSIM_HOST=192.168.1.100 CAMSIM_PORT=5004 \
docker compose -f docker/docker-compose.yml up
```

### Run with Docker Directly

```bash
# GPU + shared IPC (required for POSIX shm access)
docker run --rm \
    --gpus all \
    --ipc=host \
    --network=host \
    -e CAMSIM_HOST=239.1.1.1 \
    camsim-sidecar:latest

# Software encode (no GPU required)
docker run --rm \
    --ipc=host \
    --network=host \
    camsim-sidecar:latest --software
```

**`--ipc=host` is required** — it shares the host's `/dev/shm` namespace,
which is where POSIX shared memory lives.

**`--network=host` is required for multicast** — Docker bridge networking does
not forward multicast UDP. For unicast to a specific IP you can use the default
bridge network and map the port.

### Sidecar Container on a Different Host from UE5

POSIX shared memory is kernel-local — it cannot cross machine boundaries.
If the sidecar must run on a different machine, replace the shm transport
with a TCP/UDP raw frame stream, then rebuild `shm_reader.py` as a network
receiver. The KLV encoder and GStreamer pipeline are unaffected.

---

## Network Configuration

### Multicast (default)

Default multicast group: **239.1.1.1:5004** (administratively scoped,
RFC 2365 §2.5 — link-local multicast, routable within an AS).

The udpsink GStreamer element sends multicast with a TTL of 1 (LAN-local).
To cross router hops, add `ttl=4` to the udpsink properties in `pipeline.py`.

Multicast reception:
```bash
# VLC
vlc --demux=ts "udp://@239.1.1.1:5004"

# mpv
mpv udp://239.1.1.1:5004

# Python inspector
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004
```

The receiver must join the multicast group. VLC and mpv do this automatically.
The `recv_and_inspect.py` tool uses `IP_ADD_MEMBERSHIP` on `0.0.0.0` (any
interface).

### Unicast

```bash
# Sidecar sends to a single receiver
python -m camsim_sidecar --host 192.168.1.100 --port 5004 --no-multicast

# Receive
python tools/recv_and_inspect.py --host 0.0.0.0 --port 5004
```

---

## Verifying the Stream

### Step 1 — MPEG-TS packets arriving

```bash
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004 --quiet
```

Expected output after 5 seconds:
```
[  5.0s] TS pkts:   4500  Video PID 0x0100: 29.8 fps  KLV pkts:   147  Errors: 0
  PIDs seen:
    PID 0x0000     1 pkts  PAT
    PID 0x0100  4350 pkts  Video (H.264)
    PID 0x0201   149 pkts  KLV Metadata
```

### Step 2 — Video decodes in VLC

```bash
vlc --demux=ts "udp://@239.1.1.1:5004"
```

Expect live video of the terrain with the camera looking downward at a 45°
angle.

### Step 3 — KLV tags match the scene

```bash
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004
```

Verify:
- Tag 2 (timestamp) advances at wall-clock rate.
- Tags 13/14 (sensor lat/lon) match the initial `AircraftKinematicActor`
  position (default: 36.5°N, 117.5°W).
- Tag 15 (sensor altitude) ≈ 1500 m.
- Tag 18 (sensor rel. azimuth) ≈ 0° (gimbal at default center).
- Tag 21 (slant range) ≈ aircraft altitude / cos(tilt angle).

### Step 4 — Slew test

```bash
# Start a 10 deg/s pan slew for 5 seconds
python tools/inject_commands.py slew-pan --rate 10 --duration 5
```

During those 5 seconds, Tag 18 in the stream should increase at ~10 deg/s,
then stop. Verify with `recv_and_inspect.py`.

### Step 5 — Gimbal limits

```bash
# Slew past the +170° limit
python tools/inject_commands.py slew-pan --rate 60 --duration 10
```

Tag 18 should stop increasing at 170° despite the command still being active.

---

## Production Checklist

- [ ] `CESIUM_ION_TOKEN` set via environment (not in code or config files)
- [ ] `recv_and_inspect.py` shows TS packets at ~30 fps with 0 errors
- [ ] KLV inspector confirms PID 0x0201 with correct Universal Key bytes
- [ ] `test_klv_encoder.py` all tests pass
- [ ] Tag 2 (timestamp) advances at wall-clock rate
- [ ] Tags 13/14 (sensor lat/lon) match aircraft track
- [ ] Slew at 10 deg/s → Tag 18 increases at ~10 deg/s
- [ ] Gimbal limits enforced (Tag 18 stops at ±170°)
- [ ] Headless run with Xvfb produces the same stream as desktop
- [ ] Docker sidecar (`--ipc=host`) receives frames from host UE5 process
