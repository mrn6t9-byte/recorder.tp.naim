"""Telegram bot starter with honest status messaging.

This is a helper bot only. It does not control a running GitHub Actions job.
A persistent host plus GitHub API integration is needed for reliable live
/status, /split, /slipt and /stop control.
"""
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📖 Setup guide", callback_data="guide")],
    [InlineKeyboardButton("ℹ️ Status / limitations", callback_data="status")],
])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎬 Stream Recorder Bot\n\n"
        "Start recording in GitHub: Repository → Actions → Stream Recorder → Run workflow.\n\n"
        "This bot is not connected to a live runner, so it cannot yet stop/split an active recording.",
        reply_markup=KEYBOARD,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "ℹ️ Bot is online. Live recording status is unavailable because this starter "
        "bot is not connected to GitHub Actions run control."
    )


async def split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚠️ /split is not connected to the running recorder in this version."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚠️ /stop is not connected to GitHub Actions in this version."
    )


async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "guide":
        await query.edit_message_text(
            "GitHub repository → Actions → Stream Recorder → Run workflow. "
            "Use only a stream URL you are authorized to record."
        )
    else:
        await query.edit_message_text(
            "This bot can show help but does not control an active job. "
            "Artifacts upload after the recording job ends."
        )


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN before running bot.py.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("split", split))
    app.add_handler(CommandHandler("slipt", split))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(__import__("telegram.ext", fromlist=["CallbackQueryHandler"]).CallbackQueryHandler(guide))
    app.run_polling()


if __name__ == "__main__":
    main()
