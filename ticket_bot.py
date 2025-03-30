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
from fastapi import FastAPI, BackgroundTasks
from contextlib import asynccontextmanager
import uvicorn
import threading
import gc
try:
    import psutil
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "psutil"])
    import psutil
from dotenv import load_dotenv  
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 60))   
API_URL = os.getenv('API_URL')
CACHE_FILE = 'event_cache.json'
PORT = int(os.getenv("PORT", 8000))  # Default to 8000 instead of 10000

# Add timestamp to track bot uptime
START_TIME = datetime.now()

# User agents to rotate through to avoid being blocked
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_4_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.51 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
]

# Keep track of last successful API call
last_successful_api_call = None

# Rate limiting for Telegram
telegram_message_queue = asyncio.Queue()
telegram_rate_limit_sleep = 3  # seconds between messages to avoid rate limiting

def get_event_data():
    """Fetch event data from the API with browser-like headers"""
    global last_successful_api_call
    
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
        response = requests.get(API_URL, headers=headers, timeout=30)
        response.raise_for_status()
        last_successful_api_call = datetime.now()
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

async def telegram_message_worker():
    while True:
        item = await telegram_message_queue.get()
        context, chat_id, text, parse_mode, retry_count = item
        try:
            chat_id_to_use = int(chat_id) if isinstance(chat_id, str) and chat_id.lstrip('-').isdigit() else chat_id
            await context.bot.send_message(
                chat_id=chat_id_to_use,
                text=text,
                parse_mode=parse_mode
            )
            logger.info(f"Message sent to {chat_id_to_use}")
        except TimedOut:
            if retry_count < 3:
                logger.warning(f"Timeout occurred while sending message to {chat_id_to_use}. Retry {retry_count + 1}/3 in 10 seconds...")
                await asyncio.sleep(10)
                await telegram_message_queue.put((context, chat_id, text, parse_mode, retry_count + 1))
            else:
                logger.error("Max retries reached for message. Dropping message.")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            if "RetryAfter" in str(e):
                try:
                    retry_seconds = int(str(e).split("Retry in ")[1].split(" ")[0])
                    logger.info(f"Rate limited. Waiting {retry_seconds} seconds")
                    await asyncio.sleep(retry_seconds)
                    await telegram_message_queue.put((context, chat_id, text, parse_mode, retry_count))
                except (IndexError, ValueError):
                    logger.error("Failed to parse RetryAfter seconds")
        finally:
            await asyncio.sleep(telegram_rate_limit_sleep)
            telegram_message_queue.task_done()

async def send_message(context, chat_id, message, parse_mode='Markdown'):
    """Queue a message to be sent to Telegram with rate limiting"""
    await telegram_message_queue.put((context, chat_id, message, parse_mode, 0))
    return True  # Immediate return as message is queued

async def check_for_updates_with_notification(context):
    """Job to check for updates and send notifications"""
    logger.info("Scheduled check for ticket updates...")
    
    # Perform memory cleanup occasionally
    if random.random() < 0.05:  # ~5% chance on each run
        perform_cleanup()
    
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
            await send_message(context, CHAT_ID, notification)
            # Update cache whenever we find new events
            save_cached_events(current_events)
    else:
        logger.info("No new events found")

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = (
        "👋 *Welcome to the RCB Ticket Alert Bot!*\n\n"
        "I'll notify you when new RCB match tickets become available.\n"
        f"Currently I am checking for new tickets every {CHECK_INTERVAL} seconds!\n\n"
        "*Commands:*\n"
        "/check - Check for new tickets right now\n"
        "/status - View currently available tickets\n"
        "/health - Check bot health status\n"
        "/help - for help\n"
        "Contact the developer at: [@tony_de_costa](https://t.me/tony_de_costa)"
    )
    await send_message(context, update.effective_chat.id, welcome_message)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually check for new tickets when the command /check is issued."""
    await send_message(context, update.effective_chat.id, "🔍 Checking for new tickets...")
    
    # Get current event data
    current_events = get_event_data()
    if not current_events:
        await send_message(context, update.effective_chat.id, "❌ Failed to fetch events from the API.")
        return

    # Load cached events
    cached_events = load_cached_events()

    # Find new events
    new_events = find_new_events(current_events, cached_events)

    # If new events are found, display them
    if new_events:
        notification = format_notification(new_events)
        await send_message(context, update.effective_chat.id, notification)
    else:
        await send_message(context, update.effective_chat.id, "⚠️ No new events found.")

    await send_message(context, update.effective_chat.id, "✅ Check completed!")
    
    # Save the current events if new ones were found (as part of notification)
    if new_events:
        save_cached_events(current_events)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current ticket status when the command /status is issued."""
    await send_message(context, update.effective_chat.id, "📊 Fetching current ticket status...")
    status_message = format_current_events()
    await send_message(context, update.effective_chat.id, status_message)

async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show health status of the bot"""
    uptime = datetime.now() - START_TIME
    hours, remainder = divmod(uptime.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    # Get memory usage
    process = psutil.Process(os.getpid())
    memory_usage = process.memory_info().rss / 1024 / 1024  # Convert to MB
    
    # Get API health info
    api_status = "✅ Working" if last_successful_api_call else "❌ No successful calls yet"
    if last_successful_api_call:
        time_since_last_call = datetime.now() - last_successful_api_call
        last_call_info = f"{time_since_last_call.total_seconds():.1f} seconds ago"
    else:
        last_call_info = "N/A"
    
    health_message = (
        "🤖 *Bot Health Status*\n\n"
        f"⏱️ Uptime: {int(hours)} hours, {int(minutes)} minutes, {int(seconds)} seconds\n"
        f"🔄 Check Interval: Every {CHECK_INTERVAL} seconds\n"
        f"🧠 Memory Usage: {memory_usage:.2f} MB\n"
        f"🔌 API Status: {api_status}\n"
        f"⏰ Last Successful API Call: {last_call_info}\n"
        f"📋 Message Queue Size: {telegram_message_queue.qsize()}\n"
    )
    
    await send_message(context, update.effective_chat.id, health_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    help_message = (
        "🤖 *RCB Ticket Alert Bot Help*\n\n"
        "I monitor RCB match tickets and notify you when they become available.\n"
        f"Currently I am checking for new tickets every {CHECK_INTERVAL} secs!\n\n"
        "*Commands:*\n"
        "/check - Check for new tickets right now\n"
        "/status - View currently available tickets\n"
        "/health - Check bot health status\n"
        "/help - Show this help message\n"
        "Contact the developer at: [@tony_de_costa](https://t.me/tony_de_costa)"
    )
    await send_message(context, update.effective_chat.id, help_message)

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
                "/health - Check bot health status\n"
                "/help - for help\n"
            )
            await send_message(context, update.effective_chat.id, default_response)

def perform_cleanup():
    """Perform memory cleanup to prevent memory leaks"""
    logger.info("Performing memory cleanup...")
    gc.collect()

# Keep-alive task
async def keep_alive():
    """Keep server alive by performing light tasks periodically"""
    while True:
        logger.info("Keep-alive ping")
        # Clean memory occasionally
        if random.random() < 0.2:  # 20% chance
            perform_cleanup()
        await asyncio.sleep(60)  # Ping every minute

# Using the new FastAPI lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the keep-alive task
    background_task = asyncio.create_task(keep_alive())
    # Start the Telegram message worker
    message_worker = asyncio.create_task(telegram_message_worker())
    
    yield  # This yields control back to FastAPI
    
    # Cleanup when the app is shutting down
    background_task.cancel()
    message_worker.cancel()

app = FastAPI()  # Remove the lifespan parameter

@app.get("/")
async def root():
    """Root endpoint with basic health check information"""
    uptime = datetime.now() - START_TIME
    hours, remainder = divmod(uptime.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    return {
        "status": "running",
        "uptime": f"{int(hours)}h {int(minutes)}m {int(seconds)}s",
        "last_api_call": last_successful_api_call.isoformat() if last_successful_api_call else None
    }

@app.get("/health")
async def health_check():
    """Detailed health check endpoint"""
    process = psutil.Process(os.getpid())
    memory_usage = process.memory_info().rss / 1024 / 1024  # MB
    
    return {
        "status": "healthy",
        "uptime": str(datetime.now() - START_TIME),
        "memory_usage_mb": round(memory_usage, 2),
        "last_api_call": last_successful_api_call.isoformat() if last_successful_api_call else None,
        "message_queue_size": telegram_message_queue.qsize()
    }

@app.get("/ping")
async def ping():
    """Simple ping endpoint for monitoring services"""
    return {"ping": "pong"}

# Add this function to handle text messages
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle normal text messages."""
    message_text = update.message.text.lower()
    
    # Check if the message is in a group
    is_group = update.effective_chat.type in ["group", "supergroup"]
    
    # Only respond to "hi" messages that either mention the bot or are in private chat
    if "hi" in message_text or "hello" in message_text:
        bot_mentioned = False
        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention" and f"@{context.bot.username.lower()}" in message_text.lower():
                    bot_mentioned = True
                    break
        
        # Respond if it's a private message or if the bot was mentioned in a group
        if not is_group or bot_mentioned:
            greeting = (
                "👋 Hello there! I'm the RCB Ticket Alert Bot.\n\n"
                "I can help you stay updated on RCB match tickets.\n"
                "Type /help to see what I can do for you!"
            )
            await send_message(context, update.effective_chat.id, greeting)

async def run_bot(existing_loop=None):
    """Run the Telegram bot"""
    request = HTTPXRequest(
    read_timeout=30.0,
    write_timeout=30.0,
    connect_timeout=30.0
    )
    # Create the Application with the specified event loop
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Set up periodic ticket checking job (every CHECK_INTERVAL seconds)
    job_queue = application.job_queue
    job_queue.run_repeating(check_for_updates_with_notification, interval=CHECK_INTERVAL, first=10)

    # Start polling in non-blocking mode
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    return application  # Return the application object so we can shutdown properly later

def main():
    """Main function to run the application"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def run_application():
        # Start the Telegram bot
        telegram_app = await run_bot()
        
        # Set up the keep-alive task
        keep_alive_task = asyncio.create_task(keep_alive())
        
        # Set up the Telegram message worker
        message_worker = asyncio.create_task(telegram_message_worker())
        
        # Start FastAPI with Uvicorn
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
        
        # Cleanup (this will only execute if server.serve() completes)
        keep_alive_task.cancel()
        message_worker.cancel()
        await telegram_app.stop()
    
    # Run everything in the same event loop
    try:
        loop.run_until_complete(run_application())
    except KeyboardInterrupt:
        # Handle clean shutdown
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    # Run the bot in the main thread
    main()
