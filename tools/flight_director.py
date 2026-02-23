#!/usr/bin/env python3
"""
tools/flight_director.py — JSBSim flight director for CamSim.

Runs a JSBSim 6-DOF flight dynamics model (banked surveillance orbit) and
sends aircraft state to the Unreal Engine CamSimPlugin via the SetFlightState
(0x08) UDP command at a configurable rate (default 30 Hz).

This makes UE5 a "dumb renderer" — the flight director owns position,
heading, pitch, roll, and speed.  No shared memory or GStreamer dependency.

Usage:
    python tools/flight_director.py --host 127.0.0.1 --port 5005

    # Custom orbit parameters
    python tools/flight_director.py --speed 120 --bank-angle 30 --alt-ft 8000

Dependencies:
    pip install jsbsim
"""

from __future__ import annotations

import argparse
import math
import signal
import socket
import struct
import time


CMD_MAGIC = 0x43534D53  # 'CSMS'
CMD_SET_FLIGHT_STATE = 0x08

_FT_TO_M    = 0.3048
_RAD_TO_DEG = 180.0 / math.pi


# ---------------------------------------------------------------------------
# JSBSim initialisation and orbit controller
# (Same logic as frame_gen.py — kept standalone so this script has zero
#  dependency on the sidecar / shared-memory code.)
# ---------------------------------------------------------------------------

def _init_jsbsim(aircraft: str, lat: float, lon: float, alt_ft: float,
                 heading: float, speed_kts: float) -> "jsbsim.FGFDMExec":
    """Initialize JSBSim FDM and trim for steady level flight."""
    import jsbsim

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
        print("[flight-director] JSBSim trim succeeded", flush=True)
    except RuntimeError:
        print("[flight-director] JSBSim trim failed — using raw IC state",
              flush=True)

    return fdm


def _update_orbit_controller(fdm: "jsbsim.FGFDMExec",
                             target_bank_deg: float,
                             target_alt_ft: float,
                             target_speed_kts: float) -> None:
    """Simple proportional controller for a banked surveillance orbit."""
    bank_deg = fdm["attitude/phi-rad"] * _RAD_TO_DEG
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


# ---------------------------------------------------------------------------
# UDP packet builder
# ---------------------------------------------------------------------------

def _build_packet(lat: float, lon: float, alt_m: float,
                  heading: float, pitch: float, roll: float,
                  speed_kts: float) -> bytes:
    """Build a SetFlightState (0x08) UDP command packet."""
    payload = struct.pack("<ddffffff",
                          lat, lon, alt_m, heading, pitch, roll, speed_kts)
    header = struct.pack("<IBBH",
                         CMD_MAGIC, CMD_SET_FLIGHT_STATE, 0, len(payload))
    return header + payload


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    fdm = _init_jsbsim(
        aircraft=args.aircraft,
        lat=args.lat,
        lon=args.lon,
        alt_ft=args.alt_ft,
        heading=args.heading,
        speed_kts=args.speed,
    )

    steps_per_tick = math.ceil(120.0 / args.rate)
    interval = 1.0 / args.rate

    print(
        f"[flight-director] {args.aircraft} at {args.lat:.4f}, {args.lon:.4f}"
        f"  alt={args.alt_ft} ft  speed={args.speed} kts"
        f"  bank={args.bank_angle}°  rate={args.rate} Hz"
        f"  → {args.host}:{args.port}"
        f"  ({steps_per_tick} physics steps/tick)",
        flush=True,
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)

    running = True

    def _stop(sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    tick_count = 0
    next_tick = time.monotonic()
    last_log = time.monotonic()

    while running:
        # Sleep until next tick
        sleep_t = next_tick - time.monotonic()
        if sleep_t > 0:
            time.sleep(sleep_t)

        # Step JSBSim physics
        for _ in range(steps_per_tick):
            _update_orbit_controller(
                fdm, args.bank_angle, args.alt_ft, args.speed,
            )
            fdm.run()

        # Read current state
        lat = fdm["position/lat-geod-deg"]
        lon = fdm["position/long-gc-deg"]
        alt_m = fdm["position/h-sl-ft"] * _FT_TO_M
        heading = fdm["attitude/psi-true-rad"] * _RAD_TO_DEG % 360.0
        pitch = fdm["attitude/theta-rad"] * _RAD_TO_DEG
        roll = fdm["attitude/phi-rad"] * _RAD_TO_DEG
        speed_kts = fdm["velocities/vc-kts"]

        # Send SetFlightState packet
        pkt = _build_packet(lat, lon, alt_m, heading, pitch, roll, speed_kts)
        sock.sendto(pkt, dest)

        tick_count += 1
        next_tick += interval

        # Periodic log (every 5 seconds)
        now = time.monotonic()
        if now - last_log >= 5.0:
            print(
                f"[flight-director] tick={tick_count}"
                f"  lat={lat:.6f} lon={lon:.6f}"
                f"  alt={alt_m:.0f}m  hdg={heading:.1f}°"
                f"  pitch={pitch:.1f}° roll={roll:.1f}°"
                f"  spd={speed_kts:.1f}kts",
                flush=True,
            )
            last_log = now

    sock.close()
    print("[flight-director] Shutdown complete.", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(
        description="CamSim JSBSim flight director — sends SetFlightState "
                    "packets to UE5 CommandReceiver"
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="UE5 command receiver host (default: %(default)s)")
    p.add_argument("--port", type=int, default=5005,
                   help="UE5 command receiver UDP port (default: %(default)s)")
    p.add_argument("--rate", type=int, default=30,
                   help="State update rate in Hz (default: %(default)s)")
    p.add_argument("--aircraft", default="c172p",
                   help="JSBSim aircraft model (default: %(default)s)")
    p.add_argument("--speed", type=float, default=100,
                   help="Target airspeed in knots (default: %(default)s)")
    p.add_argument("--heading", type=float, default=0,
                   help="Initial true heading in degrees (default: %(default)s)")
    p.add_argument("--lat", type=float, default=36.5,
                   help="Initial latitude in degrees (default: %(default)s)")
    p.add_argument("--lon", type=float, default=-117.5,
                   help="Initial longitude in degrees (default: %(default)s)")
    p.add_argument("--alt-ft", type=float, default=5000,
                   help="Target altitude in feet MSL (default: %(default)s)")
    p.add_argument("--bank-angle", type=float, default=25,
                   help="Target bank angle for orbit in degrees (default: %(default)s)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
