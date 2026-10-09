"""BougtCam configuration persistence.

Stored in %LOCALAPPDATA%\\BougtCam\\config.json on Windows
(falls back to ~/.config/BougtCam on other platforms, which is
useful only for development/tests).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_NAME = "BougtCam"
DEFAULT_OUTPUT_DIR = r"C:\Users\bougt\video"


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    output_dir: str = DEFAULT_OUTPUT_DIR
    last_camera: str = ""
    audio_enabled: bool = False
    audio_device: str = ""
    prefer_hw_encoder: bool = True
    reconnect_interval_sec: float = 3.0
    min_free_space_mb: int = 512
    extra: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return app_data_dir() / "config.json"

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        p = cfg.path
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (OSError, ValueError):
                pass
        return cfg
