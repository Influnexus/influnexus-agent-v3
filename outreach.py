import os
import re
import logging
import smtplib
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from ai_helper import generate_outreach_email
from crm import crm_add_lead, crm_update_status

logger = logging.getLogger(__name__)

SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
GMASS_API_KEY = os.environ.get("GMASS_API_KEY", "")

# Conversation states
OUTREACH_SUBJECT = 10
OUTREACH_CONFIRM = 11


async def send_email_smtp(to_email: str, subject: str, body_html: str) -> bool:
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        logger.error("SMTP credentials not configured")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject

        plain = re.sub(r"<[^>]+>", "", body_html.replace("<br>", "\n").replace("<p>", "\n"))
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())

        logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"SMTP error ({to_email}): {e}")
        return False


async def send_via_gmass(leads: list[dict], subject: str) -> dict:
    if not GMASS_API_KEY:
        return {"sent": 0, "failed": len(leads)}

    email_list = []
    for lead in leads:
        if lead.get("email"):
            body = await generate_outreach_email(lead, subject)
            email_list.append({
                "EmailAddress": lead["email"],
                "Name": lead.get("name", ""),
                "Company": lead.get("company", ""),
                "CustomBody": body,
            })

    payload = {
        "subject": subject,
        "fromEmail": SMTP_EMAIL,
        "emailList": email_list,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.gmass.co/api/campaigns",
                json=payload,
                headers={"apikey": GMASS_API_KEY},
            ) as resp:
                if resp.status == 200:
                    return {"sent": len(email_list), "failed": 0}
                logger.error(f"GMass error: {await resp.text()}")
                return {"sent": 0, "failed": len(email_list)}
    except Exception as e:
        logger.error(f"GMass error: {e}")
        return {"sent": 0, "failed": len(email_list)}


async def outreach_flow(leads: list[dict], subject: str) -> dict:
    # Try GMass bulk first
    if GMASS_API_KEY:
        result = await send_via_gmass(leads, subject)
        if result["sent"] > 0:
            await crm_add_lead(leads, status="Outreached")
            return result

    # Fallback: individual SMTP
    sent = 0
    failed = 0
    for lead in leads:
        email = lead.get("email", "")
        if not email:
            failed += 1
            continue

        body = await generate_outreach_email(lead, subject)
        if await send_email_smtp(email, subject, body):
            sent += 1
            await crm_update_status(email, "Outreached")
        else:
            failed += 1

    await crm_add_lead(leads, status="Outreached")
    return {"sent": sent, "failed": failed}
