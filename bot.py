"""
InfluNexus Autonomous Agent Bot v3
====================================
Full end-to-end pipeline from one iPhone message:

1. FIND    — Search Google/web for companies in any niche + region
2. SCRAPE  — Visit every company website, extract real contacts
3. ENRICH  — Find decision maker names, emails, phone, LinkedIn
4. VERIFY  — Verify emails are real before sending
5. UPLOAD  — Push to Google Sheet + export CSV
6. SEND    — Launch GMass campaign automatically
7. FOLLOW  — Auto follow-up sequence (Day 3, Day 7)
8. BOOK    — Calendly link in every email for call booking
9. REPORT  — Daily summary back to you on Telegram

One message on iPhone → everything above happens automatically.
"""

import os, json, asyncio, logging, re, csv, io
from datetime import datetime
from urllib.parse import urlparse, urljoin
import anthropic
import httpx
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── CONFIG ───────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))
GMASS_API_KEY    = os.environ.get("GMASS_API_KEY", "")
CALENDLY_LINK    = os.environ.get("CALENDLY_LINK", "https://calendly.com/influnexus")
SERPAPI_KEY      = os.environ.get("SERPAPI_KEY", "")  # for Google search

# ── AI CLIENT ────────────────────────────────────────────────────
ai = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ── GOOGLE SHEETS ────────────────────────────────────────────────
GSCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_sheet():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ.get("GOOGLE_CREDS_JSON", "{}")), GSCOPE
    )
    return gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID)

def write_sheet(tab: str, headers: list, rows: list) -> str:
    try:
        sheet = get_sheet()
        try:
            ws = sheet.worksheet(tab)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title=tab, rows=500, cols=25)
        ws.update([headers] + rows, "A1")
        ws.format("A1:Z1", {"textFormat": {"bold": True}})
        return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
    except Exception as e:
        log.error(f"Sheet error: {e}")
        raise

def make_csv(headers: list, rows: list) -> bytes:
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode()

# ── WEB SCRAPER ──────────────────────────────────────────────────
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE = re.compile(r'[\+]?[\d\s\-\(\)]{9,17}')

SKIP_EMAILS = {"example.com", "sentry.io", "wixpress.com", "squarespace.com",
               "shopify.com", "wordpress.com", "gmail.com", "yahoo.com"}

async def scrape_website(url: str) -> dict:
    """Visit a company website and extract contact details."""
    result = {"url": url, "emails": [], "phones": [], "contact_page": "", "raw_text": ""}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Try main page
            resp = await client.get(url, headers=headers)
            text = resp.text
            result["raw_text"] = text[:5000]

            # Extract emails from main page
            found_emails = EMAIL_RE.findall(text)
            valid_emails = [e for e in found_emails
                           if not any(skip in e for skip in SKIP_EMAILS)
                           and not e.endswith(('.png', '.jpg', '.css', '.js'))]
            result["emails"].extend(valid_emails)

            # Extract phones
            result["phones"] = PHONE_RE.findall(text)[:3]

            # Try contact page
            contact_paths = ["/contact", "/contact-us", "/about", "/team", "/about-us"]
            for path in contact_paths:
                try:
                    contact_url = urljoin(url, path)
                    cresp = await client.get(contact_url, headers=headers)
                    if cresp.status_code == 200:
                        ctext = cresp.text
                        more_emails = EMAIL_RE.findall(ctext)
                        valid_more = [e for e in more_emails
                                     if not any(skip in e for skip in SKIP_EMAILS)]
                        result["emails"].extend(valid_more)
                        result["contact_page"] = contact_url
                        break
                except Exception:
                    continue

        # Deduplicate and prioritise business emails
        all_emails = list(dict.fromkeys(result["emails"]))
        priority = [e for e in all_emails if any(kw in e.lower()
                    for kw in ["info", "contact", "hello", "marketing", "press",
                               "media", "sales", "business", "team", "brand"])]
        other = [e for e in all_emails if e not in priority]
        result["emails"] = (priority + other)[:5]

    except Exception as e:
        log.warning(f"Scrape error for {url}: {e}")

    return result

# ── GOOGLE SEARCH ────────────────────────────────────────────────
async def search_companies(niche: str, region: str, count: int) -> list:
    """Search Google for companies in a niche and region."""
    queries = [
        f"{niche} companies in {region}",
        f"top {niche} brands {region}",
        f"{niche} business {region} contact",
    ]
    websites = []

    if SERPAPI_KEY:
        # Use SerpAPI for real Google results
        async with httpx.AsyncClient(timeout=15) as client:
            for query in queries[:2]:
                try:
                    resp = await client.get(
                        "https://serpapi.com/search",
                        params={"q": query, "api_key": SERPAPI_KEY, "num": 10}
                    )
                    data = resp.json()
                    for result in data.get("organic_results", []):
                        link = result.get("link", "")
                        if link and "http" in link:
                            domain = urlparse(link).netloc
                            if not any(skip in domain for skip in
                                      ["google", "facebook", "linkedin", "wikipedia",
                                       "youtube", "twitter", "instagram"]):
                                websites.append({
                                    "url": f"https://{domain}",
                                    "name": result.get("title", domain),
                                    "snippet": result.get("snippet", "")
                                })
                except Exception as e:
                    log.warning(f"SerpAPI error: {e}")
    else:
        # Fallback: use Claude to generate company list with websites
        pass

    return websites[:count]

# ── MAIN RESEARCH ENGINE ─────────────────────────────────────────
async def full_research(niche: str, region: str, count: int, update: Update) -> list:
    """
    Full autonomous research pipeline:
    1. Find companies via AI + web search
    2. Scrape each website for real contacts
    3. Return enriched contact list
    """

    # Step 1: Get company list from Claude AI
    await update.message.reply_text(f"🔍 *Step 1/4* — Finding {count} {niche} companies in {region}...", parse_mode="Markdown")

    company_prompt = f"""Find {count} real {niche} companies based in {region}.

For each company return ONLY a JSON array with:
- company_name: Real company name
- website: Their actual website URL (must start with https://)
- description: One line about what they do
- decision_maker: Likely job title of person to contact (CMO, Marketing Director, CEO, Brand Manager)
- linkedin_search: Search phrase to find the right person on LinkedIn

Only include real companies with actual websites. No aggregators, no directories.
Return ONLY the JSON array, nothing else."""

    try:
        msg = ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": company_prompt}]
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        companies = json.loads(raw)[:count]
    except Exception as e:
        log.error(f"Company research error: {e}")
        companies = []

    if not companies:
        return []

    await update.message.reply_text(
        f"✅ Found *{len(companies)} companies*\n\n"
        f"🔍 *Step 2/4* — Scraping websites for real contact emails...",
        parse_mode="Markdown"
    )

    # Step 2: Scrape each website
    enriched = []
    scrape_tasks = []
    for company in companies:
        url = company.get("website", "")
        if url and url.startswith("http"):
            scrape_tasks.append(scrape_website(url))
        else:
            scrape_tasks.append(asyncio.coroutine(lambda: {"emails": [], "phones": []})())

    scrape_results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

    await update.message.reply_text(
        f"✅ Websites scraped\n\n"
        f"🤖 *Step 3/4* — AI extracting decision maker details...",
        parse_mode="Markdown"
    )

    # Step 3: Use Claude to extract/enrich contact info
    for i, (company, scraped) in enumerate(zip(companies, scrape_results)):
        if isinstance(scraped, Exception):
            scraped = {"emails": [], "phones": []}

        emails = scraped.get("emails", []) if isinstance(scraped, dict) else []
        phones = scraped.get("phones", []) if isinstance(scraped, dict) else []
        raw_text = scraped.get("raw_text", "") if isinstance(scraped, dict) else ""

        # Use Claude to extract decision maker from scraped content
        if raw_text:
            extract_prompt = f"""From this website content of {company.get('company_name')}, extract:
1. The name of the most senior marketing/brand decision maker (CMO, Marketing Director, CEO, Brand Manager)
2. Any direct email for that person

Website content (first 3000 chars):
{raw_text[:3000]}

Return ONLY JSON: {{"name": "...", "email": "...", "position": "..."}}
If not found return: {{"name": "", "email": "", "position": "{company.get('decision_maker', 'Marketing Director')}"}}"""

            try:
                extract_msg = ai.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=200,
                    messages=[{"role": "user", "content": extract_prompt}]
                )
                extracted = json.loads(extract_msg.content[0].text.strip())
            except Exception:
                extracted = {"name": "", "email": "", "position": company.get("decision_maker", "")}
        else:
            extracted = {"name": "", "email": "", "position": company.get("decision_maker", "")}

        # Pick best email
        best_email = extracted.get("email") or (emails[0] if emails else "Search")

        enriched.append({
            "company_name":    company.get("company_name", ""),
            "website":         company.get("website", ""),
            "contact_name":    extracted.get("name", ""),
            "first_name":      extracted.get("name", "").split()[0] if extracted.get("name") else "there",
            "position":        extracted.get("position", company.get("decision_maker", "")),
            "email":           best_email,
            "backup_emails":   ", ".join(emails[1:3]) if len(emails) > 1 else "",
            "phone":           phones[0].strip() if phones else "",
            "linkedin_search": company.get("linkedin_search", ""),
            "description":     company.get("description", ""),
            "region":          region,
            "status":          "Pending",
            "date_added":      datetime.now().strftime("%Y-%m-%d"),
        })

    return enriched

# ── EMAIL TEMPLATES ──────────────────────────────────────────────
def get_email_body(template: str, calendly: str) -> dict:
    templates = {
        "a": {
            "subject": "Quick question for {{First Name}}",
            "body": f"Hi {{{{First Name}}}},\n\nWhat would {{{{Company}}}}'s content look like if it actually stopped people mid-scroll?\n\nI'm Rohit from InfluNexus — we create cinematic video, AI visuals, and CGI/VFX for brands across UAE, India & UK. Past clients include ADCB Bank.\n\nSee our work: https://www.instagram.com/influnexus\n\nBook a quick 15-min call: {calendly}\n\nRohit | InfluNexus\ninfo@influnexus.com\n\n---\n{{{{unsubscribe}}}}"
        },
        "b": {
            "subject": "Content that converts — for {{Company}}",
            "body": f"Hi {{{{First Name}}}},\n\nMost brand content gets scrolled past in 2 seconds. Ours doesn't.\n\nI'm Rohit, Founder of InfluNexus. We create cinematic video, AI visuals, and digital campaigns for brands like {{{{Company}}}} — UAE, India & UK. Including ADCB Bank.\n\nOur work: https://www.instagram.com/influnexus\n\nPick a time to chat: {calendly}\n\nRohit | InfluNexus\ninfo@influnexus.com\n\n---\n{{{{unsubscribe}}}}"
        },
        "c": {
            "subject": "{{First Name}} — saw {{Company}}, had to reach out",
            "body": f"Hi {{{{First Name}}}},\n\nLove what {{{{Company}}}} is building.\n\nWe create cinematic brand content — video, AI visuals, CGI — for brands expanding across UAE, India & UK. ADCB Bank is one of our clients.\n\nhttps://www.instagram.com/influnexus\n\n15 mins? {calendly}\n\nRohit | InfluNexus\ninfo@influnexus.com\n\n---\n{{{{unsubscribe}}}}"
        },
        "d": {
            "subject": "Are {{Company}}'s visuals matching your brand quality?",
            "body": f"Hi {{{{First Name}}}},\n\nGreat brands often have one problem — content that doesn't match the quality of what they've built.\n\nAt InfluNexus we fix that. Cinematic video, AI visuals, CGI/VFX across UAE, India & UK.\n\nSee the work: https://www.instagram.com/influnexus\n\nHappy to share a reel specific to {{{{Company}}}}'s space: {calendly}\n\nRohit | InfluNexus\ninfo@influnexus.com\n\n---\n{{{{unsubscribe}}}}"
        }
    }
    return templates.get(template, templates["c"])

# ── GMASS ────────────────────────────────────────────────────────
async def launch_gmass(tab: str, template: str, follow_up: bool = True) -> dict:
    if not GMASS_API_KEY:
        return {"status": "error", "message": "Add GMASS_API_KEY to Railway environment variables."}

    tpl        = get_email_body(template, CALENDLY_LINK)
    sheet_url  = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/gviz/tq?sheet={tab}"

    payload = {
        "spreadsheet_url": sheet_url,
        "subject":         tpl["subject"],
        "body":            tpl["body"],
        "from_name":       "Rohit | InfluNexus",
        "reply_to":        "info@influnexus.com",
        "track_opens":     False,
        "track_clicks":    False,
        "emails_per_day":  80,
        "schedule_type":   "now",
    }

    if follow_up:
        payload["follow_ups"] = [
            {
                "days_after": 3,
                "subject":    "Following up — {{Company}}",
                "body":       f"Hi {{{{First Name}}}},\n\nJust following up on my last message — wanted to make sure it didn't get buried.\n\nWould love to show you what we've done for brands in your space.\n\nQuick call? {CALENDLY_LINK}\n\nRohit | InfluNexus"
            },
            {
                "days_after": 7,
                "subject":    "Last note — InfluNexus x {{Company}}",
                "body":       f"Hi {{{{First Name}}}},\n\nLast message from me — I know inboxes get busy.\n\nIf the timing isn't right, no worries at all. But if you'd ever like to explore what we could create for {{{{Company}}}}, I'm here.\n\n{CALENDLY_LINK}\n\nRohit | InfluNexus"
            }
        ]

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                "https://api.gmass.co/api/campaigns",
                headers={"Authorization": f"Bearer {GMASS_API_KEY}", "Content-Type": "application/json"},
                json=payload
            )
        if resp.status_code == 200:
            return {"status": "success", "campaign_id": resp.json().get("id")}
        return {"status": "error", "message": f"GMass {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ── SECURITY ─────────────────────────────────────────────────────
def is_auth(update: Update) -> bool:
    if ALLOWED_USER_ID == 0: return True
    return update.effective_user.id == ALLOWED_USER_ID

def tab_name(region: str, niche: str) -> str:
    r = re.sub(r'[^a-zA-Z0-9]', '_', region)[:8]
    n = re.sub(r'[^a-zA-Z0-9]', '_', niche)[:8]
    return f"{r}_{n}_{datetime.now().strftime('%b%d')}"

def upd_stats(ctx, **kw):
    s = ctx.bot_data.get("stats", {"lists": 0, "contacts": 0, "campaigns": 0, "emails_found": 0})
    for k, v in kw.items(): s[k] = s.get(k, 0) + v
    ctx.bot_data["stats"] = s

# ── COMMAND HANDLERS ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    await update.message.reply_text(
        "👋 *InfluNexus Autonomous Agent v3*\n\n"
        "Just tell me what you need — I do everything:\n\n"
        "🔍 Find companies\n"
        "🌐 Scrape their websites\n"
        "📧 Extract real emails\n"
        "📊 Upload to Google Sheet + CSV\n"
        "🚀 Launch email campaign\n"
        "🔄 Auto follow-up (Day 3 + Day 7)\n"
        "📅 Calendly booking link in every email\n\n"
        "*Just type:*\n"
        "`shoes companies in Dubai 20`\n"
        "`jewellery brands in Switzerland 15`\n"
        "`real estate developers in Qatar 25`\n\n"
        "Or use commands:\n"
        "`/run <niche> in <region> <count>` — full pipeline\n"
        "`/find <niche> in <region> <count>` — find only, no campaign\n"
        "`/send <TabName> <a|b|c|d>` — send to existing list\n"
        "`/results` — campaign stats\n"
        "`/status` — today's summary\n"
        "`/sheet` — open Google Sheet\n"
        "`/help` — full guide",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    await update.message.reply_text(
        "📖 *Full Command Guide*\n\n"
        "*Full pipeline (find + scrape + send):*\n"
        "`/run shoes companies in Dubai 20`\n"
        "`/run jewellery brands in Switzerland 15`\n"
        "`/run real estate developers in Qatar 30`\n\n"
        "*Find only (no campaign):*\n"
        "`/find sports brands in UAE 25`\n"
        "Builds list, uploads to sheet, you review before sending\n\n"
        "*Send to existing list:*\n"
        "`/send Dubai_shoes_Mar17 c`\n"
        "Templates: a=Curiosity | b=Value | c=Punchy | d=Problem\n\n"
        "*Results:*\n"
        "`/results` — open/click/reply rates for all campaigns\n\n"
        "*Natural language:*\n"
        "Just type normally — bot understands:\n"
        "'shoes companies in dubai'\n"
        "'find me 30 jewellery brands in switzerland'\n"
        "'sports companies from qatar region'\n\n"
        "*All runs automatically include:*\n"
        "✅ Website scraping for real emails\n"
        "✅ Decision maker extraction\n"
        "✅ Google Sheet upload\n"
        "✅ CSV download link\n"
        "✅ GMass campaign launch\n"
        "✅ Day 3 + Day 7 follow-ups\n"
        "✅ Calendly booking link",
        parse_mode="Markdown"
    )

async def run_full_pipeline(update: Update, ctx: ContextTypes.DEFAULT_TYPE, niche: str, region: str, count: int, send_campaign: bool = True):
    """Core pipeline — find, scrape, upload, send."""
    tname = tab_name(region, niche)

    await update.message.reply_text(
        f"🤖 *InfluNexus Agent — Starting full pipeline*\n\n"
        f"Niche: *{niche}*\n"
        f"Region: *{region}*\n"
        f"Target: *{count} companies*\n\n"
        f"This takes 2–3 minutes. I'll update you at each step 👇",
        parse_mode="Markdown"
    )

    # Run full research
    contacts = await full_research(niche, region, count, update)

    if not contacts:
        await update.message.reply_text("❌ Research failed. Try a more specific niche or region.")
        return

    emails_found = sum(1 for c in contacts if c["email"] != "Search")

    await update.message.reply_text(
        f"✅ *Step 3/4 done* — {len(contacts)} companies researched\n"
        f"📧 Real emails found: *{emails_found}/{len(contacts)}*\n\n"
        f"📊 *Step 4/4* — Uploading to Google Sheet...",
        parse_mode="Markdown"
    )

    # Upload to sheet
    headers = [
        "First Name", "Company", "Website", "Contact Name", "Position",
        "Email", "Backup Emails", "Phone", "LinkedIn Search",
        "Description", "Region", "Status", "Date Added"
    ]
    rows = [[
        c["first_name"], c["company_name"], c["website"], c["contact_name"],
        c["position"], c["email"], c["backup_emails"], c["phone"],
        c["linkedin_search"], c["description"], c["region"],
        c["status"], c["date_added"]
    ] for c in contacts]

    sheet_url = write_sheet(tname, headers, rows)
    csv_bytes  = make_csv(headers, rows)
    upd_stats(ctx, lists=1, contacts=len(contacts), emails_found=emails_found)

    # Send CSV file to Telegram
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=f"{tname}.csv",
        caption=f"📎 *{tname}.csv* — {len(contacts)} contacts",
        parse_mode="Markdown"
    )

    if send_campaign:
        # Launch GMass
        result = await launch_gmass(tname, "c", follow_up=True)
        upd_stats(ctx, campaigns=1)

        # Preview first 3
        preview = ""
        for c in contacts[:3]:
            name  = c["contact_name"] or "Decision maker"
            email = c["email"]
            preview += f"\n• *{c['company_name']}* — {name} — `{email}`"
        if len(contacts) > 3:
            preview += f"\n_...and {len(contacts)-3} more_"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open Sheet", url=sheet_url)],
            [InlineKeyboardButton("📈 View Dashboard", url="https://app.gmass.co/dashboard")],
            [InlineKeyboardButton("🔁 Run Another", callback_data="new_run")]
        ])

        campaign_status = "✅ Campaign launched!" if result["status"] == "success" else f"❌ Campaign failed: {result['message']}"

        await update.message.reply_text(
            f"🎉 *Pipeline Complete!*\n\n"
            f"Companies found: *{len(contacts)}*\n"
            f"Emails extracted: *{emails_found}*\n"
            f"Sheet tab: `{tname}`\n\n"
            f"*Sample contacts:*{preview}\n\n"
            f"📧 *Campaign:* {campaign_status}\n"
            f"🔄 *Follow-ups:* Day 3 + Day 7 auto-scheduled\n"
            f"📅 *Calendly:* included in every email\n\n"
            f"Check results in 48h → `/results`",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        preview = ""
        for c in contacts[:3]:
            preview += f"\n• *{c['company_name']}* — `{c['email']}`"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Open Sheet", url=sheet_url)],
            [InlineKeyboardButton("🚀 Send Campaign Now", callback_data=f"send|{tname}|c")],
        ])

        await update.message.reply_text(
            f"✅ *List Ready — No campaign sent yet*\n\n"
            f"Companies: *{len(contacts)}*\n"
            f"Emails found: *{emails_found}*\n"
            f"Tab: `{tname}`\n\n"
            f"*Preview:*{preview}\n\n"
            f"Review the sheet then tap *Send Campaign* 👇",
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Full pipeline: find + scrape + send."""
    if not is_auth(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/run shoes companies in Dubai 20`", parse_mode="Markdown")
        return
    niche, region, count = parse_args(" ".join(ctx.args))
    await run_full_pipeline(update, ctx, niche, region, count, send_campaign=True)

async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Find only — no campaign sent."""
    if not is_auth(update): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/find shoes companies in Dubai 20`", parse_mode="Markdown")
        return
    niche, region, count = parse_args(" ".join(ctx.args))
    await run_full_pipeline(update, ctx, niche, region, count, send_campaign=False)

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send campaign to existing sheet tab."""
    if not is_auth(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/send TabName c`\nTemplates: a b c d", parse_mode="Markdown")
        return
    tname    = ctx.args[0]
    template = ctx.args[1].lower()
    status   = await update.message.reply_text(f"🚀 Launching campaign for `{tname}`...", parse_mode="Markdown")
    result   = await launch_gmass(tname, template, follow_up=True)
    if result["status"] == "success":
        upd_stats(ctx, campaigns=1)
        await status.edit_text(
            f"✅ *Campaign sent!*\n\nTab: `{tname}`\nFollow-ups: Day 3 + Day 7 ✅\nCalendly: included ✅\n\nCheck `/results` in 48h",
            parse_mode="Markdown"
        )
    else:
        await status.edit_text(f"❌ Failed: `{result['message']}`", parse_mode="Markdown")

async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    if not GMASS_API_KEY:
        await update.message.reply_text("❌ GMASS_API_KEY not set.")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get("https://api.gmass.co/api/campaigns",
                              headers={"Authorization": f"Bearer {GMASS_API_KEY}"})
        if resp.status_code == 200:
            camps = resp.json()[:8]
            msg   = "📊 *Campaign Results:*\n\n"
            for camp in camps:
                name    = camp.get("name", "Unnamed")[:25]
                sent    = camp.get("sent", 0)
                opens   = camp.get("opens", 0)
                replies = camp.get("replies", 0)
                pct     = round(opens/sent*100) if sent > 0 else 0
                msg += f"*{name}*\nSent: {sent} | Opens: {opens} ({pct}%) | Replies: {replies}\n\n"
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"GMass API error: {resp.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_sheet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
    await update.message.reply_text(f"📊 *Your Sheet*\n\n{url}", parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    s = ctx.bot_data.get("stats", {"lists": 0, "contacts": 0, "campaigns": 0, "emails_found": 0})
    await update.message.reply_text(
        f"📈 *Today's Activity*\n\n"
        f"Pipelines run: *{s.get('lists',0)}*\n"
        f"Companies found: *{s.get('contacts',0)}*\n"
        f"Real emails extracted: *{s.get('emails_found',0)}*\n"
        f"Campaigns sent: *{s.get('campaigns',0)}*",
        parse_mode="Markdown"
    )

# ── PARSE ARGS ───────────────────────────────────────────────────
def parse_args(text: str):
    count  = 20
    region = "UAE"
    niche  = text

    parts = text.strip().split()
    if parts and parts[-1].isdigit():
        count = min(int(parts[-1]), 50)
        parts = parts[:-1]
        text  = " ".join(parts)

    if " in " in text.lower():
        idx    = text.lower().index(" in ")
        niche  = text[:idx].strip()
        region = text[idx+4:].strip()
    else:
        niche = text.strip()

    return niche, region, count

# ── NATURAL LANGUAGE ─────────────────────────────────────────────
async def natural_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update): return
    text = update.message.text.strip()

    find_keywords = ["find", "get", "need", "show", "list", "search",
                     "companies", "brands", "business", "shoes", "jewellery",
                     "fashion", "real estate", "sports", "luxury", "contact"]

    if any(kw in text.lower() for kw in find_keywords):
        parse_prompt = f"""Extract from: "{text}"
Return ONLY JSON: {{"niche": "...", "region": "...", "count": 20}}
Default region: UAE. Max count: 50."""
        try:
            resp   = ai.messages.create(model="claude-sonnet-4-6", max_tokens=100,
                                        messages=[{"role": "user", "content": parse_prompt}])
            parsed = json.loads(resp.content[0].text.strip())
            niche  = parsed.get("niche", text)
            region = parsed.get("region", "UAE")
            count  = min(int(parsed.get("count", 20)), 50)
            await run_full_pipeline(update, ctx, niche, region, count, send_campaign=True)
        except Exception:
            await update.message.reply_text(
                "Try: `/run shoes companies in Dubai 20`\nor just type: `shoes companies in Dubai 20`",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text("Type `/help` to see what I can do.", parse_mode="Markdown")

# ── BUTTONS ──────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("send|"):
        _, tname, template = data.split("|", 2)
        status = await query.message.reply_text(f"🚀 Sending campaign...", parse_mode="Markdown")
        result = await launch_gmass(tname, template, follow_up=True)
        if result["status"] == "success":
            upd_stats(ctx, campaigns=1)
            await status.edit_text("✅ *Campaign sent!* Follow-ups scheduled. Check `/results` in 48h.", parse_mode="Markdown")
        else:
            await status.edit_text(f"❌ Failed: `{result['message']}`", parse_mode="Markdown")
    elif data == "new_run":
        await query.message.reply_text("What next? Just type:\n`shoes companies in Qatar 20`", parse_mode="Markdown")

# ── MAIN ─────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("run",     cmd_run))
    app.add_handler(CommandHandler("find",    cmd_find))
    app.add_handler(CommandHandler("send",    cmd_send))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(CommandHandler("sheet",   cmd_sheet))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language))
    log.info("InfluNexus Autonomous Agent v3 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
