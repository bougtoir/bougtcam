"""BougtCam GUI: minimal main window + system tray resident."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import APP_NAME, Config, app_data_dir
from .recorder import Recorder, Status

log = logging.getLogger("bougtcam.app")


def make_icon(recording: bool) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#d43d3d") if recording else QColor("#6b7280"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setBrush(QColor("white"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


class StatusBridge(QObject):
    updated = Signal(object)


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"{APP_NAME} 設定")
        lay = QFormLayout(self)
        self.out_edit = QLineEdit(cfg.output_dir)
        lay.addRow("保存先フォルダ", self.out_edit)
        self.audio_chk = QCheckBox("マイク音声も録音する")
        self.audio_chk.setChecked(cfg.audio_enabled)
        lay.addRow("音声", self.audio_chk)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addRow(btns)

    def accept(self):
        self.cfg.output_dir = self.out_edit.text().strip() or self.cfg.output_dir
        self.cfg.audio_enabled = self.audio_chk.isChecked()
        self.cfg.save()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, recorder: Recorder):
        super().__init__()
        self.cfg = cfg
        self.recorder = recorder
        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(420)

        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_cam = QLabel("カメラ: -")
        self.lbl_state = QLabel("状態: -")
        self.lbl_res = QLabel("解像度: -")
        self.lbl_time = QLabel("経過: -")
        self.lbl_dir = QLabel(f"保存先: {cfg.output_dir}")
        self.lbl_msg = QLabel("")
        self.lbl_msg.setStyleSheet("color:#b91c1c")
        for l in (self.lbl_cam, self.lbl_state, self.lbl_res, self.lbl_time,
                  self.lbl_dir, self.lbl_msg):
            lay.addWidget(l)
        btns = QHBoxLayout()
        self.btn_start = QPushButton("録画開始")
        self.btn_stop = QPushButton("録画停止")
        self.btn_start.clicked.connect(recorder.start)
        self.btn_stop.clicked.connect(recorder.stop)
        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        lay.addLayout(btns)
        self.setCentralWidget(w)

    def update_status(self, st: Status):
        state_ja = {
            "idle": "待機", "starting": "開始中", "recording": "録画中",
            "stopping": "停止中", "error": "エラー",
            "no_camera": "カメラ未検出", "reconnecting": "再接続待機",
        }.get(st.state, st.state)
        self.lbl_cam.setText(f"カメラ: {st.camera or '-'}")
        self.lbl_state.setText(f"状態: {state_ja}")
        res = f"{st.width}x{st.height} @{st.fps:g}fps" if st.width else "-"
        self.lbl_res.setText(f"解像度: {res}")
        self.lbl_msg.setText(st.message or "")
        self.btn_start.setEnabled(st.state in ("idle", "error", "no_camera"))
        self.btn_stop.setEnabled(st.state == "recording")

    def tick(self, st: Status):
        if st.state == "recording":
            e = int(st.elapsed)
            self.lbl_time.setText(f"経過: {e//3600:02d}:{(e%3600)//60:02d}:{e%60:02d}  {st.current_file}")
        else:
            self.lbl_time.setText("経過: -")

    def closeEvent(self, ev):
        # Close -> hide to tray; recording continues.
        ev.ignore()
        self.hide()


class BougtCamApp:
    def __init__(self):
        self.cfg = Config.load()
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.bridge = StatusBridge()
        self.recorder = Recorder(self.cfg, self._on_status)
        self.status = Status()
        self.bridge.updated.connect(self._status_changed)

        self.win = MainWindow(self.cfg, self.recorder)

        self.tray = QSystemTrayIcon(make_icon(False), self.app)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.act_status = QAction("状態: -")
        self.act_status.setEnabled(False)
        self.act_res = QAction("解像度: -")
        self.act_res.setEnabled(False)
        act_start = QAction("録画開始")
        act_start.triggered.connect(self.recorder.start)
        act_stop = QAction("録画停止")
        act_stop.triggered.connect(self.recorder.stop)
        act_open = QAction("保存フォルダを開く")
        act_open.triggered.connect(self._open_folder)
        act_settings = QAction("設定")
        act_settings.triggered.connect(self._settings)
        act_show = QAction("ウィンドウを表示")
        act_show.triggered.connect(self._show)
        act_quit = QAction("終了")
        act_quit.triggered.connect(self._quit)
        for a in (self.act_status, self.act_res, act_start, act_stop,
                  act_open, act_settings, act_show, act_quit):
            menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._show() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(lambda: self.win.tick(self.status))
        self.timer.start(1000)

        self.app.aboutToQuit.connect(self.recorder.shutdown)

    def _on_status(self, st: Status):
        # Recorder thread -> Qt main thread
        self.bridge.updated.emit(st)

    def _status_changed(self, st: Status):
        self.status = st
        self.win.update_status(st)
        rec = st.state == "recording"
        self.tray.setIcon(make_icon(rec))
        state_ja = {
            "idle": "待機", "starting": "開始中", "recording": "録画中",
            "stopping": "停止中", "error": "エラー",
            "no_camera": "カメラ未検出", "reconnecting": "再接続待機",
        }.get(st.state, st.state)
        self.act_status.setText(f"状態: {state_ja}")
        self.act_res.setText(
            f"解像度: {st.width}x{st.height} @{st.fps:g}fps" if st.width else "解像度: -")
        tip = f"{APP_NAME} - {state_ja}"
        if st.camera:
            tip += f" ({st.camera})"
        self.tray.setToolTip(tip)
        if st.message:
            log.info("status: %s %s", st.state, st.message)

    def _open_folder(self):
        d = Path(self.cfg.output_dir)
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(d))
        else:
            subprocess.Popen(["xdg-open", str(d)])

    def _settings(self):
        SettingsDialog(self.cfg, self.win).exec()
        self.win.lbl_dir.setText(f"保存先: {self.cfg.output_dir}")

    def _show(self):
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def _quit(self):
        self.tray.hide()
        self.app.quit()

    def run(self) -> int:
        # Auto-start recording as soon as the event loop is up.
        QTimer.singleShot(0, self.recorder.start)
        return self.app.exec()


def _pid_alive(pid: int) -> bool:
    """True when a process with pid still exists (Windows)."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    import ctypes.wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.wintypes.HANDLE
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def acquire_single_instance() -> bool:
    """Prevent double launch via a lock file recording the owner's pid."""
    lock = app_data_dir() / "bougtcam.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # Lock exists: another live instance holds it unless its pid is gone.
        try:
            other_pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            other_pid = -1
        if other_pid > 0 and _pid_alive(other_pid):
            return False
        # stale lock: remove and retry once
        try:
            lock.unlink()
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except OSError:
            return False
    except OSError:
        return False
    import atexit
    atexit.register(lambda: lock.exists() and lock.unlink())
    return True


def setup_logging() -> None:
    logdir = app_data_dir() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / (time.strftime("bougtcam-%Y%m%d") + ".log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stderr)],
    )


def main() -> int:
    setup_logging()
    if not acquire_single_instance():
        # Second instance: just exit quietly (optionally show a box).
        return 0
    log.info("%s starting", APP_NAME)
    app = BougtCamApp()
    return app.run()
