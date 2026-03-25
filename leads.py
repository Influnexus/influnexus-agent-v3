import os
import re
import logging
import asyncio
import aiohttp

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# Conversation states
LEADS_INDUSTRY = 0
LEADS_LOCATION = 1
LEADS_COUNT = 2

# Shared timeout for all API calls
API_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Email regex for scraping websites
EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# Domains to ignore when scraping emails
JUNK_EMAIL_DOMAINS = {
    "example.com", "email.com", "yourdomain.com", "domain.com",
    "sentry.io", "wixpress.com", "googleapis.com", "w3.org",
    "schema.org", "gravatar.com", "wordpress.org", "wp.com",
    "cloudflare.com", "google.com", "facebook.com", "twitter.com",
}


async def scrape_website_emails(url: str) -> list[str]:
    """Scrape a website page for email addresses."""
    if not url:
        return []

    # Ensure URL has protocol
    if not url.startswith("http"):
        url = "https://" + url

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; InflunexusBot/1.0)"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Try the main page first
            emails = set()
            for path in ["", "/contact", "/contact-us", "/about", "/about-us"]:
                try:
                    target = url.rstrip("/") + path
                    async with session.get(target, headers=headers, allow_redirects=True, ssl=False) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text(errors="ignore")
                        found = EMAIL_REGEX.findall(text)
                        for em in found:
                            em_lower = em.lower()
                            domain = em_lower.split("@")[1]
                            # Filter out junk emails
                            if domain not in JUNK_EMAIL_DOMAINS and not em_lower.endswith((".png", ".jpg", ".gif", ".css", ".js")):
                                emails.add(em_lower)
                except Exception:
                    continue

                # Stop if we found emails
                if emails:
                    break

            result = list(emails)[:3]  # Max 3 emails per site
            if result:
                logger.info(f"Scraped {len(result)} emails from {url}: {result}")
            return result

    except Exception as e:
        logger.debug(f"Scrape error for {url}: {e}")
        return []


async def find_email_hunter(first_name: str, last_name: str, company_domain: str) -> str:
    """Use Hunter.io email finder to get a specific person's email."""
    if not HUNTER_API_KEY or not company_domain:
        return ""

    url = (
        f"https://api.hunter.io/v2/email-finder"
        f"?domain={company_domain}"
        f"&first_name={first_name}"
        f"&last_name={last_name}"
        f"&api_key={HUNTER_API_KEY}"
    )

    try:
        async with aiohttp.ClientSession(timeout=API_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Hunter email-finder {resp.status} for {company_domain}")
                    return ""
                data = await resp.json()
                return data.get("data", {}).get("email", "") or ""
    except Exception as e:
        logger.error(f"Hunter email-finder error: {e}")
        return ""


async def enrich_with_hunter(domain: str) -> list[dict]:
    """Use Hunter.io domain search to find all emails at a domain."""
    if not HUNTER_API_KEY or not domain:
        return []

    url = (
        f"https://api.hunter.io/v2/domain-search"
        f"?domain={domain}&api_key={HUNTER_API_KEY}"
    )

    try:
        async with aiohttp.ClientSession(timeout=API_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Hunter domain-search {resp.status} for {domain}")
                    return []
                data = await resp.json()

        leads = []
        for e in data.get("data", {}).get("emails", []):
            first = e.get("first_name", "") or ""
            last = e.get("last_name", "") or ""
            leads.append({
                "name": f"{first} {last}".strip(),
                "email": e.get("value", "") or "",
                "phone": "",
                "company": domain,
                "title": e.get("position", "") or "",
                "linkedin": "",
                "source": "Hunter",
            })
        return leads
    except Exception as e:
        logger.error(f"Hunter domain-search error: {e}")
        return []


async def enrich_email(name: str, domain: str, website: str) -> tuple[str, str]:
    """Try all methods to find an email: Hunter person -> Hunter domain -> Website scrape.
    Returns (email, source_suffix) e.g. ("john@co.com", "+Hunter") or ("info@co.com", "+Web")
    """
    # 1) Hunter email-finder (person-level)
    if HUNTER_API_KEY and domain and name:
        parts = name.split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        email = await find_email_hunter(first, last, domain)
        if email:
            logger.info(f"Hunter email-finder found {email} for {name}")
            return email, "+Hunter"

    # 2) Hunter domain search
    if HUNTER_API_KEY and domain:
        hunter_results = await enrich_with_hunter(domain)
        if hunter_results:
            email = hunter_results[0].get("email", "")
            if email:
                logger.info(f"Hunter domain-search found {email} for {domain}")
                return email, "+Hunter"

    # 3) Scrape the company website for emails
    scrape_url = website or (f"https://{domain}" if domain else "")
    if scrape_url:
        scraped = await scrape_website_emails(scrape_url)
        if scraped:
            logger.info(f"Website scrape found {scraped[0]} from {scrape_url}")
            return scraped[0], "+Web"

    return "", ""


async def search_apollo(industry: str, location: str, count: int) -> list[dict]:
    """Search Apollo for B2B people data."""
    if not APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set — skipping Apollo")
        return []

    url = "https://api.apollo.io/v1/mixed_people/search"
    payload = {
        "api_key": APOLLO_API_KEY,
        "q_organization_keyword_tags": [industry],
        "person_locations": [location],
        "per_page": count,
        "person_titles": [
            "CEO", "Founder", "Owner", "Director",
            "Managing Director", "Head of Marketing",
        ],
    }

    try:
        async with aiohttp.ClientSession(timeout=API_TIMEOUT) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Apollo error {resp.status}: {body[:200]}")
                    return []
                data = await resp.json()

        leads = []
        for p in data.get("people", []):
            phone = ""
            if p.get("phone_numbers"):
                phone = p["phone_numbers"][0].get("sanitized_number", "") or ""

            email = p.get("email", "") or ""
            name = p.get("name", "") or ""
            company = (p.get("organization") or {}).get("name", "") or ""
            domain = (p.get("organization") or {}).get("primary_domain", "") or ""

            # If Apollo didn't give email, try all enrichment methods
            source_suffix = ""
            website = (p.get("organization") or {}).get("website_url", "") or ""
            if not email:
                email, source_suffix = await enrich_email(name, domain, website)

            leads.append({
                "name": name,
                "email": email,
                "phone": phone,
                "company": company,
                "title": p.get("title", "") or "",
                "linkedin": p.get("linkedin_url", "") or "",
                "domain": domain,
                "website": website or (f"https://{domain}" if domain else ""),
                "source": "Apollo" + source_suffix,
            })
        return leads

    except asyncio.TimeoutError:
        logger.error("Apollo API timeout")
        return []
    except Exception as e:
        logger.error(f"Apollo error: {e}")
        return []


async def search_serpapi_maps(industry: str, location: str, count: int) -> list[dict]:
    """Search Google Maps via SerpAPI for local businesses with contact info."""
    if not SERPAPI_KEY:
        logger.warning("SERPAPI_KEY not set — skipping Google Maps")
        return []

    query = f"{industry} in {location}"
    params = {
        "q": query,
        "api_key": SERPAPI_KEY,
        "engine": "google_maps",
        "type": "search",
    }

    try:
        async with aiohttp.ClientSession(timeout=API_TIMEOUT) as session:
            async with session.get(
                "https://serpapi.com/search.json", params=params
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"SerpAPI Maps error {resp.status}: {body[:200]}")
                    return []
                data = await resp.json()

        leads = []
        for r in data.get("local_results", []):
            website = r.get("website", "") or ""
            domain = ""
            if website:
                domain = (
                    website
                    .replace("https://", "")
                    .replace("http://", "")
                    .split("/")[0]
                    .split("?")[0]
                )

            biz_name = r.get("title", "") or ""
            email, source_suffix = await enrich_email(biz_name, domain, website)

            leads.append({
                "name": biz_name,
                "email": email,
                "phone": r.get("phone", "") or "",
                "company": biz_name,
                "title": "Business Owner",
                "linkedin": "",
                "website": website,
                "domain": domain,
                "address": r.get("address", "") or "",
                "source": "GoogleMaps" + source_suffix,
            })

        return leads[:count]

    except asyncio.TimeoutError:
        logger.error("SerpAPI Maps timeout")
        return []
    except Exception as e:
        logger.error(f"SerpAPI Maps error: {e}")
        return []


async def search_serpapi(industry: str, location: str, count: int) -> list[dict]:
    """Search Google web via SerpAPI."""
    if not SERPAPI_KEY:
        logger.warning("SERPAPI_KEY not set — skipping Google web search")
        return []

    query = f"{industry} companies in {location} contact email"
    params = {
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": count,
        "engine": "google",
    }

    try:
        async with aiohttp.ClientSession(timeout=API_TIMEOUT) as session:
            async with session.get(
                "https://serpapi.com/search.json", params=params
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"SerpAPI error {resp.status}: {body[:200]}")
                    return []
                data = await resp.json()

        leads = []

        # Google local results
        for r in data.get("local_results", []):
            website = r.get("website", "") or ""
            domain = ""
            if website:
                domain = website.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0]

            biz_name = r.get("title", "") or ""
            email, source_suffix = await enrich_email(biz_name, domain, website)

            leads.append({
                "name": biz_name,
                "email": email,
                "phone": r.get("phone", "") or "",
                "company": biz_name,
                "title": "Business Owner",
                "linkedin": "",
                "website": website,
                "domain": domain,
                "source": "SerpAPI" + source_suffix,
            })

        # Organic results
        for r in data.get("organic_results", [])[:count]:
            link = r.get("link", "") or ""
            domain = ""
            if link:
                domain = link.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0]

            title = r.get("title", "") or ""
            email, source_suffix = await enrich_email(title, domain, link)

            leads.append({
                "name": title,
                "email": email,
                "phone": "",
                "company": title,
                "title": "",
                "linkedin": "",
                "website": link,
                "domain": domain,
                "source": "SerpAPI" + source_suffix,
            })

        return leads[:count]

    except asyncio.TimeoutError:
        logger.error("SerpAPI web timeout")
        return []
    except Exception as e:
        logger.error(f"SerpAPI error: {e}")
        return []


async def find_leads_flow(industry: str, location: str, count: int) -> list[dict]:
    """Main lead search: Apollo -> Google Maps -> Google Web, with Hunter enrichment."""
    leads = []

    # 1) Apollo (best for B2B people data) + auto Hunter enrichment
    apollo_leads = await search_apollo(industry, location, count)
    leads.extend(apollo_leads)
    logger.info(f"Apollo: {len(apollo_leads)} leads, {sum(1 for l in apollo_leads if l.get('email'))} with emails")

    # 2) Google Maps (best for local businesses with phone/website)
    remaining = count - len(leads)
    if remaining > 0:
        maps_leads = await search_serpapi_maps(industry, location, remaining)
        leads.extend(maps_leads)
        logger.info(f"Google Maps: {len(maps_leads)} leads, {sum(1 for l in maps_leads if l.get('email'))} with emails")

    # 3) Google web search if we still need more
    remaining = count - len(leads)
    if remaining > 0:
        serp_leads = await search_serpapi(industry, location, remaining)
        leads.extend(serp_leads)
        logger.info(f"Google web: {len(serp_leads)} leads, {sum(1 for l in serp_leads if l.get('email'))} with emails")

    # Deduplicate by email and by domain
    seen_emails = set()
    seen_domains = set()
    unique = []
    for lead in leads:
        email = (lead.get("email") or "").lower().strip()
        domain = (lead.get("domain") or "").lower().strip()

        if email and email in seen_emails:
            continue
        if not email and domain and domain in seen_domains:
            continue

        if email:
            seen_emails.add(email)
        if domain:
            seen_domains.add(domain)
        unique.append(lead)

    total_with_email = sum(1 for l in unique if l.get("email"))
    logger.info(f"Final: {len(unique)} leads, {total_with_email} with emails")

    return unique[:count]
