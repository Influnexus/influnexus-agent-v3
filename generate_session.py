"""
=== TELETHON SESSION STRING GENERATOR ===

Run this ONCE on your Mac to generate a session string.
Then paste it into Railway as TELEGRAM_SESSION_STRING.

Usage:
    pip3 install telethon
    python3 generate_session.py

You need:
    1. Go to https://my.telegram.org
    2. Log in with your phone number
    3. Click "API Development Tools"
    4. Create an app (any name/short name)
    5. Copy your API ID and API Hash
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 50)
    print("  TELETHON SESSION STRING GENERATOR")
    print("=" * 50)
    print()
    print("Get your API ID and Hash from: https://my.telegram.org")
    print()

    api_id = input("Enter your TELEGRAM_API_ID: ").strip()
    api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()

    if not api_id or not api_hash:
        print("Error: Both API ID and Hash are required.")
        return

    print()
    print("A login code will be sent to your Telegram app...")
    print()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        phone = input("Enter your phone number (with country code, e.g. +919876543210): ").strip()
        await client.send_code_request(phone)
        code = input("Enter the code you received in Telegram: ").strip()

        try:
            await client.sign_in(phone, code)
        except Exception:
            password = input("2FA is enabled. Enter your password: ").strip()
            await client.sign_in(password=password)

    session_string = client.session.save()
    me = await client.get_me()

    print()
    print("=" * 50)
    print(f"  Logged in as: {me.first_name} {me.last_name or ''}")
    print(f"  Phone: +{me.phone}")
    print("=" * 50)
    print()
    print("Your TELEGRAM_SESSION_STRING (copy everything below):")
    print()
    print(session_string)
    print()
    print("=" * 50)
    print("Paste this as TELEGRAM_SESSION_STRING in Railway.")
    print("=" * 50)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
