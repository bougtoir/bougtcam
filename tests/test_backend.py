"""Unit tests for pure-logic parts (run on any OS)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bougtcam.ffmpeg_backend import (
    CaptureMode, choose_best_mode, fallback_modes, _MODE_RE,
    build_record_command, with_encoder, HW_ENCODERS,
)
from bougtcam.recorder import unique_output_path


def test_mode_regex_parses_list_options():
    line = '  pixel_format=yuyv422  min s=640x480 fps=30 max s=640x480 fps=30'
    m = _MODE_RE.search(line)
    assert m and m.group("pf") == "yuyv422" and m.group("maxw") == "640"
    line2 = '  vcodec=mjpeg  min s=1280x720 fps=10 max s=3840x2160 fps=30'
    m2 = _MODE_RE.search(line2)
    assert m2 and m2.group("vc") == "mjpeg" and m2.group("maxh") == "2160"


def test_choose_best_mode_prefers_pixels_then_fps():
    modes = [
        CaptureMode(1920, 1080, 30, "yuyv422"),
        CaptureMode(3840, 2160, 15, "mjpeg"),
        CaptureMode(1280, 720, 60, "mjpeg"),
        CaptureMode(3840, 2160, 30, "mjpeg"),
    ]
    best = choose_best_mode(modes)
    assert (best.width, best.height, best.fps) == (3840, 2160, 30)


def test_choose_best_prefers_mjpeg_at_same_res():
    modes = [
        CaptureMode(1920, 1080, 30, "yuyv422"),
        CaptureMode(1920, 1080, 30, "mjpeg"),
    ]
    assert choose_best_mode(modes).pixel_format == "mjpeg"


def test_fallback_order_descending():
    modes = [CaptureMode(640, 480, 30), CaptureMode(1920, 1080, 30), CaptureMode(1280, 720, 30)]
    seq = [(m.width) for m in fallback_modes(modes)]
    assert seq == [1920, 1280, 640]


def test_build_command_has_no_console_relevant_bits():
    args = build_record_command(
        "ffmpeg.exe", "Cam", CaptureMode(1920, 1080, 30, "mjpeg"),
        Path("out.mkv"), audio_device=None)
    s = " ".join(args)
    assert "-f dshow" in s and "video=Cam" in s and "out.mkv" in s
    assert "-vcodec mjpeg" in s


def test_with_encoder_swaps_codec():
    args = build_record_command(
        "ffmpeg.exe", "Cam", CaptureMode(640, 480, 30),
        Path("o.mkv"))
    new = with_encoder(args, HW_ENCODERS[0][1])
    assert "h264_nvenc" in new and "libx264" not in new


def test_unique_output_path(tmp_path):
    p1 = unique_output_path(tmp_path, "mkv")
    p1.write_bytes(b"x")
    p2 = unique_output_path(tmp_path, "mkv")
    assert p1 != p2 and p2.name.endswith("_01.mkv")
