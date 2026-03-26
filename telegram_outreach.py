import os
import asyncio
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")

# Rate limit: seconds between messages to avoid Telegram bans
DELAY_BETWEEN_MESSAGES = 5

# Lazy-loaded Telethon client
_client = None


def _normalize_phone(phone: str) -> str:
    """Normalize phone to international format with + prefix."""
    digits = "".join(c for c in phone if c.isdigit())

    # Indian numbers: 10 digits → +91
    if len(digits) == 10:
        digits = "91" + digits
    elif digits.startswith("0"):
        digits = "91" + digits[1:]

    return "+" + digits


async def _get_client():
    """Get or create the Telethon client."""
    global _client

    if _client and _client.is_connected():
        return _client

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING]):
            return None

        _client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING),
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH,
        )
        await _client.connect()

        if not await _client.is_user_authorized():
            logger.error("Telethon session not authorized. Run generate_session.py again.")
            _client = None
            return None

        logger.info("Telethon client connected.")
        return _client

    except ImportError:
        logger.error("telethon not installed. Add it to requirements.txt.")
        return None
    except Exception as e:
        logger.error(f"Telethon client error: {e}")
        _client = None
        return None


async def send_telegram_dm(phone: str, message: str, lead_name: str = "") -> dict:
    """Send a Telegram DM to a phone number via Telethon."""
    if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING]):
        return {
            "success": False,
            "error": "Telegram outreach not configured (need TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING)",
        }

    normalized = _normalize_phone(phone)
    if len(normalized) < 11:  # +XX at minimum
        return {"success": False, "error": f"Invalid phone: {phone}"}

    try:
        from telethon.errors import (
            FloodWaitError,
            PeerFloodError,
            UserPrivacyRestrictedError,
            PhoneNumberBannedError,
        )
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        client = await _get_client()
        if not client:
            return {"success": False, "error": "Telethon client not available"}

        # Import the phone number as a contact to resolve it
        contact = InputPhoneContact(
            client_id=0,
            phone=normalized,
            first_name=lead_name.split()[0] if lead_name else "Lead",
            last_name=lead_name.split()[-1] if lead_name and " " in lead_name else "",
        )

        result = await client(ImportContactsRequest([contact]))

        if not result.users:
            return {
                "success": False,
                "error": f"Phone {phone} not on Telegram",
            }

        user = result.users[0]
        await client.send_message(user, message)
        logger.info(f"Telegram DM sent to {normalized} ({lead_name})")

        # Rate limiting
        await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

        return {"success": True, "error": ""}

    except FloodWaitError as e:
        wait = e.seconds
        logger.error(f"Telegram flood wait: {wait}s")
        return {"success": False, "error": f"Rate limited by Telegram. Wait {wait}s."}
    except PeerFloodError:
        logger.error("Telegram PeerFloodError — too many messages")
        return {"success": False, "error": "Too many messages sent. Telegram blocked further sends. Wait a few hours."}
    except UserPrivacyRestrictedError:
        return {"success": False, "error": f"User {phone} has privacy settings blocking messages"}
    except PhoneNumberBannedError:
        return {"success": False, "error": "Your Telegram account is restricted. Check your account."}
    except Exception as e:
        logger.error(f"Telegram DM error for {phone}: {e}")
        return {"success": False, "error": str(e)}


async def test_telegram_outreach() -> dict:
    """Test if Telethon client can connect."""
    if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_STRING]):
        missing = []
        if not TELEGRAM_API_ID:
            missing.append("TELEGRAM_API_ID")
        if not TELEGRAM_API_HASH:
            missing.append("TELEGRAM_API_HASH")
        if not TELEGRAM_SESSION_STRING:
            missing.append("TELEGRAM_SESSION_STRING")
        return {"success": False, "error": f"Missing: {', '.join(missing)}"}

    try:
        client = await _get_client()
        if not client:
            return {"success": False, "error": "Could not connect Telethon client"}

        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        phone = me.phone or "unknown"
        return {"success": True, "error": "", "account": f"{name} (+{phone})"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def disconnect_client():
    """Disconnect the Telethon client cleanly."""
    global _client
    if _client:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
