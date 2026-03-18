"""
InfluNexus Autonomous Sales Agent Bot v4
=========================================
Full-stack Telegram bot that:
1. Searches for business contacts (Apollo, Hunter, SerpAPI)
2. Stores leads in Google Sheets CRM
3. Sends personalized outreach emails (GMass bulk + SMTP follow-ups)
4. Books meetings via Google Calendar + Meet
5. Manages follow-up sequences automatically
6. Uses Claude AI for personalized email generation

Deploy on Railway with environment variables.
"""

import os
import json
import asyncio
import logging
import re
import smtplib
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import httpx
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── Environment Variables ─────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
ALLOWED_USER_ID   = int(os.environ.get("ALLOWED_USER_ID", "0"))

# API Keys for lead finding
APOLLO_API_KEY    = os.environ.get("APOLLO_API_KEY", "")
HUNTER_API_KEY    = os.environ.get("HUNTER_API_KEY", "")
SERPAPI_KEY        = os.environ.get("SERPAPI_KEY", "")

# Email sending
GMASS_API_KEY     = os.environ.get("GMASS_API_KEY", "")
SMTP_EMAIL        = os.environ.get("SMTP_EMAIL", "info@influnexus.com")
SMTP_PASSWORD     = os.environ.get("SMTP_PASSWORD", "")

# Google Calendar
CALENDLY_LINK     = os.environ.get("CALENDLY_LINK", "https://calendly.com/influnexus")
GCAL_CALENDAR_ID  = os.environ.get("GCAL_CALENDAR_ID", "primary")

# Google Service Account JSON (stored as env var on Railway)
_raw_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
# Handle cases where the JSON might be escaped or have extra quotes
try:
    _test = json.loads(_raw_creds)
    GOOGLE_CREDS_JSON = _raw_creds
except json.JSONDecodeError:
    try:
        # Try unescaping if Railway double-escaped it
        _raw_creds = _raw_creds.replace('\\"', '"').replace("\\n", "\n")
        _test = json.loads(_raw_creds)
        GOOGLE_CREDS_JSON = _raw_creds
    except json.JSONDecodeError:
        log.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Check the env variable.")
        GOOGLE_CREDS_JSON = "{}"

# ─── Constants ─────────────────────────────────────────────
INFLUNEXUS_PITCH = """InfluNexus (by Raah Enterprises) is a full-service creative & AI production agency operating across UAE, India, UK, and global markets.

Our Services:
• Cinematic Video Production & CGI/VFX
• AI-Generated Visuals & Motion Graphics
• Branding & Digital Marketing
• Web & App Development
• Influencer Marketing & Social Media Management

We've worked with brands like ADCB Bank, delivering premium creative campaigns that drive real business results."""

COMPANY_WEBSITE = "https://influnexus.com"

# Conversation states
(SEARCH_INDUSTRY, SEARCH_LOCATION, SEARCH_COUNT,
 CONFIRM_OUTREACH, CUSTOM_MESSAGE, FOLLOWUP_SETUP) = range(6)


# ═══════════════════════════════════════════════════════════
#  GOOGLE SHEETS CRM
# ═══════════════════════════════════════════════════════════
class CRM:
    """Google Sheets-based CRM for lead management."""

    HEADERS = [
        "ID", "Company", "Contact Name", "Email", "Phone",
        "Industry", "Location", "Source", "Status",
        "Last Contacted", "Next Follow-up", "Notes", "Created"
    ]

    def __init__(self):
        self.client = None
        self.sheet = None
        self._connect()

    def _connect(self):
        try:
            creds_dict = json.loads(GOOGLE_CREDS_JSON)
            if not creds_dict or "type" not in creds_dict:
                log.error("CRM: Google Service Account JSON is empty or invalid. Check GOOGLE_SERVICE_ACCOUNT_JSON env var.")
                return
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/calendar"
            ]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            self.client = gspread.authorize(creds)
            self.sheet = self.client.open_by_key(GOOGLE_SHEET_ID)
            self._ensure_worksheets()
            log.info("Google Sheets CRM connected.")
        except Exception as e:
            log.error(f"CRM connection failed: {e}")

    def _ensure_worksheets(self):
        """Create worksheets if they don't exist."""
        existing = [ws.title for ws in self.sheet.worksheets()]

        if "Leads" not in existing:
            ws = self.sheet.add_worksheet("Leads", rows=5000, cols=15)
            ws.append_row(self.HEADERS)
            log.info("Created 'Leads' worksheet.")

        if "Outreach Log" not in existing:
            ws = self.sheet.add_worksheet("Outreach Log", rows=5000, cols=10)
            ws.append_row([
                "Lead ID", "Email", "Type", "Subject",
                "Status", "Sent At", "Opened", "Replied", "Notes"
            ])
            log.info("Created 'Outreach Log' worksheet.")

        if "Follow-ups" not in existing:
            ws = self.sheet.add_worksheet("Follow-ups", rows=5000, cols=8)
            ws.append_row([
                "Lead ID", "Email", "Follow-up #",
                "Scheduled Date", "Status", "Sent At", "Content"
            ])
            log.info("Created 'Follow-ups' worksheet.")

    def add_lead(self, data: dict) -> str:
        """Add a lead to the CRM. Returns the lead ID."""
        try:
            ws = self.sheet.worksheet("Leads")
            lead_id = hashlib.md5(
                f"{data.get('email','')}{data.get('company','')}".encode()
            ).hexdigest()[:8].upper()

            row = [
                lead_id,
                data.get("company", ""),
                data.get("contact_name", ""),
                data.get("email", ""),
                data.get("phone", ""),
                data.get("industry", ""),
                data.get("location", ""),
                data.get("source", ""),
                "New",
                "",
                "",
                data.get("notes", ""),
                datetime.now().strftime("%Y-%m-%d %H:%M")
            ]
            ws.append_row(row)
            return lead_id
        except Exception as e:
            log.error(f"Failed to add lead: {e}")
            return ""

    def get_leads_by_status(self, status: str) -> list:
        """Get all leads with a given status."""
        try:
            ws = self.sheet.worksheet("Leads")
            records = ws.get_all_records()
            return [r for r in records if r.get("Status") == status]
        except Exception as e:
            log.error(f"Failed to get leads: {e}")
            return []

    def update_lead_status(self, lead_id: str, status: str):
        """Update lead status by ID."""
        try:
            ws = self.sheet.worksheet("Leads")
            cell = ws.find(lead_id)
            if cell:
                ws.update_cell(cell.row, 9, status)  # Status column
                ws.update_cell(cell.row, 10, datetime.now().strftime("%Y-%m-%d %H:%M"))
        except Exception as e:
            log.error(f"Failed to update lead: {e}")

    def log_outreach(self, lead_id: str, email: str, email_type: str, subject: str):
        """Log an outreach email."""
        try:
            ws = self.sheet.worksheet("Outreach Log")
            ws.append_row([
                lead_id, email, email_type, subject,
                "Sent", datetime.now().strftime("%Y-%m-%d %H:%M"),
                "No", "No", ""
            ])
        except Exception as e:
            log.error(f"Failed to log outreach: {e}")

    def schedule_followup(self, lead_id: str, email: str, followup_num: int, days_later: int, content: str):
        """Schedule a follow-up email."""
        try:
            ws = self.sheet.worksheet("Follow-ups")
            scheduled = (datetime.now() + timedelta(days=days_later)).strftime("%Y-%m-%d")
            ws.append_row([
                lead_id, email, followup_num,
                scheduled, "Pending", "", content
            ])
        except Exception as e:
            log.error(f"Failed to schedule follow-up: {e}")

    def get_pending_followups(self) -> list:
        """Get follow-ups due today or overdue."""
        try:
            ws = self.sheet.worksheet("Follow-ups")
            records = ws.get_all_records()
            today = datetime.now().strftime("%Y-%m-%d")
            return [
                r for r in records
                if r.get("Status") == "Pending" and r.get("Scheduled Date", "") <= today
            ]
        except Exception as e:
            log.error(f"Failed to get follow-ups: {e}")
            return []

    def mark_followup_sent(self, lead_id: str, followup_num: int):
        """Mark a follow-up as sent."""
        try:
            ws = self.sheet.worksheet("Follow-ups")
            records = ws.get_all_records()
            for i, r in enumerate(records, start=2):
                if str(r.get("Lead ID")) == str(lead_id) and str(r.get("Follow-up #")) == str(followup_num):
                    ws.update_cell(i, 5, "Sent")
                    ws.update_cell(i, 6, datetime.now().strftime("%Y-%m-%d %H:%M"))
                    break
        except Exception as e:
            log.error(f"Failed to mark follow-up: {e}")

    def get_all_leads_count(self) -> dict:
        """Get lead counts by status."""
        try:
            ws = self.sheet.worksheet("Leads")
            records = ws.get_all_records()
            counts = {}
            for r in records:
                s = r.get("Status", "Unknown")
                counts[s] = counts.get(s, 0) + 1
            counts["Total"] = len(records)
            return counts
        except Exception as e:
            log.error(f"Failed to get counts: {e}")
            return {"Total": 0}


# ═══════════════════════════════════════════════════════════
#  LEAD FINDER - Multi-source contact search
# ═══════════════════════════════════════════════════════════
class LeadFinder:
    """Finds business contacts from Apollo, Hunter, and SerpAPI."""

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=30)

    async def find_leads(self, industry: str, location: str, count: int = 50) -> list:
        """Search all sources and merge results."""
        leads = []

        # Run all sources in parallel
        tasks = []
        if APOLLO_API_KEY:
            tasks.append(self._search_apollo(industry, location, count))
        if HUNTER_API_KEY:
            tasks.append(self._search_hunter(industry, location, count))
        if SERPAPI_KEY:
            tasks.append(self._search_serpapi(industry, location, count))

        if not tasks:
            log.warning("No API keys configured for lead finding!")
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                leads.extend(result)

        # Deduplicate by email
        seen_emails = set()
        unique_leads = []
        for lead in leads:
            email = lead.get("email", "").lower()
            if email and email not in seen_emails:
                seen_emails.add(email)
                unique_leads.append(lead)

        return unique_leads[:count]

    async def _search_apollo(self, industry: str, location: str, count: int) -> list:
        """Search Apollo.io for contacts."""
        leads = []
        try:
            # Apollo People Search API
            url = "https://api.apollo.io/v1/mixed_people/search"
            payload = {
                "api_key": APOLLO_API_KEY,
                "q_organization_keyword_tags": [industry],
                "person_locations": [location],
                "page": 1,
                "per_page": min(count, 100),
                "person_seniorities": ["director", "vp", "c_suite", "owner", "founder"],
            }
            resp = await self.http.post(url, json=payload)
            data = resp.json()

            for person in data.get("people", []):
                email = person.get("email") or ""
                if not email:
                    continue
                leads.append({
                    "company": person.get("organization", {}).get("name", ""),
                    "contact_name": person.get("name", ""),
                    "email": email,
                    "phone": person.get("phone_numbers", [{}])[0].get("sanitized_number", "") if person.get("phone_numbers") else "",
                    "industry": industry,
                    "location": location,
                    "source": "Apollo.io",
                    "title": person.get("title", ""),
                    "linkedin": person.get("linkedin_url", ""),
                    "company_website": person.get("organization", {}).get("website_url", ""),
                    "notes": f"Title: {person.get('title', 'N/A')}"
                })

            log.info(f"Apollo returned {len(leads)} leads")
        except Exception as e:
            log.error(f"Apollo search failed: {e}")
        return leads

    async def _search_hunter(self, industry: str, location: str, count: int) -> list:
        """Search Hunter.io for emails by domain."""
        leads = []
        try:
            # First search for companies via domain search
            search_url = "https://api.hunter.io/v2/domain-search"
            # Use SerpAPI to find company domains first, then Hunter for emails
            if SERPAPI_KEY:
                domains = await self._find_domains_via_serp(industry, location)
                for domain_info in domains[:min(count, 20)]:
                    domain = domain_info.get("domain", "")
                    if not domain:
                        continue
                    try:
                        resp = await self.http.get(search_url, params={
                            "domain": domain,
                            "api_key": HUNTER_API_KEY,
                            "type": "personal",
                            "seniority": "senior,executive",
                            "limit": 5
                        })
                        data = resp.json()
                        for email_data in data.get("data", {}).get("emails", []):
                            leads.append({
                                "company": data.get("data", {}).get("organization", domain_info.get("company", "")),
                                "contact_name": f"{email_data.get('first_name', '')} {email_data.get('last_name', '')}".strip(),
                                "email": email_data.get("value", ""),
                                "phone": email_data.get("phone_number", "") or "",
                                "industry": industry,
                                "location": location,
                                "source": "Hunter.io",
                                "title": email_data.get("position", ""),
                                "notes": f"Confidence: {email_data.get('confidence', 'N/A')}%"
                            })
                    except Exception as e:
                        log.error(f"Hunter domain search for {domain} failed: {e}")

            log.info(f"Hunter returned {len(leads)} leads")
        except Exception as e:
            log.error(f"Hunter search failed: {e}")
        return leads

    async def _find_domains_via_serp(self, industry: str, location: str) -> list:
        """Find company domains via Google search."""
        domains = []
        try:
            query = f"{industry} companies in {location} contact email"
            resp = await self.http.get("https://serpapi.com/search.json", params={
                "api_key": SERPAPI_KEY,
                "q": query,
                "num": 50,
                "engine": "google"
            })
            data = resp.json()
            for result in data.get("organic_results", []):
                link = result.get("link", "")
                title = result.get("title", "")
                # Extract domain
                if link:
                    from urllib.parse import urlparse
                    parsed = urlparse(link)
                    domain = parsed.netloc.replace("www.", "")
                    if domain and not any(x in domain for x in [
                        "linkedin.com", "facebook.com", "twitter.com",
                        "instagram.com", "youtube.com", "wikipedia.org",
                        "yelp.com", "crunchbase.com", "google.com"
                    ]):
                        domains.append({
                            "domain": domain,
                            "company": title
                        })
        except Exception as e:
            log.error(f"SerpAPI domain search failed: {e}")
        return domains

    async def _search_serpapi(self, industry: str, location: str, count: int) -> list:
        """Search Google via SerpAPI for business contact info."""
        leads = []
        try:
            queries = [
                f"{industry} companies in {location} email contact",
                f"top {industry} firms {location} CEO email",
                f"{industry} {location} business directory email",
            ]
            for query in queries:
                resp = await self.http.get("https://serpapi.com/search.json", params={
                    "api_key": SERPAPI_KEY,
                    "q": query,
                    "num": 20,
                    "engine": "google"
                })
                data = resp.json()

                for result in data.get("organic_results", []):
                    snippet = result.get("snippet", "")
                    title = result.get("title", "")
                    link = result.get("link", "")

                    # Extract emails from snippets
                    emails_found = re.findall(
                        r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
                        snippet
                    )

                    for email in emails_found:
                        if not any(x in email.lower() for x in [
                            "example.com", "email.com", "test.com", "sentry"
                        ]):
                            leads.append({
                                "company": title.split("-")[0].split("|")[0].strip()[:60],
                                "contact_name": "",
                                "email": email,
                                "phone": "",
                                "industry": industry,
                                "location": location,
                                "source": "SerpAPI/Google",
                                "notes": f"Found via: {link[:80]}"
                            })

                # Also check local results
                for result in data.get("local_results", []):
                    if result.get("email"):
                        leads.append({
                            "company": result.get("title", ""),
                            "contact_name": "",
                            "email": result.get("email", ""),
                            "phone": result.get("phone", ""),
                            "industry": industry,
                            "location": location,
                            "source": "SerpAPI/Google Local",
                            "notes": f"Rating: {result.get('rating', 'N/A')}"
                        })

            log.info(f"SerpAPI returned {len(leads)} leads")
        except Exception as e:
            log.error(f"SerpAPI search failed: {e}")
        return leads

    async def verify_email(self, email: str) -> dict:
        """Verify an email using Hunter.io."""
        if not HUNTER_API_KEY:
            return {"status": "unknown", "score": 0}
        try:
            resp = await self.http.get("https://api.hunter.io/v2/email-verifier", params={
                "email": email,
                "api_key": HUNTER_API_KEY
            })
            data = resp.json().get("data", {})
            return {
                "status": data.get("status", "unknown"),
                "score": data.get("score", 0)
            }
        except Exception:
            return {"status": "unknown", "score": 0}

    async def close(self):
        await self.http.aclose()


# ═══════════════════════════════════════════════════════════
#  EMAIL ENGINE - GMass bulk + SMTP follow-ups
# ═══════════════════════════════════════════════════════════
class EmailEngine:
    """Handles all email sending — GMass for bulk, SMTP for follow-ups."""

    def __init__(self):
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
            log.info("Gemini AI configured for email generation.")
        else:
            self.model = None
            log.warning("GEMINI_API_KEY not set — will use fallback emails.")

    def generate_email(self, lead: dict, email_type: str = "initial") -> dict:
        """Use Gemini to generate a personalized email."""
        if email_type == "initial":
            prompt = f"""Write a professional, warm, and personalized cold outreach email from Rohit at InfluNexus.

Lead Info:
- Company: {lead.get('company', 'Unknown')}
- Contact: {lead.get('contact_name', 'there')}
- Industry: {lead.get('industry', 'their industry')}
- Location: {lead.get('location', '')}
- Title: {lead.get('title', '')}
- Company Website: {lead.get('company_website', '')}

About InfluNexus:
{INFLUNEXUS_PITCH}

Guidelines:
- Keep it under 150 words
- Be warm and genuine, NOT salesy
- Reference something specific about their industry
- Show how InfluNexus can add value to THEIR specific business
- End with a soft CTA (suggest a quick call or meeting)
- Include the Calendly link: {CALENDLY_LINK}
- Sign off as: Rohit | Founder, InfluNexus | {COMPANY_WEBSITE}

Return ONLY a JSON object with "subject" and "body" keys. No markdown, no code fences."""

        elif email_type == "followup_1":
            prompt = f"""Write a follow-up email (1st follow-up, 3 days after initial outreach) from Rohit at InfluNexus.

Lead: {lead.get('contact_name', 'there')} at {lead.get('company', 'their company')}
Industry: {lead.get('industry', '')}

Guidelines:
- Very short (under 80 words)
- Reference the previous email naturally
- Add one new value point or recent work example
- Keep the tone friendly and non-pushy
- Include Calendly: {CALENDLY_LINK}
- Sign off as: Rohit | InfluNexus

Return ONLY a JSON object with "subject" and "body" keys. No markdown, no code fences."""

        elif email_type == "followup_2":
            prompt = f"""Write a 2nd follow-up email (7 days after initial outreach) from Rohit at InfluNexus.

Lead: {lead.get('contact_name', 'there')} at {lead.get('company', 'their company')}
Industry: {lead.get('industry', '')}

Guidelines:
- Ultra short (under 60 words)
- Friendly breakup-style email ("Just checking if this is on your radar")
- Mention one specific thing you could do for them
- Last gentle nudge with Calendly: {CALENDLY_LINK}
- Sign off as: Rohit | InfluNexus

Return ONLY a JSON object with "subject" and "body" keys. No markdown, no code fences."""

        else:
            return {"subject": "InfluNexus - Creative & AI Production", "body": "Hi there"}

        try:
            if not self.model:
                raise Exception("Gemini not configured")
            response = self.model.generate_content(prompt)
            text = response.text.strip()
            # Clean up potential markdown
            text = re.sub(r'^```json\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
            text = text.strip()
            return json.loads(text)
        except Exception as e:
            log.error(f"Gemini email generation failed: {e}")
            return {
                "subject": f"Creative & AI Production for {lead.get('company', 'your brand')} — InfluNexus",
                "body": f"Hi {lead.get('contact_name', 'there')},\n\nI'm Rohit from InfluNexus — we're a creative & AI production agency working across UAE, India, and the UK.\n\nI'd love to explore how we could support {lead.get('company', 'your brand')} with premium video, AI visuals, or digital campaigns.\n\nWould you be open to a quick 15-min chat?\n\nBook a time: {CALENDLY_LINK}\n\nBest,\nRohit\nFounder, InfluNexus\n{COMPANY_WEBSITE}"
            }

    async def send_via_gmass(self, leads: list, email_data: dict) -> dict:
        """Send bulk emails via GMass API."""
        if not GMASS_API_KEY:
            return {"success": False, "error": "GMass API key not configured"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # GMass campaign creation
                recipients = []
                for lead in leads:
                    recipients.append({
                        "EmailAddress": lead.get("email", ""),
                        "Name": lead.get("contact_name", ""),
                        "Company": lead.get("company", ""),
                    })

                payload = {
                    "apiKey": GMASS_API_KEY,
                    "subject": email_data["subject"],
                    "message": email_data["body"],
                    "recipients": recipients,
                    "openTracking": True,
                    "clickTracking": True,
                    "sendAs": SMTP_EMAIL
                }

                resp = await client.post(
                    "https://api.gmass.co/api/campaigns",
                    json=payload
                )

                if resp.status_code == 200:
                    return {"success": True, "data": resp.json()}
                else:
                    log.error(f"GMass error: {resp.text}")
                    return {"success": False, "error": resp.text}
        except Exception as e:
            log.error(f"GMass send failed: {e}")
            return {"success": False, "error": str(e)}

    def send_via_smtp(self, to_email: str, subject: str, body: str) -> bool:
        """Send a single email via Google Workspace SMTP."""
        if not SMTP_PASSWORD:
            log.error("SMTP password not configured — set SMTP_PASSWORD env var")
            return False
        if not SMTP_EMAIL:
            log.error("SMTP email not configured — set SMTP_EMAIL env var")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"Rohit | InfluNexus <{SMTP_EMAIL}>"
            msg["To"] = to_email
            msg["Subject"] = subject

            # HTML version
            html_body = body.replace("\n", "<br>")
            html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                {html_body}
            </body>
            </html>
            """

            msg.attach(MIMEText(body, "plain"))
            msg.attach(MIMEText(html, "html"))

            log.info(f"Connecting to SMTP for {to_email}...")
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                log.info(f"SMTP login as {SMTP_EMAIL}...")
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.send_message(msg)

            log.info(f"✅ Email sent to {to_email}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            log.error(f"❌ SMTP AUTH FAILED — check SMTP_PASSWORD (App Password). Error: {e}")
            return False
        except smtplib.SMTPException as e:
            log.error(f"❌ SMTP error to {to_email}: {e}")
            return False
        except Exception as e:
            log.error(f"❌ SMTP send failed to {to_email}: {type(e).__name__}: {e}")
            return False


# ═══════════════════════════════════════════════════════════
#  MEETING BOOKER - Google Calendar + Meet
# ═══════════════════════════════════════════════════════════
class MeetingBooker:
    """Books meetings with Google Meet links."""

    def __init__(self):
        try:
            creds_dict = json.loads(GOOGLE_CREDS_JSON)
            scope = [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/calendar.events"
            ]
            self.creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        except Exception as e:
            log.error(f"Calendar auth failed: {e}")
            self.creds = None

    async def create_meeting(self, attendee_email: str, attendee_name: str, 
                              date_str: str, time_str: str = "10:00") -> dict:
        """Create a Google Calendar event with Meet link."""
        if not self.creds:
            return {"success": False, "error": "Calendar not configured"}

        try:
            import google.auth.transport.requests
            from googleapiclient.discovery import build

            service = build("calendar", "v3", credentials=self.creds)

            start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            end_dt = start_dt + timedelta(minutes=30)

            event = {
                "summary": f"InfluNexus x {attendee_name} — Intro Call",
                "description": f"Quick intro call to discuss how InfluNexus can help.\n\n{COMPANY_WEBSITE}",
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Asia/Dubai"
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Asia/Dubai"
                },
                "attendees": [
                    {"email": attendee_email},
                    {"email": SMTP_EMAIL}
                ],
                "conferenceData": {
                    "createRequest": {
                        "requestId": hashlib.md5(attendee_email.encode()).hexdigest(),
                        "conferenceSolutionKey": {"type": "hangoutsMeet"}
                    }
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "email", "minutes": 60},
                        {"method": "popup", "minutes": 15}
                    ]
                }
            }

            result = service.events().insert(
                calendarId=GCAL_CALENDAR_ID,
                body=event,
                conferenceDataVersion=1,
                sendUpdates="all"
            ).execute()

            meet_link = result.get("hangoutLink", "")
            return {
                "success": True,
                "event_link": result.get("htmlLink", ""),
                "meet_link": meet_link,
                "event_id": result.get("id", "")
            }
        except Exception as e:
            log.error(f"Meeting creation failed: {e}")
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
#  TELEGRAM BOT HANDLERS
# ═══════════════════════════════════════════════════════════

# Initialize components
crm = CRM()
lead_finder = LeadFinder()
email_engine = EmailEngine()
meeting_booker = MeetingBooker()


def auth_check(func):
    """Decorator to restrict bot to allowed user."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
            await update.message.reply_text("⛔ Unauthorized. This bot is private.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


@auth_check
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with main menu."""
    keyboard = ReplyKeyboardMarkup([
        ["🔍 Find Leads", "📧 Run Outreach"],
        ["📊 CRM Dashboard", "📅 Book Meeting"],
        ["🔄 Send Follow-ups", "🤖 AI Chat"],
    ], resize_keyboard=True)

    await update.message.reply_text(
        "🚀 *InfluNexus Sales Agent Bot v4*\n\n"
        "I'm your autonomous sales agent. Here's what I can do:\n\n"
        "🔍 *Find Leads* — Search any industry/location for contacts\n"
        "📧 *Run Outreach* — Auto-send personalized emails\n"
        "📊 *CRM Dashboard* — View all leads & stats\n"
        "📅 *Book Meeting* — Schedule calls with Google Meet\n"
        "🔄 *Send Follow-ups* — Process pending follow-ups\n"
        "🤖 *AI Chat* — Ask me anything about sales strategy\n\n"
        "Let's get started! 👇",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ─── Lead Search Flow ─────────────────────────────────────
@auth_check
async def cmd_find_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the lead search flow."""
    await update.message.reply_text(
        "🔍 *Lead Search*\n\nWhat industry/niche are you targeting?\n\n"
        "Examples: shoes, real estate, jewelry, fashion, restaurants, tech startups",
        parse_mode="Markdown"
    )
    return SEARCH_INDUSTRY


async def handle_industry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search_industry"] = update.message.text
    await update.message.reply_text(
        "📍 Which location/city?\n\n"
        "Examples: Dubai, Mumbai, London, New York, Riyadh"
    )
    return SEARCH_LOCATION


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search_location"] = update.message.text
    await update.message.reply_text(
        "🔢 How many leads do you need?\n\n"
        "Enter a number (e.g., 50, 100, 500)"
    )
    return SEARCH_COUNT


async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return SEARCH_COUNT

    context.user_data["search_count"] = count
    industry = context.user_data["search_industry"]
    location = context.user_data["search_location"]

    await update.message.reply_text(
        f"⏳ Searching for *{count}* leads in *{industry}* across *{location}*...\n\n"
        "This may take a minute. Searching Apollo, Hunter, and Google...",
        parse_mode="Markdown"
    )

    # Search for leads
    leads = await lead_finder.find_leads(industry, location, count)

    if not leads:
        await update.message.reply_text(
            "😕 No leads found. Try:\n"
            "- Broader industry terms\n"
            "- Larger cities\n"
            "- Check your API keys are set up correctly"
        )
        return ConversationHandler.END

    # Store leads in context and CRM
    context.user_data["found_leads"] = leads
    added_count = 0
    for lead in leads:
        lead_id = crm.add_lead(lead)
        if lead_id:
            lead["id"] = lead_id
            added_count += 1

    # Summary
    sources = {}
    for lead in leads:
        src = lead.get("source", "Unknown")
        sources[src] = sources.get(src, 0) + 1

    source_text = "\n".join([f"  • {src}: {cnt}" for src, cnt in sources.items()])

    # Show sample leads
    sample = leads[:5]
    sample_text = ""
    for i, lead in enumerate(sample, 1):
        sample_text += (
            f"\n{i}. *{lead.get('company', 'N/A')}*\n"
            f"   👤 {lead.get('contact_name', 'N/A')} — {lead.get('title', 'N/A')}\n"
            f"   📧 {lead.get('email', 'N/A')}\n"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Start Outreach to ALL", callback_data="outreach_all")],
        [InlineKeyboardButton("✅ Done — Save to CRM only", callback_data="save_only")],
    ])

    await update.message.reply_text(
        f"✅ *Found {len(leads)} leads!*\n\n"
        f"📊 Sources:\n{source_text}\n\n"
        f"📝 Sample leads:{sample_text}\n\n"
        f"💾 {added_count} leads saved to Google Sheets CRM.\n\n"
        "What would you like to do?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return CONFIRM_OUTREACH


async def handle_outreach_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "save_only":
        await query.edit_message_text("✅ Leads saved to CRM. Use 📧 *Run Outreach* when ready.", parse_mode="Markdown")
        return ConversationHandler.END

    elif query.data == "outreach_all":
        leads = context.user_data.get("found_leads", [])
        if not leads:
            await query.edit_message_text("No leads found. Run a search first.")
            return ConversationHandler.END

        await query.edit_message_text(
            f"📧 *Starting outreach to {len(leads)} leads...*\n\n"
            "Generating personalized emails with Claude AI...\n"
            "This may take a few minutes for large batches.",
            parse_mode="Markdown"
        )

        # Generate and send emails
        success_count = 0
        fail_count = 0

        # Try GMass first for bulk, fall back to SMTP
        gmass_worked = False
        if GMASS_API_KEY and len(leads) > 10:
            try:
                sample_lead = leads[0]
                email_data = email_engine.generate_email(sample_lead, "initial")

                email_data["subject"] = email_data["subject"].replace(
                    sample_lead.get("company", ""), "{Company}"
                )
                email_data["body"] = email_data["body"].replace(
                    sample_lead.get("contact_name", ""), "{Name}"
                ).replace(
                    sample_lead.get("company", ""), "{Company}"
                )

                result = await email_engine.send_via_gmass(leads, email_data)
                if result["success"]:
                    gmass_worked = True
                    success_count = len(leads)
                    for lead in leads:
                        crm.log_outreach(lead.get("id", ""), lead["email"], "Initial - GMass", email_data["subject"])
                        crm.update_lead_status(lead.get("id", ""), "Contacted")
                        crm.schedule_followup(lead.get("id", ""), lead["email"], 1, 3, "followup_1")
                        crm.schedule_followup(lead.get("id", ""), lead["email"], 2, 7, "followup_2")
                else:
                    log.warning(f"GMass failed: {result.get('error', 'unknown')}. Falling back to SMTP...")
            except Exception as e:
                log.error(f"GMass error: {e}. Falling back to SMTP...")

        # SMTP sending (either as fallback or primary)
        if not gmass_worked:
            for lead in leads:
                try:
                    email_data = email_engine.generate_email(lead, "initial")
                    sent = email_engine.send_via_smtp(
                        lead["email"],
                        email_data["subject"],
                        email_data["body"]
                    )
                    if sent:
                        success_count += 1
                        crm.log_outreach(lead.get("id", ""), lead["email"], "Initial - SMTP", email_data["subject"])
                        crm.update_lead_status(lead.get("id", ""), "Contacted")
                        crm.schedule_followup(lead.get("id", ""), lead["email"], 1, 3, "followup_1")
                        crm.schedule_followup(lead.get("id", ""), lead["email"], 2, 7, "followup_2")
                    else:
                        fail_count += 1
                except Exception as e:
                    log.error(f"SMTP send error for {lead.get('email', '?')}: {e}")
                    fail_count += 1
                # Rate limiting - avoid Gmail throttling
                await asyncio.sleep(3)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"📧 *Outreach Complete!*\n\n"
                f"✅ Sent: {success_count}\n"
                f"❌ Failed: {fail_count}\n\n"
                f"📅 Follow-up 1 scheduled: Day 3\n"
                f"📅 Follow-up 2 scheduled: Day 7\n\n"
                "Use 🔄 *Send Follow-ups* to process them when due."
            ),
            parse_mode="Markdown"
        )
        return ConversationHandler.END


# ─── Outreach from CRM ────────────────────────────────────
@auth_check
async def cmd_run_outreach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run outreach to new leads in CRM."""
    new_leads = crm.get_leads_by_status("New")

    if not new_leads:
        await update.message.reply_text(
            "📭 No new leads in CRM. Use 🔍 *Find Leads* to add some first.",
            parse_mode="Markdown"
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📧 Email all {len(new_leads)} new leads", callback_data="outreach_crm_all")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_outreach")],
    ])

    await update.message.reply_text(
        f"📧 *Outreach Queue*\n\n"
        f"Found *{len(new_leads)}* new leads ready for outreach.\n\n"
        "Start emailing them?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def handle_crm_outreach_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_outreach":
        await query.edit_message_text("Outreach cancelled.")
        return

    if query.data == "outreach_crm_all":
        new_leads = crm.get_leads_by_status("New")
        await query.edit_message_text(f"⏳ Sending to {len(new_leads)} leads...")

        success = 0
        for lead in new_leads:
            email_data = email_engine.generate_email(lead, "initial")
            sent = email_engine.send_via_smtp(lead["Email"], email_data["subject"], email_data["body"])
            if sent:
                success += 1
                crm.log_outreach(lead["ID"], lead["Email"], "Initial", email_data["subject"])
                crm.update_lead_status(lead["ID"], "Contacted")
                crm.schedule_followup(lead["ID"], lead["Email"], 1, 3, "followup_1")
                crm.schedule_followup(lead["ID"], lead["Email"], 2, 7, "followup_2")
            await asyncio.sleep(2)

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Outreach sent to {success}/{len(new_leads)} leads!"
        )


# ─── Follow-ups ───────────────────────────────────────────
@auth_check
async def cmd_followups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process pending follow-ups."""
    pending = crm.get_pending_followups()

    if not pending:
        await update.message.reply_text(
            "✅ No follow-ups due today! All caught up."
        )
        return

    await update.message.reply_text(
        f"🔄 Processing *{len(pending)}* pending follow-ups...",
        parse_mode="Markdown"
    )

    success = 0
    for fu in pending:
        email = fu.get("Email", "")
        fu_num = fu.get("Follow-up #", 1)
        lead_id = fu.get("Lead ID", "")

        # Build lead dict for email generation
        lead = {"email": email, "contact_name": "", "company": "", "industry": ""}

        email_type = "followup_1" if fu_num == 1 else "followup_2"
        email_data = email_engine.generate_email(lead, email_type)

        sent = email_engine.send_via_smtp(email, email_data["subject"], email_data["body"])
        if sent:
            success += 1
            crm.mark_followup_sent(lead_id, fu_num)
            crm.log_outreach(lead_id, email, f"Follow-up #{fu_num}", email_data["subject"])
        await asyncio.sleep(2)

    await update.message.reply_text(
        f"✅ Follow-ups sent: {success}/{len(pending)}"
    )


# ─── CRM Dashboard ────────────────────────────────────────
@auth_check
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show CRM dashboard."""
    counts = crm.get_all_leads_count()
    pending_fus = crm.get_pending_followups()

    text = (
        "📊 *InfluNexus CRM Dashboard*\n\n"
        f"📈 Total Leads: *{counts.get('Total', 0)}*\n"
        f"🆕 New: {counts.get('New', 0)}\n"
        f"📧 Contacted: {counts.get('Contacted', 0)}\n"
        f"🔄 Follow-up: {counts.get('Follow-up', 0)}\n"
        f"📅 Meeting Booked: {counts.get('Meeting Booked', 0)}\n"
        f"✅ Won: {counts.get('Won', 0)}\n"
        f"❌ Lost: {counts.get('Lost', 0)}\n\n"
        f"🔄 Pending Follow-ups: *{len(pending_fus)}*\n\n"
        f"📄 [Open Google Sheet](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID})"
    )

    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# ─── Book Meeting ──────────────────────────────────────────
@auth_check
async def cmd_book_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Book a meeting with a lead."""
    await update.message.reply_text(
        "📅 *Book a Meeting*\n\n"
        "Send me the details in this format:\n\n"
        "`email@example.com, Contact Name, 2026-03-25, 14:00`\n\n"
        "Or just share your Calendly link with them:\n"
        f"{CALENDLY_LINK}",
        parse_mode="Markdown"
    )


@auth_check
async def handle_meeting_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle meeting booking from text input."""
    text = update.message.text
    if "@" not in text or "," not in text:
        return  # Not a meeting request

    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 4:
        await update.message.reply_text("Please use format: `email, name, date, time`", parse_mode="Markdown")
        return

    email, name, date, time = parts[0], parts[1], parts[2], parts[3]

    await update.message.reply_text(f"⏳ Creating meeting with {name}...")

    result = await meeting_booker.create_meeting(email, name, date, time)

    if result["success"]:
        await update.message.reply_text(
            f"✅ *Meeting Booked!*\n\n"
            f"👤 {name} ({email})\n"
            f"📅 {date} at {time} (UAE time)\n"
            f"🔗 Meet: {result.get('meet_link', 'N/A')}\n"
            f"📎 Event: {result.get('event_link', 'N/A')}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ Failed: {result.get('error', 'Unknown error')}\n\n"
            f"Use Calendly instead: {CALENDLY_LINK}"
        )


# ─── AI Chat ──────────────────────────────────────────────
@auth_check
async def cmd_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *AI Assistant Mode*\n\n"
        "Ask me anything about:\n"
        "- Sales strategy\n"
        "- Email copywriting\n"
        "- Lead qualification\n"
        "- Industry research\n\n"
        "Just type your question!",
        parse_mode="Markdown"
    )


@auth_check
async def handle_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general AI chat messages."""
    text = update.message.text

    # Skip if it's a command or button press
    if text.startswith("/") or text in [
        "🔍 Find Leads", "📧 Run Outreach", "📊 CRM Dashboard",
        "📅 Book Meeting", "🔄 Send Follow-ups", "🤖 AI Chat"
    ]:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        model = genai.GenerativeModel("gemini-2.0-flash",
            system_instruction=(
                "You are Rohit's AI sales assistant at InfluNexus, a creative & AI production agency. "
                "Help with sales strategy, email writing, lead qualification, and business development. "
                "Be concise and actionable. "
                f"Company info: {INFLUNEXUS_PITCH}"
            ))
        response = model.generate_content(text)
        reply = response.text
        # Truncate if too long for Telegram
        if len(reply) > 4000:
            reply = reply[:4000] + "..."
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"AI error: {e}")


# ─── Button Router ─────────────────────────────────────────
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route keyboard button presses."""
    text = update.message.text

    routes = {
        "🔍 Find Leads": cmd_find_leads,
        "📧 Run Outreach": cmd_run_outreach,
        "📊 CRM Dashboard": cmd_dashboard,
        "📅 Book Meeting": cmd_book_meeting,
        "🔄 Send Follow-ups": cmd_followups,
        "🤖 AI Chat": cmd_ai_chat,
    }

    handler = routes.get(text)
    if handler:
        return await handler(update, context)


# ─── Follow-up Scheduler (Background Job) ─────────────────
async def followup_job(context: ContextTypes.DEFAULT_TYPE):
    """Auto-send follow-ups (runs every 6 hours)."""
    pending = crm.get_pending_followups()
    if not pending:
        return

    success = 0
    for fu in pending:
        email = fu.get("Email", "")
        fu_num = fu.get("Follow-up #", 1)
        lead = {"email": email, "contact_name": "", "company": "", "industry": ""}
        email_type = "followup_1" if fu_num == 1 else "followup_2"
        email_data = email_engine.generate_email(lead, email_type)

        sent = email_engine.send_via_smtp(email, email_data["subject"], email_data["body"])
        if sent:
            success += 1
            crm.mark_followup_sent(fu.get("Lead ID", ""), fu_num)
        await asyncio.sleep(2)

    if ALLOWED_USER_ID and success > 0:
        try:
            await context.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=f"🔄 Auto follow-up: {success}/{len(pending)} sent!"
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  MAIN — Wire everything up
# ═══════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversation handler for lead search flow
    search_conv = ConversationHandler(
        entry_points=[
            CommandHandler("find", cmd_find_leads),
            MessageHandler(filters.Regex("^🔍 Find Leads$"), cmd_find_leads),
        ],
        states={
            SEARCH_INDUSTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_industry)],
            SEARCH_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_location)],
            SEARCH_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_count)],
            CONFIRM_OUTREACH: [CallbackQueryHandler(handle_outreach_callback)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True
    )

    # Register handlers (order matters!)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("outreach", cmd_run_outreach))
    app.add_handler(CommandHandler("followups", cmd_followups))
    app.add_handler(CommandHandler("meeting", cmd_book_meeting))

    app.add_handler(search_conv)

    # CRM outreach callbacks
    app.add_handler(CallbackQueryHandler(handle_crm_outreach_callback, pattern="^(outreach_crm_all|cancel_outreach)$"))

    # Button router for reply keyboard
    app.add_handler(MessageHandler(
        filters.Regex("^(📧 Run Outreach|📊 CRM Dashboard|📅 Book Meeting|🔄 Send Follow-ups|🤖 AI Chat)$"),
        button_router
    ))

    # General AI message handler (catch-all, must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_message))

    # Background job: auto follow-ups every 6 hours
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(followup_job, interval=21600, first=60)
        log.info("Follow-up scheduler enabled (every 6 hours).")
    else:
        log.warning("Job queue not available — follow-ups must be triggered manually.")

    log.info("🚀 InfluNexus Agent Bot v4 starting...")
    log.info(f"   Telegram: ✅ configured")
    log.info(f"   Gemini AI: {'✅' if GEMINI_API_KEY else '❌'}")
    log.info(f"   Google Sheets: {'✅' if crm.sheet else '❌'}")
    log.info(f"   Apollo: {'✅' if APOLLO_API_KEY else '❌ (skipped)'}")
    log.info(f"   Hunter: {'✅' if HUNTER_API_KEY else '❌ (skipped)'}")
    log.info(f"   SerpAPI: {'✅' if SERPAPI_KEY else '❌ (skipped)'}")
    log.info(f"   GMass: {'✅' if GMASS_API_KEY else '❌ (will use SMTP)'}")
    log.info(f"   SMTP Email: {SMTP_EMAIL}")
    log.info(f"   SMTP Password: {'✅ set' if SMTP_PASSWORD else '❌ NOT SET'}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
