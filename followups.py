import logging
from datetime import datetime, timedelta

from crm import crm_get_followup_due, crm_get_leads_by_status, crm_update_status
from outreach import send_email
from ai_helper import generate_followup_email

logger = logging.getLogger(__name__)


async def send_followups() -> str:
    outreached = await crm_get_leads_by_status("Outreached")
    due = await crm_get_followup_due()

    leads_to_followup = []
    seen = set()

    # Explicitly due follow-ups
    for lead in due:
        email = lead.get("Email", "")
        if email and email not in seen:
            leads_to_followup.append(lead)
            seen.add(email)

    # Outreached leads not contacted in 3+ days
    for lead in outreached:
        email = lead.get("Email", "")
        last = lead.get("Last Contacted", "")
        if email and email not in seen and last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
                if datetime.now() - last_dt > timedelta(days=3):
                    leads_to_followup.append(lead)
                    seen.add(email)
            except ValueError:
                pass

    if not leads_to_followup:
        return "No pending follow-ups. All caught up!"

    sent = 0
    failed = 0

    for lead in leads_to_followup:
        email = lead.get("Email", "")
        if not email:
            continue

        body = await generate_followup_email({
            "name": lead.get("Name", ""),
            "email": email,
            "company": lead.get("Company", ""),
        })

        subject = f"Following up - {lead.get('Company') or lead.get('Name', '')}"
        result = await send_email(email, subject, body)
        if result["success"]:
            sent += 1
            await crm_update_status(email, "Followed Up")
        else:
            failed += 1

    result = f"*Follow-up Results:*\n\nSent: *{sent}*\n"
    if failed:
        result += f"Failed: *{failed}*\n"
    result += f"\nTotal pending: *{len(leads_to_followup)}*"
    return result
