#!/usr/bin/env python3
"""
tools/frame_gen.py — Synthetic frame generator for CamSim macOS development.

Mimics the Unreal Engine CamSimPlugin: creates the two POSIX shared memory
regions (camsim_frames, camsim_telemetry) and writes synthetic BGRA colour-bar
frames + fake aircraft telemetry at the target frame rate.

When JSBSim is available (default), uses a full 6-DOF flight dynamics model
(Cessna 172) flying a banked surveillance orbit for realistic aircraft state.
Falls back to simple synthetic telemetry with --no-jsbsim or if JSBSim is not
installed.

Run this inside the 'frame-gen' Docker service alongside the sidecar to get a
full end-to-end MPEG-TS + KLV stream without Unreal Engine installed.

Dependencies:
    pip install posix-ipc jsbsim
"""

from __future__ import annotations

import argparse
import ctypes
import math
import mmap
import os
import signal
import sys
import time

import posix_ipc  # pip install posix-ipc

# Optional — graceful fallback when JSBSim is not installed
try:
    import jsbsim
    _HAS_JSBSIM = True
except ImportError:
    _HAS_JSBSIM = False


# ---------------------------------------------------------------------------
# Wire-format constants and structs
# Must match SharedMemoryTypes.h exactly (natural / default alignment).
# ---------------------------------------------------------------------------

CAMSIM_SHM_MAGIC       = 0x43534D46  # 'CSMF'
CAMSIM_TELEMETRY_MAGIC = 0x43534D54  # 'CSMT'
FRAME_SLOTS            = 3
BYTES_PER_PIXEL        = 4           # BGRA

_FT_TO_M    = 0.3048
_RAD_TO_DEG = 180.0 / math.pi


class _FrameHeader(ctypes.Structure):
    # All uint32 fields — pack makes no difference; kept for clarity.
    _pack_ = 1
    _fields_ = [
        ("magic",        ctypes.c_uint32),
        ("version",      ctypes.c_uint32),
        ("frame_width",  ctypes.c_uint32),
        ("frame_height", ctypes.c_uint32),
        ("slot_count",   ctypes.c_uint32),
        ("slot_stride",  ctypes.c_uint32),
        ("write_index",  ctypes.c_uint32),
        ("read_index",   ctypes.c_uint32),
        ("_pad",         ctypes.c_uint8 * (64 - 8 * 4)),
    ]


assert ctypes.sizeof(_FrameHeader) == 64


class _FrameSlot(ctypes.Structure):
    # Natural alignment: c_uint64 aligns to 8, inserting 4 implicit bytes
    # after height.  Total = 32 bytes (matches ShmFrameSlot static_assert).
    _fields_ = [
        ("sequence",     ctypes.c_uint32),
        ("width",        ctypes.c_uint32),
        ("height",       ctypes.c_uint32),
        ("timestamp_us", ctypes.c_uint64),
        ("data_size",    ctypes.c_uint32),
        ("_pad",         ctypes.c_uint32),
    ]


assert ctypes.sizeof(_FrameSlot) == 32, (
    f"_FrameSlot: {ctypes.sizeof(_FrameSlot)} != 32"
)


class _TelemetryHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic",      ctypes.c_uint32),
        ("version",    ctypes.c_uint32),
        ("write_slot", ctypes.c_uint32),
        ("_pad",       ctypes.c_uint32),
    ]


assert ctypes.sizeof(_TelemetryHeader) == 16


class _TelemetryFrame(ctypes.Structure):
    # Natural alignment: two 4-byte implicit pads (before sensor_lat_deg and
    # before frame_center_lat_deg) + 4-byte trailing pad → 128 bytes.
    _fields_ = [
        ("timestamp_us",          ctypes.c_uint64),
        ("platform_lat_deg",      ctypes.c_double),
        ("platform_lon_deg",      ctypes.c_double),
        ("platform_alt_m_hae",    ctypes.c_double),
        ("platform_heading_deg",  ctypes.c_float),
        ("platform_pitch_deg",    ctypes.c_float),
        ("platform_roll_deg",     ctypes.c_float),
        ("sensor_lat_deg",        ctypes.c_double),   # +4 implicit pad before
        ("sensor_lon_deg",        ctypes.c_double),
        ("sensor_alt_m_hae",      ctypes.c_float),
        ("sensor_rel_az_deg",     ctypes.c_float),
        ("sensor_rel_el_deg",     ctypes.c_float),
        ("sensor_rel_roll_deg",   ctypes.c_float),
        ("hfov_deg",              ctypes.c_float),
        ("vfov_deg",              ctypes.c_float),
        ("slant_range_m",         ctypes.c_float),
        ("frame_center_lat_deg",  ctypes.c_double),   # +4 implicit pad before
        ("frame_center_lon_deg",  ctypes.c_double),
        ("frame_center_elev_m",   ctypes.c_float),
        ("sequence",              ctypes.c_uint32),
        ("_pad",                  ctypes.c_uint8 * 4),  # + 4 trailing = 128
    ]


assert ctypes.sizeof(_TelemetryFrame) == 128, (
    f"_TelemetryFrame: {ctypes.sizeof(_TelemetryFrame)} != 128"
)


# ---------------------------------------------------------------------------
# Synthetic video: 8-stripe SMPTE-style colour bars (BGRA order)
# ---------------------------------------------------------------------------

_BARS_BGRA = [
    (235, 235, 235, 255),  # White
    ( 16, 235, 235, 255),  # Yellow  (B=16,  G=235, R=235)
    (235, 235,  16, 255),  # Cyan    (B=235, G=235, R=16)
    ( 16, 235,  16, 255),  # Green
    (235,  16, 235, 255),  # Magenta
    ( 16,  16, 235, 255),  # Red     (B=16,  G=16,  R=235)
    (235,  16,  16, 255),  # Blue    (B=235, G=16,  R=16)
    ( 16,  16,  16, 255),  # Black
]


def _make_frame(width: int, height: int, bar_offset: int) -> bytes:
    """
    Return a width×height BGRA frame of 8 colour bars.  bar_offset rotates
    which colour appears in which stripe (shifts once per second).
    """
    n = len(_BARS_BGRA)
    colors = _BARS_BGRA[bar_offset % n:] + _BARS_BGRA[:bar_offset % n]
    stripe = width // n

    row = bytearray(width * 4)
    for i, (b, g, r, a) in enumerate(colors):
        x0 = i * stripe
        x1 = (i + 1) * stripe if i < n - 1 else width
        pixel = bytes([b, g, r, a])
        # Fill stripe in one C-speed operation
        for x in range(x0, x1):
            row[x * 4: x * 4 + 4] = pixel

    # Repeat the row for every scan line (fast bytes repetition in CPython)
    return bytes(row) * height


# ---------------------------------------------------------------------------
# Shared memory helpers
# ---------------------------------------------------------------------------

def _create_shm(name: str, size: int) -> mmap.mmap:
    """Unlink any stale region, create fresh, return a writable mmap."""
    try:
        posix_ipc.unlink_shared_memory("/" + name)
    except posix_ipc.ExistentialError:
        pass

    shm = posix_ipc.SharedMemory("/" + name, posix_ipc.O_CREAT, size=size)
    mm = mmap.mmap(shm.fd, size, mmap.MAP_SHARED,
                   mmap.PROT_READ | mmap.PROT_WRITE)
    shm.close_fd()
    return mm


def _init_frame_shm(width: int, height: int) -> tuple[mmap.mmap, int, int]:
    """
    Create camsim_frames shm and write the ShmFrameHeader.
    Returns (mmap, header_size_bytes, slot_stride_bytes).
    """
    pixel_bytes = width * height * BYTES_PER_PIXEL
    slot_stride = ctypes.sizeof(_FrameSlot) + pixel_bytes
    total       = ctypes.sizeof(_FrameHeader) + FRAME_SLOTS * slot_stride

    mm  = _create_shm("camsim_frames", total)
    hdr = _FrameHeader.from_buffer(mm)
    hdr.magic        = CAMSIM_SHM_MAGIC
    hdr.version      = 1
    hdr.frame_width  = width
    hdr.frame_height = height
    hdr.slot_count   = FRAME_SLOTS
    hdr.slot_stride  = slot_stride
    hdr.write_index  = 0
    hdr.read_index   = 0
    mm.flush()

    return mm, ctypes.sizeof(_FrameHeader), slot_stride


def _init_telemetry_shm() -> mmap.mmap:
    """Create camsim_telemetry shm and write the ShmTelemetryHeader."""
    total = ctypes.sizeof(_TelemetryHeader) + 2 * ctypes.sizeof(_TelemetryFrame)
    mm    = _create_shm("camsim_telemetry", total)

    hdr = _TelemetryHeader.from_buffer(mm)
    hdr.magic      = CAMSIM_TELEMETRY_MAGIC
    hdr.version    = 1
    hdr.write_slot = 0
    mm.flush()

    return mm


# ---------------------------------------------------------------------------
# Simple synthetic telemetry (fallback when JSBSim is unavailable)
# ---------------------------------------------------------------------------

_GIMBAL_EL     = -45.0    # deg (negative = looking down)
_HFOV          = 18.0     # degrees
_VFOV          = 10.0     # degrees


def _build_telemetry_simple(seq: int, elapsed: float,
                            lat: float, lon: float, alt_m: float,
                            az_deg: float) -> _TelemetryFrame:
    heading_deg = (elapsed * 3.0) % 360.0   # 3 deg/s slow turn

    slant_m  = alt_m / math.cos(math.radians(abs(_GIMBAL_EL)))
    horiz_m  = slant_m * math.cos(math.radians(_GIMBAL_EL))
    fc_lat   = lat + math.degrees(horiz_m / 6_378_137.0)
    fc_lon   = lon

    f = _TelemetryFrame()
    f.timestamp_us         = int(time.time() * 1_000_000)
    f.platform_lat_deg     = lat
    f.platform_lon_deg     = lon
    f.platform_alt_m_hae   = alt_m
    f.platform_heading_deg = heading_deg
    f.platform_pitch_deg   = 0.0
    f.platform_roll_deg    = 0.0
    f.sensor_lat_deg       = lat
    f.sensor_lon_deg       = lon
    f.sensor_alt_m_hae     = alt_m
    f.sensor_rel_az_deg    = az_deg
    f.sensor_rel_el_deg    = _GIMBAL_EL
    f.sensor_rel_roll_deg  = 0.0
    f.hfov_deg             = _HFOV
    f.vfov_deg             = _VFOV
    f.slant_range_m        = slant_m
    f.frame_center_lat_deg = fc_lat
    f.frame_center_lon_deg = fc_lon
    f.frame_center_elev_m  = 0.0
    f.sequence             = seq
    return f


# ---------------------------------------------------------------------------
# JSBSim 6-DOF flight dynamics
# ---------------------------------------------------------------------------

def _init_jsbsim(aircraft: str, lat: float, lon: float, alt_ft: float,
                 heading: float, speed_kts: float) -> "jsbsim.FGFDMExec":
    """Initialize JSBSim FDM and trim for steady level flight."""
    fdm = jsbsim.FGFDMExec(jsbsim.get_default_root_dir())
    fdm.set_debug_lvl(0)
    fdm.load_model(aircraft)
    fdm.set_dt(1.0 / 120.0)  # 120 Hz internal physics rate

    # Initial conditions
    fdm["ic/lat-geod-deg"] = lat
    fdm["ic/long-gc-deg"] = lon
    fdm["ic/h-sl-ft"] = alt_ft
    fdm["ic/psi-true-deg"] = heading
    fdm["ic/vc-kts"] = speed_kts
    fdm["ic/gamma-deg"] = 0.0  # level flight
    fdm.run_ic()

    # Trim for steady level flight
    try:
        fdm.do_trim(1)  # tFull
        print("[frame-gen] JSBSim trim succeeded", flush=True)
    except RuntimeError:
        print("[frame-gen] JSBSim trim failed — using raw IC state", flush=True)

    return fdm


def _update_orbit_controller(fdm: "jsbsim.FGFDMExec",
                             target_bank_deg: float,
                             target_alt_ft: float,
                             target_speed_kts: float) -> None:
    """Simple proportional controller for a banked surveillance orbit."""
    # Current state
    bank_rad = fdm["attitude/phi-rad"]
    bank_deg = bank_rad * _RAD_TO_DEG
    alt_ft = fdm["position/h-sl-ft"]
    vc_kts = fdm["velocities/vc-kts"]
    beta_rad = fdm["aero/beta-rad"]
    pitch_rate = fdm["velocities/q-rad_sec"]

    # Aileron: P-control on bank angle error
    bank_err = target_bank_deg - bank_deg
    aileron = max(-1.0, min(1.0, bank_err * 0.01))
    fdm["fcs/aileron-cmd-norm"] = aileron

    # Elevator: P-control on altitude error + pitch rate damping
    alt_err = target_alt_ft - alt_ft
    elevator = max(-1.0, min(1.0, alt_err * 0.001 - pitch_rate * 0.5))
    fdm["fcs/elevator-cmd-norm"] = elevator

    # Throttle: P-control on airspeed error
    speed_err = target_speed_kts - vc_kts
    throttle = max(0.0, min(1.0, 0.6 + speed_err * 0.02))
    fdm["fcs/throttle-cmd-norm"] = throttle

    # Rudder: P-control on sideslip for coordinated turn
    rudder = max(-1.0, min(1.0, -beta_rad * 2.0))
    fdm["fcs/rudder-cmd-norm"] = rudder


def _build_telemetry_jsbsim(fdm: "jsbsim.FGFDMExec", seq: int,
                            az_deg: float) -> _TelemetryFrame:
    """Build telemetry frame from current JSBSim state."""
    lat = fdm["position/lat-geod-deg"]
    lon = fdm["position/long-gc-deg"]
    alt_m = fdm["position/h-sl-ft"] * _FT_TO_M
    heading_deg = fdm["attitude/psi-true-rad"] * _RAD_TO_DEG
    pitch_deg = fdm["attitude/theta-rad"] * _RAD_TO_DEG
    roll_deg = fdm["attitude/phi-rad"] * _RAD_TO_DEG

    # Normalize heading to [0, 360)
    heading_deg = heading_deg % 360.0

    slant_m = alt_m / math.cos(math.radians(abs(_GIMBAL_EL)))
    horiz_m = slant_m * math.cos(math.radians(_GIMBAL_EL))
    fc_lat = lat + math.degrees(horiz_m / 6_378_137.0)
    fc_lon = lon

    f = _TelemetryFrame()
    f.timestamp_us         = int(time.time() * 1_000_000)
    f.platform_lat_deg     = lat
    f.platform_lon_deg     = lon
    f.platform_alt_m_hae   = alt_m
    f.platform_heading_deg = heading_deg
    f.platform_pitch_deg   = pitch_deg
    f.platform_roll_deg    = roll_deg
    f.sensor_lat_deg       = lat
    f.sensor_lon_deg       = lon
    f.sensor_alt_m_hae     = alt_m
    f.sensor_rel_az_deg    = az_deg
    f.sensor_rel_el_deg    = _GIMBAL_EL
    f.sensor_rel_roll_deg  = 0.0
    f.hfov_deg             = _HFOV
    f.vfov_deg             = _VFOV
    f.slant_range_m        = slant_m
    f.frame_center_lat_deg = fc_lat
    f.frame_center_lon_deg = fc_lon
    f.frame_center_elev_m  = 0.0
    f.sequence             = seq
    return f


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    use_jsbsim = not args.no_jsbsim and _HAS_JSBSIM
    if args.no_jsbsim:
        print("[frame-gen] JSBSim disabled via --no-jsbsim", flush=True)
    elif not _HAS_JSBSIM:
        print("[frame-gen] WARNING: jsbsim not installed — using simple mode",
              flush=True)

    print(
        f"[frame-gen] {args.width}x{args.height} @ {args.fps} fps"
        f"  mode={'jsbsim' if use_jsbsim else 'simple'}",
        flush=True,
    )

    # JSBSim initialization
    fdm = None
    if use_jsbsim:
        alt_m = args.alt_ft * _FT_TO_M
        fdm = _init_jsbsim(
            aircraft=args.aircraft,
            lat=args.lat,
            lon=args.lon,
            alt_ft=args.alt_ft,
            heading=args.heading,
            speed_kts=args.speed,
        )
        jsbsim_steps_per_frame = math.ceil(120.0 / args.fps)
        print(
            f"[frame-gen] JSBSim: {args.aircraft} at {args.lat:.4f},"
            f" {args.lon:.4f}  alt={args.alt_ft} ft"
            f"  speed={args.speed} kts  bank={args.bank_angle}°"
            f"  ({jsbsim_steps_per_frame} physics steps/frame)",
            flush=True,
        )

    frame_mm, hdr_size, slot_stride = _init_frame_shm(args.width, args.height)
    tel_mm                          = _init_telemetry_shm()

    print("[frame-gen] Shared memory ready — waiting for sidecar …", flush=True)

    running = True

    def _stop(sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    frame_hdr = _FrameHeader.from_buffer(frame_mm)
    tel_hdr   = _TelemetryHeader.from_buffer(tel_mm)

    tel_slot_size = ctypes.sizeof(_TelemetryFrame)
    tel_base      = ctypes.sizeof(_TelemetryHeader)

    seq          = 0
    interval     = 1.0 / args.fps
    start_time   = time.time()
    next_frame_t = time.monotonic()
    last_log_t   = time.monotonic()

    # Gimbal state (same for both modes)
    gimbal_az_deg = 0.0

    while running:
        sleep_t = next_frame_t - time.monotonic()
        if sleep_t > 0:
            time.sleep(sleep_t)

        elapsed   = time.time() - start_time
        bar_shift = int(elapsed)  # rotate bars once per second

        # Gimbal pan: 5 deg/s
        gimbal_az_deg = (elapsed * 5.0) % 360.0

        # ----------------------------------------------------------------
        # Step JSBSim physics (if enabled)
        # ----------------------------------------------------------------
        if fdm is not None:
            for _ in range(jsbsim_steps_per_frame):
                _update_orbit_controller(
                    fdm, args.bank_angle, args.alt_ft, args.speed,
                )
                fdm.run()

        # ----------------------------------------------------------------
        # Video frame → camsim_frames slot
        # ----------------------------------------------------------------
        pixel_bytes = _make_frame(args.width, args.height, bar_shift)
        ts_us       = int(time.time() * 1_000_000)

        slot_idx = frame_hdr.write_index % FRAME_SLOTS
        slot_off = hdr_size + slot_idx * slot_stride

        slot = _FrameSlot.from_buffer(frame_mm, slot_off)
        slot.sequence     = seq
        slot.width        = args.width
        slot.height       = args.height
        slot.timestamp_us = ts_us
        slot.data_size    = len(pixel_bytes)

        pixel_off = slot_off + ctypes.sizeof(_FrameSlot)
        frame_mm.seek(pixel_off)
        frame_mm.write(pixel_bytes)

        # Increment write_index AFTER writing so the sidecar sees the
        # complete frame (write_index > last_read_idx signals a new frame).
        frame_hdr.write_index += 1
        frame_mm.flush()

        # ----------------------------------------------------------------
        # Telemetry → camsim_telemetry (seqlock / double-buffer protocol)
        #
        # Set write_slot = the slot we are about to write.
        # The sidecar reads from (1 - write_slot), i.e. the previous frame.
        # ----------------------------------------------------------------
        tel_hdr.write_slot ^= 1
        write_off = tel_base + tel_hdr.write_slot * tel_slot_size

        if fdm is not None:
            new_tel = _build_telemetry_jsbsim(fdm, seq, gimbal_az_deg)
        else:
            new_tel = _build_telemetry_simple(
                seq, elapsed, args.lat, args.lon,
                args.alt_ft * _FT_TO_M, gimbal_az_deg,
            )

        ctypes.memmove(
            ctypes.addressof(_TelemetryFrame.from_buffer(tel_mm, write_off)),
            ctypes.addressof(new_tel),
            tel_slot_size,
        )
        tel_mm.flush()

        seq          += 1
        next_frame_t += interval

        # ----------------------------------------------------------------
        # Periodic log
        # ----------------------------------------------------------------
        now = time.monotonic()
        if now - last_log_t >= 5.0:
            actual_fps = seq / elapsed if elapsed > 0 else 0
            print(
                f"[frame-gen] seq={seq}  fps≈{actual_fps:.1f}"
                f"  heading={new_tel.platform_heading_deg:.1f}°"
                f"  pitch={new_tel.platform_pitch_deg:.1f}°"
                f"  roll={new_tel.platform_roll_deg:.1f}°"
                f"  alt={new_tel.platform_alt_m_hae:.0f}m"
                f"  az={new_tel.sensor_rel_az_deg:.1f}°",
                flush=True,
            )
            last_log_t = now

    # Cleanup
    frame_mm.close()
    tel_mm.close()
    for name in ("camsim_frames", "camsim_telemetry"):
        try:
            posix_ipc.unlink_shared_memory("/" + name)
        except posix_ipc.ExistentialError:
            pass
    print("[frame-gen] Shutdown complete.", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(
        description="CamSim synthetic frame generator (macOS / no-UE5 dev mode)"
    )
    p.add_argument("--width",  type=int, default=1280,
                   help="Frame width in pixels (default: %(default)s)")
    p.add_argument("--height", type=int, default=720,
                   help="Frame height in pixels (default: %(default)s)")
    p.add_argument("--fps",    type=int, default=30,
                   help="Target frame rate (default: %(default)s)")
    # JSBSim flight dynamics arguments
    p.add_argument("--aircraft", type=str, default="c172p",
                   help="JSBSim aircraft model (default: %(default)s)")
    p.add_argument("--speed", type=float, default=100,
                   help="Initial calibrated airspeed in knots (default: %(default)s)")
    p.add_argument("--heading", type=float, default=0,
                   help="Initial true heading in degrees (default: %(default)s)")
    p.add_argument("--lat", type=float, default=36.5,
                   help="Initial latitude in degrees (default: %(default)s)")
    p.add_argument("--lon", type=float, default=-117.5,
                   help="Initial longitude in degrees (default: %(default)s)")
    p.add_argument("--alt-ft", type=float, default=5000,
                   help="Initial altitude in feet MSL (default: %(default)s)")
    p.add_argument("--bank-angle", type=float, default=25,
                   help="Target bank angle for orbit in degrees (default: %(default)s)")
    p.add_argument("--no-jsbsim", action="store_true",
                   help="Disable JSBSim; use simple synthetic telemetry")
    run(p.parse_args())


if __name__ == "__main__":
    main()
