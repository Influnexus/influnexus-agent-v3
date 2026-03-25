import os
import json
import logging
import asyncio
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Name", "Email", "Phone", "Company", "Title",
    "LinkedIn", "Source", "Status", "Date Added",
    "Last Contacted", "Follow-up Date", "Notes",
]


def get_sheet():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        raise ValueError("Google Sheets not configured")

    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        ws = sheet.worksheet("Leads")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Leads", rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)

    return ws


def _sync_crm_dashboard() -> str:
    ws = get_sheet()
    data = ws.get_all_records()

    if not data:
        return "CRM is empty. Find some leads first!"

    total = len(data)
    statuses = {}
    for row in data:
        s = row.get("Status", "Unknown")
        statuses[s] = statuses.get(s, 0) + 1

    summary = f"*Total Leads:* {total}\n\n"
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        summary += f"  {status}: *{count}*\n"

    recent = data[-5:]
    summary += "\n*Recent Leads:*\n"
    for r in reversed(recent):
        summary += f"  {r.get('Name', 'N/A')} - {r.get('Company', 'N/A')} ({r.get('Status', 'New')})\n"

    return summary


def _sync_crm_add_lead(leads: list[dict], status: str = "New") -> int:
    ws = get_sheet()
    existing = ws.get_all_records()
    existing_emails = {r.get("Email", "").lower() for r in existing}

    rows = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for lead in leads:
        email = lead.get("email", "").lower()
        if email and email in existing_emails:
            continue
        rows.append([
            lead.get("name", ""),
            lead.get("email", ""),
            lead.get("phone", ""),
            lead.get("company", ""),
            lead.get("title", ""),
            lead.get("linkedin", ""),
            lead.get("source", ""),
            status,
            now,
            "",  # Last Contacted
            "",  # Follow-up Date
            "",  # Notes
        ])

    if rows:
        ws.append_rows(rows)
    return len(rows)


def _sync_crm_update_status(email: str, status: str):
    ws = get_sheet()
    cell = ws.find(email)
    if cell:
        ws.update_cell(cell.row, HEADERS.index("Status") + 1, status)
        ws.update_cell(
            cell.row,
            HEADERS.index("Last Contacted") + 1,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        )


def _sync_crm_get_leads_by_status(status: str) -> list[dict]:
    ws = get_sheet()
    return [r for r in ws.get_all_records() if r.get("Status") == status]


def _sync_crm_get_followup_due() -> list[dict]:
    ws = get_sheet()
    today = datetime.now().strftime("%Y-%m-%d")
    due = []
    for row in ws.get_all_records():
        f = row.get("Follow-up Date", "")
        if f and f <= today and row.get("Status") not in ("Converted", "Not Interested"):
            due.append(row)
    return due


# Async wrappers — run blocking gspread calls in a thread to avoid blocking the bot

async def crm_dashboard() -> str:
    try:
        return await asyncio.get_event_loop().run_in_executor(None, _sync_crm_dashboard)
    except Exception as e:
        logger.error(f"CRM dashboard error: {e}")
        return f"Error loading CRM: {e}"


async def crm_add_lead(leads: list[dict], status: str = "New") -> int:
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, _sync_crm_add_lead, leads, status
        )
    except Exception as e:
        logger.error(f"CRM add error: {e}")
        return 0


async def crm_update_status(email: str, status: str):
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, _sync_crm_update_status, email, status
        )
    except Exception as e:
        logger.error(f"CRM update error: {e}")


async def crm_get_leads_by_status(status: str) -> list[dict]:
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, _sync_crm_get_leads_by_status, status
        )
    except Exception as e:
        logger.error(f"CRM query error: {e}")
        return []


async def crm_get_followup_due() -> list[dict]:
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, _sync_crm_get_followup_due
        )
    except Exception as e:
        logger.error(f"CRM followup error: {e}")
        return []
