"""FFmpeg/DirectShow backend for BougtCam.

Responsible for:
- locating an ffmpeg executable (bundled next to the exe, or on PATH)
- enumerating DirectShow video/audio devices
- enumerating the capture modes a device supports
- choosing the best mode (max pixels, then fps)
- building the record command line
- remuxing a finished MKV to MP4 (stream copy, no re-encode)

Everything here is side-effect free except for the subprocess calls;
the Recorder engine owns process lifecycle.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("bougtcam.ffmpeg")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _hidden_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg.exe: bundled dir first, then PATH."""
    candidates = []
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        candidates += [
            base / "ffmpeg" / "ffmpeg.exe",
            base / "ffmpeg.exe",
            base / "_internal" / "ffmpeg" / "ffmpeg.exe",
        ]
        # PyInstaller extraction dir (_internal for onedir)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates += [
                Path(meipass) / "ffmpeg" / "ffmpeg.exe",
                Path(meipass) / "ffmpeg.exe",
            ]
    else:
        base = Path(__file__).resolve().parent.parent
        candidates += [base / "ffmpeg" / "ffmpeg.exe", base / "ffmpeg.exe"]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("ffmpeg")


def run_ffmpeg(ffmpeg: str, args: List[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Run ffmpeg and capture combined stderr output (ffmpeg logs to stderr)."""
    return subprocess.run(
        [ffmpeg] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        **_hidden_kwargs(),
    )


def list_video_devices(ffmpeg: str) -> List[str]:
    """Return DirectShow video device names."""
    try:
        proc = run_ffmpeg(ffmpeg, ["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("device enumeration failed: %s", e)
        return []
    out = proc.stderr.decode("utf-8", errors="replace")
    return _parse_device_list(out, want="video")


def list_audio_devices(ffmpeg: str) -> List[str]:
    """Return DirectShow audio device names."""
    try:
        proc = run_ffmpeg(ffmpeg, ["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("audio device enumeration failed: %s", e)
        return []
    out = proc.stderr.decode("utf-8", errors="replace")
    return _parse_device_list(out, want="audio")


_DEVICE_LINE_RE = re.compile(r'"(.+)"\s*(?:\((?P<kind>\w+)\))?\s*$')


def _parse_device_list(out: str, want: str) -> List[str]:
    """Parse -list_devices output for ffmpeg >= 6 style output.

    ffmpeg 9.x drops the "DirectShow video/audio devices" section headers
    and tags each device inline as (video), (audio) or (none). Older
    versions group devices under the section headers; support both.
    """
    devices = []
    in_section = False
    saw_tags = False
    for line in out.splitlines():
        if "DirectShow video devices" in line:
            in_section = want == "video"
            continue
        if "DirectShow audio devices" in line:
            in_section = want == "audio"
            continue
        m = _DEVICE_LINE_RE.search(line.strip())
        if not m:
            continue
        name, kind = m.group(1), m.group("kind")
        if name.startswith("@device"):
            continue
        if kind is not None:  # inline tag format (ffmpeg 9.x)
            saw_tags = True
            if kind == want:
                devices.append(name)
        elif in_section and not saw_tags:
            devices.append(name)
    return devices


@dataclass(frozen=True)
class CaptureMode:
    width: int
    height: int
    fps: float
    pixel_format: str = ""          # e.g. mjpeg, yuyv422, nv12 (may be "")
    compression: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height


# Lines look like:
#   pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30
#   vcodec=mjpeg  min s=1280x720 fps=10 max s=3840x2160 fps=30
_MODE_RE = re.compile(
    r'(?:pixel_format=(?P<pf>\w+)|vcodec=(?P<vc>\w+))?\s*'
    r'min s=(?P<minw>\d+)x(?P<minh>\d+) fps=(?P<minf>[\d.]+)\s+'
    r'max s=(?P<maxw>\d+)x(?P<maxh>\d+) fps=(?P<maxf>[\d.]+)'
)


def list_capture_modes(ffmpeg: str, device_name: str, timeout: float = 25.0) -> List[CaptureMode]:
    """Enumerate capture modes the device advertises via -list_options."""
    try:
        proc = run_ffmpeg(
            ffmpeg,
            ["-f", "dshow", "-list_options", "true", "-i", f"video={device_name}"],
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("mode enumeration failed for %r: %s", device_name, e)
        return []
    out = proc.stderr.decode("utf-8", errors="replace")
    modes: List[CaptureMode] = []
    for line in out.splitlines():
        m = _MODE_RE.search(line)
        if not m:
            continue
        pf = m.group("pf") or ""
        vc = m.group("vc") or ""
        # Use the max advertised size/fps for this range line.
        modes.append(
            CaptureMode(
                width=int(m.group("maxw")),
                height=int(m.group("maxh")),
                fps=float(m.group("maxf")),
                pixel_format=vc or pf,
            )
        )
    return modes


# Preferred pixel formats / codecs for stable long recording.
# mjpeg is cheap on the camera side and reliable; raw yuyv422/nv12
# cost USB bandwidth at high res but avoid decode issues.
_FORMAT_PREFERENCE = {"mjpeg": 0, "h264": 1, "nv12": 2, "yuyv422": 3, "yuv420p": 4, "rgb24": 5, "": 6}


def choose_best_mode(modes: List[CaptureMode]) -> Optional[CaptureMode]:
    """Pick max pixels, then highest fps, then preferred pixel format."""
    if not modes:
        return None
    return min(
        modes,
        key=lambda m: (
            -m.pixels,
            -m.fps,
            _FORMAT_PREFERENCE.get(m.pixel_format.lower(), 5),
        ),
    )


def fallback_modes(modes: List[CaptureMode]) -> List[CaptureMode]:
    """All modes sorted best-first for sequential fallback attempts."""
    return sorted(
        modes,
        key=lambda m: (
            -m.pixels,
            -m.fps,
            _FORMAT_PREFERENCE.get(m.pixel_format.lower(), 5),
        ),
    )


def build_record_command(
    ffmpeg: str,
    device_name: str,
    mode: CaptureMode,
    output_path: Path,
    audio_device: Optional[str] = None,
    prefer_hw: bool = True,
) -> List[str]:
    """Build the ffmpeg capture command writing to a fault-tolerant MKV."""
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-stats_period", "5",
            # machine-readable progress on stdout (independent of -loglevel);
            # used as a liveness signal when low-bitrate output makes the
            # muxer flush the file only in rare bursts
            "-progress", "pipe:1"]
    args += [
        "-f", "dshow",
        "-video_size", f"{mode.width}x{mode.height}",
        "-framerate", f"{mode.fps:g}",
    ]
    pf = mode.pixel_format.lower()
    if pf == "mjpeg":
        args += ["-vcodec", "mjpeg"]
    elif pf:
        args += ["-pixel_format", pf]
    if audio_device:
        args += ["-i", f"audio={audio_device}:video={device_name}"]
    else:
        args += ["-i", f"video={device_name}"]
    # Video encoder: keep source resolution/framerate, good quality.
    args += ["-map", "0:v"]
    if audio_device:
        args += ["-map", "1:a?"]
    if prefer_hw:
        # h264_amf/qsv/nvenc tried by caller via encoder fallback chain;
        # default command uses libx264 unless caller overrides.
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    if audio_device:
        args += ["-c:a", "aac", "-b:a", "128k"]
    # Timestamp integrity
    args += ["-use_wallclock_as_timestamps", "1", "-fflags", "+genpts"]
    args += ["-f", "matroska", str(output_path)]
    return args


HW_ENCODERS = [
    ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "22"]),
    ("h264_qsv", ["-c:v", "h264_qsv", "-global_quality", "22"]),
    ("h264_amf", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"]),
]


# Encoder option keys emitted by build_record_command / HW_ENCODERS, each
# followed by one value token.
_ENCODER_OPT_KEYS = {
    "-preset", "-crf", "-tune", "-profile:v", "-global_quality",
    "-cq", "-quality", "-rc", "-qp_i", "-qp_p", "-b:v", "-maxrate",
}


def with_encoder(args: List[str], encoder_args: List[str]) -> List[str]:
    """Replace the -c:v ... codec block in args with encoder_args."""
    out = list(args)
    try:
        i = out.index("-c:v")
    except ValueError:
        return out
    # remove -c:v <name> plus any following known encoder option pairs
    j = i + 2
    while j + 1 < len(out) and out[j] in _ENCODER_OPT_KEYS:
        j += 2
    del out[i:j]
    out[i:i] = encoder_args
    return out


def remux_to_mp4(ffmpeg: str, src: Path, dst: Path, timeout: float = 3600.0) -> bool:
    """Remux MKV -> MP4 without re-encoding. Returns True on success.

    Caller keeps src on failure.
    """
    args = [
        "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        proc = run_ffmpeg(ffmpeg, args, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("remux failed to run: %s", e)
        return False
    if proc.returncode != 0:
        log.error("remux failed rc=%s: %s", proc.returncode,
                  proc.stderr.decode("utf-8", errors="replace")[-2000:])
        return False
    return dst.exists() and dst.stat().st_size > 0
