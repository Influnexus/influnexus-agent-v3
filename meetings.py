import os
import json
import logging
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def get_calendar_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    return build("calendar", "v3", credentials=creds)


async def list_upcoming_meetings() -> str:
    try:
        service = get_calendar_service()
        if not service or not GCAL_CALENDAR_ID:
            return ""

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        week = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z")

        result = service.events().list(
            calendarId=GCAL_CALENDAR_ID,
            timeMin=now,
            timeMax=week,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return ""

        lines = []
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date"))
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                fmt = dt.strftime("%b %d, %I:%M %p")
            except (ValueError, AttributeError):
                fmt = start
            lines.append(f"  {fmt} - {ev.get('summary', 'No title')}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return ""
