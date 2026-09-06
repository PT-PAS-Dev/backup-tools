from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from paths import PROJECT_ROOT, load_project_env

load_project_env()

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


class AuthError(Exception):
    pass


def client_secrets_path() -> Path:
    return Path(os.environ.get("GOOGLE_OAUTH_CLIENT", str(PROJECT_ROOT / "oauth-client.json")))


def token_path() -> Path:
    return Path(os.environ.get("GOOGLE_OAUTH_TOKEN", str(PROJECT_ROOT / "token.json")))


def save_credentials(creds: Credentials) -> None:
    path = token_path()
    path.write_text(creds.to_json())


def load_credentials(*, interactive: bool = False) -> Credentials:
    secrets = client_secrets_path()
    token = token_path()
    creds: Credentials | None = None

    if token.is_file() and token.stat().st_size > 0:
        creds = Credentials.from_authorized_user_file(str(token), DRIVE_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds

    if not interactive:
        raise AuthError(
            "Google login belum ada atau token kadaluarsa. "
            "Jalankan: docker compose run --rm -p 8080:8080 backup python /app/auth.py"
        )

    if not secrets.is_file():
        raise AuthError(f"OAuth client file not found: {secrets}")

    in_docker = Path("/.dockerenv").exists()
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), DRIVE_SCOPES)
    port = int(os.environ.get("OAUTH_PORT", "8080"))
    print("Login Google: pakai akun kantor yang bisa menulis ke folder Drive.")
    if in_docker:
        print("Buka URL yang muncul di browser Mac.")
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="0.0.0.0" if in_docker else "127.0.0.1",
        port=port,
        open_browser=not in_docker,
        authorization_prompt_message="URL login:\n{url}\n",
        success_message="Login berhasil. Tab ini boleh ditutup.",
        access_type="offline",
        prompt="consent",
    )
    save_credentials(creds)
    return creds
