#!/usr/bin/env python3
"""One-time Telegram MTProto login for SignalTrail.

Reads credentials from .env in the project root (or --env-file path).
Creates a session file that evaluate.py reuses for non-interactive runs.

Run once interactively:

    pip install -r requirements.txt
    python3 scripts/login.py

After success the session file is saved and all subsequent runs are
non-interactive.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def load_env(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: {path} not found. Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


async def _amain(env_file: Path) -> int:
    env = load_env(env_file)
    api_id = env.get("TELEGRAM_API_ID")
    api_hash = env.get("TELEGRAM_API_HASH")
    session_path = env.get("TELEGRAM_SESSION_PATH", str(ROOT / ".cache" / "telegram.session"))
    if not api_id or not api_hash:
        print("ERROR: TELEGRAM_API_ID or TELEGRAM_API_HASH missing in .env")
        return 1
    try:
        from telethon import TelegramClient
    except ImportError:
        print("ERROR: telethon not installed. Run:  pip install -r requirements.txt")
        return 1

    phone = input("Phone number in international format (e.g. +919876543210): ").strip()
    if not phone:
        print("ERROR: phone number required.")
        return 1

    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, int(api_id), api_hash)
    print("\nConnecting to Telegram...")
    await client.connect()

    if await client.is_user_authorized():
        print(f"Already authorized. Session at {session_path}")
        await client.disconnect()
        return 0

    await client.send_code_request(phone)
    code = input("Enter the OTP Telegram just sent you: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except Exception as error:
        if "password" in str(error).lower() or "two" in str(error).lower():
            password = getpass.getpass("2FA password: ")
            await client.sign_in(password=password)
        else:
            print(f"ERROR: sign-in failed: {error}")
            await client.disconnect()
            return 1

    me = await client.get_me()
    print(f"\nLogged in as: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', 'n/a')})")
    print(f"Session saved to: {session_path}")
    print("\nNext: run  python3 scripts/evaluate.py  to fetch and score channels.")
    await client.disconnect()
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="One-time Telegram login for SignalTrail")
    ap.add_argument("--env-file", default=str(ROOT / ".env"), help="Path to .env credentials file")
    args = ap.parse_args()
    try:
        return asyncio.run(_amain(Path(args.env_file)))
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
