"""
CamSim sidecar — entry point.

Reads BGRA frames and telemetry from POSIX shared memory written by the
Unreal Engine CamSim plugin, encodes them as H.264 + MISB ST 0601 KLV,
and transmits a standards-compliant MPEG-TS stream over UDP.

Usage:
    python -m camsim_sidecar [options]
    python -m camsim_sidecar --help
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .klv_encoder import encode_klv_packet, TelemetryData
from .pipeline import CamSimPipeline
from .shm_reader import FrameShmReader, TelemetryShmReader


log = logging.getLogger("camsim_sidecar")


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CamSim sidecar: SHM → GStreamer → MPEG-TS/UDP"
    )
    p.add_argument("--host",       default="239.1.1.1",
                   help="UDP destination host (default: %(default)s)")
    p.add_argument("--port",       type=int, default=5004,
                   help="UDP destination port (default: %(default)s)")
    p.add_argument("--no-multicast", dest="multicast", action="store_false",
                   help="Disable multicast (use unicast)")
    p.add_argument("--width",      type=int, default=1920,
                   help="Expected frame width (default: %(default)s)")
    p.add_argument("--height",     type=int, default=1080,
                   help="Expected frame height (default: %(default)s)")
    p.add_argument("--fps",        type=int, default=30,
                   help="Frame rate (default: %(default)s)")
    p.add_argument("--bitrate",    type=int, default=4000,
                   help="H.264 bitrate in kbps (default: %(default)s)")
    p.add_argument("--software",   action="store_true",
                   help="Force x264enc (skip NVENC even if available)")
    p.add_argument("--video-pid",  type=lambda x: int(x, 0), default=0x0100,
                   help="MPEG-TS PID for video (default: 0x0100)")
    p.add_argument("--klv-pid",    type=lambda x: int(x, 0), default=0x0201,
                   help="MPEG-TS PID for KLV (default: 0x0201)")
    p.add_argument("--frame-shm",  default="camsim_frames",
                   help="Frame shared memory name (default: %(default)s)")
    p.add_argument("--tel-shm",    default="camsim_telemetry",
                   help="Telemetry shared memory name (default: %(default)s)")
    p.add_argument("--wait-shm",   type=float, default=30.0,
                   help="Seconds to wait for shm to appear (default: %(default)s)")
    p.add_argument("--log-level",  default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log level (default: %(default)s)")
    return p.parse_args(argv)


def _wait_for_shm(shm_name: str, timeout_s: float):
    """Block until the POSIX shm region appears (created by UE plugin)."""
    import posix_ipc  # type: ignore
    deadline = time.monotonic() + timeout_s
    log.info("Waiting up to %.0f s for shm /%s ...", timeout_s, shm_name)
    while time.monotonic() < deadline:
        try:
            m = posix_ipc.SharedMemory(f"/{shm_name}", flags=0)
            m.close_fd()
            log.info("Found shm /%s", shm_name)
            return
        except posix_ipc.ExistentialError:
            time.sleep(0.25)
    raise TimeoutError(f"Shared memory /{shm_name} did not appear within {timeout_s} s")


def run(args: argparse.Namespace):
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # ------------------------------------------------------------------
    # Wait for Unreal to start and create shm regions
    # ------------------------------------------------------------------
    _wait_for_shm(args.frame_shm, args.wait_shm)
    _wait_for_shm(args.tel_shm,   args.wait_shm)

    # ------------------------------------------------------------------
    # Build GStreamer pipeline
    # ------------------------------------------------------------------
    pipeline = CamSimPipeline(
        width=args.width,
        height=args.height,
        fps=args.fps,
        host=args.host,
        port=args.port,
        multicast=args.multicast,
        bitrate_kbps=args.bitrate,
        force_software=args.software,
        video_pid=args.video_pid,
        klv_pid=args.klv_pid,
    )
    pipeline.start()
    pipeline.configure_pids()

    # ------------------------------------------------------------------
    # Open shared memory
    # ------------------------------------------------------------------
    frame_reader = FrameShmReader(args.frame_shm)
    tel_reader   = TelemetryShmReader(args.tel_shm)
    frame_reader.open()
    tel_reader.open()

    # ------------------------------------------------------------------
    # Graceful shutdown on SIGINT / SIGTERM
    # ------------------------------------------------------------------
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        log.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    frame_count   = 0
    last_stat_t   = time.monotonic()

    log.info("CamSim sidecar running — sending to udp://%s:%d", args.host, args.port)

    try:
        while running:
            result = frame_reader.read_next_frame()
            if result is None:
                time.sleep(0.001)
                continue

            pixels, ts_us, width, height, seq = result
            pts_ns = ts_us * 1000  # µs → ns

            # Push video frame
            pipeline.push_frame(pixels, pts_ns)

            # Read telemetry (best-effort; reuse last if not fresh)
            tel = tel_reader.read()
            if tel is not None:
                klv_bytes = encode_klv_packet(tel)
                pipeline.push_klv(klv_bytes, pts_ns)

            frame_count += 1

            # Periodic stats
            now = time.monotonic()
            if now - last_stat_t >= 5.0:
                fps_actual = frame_count / (now - last_stat_t)
                log.info("Running: %.1f fps, frame seq=%d", fps_actual, seq)
                frame_count  = 0
                last_stat_t  = now

    finally:
        frame_reader.close()
        tel_reader.close()
        pipeline.stop()
        log.info("CamSim sidecar exited cleanly")


def main():
    run(_parse_args())


if __name__ == "__main__":
    main()
