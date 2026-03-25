import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL = "anthropic/claude-sonnet-4-20250514"


async def _call_ai(prompt: str, system: str = "") -> str:
    if not OPENROUTER_API_KEY:
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
                    logger.error(f"AI error: {await resp.text()}")
                    return ""
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"AI call error: {e}")
        return ""


async def generate_outreach_email(lead: dict, subject: str) -> str:
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
        name = lead.get("name", "there").split()[0] if lead.get("name") else "there"
        company = lead.get("company", "your company")
        result = (
            f"<p>Hi {name},</p>"
            f"<p>I came across {company} and was impressed by your work. "
            f"I believe our services could help you achieve even greater results.</p>"
            f"<p>Would you be open to a quick 15-minute call this week?</p>"
            f"<p>Looking forward to hearing from you.</p>"
            f"<p>Best regards</p>"
        )
    return result


async def generate_followup_email(lead: dict) -> str:
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
        name = lead.get("name", "there").split()[0] if lead.get("name") else "there"
        result = (
            f"<p>Hi {name},</p>"
            f"<p>Just following up on my previous email. "
            f"Would a quick 10-minute call work for you this week?</p>"
            f"<p>Best regards</p>"
        )
    return result
