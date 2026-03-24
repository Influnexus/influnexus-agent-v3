import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# Conversation states
LEADS_INDUSTRY = 0
LEADS_LOCATION = 1
LEADS_COUNT = 2


async def search_apollo(industry: str, location: str, count: int) -> list[dict]:
    if not APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set")
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
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"Apollo error: {resp.status}")
                    return []
                data = await resp.json()

        leads = []
        for p in data.get("people", []):
            phone = ""
            if p.get("phone_numbers"):
                phone = p["phone_numbers"][0].get("sanitized_number", "")
            leads.append({
                "name": p.get("name", ""),
                "email": p.get("email", ""),
                "phone": phone,
                "company": p.get("organization", {}).get("name", ""),
                "title": p.get("title", ""),
                "linkedin": p.get("linkedin_url", ""),
                "source": "Apollo",
            })
        return leads
    except Exception as e:
        logger.error(f"Apollo error: {e}")
        return []


async def enrich_with_hunter(domain: str) -> list[dict]:
    if not HUNTER_API_KEY:
        return []

    url = (
        f"https://api.hunter.io/v2/domain-search"
        f"?domain={domain}&api_key={HUNTER_API_KEY}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        leads = []
        for e in data.get("data", {}).get("emails", []):
            first = e.get("first_name", "")
            last = e.get("last_name", "")
            leads.append({
                "name": f"{first} {last}".strip(),
                "email": e.get("value", ""),
                "phone": "",
                "company": domain,
                "title": e.get("position", ""),
                "linkedin": "",
                "source": "Hunter",
            })
        return leads
    except Exception as e:
        logger.error(f"Hunter error: {e}")
        return []


async def search_serpapi(industry: str, location: str, count: int) -> list[dict]:
    if not SERPAPI_KEY:
        logger.warning("SERPAPI_KEY not set")
        return []

    query = f"{industry} companies in {location} contact email"
    params = {
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": count,
        "engine": "google",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://serpapi.com/search.json", params=params
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        leads = []

        # Google Maps / local results
        for r in data.get("local_results", []):
            lead = {
                "name": r.get("title", ""),
                "email": "",
                "phone": r.get("phone", ""),
                "company": r.get("title", ""),
                "title": "Business Owner",
                "linkedin": "",
                "website": r.get("website", ""),
                "source": "SerpAPI",
            }
            # Try to find email via Hunter
            if lead["website"] and HUNTER_API_KEY:
                domain = (
                    lead["website"]
                    .replace("https://", "")
                    .replace("http://", "")
                    .split("/")[0]
                )
                hunter = await enrich_with_hunter(domain)
                if hunter:
                    lead["email"] = hunter[0].get("email", "")
                    lead["name"] = hunter[0].get("name", "") or lead["name"]
            leads.append(lead)

        # Organic results
        for r in data.get("organic_results", [])[:count]:
            link = r.get("link", "")
            if link:
                leads.append({
                    "name": r.get("title", ""),
                    "email": "",
                    "phone": "",
                    "company": r.get("title", ""),
                    "title": "",
                    "linkedin": "",
                    "website": link,
                    "source": "SerpAPI",
                })

        return leads[:count]
    except Exception as e:
        logger.error(f"SerpAPI error: {e}")
        return []


async def find_leads_flow(industry: str, location: str, count: int) -> list[dict]:
    leads = []

    # 1) Apollo (best for B2B)
    leads.extend(await search_apollo(industry, location, count))

    # 2) SerpAPI + Hunter if we need more
    remaining = count - len(leads)
    if remaining > 0:
        leads.extend(await search_serpapi(industry, location, remaining))

    # Deduplicate by email
    seen = set()
    unique = []
    for lead in leads:
        email = lead.get("email", "")
        if email and email in seen:
            continue
        if email:
            seen.add(email)
        unique.append(lead)

    return unique[:count]
