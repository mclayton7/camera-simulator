# CamSim — Copilot Instructions

## Architecture

CamSim is a two-process system that communicates exclusively via **POSIX shared memory**:

1. **UE5 plugin** (`UnrealProject/Plugins/CamSimPlugin/`) — renders terrain with Cesium, advances the kinematic model, accepts UDP commands on port 5005, and writes raw BGRA frames + telemetry to shared memory.
2. **Python sidecar** (`sidecar/camsim_sidecar/`) — reads from shared memory, encodes H.264 via GStreamer (NVENC or x264 fallback), packs MISB ST 0601.19 KLV telemetry, and muxes everything into MPEG-TS/UDP on port 5004.

### UE5 Component Hierarchy (all owned by `AircraftKinematicActor`)
- `CommandReceiver` → polls UDP socket each game tick, dispatches to game thread
- `GimbalComponent` → integrates slew rates into pan/tilt angles, enforces limits
- `SimCameraComponent` → `USceneCaptureComponent2D` subclass; fires line-trace to compute frame-centre geodetics and slant range
- `FrameExporter` → `ReadPixels()` → SHM ring buffer (`/camsim_frames`, triple-buffer)
- `TelemetryExporter` → seqlock double-buffer write → `/camsim_telemetry`

### Sidecar Module Responsibilities
- `shm_reader.py` — `FrameShmReader` + `TelemetryShmReader`; all SHM access is here
- `klv_encoder.py` — builds MISB ST 0601.19 KLV packets from `TelemetryData`
- `pipeline.py` — constructs the GStreamer pipeline string; change encoder/mux settings here
- `crc.py` — CRC-16/CCITT-FALSE used for KLV checksum (Tag 1)
- `main.py` — main loop: polls SHM, pushes buffers to GStreamer `appsrc`s

### External Tools (`tools/`)
- `inject_commands.py` — CLI for sending UDP commands to UE5
- `flight_director.py` — JSBSim 6-DOF flight model; sends `SetFlightState` (0x08) at 30 Hz, permanently disabling UE5's dead-reckoning for the session
- `recv_and_inspect.py` — MPEG-TS/KLV inspector for the output stream
- `klv_decoder.py` — standalone KLV parser (usable as a library)

---

## Build & Run

### Python sidecar
```bash
pip install -e sidecar/
python -m camsim_sidecar --help
```

### UE5 plugin (Linux/macOS CLI)
```bash
/path/to/UnrealEngine/Engine/Build/BatchFiles/Linux/Build.sh \
    CameraSimulatorEditor Linux Development \
    -Project="$(pwd)/UnrealProject/CameraSimulator.uproject" -WaitMutex
```

### Launch (full stack)
```bash
export CESIUM_ION_TOKEN="your_token"
./scripts/run_desktop.sh          # GPU desktop
./scripts/run_headless.sh         # Xvfb + Vulkan, no display
```

### macOS Docker stack (no UE5 needed)
```bash
docker compose -f docker/docker-compose.mac.yml up --build
```

---

## Tests

```bash
# Full suite
cd sidecar && pytest test_klv_encoder.py -v

# Single test
cd sidecar && pytest test_klv_encoder.py -v -k "test_name"

# With KLV reference cross-check
pip install klvdata
cd sidecar && pytest test_klv_encoder.py -v -k "round_trip"
```

All tests run without UE5 or GStreamer.

---

## Key Conventions

### SharedMemoryTypes.h ↔ shm_reader.py must stay in sync
Both files define the same binary layout. C++ uses `#pragma pack(1)`; Python uses `ctypes.Structure._pack_ = 1`. A layout mismatch produces **silently wrong telemetry values**, not a crash. After any struct change:
1. Update `SharedMemoryTypes.h`
2. Update the matching `ctypes.Structure` in `shm_reader.py`
3. Update the offset table in `docs/ipc-protocol.md`
4. Run the test suite to catch regressions

### Adding a new KLV tag
1. Add a `_tagN()` function in `klv_encoder.py` following existing patterns
2. Call it inside `encode_klv_packet()` before the checksum tag
3. Add the tag description to `tools/klv_decoder.py:TAG_DESCRIPTIONS`
4. Add a round-trip test in `sidecar/test_klv_encoder.py`
5. If the tag requires new data, add a field to `TelemetryFrame` in both `SharedMemoryTypes.h` **and** `shm_reader.py`

### Adding a new UDP command type
1. Add the enum value to `ECamSimCmd` in `CommandReceiver.h`
2. Add the decode + dispatch case in `CommandReceiver.cpp::DispatchCommand`
3. Add the sender subcommand in `tools/inject_commands.py`

### Adding a new UE5 component
1. `MyComponent.h` → `Source/CamSimPlugin/Public/`
2. `MyComponent.cpp` → `Source/CamSimPlugin/Private/`
3. Declare as `UPROPERTY()` in `AircraftKinematicActor.h`
4. Create with `CreateDefaultSubobject<UMyComponent>` in the constructor
5. Wire via a `SetXxx()` call from `AircraftKinematicActor::BeginPlay`

### Coordinate systems
- All geodetic values are WGS-84: degrees lat (N+), lon (E+), altitude in metres HAE
- **Never** apply manual ENU/ECEF transforms — always call through `ACesiumGeoreference` API
- Gimbal pan: degrees relative to aircraft nose, positive = right (CW from above); limits ±170°
- Gimbal tilt: degrees elevation, positive = up, negative = down; limits −90° to +30°
- KLV sensor relative azimuth: 0–360° CW from nose; KLV sensor relative elevation: ±180° (negative = looking down)

### UDP command protocol
- All values little-endian; header: `magic=0x43534D53 u32LE | type u8 | reserved=0 u8 | payload_len u16LE`
- Slew rates are **latched** — a non-zero slew continues until a zero-rate command or `SetGimbalAbs`
- `SetFlightState` (0x08) permanently disables UE5 dead-reckoning until restart; used by `flight_director.py` at 30 Hz

### MPEG-TS PID assignments
| PID | Content | stream_type |
|-----|---------|-------------|
| 0x0100 | H.264 video | 0x1B |
| 0x0201 | MISB ST 0601 KLV | 0x15 |

### Security
- **Never commit `CESIUM_ION_TOKEN`** — `.gitignore` blocks `.env` files; set as an environment variable
- UDP command port 5005 accepts from any host; apply firewall rules in production
- The KLV stream embeds real geodetic coordinates; restrict network access if needed
