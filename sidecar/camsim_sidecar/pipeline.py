"""
GStreamer pipeline builder for CamSim sidecar.

Pipeline graph:

  framesrc (appsrc, BGRA)
    → videoconvert → I420
    → nvh264enc (or x264enc fallback)
    → h264parse
    → mux (mpegtsmux)

  klvsrc (appsrc, meta/x-klv)
    → mux

  mux → udpsink

PIDs: video 0x0100, KLV 0x0201 (stream_type 0x15 = data stream).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


def _try_nvh264() -> bool:
    """Return True if the nvh264enc GStreamer plugin is available."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore
        Gst.init(None)
        factory = Gst.ElementFactory.find("nvh264enc")
        return factory is not None
    except Exception:
        return False


class CamSimPipeline:
    """
    Manages the GStreamer pipeline lifecycle.

    Usage:
        pipeline = CamSimPipeline(width=1920, height=1080, fps=30,
                                   host="239.1.1.1", port=5004)
        pipeline.start()
        pipeline.push_frame(bgra_bytes, pts_ns)
        pipeline.push_klv(klv_bytes, pts_ns)
        pipeline.stop()
    """

    def __init__(
        self,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        host: str = "239.1.1.1",
        port: int = 5004,
        multicast: bool = True,
        bitrate_kbps: int = 4000,
        force_software: bool = False,
        video_pid: int = 0x0100,
        klv_pid: int = 0x0201,
    ):
        self.width          = width
        self.height         = height
        self.fps            = fps
        self.host           = host
        self.port           = port
        self.multicast      = multicast
        self.bitrate_kbps   = bitrate_kbps
        self.force_software = force_software
        self.video_pid      = video_pid
        self.klv_pid        = klv_pid

        self._pipeline = None
        self._framesrc = None
        self._klvsrc   = None
        self._use_nvenc: bool = False

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, GLib  # type: ignore

        Gst.init(None)

        self._use_nvenc = (not self.force_software) and _try_nvh264()
        log.info("CamSim pipeline: using %s encoder",
                 "nvh264enc (NVENC)" if self._use_nvenc else "x264enc (software)")

        pipeline_str = self._build_pipeline_string()
        log.debug("GStreamer pipeline:\n%s", pipeline_str)

        self._pipeline = Gst.parse_launch(pipeline_str)
        self._framesrc = self._pipeline.get_by_name("framesrc")
        self._klvsrc   = self._pipeline.get_by_name("klvsrc")

        # Disable GStreamer's automatic timestamping — we supply PTS
        self._framesrc.set_property("do-timestamp", False)
        self._klvsrc.set_property("do-timestamp", False)
        self._framesrc.set_property("format", 3)  # GST_FORMAT_TIME
        self._klvsrc.set_property("format", 3)

        self._pipeline.set_state(Gst.State.PLAYING)
        log.info("CamSim pipeline started → udp://%s:%d", self.host, self.port)

    def stop(self):
        if self._pipeline:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # type: ignore
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._framesrc = None
            self._klvsrc   = None
            log.info("CamSim pipeline stopped")

    # -----------------------------------------------------------------------
    # Push buffers
    # -----------------------------------------------------------------------

    def push_frame(self, bgra_bytes: bytes, pts_ns: int):
        """Push a BGRA video frame into the pipeline."""
        if not self._framesrc:
            return
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore

        buf = Gst.Buffer.new_wrapped(bgra_bytes)
        buf.pts      = pts_ns
        buf.duration = Gst.util_uint64_scale_int(Gst.SECOND, 1, self.fps)

        self._framesrc.emit("push-buffer", buf)

    def push_klv(self, klv_bytes: bytes, pts_ns: int):
        """Push a KLV packet into the metadata stream."""
        if not self._klvsrc:
            return
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore

        buf = Gst.Buffer.new_wrapped(klv_bytes)
        buf.pts = pts_ns

        self._klvsrc.emit("push-buffer", buf)

    # -----------------------------------------------------------------------
    # Pipeline string
    # -----------------------------------------------------------------------

    def _build_pipeline_string(self) -> str:
        w, h, fps     = self.width, self.height, self.fps
        br            = self.bitrate_kbps
        vpid          = self.video_pid
        kpid          = self.klv_pid
        size          = w * h * 4  # BGRA bytes per frame

        frame_caps = (
            f"video/x-raw,format=BGRA,width={w},height={h},"
            f"framerate={fps}/1"
        )

        if self._use_nvenc:
            encoder = (
                f"nvh264enc bitrate={br} gop-size={fps} rc-mode=cbr "
                f"preset=low-latency-hq"
            )
        else:
            encoder = (
                f"x264enc bitrate={br} key-int-max={fps} "
                f"tune=zerolatency speed-preset=ultrafast"
            )

        udp_props = f'host="{self.host}" port={self.port}'
        if self.multicast:
            udp_props += " auto-multicast=true"

        return (
            f'appsrc name=framesrc '
            f'  caps="{frame_caps}" '
            f'  blocksize={size} '
            f'  max-bytes={size * 2} '
            f'! videoconvert '
            f'! video/x-raw,format=I420 '
            f'! {encoder} '
            f'! h264parse '
            f'! mux. '
            f''
            f'appsrc name=klvsrc '
            f'  caps="meta/x-klv,parsed=true" '
            f'  max-bytes=65536 '
            f'! mux. '
            f''
            f'mpegtsmux name=mux '
            f'  alignment=7 '
            f'  pat-interval=100000000 '
            f'  si-interval=100000000 '
            f'! udpsink {udp_props} '
            f'  sync=false '
            f'  async=false'
        )

    # -----------------------------------------------------------------------
    # PID assignment (done via pad properties after construction)
    # -----------------------------------------------------------------------

    def configure_pids(self):
        """
        Set video PID to 0x0100 and KLV PID to 0x0201 on the mpegtsmux pads.
        Must be called after start() while pipeline is in PLAYING state.

        Waits briefly for pad caps to be negotiated, then sets PIDs if the
        GStreamer version supports per-pad pid/stream-type properties (>= 1.22).
        """
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore

        if not self._pipeline:
            return

        gst_ver = Gst.version()
        gst_ver_tuple = (gst_ver.major, gst_ver.minor)
        log.info("GStreamer version: %d.%d.%d.%d", *gst_ver)

        if gst_ver_tuple < (1, 22):
            log.warning(
                "GStreamer %d.%d < 1.22 — per-pad PID properties not supported; "
                "PIDs will be auto-assigned by mpegtsmux",
                *gst_ver_tuple,
            )
            return

        mux = self._pipeline.get_by_name("mux")
        if not mux:
            return

        # Wait for pad caps to negotiate (up to 2 s)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pads_with_caps = sum(
                1 for pad in mux.pads if pad.get_current_caps() is not None
            )
            # We expect at least 2 sink pads (video + klv) with caps
            if pads_with_caps >= 2:
                break
            time.sleep(0.05)
        else:
            log.warning(
                "Timed out waiting for mpegtsmux pad caps; "
                "only %d pad(s) have caps — PID assignment may be incomplete",
                pads_with_caps,
            )

        for pad in mux.pads:
            caps = pad.get_current_caps()
            if not caps:
                continue
            struct = caps.get_structure(0)
            mime = struct.get_name() if struct else ""

            try:
                if "video" in mime or "x-h264" in mime:
                    pad.set_property("pid", self.video_pid)
                    log.info("Video pad PID set to 0x%04x", self.video_pid)
                elif "klv" in mime:
                    pad.set_property("pid", self.klv_pid)
                    pad.set_property("stream-type", 0x15)
                    log.info("KLV pad PID set to 0x%04x (stream-type 0x15)", self.klv_pid)
            except TypeError as exc:
                log.warning("Could not configure mpegtsmux pad PID (%s): %s",
                            mime, exc)
