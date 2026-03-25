import os
import re
import logging
import smtplib
import asyncio
import base64
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from ai_helper import generate_outreach_email
from crm import crm_add_lead, crm_update_status

logger = logging.getLogger(__name__)

SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
GMASS_API_KEY = os.environ.get("GMASS_API_KEY", "")

GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_SENDER_EMAIL = os.environ.get("GMAIL_SENDER_EMAIL", "")

# Conversation states
OUTREACH_SUBJECT = 10
OUTREACH_CONFIRM = 11


async def send_email_smtp(to_email: str, subject: str, body_html: str) -> dict:
    """Send email via Gmail SMTP. Returns dict with status and error."""
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        return {"success": False, "error": "SMTP_EMAIL or SMTP_PASSWORD not set in Railway variables"}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"Invalid email: '{to_email}'"}

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject

        plain = re.sub(r"<[^>]+>", "", body_html.replace("<br>", "\n").replace("<p>", "\n"))
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        # Run SMTP in a thread so it doesn't block the bot
        def _send():
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.sendmail(SMTP_EMAIL, to_email, msg.as_string())

        await asyncio.get_event_loop().run_in_executor(None, _send)

        logger.info(f"Email sent to {to_email}")
        return {"success": True, "error": ""}
    except smtplib.SMTPAuthenticationError:
        error = "Gmail login failed. Check SMTP_EMAIL and SMTP_PASSWORD (must be App Password, not regular password)"
        logger.error(error)
        return {"success": False, "error": error}
    except smtplib.SMTPRecipientsRefused:
        error = f"Recipient refused: {to_email}"
        logger.error(error)
        return {"success": False, "error": error}
    except Exception as e:
        logger.error(f"SMTP error ({to_email}): {e}")
        return {"success": False, "error": str(e)}


def _get_gmail_service():
    """Build Gmail API service using OAuth2 refresh token."""
    creds = OAuthCredentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


async def send_email_gmail_api(to_email: str, subject: str, body_html: str) -> dict:
    """Send email via Gmail API using OAuth2 refresh token."""
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        return {"success": False, "error": "Gmail API credentials not configured"}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"Invalid email: '{to_email}'"}

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = GMAIL_SENDER_EMAIL or SMTP_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject

        plain = re.sub(r"<[^>]+>", "", body_html.replace("<br>", "\n").replace("<p>", "\n"))
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        def _send():
            service = _get_gmail_service()
            service.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()

        await asyncio.get_event_loop().run_in_executor(None, _send)

        logger.info(f"Email sent via Gmail API to {to_email}")
        return {"success": True, "error": ""}
    except Exception as e:
        logger.error(f"Gmail API error ({to_email}): {e}")
        return {"success": False, "error": str(e)}


async def send_email(to_email: str, subject: str, body_html: str) -> dict:
    """Send email: tries Gmail API first, falls back to SMTP."""
    if GMAIL_REFRESH_TOKEN:
        result = await send_email_gmail_api(to_email, subject, body_html)
        if result["success"]:
            return result
        logger.warning(f"Gmail API failed, trying SMTP fallback: {result['error']}")

    return await send_email_smtp(to_email, subject, body_html)


async def send_via_gmass(leads: list[dict], subject: str) -> dict:
    if not GMASS_API_KEY:
        return {"sent": 0, "failed": len(leads), "errors": ["GMASS_API_KEY not set"]}

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

    if not email_list:
        return {"sent": 0, "failed": len(leads), "errors": ["No leads have email addresses"]}

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
                    return {"sent": len(email_list), "failed": 0, "errors": []}
                err = await resp.text()
                logger.error(f"GMass error: {err}")
                return {"sent": 0, "failed": len(email_list), "errors": [f"GMass API: {err}"]}
    except Exception as e:
        logger.error(f"GMass error: {e}")
        return {"sent": 0, "failed": len(email_list), "errors": [str(e)]}


async def outreach_flow(leads: list[dict], subject: str) -> dict:
    errors = []

    # Count how many leads have emails
    leads_with_email = [l for l in leads if l.get("email") and "@" in l.get("email", "")]
    leads_without_email = len(leads) - len(leads_with_email)

    if not leads_with_email:
        return {
            "sent": 0,
            "failed": len(leads),
            "errors": [f"None of the {len(leads)} leads have email addresses. Apollo free tier may hide emails. Make sure HUNTER_API_KEY is set to enrich leads."],
        }

    if leads_without_email > 0:
        errors.append(f"{leads_without_email} leads skipped (no email)")

    # Try GMass bulk first
    if GMASS_API_KEY:
        result = await send_via_gmass(leads_with_email, subject)
        if result["sent"] > 0:
            await crm_add_lead(leads, status="Outreached")
            result["errors"] = errors + result.get("errors", [])
            return result

    # Fallback: individual SMTP
    sent = 0
    failed = 0
    for lead in leads_with_email:
        email = lead["email"]
        body = await generate_outreach_email(lead, subject)
        result = await send_email(email, subject, body)

        if result["success"]:
            sent += 1
            await crm_update_status(email, "Outreached")
        else:
            failed += 1
            errors.append(f"{email}: {result['error']}")
            # If first email fails with auth error, stop trying
            if "login failed" in result["error"].lower():
                errors.append("Stopping - Gmail auth failed for all remaining")
                failed += len(leads_with_email) - sent - failed
                break

    await crm_add_lead(leads, status="Outreached" if sent > 0 else "New")
    return {"sent": sent, "failed": failed + leads_without_email, "errors": errors}
