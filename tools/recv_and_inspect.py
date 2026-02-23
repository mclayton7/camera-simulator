#!/usr/bin/env python3
"""
UDP MPEG-TS receiver and inspector for CamSim streams.

Receives MPEG-TS packets from a UDP socket, counts TS packets per PID,
and decodes any KLV packets found in PID 0x0201.

Usage:
    python recv_and_inspect.py                         # defaults
    python recv_and_inspect.py --host 0.0.0.0 --port 5004
    python recv_and_inspect.py --multicast 239.1.1.1 --port 5004
    python recv_and_inspect.py --duration 10           # run for 10 s then exit

Requires: no third-party libraries (uses stdlib only for TS parsing).
KLV decoding requires tools/klv_decoder.py in the same or parent directory.
"""

from __future__ import annotations

import argparse
import collections
import ipaddress
import os
import socket
import struct
import sys
import time
from pathlib import Path

# Allow importing klv_decoder from same directory or parent tools/
_TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(_TOOLS_DIR))

try:
    from klv_decoder import decode_klv_packet, KlvDecodeError, UAS_LS_UNIVERSAL_KEY
    _KLV_AVAILABLE = True
except ImportError:
    _KLV_AVAILABLE = False


# ---------------------------------------------------------------------------
# MPEG-TS constants
# ---------------------------------------------------------------------------

TS_PACKET_SIZE  = 188
TS_SYNC_BYTE    = 0x47
TS_NULL_PID     = 0x1FFF


# ---------------------------------------------------------------------------
# TS packet parser
# ---------------------------------------------------------------------------

def parse_ts_packet(pkt: bytes) -> dict | None:
    """Parse a 188-byte TS packet. Returns None on error."""
    if len(pkt) < TS_PACKET_SIZE or pkt[0] != TS_SYNC_BYTE:
        return None

    transport_error  = (pkt[1] >> 7) & 1
    payload_start    = (pkt[1] >> 6) & 1
    pid              = ((pkt[1] & 0x1F) << 8) | pkt[2]
    adaptation_ctrl  = (pkt[3] >> 4) & 0x3
    continuity_ctr   = pkt[3] & 0xF

    adaptation_size = 0
    if adaptation_ctrl in (2, 3):
        adaptation_size = pkt[4] + 1  # 1 for the length byte itself

    payload_offset = 4 + adaptation_size
    has_payload = adaptation_ctrl in (1, 3)
    payload = pkt[payload_offset:] if has_payload else b""

    return {
        "transport_error": transport_error,
        "payload_unit_start": payload_start,
        "pid": pid,
        "adaptation_ctrl": adaptation_ctrl,
        "continuity_ctr": continuity_ctr,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# KLV reassembler (PES-based, simplified)
# ---------------------------------------------------------------------------

class KlvReassembler:
    """
    Collects payload bytes from TS packets on the KLV PID and reassembles
    KLV packets (which may span multiple TS packets).

    Simplified: assumes each PES packet = one KLV packet and that
    payload_unit_start_indicator marks the beginning of a new KLV packet.
    """

    def __init__(self):
        self._buf: bytearray = bytearray()
        self._in_pes: bool = False

    def feed(self, ts: dict) -> bytes | None:
        """
        Feed a parsed TS packet.  Returns a complete KLV bytes object if a
        packet boundary is detected, else None.
        """
        payload = ts["payload"]
        if not payload:
            return None

        completed = None

        if ts["payload_unit_start"]:
            # A new PES packet starts; flush what we have
            if self._buf and self._in_pes:
                completed = self._extract_klv(bytes(self._buf))
            self._buf.clear()
            self._in_pes = True

            # Skip PES header (stream_id, packet_length, flags, header_data_length)
            # PES header: starts 0x000001 + stream_id(1) + length(2) + flags(2) + hdr_len(1)
            if len(payload) >= 9 and payload[0] == 0 and payload[1] == 0 and payload[2] == 1:
                pes_header_len = 9 + payload[8]  # 9 fixed + optional
                self._buf += payload[pes_header_len:]
            else:
                self._buf += payload
        elif self._in_pes:
            self._buf += payload

        return completed

    def _extract_klv(self, raw: bytes) -> bytes | None:
        """Return raw if it starts with the UAS LS Universal Key, else None."""
        if _KLV_AVAILABLE and raw[:16] == UAS_LS_UNIVERSAL_KEY:
            return raw
        elif not _KLV_AVAILABLE and len(raw) >= 16:
            return raw
        return None


# ---------------------------------------------------------------------------
# Main inspector
# ---------------------------------------------------------------------------

def main():
    args = _parse_args()

    sock = _open_socket(args)
    print(f"Listening on {args.host}:{args.port} …")
    if args.multicast:
        print(f"Joined multicast group {args.multicast}")
    print()

    pid_counts: dict[int, int] = collections.defaultdict(int)
    pid_continuity: dict[int, int] = {}
    klv_reassembler = KlvReassembler()
    klv_count = 0
    ts_count  = 0
    error_count = 0
    start_time  = time.monotonic()
    last_stat   = start_time
    stat_interval = 2.0

    try:
        while True:
            now = time.monotonic()
            elapsed = now - start_time

            if args.duration > 0 and elapsed >= args.duration:
                break

            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue

            # Parse TS packets from UDP payload
            offset = 0
            while offset + TS_PACKET_SIZE <= len(data):
                raw_pkt = data[offset: offset + TS_PACKET_SIZE]
                offset += TS_PACKET_SIZE
                ts_count += 1

                ts = parse_ts_packet(raw_pkt)
                if ts is None:
                    error_count += 1
                    continue

                pid = ts["pid"]
                if ts["transport_error"]:
                    error_count += 1

                pid_counts[pid] += 1

                # Continuity counter check
                if pid not in (TS_NULL_PID,):
                    expected = (pid_continuity.get(pid, ts["continuity_ctr"] - 1) + 1) % 16
                    if expected != ts["continuity_ctr"]:
                        error_count += 1
                    pid_continuity[pid] = ts["continuity_ctr"]

                # KLV extraction
                if pid == args.klv_pid:
                    klv_bytes = klv_reassembler.feed(ts)
                    if klv_bytes and _KLV_AVAILABLE:
                        klv_count += 1
                        if not args.quiet:
                            try:
                                tags = decode_klv_packet(klv_bytes)
                                _print_klv(tags, klv_count)
                            except KlvDecodeError as exc:
                                print(f"  [KLV parse error: {exc}]")

            # Periodic statistics
            if now - last_stat >= stat_interval:
                dt = now - last_stat
                fps = pid_counts.get(args.video_pid, 0) / dt if dt > 0 else 0
                print(
                    f"[{elapsed:6.1f}s] TS pkts: {ts_count:7d}  "
                    f"Video PID {args.video_pid:#06x}: {fps:.1f} fps  "
                    f"KLV pkts: {klv_count:5d}  "
                    f"Errors: {error_count}"
                )
                _print_pid_table(pid_counts)
                # Reset per-interval counters
                pid_counts.clear()
                last_stat = now

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        sock.close()
        print(f"\nTotal TS packets: {ts_count}, KLV packets decoded: {klv_count}, Errors: {error_count}")


def _print_pid_table(pid_counts: dict[int, int]):
    if not pid_counts:
        return
    print("  PIDs seen:")
    for pid, count in sorted(pid_counts.items()):
        label = {0x0000: "PAT", 0x0001: "CAT", 0x0011: "SDT",
                 0x0100: "Video (H.264)", 0x0201: "KLV Metadata",
                 0x1FFF: "Null"}.get(pid, "")
        print(f"    PID {pid:#06x}  {count:6d} pkts  {label}")


def _print_klv(tags: dict, count: int):
    ts_us = tags.get(2)
    lat   = tags.get(13)
    lon   = tags.get(14)
    az    = tags.get(18)
    el    = tags.get(19)
    sr    = tags.get(21)
    print(f"  KLV#{count:5d}  ts={ts_us}µs  "
          f"sensor=({lat:.4f}°, {lon:.4f}°)  "
          f"az={az:.1f}°  el={el:.1f}°  slant={sr:.0f}m")


def _open_socket(args) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.settimeout(1.0)
    sock.bind((args.host, args.port))

    if args.multicast:
        group = socket.inet_aton(args.multicast)
        mreq  = group + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    return sock


def _parse_args():
    p = argparse.ArgumentParser(description="CamSim MPEG-TS inspector")
    p.add_argument("--host",      default="0.0.0.0",   help="Bind address")
    p.add_argument("--port",      type=int, default=5004, help="UDP port")
    p.add_argument("--multicast", default=None,
                   help="Join multicast group (e.g. 239.1.1.1)")
    p.add_argument("--video-pid", type=lambda x: int(x, 0), default=0x0100)
    p.add_argument("--klv-pid",   type=lambda x: int(x, 0), default=0x0201)
    p.add_argument("--duration",  type=float, default=0,
                   help="Stop after N seconds (0 = run forever)")
    p.add_argument("--quiet",     action="store_true",
                   help="Suppress per-packet KLV output (show stats only)")
    return p.parse_args()


if __name__ == "__main__":
    main()
