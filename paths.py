from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_project_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    os.environ.setdefault("TZ", "Asia/Jakarta")
    os.environ.setdefault("CONFIG_PATH", str(PROJECT_ROOT / "config.yaml"))
    os.environ.setdefault("GOOGLE_OAUTH_CLIENT", str(PROJECT_ROOT / "oauth-client.json"))
    os.environ.setdefault("GOOGLE_OAUTH_TOKEN", str(PROJECT_ROOT / "token.json"))
    os.environ.setdefault("BACKUP_DIR", str(PROJECT_ROOT / "tmp"))
    os.environ.setdefault("STATE_PATH", str(PROJECT_ROOT / "state" / "fingerprints.json"))
    os.environ.setdefault("OAUTH_PORT", "8080")
    os.environ.setdefault("INCREMENTAL", "1")

    for key in (
        "CONFIG_PATH",
        "GOOGLE_OAUTH_CLIENT",
        "GOOGLE_OAUTH_TOKEN",
        "BACKUP_DIR",
        "STATE_PATH",
    ):
        path = Path(os.environ[key])
        if not path.is_absolute():
            os.environ[key] = str(PROJECT_ROOT / path)
