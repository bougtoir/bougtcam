"""Recording engine for BougtCam.

Runs on its own thread, independent of the GUI. Owns the ffmpeg
child process, watchdog, reconnect loop, disk-space checks, and
MKV -> MP4 finalization. Communicates with the UI through a
callback (Qt signal emission is done by the caller).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import ffmpeg_backend as fb
from .config import Config

log = logging.getLogger("bougtcam.recorder")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class Status:
    state: str = "idle"            # idle|starting|recording|stopping|error|no_camera|reconnecting
    camera: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    started_at: float = 0.0
    current_file: str = ""
    message: str = ""

    @property
    def elapsed(self) -> float:
        if self.state == "recording" and self.started_at:
            return time.time() - self.started_at
        return 0.0


def unique_output_path(output_dir: Path, ext: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now()
    stem = when.strftime("%Y-%m-%d_%H-%M-%S")
    p = output_dir / f"{stem}.{ext}"
    n = 0
    while p.exists():
        n += 1
        p = output_dir / f"{stem}_{n:02d}.{ext}"
    return p


def free_space_mb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.anchor)
        return usage.free / (1024 * 1024)
    except OSError:
        return -1.0


class Recorder:
    """Threaded recorder; safe to drive from Qt signals."""

    def __init__(self, config: Config, on_status: Callable[[Status], None]):
        self.config = config
        self.on_status = on_status
        self.ffmpeg = fb.find_ffmpeg()
        self.status = Status()
        self._proc: Optional[subprocess.Popen] = None
        self._mkv: Optional[Path] = None
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._want_recording = False
        self._worker: Optional[threading.Thread] = None
        self._last_size = 0
        self._last_growth = 0.0
        self._stderr_tail: List[str] = []
        self._progress_time = 0.0
        self._last_progress = 0.0

    # ---- public control ----

    def start(self) -> None:
        """Begin the record loop (idempotent)."""
        with self._lock:
            self._want_recording = True
            if self._worker and self._worker.is_alive():
                return
            self._stop_evt.clear()
            self._worker = threading.Thread(target=self._run, name="recorder", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Stop recording but keep the app alive."""
        self._want_recording = False
        self._terminate_proc()

    def shutdown(self) -> None:
        """App exit: stop everything and wait briefly."""
        self._want_recording = False
        self._stop_evt.set()
        self._terminate_proc()
        if self._worker:
            self._worker.join(timeout=30)

    # ---- internals ----

    def _emit(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.status, k, v)
        try:
            self.on_status(self.status)
        except Exception:
            log.exception("status callback failed")

    def _terminate_proc(self) -> None:
        with self._lock:
            proc = self._proc
        if not proc:
            return
        log.info("stopping ffmpeg pid=%s", proc.pid)
        try:
            # 'q' asks ffmpeg to finalize the file cleanly.
            if proc.stdin:
                try:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass
        self._finalize_recording()

    def _finalize_recording(self) -> None:
        mkv = self._mkv
        self._mkv = None
        if not mkv or not mkv.exists() or mkv.stat().st_size == 0:
            return
        mp4 = mkv.with_suffix(".mp4")
        if mp4.exists():
            mp4 = unique_output_path(mkv.parent, "mp4")
        log.info("remuxing %s -> %s", mkv.name, mp4.name)
        if self.ffmpeg and fb.remux_to_mp4(self.ffmpeg, mkv, mp4):
            try:
                mkv.unlink()
            except OSError:
                log.warning("could not remove intermediate %s", mkv)
            log.info("saved %s", mp4)
        else:
            log.error("MP4 conversion failed; keeping %s", mkv)

    def _pick_camera(self) -> Optional[str]:
        if not self.ffmpeg:
            self._emit(state="error", message="ffmpeg が見つかりません")
            return None
        devices = fb.list_video_devices(self.ffmpeg)
        if not devices:
            return None
        if self.config.last_camera in devices:
            return self.config.last_camera
        return devices[0]

    def _open_capture(self, camera: str, modes: List[fb.CaptureMode]) -> bool:
        """Try modes best-first until ffmpeg actually produces output."""
        outdir = Path(self.config.output_dir)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._emit(state="error", message=f"保存先を作成できません: {e}")
            return False
        free = free_space_mb(outdir)
        if 0 <= free < self.config.min_free_space_mb:
            self._emit(state="error", message=f"ディスク空き容量不足 ({free:.0f} MB)")
            return False

        audio_dev = self.config.audio_device if self.config.audio_enabled else None
        if self.config.audio_enabled and not audio_dev:
            auds = fb.list_audio_devices(self.ffmpeg)
            audio_dev = auds[0] if auds else None
            if audio_dev:
                self.config.audio_device = audio_dev
                self.config.save()

        # Encoder preference: try hardware H.264 encoders first (if enabled),
        # always with libx264 as the final fallback.
        encoders: List[List[str]] = []
        if self.config.prefer_hw_encoder:
            encoders += [enc for _name, enc in fb.HW_ENCODERS]
        encoders.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])

        for mode in fb.fallback_modes(modes):
            if not self._want_recording or self._stop_evt.is_set():
                return False
            for enc_args in encoders:
                mkv = unique_output_path(outdir, "mkv")
                args = fb.build_record_command(
                    self.ffmpeg, camera, mode, mkv,
                    audio_device=audio_dev,
                    prefer_hw=self.config.prefer_hw_encoder,
                )
                args = fb.with_encoder(args, enc_args)
                log.info("attempt %dx%d@%.2f %s enc=%s -> %s",
                         mode.width, mode.height, mode.fps, mode.pixel_format,
                         enc_args[1] if len(enc_args) > 1 else "?", mkv.name)
                proc = self._spawn(args)
                if self._confirm_capture(proc, mkv):
                    self._proc = proc
                    self._mkv = mkv
                    self._last_size = 0
                    self._last_progress = self._progress_time
                    self._last_growth = time.time()
                    self._emit(state="recording", camera=camera,
                               width=mode.width, height=mode.height, fps=mode.fps,
                               started_at=time.time(), current_file=mkv.name, message="")
                    return True
                log.warning("mode %dx%d@%.2f %s enc=%s failed, falling back",
                            mode.width, mode.height, mode.fps, mode.pixel_format,
                            enc_args[1] if len(enc_args) > 1 else "?")
        return False

    def _spawn(self, args: List[str]) -> Optional[subprocess.Popen]:
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            log.error("failed to spawn ffmpeg: %s", e)
            return None
        # Drain stderr continuously from spawn: ffmpeg blocks when the pipe
        # buffer fills (verbose dshow warnings), stalling capture before the
        # watchdog's reader would ever start.
        tail = self._stderr_tail = []
        self._progress_time = 0.0

        def _drain() -> None:
            try:
                for raw in proc.stderr:
                    tail.append(raw.decode("utf-8", errors="replace").rstrip())
                    del tail[:-80]
            except (OSError, ValueError):
                pass

        threading.Thread(target=_drain, name="ffmpeg-stderr", daemon=True).start()

        def _drain_progress() -> None:
            try:
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("out_time_ms="):
                        try:
                            self._progress_time = int(line.split("=", 1)[1]) / 1e6
                        except ValueError:
                            pass
            except (OSError, ValueError):
                pass

        threading.Thread(target=_drain_progress, name="ffmpeg-stdout", daemon=True).start()
        return proc

    def _confirm_capture(self, proc: Optional[subprocess.Popen], mkv: Path, grace: float = 6.0) -> bool:
        """A mode counts as working when ffmpeg stays alive and the file grows."""
        if proc is None:
            return False
        deadline = time.time() + grace
        while time.time() < deadline:
            if proc.poll() is not None:
                err = "\n".join(self._stderr_tail)
                log.warning("ffmpeg exited rc=%s: %s", proc.returncode, err[-1500:])
                try:
                    mkv.unlink()
                except OSError:
                    pass
                return False
            if self._progress_time > 0:
                return True
            if mkv.exists() and mkv.stat().st_size > 4096:
                return True
            time.sleep(0.25)
        # give it a last chance on file size / mux progress
        ok = self._progress_time > 0 or (mkv.exists() and mkv.stat().st_size > 4096)
        if not ok:
            self._kill(proc)
            try:
                mkv.unlink()
            except OSError:
                pass
        return ok

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except OSError:
            pass

    def _watchdog(self, proc: subprocess.Popen, mkv: Path) -> str:
        """Block while recording; returns 'stopped' | 'crashed' | 'stalled' | 'disk_full'."""
        while True:
            if not self._want_recording or self._stop_evt.is_set():
                return "stopped"
            rc = proc.poll()
            if rc is not None:
                if self._stderr_tail:
                    log.warning("ffmpeg rc=%s tail: %s", rc, " | ".join(self._stderr_tail[-10:]))
                return "crashed"
            if self._progress_time > self._last_progress:
                self._last_progress = self._progress_time
                self._last_growth = time.time()
            if mkv.exists():
                size = mkv.stat().st_size
                if size > self._last_size:
                    self._last_size = size
                    self._last_growth = time.time()
                elif time.time() - self._last_growth > 15:
                    log.warning("output stalled at %d bytes (progress=%.1fs)",
                                size, self._progress_time)
                    return "stalled"
            outdir = Path(self.config.output_dir)
            free = free_space_mb(outdir)
            if 0 <= free < self.config.min_free_space_mb:
                log.error("disk nearly full (%.0f MB)", free)
                return "disk_full"
            time.sleep(1.0)

    def _run(self) -> None:
        self._emit(state="starting", message="カメラを検出中")
        if not self.ffmpeg:
            self._emit(state="error", message="ffmpeg が見つかりません（同梱/PATHを確認）")
            return
        log.info("ffmpeg: %s", self.ffmpeg)
        while not self._stop_evt.is_set():
            camera = self._pick_camera()
            if not camera:
                self._emit(state="no_camera", message="カメラが見つかりません。再接続を待機中")
                self._wait_retry()
                continue
            self._emit(state="starting", camera=camera, message="モードを列挙中")
            modes = fb.list_capture_modes(self.ffmpeg, camera)
            if modes:
                best = fb.choose_best_mode(modes)
                log.info("modes: %d, best=%dx%d@%.2f %s", len(modes),
                         best.width, best.height, best.fps, best.pixel_format)
            else:
                # Unknown modes: try a plain default open.
                modes = [fb.CaptureMode(0, 0, 0.0)]
            if not self._open_capture(camera, modes):
                if not self._want_recording:
                    break
                self._emit(state="error", camera=camera,
                           message="録画を開始できません。カメラ再接続を待機中")
                self._wait_retry()
                continue
            self.config.last_camera = camera
            self.config.save()
            result = self._watchdog(self._proc, self._mkv)
            if result == "stopped":
                break
            # abnormal end: finalize what we have, retry as a new file
            self._terminate_proc()
            if not self._want_recording:
                break
            msg = {
                "crashed": "録画プロセスが終了しました。再接続を試行します",
                "stalled": "カメラからの出力が停止しました。再接続を試行します",
                "disk_full": "ディスク容量不足のため停止しました",
            }.get(result, "録画が中断されました")
            self._emit(state="reconnecting", message=msg)
            if result == "disk_full":
                break
            self._wait_retry()
        if self.status.state not in ("idle",):
            self._emit(state="idle" if not self._want_recording else self.status.state)
        if not self._want_recording:
            self._emit(state="idle", message="")

    def _wait_retry(self) -> None:
        interval = max(1.0, float(self.config.reconnect_interval_sec))
        self._stop_evt.wait(interval)
