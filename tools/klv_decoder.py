#!/usr/bin/env python3
"""
Standalone MISB ST 0601 KLV packet decoder.

Parses a raw KLV packet (bytes) and prints human-readable tag values.
Can be used as a library or standalone script.

Usage:
    python klv_decoder.py <hex_bytes>
    python klv_decoder.py 060e2b34...

Also usable as a library:
    from klv_decoder import decode_klv_packet
    tags = decode_klv_packet(raw_bytes)
"""

from __future__ import annotations

import struct
import sys
from typing import Any


# ---------------------------------------------------------------------------
# UAS Local Set Universal Key
# ---------------------------------------------------------------------------

UAS_LS_UNIVERSAL_KEY = bytes([
    0x06, 0x0E, 0x2B, 0x34,
    0x02, 0x0B, 0x01, 0x01,
    0x0E, 0x01, 0x03, 0x01,
    0x01, 0x00, 0x00, 0x00,
])


# ---------------------------------------------------------------------------
# BER length decode
# ---------------------------------------------------------------------------

def _read_ber_length(data: bytes, offset: int) -> tuple[int, int]:
    """Return (length_value, new_offset)."""
    b = data[offset]
    if b < 0x80:
        return b, offset + 1
    num_octets = b & 0x7F
    length = 0
    for i in range(num_octets):
        length = (length << 8) | data[offset + 1 + i]
    return length, offset + 1 + num_octets


# ---------------------------------------------------------------------------
# Scale helpers (inverse of encoder)
# ---------------------------------------------------------------------------

def _decode_uint(raw: int, nbits: int, vmin: float, vmax: float) -> float:
    return vmin + raw / ((1 << nbits) - 1) * (vmax - vmin)


def _decode_int(raw: int, nbits: int, vmin: float, vmax: float) -> float:
    # Two's complement is handled by struct.unpack
    return vmin + (raw + (1 << (nbits - 1))) / ((1 << nbits) - 1) * (vmax - vmin)


# ---------------------------------------------------------------------------
# Per-tag decoders
# ---------------------------------------------------------------------------

def _dec_u8(v: bytes) -> int:
    return v[0]

def _dec_u16(v: bytes) -> int:
    return struct.unpack(">H", v)[0]

def _dec_i16(v: bytes) -> int:
    return struct.unpack(">h", v)[0]

def _dec_u32(v: bytes) -> int:
    return struct.unpack(">I", v)[0]

def _dec_i32(v: bytes) -> int:
    return struct.unpack(">i", v)[0]

def _dec_u64(v: bytes) -> int:
    return struct.unpack(">Q", v)[0]


TAG_DESCRIPTIONS: dict[int, tuple[str, Any]] = {
    1:  ("Checksum",              lambda v: ("0x%04X" % _dec_u16(v))),
    2:  ("Unix Timestamp (µs)",   lambda v: _dec_u64(v)),
    5:  ("Platform Heading (°)",  lambda v: _decode_uint(_dec_u16(v),  16, 0.0,    360.0)),
    6:  ("Platform Pitch (°)",    lambda v: _decode_int( _dec_i16(v),  16, -20.0,   20.0)),
    7:  ("Platform Roll (°)",     lambda v: _decode_int( _dec_i16(v),  16, -50.0,   50.0)),
    13: ("Sensor Latitude (°)",   lambda v: _decode_int( _dec_i32(v),  32, -90.0,   90.0)),
    14: ("Sensor Longitude (°)",  lambda v: _decode_int( _dec_i32(v),  32, -180.0, 180.0)),
    15: ("Sensor Altitude HAE (m)", lambda v: _decode_uint(_dec_u16(v), 16, -900.0, 19000.0)),
    16: ("Sensor HFoV (°)",       lambda v: _decode_uint(_dec_u16(v),  16, 0.0,    180.0)),
    17: ("Sensor VFoV (°)",       lambda v: _decode_uint(_dec_u16(v),  16, 0.0,    180.0)),
    18: ("Sensor Rel Azimuth (°)",lambda v: _decode_uint(_dec_u32(v),  32, 0.0,    360.0)),
    19: ("Sensor Rel Elevation(°)",lambda v:_decode_int( _dec_i32(v),  32, -180.0, 180.0)),
    20: ("Sensor Rel Roll (°)",   lambda v: _decode_uint(_dec_u32(v),  32, 0.0,    360.0)),
    21: ("Slant Range (m)",       lambda v: _decode_uint(_dec_u32(v),  32, 0.0,    5_000_000.0)),
    23: ("Frame Center Lat (°)",  lambda v: _decode_int( _dec_i32(v),  32, -90.0,   90.0)),
    24: ("Frame Center Lon (°)",  lambda v: _decode_int( _dec_i32(v),  32, -180.0, 180.0)),
    25: ("Frame Center Elev (m)", lambda v: _decode_uint(_dec_u16(v),  16, -900.0, 19000.0)),
    65: ("LS Version Number",     lambda v: _dec_u8(v)),
}


# ---------------------------------------------------------------------------
# Packet decoder
# ---------------------------------------------------------------------------

class KlvDecodeError(ValueError):
    pass


def decode_klv_packet(data: bytes) -> dict[int, Any]:
    """
    Parse a raw MISB ST 0601 UAS Local Set KLV packet.

    Returns a dict of {tag_number: decoded_value}.
    Raises KlvDecodeError on malformed input.
    """
    if len(data) < 16:
        raise KlvDecodeError(f"Packet too short ({len(data)} bytes)")

    key = data[:16]
    if key != UAS_LS_UNIVERSAL_KEY:
        raise KlvDecodeError(f"Universal key mismatch: {key.hex()}")

    total_length, offset = _read_ber_length(data, 16)
    if offset + total_length > len(data):
        raise KlvDecodeError(
            f"Declared length {total_length} exceeds packet size {len(data) - offset}"
        )

    end = offset + total_length
    tags: dict[int, Any] = {}

    while offset < end:
        tag = data[offset]
        offset += 1

        length, offset = _read_ber_length(data, offset)
        value = data[offset: offset + length]
        offset += length

        if tag in TAG_DESCRIPTIONS:
            _, decoder = TAG_DESCRIPTIONS[tag]
            try:
                tags[tag] = decoder(value)
            except Exception as exc:
                tags[tag] = f"<decode error: {exc}>"
        else:
            tags[tag] = value.hex()

    return tags


def print_klv_packet(data: bytes) -> None:
    """Parse and pretty-print a KLV packet to stdout."""
    try:
        tags = decode_klv_packet(data)
    except KlvDecodeError as exc:
        print(f"KLV decode error: {exc}")
        return

    print(f"KLV packet ({len(data)} bytes), {len(tags)} tags:")
    for tag_num, value in sorted(tags.items()):
        desc, _ = TAG_DESCRIPTIONS.get(tag_num, (f"Tag {tag_num}", None))
        if isinstance(value, float):
            print(f"  [{tag_num:3d}] {desc:<35} {value:.6f}")
        else:
            print(f"  [{tag_num:3d}] {desc:<35} {value}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: klv_decoder.py <hex_string>")
        print("  Example: klv_decoder.py 060e2b34020b01010e01030101000000...")
        sys.exit(1)

    hex_str = sys.argv[1].replace(" ", "").replace(":", "")
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError as exc:
        print(f"Invalid hex input: {exc}")
        sys.exit(1)

    print_klv_packet(raw)


if __name__ == "__main__":
    main()
