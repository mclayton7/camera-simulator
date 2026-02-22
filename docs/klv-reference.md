# MISB ST 0601 KLV Reference

CamSim implements **MISB ST 0601.19** "UAS Datalink Local Set" (LS version 19).
One KLV packet is produced per video frame at the matching PTS, as required by
STANAG 4609 Edition 4.

---

## Universal Key

Every UAS Local Set packet starts with this 16-byte SMPTE Universal Label:

```
06 0E 2B 34  02 0B 01 01  0E 01 03 01  01 00 00 00
```

In Python:
```python
UAS_LS_UNIVERSAL_KEY = bytes([
    0x06, 0x0E, 0x2B, 0x34,
    0x02, 0x0B, 0x01, 0x01,
    0x0E, 0x01, 0x03, 0x01,
    0x01, 0x00, 0x00, 0x00,
])
```

---

## Packet Structure

```
[Universal Key — 16 bytes]
[BER Length — 1 or 3 bytes]
  [Tag 2 — Timestamp]              ← always first (MISB requirement)
  [Tag 65 — LS Version = 19]
  [Tag 5 — Platform Heading]
  [Tag 6 — Platform Pitch]
  [Tag 7 — Platform Roll]
  [Tag 13 — Sensor Latitude]
  [Tag 14 — Sensor Longitude]
  [Tag 15 — Sensor Altitude]
  [Tag 16 — Sensor HFoV]
  [Tag 17 — Sensor VFoV]
  [Tag 18 — Sensor Rel. Azimuth]
  [Tag 19 — Sensor Rel. Elevation]
  [Tag 20 — Sensor Rel. Roll]
  [Tag 21 — Slant Range]
  [Tag 23 — Frame Center Latitude]
  [Tag 24 — Frame Center Longitude]
  [Tag 25 — Frame Center Elevation]
  [Tag 1  — Checksum]              ← always last (MISB requirement)
```

Each tag-length-value (TLV) element:
```
[tag — 1 byte] [length — BER short or long form] [value — length bytes]
```

---

## Tags Implemented

### Tag 1 — Checksum

| Field | Value |
|-------|-------|
| Type | uint16, big-endian |
| Length | 2 bytes |
| Algorithm | CRC-16/CCITT-FALSE |
| Coverage | All bytes from Universal Key start through Tag 1 length byte (inclusive) |

The checksum covers the **entire** packet up to (and including) the Tag 1 type
byte (0x01) and length byte (0x02). The two-byte CRC value follows.

```python
# Compute checksum
packet_so_far = key + ber_length + all_other_tlvs
prefix = packet_so_far + b'\x01\x02'   # tag 1, length 2
crc = crc16_ccitt(prefix)              # init=0xFFFF, poly=0x1021, no reflect
final = prefix + struct.pack(">H", crc)
```

CRC-16/CCITT-FALSE parameters:
- Polynomial: 0x1021
- Initial value: 0xFFFF
- Input reflection: No
- Output reflection: No
- Final XOR: 0x0000
- Check value ("123456789"): **0x29B1**

---

### Tag 2 — Unix Timestamp (µs)

| Field | Value |
|-------|-------|
| Type | uint64, big-endian |
| Length | 8 bytes |
| Range | 0 – 2^64−1 µs |
| Notes | UTC Unix epoch; must be first tag in value section |

Set to `FDateTime::UtcNow().GetTicks() / 10` in Unreal (UE ticks are 100 ns).

---

### Tag 5 — Platform Heading

| Field | Value |
|-------|-------|
| Type | uint16, big-endian |
| Length | 2 bytes |
| Range mapped | 0° – 360° (exclusive upper bound, so 359.994° max) |
| Resolution | 360 / 65535 ≈ **0.0055°** |

Linear mapping: `raw = round((heading % 360.0) / 360.0 × 65535)`

---

### Tag 6 — Platform Pitch

| Field | Value |
|-------|-------|
| Type | int16, big-endian (two's complement) |
| Length | 2 bytes |
| Range mapped | −20° to +20° |
| Resolution | 40 / 65535 ≈ **0.00061°** |

Values outside ±20° are clamped before encoding.

---

### Tag 7 — Platform Roll

| Field | Value |
|-------|-------|
| Type | int16, big-endian |
| Length | 2 bytes |
| Range mapped | −50° to +50° |
| Resolution | 100 / 65535 ≈ **0.0015°** |

Values outside ±50° are clamped before encoding.

---

### Tag 13 — Sensor Latitude

| Field | Value |
|-------|-------|
| Type | int32, big-endian |
| Length | 4 bytes |
| Range mapped | −90° to +90° |
| Resolution | 180 / (2^32 − 1) ≈ **4.19 × 10⁻⁸°** ≈ **4.6 mm at equator** |

---

### Tag 14 — Sensor Longitude

| Field | Value |
|-------|-------|
| Type | int32, big-endian |
| Length | 4 bytes |
| Range mapped | −180° to +180° |
| Resolution | 360 / (2^32 − 1) ≈ **8.38 × 10⁻⁸°** ≈ **9.3 mm at equator** |

---

### Tag 15 — Sensor True Altitude (HAE)

| Field | Value |
|-------|-------|
| Type | uint16, big-endian |
| Length | 2 bytes |
| Range mapped | −900 m to +19,000 m (above WGS-84 ellipsoid) |
| Resolution | 19900 / 65535 ≈ **0.30 m** |

---

### Tag 16 — Sensor Horizontal FoV

| Field | Value |
|-------|-------|
| Type | uint16, big-endian |
| Length | 2 bytes |
| Range mapped | 0° to 180° |
| Resolution | 180 / 65535 ≈ **0.0027°** |

---

### Tag 17 — Sensor Vertical FoV

Same encoding as Tag 16. Derived from HFoV and aspect ratio in `SimCameraComponent`.

---

### Tag 18 — Sensor Relative Azimuth

| Field | Value |
|-------|-------|
| Type | uint32, big-endian |
| Length | 4 bytes |
| Range mapped | 0° to 360° |
| Resolution | 360 / (2^32 − 1) ≈ **8.38 × 10⁻⁸°** |
| Notes | Relative to aircraft nose; 0 = looking forward |

---

### Tag 19 — Sensor Relative Elevation

| Field | Value |
|-------|-------|
| Type | int32, big-endian |
| Length | 4 bytes |
| Range mapped | −180° to +180° |
| Resolution | 360 / (2^32 − 1) ≈ **8.38 × 10⁻⁸°** |
| Notes | Negative = looking down toward ground |

---

### Tag 20 — Sensor Relative Roll

| Field | Value |
|-------|-------|
| Type | uint32, big-endian |
| Length | 4 bytes |
| Range mapped | 0° to 360° |
| Resolution | same as Tag 18 |
| Notes | Fixed at 0 (stabilised gimbal) |

---

### Tag 21 — Slant Range

| Field | Value |
|-------|-------|
| Type | uint32, big-endian |
| Length | 4 bytes |
| Range mapped | 0 m to 5,000,000 m |
| Resolution | 5,000,000 / (2^32 − 1) ≈ **1.16 mm** |
| Notes | Line-of-sight distance from sensor to ground; computed via line-trace |

---

### Tag 23 — Frame Center Latitude

Same encoding as Tag 13. Ground-projected image centre.

---

### Tag 24 — Frame Center Longitude

Same encoding as Tag 14. Ground-projected image centre.

---

### Tag 25 — Frame Center Elevation

Same encoding as Tag 15. Ellipsoidal elevation of the image centre ground point.

---

### Tag 65 — UAS LS Version Number

| Field | Value |
|-------|-------|
| Type | uint8 |
| Length | 1 byte |
| Value | **19** (ST 0601.19) |

---

## Linear Scaling Formula

All tags use linear (affine) scaling between physical units and integer codes.

**Unsigned integer (uint16, uint32):**
```
raw = round((value - v_min) / (v_max - v_min) × (2^nbits − 1))
raw = clamp(raw, 0, 2^nbits − 1)

decoded = v_min + raw / (2^nbits − 1) × (v_max - v_min)
```

**Signed integer (int16, int32):**
```
raw = round((value - v_min) / (v_max - v_min) × (2^nbits − 1) − 2^(nbits−1))
raw = clamp(raw, −2^(nbits−1), 2^(nbits−1) − 1)

decoded = v_min + (raw + 2^(nbits−1)) / (2^nbits − 1) × (v_max - v_min)
```

Maximum quantisation error = `(v_max − v_min) / (2^nbits − 1) / 2`.

---

## BER Length Encoding

The MISB KLV standard uses Basic Encoding Rules (BER) for lengths:

| Value | Encoding |
|-------|---------|
| 0–127 | Single byte (short form): `[N]` |
| 128–255 | Two bytes (long form): `[0x81, N]` |
| 256–65535 | Three bytes: `[0x82, high, low]` |

A typical 1080p CamSim packet is ~200 bytes — short or 2-byte long form.

---

## Timestamp Alignment

The KLV packet and the corresponding video frame **must share the same PTS**
in the MPEG-TS stream. In the sidecar:

```python
pts_ns = timestamp_us * 1000          # µs → ns (GStreamer time base)

pipeline.push_frame(bgra_bytes, pts_ns)   # video appsrc
pipeline.push_klv(klv_bytes,   pts_ns)   # KLV appsrc — same PTS
```

The timestamp stored in Tag 2 is the same value (`timestamp_us`) written into
`ShmFrameSlot.timestamp_us` by `FrameExporter` at the moment `ReadPixels`
completes, so the KLV telemetry is temporally aligned with the rendered frame.

---

## Verifying KLV in a Live Stream

```bash
# Decode a captured KLV packet
python tools/klv_decoder.py 060e2b34020b01010e01030101000000...

# Live inspection (prints tag values as they arrive)
python tools/recv_and_inspect.py --multicast 239.1.1.1 --port 5004

# Run round-trip unit tests
pytest sidecar/test_klv_encoder.py -v

# Validate against the paretech/klvdata reference implementation
pip install klvdata
python - <<'EOF'
from klvdata import StreamParser
with open("capture.ts", "rb") as f:
    for packet in StreamParser(f):
        packet.structure()
EOF
```

---

## MPEG-TS Embedding

KLV is carried as an **asynchronous metadata** stream in the MPEG-TS:

```
PID 0x0201  stream_type = 0x15  (ISO 13818-1 Table 2-3, "Metadata in PES")
```

The `mpegtsmux` GStreamer element sets the stream type automatically when
the pad capabilities include `meta/x-klv`. GStreamer wraps each KLV packet
in a PES packet with `stream_id = 0xBD` (private stream 1) before muxing.

Receivers that parse PIDs conforming to STANAG 4609 (e.g., ATAK, Wintac, VLC
with KLV plugin) will find the KLV stream at PID 0x0201 and decode it using
the 16-byte Universal Key for dispatch.
