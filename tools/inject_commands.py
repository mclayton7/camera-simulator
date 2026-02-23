#!/usr/bin/env python3
"""
Manual slew / position command injector for the CamSim simulator.

Sends UDP commands to the Unreal CommandReceiver (port 5005 by default).
All packet encoding mirrors the protocol defined in CommandReceiver.h.

Wire format:
    [magic u32 LE] [msg_type u8] [reserved u8] [payload_len u16 LE] [payload]
    magic = 0x43534D53  ('CSMS')

Usage examples:

    # Slew gimbal pan at +10 deg/s for 3 seconds
    python inject_commands.py slew-pan --rate 10 --duration 3

    # Set absolute gimbal position (pan=0, tilt=-45)
    python inject_commands.py gimbal-abs --pan 0 --tilt -45

    # Move aircraft to a new geodetic position
    python inject_commands.py set-position --lat 36.5 --lon -117.5 --alt 1500

    # Change heading
    python inject_commands.py set-heading --heading 90

    # Change speed
    python inject_commands.py set-speed --speed 120

    # Ping
    python inject_commands.py ping
"""

from __future__ import annotations

import argparse
import socket
import struct
import time


CMD_MAGIC = 0x43534D53  # 'CSMS'

MSG_TYPES = {
    "slew-pan":       0x01,
    "slew-tilt":      0x02,
    "slew-both":      0x03,
    "set-position":   0x04,
    "set-heading":    0x05,
    "set-speed":      0x06,
    "gimbal-abs":       0x07,
    "set-flight-state": 0x08,
    "ping":             0xFF,
}


def _encode_packet(msg_type: int, payload: bytes) -> bytes:
    header = struct.pack("<IBBh",
                         CMD_MAGIC,
                         msg_type & 0xFF,
                         0,  # reserved
                         len(payload))
    # Note: payload_len is uint16 LE but struct format uses signed 'h' —
    # repack properly:
    header = struct.pack("<IBBH", CMD_MAGIC, msg_type & 0xFF, 0, len(payload))
    return header + payload


def _send(sock: socket.socket, host: str, port: int, msg_type: int, payload: bytes):
    pkt = _encode_packet(msg_type, payload)
    sock.sendto(pkt, (host, port))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_slew_pan(args, sock):
    payload = struct.pack("<f", args.rate)
    _send(sock, args.host, args.port, MSG_TYPES["slew-pan"], payload)
    if args.duration > 0:
        time.sleep(args.duration)
        # Stop slew
        _send(sock, args.host, args.port, MSG_TYPES["slew-pan"], struct.pack("<f", 0.0))
        print(f"Slew pan: {args.rate:+.1f} deg/s for {args.duration:.1f} s → stopped")
    else:
        print(f"Slew pan: {args.rate:+.1f} deg/s (latched)")


def cmd_slew_tilt(args, sock):
    payload = struct.pack("<f", args.rate)
    _send(sock, args.host, args.port, MSG_TYPES["slew-tilt"], payload)
    if args.duration > 0:
        time.sleep(args.duration)
        _send(sock, args.host, args.port, MSG_TYPES["slew-tilt"], struct.pack("<f", 0.0))
        print(f"Slew tilt: {args.rate:+.1f} deg/s for {args.duration:.1f} s → stopped")
    else:
        print(f"Slew tilt: {args.rate:+.1f} deg/s (latched)")


def cmd_slew_both(args, sock):
    payload = struct.pack("<ff", args.pan_rate, args.tilt_rate)
    _send(sock, args.host, args.port, MSG_TYPES["slew-both"], payload)
    if args.duration > 0:
        time.sleep(args.duration)
        stop = struct.pack("<ff", 0.0, 0.0)
        _send(sock, args.host, args.port, MSG_TYPES["slew-both"], stop)
        print(f"Slew pan={args.pan_rate:+.1f} tilt={args.tilt_rate:+.1f} deg/s "
              f"for {args.duration:.1f} s → stopped")
    else:
        print(f"Slew pan={args.pan_rate:+.1f} tilt={args.tilt_rate:+.1f} deg/s (latched)")


def cmd_set_position(args, sock):
    payload = struct.pack("<ddf", args.lat, args.lon, args.alt)
    _send(sock, args.host, args.port, MSG_TYPES["set-position"], payload)
    print(f"SetPosition: lat={args.lat:.6f} lon={args.lon:.6f} alt={args.alt:.1f} m HAE")


def cmd_set_heading(args, sock):
    payload = struct.pack("<f", args.heading)
    _send(sock, args.host, args.port, MSG_TYPES["set-heading"], payload)
    print(f"SetHeading: {args.heading:.1f}°")


def cmd_set_speed(args, sock):
    payload = struct.pack("<f", args.speed)
    _send(sock, args.host, args.port, MSG_TYPES["set-speed"], payload)
    print(f"SetSpeed: {args.speed:.1f} kts")


def cmd_gimbal_abs(args, sock):
    payload = struct.pack("<ff", args.pan, args.tilt)
    _send(sock, args.host, args.port, MSG_TYPES["gimbal-abs"], payload)
    print(f"GimbalAbs: pan={args.pan:.1f}° tilt={args.tilt:.1f}°")


def cmd_set_flight_state(args, sock):
    payload = struct.pack("<ddffffff",
                          args.lat, args.lon, args.alt,
                          args.heading, args.pitch, args.roll, args.speed)
    _send(sock, args.host, args.port, MSG_TYPES["set-flight-state"], payload)
    print(f"SetFlightState: lat={args.lat:.6f} lon={args.lon:.6f} alt={args.alt:.1f}m"
          f"  hdg={args.heading:.1f}° pitch={args.pitch:.1f}° roll={args.roll:.1f}°"
          f"  speed={args.speed:.1f}kts")


def cmd_ping(args, sock):
    _send(sock, args.host, args.port, MSG_TYPES["ping"], b"")
    print("Ping sent")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CamSim command injector")
    p.add_argument("--host", default="127.0.0.1", help="Target host (default: %(default)s)")
    p.add_argument("--port", type=int, default=5005, help="Target UDP port (default: %(default)s)")

    sub = p.add_subparsers(dest="command", required=True)

    # slew-pan
    sp = sub.add_parser("slew-pan", help="Set gimbal pan slew rate")
    sp.add_argument("--rate", type=float, required=True, help="Pan rate deg/s (+right, -left)")
    sp.add_argument("--duration", type=float, default=0, help="Auto-stop after N seconds (0=latch)")

    # slew-tilt
    sp = sub.add_parser("slew-tilt", help="Set gimbal tilt slew rate")
    sp.add_argument("--rate", type=float, required=True, help="Tilt rate deg/s (+up, -down)")
    sp.add_argument("--duration", type=float, default=0, help="Auto-stop after N seconds")

    # slew-both
    sp = sub.add_parser("slew-both", help="Set both pan and tilt slew rates")
    sp.add_argument("--pan-rate",  type=float, default=0.0)
    sp.add_argument("--tilt-rate", type=float, default=0.0)
    sp.add_argument("--duration",  type=float, default=0)

    # set-position
    sp = sub.add_parser("set-position", help="Teleport aircraft to geodetic position")
    sp.add_argument("--lat", type=float, required=True, help="Latitude deg")
    sp.add_argument("--lon", type=float, required=True, help="Longitude deg")
    sp.add_argument("--alt", type=float, default=1500.0, help="Altitude HAE metres")

    # set-heading
    sp = sub.add_parser("set-heading", help="Set aircraft true heading")
    sp.add_argument("--heading", type=float, required=True, help="Heading degrees (0=N)")

    # set-speed
    sp = sub.add_parser("set-speed", help="Set aircraft airspeed")
    sp.add_argument("--speed", type=float, required=True, help="Speed in knots")

    # gimbal-abs
    sp = sub.add_parser("gimbal-abs", help="Set absolute gimbal position")
    sp.add_argument("--pan",  type=float, default=0.0, help="Pan angle degrees")
    sp.add_argument("--tilt", type=float, default=-45.0, help="Tilt angle degrees")

    # set-flight-state
    sp = sub.add_parser("set-flight-state",
                        help="Set full aircraft state (position + attitude + speed)")
    sp.add_argument("--lat", type=float, required=True, help="Latitude deg")
    sp.add_argument("--lon", type=float, required=True, help="Longitude deg")
    sp.add_argument("--alt", type=float, default=1500.0, help="Altitude HAE metres")
    sp.add_argument("--heading", type=float, default=0.0, help="Heading degrees")
    sp.add_argument("--pitch", type=float, default=0.0, help="Pitch degrees")
    sp.add_argument("--roll", type=float, default=0.0, help="Roll degrees")
    sp.add_argument("--speed", type=float, default=120.0, help="Airspeed knots")

    # ping
    sub.add_parser("ping", help="Send ping command")

    return p.parse_args(argv)


HANDLERS = {
    "slew-pan":     cmd_slew_pan,
    "slew-tilt":    cmd_slew_tilt,
    "slew-both":    cmd_slew_both,
    "set-position": cmd_set_position,
    "set-heading":  cmd_set_heading,
    "set-speed":    cmd_set_speed,
    "gimbal-abs":       cmd_gimbal_abs,
    "set-flight-state": cmd_set_flight_state,
    "ping":             cmd_ping,
}


def main():
    args = _parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        HANDLERS[args.command](args, sock)


if __name__ == "__main__":
    main()
