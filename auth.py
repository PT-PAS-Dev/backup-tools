#!/usr/bin/env python3
"""Login Google sekali (akun kantor) dan simpan token.json untuk backup."""

from drive_auth import AuthError, load_credentials, token_path


def main() -> int:
    try:
        load_credentials(interactive=True)
    except AuthError as exc:
        print(exc)
        return 1
    print(f"Login berhasil. Token disimpan di {token_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
