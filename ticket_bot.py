import requests
import json
import time
import os
import logging
import asyncio
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime
import random
from dotenv import load_dotenv  
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = int(os.getenv('CHAT_ID'))
CHECK_INTERVAL =  int(os.getenv('CHECK_INTERVAL'))   
API_URL =  os.getenv('API_URL')
CACHE_FILE = 'event_cache.json'

# User agents to rotate through to avoid being blocked
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_4_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.51 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
]

def get_event_data():
    """Fetch event data from the API with browser-like headers"""
    # Use random user agent and add common headers
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://rcb.ticketgenie.in/',
        'Origin': 'https://rcb.ticketgenie.in',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }
    
    try:
        response = requests.get(API_URL, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from API: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Response status: {e.response.status_code}, Body: {e.response.text[:500]}")
        return None

def load_cached_events():
    """Load previously cached events"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("Error decoding cached events file")
    return {"result": []}

def save_cached_events(events):
    """Save current events to cache file"""
    with open(CACHE_FILE, 'w') as f:
        json.dump(events, f)

def find_new_events(current_events, cached_events):
    """Find new events by comparing current events with cached ones"""
    if not current_events or "result" not in current_events:
        return []
    
    current_event_codes = {event["event_Code"] for event in current_events["result"]}
    cached_event_codes = {event["event_Code"] for event in cached_events["result"]}
    
    new_event_codes = current_event_codes - cached_event_codes
    
    new_events = [
        event for event in current_events["result"] 
        if event["event_Code"] in new_event_codes
    ]
    
    return new_events

def format_notification(new_events):
    """Format notification message for Telegram"""
    if not new_events:
        return None
    
    message = f"🎉 *{len(new_events)} new IPL match tickets available!* 🎉\n\n"
    
    for event in new_events:
        message += f"*{event['event_Name']}*\n"
        message += f"📅 {event['event_Display_Date']}\n"
        message += f"🏟️ {event['venue_Name']}, {event['city_Name']}\n"
        message += f"💰 Price Range: {event['event_Price_Range']}\n\n"
    
    message += "🔗 Buy tickets at: https://shop.royalchallengers.com/ticket"
    
    return message

def format_current_events():
    """Format a message with current available events"""
    events = get_event_data()
    if not events or "result" not in events or not events["result"]:
        return "No events are currently available."
    
    message = f"📅 *Current IPL Matches ({len(events['result'])} events):*\n\n"
    
    for event in events["result"]:
        message += f"*{event['event_Name']}*\n"
        message += f"📅 {event['event_Display_Date']}\n"
        message += f"💰 Price Range: {event['event_Price_Range']}\n\n"
    
    message += "🔗 Buy tickets at: https://shop.royalchallengers.com/ticket"
    
    return message

async def send_message(context, chat_id, message):
    """Send a message to a specific chat"""
    try:
        # Convert chat_id to integer if it's a string of digits
        chat_id_to_use = int(chat_id) if isinstance(chat_id, str) and chat_id.lstrip('-').isdigit() else chat_id
        
        await context.bot.send_message(
            chat_id=chat_id_to_use,
            text=message,
            parse_mode='Markdown'
        )
        logger.info(f"Message sent to {chat_id_to_use}")
        return True
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return False

async def check_for_updates_with_notification(context):
    """Job to check for updates and send notifications"""
    logger.info("Scheduled check for ticket updates...")
    
    # Get current event data
    current_events = get_event_data()
    if not current_events:
        logger.error("Failed to fetch current events")
        return
    
    # Load cached events
    cached_events = load_cached_events()
    
    # Find new events
    new_events = find_new_events(current_events, cached_events)
    
    # If new events are found, send notification
    if new_events:
        logger.info(f"Found {len(new_events)} new events")
        notification = format_notification(new_events)
        if notification:
            success = await send_message(context, CHAT_ID, notification)
            if success:
                # Update cache only after successful notification
                save_cached_events(current_events)
    else:
        logger.info("No new events found")

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = (
        "👋 *Welcome to the RCB Ticket Alert Bot!*\n\n"
        "I'll notify you when new RCB match tickets become available.\n"
        "Currently i am checking for new tickets every 30 secs!\n\n"
        "*Commands:*\n"
        "/check - Check for new tickets right now\n"
        "/status - View currently available tickets\n"
        "/help - for help\n"
        "Contact the developer at: [@tony_de_costa](https://t.me/tony_de_costa)"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually check for new tickets when the command /check is issued."""
    await update.message.reply_text("🔍 Checking for new tickets...", parse_mode='Markdown')
    
    # Get current event data
    current_events = get_event_data()
    if not current_events:
        await update.message.reply_text("❌ Failed to fetch events from the API.", parse_mode='Markdown')
        return

    # Load cached events
    cached_events = load_cached_events()

    # Find new events
    new_events = find_new_events(current_events, cached_events)

    # If new events are found, display them
    if new_events:
        notification = format_notification(new_events)
        await update.message.reply_text(notification, parse_mode='Markdown')
    else:
        await update.message.reply_text("⚠️ No new events found.", parse_mode='Markdown')

    await update.message.reply_text("✅ Check completed!", parse_mode='Markdown')
    
    # Save the current events if new ones were found (as part of notification)
    if new_events:
        save_cached_events(current_events)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current ticket status when the command /status is issued."""
    await update.message.reply_text("📊 Fetching current ticket status...", parse_mode='Markdown')
    status_message = format_current_events()
    await update.message.reply_text(status_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    help_message = (
        "🤖 *RCB Ticket Alert Bot Help*\n\n"
        "I monitor RCB match tickets and notify you when they become available.\n"
        "Currently i am checking for new tickets every 30 secs!\n\n"
        "*Commands:*\n"
        "/check - Check for new tickets right now\n"
        "/status - View currently available tickets\n"
        "/help - Show this help message\n"
        "Contact the developer at: [@tony_de_costa](https://t.me/tony_de_costa)"
    )
    await update.message.reply_text(help_message, parse_mode='Markdown')

async def handle_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respond when the bot is mentioned in a message."""
    # Check if bot was mentioned
    if update.message.entities and any(entity.type == "mention" for entity in update.message.entities):
        message_text = update.message.text.lower()
        bot_username = context.bot.username.lower()
        
        # Make sure the mention is actually for this bot
        if f"@{bot_username}" in message_text:
            default_response = (
                "👋 Hi there! I'm the RCB Ticket Alert Bot.\n\n"
                "I'm monitoring RCB match tickets and will notify when they become available.\n\n"
                "*Commands:*\n"
                "/check - Check for new tickets right now\n"
                "/status - View currently available tickets\n"
                "/help - for help\n"
            )
            await update.message.reply_text(default_response, parse_mode='Markdown')

def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Add message handler for mentions
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mention))
    
    # Set up periodic ticket checking job (every CHECK_INTERVAL seconds)
    job_queue = application.job_queue
    job_queue.run_repeating(check_for_updates_with_notification, interval=CHECK_INTERVAL, first=10)
    
    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()