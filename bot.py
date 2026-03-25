import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
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

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))


# ── Security: only your Telegram account can use this bot ────────────

def restricted(func):
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


# ── /start command ───────────────────────────────────────────────────

@restricted
async def start(update: Update, context):
    await update.message.reply_text(
        "*Influnexus Agent*\n\nTap a button to get started.",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown",
    )


@restricted
async def main_menu_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Influnexus Agent*\n\nTap a button to get started.",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown",
    )


# ── FIND LEADS flow ─────────────────────────────────────────────────

@restricted
async def find_leads_start(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Find Leads*\n\nWhat industry are you targeting?\n\n"
        "Example: _real estate, SaaS, e-commerce, healthcare_",
        parse_mode="Markdown",
    )
    return LEADS_INDUSTRY


@restricted
async def ask_location(update: Update, context):
    context.user_data["lead_industry"] = update.message.text
    await update.message.reply_text(
        "What location/city?\n\nExample: _Mumbai, Delhi, Bangalore, Pan India_",
        parse_mode="Markdown",
    )
    return LEADS_LOCATION


@restricted
async def ask_count(update: Update, context):
    context.user_data["lead_location"] = update.message.text
    await update.message.reply_text("How many leads? (1-50, default 10)")
    return LEADS_COUNT


@restricted
async def do_find_leads(update: Update, context):
    try:
        count = min(max(int(update.message.text), 1), 50)
    except ValueError:
        count = 10

    await update.message.reply_text("Searching for leads... Please wait.")

    leads = await find_leads_flow(
        industry=context.user_data["lead_industry"],
        location=context.user_data["lead_location"],
        count=count,
    )

    if not leads:
        await update.message.reply_text(
            "No leads found. Try different criteria.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    context.user_data["found_leads"] = leads

    with_email = sum(1 for l in leads if l.get("email"))
    msg = f"Found *{len(leads)}* leads ({with_email} with email):\n\n"
    for i, lead in enumerate(leads, 1):
        email_display = lead.get("email") or "No email found"
        msg += (
            f"*{i}.* {lead.get('name', 'N/A')}\n"
            f"   Company: {lead.get('company', 'N/A')}\n"
            f"   Email: {email_display}\n"
            f"   Phone: {lead.get('phone', 'N/A')}\n\n"
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Outreach All", callback_data="outreach_all"),
            InlineKeyboardButton("Save to CRM", callback_data="save_to_crm"),
        ],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")],
    ])

    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return ConversationHandler.END


# ── RUN OUTREACH flow ────────────────────────────────────────────────

@restricted
async def outreach_start(update: Update, context):
    query = update.callback_query
    await query.answer()

    leads = context.user_data.get("found_leads", [])
    if not leads:
        await query.edit_message_text(
            "No leads loaded. Find leads first!",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"*Run Outreach*\n\nYou have *{len(leads)}* leads.\n\nEnter email subject line:",
        parse_mode="Markdown",
    )
    return OUTREACH_SUBJECT


@restricted
async def outreach_preview(update: Update, context):
    context.user_data["email_subject"] = update.message.text
    leads = context.user_data.get("found_leads", [])

    await update.message.reply_text("Generating personalized email with AI...")

    preview = await generate_outreach_email(
        lead=leads[0],
        subject=update.message.text,
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Send to All", callback_data="confirm_send_all"),
            InlineKeyboardButton("Edit & Retry", callback_data="edit_outreach"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="main_menu")],
    ])

    await update.message.reply_text(
        f"*Preview (Lead 1):*\n\n"
        f"*To:* {leads[0].get('email', 'N/A')}\n"
        f"*Subject:* {context.user_data['email_subject']}\n\n"
        f"{preview}\n\n"
        f"_Send this to all {len(leads)} leads?_",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return OUTREACH_CONFIRM


@restricted
async def outreach_send(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Sending emails... Please wait.")

    results = await outreach_flow(
        leads=context.user_data.get("found_leads", []),
        subject=context.user_data.get("email_subject", ""),
    )

    msg = (
        f"*Outreach Complete!*\n\n"
        f"Sent: {results['sent']}\n"
        f"Failed: {results['failed']}\n"
    )

    # Show errors so user knows what went wrong
    errors = results.get("errors", [])
    if errors:
        msg += "\n*Issues:*\n"
        for err in errors[:5]:  # Max 5 errors to avoid message too long
            msg += f"- {err}\n"

    msg += "\nLeads saved to CRM."

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── CRM DASHBOARD ────────────────────────────────────────────────────

@restricted
async def crm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Loading CRM...")

    summary = await crm_dashboard()

    await query.edit_message_text(
        f"*CRM Dashboard*\n\n{summary}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
    )


@restricted
async def save_to_crm_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    leads = context.user_data.get("found_leads", [])
    if not leads:
        await query.edit_message_text("No leads to save.")
        return

    await query.edit_message_text("Saving leads to CRM...")
    count = await crm_add_lead(leads)

    await query.edit_message_text(
        f"Saved *{count}* leads to Google Sheets CRM!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
    )


# ── BOOK MEETING ─────────────────────────────────────────────────────

@restricted
async def book_meeting_handler(update: Update, context):
    query = update.callback_query
    await query.answer()

    calendly = os.environ.get("CALENDLY_LINK", "")
    upcoming = await list_upcoming_meetings()

    msg = "*Book a Meeting*\n\n"
    if calendly:
        msg += f"Share this link with clients:\n{calendly}\n\n"
    if upcoming:
        msg += "*Upcoming Meetings:*\n" + upcoming
    else:
        msg += "No upcoming meetings."

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── SEND FOLLOW-UPS ──────────────────────────────────────────────────

@restricted
async def followups_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Checking for pending follow-ups...")

    result = await send_followups()

    await query.edit_message_text(
        f"*Follow-ups*\n\n{result}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
    )


# ── INSTAGRAM DM (placeholder) ──────────────────────────────────────

@restricted
async def instagram_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*Instagram DM*\n\n"
        "Coming soon. Configure your Instagram API credentials to enable.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
        ]),
        parse_mode="Markdown",
    )


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
        fallbacks=[CommandHandler("start", start)],
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
        fallbacks=[CommandHandler("start", start)],
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

    logger.info("Influnexus Agent started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
