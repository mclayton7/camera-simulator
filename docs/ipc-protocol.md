# IPC Protocol Reference

CamSim uses two POSIX shared memory regions for zero-copy inter-process
communication between the Unreal Engine plugin and the Python sidecar.

The canonical definitions are in
`UnrealProject/Plugins/CamSimPlugin/Source/CamSimPlugin/Public/SharedMemoryTypes.h`
(C++) and mirrored in `sidecar/camsim_sidecar/shm_reader.py` (Python).
**Both sides must agree on this layout exactly.** All structs use
`#pragma pack(1)` / `ctypes.Structure._pack_ = 1` to suppress padding.

---

## Region 1 — Video Frame Ring Buffer

**Name:** `/camsim_frames`
**Access:** Created by Unreal (producer); opened read-only by sidecar (consumer).

### Layout

```
Offset 0                           64
├─ ShmFrameHeader (64 bytes) ──────┤
│                                  │
│  magic        u32  0x43534D46    │  ('CSMF') sanity check
│  version      u32  1             │
│  frame_width  u32                │  pixels
│  frame_height u32                │  pixels
│  slot_count   u32  3             │  always 3 (triple buffer)
│  slot_stride  u32                │  bytes per slot = sizeof(ShmFrameSlot) + w*h*4
│  write_index  u32  volatile      │  producer increments before each write
│  read_index   u32  volatile      │  consumer increments after each read
│  _pad         [32 bytes]         │  to fill 64 bytes
│                                  │
└──────────────────────────────────┘

Offset 64
├─ ShmFrameSlot[0] ────────────────┤
│                                  │
│  sequence     u32                │  monotonic frame counter
│  width        u32                │
│  height       u32                │
│  timestamp_us u64                │  Unix epoch microseconds (UTC)
│  data_size    u32                │  width * height * 4 (BGRA bytes)
│  _pad         u32                │  struct alignment
│  pixels       [width*height*4 bytes]  BGRA row-major, top-left origin
│                                  │
├─ ShmFrameSlot[1] ────────────────┤
│  ...                             │
├─ ShmFrameSlot[2] ────────────────┤
│  ...                             │
└──────────────────────────────────┘

Total size = 64 + 3 × (32 + width × height × 4)
           = 64 + 3 × (32 + 1920 × 1080 × 4)   (at 1080p)
           ≈ 25 MB
```

### Pixel Format

Pixels are stored as **BGRA**, 8 bits per channel, matching Unreal's `FColor`
layout on little-endian platforms. The sidecar feeds these directly to
GStreamer `appsrc` with `caps="video/x-raw,format=BGRA,..."` which then
converts to I420 before encoding.

### Ring-Buffer Protocol

```
Producer (Unreal)                   Consumer (sidecar)
─────────────────                   ──────────────────
slot_idx = write_index % 3          if write_index == last_read:
write into ShmFrameSlot[slot_idx]       sleep 1 ms; retry
InterlockedIncrement(write_index)   slot_idx = last_read % 3
                                    read ShmFrameSlot[slot_idx]
                                    last_read++
```

The consumer never modifies `write_index`. `read_index` is stored in the header
for diagnostic purposes but is only written by the sidecar; Unreal never reads
it. The producer does **not** wait for the consumer — if the sidecar falls
behind by more than 3 frames, frames are silently overwritten (the latest frame
always wins). This is the correct behaviour for a real-time video simulator.

### Total Size Calculation

```python
slot_stride = 32 + width * height * 4          # ShmFrameSlot header + pixels
total_size  = 64 + slot_count * slot_stride    # ShmFrameHeader + slots
```

---

## Region 2 — Telemetry Double Buffer

**Name:** `/camsim_telemetry`
**Access:** Created by Unreal (producer); opened read-only by sidecar (consumer).

### Layout

```
Offset 0                           16
├─ ShmTelemetryHeader (16 bytes) ──┤
│                                  │
│  magic       u32  0x43534D54    │  ('CSMT') sanity check
│  version     u32  1             │
│  write_slot  u32  volatile      │  0 or 1 — slot currently being written
│  _pad        u32                │
│                                  │
└──────────────────────────────────┘

Offset 16
├─ TelemetryFrame[0] (128 bytes) ──┤
├─ TelemetryFrame[1] (128 bytes) ──┤
└──────────────────────────────────┘

Total size = 16 + 2 × 128 = 272 bytes
```

### TelemetryFrame Layout (128 bytes, `#pragma pack(1)`)

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| 0 | 8 | u64 | `timestamp_us` | Unix epoch µs (UTC) |
| 8 | 8 | f64 | `platform_lat_deg` | WGS-84 degrees |
| 16 | 8 | f64 | `platform_lon_deg` | WGS-84 degrees |
| 24 | 8 | f64 | `platform_alt_m_hae` | Height above WGS-84 ellipsoid, metres |
| 32 | 4 | f32 | `platform_heading_deg` | 0–360, true north |
| 36 | 4 | f32 | `platform_pitch_deg` | ±90 |
| 40 | 4 | f32 | `platform_roll_deg` | ±180 |
| 44 | 8 | f64 | `sensor_lat_deg` | Sensor aperture lat |
| 52 | 8 | f64 | `sensor_lon_deg` | Sensor aperture lon |
| 60 | 4 | f32 | `sensor_alt_m_hae` | Sensor altitude HAE |
| 64 | 4 | f32 | `sensor_rel_az_deg` | Gimbal pan 0–360 rel. nose |
| 68 | 4 | f32 | `sensor_rel_el_deg` | Gimbal tilt ±180 |
| 72 | 4 | f32 | `sensor_rel_roll_deg` | Sensor roll 0–360 |
| 76 | 4 | f32 | `hfov_deg` | Horizontal field-of-view |
| 80 | 4 | f32 | `vfov_deg` | Vertical field-of-view |
| 84 | 4 | f32 | `slant_range_m` | Line-of-sight range to ground |
| 88 | 8 | f64 | `frame_center_lat_deg` | Image centre ground lat |
| 96 | 8 | f64 | `frame_center_lon_deg` | Image centre ground lon |
| 104 | 4 | f32 | `frame_center_elev_m` | Image centre ground elevation HAE |
| 108 | 4 | u32 | `sequence` | Written **last**; seqlock unlock |
| 112 | 4 | — | `_pad` | Padding to 128 bytes |

### Seqlock Protocol

```
Producer (Unreal TelemetryExporter)     Consumer (sidecar TelemetryShmReader)
───────────────────────────────         ─────────────────────────────────────
write_slot ^= 1                         read_slot = 1 - header.write_slot
write all fields to Slots[write_slot]   seq_before = Slots[read_slot].sequence
Slots[write_slot].sequence = ++TelSeq   copy all fields
                                        seq_after  = Slots[read_slot].sequence
                                        if seq_before != seq_after: retry
```

The consumer reads from the slot that is **not** currently being written.
Because `sequence` is written last by the producer, the consumer can detect a
torn read by checking `sequence` before and after copying. In practice at 30 Hz
the retry path is never taken.

`sequence = 0` means the slot has never been written; the consumer returns
`None` until at least one valid frame has been produced.

---

## UDP Command Protocol

**Direction:** Controller → Unreal CommandReceiver
**Port:** 5005 (UDP)
**Byte order:** little-endian throughout

### Packet Header (8 bytes)

```
Offset  Size  Type    Field
──────  ────  ──────  ─────────────────────────────────────────────
0       4     u32 LE  magic = 0x43534D53  ('CSMS')
4       1     u8      msg_type
5       1     u8      reserved (must be 0)
6       2     u16 LE  payload_len (bytes that follow the header)
```

Packets with wrong magic or `payload_len` larger than the received datagram
are silently dropped.

### Message Types

| Type | Name | Payload | Description |
|------|------|---------|-------------|
| 0x01 | SlewPan | `f32 pan_rate_deg_s` | Set gimbal pan slew rate. Positive = right. Zero to stop. |
| 0x02 | SlewTilt | `f32 tilt_rate_deg_s` | Set gimbal tilt slew rate. Positive = up. Zero to stop. |
| 0x03 | SlewBoth | `f32 pan_rate_deg_s, f32 tilt_rate_deg_s` | Set both axes simultaneously. |
| 0x04 | SetPosition | `f64 lat_deg, f64 lon_deg, f32 alt_m_hae` | Teleport aircraft to geodetic position. |
| 0x05 | SetHeading | `f32 heading_deg` | Set aircraft true heading (0 = north, clockwise). |
| 0x06 | SetSpeed | `f32 speed_kts` | Set aircraft indicated airspeed in knots. |
| 0x07 | SetGimbalAbs | `f32 pan_deg, f32 tilt_deg` | Jump gimbal to absolute position; clears slew rates. |
| 0x08 | SetFlightState | `f64 lat_deg, f64 lon_deg, f32 alt_m_hae, f32 heading_deg, f32 pitch_deg, f32 roll_deg, f32 speed_kts` | Set full aircraft state; disables dead-reckoning (36 bytes). |
| 0xFF | Ping | (none) | No-op; useful for testing connectivity. |

### SetFlightState Semantics

`SetFlightState` (0x08) sets all aircraft kinematic state in a single packet:
position (lat/lon/alt), attitude (heading/pitch/roll), and speed. On first
receipt, `AircraftKinematicActor` sets `bExternallyDriven = true` and stops
running `AdvancePosition()` dead-reckoning. From that point the external
sender (typically `tools/flight_director.py`) is responsible for continuous
state updates.

The flag is not automatically cleared — if the external sender stops, the
aircraft freezes at its last reported state. Restart UE5 to re-enable
dead-reckoning.

Payload layout (36 bytes, little-endian):

```
Offset  Size  Type  Field
──────  ────  ────  ─────────────
0       8     f64   lat_deg        WGS-84 latitude
8       8     f64   lon_deg        WGS-84 longitude
16      4     f32   alt_m_hae      Height above ellipsoid (metres)
20      4     f32   heading_deg    True heading (0 = north, clockwise)
24      4     f32   pitch_deg      Nose pitch (positive = up)
28      4     f32   roll_deg       Bank angle (positive = right wing down)
32      4     f32   speed_kts      Indicated airspeed (knots)
```

### Slew Rate Semantics

Slew rates are **latched** — a `SlewPan` command with `rate = 10` will
continue panning at 10 deg/s on every subsequent game tick until another
`SlewPan` with `rate = 0` (or a `SetGimbalAbs`) is received. The
`inject_commands.py` tool's `--duration` flag handles the stop command
automatically.

### Example: Encode a SlewBoth Packet

```python
import struct

MAGIC = 0x43534D53
header = struct.pack("<IBBH", MAGIC, 0x03, 0, 8)   # 8 bytes payload
payload = struct.pack("<ff", 10.0, -5.0)            # pan +10, tilt -5 deg/s
packet = header + payload
sock.sendto(packet, ("127.0.0.1", 5005))
```

---

## Checking Shared Memory from the Shell

```bash
# List POSIX shm regions (Linux)
ls -la /dev/shm/

# Inspect frame header (first 64 bytes) with xxd
xxd /dev/shm/camsim_frames | head -4

# Python one-liner: print write_index and read_index
python3 -c "
import mmap, struct
with open('/dev/shm/camsim_frames', 'rb') as f:
    data = f.read(64)
magic, ver, fw, fh, slots, stride, widx, ridx = struct.unpack_from('<IIIIIIII', data)
print(f'magic=0x{magic:08X} {fw}x{fh} slots={slots} write={widx} read={ridx}')
"
```

---

## Platform Notes

| Platform | Frame shm | Telemetry shm |
|----------|-----------|---------------|
| Linux | `shm_open("/camsim_frames")` → `/dev/shm/camsim_frames` | same |
| macOS | `shm_open("/camsim_frames")` → kernel-managed | same |
| Windows | `CreateFileMapping(INVALID_HANDLE_VALUE, ..., "camsim_frames")` | same |

On Windows the mapping name is a kernel object name, not a path. The sidecar
currently uses `posix_ipc` which requires POSIX shm (Linux/macOS). A Windows
sidecar would need to use `mmap` with `CreateFileMapping` / `OpenFileMapping`
directly.
