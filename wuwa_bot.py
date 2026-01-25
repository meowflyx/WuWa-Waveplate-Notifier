import logging
import json
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# --- CONFIGURATION ---
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # <--- PASTE YOUR TOKEN HERE
WAVEPLATE_CAP = 240
REGEN_RATE_MINUTES = 6
DB_FILE = "wuwa_db.json"

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE HANDLING (JSON) ---
user_data_store = {}

def load_db():
    """Loads user data from JSON file on startup."""
    global user_data_store
    if not os.path.exists(DB_FILE):
        return

    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
            # Convert ISO string timestamps back to datetime objects
            for uid, data in raw_data.items():
                user_data_store[int(uid)] = {
                    "waveplates": data["waveplates"],
                    "timestamp": datetime.fromisoformat(data["timestamp"])
                }
        logger.info(f"Database loaded. Users found: {len(user_data_store)}")
    except Exception as e:
        logger.error(f"Error loading DB: {e}")

def save_db():
    """Saves current state to JSON file."""
    try:
        # Convert datetime objects to ISO strings for JSON compatibility
        export_data = {}
        for uid, data in user_data_store.items():
            export_data[str(uid)] = {
                "waveplates": data["waveplates"],
                "timestamp": data["timestamp"].isoformat()
            }
        
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving DB: {e}")

# --- MATH LOGIC ---
def calculate_current_wp(user_id):
    data = user_data_store.get(user_id)
    if not data:
        return None
    
    elapsed_minutes = (datetime.now() - data["timestamp"]).total_seconds() / 60
    regenerated = int(elapsed_minutes / REGEN_RATE_MINUTES)
    total = data["waveplates"] + regenerated
    
    return min(total, WAVEPLATE_CAP)

def get_seconds_to_cap(current_wp):
    if current_wp >= WAVEPLATE_CAP:
        return 0
    missing = WAVEPLATE_CAP - current_wp
    return missing * REGEN_RATE_MINUTES * 60

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    
    # If user not in DB, add them as "full"
    if user_id not in user_data_store:
        user_data_store[user_id] = {
            "timestamp": datetime.now(),
            "waveplates": WAVEPLATE_CAP
        }
        save_db()

    keyboard = [
        [InlineKeyboardButton("🌊 I just spent it ALL (Reset to 0)", callback_data='set_zero')],
        [InlineKeyboardButton("🔄 Status", callback_data='status')]
    ]
    
    await update.message.reply_text(
        "Bot active. I'm tracking your Waveplates.\n"
        "Use /set <amount> if you want to input a specific number.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /set <amount>")
        return

    user_id = update.effective_user.id
    try:
        amount = int(context.args[0])
        if amount < 0 or amount > WAVEPLATE_CAP:
            await update.message.reply_text(f"Amount must be between 0 and {WAVEPLATE_CAP}.")
            return
        
        await update_state_and_schedule(user_id, amount, context)
        await update.message.reply_text(f"Updated. Tracking from {amount}/{WAVEPLATE_CAP}.")
        
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return

    await query.answer()
    user_id = query.from_user.id

    if query.data == 'set_zero':
        await update_state_and_schedule(user_id, 0, context)
        await query.edit_message_text(
            text=f"✅ Timer reset! Count is 0/{WAVEPLATE_CAP}.\nI'll ping you when it's full.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Status", callback_data='status')]])
        )

    elif query.data == 'status':
        current = calculate_current_wp(user_id)
        if current is None:
            text = "Please run /start first."
        else:
            if current >= WAVEPLATE_CAP:
                text = f"⚡ **{WAVEPLATE_CAP}/{WAVEPLATE_CAP}**\nWaveplates full! Go farm!"
            else:
                seconds_left = get_seconds_to_cap(current)
                minutes_left = seconds_left / 60
                full_time = datetime.now() + timedelta(seconds=seconds_left)
                text = (
                    f"🌊 Current: **{current}/{WAVEPLATE_CAP}**\n"
                    f"⏳ Full in: {int(minutes_left // 60)}h {int(minutes_left % 60)}m\n"
                    f"📅 Cap time: {full_time.strftime('%H:%M')}"
                )
        
        await query.edit_message_text(
            text=text, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌊 I just spent it ALL (Reset to 0)", callback_data='set_zero')],
                [InlineKeyboardButton("🔄 Status", callback_data='status')]
            ])
        )

# --- TIMER LOGIC ---

async def update_state_and_schedule(user_id, amount, context):
    """Updates memory, saves to DB, and sets the timer."""
    # 1. Update memory
    user_data_store[user_id] = {
        "timestamp": datetime.now(),
        "waveplates": amount
    }
    # 2. Save to file
    save_db()

    # 3. Restart timer
    await schedule_notification(user_id, amount, context)

async def schedule_notification(user_id, amount, context):
    """Schedules the job in the queue (removing old ones first)."""
    if not context.job_queue:
        return

    # Remove old jobs for this user
    current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
    for job in current_jobs:
        job.schedule_removal()

    # Calculate wait time
    seconds_to_wait = get_seconds_to_cap(amount)
    
    # If wait time > 0, set timer
    if seconds_to_wait > 0:
        context.job_queue.run_once(
            notify_cap, 
            seconds_to_wait, 
            chat_id=user_id, 
            name=str(user_id)
        )
        logger.info(f"Timer set for user {user_id}: wait {seconds_to_wait/60:.1f} mins")

async def notify_cap(context: ContextTypes.DEFAULT_TYPE):
    """Triggered when the timer runs out."""
    job = context.job
    if not job or not job.chat_id:
        return
        
    await context.bot.send_message(
        chat_id=job.chat_id, 
        text=f"🚨 **WAVEPLATES FULL ({WAVEPLATE_CAP}/{WAVEPLATE_CAP})**\nGo farm before you overcap!",
        parse_mode='Markdown'
    )

async def restore_jobs(application):
    """Restores timers from database after a bot restart."""
    logger.info("Restoring timers from database...")
    if not application.job_queue:
        return

    for user_id, data in user_data_store.items():
        # Calculate current state based on saved timestamp
        elapsed_seconds = (datetime.now() - data["timestamp"]).total_seconds()
        regenerated = int(elapsed_seconds / 60 / REGEN_RATE_MINUTES)
        current_wp = min(data["waveplates"] + regenerated, WAVEPLATE_CAP)

        # If not full yet, set timer for the REMAINING time
        if current_wp < WAVEPLATE_CAP:
            seconds_left = get_seconds_to_cap(current_wp)
            application.job_queue.run_once(
                notify_cap,
                seconds_left,
                chat_id=user_id,
                name=str(user_id)
            )
            logger.info(f"Restored timer for {user_id}. Notify in {seconds_left/3600:.1f} hours.")

# --- MAIN ---
if __name__ == '__main__':
    # Load DB first
    load_db()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_manual))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Restore timers on startup
    if application.job_queue:
        application.job_queue.run_once(lambda ctx: restore_jobs(application), 1)

    print("Bot is running...")
    application.run_polling()