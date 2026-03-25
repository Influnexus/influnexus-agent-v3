import os
import re
import html
import logging
import traceback
import functools
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from leads import find_leads_flow, LEADS_INDUSTRY, LEADS_LOCATION, LEADS_COUNT
from outreach import outreach_flow, OUTREACH_SUBJECT, OUTREACH_CONFIRM
from crm import crm_dashboard, crm_add_lead
from meetings import list_upcoming_meetings
from followups import send_followups
from ai_helper import generate_outreach_email

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required")

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))


# ── Helpers ─────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """Escape Markdown special characters so Telegram doesn't choke."""
    # Replace Markdown special chars but preserve intentional formatting
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, "\\" + ch)
    return text


def strip_html(text: str) -> str:
    """Convert HTML email to plain text for Telegram preview."""
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = text.replace("</p>", "\n").replace("<p>", "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


# ── Security: only your Telegram account can use this bot ────────────

def restricted(func):
    @functools.wraps(func)
    async def wrapper(update: Update, *args, **kwargs):
        user_id = update.effective_user.id
        if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
            if update.callback_query:
                await update.callback_query.answer("Unauthorized.", show_alert=True)
            else:
                await update.message.reply_text("Unauthorized. This bot is private.")
            return ConversationHandler.END
        return await func(update, *args, **kwargs)
    return wrapper


# ── Main menu keyboard ───────────────────────────────────────────────

MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Find Leads", callback_data="find_leads"),
        InlineKeyboardButton("Run Outreach", callback_data="run_outreach"),
    ],
    [
        InlineKeyboardButton("CRM Dashboard", callback_data="crm_dashboard"),
        InlineKeyboardButton("Book Meeting", callback_data="book_meeting"),
    ],
    [
        InlineKeyboardButton("Send Follow-ups", callback_data="send_followups"),
        InlineKeyboardButton("Instagram DM", callback_data="instagram_dm"),
    ],
])


# ── Safe message sender ─────────────────────────────────────────────

async def safe_reply(update_or_query, text, reply_markup=None):
    """Send message with Markdown, fall back to plain text if it fails."""
    kwargs = {}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup

    # Determine if this is a callback query or a message
    if hasattr(update_or_query, "edit_message_text"):
        send = update_or_query.edit_message_text
    elif hasattr(update_or_query, "reply_text"):
        send = update_or_query.reply_text
    else:
        return

    try:
        await send(text, parse_mode="Markdown", **kwargs)
    except Exception:
        # Markdown failed — strip formatting and send plain
        try:
            plain = text.replace("*", "").replace("_", "").replace("`", "")
            await send(plain, **kwargs)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")


# ── /start command ───────────────────────────────────────────────────

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Influnexus Agent*\n\nTap a button to get started.",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown",
    )


@restricted
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_reply(query, "*Influnexus Agent*\n\nTap a button to get started.", MAIN_MENU)


# ── FIND LEADS flow ─────────────────────────────────────────────────

@restricted
async def find_leads_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Find Leads\n\nWhat industry are you targeting?\n\n"
        "Example: real estate, SaaS, e-commerce, healthcare",
    )
    return LEADS_INDUSTRY


@restricted
async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lead_industry"] = update.message.text
    await update.message.reply_text(
        "What location/city?\n\nExample: Mumbai, Delhi, Bangalore, Pan India",
    )
    return LEADS_LOCATION


@restricted
async def ask_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lead_location"] = update.message.text
    await update.message.reply_text("How many leads? (1-50, default 10)")
    return LEADS_COUNT


@restricted
async def do_find_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = min(max(int(update.message.text), 1), 50)
    except ValueError:
        count = 10

    status_msg = await update.message.reply_text("Searching for leads... Please wait.")

    try:
        leads = await find_leads_flow(
            industry=context.user_data.get("lead_industry", ""),
            location=context.user_data.get("lead_location", ""),
            count=count,
        )
    except Exception as e:
        logger.error(f"Lead search failed: {e}")
        await update.message.reply_text(
            f"Lead search failed: {e}\n\nPlease try again.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    if not leads:
        await update.message.reply_text(
            "No leads found. Try different criteria.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    context.user_data["found_leads"] = leads

    with_email = sum(1 for l in leads if l.get("email"))
    msg = f"Found {len(leads)} leads ({with_email} with email):\n\n"
    for i, lead in enumerate(leads, 1):
        email_display = lead.get("email") or "No email found"
        name = escape_md(lead.get("name", "N/A"))
        company = escape_md(lead.get("company", "N/A"))
        msg += (
            f"{i}. {name}\n"
            f"   Company: {company}\n"
            f"   Email: {email_display}\n"
            f"   Phone: {lead.get('phone') or 'N/A'}\n\n"
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Outreach All", callback_data="outreach_all"),
            InlineKeyboardButton("Save to CRM", callback_data="save_to_crm"),
        ],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")],
    ])

    # Send without Markdown to avoid escaping issues with lead data
    await update.message.reply_text(msg, reply_markup=keyboard)
    return ConversationHandler.END


# ── RUN OUTREACH flow ────────────────────────────────────────────────

@restricted
async def outreach_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    leads = context.user_data.get("found_leads", [])
    if not leads:
        await safe_reply(query, "No leads loaded. Find leads first!", MAIN_MENU)
        return ConversationHandler.END

    with_email = sum(1 for l in leads if l.get("email"))
    await query.edit_message_text(
        f"Run Outreach\n\n"
        f"You have {len(leads)} leads ({with_email} with email).\n\n"
        f"Enter email subject line:",
    )
    return OUTREACH_SUBJECT


@restricted
async def outreach_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["email_subject"] = update.message.text
    leads = context.user_data.get("found_leads", [])

    if not leads:
        await update.message.reply_text("No leads loaded. Find leads first!", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    await update.message.reply_text("Generating personalized email with AI...")

    try:
        preview_html = await generate_outreach_email(
            lead=leads[0],
            subject=update.message.text,
        )
    except Exception as e:
        logger.error(f"Email generation failed: {e}")
        preview_html = ""

    # Convert HTML to plain text for Telegram display
    preview_text = strip_html(preview_html) if preview_html else "Could not generate preview."

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Send to All", callback_data="confirm_send_all"),
            InlineKeyboardButton("Edit Subject", callback_data="edit_outreach"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="main_menu")],
    ])

    email_to = leads[0].get("email") or "No email"
    subject = context.user_data["email_subject"]

    # Send as plain text — no Markdown to avoid parse errors with email content
    msg = (
        f"Preview (Lead 1):\n\n"
        f"To: {email_to}\n"
        f"Subject: {subject}\n\n"
        f"{preview_text}\n\n"
        f"Send this to all {len(leads)} leads?"
    )

    await update.message.reply_text(msg, reply_markup=keyboard)
    return OUTREACH_CONFIRM


@restricted
async def outreach_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    leads = context.user_data.get("found_leads", [])
    subject = context.user_data.get("email_subject", "")

    if not leads:
        await safe_reply(query, "No leads loaded.", MAIN_MENU)
        return ConversationHandler.END

    await query.edit_message_text("Sending emails... This may take a minute.")

    try:
        results = await outreach_flow(leads=leads, subject=subject)
    except Exception as e:
        logger.error(f"Outreach flow error: {e}")
        results = {"sent": 0, "failed": len(leads), "errors": [str(e)]}

    msg = (
        f"Outreach Complete!\n\n"
        f"Sent: {results['sent']}\n"
        f"Failed: {results['failed']}\n"
    )

    errors = results.get("errors", [])
    if errors:
        msg += "\nIssues:\n"
        for err in errors[:5]:
            msg += f"- {err}\n"

    msg += "\nLeads saved to CRM."

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
    )
    return ConversationHandler.END


# ── CRM DASHBOARD ────────────────────────────────────────────────────

@restricted
async def crm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Loading CRM...")

    try:
        summary = await crm_dashboard()
    except Exception as e:
        summary = f"Error: {e}"

    await safe_reply(
        query,
        f"*CRM Dashboard*\n\n{summary}",
        InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
    )


@restricted
async def save_to_crm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    leads = context.user_data.get("found_leads", [])
    if not leads:
        await query.edit_message_text("No leads to save.", reply_markup=MAIN_MENU)
        return

    await query.edit_message_text("Saving leads to CRM...")

    try:
        count = await crm_add_lead(leads)
    except Exception as e:
        logger.error(f"CRM save failed: {e}")
        count = 0

    await query.edit_message_text(
        f"Saved {count} leads to Google Sheets CRM!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
    )


# ── BOOK MEETING ─────────────────────────────────────────────────────

@restricted
async def book_meeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    calendly = os.environ.get("CALENDLY_LINK", "")

    try:
        upcoming = await list_upcoming_meetings()
    except Exception:
        upcoming = ""

    msg = "Book a Meeting\n\n"
    if calendly:
        msg += f"Share this link with clients:\n{calendly}\n\n"
    if upcoming:
        msg += "Upcoming Meetings:\n" + upcoming
    else:
        msg += "No upcoming meetings."

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        disable_web_page_preview=True,
    )


# ── SEND FOLLOW-UPS ──────────────────────────────────────────────────

@restricted
async def followups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Checking for pending follow-ups...")

    try:
        result = await send_followups()
    except Exception as e:
        result = f"Error: {e}"

    await safe_reply(
        query,
        f"*Follow-ups*\n\n{result}",
        InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
    )


# ── INSTAGRAM DM (placeholder) ──────────────────────────────────────

@restricted
async def instagram_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Instagram DM\n\n"
        "Coming soon. Configure your Instagram API credentials to enable.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
    )


# ── GLOBAL ERROR HANDLER ────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and notify user so the bot never silently dies."""
    logger.error(f"Exception while handling update: {context.error}")
    logger.error(traceback.format_exc())

    if isinstance(update, Update):
        try:
            msg = f"Something went wrong: {context.error}\n\nPlease try /start again."
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(msg, reply_markup=MAIN_MENU)
            elif update.message:
                await update.message.reply_text(msg, reply_markup=MAIN_MENU)
        except Exception:
            pass


# ── WIRE EVERYTHING UP ──────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Find Leads conversation
    leads_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(find_leads_start, pattern="^find_leads$")],
        states={
            LEADS_INDUSTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location)],
            LEADS_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_count)],
            LEADS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, do_find_leads)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
        ],
        per_message=False,
    )

    # Outreach conversation
    outreach_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(outreach_start, pattern="^run_outreach$"),
            CallbackQueryHandler(outreach_start, pattern="^outreach_all$"),
        ],
        states={
            OUTREACH_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, outreach_preview)],
            OUTREACH_CONFIRM: [
                CallbackQueryHandler(outreach_send, pattern="^confirm_send_all$"),
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
                CallbackQueryHandler(outreach_start, pattern="^edit_outreach$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(leads_conv)
    app.add_handler(outreach_conv)
    app.add_handler(CallbackQueryHandler(crm_handler, pattern="^crm_dashboard$"))
    app.add_handler(CallbackQueryHandler(save_to_crm_handler, pattern="^save_to_crm$"))
    app.add_handler(CallbackQueryHandler(book_meeting_handler, pattern="^book_meeting$"))
    app.add_handler(CallbackQueryHandler(followups_handler, pattern="^send_followups$"))
    app.add_handler(CallbackQueryHandler(instagram_handler, pattern="^instagram_dm$"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))

    # Global error handler — catches ALL unhandled exceptions
    app.add_error_handler(error_handler)

    logger.info("Influnexus Agent started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
