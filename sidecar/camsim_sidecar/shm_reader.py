"""
Shared memory reader for CamSim IPC.

Reads video frames and telemetry from the POSIX shm regions created by
the Unreal Engine plugin (FrameExporter / TelemetryExporter).

Shared memory layout: see SharedMemoryTypes.h
"""

from __future__ import annotations

import ctypes
import mmap
import os
import time

from .klv_encoder import TelemetryData


# ---------------------------------------------------------------------------
# Mirror of SharedMemoryTypes.h (must stay in sync)
# ---------------------------------------------------------------------------

SHM_FRAME_NAME     = "camsim_frames"
SHM_TELEMETRY_NAME = "camsim_telemetry"

CAMSIM_SHM_MAGIC       = 0x43534D46  # 'CSMF'
CAMSIM_TELEMETRY_MAGIC = 0x43534D54  # 'CSMT'
FRAME_SLOTS            = 3
BYTES_PER_PIXEL        = 4           # BGRA


class _FrameHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic",       ctypes.c_uint32),
        ("version",     ctypes.c_uint32),
        ("frame_width", ctypes.c_uint32),
        ("frame_height",ctypes.c_uint32),
        ("slot_count",  ctypes.c_uint32),
        ("slot_stride", ctypes.c_uint32),
        ("write_index", ctypes.c_uint32),
        ("read_index",  ctypes.c_uint32),
        ("_pad",        ctypes.c_uint8 * (64 - 8 * 4)),
    ]


class _FrameSlot(ctypes.Structure):
    # Natural alignment: c_uint64 aligns to 8, adding 4 implicit bytes after
    # height. Total = 32 bytes, matching ShmFrameSlot in SharedMemoryTypes.h.
    _fields_ = [
        ("sequence",     ctypes.c_uint32),
        ("width",        ctypes.c_uint32),
        ("height",       ctypes.c_uint32),
        ("timestamp_us", ctypes.c_uint64),
        ("data_size",    ctypes.c_uint32),
        ("_pad",         ctypes.c_uint32),
    ]


class _TelemetryHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic",      ctypes.c_uint32),
        ("version",    ctypes.c_uint32),
        ("write_slot", ctypes.c_uint32),
        ("_pad",       ctypes.c_uint32),
    ]


class _TelemetryFrame(ctypes.Structure):
    # Natural alignment: two 4-byte implicit pads (before sensor_lat_deg and
    # frame_center_lat_deg) plus 4-byte struct trailing pad → 128 bytes,
    # matching TelemetryFrame in SharedMemoryTypes.h.
    _fields_ = [
        ("timestamp_us",          ctypes.c_uint64),
        ("platform_lat_deg",      ctypes.c_double),
        ("platform_lon_deg",      ctypes.c_double),
        ("platform_alt_m_hae",    ctypes.c_double),
        ("platform_heading_deg",  ctypes.c_float),
        ("platform_pitch_deg",    ctypes.c_float),
        ("platform_roll_deg",     ctypes.c_float),
        ("sensor_lat_deg",        ctypes.c_double),
        ("sensor_lon_deg",        ctypes.c_double),
        ("sensor_alt_m_hae",      ctypes.c_float),
        ("sensor_rel_az_deg",     ctypes.c_float),
        ("sensor_rel_el_deg",     ctypes.c_float),
        ("sensor_rel_roll_deg",   ctypes.c_float),
        ("hfov_deg",              ctypes.c_float),
        ("vfov_deg",              ctypes.c_float),
        ("slant_range_m",         ctypes.c_float),
        ("frame_center_lat_deg",  ctypes.c_double),
        ("frame_center_lon_deg",  ctypes.c_double),
        ("frame_center_elev_m",   ctypes.c_float),
        ("sequence",              ctypes.c_uint32),
        ("_pad",                  ctypes.c_uint8 * 4),
    ]


assert ctypes.sizeof(_TelemetryFrame) == 128, \
    f"TelemetryFrame size mismatch: {ctypes.sizeof(_TelemetryFrame)} != 128"


# ---------------------------------------------------------------------------
# Frame ring-buffer reader
# ---------------------------------------------------------------------------

class FrameShmReader:
    """
    Maps the camsim_frames POSIX shared memory region and yields (frame_bytes,
    timestamp_us, width, height, sequence) tuples for each new frame produced
    by the Unreal plugin.
    """

    def __init__(self, shm_name: str = SHM_FRAME_NAME, poll_interval_s: float = 0.001):
        self._shm_name      = shm_name
        self._poll_interval = poll_interval_s
        self._fd: int | None = None
        self._mm: mmap.mmap | None = None
        self._header: _FrameHeader | None = None
        self._last_read_idx: int = 0
        self._slot_stride: int = 0

    # -----------------------------------------------------------------------
    # Context manager
    # -----------------------------------------------------------------------

    def open(self) -> "FrameShmReader":
        """Open (or wait for) the shared memory region."""
        import posix_ipc  # type: ignore
        memory = posix_ipc.SharedMemory(f"/{self._shm_name}", flags=0)
        self._fd = memory.fd
        # Do NOT call memory.close_fd() here — the fd must remain open until
        # after mmap() is called.  close() below handles fd cleanup.

        # Map the header first (64 bytes) to learn the full size
        header_map = mmap.mmap(self._fd, ctypes.sizeof(_FrameHeader),
                               mmap.MAP_SHARED, mmap.PROT_READ)
        h = _FrameHeader.from_buffer_copy(header_map)
        header_map.close()

        if h.magic != CAMSIM_SHM_MAGIC:
            raise RuntimeError(f"SHM magic mismatch: 0x{h.magic:08X}")

        total_size = (ctypes.sizeof(_FrameHeader) +
                      h.slot_count * h.slot_stride)
        self._mm = mmap.mmap(self._fd, total_size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        self._header = _FrameHeader.from_buffer(self._mm)
        self._slot_stride = h.slot_stride
        self._last_read_idx = self._header.write_index  # start from current

        return self

    def close(self):
        if self._mm:
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    # -----------------------------------------------------------------------
    # Frame iteration
    # -----------------------------------------------------------------------

    def read_next_frame(self) -> tuple[bytes, int, int, int, int] | None:
        """
        Non-blocking. Returns (pixels_bgra, timestamp_us, width, height, seq)
        if a new frame is available, otherwise None.
        """
        if not self._mm or not self._header:
            return None

        write_idx = self._header.write_index
        if write_idx == self._last_read_idx:
            return None  # no new frame

        slot_idx = self._last_read_idx % self._header.slot_count
        slot_offset = ctypes.sizeof(_FrameHeader) + slot_idx * self._slot_stride

        slot = _FrameSlot.from_buffer_copy(self._mm, slot_offset)
        pixel_offset = slot_offset + ctypes.sizeof(_FrameSlot)
        data_size = slot.width * slot.height * BYTES_PER_PIXEL

        self._mm.seek(pixel_offset)
        pixels = self._mm.read(data_size)

        self._last_read_idx += 1
        return pixels, slot.timestamp_us, slot.width, slot.height, slot.sequence

    def iter_frames(self):
        """Blocking generator: yields new frames as they arrive."""
        while True:
            frame = self.read_next_frame()
            if frame is not None:
                yield frame
            else:
                time.sleep(self._poll_interval)


# ---------------------------------------------------------------------------
# Telemetry double-buffer reader
# ---------------------------------------------------------------------------

class TelemetryShmReader:
    """
    Maps the camsim_telemetry POSIX shared memory region and reads the latest
    TelemetryFrame using a seqlock-safe protocol.
    """

    def __init__(self, shm_name: str = SHM_TELEMETRY_NAME):
        self._shm_name = shm_name
        self._fd: int | None = None
        self._mm: mmap.mmap | None = None

    def open(self) -> "TelemetryShmReader":
        import posix_ipc  # type: ignore
        memory = posix_ipc.SharedMemory(f"/{self._shm_name}", flags=0)
        self._fd = memory.fd
        # Do NOT call memory.close_fd() — fd must stay open until after mmap().

        total_size = ctypes.sizeof(_TelemetryHeader) + 2 * ctypes.sizeof(_TelemetryFrame)
        self._mm = mmap.mmap(self._fd, total_size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)

        hdr = _TelemetryHeader.from_buffer_copy(self._mm, 0)
        if hdr.magic != CAMSIM_TELEMETRY_MAGIC:
            raise RuntimeError(f"Telemetry SHM magic mismatch: 0x{hdr.magic:08X}")

        return self

    def close(self):
        if self._mm:
            self._mm.close()
            self._mm = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    def read(self) -> TelemetryData | None:
        """
        Seqlock-safe read of the latest telemetry frame.
        Returns None if data not yet available.
        """
        if not self._mm:
            return None

        hdr = _TelemetryHeader.from_buffer_copy(self._mm, 0)
        if hdr.magic != CAMSIM_TELEMETRY_MAGIC:
            return None

        # Read from the slot that is NOT being written
        read_slot = 1 - hdr.write_slot
        slot_offset = ctypes.sizeof(_TelemetryHeader) + read_slot * ctypes.sizeof(_TelemetryFrame)

        # Seqlock: read sequence before and after; retry on mismatch
        for _ in range(5):
            f = _TelemetryFrame.from_buffer_copy(self._mm, slot_offset)
            if f.sequence == 0:
                return None  # not yet populated
            # Re-read to check consistency
            f2 = _TelemetryFrame.from_buffer_copy(self._mm, slot_offset)
            if f.sequence == f2.sequence:
                return TelemetryData(
                    timestamp_us=f.timestamp_us,
                    platform_lat_deg=f.platform_lat_deg,
                    platform_lon_deg=f.platform_lon_deg,
                    platform_alt_m_hae=f.platform_alt_m_hae,
                    platform_heading_deg=f.platform_heading_deg,
                    platform_pitch_deg=f.platform_pitch_deg,
                    platform_roll_deg=f.platform_roll_deg,
                    sensor_lat_deg=f.sensor_lat_deg,
                    sensor_lon_deg=f.sensor_lon_deg,
                    sensor_alt_m_hae=f.sensor_alt_m_hae,
                    sensor_rel_az_deg=f.sensor_rel_az_deg,
                    sensor_rel_el_deg=f.sensor_rel_el_deg,
                    sensor_rel_roll_deg=f.sensor_rel_roll_deg,
                    hfov_deg=f.hfov_deg,
                    vfov_deg=f.vfov_deg,
                    slant_range_m=f.slant_range_m,
                    frame_center_lat_deg=f.frame_center_lat_deg,
                    frame_center_lon_deg=f.frame_center_lon_deg,
                    frame_center_elev_m=f.frame_center_elev_m,
                    sequence=f.sequence,
                )
        return None  # inconsistent reads (should not happen at 30 Hz)
