import os
import asyncio
import logging
import aiohttp

logger = logging.getLogger(__name__)

WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "hello_world")

API_URL = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def _normalize_phone(phone: str) -> str:
    """Normalize phone to international format (digits only, with country code)."""
    digits = "".join(c for c in phone if c.isdigit())

    # Indian numbers: if 10 digits, prepend 91
    if len(digits) == 10:
        digits = "91" + digits
    # If starts with 0, remove it and add country code
    elif digits.startswith("0"):
        digits = "91" + digits[1:]

    return digits


async def send_whatsapp_template(phone: str, lead_name: str = "") -> dict:
    """Send a WhatsApp template message to a phone number.
    WhatsApp requires template messages for first contact with new numbers.
    """
    if not WHATSAPP_PHONE_NUMBER_ID or not WHATSAPP_ACCESS_TOKEN:
        return {"success": False, "error": "WhatsApp not configured (need WHATSAPP_PHONE_NUMBER_ID + WHATSAPP_ACCESS_TOKEN)"}

    normalized = _normalize_phone(phone)
    if len(normalized) < 10:
        return {"success": False, "error": f"Invalid phone: {phone}"}

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized,
        "type": "template",
        "template": {
            "name": WHATSAPP_TEMPLATE_NAME,
            "language": {"code": "en"},
        },
    }

    # If template supports name parameter, add it
    if lead_name:
        payload["template"]["components"] = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": lead_name.split()[0] if lead_name else "there"}
                ],
            }
        ]

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                data = await resp.json()

                if resp.status == 200 and "messages" in data:
                    msg_id = data["messages"][0].get("id", "")
                    logger.info(f"WhatsApp sent to {normalized} (msg_id: {msg_id})")
                    return {"success": True, "error": ""}

                error = data.get("error", {}).get("message", str(data))
                logger.error(f"WhatsApp error for {normalized}: {error}")
                return {"success": False, "error": f"WhatsApp: {error}"}

    except Exception as e:
        logger.error(f"WhatsApp error for {phone}: {e}")
        return {"success": False, "error": str(e)}


async def send_whatsapp_text(phone: str, message: str) -> dict:
    """Send a freeform WhatsApp text message.
    Only works if the recipient has messaged your business number within the last 24 hours.
    """
    if not WHATSAPP_PHONE_NUMBER_ID or not WHATSAPP_ACCESS_TOKEN:
        return {"success": False, "error": "WhatsApp not configured"}

    normalized = _normalize_phone(phone)
    if len(normalized) < 10:
        return {"success": False, "error": f"Invalid phone: {phone}"}

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized,
        "type": "text",
        "text": {"body": message},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                data = await resp.json()

                if resp.status == 200 and "messages" in data:
                    logger.info(f"WhatsApp text sent to {normalized}")
                    return {"success": True, "error": ""}

                error = data.get("error", {}).get("message", str(data))
                logger.error(f"WhatsApp text error: {error}")
                return {"success": False, "error": f"WhatsApp: {error}"}

    except Exception as e:
        logger.error(f"WhatsApp text error: {e}")
        return {"success": False, "error": str(e)}


async def test_whatsapp() -> dict:
    """Test WhatsApp connection by checking the API."""
    if not WHATSAPP_PHONE_NUMBER_ID or not WHATSAPP_ACCESS_TOKEN:
        return {"success": False, "error": "WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN not set"}

    try:
        url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    phone = data.get("display_phone_number", "unknown")
                    return {"success": True, "error": "", "phone": phone}
                data = await resp.json()
                error = data.get("error", {}).get("message", f"HTTP {resp.status}")
                return {"success": False, "error": error}
    except Exception as e:
        return {"success": False, "error": str(e)}
