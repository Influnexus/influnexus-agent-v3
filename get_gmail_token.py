"""
Run this LOCALLY (not on Railway) once to get your Gmail refresh token.

Steps:
1. Go to Google Cloud Console > APIs & Services > Credentials
2. Create OAuth 2.0 Client ID (choose "Desktop app" type)
3. Download the JSON, rename to client_secret.json, place next to this script
4. Enable the Gmail API: APIs & Services > Library > Gmail API > Enable
5. Run: python get_gmail_token.py
6. A browser window will open — sign in and authorize
7. Copy the printed values into Railway environment variables

IMPORTANT: If your Google Cloud app is in "Testing" mode, refresh tokens
expire after 7 days. To get long-lived tokens, go to OAuth consent screen
and publish the app (or set it to "In production").
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "=" * 50)
print("Add these to Railway environment variables:")
print("=" * 50)
print(f"GMAIL_CLIENT_ID={creds.client_id}")
print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
print(f"GMAIL_SENDER_EMAIL=<your Gmail address>")
print("=" * 50)
