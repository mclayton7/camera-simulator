# CamSim — Flying Camera / Gimbal Simulator

Synthetic EO/IR camera simulator for a gimbal-mounted payload on a fixed-wing
aircraft. Renders real WGS-84 terrain via Cesium, outputs standards-compliant
H.264 video over MPEG-TS/UDP, and embeds STANAG 4609 / MISB ST 0601 KLV
telemetry at 30 Hz.

| Feature | Technology |
|---------|-----------|
| 3D terrain | [Cesium World Terrain](https://cesium.com/platform/cesium-ion/content/cesium-world-terrain/) via Cesium for Unreal |
| Rendering | Unreal Engine 5 (`SceneCaptureComponent2D`) |
| Video encoding | H.264 via GStreamer `nvh264enc` (NVENC) / `x264enc` fallback |
| Transport | MPEG-TS over UDP (unicast or multicast) |
| Telemetry | MISB ST 0601.19 KLV — 17 tags, 30 Hz, timestamp-aligned to video |
| Control | UDP command protocol — slew rates, absolute gimbal, position, speed |
| IPC | POSIX shared memory (zero-copy BGRA ring buffer + seqlock telemetry) |

---

## Documentation

| Document | Contents |
|----------|---------|
| [docs/architecture.md](docs/architecture.md) | System design, data-flow diagram, component responsibilities, design decisions, threading model, coordinate conventions |
| [docs/ipc-protocol.md](docs/ipc-protocol.md) | Shared memory wire format (frame ring buffer + telemetry double buffer), UDP command protocol |
| [docs/klv-reference.md](docs/klv-reference.md) | MISB ST 0601.19 tag table, quantisation ranges and resolution, BER encoding, checksum algorithm |
| [docs/development.md](docs/development.md) | Building the UE5 plugin, GStreamer install, running tests, common issues |
| [docs/deployment.md](docs/deployment.md) | Desktop, headless Linux, Docker, network config, verification checklist |

---

## Repository Layout

```
camera-simulator/
├── UnrealProject/                    UE5 project
│   ├── CameraSimulator.uproject
│   ├── Config/DefaultEngine.ini      Resolution, port, shm names
│   └── Plugins/CamSimPlugin/
│       └── Source/CamSimPlugin/
│           ├── Public/
│           │   ├── SharedMemoryTypes.h    IPC wire contract (define first)
│           │   ├── AircraftKinematicActor.h
│           │   ├── GimbalComponent.h
│           │   ├── SimCameraComponent.h
│           │   ├── CommandReceiver.h
│           │   ├── FrameExporter.h
│           │   └── TelemetryExporter.h
│           └── Private/               Corresponding .cpp files
│
├── sidecar/
│   ├── pyproject.toml
│   ├── test_klv_encoder.py            KLV round-trip tests (pytest)
│   └── camsim_sidecar/
│       ├── main.py                    Entry point + main loop
│       ├── pipeline.py                GStreamer pipeline builder
│       ├── shm_reader.py              POSIX shm frame + telemetry readers
│       ├── klv_encoder.py             MISB ST 0601.19 KLV packet builder
│       └── crc.py                     CRC-16/CCITT-FALSE
│
├── tools/
│   ├── recv_and_inspect.py            UDP MPEG-TS inspector + KLV decoder
│   ├── inject_commands.py             Manual slew/position command sender
│   └── klv_decoder.py                 Standalone KLV parser (lib + CLI)
│
├── docker/
│   ├── Dockerfile.sidecar
│   └── docker-compose.yml
│
├── scripts/
│   ├── run_desktop.sh                 Desktop launch (GPU)
│   └── run_headless.sh               Headless launch (Xvfb + Vulkan)
│
└── docs/
    ├── architecture.md
    ├── ipc-protocol.md
    ├── klv-reference.md
    ├── development.md
    └── deployment.md
```

---

## Quick Start

### 1. Prerequisites

- Unreal Engine 5.3+ with Cesium for Unreal plugin
- GStreamer 1.22+ (`gst-plugins-good`, `gst-plugins-bad`, `gst-plugins-ugly`,
  `gst-libav`, `python3-gst-1.0`)
- Python 3.11+
- A free [Cesium ion token](https://ion.cesium.com)

See [docs/development.md](docs/development.md) for detailed install instructions.

### 2. Install the sidecar

```bash
pip install -e sidecar/
```

### 3. Run

```bash
export CESIUM_ION_TOKEN="your_token_here"   # never commit this
./scripts/run_desktop.sh
```

### 4. Receive the stream

```bash
# Video player
vlc udp://@239.1.1.1:5004

# Inspect TS packets + KLV tags
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004
```

### 5. Send commands

```bash
# Slew gimbal pan right at 10 deg/s for 5 seconds
python tools/inject_commands.py slew-pan --rate 10 --duration 5

# Move aircraft to a new position
python tools/inject_commands.py set-position --lat 36.5 --lon -117.5 --alt 2000

# Set absolute gimbal angle
python tools/inject_commands.py gimbal-abs --pan 0 --tilt -45
```

### 6. Run tests

```bash
cd sidecar && pytest test_klv_encoder.py -v
```

---

## System Architecture (Summary)

```
 inject_commands.py
       │ UDP :5005
       ▼
┌─────────────────────────────────────────────────────────┐
│                  Unreal Engine 5 Process                │
│                                                         │
│  CommandReceiver ──► AircraftKinematicActor             │
│                           │                             │
│                     GimbalComponent                     │
│                     SimCameraComponent (SceneCapture2D) │
│                           │                             │
│  FrameExporter ───────────┘──► /camsim_frames  (shm)   │
│  TelemetryExporter ───────────► /camsim_telemetry (shm) │
└────────────────────────────┬────────────────────────────┘
                             │ POSIX shared memory (zero-copy)
                             ▼
┌─────────────────────────────────────────────────────────┐
│              Python sidecar (GStreamer)                  │
│                                                         │
│  FrameShmReader → appsrc → nvh264enc/x264enc ─┐         │
│  TelemetryShmReader → klv_encoder → appsrc   ─┤         │
│                                               ▼         │
│          mpegtsmux (video 0x0100, KLV 0x0201)           │
│          → udpsink → UDP MPEG-TS                        │
└────────────────────────────┬────────────────────────────┘
                             │ UDP MPEG-TS :5004
                             ▼
              VLC / mpv / ATAK / recv_and_inspect.py
```

See [docs/architecture.md](docs/architecture.md) for the full diagram, design
decisions, and threading model.

---

## UDP Command Reference

Header: `[magic=0x43534D53 u32LE] [type u8] [reserved u8] [payload_len u16LE]`

| Type | Name | Payload | Description |
|------|------|---------|-------------|
| 0x01 | SlewPan | `f32 rate_deg_s` | Pan slew rate (+ = right). Zero to stop. |
| 0x02 | SlewTilt | `f32 rate_deg_s` | Tilt slew rate (+ = up). Zero to stop. |
| 0x03 | SlewBoth | `f32 pan, f32 tilt` | Both axes simultaneously. |
| 0x04 | SetPosition | `f64 lat, f64 lon, f32 alt_hae` | Teleport aircraft. |
| 0x05 | SetHeading | `f32 heading_deg` | True heading (0 = north, CW). |
| 0x06 | SetSpeed | `f32 speed_kts` | Airspeed in knots. |
| 0x07 | SetGimbalAbs | `f32 pan_deg, f32 tilt_deg` | Absolute gimbal position. |
| 0xFF | Ping | (none) | No-op connectivity check. |

See [docs/ipc-protocol.md](docs/ipc-protocol.md) for full wire-format details.

---

## KLV Tags Implemented

MISB ST 0601.19, LS version 19. Tags: **1, 2, 5–7, 13–21, 23–25, 65**.

See [docs/klv-reference.md](docs/klv-reference.md) for quantisation ranges,
resolution per tag, checksum algorithm, and verification instructions.

---

## Security

- **Never commit `CESIUM_ION_TOKEN`** to git. The `.gitignore` blocks `.env`
  files. Set it as a shell environment variable or use a secrets manager.
- The UDP command socket (port 5005) accepts commands from any host. In
  production, bind it to a specific interface or add a firewall rule.
- MPEG-TS output contains real geodetic coordinates in the KLV stream.
  Apply network-level access control if the stream must be restricted.
