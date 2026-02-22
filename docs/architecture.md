# Architecture

## System Overview

CamSim synthesises an electro-optical / infrared (EO/IR) camera payload mounted
on a gimbal carried by a fixed-wing aircraft. It renders real WGS-84 terrain,
produces standards-compliant H.264 video over MPEG-TS/UDP, and embeds STANAG 4609
MISB ST 0601 KLV telemetry that mirrors the simulated flight and gimbal state.

The system is split into two processes that communicate through POSIX shared
memory:

1. **Unreal Engine 5 process** — renders the scene, advances the kinematic
   model, and exposes UDP control input.
2. **Python sidecar** — reads frames and telemetry from shared memory, encodes
   H.264, builds KLV packets, and muxes everything into MPEG-TS/UDP.

---

## Data-Flow Diagram

```
 External controller (e.g. inject_commands.py)
        │ UDP :5005
        ▼
┌───────────────────────────────────────────────────────────────────┐
│                     Unreal Engine 5 Process                       │
│                                                                   │
│  CommandReceiver (UActorComponent)                                │
│    Non-blocking UDP socket, polled every game tick               │
│    Dispatches to game thread immediately                          │
│         │                                                         │
│         ▼                                                         │
│  AircraftKinematicActor (AActor, ticked at game rate)            │
│    Haversine dead-reckoning → lat/lon advances each tick         │
│    ACesiumGeoreference::TransformLLH → Unreal world coords       │
│         │                                                         │
│         ├──► GimbalComponent (UActorComponent)                   │
│         │     Integrates pan/tilt rates, enforces hard limits     │
│         │     GetCameraWorldRotation() for SceneCapture           │
│         │                                                         │
│         └──► SimCameraComponent (USceneCaptureComponent2D)       │
│               FOVAngle = HFovDeg                                  │
│               LineTrace → slant range + frame-centre geodetics   │
│               TextureRenderTarget2D (BGRA, 1920×1080)            │
│                                                                   │
│  FrameExporter (UActorComponent)                                  │
│    ReadPixels() → BGRA bytes                                      │
│    Writes into SHM frame ring buffer                              │
│         │                                                         │
│  TelemetryExporter (UActorComponent)                              │
│    Snapshot of all flight + gimbal + camera state                │
│    Seqlock write into SHM telemetry double-buffer                │
└────────────────────────────┬──────────────────────────────────────┘
                             │ POSIX shared memory
                             │   /camsim_frames      (BGRA ring buffer)
                             │   /camsim_telemetry   (TelemetryFrame)
                             ▼
┌───────────────────────────────────────────────────────────────────┐
│                  Python Sidecar (camsim_sidecar)                  │
│                                                                   │
│  shm_reader.FrameShmReader                                        │
│    Polls write_index; copies frame bytes + timestamp              │
│         │                                                         │
│         ▼                                                         │
│  GStreamer appsrc  "framesrc"                                     │
│    caps: video/x-raw,format=BGRA,width=1920,height=1080,fps=30   │
│    push_buffer(bgra_bytes, pts=timestamp_us * 1000)              │
│         │                                                         │
│         ├── videoconvert → I420                                   │
│         ├── nvh264enc  (NVENC, CBR 4 Mbps, GOP=30)               │
│         │   or x264enc (software fallback)                        │
│         └── h264parse → mux.                                      │
│                                                                   │
│  shm_reader.TelemetryShmReader                                    │
│    Seqlock read from double-buffer                                │
│         │                                                         │
│         ▼                                                         │
│  klv_encoder.encode_klv_packet(TelemetryData)                    │
│    Builds MISB ST 0601.19 UAS Local Set KLV packet               │
│         │                                                         │
│         ▼                                                         │
│  GStreamer appsrc  "klvsrc"                                       │
│    caps: meta/x-klv,parsed=true                                   │
│    push_buffer(klv_bytes, pts=timestamp_us * 1000)               │
│         └── mux.                                                  │
│                                                                   │
│  mpegtsmux  "mux"                                                 │
│    video PID 0x0100  (stream_type 0x1B, H.264)                   │
│    KLV   PID 0x0201  (stream_type 0x15, data stream)             │
│         │                                                         │
│         ▼                                                         │
│  udpsink → UDP MPEG-TS (default: multicast 239.1.1.1:5004)       │
└───────────────────────────────────────────────────────────────────┘
                             │ UDP MPEG-TS
                             ▼
              VLC / mpv / ATAK / recv_and_inspect.py
```

---

## Component Responsibilities

### `AircraftKinematicActor`

The root actor in the simulation. On `BeginPlay` it:
- Caches the `ACesiumGeoreference` actor present in the level.
- Wires all sub-components together (sets target pointers).

On each `Tick`:
1. Calls `AdvancePosition(DeltaTime)` — haversine formula on a spherical Earth
   (radius 6,378,137 m) advancing lat/lon from heading + speed.
2. Calls `SyncWorldTransform()` — converts geodetic (lon/lat/alt) to Unreal
   world coordinates via `ACesiumGeoreference::TransformLLH`.
3. Sub-components tick independently (via `UActorComponent::TickComponent`).

UDP command handlers (`HandleSetPosition`, `HandleSetHeading`, `HandleSetSpeed`)
are called from `CommandReceiver` on the game thread — no locking required.

### `GimbalComponent`

Maintains the two-axis gimbal state (`PanDeg`, `TiltDeg`). Commands arrive as:
- **Rate commands** (`SetSlewRates`) — stored as `PendingPanRate` /
  `PendingTiltRate` and integrated each tick: `PanDeg += rate * DeltaTime`.
- **Absolute commands** (`SetAbsolutePosition`) — applied immediately, rates cleared.

Hard limits are enforced by `ClampPan` / `ClampTilt` (±170°, −90° to +30°).
`GetCameraWorldRotation()` combines the aircraft world rotation with the gimbal
local pan/tilt via quaternion multiplication, giving the correct world-space
look direction for `SimCameraComponent`.

### `SimCameraComponent`

Subclasses `USceneCaptureComponent2D`. Each tick it:
1. Queries `GimbalComponent::GetCameraWorldRotation()` and calls
   `SetWorldRotation` to orient the virtual camera.
2. Calls `UpdateGroundPoint()` — fires a `LineTraceSingleByChannel` from the
   camera aperture along its forward vector. The hit distance gives **slant
   range**; the hit location is converted back to geodetic via
   `ACesiumGeoreference::TransformUnrealPositionToLLH` to get **frame-centre
   coordinates** (lat/lon/elev).
3. Sets `FOVAngle = HFovDeg` on the `SceneCaptureComponent2D`.

VFoV is derived from HFoV and the 16:9 aspect ratio:
`VFoV = 2 * atan(tan(HFoV/2) / AspectRatio)`.

### `CommandReceiver`

Binds a non-blocking UDP socket on port 5005 at `BeginPlay`. `TickComponent`
calls `DrainSocket()` which loops `Socket->HasPendingData` / `RecvFrom` until
the queue is empty, then calls `DispatchCommand` for each valid datagram.
All processing happens on the game thread — no separate receive thread or queue
is needed at 30 Hz command rates.

### `FrameExporter`

On the first tick after `BeginPlay`, opens (or creates) the `camsim_frames`
POSIX shared memory region sized for 3 × (header + BGRA pixels). Each tick:
1. Gets `TextureRenderTarget2D` from `SimCameraComponent`.
2. Calls `FRenderTarget::ReadPixels` (blocks until GPU flush — ~1 frame late).
3. Writes pixels into the next slot using `InterlockedIncrement` on
   `write_index`.

### `TelemetryExporter`

Opens `camsim_telemetry` shared memory at `BeginPlay`. Each tick it:
1. Toggles `write_slot` (0 ↔ 1).
2. Writes all telemetry fields into `Slots[write_slot]`.
3. Writes `sequence` **last** — this is the seqlock "unlock".

The sidecar reads from the slot that is NOT `write_slot`, checking `sequence`
before and after to detect torn reads (should not occur at 30 Hz).

---

## Design Decisions

### Why Unreal Engine 5?

| Requirement | UE5 | Unity | Godot |
|-------------|-----|-------|-------|
| Cesium plugin maturity | Excellent (CesiumGS official) | Good | Beta |
| Native H.264 GPU path | AVEncoder (NVENC) | Custom plugin needed | No |
| C++ render access | Full | Limited | Limited |
| AirSim / CARLA precedent | Yes | No | No |
| Nanite / Lumen | Yes | No | No |

### Why a Sidecar Process Instead of All-in-Engine?

Unreal's `AVEncoder` pipeline is coupled to **Pixel Streaming / WebRTC** — it
has no built-in MPEG-TS muxer and no KLV interleaving. Achieving STANAG 4609
compliance would require writing a custom muxer in C++ against the `AVEncoder`
or `IMediaEncoder` abstractions, which are internal and unstable across UE
releases.

GStreamer's `mpegtsmux` has native KLV data stream support (stream_type 0x15,
second `appsrc` with `meta/x-klv` caps). The sidecar approach also makes the
encoder independently testable, replaceable (swap GStreamer for FFmpeg), and
deployable in Docker with `--runtime=nvidia` without modifying the UE project.

### Why POSIX Shared Memory?

At 1920 × 1080 × 4 bytes × 30 fps = **247 MB/s** raw throughput, any
inter-process transport that serialises or copies data twice would saturate a
typical PCIe bus or consume significant CPU. POSIX `shm_open` + `mmap` gives
**zero-copy** transfer: the Unreal render thread writes directly into a kernel
page that the sidecar reads with `mmap`. No serialisation, no sockets, no
pipe overhead.

Named shared memory is also trivially reachable from Docker containers with
`--ipc=host`, and from headless Linux sessions without any additional
infrastructure.

### Why Triple-Buffering for Frames?

With 3 slots (slot N being written, N-1 the latest complete, N-2 a fallback),
the sidecar can read the most-recently-completed frame without stalling the Unreal
render thread. A 2-slot buffer risks the producer overwriting the slot the
consumer is actively reading. A triple buffer trades 2× the extra memory
(~8 MB at 1080p) for race-free access without any mutex.

### Why a Seqlock for Telemetry?

Telemetry is 128 bytes — small enough that a mutex round-trip would be
disproportionate overhead. The seqlock pattern (write sequence last, consumer
checks before + after) is lock-free for the reader, requires no kernel call,
and handles the case where the writer preempts the reader mid-copy. At 30 Hz
the probability of a torn read is negligible; the retry loop exits after ≤ 5
attempts in all realistic scenarios.

### Why GStreamer Over FFmpeg Directly?

GStreamer's `mpegtsmux` has a documented, stable interface for pushing KLV
metadata via a second `appsrc` with `meta/x-klv` caps, and it correctly sets
PID 0x0201 with stream_type 0x15 as required by STANAG 4609. FFmpeg's
`mpegts` muxer can embed metadata but the API for feeding it from Python
is less ergonomic and the KLV stream_type handling is less well-documented.
GStreamer also gives automatic NVENC detection and fallback in a single
pipeline description string.

### Why Haversine Dead-Reckoning?

The kinematic model integrates lat/lon from heading + speed using the haversine
formula on a spherical-Earth approximation (radius 6,378,137 m, the WGS-84
semi-major axis). The WGS-84 ellipsoid causes less than 0.3% position error
over the time scales of a simulator session. A full geodetic integrator would
add complexity with no perceptible benefit for rendering purposes.

### MPEG-TS PID Assignments

| PID | Content | Stream Type |
|-----|---------|-------------|
| 0x0100 (256) | H.264 video | 0x1B |
| 0x0201 (513) | MISB ST 0601 KLV | 0x15 (synchronous data stream) |

PIDs are chosen in the private range (0x0010–0x1FFE) and away from commonly
used values (0x0100 for video is conventional for single-program streams).

---

## Threading Model

```
Game thread                 Render thread               Sidecar (main thread)
─────────────               ─────────────               ─────────────────────
CommandReceiver::Tick()
  DrainSocket()
  DispatchCommand()
    GimbalComponent::SetSlewRates()
    AircraftKinematicActor::HandleSetPosition()

AircraftKinematicActor::Tick()
  AdvancePosition()
  SyncWorldTransform()         → UE internal render submit

GimbalComponent::TickComponent()
  Integrate rates → PanDeg, TiltDeg

SimCameraComponent::TickComponent()
  SetWorldRotation()
  UpdateGroundPoint()          → SceneCapture captured by render thread

FrameExporter::TickComponent()
  ReadPixels()               ← waits for render thread flush
  WriteFrame() → shm                                     ← poll write_index
                                                           push_frame() → GStreamer
                                                           read TelemetryFrame
                                                           encode_klv_packet()
                                                           push_klv() → GStreamer

TelemetryExporter::TickComponent()
  BuildAndWrite() → shm
```

All Unreal work happens on the game thread or render thread — no custom threads
are introduced. The sidecar is single-threaded; GStreamer manages its own
internal threads for encoding and network output.

---

## Coordinate System Conventions

| System | Convention |
|--------|-----------|
| Geodetic | WGS-84, degrees (lat N positive, lon E positive), HAE metres |
| Unreal world | cm, Cesium-managed origin near the current viewpoint |
| Gimbal pan | Degrees relative to aircraft nose; positive = right (clockwise viewed from above) |
| Gimbal tilt | Degrees elevation; positive = up, negative = down toward ground |
| KLV sensor rel. azimuth | 0–360°, positive clockwise from nose |
| KLV sensor rel. elevation | ±180°, negative = looking down |

The `ACesiumGeoreference` actor handles all conversions between WGS-84 and
Unreal world space. CamSim never manually applies an ENU or ECEF transform —
it always calls through the Cesium API.

---

## Latency Budget

| Stage | Typical latency |
|-------|----------------|
| UE SceneCapture → GPU | ~1 frame (33 ms at 30 fps) |
| GPU → CPU ReadPixels | ~1 frame (pipeline bubble) |
| Shm ring write | < 1 ms |
| Sidecar shm poll + push | < 1 ms |
| GStreamer encode (NVENC) | ~8–15 ms |
| GStreamer encode (x264) | ~20–40 ms |
| UDP send | < 1 ms (LAN) |
| **End-to-end (NVENC)** | **~75–100 ms** |
| **End-to-end (x264)** | **~90–120 ms** |

These are glass-to-glass latencies. For a simulator the primary concern is
timestamp accuracy, not display latency. The KLV timestamp (Tag 2) is set to
`FDateTime::UtcNow()` at the moment `ReadPixels` completes, ensuring the
telemetry matches the frame content rather than the game-thread time.

---

## Extending the System

### Adding a New KLV Tag

1. Add the encoding function in `sidecar/camsim_sidecar/klv_encoder.py`
   following the pattern of existing `_tagN()` functions.
2. Call it inside `encode_klv_packet()` before the checksum tag.
3. Add the decoder entry to `tools/klv_decoder.py:TAG_DESCRIPTIONS`.
4. Add a round-trip test in `sidecar/test_klv_encoder.py`.
5. If the new tag requires data not in `TelemetryFrame`, add the field to
   `SharedMemoryTypes.h:TelemetryFrame` **and** `shm_reader.py:_TelemetryFrame`
   (must stay in sync).

### Adding a New Command Type

1. Add the enum value to `ECamSimCmd` in `CommandReceiver.h`.
2. Add the decode + dispatch case in `CommandReceiver.cpp::DispatchCommand`.
3. Add the sender subcommand in `tools/inject_commands.py`.

### Changing Resolution

Set `CaptureWidth` / `CaptureHeight` on `USimCameraComponent` (editor property
or `DefaultEngine.ini`). The shared memory region is sized dynamically in
`FrameExporter::OpenSharedMemory`. Pass matching `--width` / `--height` args to
the sidecar. The GStreamer pipeline caps string is built from `self.width` /
`self.height` in `pipeline.py::_build_pipeline_string`.

### Replacing GStreamer with FFmpeg

Replace `pipeline.py` with an FFmpeg `subprocess` call or `av` library wrapper.
The `FrameShmReader` and `TelemetryShmReader` interfaces remain unchanged — only
`pipeline.py` and `main.py` need to change.
