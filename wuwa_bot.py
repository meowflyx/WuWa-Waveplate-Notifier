import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# --- CONFIGURATION ---
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
WAVEPLATE_CAP = 240
REGEN_RATE_MINUTES = 6

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- IN-MEMORY DATABASE ---
user_data_store = {}

# --- LOGIC HELPERS ---
def calculate_current_wp(user_id):
    data = user_data_store.get(user_id)
    if not data:
        return None
    
    elapsed_minutes = (datetime.now() - data["timestamp"]).total_seconds() / 60
    regenerated = int(elapsed_minutes / REGEN_RATE_MINUTES)
    total = data["waveplates"] + regenerated
    
    return min(total, WAVEPLATE_CAP)

def get_time_to_cap(current_wp):
    if current_wp >= WAVEPLATE_CAP:
        return 0
    missing = WAVEPLATE_CAP - current_wp
    return missing * REGEN_RATE_MINUTES * 60

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # GUARD CLAUSE: Strict check to ensure user and message exist
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    
    if user_id not in user_data_store:
        user_data_store[user_id] = {
            "timestamp": datetime.now(),
            "waveplates": WAVEPLATE_CAP
        }

    keyboard = [
        [InlineKeyboardButton("🌊 I just spent it ALL (Reset to 0)", callback_data='set_zero')],
        [InlineKeyboardButton("🔄 Refresh Status", callback_data='status')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Bot active. I'm tracking your Waveplates.\n"
        "Use /set <amount> if you have a specific number (e.g. /set 30).",
        reply_markup=reply_markup
    )

async def set_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # GUARD CLAUSE: Strict check
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
        
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set <amount>")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # GUARD CLAUSE: Ensure the query exists (it always does for buttons, but Pylance checks)
    if not query:
        return

    await query.answer()

    # GUARD CLAUSE: Ensure we have a user
    if not query.from_user:
        return
        
    user_id = query.from_user.id

    if query.data == 'set_zero':
        await update_state_and_schedule(user_id, 0, context)
        await query.edit_message_text(
            text=f"✅ Timer reset! Count is 0/{WAVEPLATE_CAP}.\nI'll ping you in 24 hours.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Status", callback_data='status')]])
        )

    elif query.data == 'status':
        current = calculate_current_wp(user_id)
        if current is None:
            text = "Please run /start first."
        else:
            if current >= WAVEPLATE_CAP:
                text = f"⚡ **{WAVEPLATE_CAP}/{WAVEPLATE_CAP}**\nYou are capped!"
            else:
                minutes_left = get_time_to_cap(current) / 60
                full_time = datetime.now() + timedelta(seconds=get_time_to_cap(current))
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
                [InlineKeyboardButton("🔄 Refresh Status", callback_data='status')]
            ])
        )

# --- CORE LOGIC ---

async def update_state_and_schedule(user_id, amount, context):
    user_data_store[user_id] = {
        "timestamp": datetime.now(),
        "waveplates": amount
    }

    if context.job_queue:
        current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
        for job in current_jobs:
            job.schedule_removal()

        seconds_to_wait = get_time_to_cap(amount)
        if seconds_to_wait > 0:
            context.job_queue.run_once(
                notify_cap, 
                seconds_to_wait, 
                chat_id=user_id, 
                name=str(user_id)
            )

async def notify_cap(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    # GUARD CLAUSE: Ensure the job and chat_id exist
    if not job or not job.chat_id:
        return
        
    await context.bot.send_message(
        chat_id=job.chat_id, 
        text=f"🚨 **WAVEPLATES FULL ({WAVEPLATE_CAP}/{WAVEPLATE_CAP})**\nGo farm before you overcap!",
        parse_mode='Markdown'
    )

# --- MAIN ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_manual))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("Bot is running...")
    application.run_polling()