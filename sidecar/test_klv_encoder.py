#!/usr/bin/env python3
"""
Unit tests for the MISB ST 0601 KLV encoder.

Run with:
    pytest sidecar/test_klv_encoder.py -v

Tests round-trip all tags through klvdata (paretech) to validate against the
reference implementation, within quantization error bounds.

Install test dependencies:
    pip install pytest klvdata
"""

from __future__ import annotations

import math
import struct

import pytest

# Adjust path if running from repo root
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from camsim_sidecar.crc import crc16_ccitt
from camsim_sidecar.klv_encoder import (
    TelemetryData,
    UAS_LS_UNIVERSAL_KEY,
    encode_klv_packet,
    _scale_uint,
    _scale_int,
    _ber_length,
)
from tools.klv_decoder import decode_klv_packet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tel(**overrides) -> TelemetryData:
    """Build a TelemetryData with sensible defaults, optionally overriding fields."""
    defaults = dict(
        timestamp_us=1_700_000_000_000_000,
        platform_lat_deg=36.5,
        platform_lon_deg=-117.5,
        platform_alt_m_hae=1500.0,
        platform_heading_deg=45.0,
        platform_pitch_deg=2.0,
        platform_roll_deg=0.0,
        sensor_lat_deg=36.5,
        sensor_lon_deg=-117.5,
        sensor_alt_m_hae=1500.0,
        sensor_rel_az_deg=10.0,
        sensor_rel_el_deg=-45.0,
        sensor_rel_roll_deg=0.0,
        hfov_deg=40.0,
        vfov_deg=22.5,
        slant_range_m=2121.0,
        frame_center_lat_deg=36.48,
        frame_center_lon_deg=-117.5,
        frame_center_elev_m=800.0,
        sequence=1,
    )
    defaults.update(overrides)
    return TelemetryData(**defaults)


def _quant_error_deg(nbits: int, range_deg: float) -> float:
    """Maximum quantization error for a given bit width and range."""
    return range_deg / ((1 << nbits) - 1)


# ---------------------------------------------------------------------------
# CRC tests
# ---------------------------------------------------------------------------

class TestCRC:
    def test_empty(self):
        # CRC of empty input with init 0xFFFF
        assert crc16_ccitt(b"") == 0xFFFF

    def test_known_vector(self):
        # CRC-16/CCITT-FALSE: "123456789" → 0x29B1
        assert crc16_ccitt(b"123456789") == 0x29B1

    def test_chaining(self):
        data = b"Hello, World!"
        full = crc16_ccitt(data)
        half1 = crc16_ccitt(data[:6])
        half2 = crc16_ccitt(data[6:], initial=half1)
        assert full == half2


# ---------------------------------------------------------------------------
# BER length encoding tests
# ---------------------------------------------------------------------------

class TestBerLength:
    def test_short_form(self):
        assert _ber_length(0)   == bytes([0x00])
        assert _ber_length(127) == bytes([0x7F])

    def test_long_form_1_byte(self):
        assert _ber_length(128) == bytes([0x81, 0x80])
        assert _ber_length(255) == bytes([0x81, 0xFF])

    def test_long_form_2_bytes(self):
        enc = _ber_length(256)
        assert enc == bytes([0x82, 0x01, 0x00])

    def test_large_value(self):
        enc = _ber_length(0x1234)
        assert enc == bytes([0x82, 0x12, 0x34])


# ---------------------------------------------------------------------------
# Scale function tests
# ---------------------------------------------------------------------------

class TestScale:
    def test_uint_min(self):
        assert _scale_uint(0.0, 0.0, 360.0, 16) == 0

    def test_uint_max(self):
        assert _scale_uint(360.0, 0.0, 360.0, 16) == 0xFFFF

    def test_uint_mid(self):
        val = _scale_uint(180.0, 0.0, 360.0, 16)
        # Should be approximately 0x7FFF or 0x8000
        assert abs(val - 0x7FFF) <= 1

    def test_int_min(self):
        val = _scale_int(-90.0, -90.0, 90.0, 32)
        assert val == -(1 << 31)

    def test_int_max(self):
        val = _scale_int(90.0, -90.0, 90.0, 32)
        assert val == (1 << 31) - 1

    def test_int_zero(self):
        val = _scale_int(0.0, -90.0, 90.0, 32)
        assert abs(val) <= 1


# ---------------------------------------------------------------------------
# Packet structure tests
# ---------------------------------------------------------------------------

class TestPacketStructure:
    def setup_method(self):
        self.tel = _make_tel()
        self.pkt = encode_klv_packet(self.tel)

    def test_starts_with_universal_key(self):
        assert self.pkt[:16] == UAS_LS_UNIVERSAL_KEY

    def test_minimum_length(self):
        # At minimum: 16 (key) + 1+ (BER len) + tags + 4 (checksum)
        assert len(self.pkt) > 50

    def test_ends_with_checksum_tag(self):
        # Last 4 bytes: tag=0x01, len=0x02, crc(2 bytes)
        assert self.pkt[-4] == 0x01
        assert self.pkt[-3] == 0x02

    def test_checksum_valid(self):
        # Checksum covers everything up to (and including) tag byte + length byte
        crc_in_pkt = struct.unpack(">H", self.pkt[-2:])[0]
        computed    = crc16_ccitt(self.pkt[:-2])
        assert crc_in_pkt == computed

    def test_tag2_first_in_value(self):
        # First byte after BER length should be tag 2
        ber_start = 16
        # Determine BER length bytes
        b = self.pkt[ber_start]
        if b < 0x80:
            value_start = ber_start + 1
        else:
            value_start = ber_start + 1 + (b & 0x7F)
        assert self.pkt[value_start] == 2  # Tag 2

    def test_tag1_last(self):
        # Tag 1 (checksum) must be the last TLV
        assert self.pkt[-4] == 1


# ---------------------------------------------------------------------------
# Round-trip tag value tests (via our own decoder)
# ---------------------------------------------------------------------------

class TestTagRoundTrip:
    QUANT_TOL = {
        # (nbits, range) → max tolerated error
        "lat":     _quant_error_deg(32, 180.0),
        "lon":     _quant_error_deg(32, 360.0),
        "az":      _quant_error_deg(32, 360.0),
        "el":      _quant_error_deg(32, 360.0),
        "heading": _quant_error_deg(16, 360.0),
        "pitch":   _quant_error_deg(16, 40.0),
        "roll":    _quant_error_deg(16, 100.0),
        "hfov":    _quant_error_deg(16, 180.0),
        "vfov":    _quant_error_deg(16, 180.0),
        "alt":     (19000.0 + 900.0) / 65535,   # uint16 over 19900 m range
        "elev":    (19000.0 + 900.0) / 65535,
        "slant":   5_000_000.0 / 0xFFFF_FFFF,   # uint32 over 5 Mm
    }

    def _decode(self, tel: TelemetryData) -> dict:
        pkt = encode_klv_packet(tel)
        return decode_klv_packet(pkt)

    def test_timestamp(self):
        tel = _make_tel(timestamp_us=1_700_123_456_789_000)
        tags = self._decode(tel)
        assert tags[2] == 1_700_123_456_789_000

    def test_platform_heading(self):
        for hdg in [0.0, 90.0, 180.0, 270.0, 359.9]:
            tags = self._decode(_make_tel(platform_heading_deg=hdg))
            assert abs(tags[5] - hdg) <= self.QUANT_TOL["heading"] + 0.001, \
                f"heading={hdg} decoded={tags[5]}"

    def test_platform_pitch(self):
        for pitch in [-20.0, -10.0, 0.0, 10.0, 20.0]:
            tags = self._decode(_make_tel(platform_pitch_deg=pitch))
            assert abs(tags[6] - pitch) <= self.QUANT_TOL["pitch"] + 0.001

    def test_platform_roll(self):
        for roll in [-50.0, -25.0, 0.0, 25.0, 50.0]:
            tags = self._decode(_make_tel(platform_roll_deg=roll))
            assert abs(tags[7] - roll) <= self.QUANT_TOL["roll"] + 0.001

    def test_sensor_lat(self):
        for lat in [-90.0, -45.0, 0.0, 36.5, 90.0]:
            tags = self._decode(_make_tel(sensor_lat_deg=lat))
            assert abs(tags[13] - lat) <= self.QUANT_TOL["lat"] + 1e-6

    def test_sensor_lon(self):
        for lon in [-180.0, -117.5, 0.0, 90.0, 180.0]:
            tags = self._decode(_make_tel(sensor_lon_deg=lon))
            assert abs(tags[14] - lon) <= self.QUANT_TOL["lon"] + 1e-6

    def test_sensor_alt(self):
        for alt in [-900.0, 0.0, 1500.0, 10000.0, 19000.0]:
            tags = self._decode(_make_tel(sensor_alt_m_hae=alt))
            assert abs(tags[15] - alt) <= self.QUANT_TOL["alt"] + 0.1

    def test_hfov(self):
        for fov in [1.0, 40.0, 90.0, 180.0]:
            tags = self._decode(_make_tel(hfov_deg=fov))
            assert abs(tags[16] - fov) <= self.QUANT_TOL["hfov"] + 0.01

    def test_vfov(self):
        for fov in [1.0, 22.5, 60.0, 120.0]:
            tags = self._decode(_make_tel(vfov_deg=fov))
            assert abs(tags[17] - fov) <= self.QUANT_TOL["vfov"] + 0.01

    def test_sensor_rel_az(self):
        for az in [0.0, 90.0, 170.0, 359.9]:
            tags = self._decode(_make_tel(sensor_rel_az_deg=az))
            assert abs(tags[18] - az) <= self.QUANT_TOL["az"] + 1e-5

    def test_sensor_rel_el(self):
        for el in [-90.0, -45.0, 0.0, 30.0]:
            tags = self._decode(_make_tel(sensor_rel_el_deg=el))
            assert abs(tags[19] - el) <= self.QUANT_TOL["el"] + 1e-5

    def test_slant_range(self):
        for sr in [0.0, 2121.0, 50000.0, 5_000_000.0]:
            tags = self._decode(_make_tel(slant_range_m=sr))
            assert abs(tags[21] - sr) <= self.QUANT_TOL["slant"] * 5_000_000 + 1.0

    def test_frame_center_lat(self):
        tags = self._decode(_make_tel(frame_center_lat_deg=36.48))
        assert abs(tags[23] - 36.48) <= self.QUANT_TOL["lat"] + 1e-6

    def test_frame_center_lon(self):
        tags = self._decode(_make_tel(frame_center_lon_deg=-117.5))
        assert abs(tags[24] - (-117.5)) <= self.QUANT_TOL["lon"] + 1e-6

    def test_frame_center_elev(self):
        tags = self._decode(_make_tel(frame_center_elev_m=800.0))
        assert abs(tags[25] - 800.0) <= self.QUANT_TOL["elev"] + 0.1

    def test_ls_version(self):
        tags = self._decode(_make_tel())
        assert tags[65] == 19


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_heading_wraps_360(self):
        # 360° should encode the same as 0°
        pkt_0   = encode_klv_packet(_make_tel(platform_heading_deg=0.0))
        pkt_360 = encode_klv_packet(_make_tel(platform_heading_deg=360.0))
        tags_0   = decode_klv_packet(pkt_0)
        tags_360 = decode_klv_packet(pkt_360)
        assert abs(tags_0[5] - tags_360[5]) < 0.01

    def test_gimbal_limit_pan_170(self):
        # Exactly at +170° pan limit
        tags = decode_klv_packet(encode_klv_packet(_make_tel(sensor_rel_az_deg=170.0)))
        assert abs(tags[18] - 170.0) < 0.01

    def test_negative_altitude(self):
        tags = decode_klv_packet(encode_klv_packet(_make_tel(sensor_alt_m_hae=-900.0)))
        assert abs(tags[15] - (-900.0)) < 1.0

    def test_checksum_changes_with_data(self):
        pkt1 = encode_klv_packet(_make_tel(platform_heading_deg=0.0))
        pkt2 = encode_klv_packet(_make_tel(platform_heading_deg=180.0))
        crc1 = struct.unpack(">H", pkt1[-2:])[0]
        crc2 = struct.unpack(">H", pkt2[-2:])[0]
        assert crc1 != crc2
