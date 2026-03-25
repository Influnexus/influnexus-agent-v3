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


def _build_mime(to_email: str, subject: str, body_html: str) -> MIMEMultipart:
    """Build a MIME email message."""
    sender = GMAIL_SENDER_EMAIL or SMTP_EMAIL or "noreply@example.com"
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject

    plain = re.sub(r"<[^>]+>", "", body_html.replace("<br>", "\n").replace("<p>", "\n"))
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    return msg


# ── Gmail API sender ─────────────────────────────────────────────────

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
        return {"success": False, "error": "Gmail API credentials not configured (need GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN)"}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"Invalid email: '{to_email}'"}

    try:
        msg = _build_mime(to_email, subject, body_html)
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


# ── SMTP sender (fallback) ──────────────────────────────────────────

async def send_email_smtp(to_email: str, subject: str, body_html: str) -> dict:
    """Send email via Gmail SMTP. Returns dict with status and error."""
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        return {"success": False, "error": "SMTP_EMAIL or SMTP_PASSWORD not set"}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"Invalid email: '{to_email}'"}

    try:
        msg = _build_mime(to_email, subject, body_html)
        msg.replace_header("From", SMTP_EMAIL)

        def _send():
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.sendmail(SMTP_EMAIL, to_email, msg.as_string())

        await asyncio.get_event_loop().run_in_executor(None, _send)
        logger.info(f"Email sent via SMTP to {to_email}")
        return {"success": True, "error": ""}

    except smtplib.SMTPAuthenticationError:
        return {"success": False, "error": "Gmail SMTP login failed (need App Password)"}
    except Exception as e:
        logger.error(f"SMTP error ({to_email}): {e}")
        return {"success": False, "error": str(e)}


# ── Unified sender ──────────────────────────────────────────────────

async def send_email(to_email: str, subject: str, body_html: str) -> dict:
    """Send email: tries Gmail API first, falls back to SMTP."""
    # Nothing configured at all
    if not GMAIL_REFRESH_TOKEN and not (SMTP_EMAIL and SMTP_PASSWORD):
        return {"success": False, "error": "No email method configured. Set GMAIL_REFRESH_TOKEN or SMTP_EMAIL+SMTP_PASSWORD in Railway"}

    errors = []

    # Try Gmail API first (works on Railway, no port blocking)
    if GMAIL_REFRESH_TOKEN:
        logger.info(f"Trying Gmail API for {to_email}...")
        result = await send_email_gmail_api(to_email, subject, body_html)
        if result["success"]:
            return result
        errors.append(f"Gmail API: {result['error']}")
        logger.error(f"Gmail API failed for {to_email}: {result['error']}")
    else:
        logger.warning("GMAIL_REFRESH_TOKEN not set — skipping Gmail API")

    # Try SMTP as fallback
    if SMTP_EMAIL and SMTP_PASSWORD:
        logger.info(f"Trying SMTP for {to_email}...")
        result = await send_email_smtp(to_email, subject, body_html)
        if result["success"]:
            return result
        errors.append(f"SMTP: {result['error']}")
        logger.error(f"SMTP failed for {to_email}: {result['error']}")

    return {"success": False, "error": " | ".join(errors)}


# ── GMass bulk sender ───────────────────────────────────────────────

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
        "fromEmail": GMAIL_SENDER_EMAIL or SMTP_EMAIL,
        "emailList": email_list,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
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


# ── Main outreach flow ──────────────────────────────────────────────

async def outreach_flow(leads: list[dict], subject: str) -> dict:
    errors = []

    leads_with_email = [l for l in leads if l.get("email") and "@" in l.get("email", "")]
    leads_without_email = len(leads) - len(leads_with_email)

    if not leads_with_email:
        return {
            "sent": 0,
            "failed": len(leads),
            "errors": [
                f"None of the {len(leads)} leads have email addresses.",
                "Tip: Make sure HUNTER_API_KEY is set to enrich leads with emails.",
            ],
        }

    if leads_without_email > 0:
        errors.append(f"{leads_without_email} leads skipped (no email)")

    # Log credential status for debugging
    logger.info(f"GMAIL_CLIENT_ID length: {len(GMAIL_CLIENT_ID)}")
    logger.info(f"GMAIL_CLIENT_SECRET length: {len(GMAIL_CLIENT_SECRET)}")
    logger.info(f"GMAIL_REFRESH_TOKEN length: {len(GMAIL_REFRESH_TOKEN)}")
    logger.info(f"GMAIL_SENDER_EMAIL: {GMAIL_SENDER_EMAIL}")
    logger.info(f"GMAIL_CLIENT_ID ends with: ...{GMAIL_CLIENT_ID[-30:]}" if GMAIL_CLIENT_ID else "GMAIL_CLIENT_ID is empty")

    # Skip GMass — use Gmail API directly (GMass key is unreliable)
    # If you want GMass back, remove this comment and uncomment below
    # if GMASS_API_KEY:
    #     result = await send_via_gmass(leads_with_email, subject)
    #     if result["sent"] > 0:
    #         await crm_add_lead(leads, status="Outreached")
    #         result["errors"] = errors + result.get("errors", [])
    #         return result

    # Individual emails via Gmail API / SMTP
    sent = 0
    failed = 0
    for lead in leads_with_email:
        email = lead["email"]

        try:
            body = await generate_outreach_email(lead, subject)
        except Exception as e:
            logger.error(f"Email generation error for {email}: {e}")
            body = (
                f"<p>Hi {lead.get('name', 'there').split()[0] if lead.get('name') else 'there'},</p>"
                f"<p>I came across {lead.get('company', 'your company')} and was impressed. "
                f"Would you be open to a quick call this week?</p>"
                f"<p>Best regards</p>"
            )

        result = await send_email(email, subject, body)

        if result["success"]:
            sent += 1
            try:
                await crm_update_status(email, "Outreached")
            except Exception as e:
                logger.error(f"CRM update error: {e}")
        else:
            failed += 1
            errors.append(f"{email}: {result['error']}")
            # If auth error, stop trying all remaining
            if any(x in result["error"].lower() for x in ["login failed", "credentials", "refresh token", "invalid_grant"]):
                remaining = len(leads_with_email) - sent - failed
                if remaining > 0:
                    errors.append(f"Auth error - skipping {remaining} remaining emails")
                    failed += remaining
                break

    try:
        await crm_add_lead(leads, status="Outreached" if sent > 0 else "New")
    except Exception as e:
        logger.error(f"CRM save error: {e}")

    return {"sent": sent, "failed": failed + leads_without_email, "errors": errors}
