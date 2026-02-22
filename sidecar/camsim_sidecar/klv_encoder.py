"""
MISB ST 0601.19 KLV packet encoder.

Builds a single UAS Local Set packet per video frame.
All required tags from the architecture plan are implemented.

References:
  MISB ST 0601.19 "UAS Datalink Local Set"
  STANAG 4609 Edition 4
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from .crc import crc16_ccitt


# ---------------------------------------------------------------------------
# Universal Key for UAS Local Metadata Set (MISB ST 0601)
# 060E2B34 020B0101 0E010301 01000000
# ---------------------------------------------------------------------------
UAS_LS_UNIVERSAL_KEY = bytes([
    0x06, 0x0E, 0x2B, 0x34,
    0x02, 0x0B, 0x01, 0x01,
    0x0E, 0x01, 0x03, 0x01,
    0x01, 0x00, 0x00, 0x00,
])


# ---------------------------------------------------------------------------
# Telemetry data container (mirrors TelemetryFrame in SharedMemoryTypes.h)
# ---------------------------------------------------------------------------

@dataclass
class TelemetryData:
    timestamp_us: int           # Unix epoch µs
    platform_lat_deg: float
    platform_lon_deg: float
    platform_alt_m_hae: float
    platform_heading_deg: float
    platform_pitch_deg: float
    platform_roll_deg: float
    sensor_lat_deg: float
    sensor_lon_deg: float
    sensor_alt_m_hae: float
    sensor_rel_az_deg: float    # 0–360
    sensor_rel_el_deg: float    # ±180
    sensor_rel_roll_deg: float  # 0–360
    hfov_deg: float
    vfov_deg: float
    slant_range_m: float
    frame_center_lat_deg: float
    frame_center_lon_deg: float
    frame_center_elev_m: float
    sequence: int = 0


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def _ber_length(n: int) -> bytes:
    """Encode *n* as BER-OID short or long form."""
    if n < 0x80:
        return bytes([n])
    elif n <= 0xFF:
        return bytes([0x81, n])
    elif n <= 0xFFFF:
        return bytes([0x82, (n >> 8) & 0xFF, n & 0xFF])
    else:
        raise ValueError(f"BER length {n} too large for ST 0601 packets")


def _tlv(tag: int, value: bytes) -> bytes:
    """Encode a single-byte tag with BER length and value."""
    return bytes([tag]) + _ber_length(len(value)) + value


def _scale_uint(val: float, vmin: float, vmax: float, nbits: int) -> int:
    """Linear scale val ∈ [vmin, vmax] → unsigned int [0, 2^nbits - 1]."""
    rng = vmax - vmin
    scaled = round((val - vmin) / rng * ((1 << nbits) - 1))
    return max(0, min((1 << nbits) - 1, scaled))


def _scale_int(val: float, vmin: float, vmax: float, nbits: int) -> int:
    """Linear scale val ∈ [vmin, vmax] → signed int [-(2^(nbits-1)), 2^(nbits-1)-1]."""
    rng = vmax - vmin
    half = 1 << (nbits - 1)
    scaled = round((val - vmin) / rng * ((1 << nbits) - 1) - half)
    return max(-half, min(half - 1, scaled))


# ---------------------------------------------------------------------------
# Tag encoders — one function per tag
# ---------------------------------------------------------------------------

def _tag2(ts_us: int) -> bytes:
    """Tag 2 — Unix timestamp µs (uint64, 8 bytes)."""
    return _tlv(2, struct.pack(">Q", ts_us & 0xFFFF_FFFF_FFFF_FFFF))


def _tag5(heading_deg: float) -> bytes:
    """Tag 5 — Platform Heading (uint16, 0–360°)."""
    val = _scale_uint(heading_deg % 360.0, 0.0, 360.0, 16)
    return _tlv(5, struct.pack(">H", val))


def _tag6(pitch_deg: float) -> bytes:
    """Tag 6 — Platform Pitch (int16, ±20°, clamped)."""
    clamped = max(-20.0, min(20.0, pitch_deg))
    val = _scale_int(clamped, -20.0, 20.0, 16)
    return _tlv(6, struct.pack(">h", val))


def _tag7(roll_deg: float) -> bytes:
    """Tag 7 — Platform Roll (int16, ±50°, clamped)."""
    clamped = max(-50.0, min(50.0, roll_deg))
    val = _scale_int(clamped, -50.0, 50.0, 16)
    return _tlv(7, struct.pack(">h", val))


def _tag13(lat_deg: float) -> bytes:
    """Tag 13 — Sensor Latitude (int32, ±90°)."""
    val = _scale_int(lat_deg, -90.0, 90.0, 32)
    return _tlv(13, struct.pack(">i", val))


def _tag14(lon_deg: float) -> bytes:
    """Tag 14 — Sensor Longitude (int32, ±180°)."""
    val = _scale_int(lon_deg, -180.0, 180.0, 32)
    return _tlv(14, struct.pack(">i", val))


def _tag15(alt_m: float) -> bytes:
    """Tag 15 — Sensor True Altitude HAE (uint16, −900 to +19000 m)."""
    val = _scale_uint(alt_m, -900.0, 19000.0, 16)
    return _tlv(15, struct.pack(">H", val))


def _tag16(hfov_deg: float) -> bytes:
    """Tag 16 — Sensor Horizontal FoV (uint16, 0–180°)."""
    val = _scale_uint(hfov_deg, 0.0, 180.0, 16)
    return _tlv(16, struct.pack(">H", val))


def _tag17(vfov_deg: float) -> bytes:
    """Tag 17 — Sensor Vertical FoV (uint16, 0–180°)."""
    val = _scale_uint(vfov_deg, 0.0, 180.0, 16)
    return _tlv(17, struct.pack(">H", val))


def _tag18(rel_az_deg: float) -> bytes:
    """Tag 18 — Sensor Relative Azimuth (uint32, 0–360°)."""
    val = _scale_uint(rel_az_deg % 360.0, 0.0, 360.0, 32)
    return _tlv(18, struct.pack(">I", val))


def _tag19(rel_el_deg: float) -> bytes:
    """Tag 19 — Sensor Relative Elevation (int32, ±180°)."""
    val = _scale_int(rel_el_deg, -180.0, 180.0, 32)
    return _tlv(19, struct.pack(">i", val))


def _tag20(rel_roll_deg: float) -> bytes:
    """Tag 20 — Sensor Relative Roll (uint32, 0–360°)."""
    val = _scale_uint(rel_roll_deg % 360.0, 0.0, 360.0, 32)
    return _tlv(20, struct.pack(">I", val))


def _tag21(slant_range_m: float) -> bytes:
    """Tag 21 — Slant Range (uint32, 0–5,000,000 m)."""
    val = _scale_uint(max(0.0, min(5_000_000.0, slant_range_m)), 0.0, 5_000_000.0, 32)
    return _tlv(21, struct.pack(">I", val))


def _tag23(lat_deg: float) -> bytes:
    """Tag 23 — Frame Center Latitude (int32, ±90°)."""
    val = _scale_int(lat_deg, -90.0, 90.0, 32)
    return _tlv(23, struct.pack(">i", val))


def _tag24(lon_deg: float) -> bytes:
    """Tag 24 — Frame Center Longitude (int32, ±180°)."""
    val = _scale_int(lon_deg, -180.0, 180.0, 32)
    return _tlv(24, struct.pack(">i", val))


def _tag25(elev_m: float) -> bytes:
    """Tag 25 — Frame Center Elevation (uint16, −900 to +19000 m)."""
    val = _scale_uint(elev_m, -900.0, 19000.0, 16)
    return _tlv(25, struct.pack(">H", val))


def _tag65() -> bytes:
    """Tag 65 — UAS LS Version Number (uint8, = 19)."""
    return _tlv(65, bytes([19]))


def _tag1_checksum(packet_so_far: bytes) -> bytes:
    """Tag 1 — Checksum (uint16 CRC-16/CCITT over entire packet up to tag 1)."""
    # The checksum covers everything: universal key + length + all preceding TLVs
    # then tag byte (0x01) and length byte (0x02).
    prefix = packet_so_far + bytes([0x01, 0x02])
    crc = crc16_ccitt(prefix)
    return bytes([0x01, 0x02]) + struct.pack(">H", crc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encode_klv_packet(tel: TelemetryData) -> bytes:
    """
    Build a complete MISB ST 0601.19 UAS Local Set KLV packet for *tel*.

    Packet structure:
      Universal Key (16 bytes)
      BER Length
      Tag 2  — Timestamp (always first per standard)
      Tag 65 — LS Version
      Tags 5–7 — Platform attitude
      Tags 13–21 — Sensor position / pointing / range
      Tags 23–25 — Frame centre
      Tag 1  — Checksum (always last)

    Returns raw bytes ready to push to a GStreamer appsrc with
    caps="meta/x-klv".
    """
    value_bytes = bytearray()

    # Tag 2 must be first
    value_bytes += _tag2(tel.timestamp_us)
    value_bytes += _tag65()

    # Platform orientation
    value_bytes += _tag5(tel.platform_heading_deg)
    value_bytes += _tag6(tel.platform_pitch_deg)
    value_bytes += _tag7(tel.platform_roll_deg)

    # Sensor position
    value_bytes += _tag13(tel.sensor_lat_deg)
    value_bytes += _tag14(tel.sensor_lon_deg)
    value_bytes += _tag15(tel.sensor_alt_m_hae)

    # Sensor optics
    value_bytes += _tag16(tel.hfov_deg)
    value_bytes += _tag17(tel.vfov_deg)

    # Gimbal pointing
    value_bytes += _tag18(tel.sensor_rel_az_deg)
    value_bytes += _tag19(tel.sensor_rel_el_deg)
    value_bytes += _tag20(tel.sensor_rel_roll_deg)

    # Slant range
    value_bytes += _tag21(tel.slant_range_m)

    # Frame centre
    value_bytes += _tag23(tel.frame_center_lat_deg)
    value_bytes += _tag24(tel.frame_center_lon_deg)
    value_bytes += _tag25(tel.frame_center_elev_m)

    # Build packet up to (but not including) checksum tag
    packet_prefix = UAS_LS_UNIVERSAL_KEY + _ber_length(len(value_bytes) + 4) + bytes(value_bytes)

    # Append checksum (covers packet_prefix + tag byte + length byte)
    checksum_tlv = _tag1_checksum(packet_prefix)

    return packet_prefix + checksum_tlv
