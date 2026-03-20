"""
InfluNexus Autonomous Sales Agent Bot v5
=========================================
Full-stack Telegram bot that:
1. Searches for business contacts (Apollo, Hunter, SerpAPI)
2. Searches Google Maps for local businesses & scrapes their emails
3. Stores leads in Google Sheets CRM
4. Sends personalized outreach emails (GMass bulk + SMTP follow-ups)
5. Books meetings via Google Calendar + Meet
6. Manages follow-up sequences automatically
7. Uses Gemini AI for personalized email generation

Deploy on Railway with environment variables.
"""

import os, json, asyncio, logging, random, re, hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, quote_plus
import httpx
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
GMASS_API_KEY = os.environ.get("GMASS_API_KEY", "")
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "info@influnexus.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
CALENDLY_LINK = os.environ.get("CALENDLY_LINK", "https://influnexus.com")
GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "primary")

_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
try:
    json.loads(_raw); GOOGLE_CREDS_JSON = _raw
except:
    try: _raw = _raw.replace('\\"','"').replace("\\n","\n"); json.loads(_raw); GOOGLE_CREDS_JSON = _raw
    except: GOOGLE_CREDS_JSON = "{}"

INFLUNEXUS_PITCH = """InfluNexus (by Raah Enterprises) is a full-service creative & AI production agency across UAE, India, UK, and global markets.
Services: Cinematic Video Production, CGI/VFX, AI-Generated Visuals, Motion Graphics, Branding, Digital Marketing, Web/App Development, Influencer Marketing.
Past clients include ADCB Bank."""

COMPANY_WEBSITE = "https://influnexus.com"
(SEARCH_INDUSTRY, SEARCH_LOCATION, SEARCH_COUNT, CONFIRM_OUTREACH, CUSTOM_MESSAGE, FOLLOWUP_SETUP) = range(6)
JUNK_EMAILS = {"example.com","email.com","test.com","sentry.io","wixpress.com","domain.com","yourcompany.com","company.com","placeholder.com"}

class CRM:
    HEADERS = ["ID","Company","Contact Name","Email","Phone","Industry","Location","Source","Status","Last Contacted","Next Follow-up","Notes","Created"]
    def __init__(self):
        self.client = None; self.sheet = None; self._connect()
    def _connect(self):
        try:
            cd = json.loads(GOOGLE_CREDS_JSON)
            if not cd or "type" not in cd: log.error("CRM: Invalid JSON"); return
            creds = ServiceAccountCredentials.from_json_keyfile_dict(cd, ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive","https://www.googleapis.com/auth/calendar"])
            self.client = gspread.authorize(creds); self.sheet = self.client.open_by_key(GOOGLE_SHEET_ID)
            self._ensure_ws(); log.info("✅ Google Sheets CRM connected.")
        except Exception as e: log.error(f"CRM failed: {e}")
    def _ensure_ws(self):
        if not self.sheet: return
        ex = [w.title for w in self.sheet.worksheets()]
        if "Leads" not in ex: self.sheet.add_worksheet("Leads",5000,15).append_row(self.HEADERS)
        if "Outreach Log" not in ex: self.sheet.add_worksheet("Outreach Log",5000,10).append_row(["Lead ID","Email","Type","Subject","Status","Sent At","Opened","Replied","Notes"])
        if "Follow-ups" not in ex: self.sheet.add_worksheet("Follow-ups",5000,8).append_row(["Lead ID","Email","Follow-up #","Scheduled Date","Status","Sent At","Content"])
    def _gid(self,d): return hashlib.md5(f"{d.get('email','')}{d.get('company','')}".encode()).hexdigest()[:8].upper()
    def add_lead(self,d):
        lid = self._gid(d)
        if not self.sheet: return lid
        try: self.sheet.worksheet("Leads").append_row([lid,d.get("company",""),d.get("contact_name",""),d.get("email",""),d.get("phone",""),d.get("industry",""),d.get("location",""),d.get("source",""),"New","","",d.get("notes",""),datetime.now().strftime("%Y-%m-%d %H:%M")]); return lid
        except: return lid
    def get_leads_by_status(self,s):
        if not self.sheet: return []
        try: return [r for r in self.sheet.worksheet("Leads").get_all_records() if r.get("Status")==s]
        except: return []
    def update_lead_status(self,lid,s):
        if not self.sheet: return
        try:
            ws=self.sheet.worksheet("Leads"); c=ws.find(lid)
            if c: ws.update_cell(c.row,9,s); ws.update_cell(c.row,10,datetime.now().strftime("%Y-%m-%d %H:%M"))
        except: pass
    def log_outreach(self,lid,email,t,subj):
        if not self.sheet: return
        try: self.sheet.worksheet("Outreach Log").append_row([lid,email,t,subj,"Sent",datetime.now().strftime("%Y-%m-%d %H:%M"),"No","No",""])
        except: pass
    def schedule_followup(self,lid,email,n,days,c):
        if not self.sheet: return
        try: self.sheet.worksheet("Follow-ups").append_row([lid,email,n,(datetime.now()+timedelta(days=days)).strftime("%Y-%m-%d"),"Pending","",c])
        except: pass
    def get_pending_followups(self):
        if not self.sheet: return []
        try:
            t=datetime.now().strftime("%Y-%m-%d")
            return [r for r in self.sheet.worksheet("Follow-ups").get_all_records() if r.get("Status")=="Pending" and r.get("Scheduled Date","")<=t]
        except: return []
    def mark_followup_sent(self,lid,n):
        if not self.sheet: return
        try:
            ws=self.sheet.worksheet("Follow-ups")
            for i,r in enumerate(ws.get_all_records(),start=2):
                if str(r.get("Lead ID"))==str(lid) and str(r.get("Follow-up #"))==str(n): ws.update_cell(i,5,"Sent"); ws.update_cell(i,6,datetime.now().strftime("%Y-%m-%d %H:%M")); break
        except: pass
    def get_all_leads_count(self):
        if not self.sheet: return {"Total":0}
        try:
            recs=self.sheet.worksheet("Leads").get_all_records(); counts={}
            for r in recs: s=r.get("Status","?"); counts[s]=counts.get(s,0)+1
            counts["Total"]=len(recs); return counts
        except: return {"Total":0}

class LeadFinder:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})

    async def find_leads(self, industry, location, count=50):
        leads=[]; tasks=[]
        if APOLLO_API_KEY: tasks.append(self._apollo(industry,location,count))
        if HUNTER_API_KEY: tasks.append(self._hunter(industry,location,count))
        if SERPAPI_KEY: tasks.append(self._serp(industry,location,count)); tasks.append(self._gmaps(industry,location,count))
        if not tasks: return []
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r,list): leads.extend(r)
        seen=set(); unique=[]
        for l in leads:
            e=l.get("email","").lower().strip()
            if e and e not in seen and not any(j in e for j in JUNK_EMAILS): seen.add(e); unique.append(l)
        log.info(f"Total unique leads: {len(unique)}")
        return unique[:count]

    async def _apollo(self,ind,loc,cnt):
        leads=[]
        try:
            r=await self.http.post("https://api.apollo.io/v1/mixed_people/search",json={"api_key":APOLLO_API_KEY,"q_organization_keyword_tags":[ind],"person_locations":[loc],"page":1,"per_page":min(cnt,100),"person_seniorities":["director","vp","c_suite","owner","founder"]})
            for p in r.json().get("people",[]):
                e=p.get("email","")
                if e: leads.append({"company":p.get("organization",{}).get("name",""),"contact_name":p.get("name",""),"email":e,"phone":(p.get("phone_numbers") or [{}])[0].get("sanitized_number",""),"industry":ind,"location":loc,"source":"Apollo.io","title":p.get("title",""),"company_website":p.get("organization",{}).get("website_url",""),"notes":f"Title: {p.get('title','')}"})
            log.info(f"Apollo: {len(leads)}")
        except Exception as e: log.error(f"Apollo: {e}")
        return leads

    async def _hunter(self,ind,loc,cnt):
        leads=[]
        try:
            if not SERPAPI_KEY: return leads
            doms = await self._domains(ind,loc)
            for d in doms[:min(cnt,20)]:
                dm=d.get("domain","")
                if not dm: continue
                try:
                    r=await self.http.get("https://api.hunter.io/v2/domain-search",params={"domain":dm,"api_key":HUNTER_API_KEY,"type":"personal","seniority":"senior,executive","limit":5})
                    for e in r.json().get("data",{}).get("emails",[]):
                        leads.append({"company":r.json().get("data",{}).get("organization",d.get("company","")),"contact_name":f"{e.get('first_name','')} {e.get('last_name','')}".strip(),"email":e.get("value",""),"phone":e.get("phone_number","") or "","industry":ind,"location":loc,"source":"Hunter.io","title":e.get("position",""),"notes":f"Confidence: {e.get('confidence','')}%"})
                except: pass
            log.info(f"Hunter: {len(leads)}")
        except Exception as e: log.error(f"Hunter: {e}")
        return leads

    async def _gmaps(self,ind,loc,cnt):
        leads=[]
        try:
            for q in [f"{ind} in {loc}", f"best {ind} {loc}", f"top {ind} near {loc}"]:
                try:
                    r=await self.http.get("https://serpapi.com/search.json",params={"api_key":SERPAPI_KEY,"q":q,"engine":"google_maps","type":"search"})
                    for p in r.json().get("local_results",[]):
                        email=""
                        if p.get("email"): email=p["email"]
                        elif p.get("website"): email=await self._scrape_email(p["website"])
                        if email: leads.append({"company":p.get("title",""),"contact_name":"","email":email,"phone":p.get("phone",""),"industry":ind,"location":p.get("address","") or loc,"source":"Google Maps","title":"","company_website":p.get("website",""),"notes":f"Rating: {p.get('rating','')} ({p.get('reviews',0)} reviews)"})
                except Exception as e: log.error(f"GMaps query err: {e}")
            log.info(f"Google Maps: {len(leads)}")
        except Exception as e: log.error(f"GMaps: {e}")
        return leads

    async def _scrape_email(self, url):
        try:
            if not url.startswith("http"): url="https://"+url
            parsed=urlparse(url); base=f"{parsed.scheme}://{parsed.netloc}"
            for page in [url, base+"/contact", base+"/contact-us", base+"/about", base+"/about-us"]:
                try:
                    r=await self.http.get(page, follow_redirects=True, timeout=10)
                    if r.status_code!=200: continue
                    emails=re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',r.text)
                    emails+=re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',r.text)
                    valid=[e.lower().strip() for e in emails if not any(j in e.lower() for j in JUNK_EMAILS) and not e.endswith(('.png','.jpg','.svg')) and 'wix' not in e and 'cloudflare' not in e and 'wordpress' not in e and '@2x' not in e and '@3x' not in e and len(e)<60]
                    if valid:
                        for prefix in ['info@','contact@','hello@','sales@','enquiry@','admin@']:
                            for e in valid:
                                if e.startswith(prefix): return e
                        return valid[0]
                except: continue
        except: pass
        return ""

    async def _serp(self,ind,loc,cnt):
        leads=[]
        try:
            for q in [f"{ind} in {loc} email contact", f"top {ind} {loc} email", f"{ind} {loc} business directory"]:
                try:
                    r=await self.http.get("https://serpapi.com/search.json",params={"api_key":SERPAPI_KEY,"q":q,"num":20,"engine":"google"})
                    data=r.json()
                    for res in data.get("organic_results",[]):
                        snip=res.get("snippet",""); title=res.get("title",""); link=res.get("link","")
                        emails=re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',snip)
                        if not emails and link:
                            p=urlparse(link)
                            if not any(x in p.netloc for x in ["linkedin","facebook","twitter","instagram","youtube","wikipedia","yelp","tripadvisor"]):
                                ex=await self._scrape_email(link)
                                if ex: emails=[ex]
                        for e in emails:
                            if not any(j in e.lower() for j in JUNK_EMAILS):
                                leads.append({"company":title.split("-")[0].split("|")[0].strip()[:60],"contact_name":"","email":e,"phone":"","industry":ind,"location":loc,"source":"Google Search","company_website":link,"notes":f"From: {link[:60]}"})
                    for res in data.get("local_results",[]):
                        email=res.get("email",""); ws=res.get("website",res.get("link",""))
                        if not email and ws: email=await self._scrape_email(ws)
                        if email: leads.append({"company":res.get("title",""),"contact_name":"","email":email,"phone":res.get("phone",""),"industry":ind,"location":loc,"source":"Google Local","company_website":ws,"notes":f"Rating: {res.get('rating','')}"})
                except Exception as e: log.error(f"Serp query: {e}")
            log.info(f"SerpAPI: {len(leads)}")
        except Exception as e: log.error(f"Serp: {e}")
        return leads

    async def _domains(self,ind,loc):
        doms=[]
        try:
            r=await self.http.get("https://serpapi.com/search.json",params={"api_key":SERPAPI_KEY,"q":f"{ind} companies in {loc}","num":50,"engine":"google"})
            for res in r.json().get("organic_results",[]):
                l=res.get("link","")
                if l:
                    d=urlparse(l).netloc.replace("www.","")
                    if d and not any(x in d for x in ["linkedin.com","facebook.com","twitter.com","instagram.com","youtube.com","wikipedia.org","yelp.com","google.com","crunchbase.com","tripadvisor.com","zomato.com"]):
                        doms.append({"domain":d,"company":res.get("title","")})
        except: pass
        return doms

class EmailEngine:
    def __init__(self):
        if GEMINI_API_KEY: genai.configure(api_key=GEMINI_API_KEY); self.model=genai.GenerativeModel("gemini-2.0-flash"); log.info("✅ Gemini AI configured.")
        else: self.model=None; log.warning("No GEMINI_API_KEY")
    def generate_email(self,lead,email_type="initial"):
        if email_type=="initial":
            prompt=f"""Write a professional, warm, personalized cold outreach email from Rohit at InfluNexus.
Lead: {lead.get('contact_name','there')} at {lead.get('company','Unknown')} ({lead.get('industry','')}, {lead.get('location','')})
Website: {lead.get('company_website','')}
About us: {INFLUNEXUS_PITCH}
Rules: Under 150 words, warm not salesy, reference their business, soft CTA, include {CALENDLY_LINK}, sign off: Rohit | Founder, InfluNexus | {COMPANY_WEBSITE}
Return ONLY JSON: {{"subject":"...","body":"..."}} No markdown no code fences."""
        elif email_type=="followup_1":
            prompt=f"""Follow-up #1 (day 3) from Rohit at InfluNexus to {lead.get('contact_name','there')} at {lead.get('company','')}.
Under 80 words, reference prev email, new value point. Include {CALENDLY_LINK}. Sign: Rohit | InfluNexus
Return ONLY JSON: {{"subject":"...","body":"..."}}"""
        elif email_type=="followup_2":
            prompt=f"""Follow-up #2 (day 7) from Rohit at InfluNexus to {lead.get('contact_name','there')} at {lead.get('company','')}.
Under 60 words, breakup style. Include {CALENDLY_LINK}. Sign: Rohit | InfluNexus
Return ONLY JSON: {{"subject":"...","body":"..."}}"""
        else: return self._fb(lead)
        try:
            if not self.model: raise Exception("No Gemini")
            t=self.model.generate_content(prompt).text.strip()
            t=re.sub(r'^```json\s*','',t); t=re.sub(r'\s*```$','',t)
            return json.loads(t.strip())
        except Exception as e:
            log.error(f"Gemini: {e}"); return self._fb(lead)
    def _fb(self,l):
        return {"subject":f"Creative & AI Production for {l.get('company','your brand')} — InfluNexus","body":f"Hi {l.get('contact_name','there')},\n\nI'm Rohit from InfluNexus — a creative & AI production agency across UAE, India, and the UK.\n\nI'd love to explore how we could support {l.get('company','your brand')} with premium video, AI visuals, or digital campaigns.\n\nOpen to a quick 15-min chat?\n\nBook a time: {CALENDLY_LINK}\n\nBest,\nRohit\nFounder, InfluNexus\n{COMPANY_WEBSITE}"}
    async def send_gmass(self,leads,ed):
        if not GMASS_API_KEY: return {"success":False,"error":"No GMass key"}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r=await c.post("https://api.gmass.co/api/campaigns",json={"apiKey":GMASS_API_KEY,"subject":ed["subject"],"message":ed["body"],"recipients":[{"EmailAddress":l.get("email",""),"Name":l.get("contact_name",""),"Company":l.get("company","")} for l in leads],"openTracking":True,"clickTracking":True,"sendAs":SMTP_EMAIL})
                if r.status_code==200: return {"success":True,"data":r.json()}
                log.error(f"GMass bulk error: {r.status_code} {r.text[:200]}")
                return {"success":False,"error":r.text[:200]}
        except Exception as e: return {"success":False,"error":str(e)}
    async def send_single(self,to,subj,body):
        """Send single email via GMass API (HTTPS, works on Railway)."""
        if not GMASS_API_KEY:
            log.error("No GMASS_API_KEY set"); return False
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r=await c.post("https://api.gmass.co/api/campaigns",json={
                    "apiKey":GMASS_API_KEY,
                    "subject":subj,
                    "message":body.replace(chr(10),'<br>'),
                    "recipients":[{"EmailAddress":to}],
                    "openTracking":True,"clickTracking":True,"sendAs":SMTP_EMAIL
                })
                if r.status_code==200:
                    log.info(f"✅ Email sent to {to} via GMass")
                    return True
                else:
                    log.error(f"❌ GMass send failed to {to}: {r.status_code} {r.text[:200]}")
                    return False
        except Exception as e:
            log.error(f"❌ GMass error for {to}: {e}")
            return False

class MeetingBooker:
    def __init__(self):
        try:
            cd=json.loads(GOOGLE_CREDS_JSON)
            self.creds=ServiceAccountCredentials.from_json_keyfile_dict(cd,["https://www.googleapis.com/auth/calendar","https://www.googleapis.com/auth/calendar.events"]) if cd and "type" in cd else None
        except: self.creds=None
    async def create_meeting(self,email,name,date,time="10:00"):
        if not self.creds: return {"success":False,"error":"Calendar not configured"}
        try:
            from googleapiclient.discovery import build
            svc=build("calendar","v3",credentials=self.creds); s=datetime.strptime(f"{date} {time}","%Y-%m-%d %H:%M"); e=s+timedelta(minutes=30)
            r=svc.events().insert(calendarId=GCAL_CALENDAR_ID,body={"summary":f"InfluNexus x {name}","start":{"dateTime":s.isoformat(),"timeZone":"Asia/Dubai"},"end":{"dateTime":e.isoformat(),"timeZone":"Asia/Dubai"},"attendees":[{"email":email},{"email":SMTP_EMAIL}],"conferenceData":{"createRequest":{"requestId":hashlib.md5(email.encode()).hexdigest(),"conferenceSolutionKey":{"type":"hangoutsMeet"}}}},conferenceDataVersion=1,sendUpdates="all").execute()
            return {"success":True,"meet_link":r.get("hangoutLink",""),"event_link":r.get("htmlLink","")}
        except Exception as e: return {"success":False,"error":str(e)}

crm=CRM(); lead_finder=LeadFinder(); email_engine=EmailEngine(); meeting_booker=MeetingBooker()

def auth_check(func):
    async def w(update,context):
        if ALLOWED_USER_ID and update.effective_user.id!=ALLOWED_USER_ID: await update.message.reply_text("⛔ Unauthorized."); return ConversationHandler.END
        return await func(update,context)
    return w

@auth_check
async def cmd_start(update,context):
    kb=ReplyKeyboardMarkup([["🔍 Find Leads","📧 Run Outreach"],["📊 CRM Dashboard","📅 Book Meeting"],["🔄 Send Follow-ups","📸 Instagram DM"],["🤖 AI Chat"]],resize_keyboard=True)
    await update.message.reply_text("🚀 *InfluNexus Sales Agent v5*\n\n🔍 *Find Leads* — Any business + location\n  → Google Maps + website email scraping\n📧 *Outreach* — AI-personalized emails\n📊 *Dashboard* — Pipeline stats\n📅 *Meeting* — Google Meet booking\n🔄 *Follow-ups* — Auto sequences\n📸 *Instagram DM* — Cold DM + auto-reply\n🤖 *AI Chat* — Strategy assistant\n\nTry: coffee shops → Dubai → 50",parse_mode="Markdown",reply_markup=kb)

@auth_check
async def cmd_find(update,context):
    await update.message.reply_text("🔍 *Lead Search*\n\nWhat business/industry?\n\nExamples: coffee shops, restaurants, real estate, shoes, hair salons, tech startups, jewelry stores",parse_mode="Markdown")
    return SEARCH_INDUSTRY

async def h_ind(update,context):
    context.user_data["ind"]=update.message.text; await update.message.reply_text("📍 Which city/location?"); return SEARCH_LOCATION

async def h_loc(update,context):
    context.user_data["loc"]=update.message.text; await update.message.reply_text("🔢 How many leads? (10, 50, 100, 500)"); return SEARCH_COUNT

async def h_cnt(update,context):
    try: cnt=int(update.message.text.strip())
    except: await update.message.reply_text("Enter a number."); return SEARCH_COUNT
    context.user_data["cnt"]=cnt; ind=context.user_data["ind"]; loc=context.user_data["loc"]
    await update.message.reply_text(f"⏳ Searching *{cnt}* leads: *{ind}* in *{loc}*...\n\n🔍 Apollo + Hunter + Google + Google Maps\n🌐 Scraping business websites for emails\n\nThis takes 1-3 min...",parse_mode="Markdown")
    leads=await lead_finder.find_leads(ind,loc,cnt)
    if not leads: await update.message.reply_text("😕 No leads found. Try broader terms or bigger city."); return ConversationHandler.END
    context.user_data["leads"]=leads; added=0
    for l in leads: lid=crm.add_lead(l); l["id"]=lid; added+=1
    srcs={}
    for l in leads: s=l.get("source","?"); srcs[s]=srcs.get(s,0)+1
    st="\n".join([f"  • {s}: {c}" for s,c in srcs.items()])
    sample=""
    for i,l in enumerate(leads[:5],1): sample+=f"\n{i}. *{l.get('company','?')}*\n   📧 {l.get('email','?')}\n   📞 {l.get('phone','') or 'N/A'} | 🌐 {(l.get('company_website','') or 'N/A')[:40]}\n"
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📧 Start Outreach to ALL",callback_data="outreach_all")],[InlineKeyboardButton("✅ Save to CRM only",callback_data="save_only")]])
    await update.message.reply_text(f"✅ *Found {len(leads)} leads!*\n\n📊 Sources:\n{st}\n\n📝 Sample:{sample}\n💾 {added} saved to CRM.\n\nWhat next?",parse_mode="Markdown",reply_markup=kb)
    return CONFIRM_OUTREACH

async def h_outreach_cb(update,context):
    q=update.callback_query; await q.answer()
    if q.data=="save_only": await q.edit_message_text("✅ Saved! Use 📧 Run Outreach when ready.",parse_mode="Markdown"); return ConversationHandler.END
    leads=context.user_data.get("leads",[])
    if not leads: await q.edit_message_text("No leads."); return ConversationHandler.END
    await q.edit_message_text(f"📧 *Sending to {len(leads)} leads...*\nGemini AI writing personalized emails...\n~3 sec per email. Please wait.",parse_mode="Markdown")
    ok=0; fail=0; gw=False
    if GMASS_API_KEY and len(leads)>10:
        try:
            s=leads[0]; ed=email_engine.generate_email(s,"initial")
            ed["subject"]=ed["subject"].replace(s.get("company",""),"{Company}"); ed["body"]=ed["body"].replace(s.get("contact_name",""),"{Name}").replace(s.get("company",""),"{Company}")
            r=await email_engine.send_gmass(leads,ed)
            if r["success"]:
                gw=True; ok=len(leads)
                for l in leads: crm.log_outreach(l.get("id",""),l["email"],"GMass",ed["subject"]); crm.update_lead_status(l.get("id",""),"Contacted"); crm.schedule_followup(l.get("id",""),l["email"],1,3,"f1"); crm.schedule_followup(l.get("id",""),l["email"],2,7,"f2")
        except: pass
    if not gw:
        for l in leads:
            try:
                ed=email_engine.generate_email(l,"initial")
                if await email_engine.send_single(l["email"],ed["subject"],ed["body"]):
                    ok+=1; crm.log_outreach(l.get("id",""),l["email"],"SMTP",ed["subject"]); crm.update_lead_status(l.get("id",""),"Contacted"); crm.schedule_followup(l.get("id",""),l["email"],1,3,"f1"); crm.schedule_followup(l.get("id",""),l["email"],2,7,"f2")
                else: fail+=1
            except: fail+=1
            await asyncio.sleep(3)
    await context.bot.send_message(chat_id=q.message.chat_id,text=f"📧 *Outreach Complete!*\n\n✅ Sent: {ok}\n❌ Failed: {fail}\n\n📅 Follow-up 1: Day 3\n📅 Follow-up 2: Day 7",parse_mode="Markdown")
    return ConversationHandler.END

@auth_check
async def cmd_outreach(update,context):
    nl=crm.get_leads_by_status("New")
    if not nl: await update.message.reply_text("📭 No new leads. Use 🔍 Find Leads first.",parse_mode="Markdown"); return
    kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📧 Email {len(nl)} leads",callback_data="outreach_crm_all")],[InlineKeyboardButton("❌ Cancel",callback_data="cancel_outreach")]])
    await update.message.reply_text(f"📧 *{len(nl)}* new leads ready.",parse_mode="Markdown",reply_markup=kb)

async def h_crm_cb(update,context):
    q=update.callback_query; await q.answer()
    if q.data=="cancel_outreach": await q.edit_message_text("Cancelled."); return
    nl=crm.get_leads_by_status("New"); await q.edit_message_text(f"⏳ Sending to {len(nl)}...")
    ok=0
    for l in nl:
        ed=email_engine.generate_email(l,"initial")
        if await email_engine.send_single(l["Email"],ed["subject"],ed["body"]):
            ok+=1; crm.log_outreach(l["ID"],l["Email"],"Initial",ed["subject"]); crm.update_lead_status(l["ID"],"Contacted"); crm.schedule_followup(l["ID"],l["Email"],1,3,"f1"); crm.schedule_followup(l["ID"],l["Email"],2,7,"f2")
        await asyncio.sleep(3)
    await context.bot.send_message(chat_id=q.message.chat_id,text=f"✅ Sent {ok}/{len(nl)}")

@auth_check
async def cmd_followups(update,context):
    p=crm.get_pending_followups()
    if not p: await update.message.reply_text("✅ No follow-ups due!"); return
    await update.message.reply_text(f"🔄 Processing {len(p)} follow-ups...",parse_mode="Markdown")
    ok=0
    for f in p:
        e=f.get("Email",""); n=f.get("Follow-up #",1); l={"email":e,"contact_name":"","company":"","industry":""}
        ed=email_engine.generate_email(l,"followup_1" if n==1 else "followup_2")
        if await email_engine.send_single(e,ed["subject"],ed["body"]): ok+=1; crm.mark_followup_sent(f.get("Lead ID",""),n)
        await asyncio.sleep(3)
    await update.message.reply_text(f"✅ Sent {ok}/{len(p)}")

@auth_check
async def cmd_dash(update,context):
    c=crm.get_all_leads_count(); p=crm.get_pending_followups()
    t=f"📊 *CRM Dashboard*\n\n📈 Total: *{c.get('Total',0)}*\n🆕 New: {c.get('New',0)}\n📧 Contacted: {c.get('Contacted',0)}\n📅 Meeting: {c.get('Meeting Booked',0)}\n✅ Won: {c.get('Won',0)}\n❌ Lost: {c.get('Lost',0)}\n\n🔄 Pending: *{len(p)}*"
    if GOOGLE_SHEET_ID: t+=f"\n\n📄 [Sheet](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID})"
    await update.message.reply_text(t,parse_mode="Markdown",disable_web_page_preview=True)

@auth_check
async def cmd_meet(update,context):
    await update.message.reply_text(f"📅 Send: `email, Name, 2026-03-25, 14:00`\n\nOr: {CALENDLY_LINK}",parse_mode="Markdown")

@auth_check
async def h_meet(update,context):
    t=update.message.text
    if "@" not in t or "," not in t: return
    p=[x.strip() for x in t.split(",")]
    if len(p)<4: await update.message.reply_text("Format: `email, name, date, time`",parse_mode="Markdown"); return
    r=await meeting_booker.create_meeting(p[0],p[1],p[2],p[3])
    if r["success"]: await update.message.reply_text(f"✅ Booked!\n👤 {p[1]}\n📅 {p[2]} {p[3]}\n🔗 {r.get('meet_link','N/A')}",parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ {r.get('error','Failed')}\n\n{CALENDLY_LINK}")

@auth_check
async def cmd_ai(update,context):
    await update.message.reply_text("🤖 *AI Mode* — Ask anything about sales/strategy!",parse_mode="Markdown")

@auth_check
async def h_ai(update,context):
    t=update.message.text
    if t.startswith("/") or t in ["🔍 Find Leads","📧 Run Outreach","📊 CRM Dashboard","📅 Book Meeting","🔄 Send Follow-ups","🤖 AI Chat"]: return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,action="typing")
    try:
        m=genai.GenerativeModel("gemini-2.0-flash",system_instruction=f"You are Rohit's AI sales assistant at InfluNexus. Concise. {INFLUNEXUS_PITCH}")
        r=m.generate_content(t).text
        if len(r)>4000: r=r[:4000]+"..."
        await update.message.reply_text(r,parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"Error: {e}")

async def btn_route(update,context):
    routes={"🔍 Find Leads":cmd_find,"📧 Run Outreach":cmd_outreach,"📊 CRM Dashboard":cmd_dash,"📅 Book Meeting":cmd_meet,"🔄 Send Follow-ups":cmd_followups,"🤖 AI Chat":cmd_ai}
    h=routes.get(update.message.text)
    if h: return await h(update,context)
    if update.message.text=="📸 Instagram DM":
        try:
            from insta_bot import cmd_ig_start
            return await cmd_ig_start(update,context)
        except: await update.message.reply_text("Instagram module not available. Check IG_USERNAME and IG_PASSWORD env vars.")

async def fu_job(context):
    p=crm.get_pending_followups()
    if not p: return
    ok=0
    for f in p:
        e=f.get("Email",""); n=f.get("Follow-up #",1); l={"email":e,"contact_name":"","company":"","industry":""}
        ed=email_engine.generate_email(l,"followup_1" if n==1 else "followup_2")
        if await email_engine.send_single(e,ed["subject"],ed["body"]): ok+=1; crm.mark_followup_sent(f.get("Lead ID",""),n)
        await asyncio.sleep(3)
    if ALLOWED_USER_ID and ok>0:
        try: await context.bot.send_message(chat_id=ALLOWED_USER_ID,text=f"🔄 Auto follow-up: {ok}/{len(p)} sent!")
        except: pass

def main():
    app=Application.builder().token(TELEGRAM_TOKEN).build()

    # Import and setup Instagram handlers
    try:
        from insta_bot import setup_ig_handlers
        setup_ig_handlers(app)
        ig_status = "✅"
    except Exception as e:
        log.warning(f"Instagram module not loaded: {e}")
        ig_status = "❌"

    sc=ConversationHandler(entry_points=[CommandHandler("find",cmd_find),MessageHandler(filters.Regex("^🔍 Find Leads$"),cmd_find)],states={SEARCH_INDUSTRY:[MessageHandler(filters.TEXT&~filters.COMMAND,h_ind)],SEARCH_LOCATION:[MessageHandler(filters.TEXT&~filters.COMMAND,h_loc)],SEARCH_COUNT:[MessageHandler(filters.TEXT&~filters.COMMAND,h_cnt)],CONFIRM_OUTREACH:[CallbackQueryHandler(h_outreach_cb)]},fallbacks=[CommandHandler("cancel",lambda u,c:ConversationHandler.END)],allow_reentry=True)
    app.add_handler(CommandHandler("start",cmd_start)); app.add_handler(CommandHandler("help",cmd_start))
    app.add_handler(CommandHandler("dashboard",cmd_dash)); app.add_handler(CommandHandler("outreach",cmd_outreach))
    app.add_handler(CommandHandler("followups",cmd_followups)); app.add_handler(CommandHandler("meeting",cmd_meet))
    app.add_handler(sc)
    app.add_handler(CallbackQueryHandler(h_crm_cb,pattern="^(outreach_crm_all|cancel_outreach)$"))
    app.add_handler(MessageHandler(filters.Regex("^(📧 Run Outreach|📊 CRM Dashboard|📅 Book Meeting|🔄 Send Follow-ups|🤖 AI Chat|📸 Instagram DM)$"),btn_route))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,h_ai))
    jq=app.job_queue
    if jq: jq.run_repeating(fu_job,interval=21600,first=60); log.info("✅ Follow-up scheduler on (6h)")
    log.info("🚀 InfluNexus Agent Bot v5 starting...")
    log.info(f"   Telegram: ✅ | Gemini: {'✅' if GEMINI_API_KEY else '❌'} | Sheets: {'✅' if crm.sheet else '❌'}")
    log.info(f"   Apollo: {'✅' if APOLLO_API_KEY else '⚪'} | Hunter: {'✅' if HUNTER_API_KEY else '⚪'} | SerpAPI: {'✅' if SERPAPI_KEY else '❌'}")
    log.info(f"   SMTP: {SMTP_EMAIL} | Pass: {'✅' if SMTP_PASSWORD else '❌'} | GMass: {'✅' if GMASS_API_KEY else '⚪'}")
    log.info(f"   Instagram: {ig_status}")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
