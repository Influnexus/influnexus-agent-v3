import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "anthropic/claude-sonnet-4-20250514")


async def _call_ai(prompt: str, system: str = "") -> str:
    """Call AI model via OpenRouter. Returns empty string on any failure."""
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set — using fallback email template")
        return ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json={
                    "model": AI_MODEL,
                    "messages": messages,
                    "max_tokens": 500,
                    "temperature": 0.7,
                },
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"AI error {resp.status}: {body[:200]}")
                    return ""
                data = await resp.json()

                # Safely navigate the response
                choices = data.get("choices", [])
                if not choices:
                    logger.error(f"AI returned no choices: {data}")
                    return ""
                return choices[0].get("message", {}).get("content", "") or ""

    except aiohttp.ClientError as e:
        logger.error(f"AI network error: {e}")
        return ""
    except Exception as e:
        logger.error(f"AI call error: {e}")
        return ""


def _fallback_outreach(lead: dict, subject: str) -> str:
    """Generate a simple fallback email when AI is unavailable."""
    name = "there"
    if lead.get("name"):
        parts = lead["name"].split()
        if parts:
            name = parts[0]

    company = lead.get("company", "your company")

    return (
        f"<p>Hi {name},</p>"
        f"<p>I came across {company} and was impressed by your work. "
        f"I believe our services could help you achieve even greater results.</p>"
        f"<p>Would you be open to a quick 15-minute call this week?</p>"
        f"<p>Looking forward to hearing from you.</p>"
        f"<p>Best regards</p>"
    )


async def generate_outreach_email(lead: dict, subject: str) -> str:
    """Generate a personalized outreach email. Always returns valid HTML."""
    system = (
        "You are a professional business development email writer. "
        "Write concise cold outreach emails under 150 words. "
        "Professional but friendly. Clear call-to-action. "
        "Output ONLY the email body in HTML (use <p>, <br>, <b> tags). "
        "No subject line or metadata."
    )
    prompt = (
        f"Write a cold outreach email:\n"
        f"- Recipient: {lead.get('name', 'the recipient')}\n"
        f"- Company: {lead.get('company', 'their company')}\n"
        f"- Title: {lead.get('title', '')}\n"
        f"- Subject context: {subject}\n\n"
        f"Introduce our services and request a brief call."
    )

    result = await _call_ai(prompt, system)
    if not result:
        result = _fallback_outreach(lead, subject)
    return result


async def generate_followup_email(lead: dict) -> str:
    """Generate a follow-up email. Always returns valid HTML."""
    system = (
        "You are a professional business development email writer. "
        "Write a brief friendly follow-up email under 100 words. "
        "Reference the previous email. Clear call-to-action. "
        "Output ONLY the email body in HTML."
    )
    prompt = (
        f"Write a follow-up email:\n"
        f"- Recipient: {lead.get('name', 'the recipient')}\n"
        f"- Company: {lead.get('company', 'their company')}\n\n"
        f"Follow-up to a previous outreach about our services."
    )

    result = await _call_ai(prompt, system)

    if not result:
        name = "there"
        if lead.get("name"):
            parts = lead["name"].split()
            if parts:
                name = parts[0]
        result = (
            f"<p>Hi {name},</p>"
            f"<p>Just following up on my previous email. "
            f"Would a quick 10-minute call work for you this week?</p>"
            f"<p>Best regards</p>"
        )
    return result
