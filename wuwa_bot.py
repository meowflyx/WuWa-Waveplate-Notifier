import logging
import json
import os
import tempfile
from datetime import datetime, timedelta
from typing import Dict, Optional, Any
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
WAVEPLATE_CAP = 240
REGEN_RATE_MINUTES = 6
DB_FILE = "wuwa_db.json"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data: Dict[int, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
                self.data = {
                    int(uid): {
                        "waveplates": d["waveplates"],
                        "timestamp": datetime.fromisoformat(d["timestamp"])
                    }
                    for uid, d in raw_data.items()
                }
            logger.info(f"Database loaded. Users found: {len(self.data)}")
        except Exception as e:
            logger.error(f"Error loading DB: {e}")

    def save(self):
        try:
            export_data = {
                str(uid): {
                    "waveplates": d["waveplates"],
                    "timestamp": d["timestamp"].isoformat()
                }
                for uid, d in self.data.items()
            }
            
            fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.filepath)), text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=4)
            
            os.replace(temp_path, self.filepath)
        except Exception as e:
            logger.error(f"Error saving DB: {e}")

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.data.get(user_id)

    def update_user(self, user_id: int, waveplates: int):
        self.data[user_id] = {
            "waveplates": waveplates,
            "timestamp": datetime.now()
        }
        self.save()

db = DatabaseManager(DB_FILE)

class WaveplateCalculator:
    @staticmethod
    def calculate_current(user_data: Dict[str, Any]) -> int:
        elapsed_minutes = (datetime.now() - user_data["timestamp"]).total_seconds() / 60
        regenerated = int(elapsed_minutes / REGEN_RATE_MINUTES)
        total = user_data["waveplates"] + regenerated
        return min(total, WAVEPLATE_CAP)

    @staticmethod
    def get_seconds_to_cap(current_wp: int) -> float:
        if current_wp >= WAVEPLATE_CAP:
            return 0.0
        missing = WAVEPLATE_CAP - current_wp
        return missing * REGEN_RATE_MINUTES * 60

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔄 Status"), KeyboardButton("🌊 I spent it ALL (Reset)")]
        ],
        resize_keyboard=True
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    
    if not db.get_user(user_id):
        db.update_user(user_id, WAVEPLATE_CAP)
    
    await update.message.reply_text(
        " **Welcome to WuWa Waveplate Tracker!**\n\n"
        "The menu is below 👇\n"
        "Use `/set <amount>` to manually set your current waveplates.\n"
        "Use the buttons to check status or reset to 0.",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text button presses."""
    if not update.effective_user or not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text

    if text == "🌊 I spent it ALL (Reset)":
        await update_state_and_schedule(user_id, 0, context)
        await update.message.reply_text(
            f"✅ **Timer Reset!**\n"
            f"Count is 0/{WAVEPLATE_CAP}.\n"
            f"I'll ping you when full.",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

    elif text == "🔄 Status":
        user_data = db.get_user(user_id)
        if not user_data:
            response = "⚠️ Please run /start first."
        else:
            current = WaveplateCalculator.calculate_current(user_data)
            if current >= WAVEPLATE_CAP:
                response = f"⚡ **{WAVEPLATE_CAP}/{WAVEPLATE_CAP}**\nWaveplates full! Go farm!"
            else:
                seconds_left = WaveplateCalculator.get_seconds_to_cap(current)
                minutes_left = seconds_left / 60
                full_time = datetime.now() + timedelta(seconds=seconds_left)
                response = (
                    f"🌊 Current: **{current}/{WAVEPLATE_CAP}**\n"
                    f"⏳ Full in: `{int(minutes_left // 60)}h {int(minutes_left % 60)}m`\n"
                    f"📅 Cap time: `{full_time.strftime('%H:%M')}`"
                )
        
        await update.message.reply_text(
            response, 
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )

async def set_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/set <amount>`", parse_mode='Markdown')
        return

    user_id = update.effective_user.id
    try:
        amount = int(context.args[0])
        if amount < 0 or amount > WAVEPLATE_CAP:
            await update.message.reply_text(f"⚠️ Amount must be between 0 and {WAVEPLATE_CAP}.")
            return
        
        await update_state_and_schedule(user_id, amount, context)
        await update.message.reply_text(
            f"✅ Updated. Tracking from **{amount}/{WAVEPLATE_CAP}**.",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )
        
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")

async def update_state_and_schedule(user_id, amount, context):
    db.update_user(user_id, amount)
    await schedule_notification(user_id, amount, context)

async def schedule_notification(user_id, amount, context):
    if not context.job_queue:
        return

    current_jobs = context.job_queue.get_jobs_by_name(str(user_id))
    for job in current_jobs:
        job.schedule_removal()

    seconds_to_wait = WaveplateCalculator.get_seconds_to_cap(amount)
    
    if seconds_to_wait > 0:
        context.job_queue.run_once(
            notify_cap, 
            seconds_to_wait, 
            chat_id=user_id, 
            name=str(user_id)
        )

async def notify_cap(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job or not job.chat_id:
        return
        
    await context.bot.send_message(
        chat_id=job.chat_id, 
        text=f"🚨 **WAVEPLATES FULL ({WAVEPLATE_CAP}/{WAVEPLATE_CAP})**\nGo farm before you overcap!",
        parse_mode='Markdown'
    )

async def restore_jobs(application):
    logger.info("Restoring timers from database...")
    if not application.job_queue:
        return

    for user_id, data in db.data.items():
        current_wp = WaveplateCalculator.calculate_current(data)

        if current_wp < WAVEPLATE_CAP:
            seconds_left = WaveplateCalculator.get_seconds_to_cap(current_wp)
            application.job_queue.run_once(
                notify_cap,
                seconds_left,
                chat_id=user_id,
                name=str(user_id)
            )
    logger.info("Timers restored.")

if __name__ == '__main__':
    if not TOKEN:
        print("Error: BOT_TOKEN is not set in environment variables or code.")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_manual))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if application.job_queue:
        application.job_queue.run_once(lambda ctx: restore_jobs(application), 1)

    print("Bot is running...")
    application.run_polling()