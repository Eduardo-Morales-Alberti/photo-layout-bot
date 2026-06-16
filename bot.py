"""Telegram bot: collect photos, pack them tightly onto A4 pages, send them back.

Send any number of photos (best quality: send them as *files*). Take your time;
nothing is built until you send /process. Then the bot reorders and rotates the
images to fit the most per A4 page, with no margins, and returns one page image
per page as a document so Telegram does not recompress it.
"""
from __future__ import annotations

import io
import logging

from PIL import Image
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import load_settings
from layout import compose_pages

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("picturetelegram")

SETTINGS = load_settings()

WELCOME = (
    "📄 *A4 Photo Packer*\n\n"
    "Send me photos and I'll pack them onto A4 pages as tightly as possible "
    "(max 13x16 cm each, aspect ratio kept, rotated when it helps).\n\n"
    "• For best quality, send the images *as files* (no compression).\n"
    "• Add as many as you like — take your time.\n"
    "• When you're done, send /process and I'll build the pages.\n"
    "• /reset clears the current batch.\n"
)


def _buffer(context: ContextTypes.DEFAULT_TYPE) -> list:
    return context.chat_data.setdefault("images", [])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _buffer(context).clear()
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await update.effective_message.reply_text("🗑️ Batch cleared.")


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an incoming photo/document image into the per-chat batch."""
    msg = update.effective_message
    try:
        if msg.photo:
            tg_file = await msg.photo[-1].get_file()
        elif msg.document:
            tg_file = await msg.document.get_file()
        else:
            return
        data = bytes(await tg_file.download_as_bytearray())
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # noqa: BLE001
        log.exception("Failed to read incoming image")
        await msg.reply_text("⚠️ Couldn't read that image, skipping it.")
        return

    _buffer(context).append(image)

    # Debounce a single acknowledgement so bulk sends don't spam the chat.
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    context.job_queue.run_once(_ack_job, 1.5, chat_id=chat_id, name=str(chat_id))


async def _ack_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    n = len(context.chat_data.get("images", []))
    if n:
        await context.bot.send_message(
            chat_id, f"📥 {n} image(s) in the batch. Send more, or /process to build the pages."
        )


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await _render_and_send(context, chat_id)


async def _render_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    images = context.chat_data.get("images", [])
    if not images:
        await context.bot.send_message(chat_id, "No images yet — send some photos first.")
        return

    await context.bot.send_message(chat_id, f"🛠️ Packing {len(images)} image(s)…")
    try:
        pages = compose_pages(images, SETTINGS.layout)
    except Exception:  # noqa: BLE001
        log.exception("Composition failed")
        await context.bot.send_message(chat_id, "⚠️ Something went wrong while composing the pages.")
        return

    ext = "png" if SETTINGS.out_format == "PNG" else "jpg"
    dpi = SETTINGS.layout.dpi
    for i, page in enumerate(pages, 1):
        bio = io.BytesIO()
        if SETTINGS.out_format == "PNG":
            page.save(bio, format="PNG", optimize=True, dpi=(dpi, dpi))
        else:
            page.save(bio, format="JPEG", quality=SETTINGS.jpeg_quality,
                      subsampling=0, dpi=(dpi, dpi))
        bio.seek(0)
        await context.bot.send_document(
            chat_id,
            document=InputFile(bio, filename=f"page_{i}.{ext}"),
            caption=f"Page {i}/{len(pages)}",
        )

    context.chat_data["images"] = []
    log.info("Sent %d page(s) to chat %s", len(pages), chat_id)


def main() -> None:
    app = Application.builder().token(SETTINGS.bot_token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler(["process", "done"], process))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, collect))

    log.info("Bot started (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
