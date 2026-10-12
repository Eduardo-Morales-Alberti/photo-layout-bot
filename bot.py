"""Telegram bot: collect photos, pack them tightly onto A4 pages, send them back.

Send any number of photos (best quality: send them as *files*). Take your time;
nothing is built until you send /process. Then the bot reorders and rotates the
images to fit the most per A4 page, with no margins, and returns the pages as
a single PDF (split only if it would exceed the size limit), sent as a document
so Telegram does not recompress it.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime

from PIL import Image
from pillow_heif import register_heif_opener
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import load_settings
from export import build_pdfs
from layout import compose_pages, free_slots, image_size, plan_pages

register_heif_opener()  # iPhone photos sent as files are HEIC

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("picturetelegram")
# httpx logs every request URL at INFO, and those URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

SETTINGS = load_settings()

# Bots cannot download files larger than this from Telegram.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

WELCOME = (
    "📄 *A4 Photo Packer*\n\n"
    "Send me photos and I'll pack them onto A4 pages as tightly as possible "
    "(max 13x16 cm each, aspect ratio kept, rotated when it helps).\n\n"
    "• For best quality, send the images *as files* (no compression, max 20 MB each).\n"
    "• Add as many as you like — take your time.\n"
    "• When you're done, send /process.\n"
    "• /reset clears the current batch.\n"
)


def _buffer(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Encoded image bytes waiting in this chat's batch."""
    return context.chat_data.setdefault("images", [])


def _cancel_ack(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["images"] = []
    _cancel_ack(context, update.effective_chat.id)
    await update.effective_message.reply_text("🗑️ Batch cleared.")


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an incoming photo/document image into the per-chat batch.

    Only the encoded bytes are kept; images are decoded one at a time when the
    pages are rendered, so a large batch does not fill the memory.
    """
    msg = update.effective_message
    if msg.photo:
        source = msg.photo[-1]
    elif msg.document:
        source = msg.document
    else:
        return

    if source.file_size and source.file_size > MAX_DOWNLOAD_BYTES:
        await msg.reply_text(
            "⚠️ That file is larger than 20 MB, the most a Telegram bot can "
            "download. Please send a smaller version."
        )
        return

    try:
        tg_file = await source.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        image_size(data)  # reads only the header: rejects non-images early
    except Exception:  # noqa: BLE001
        log.exception("Failed to read incoming image")
        await msg.reply_text("⚠️ Couldn't read that image, skipping it.")
        return

    _buffer(context).append(data)

    # Debounce a single acknowledgement so bulk sends don't spam the chat.
    chat_id = update.effective_chat.id
    _cancel_ack(context, chat_id)
    context.job_queue.run_once(_ack_job, SETTINGS.debounce_seconds,
                               chat_id=chat_id, name=str(chat_id))


async def _ack_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    n = len(context.chat_data.get("images", []))
    if n:
        await context.bot.send_message(
            chat_id, f"📥 {n} image(s) in the batch. Send more, or /process to build the pages."
        )


async def process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _process(context, update.effective_chat.id, force=False)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer the "last page is not full" question."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    chat_id = query.message.chat.id
    if query.data == "gen_now":
        await _process(context, chat_id, force=True)
    else:
        await context.bot.send_message(
            chat_id, "👍 Send more photos, then /process to build the pages."
        )


async def _process(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force: bool) -> None:
    """Build and send the queued batch.

    Unless force is set, a last page with room for more photos is not sent:
    its photos stay queued and the user is asked what to do.
    """
    _cancel_ack(context, chat_id)

    if context.chat_data.get("busy"):
        await context.bot.send_message(chat_id, "⏳ Already working on the previous batch.")
        return

    # Take the batch out so photos arriving meanwhile start a new one.
    batch = context.chat_data.get("images", [])
    if not batch:
        await context.bot.send_message(chat_id, "No images yet — send some photos first.")
        return
    context.chat_data["images"] = []

    context.chat_data["busy"] = True
    try:
        held = await _render_and_send(context, chat_id, batch, force)
        context.chat_data["images"] = held + context.chat_data.get("images", [])
    except Exception:  # noqa: BLE001
        log.exception("Processing failed")
        # Put the batch back so the user can retry with /process.
        context.chat_data["images"] = batch + context.chat_data.get("images", [])
        await context.bot.send_message(
            chat_id, "⚠️ Something went wrong while building the pages. Send /process to retry."
        )
    finally:
        context.chat_data["busy"] = False


def _encode_image(page: Image.Image) -> bytes:
    dpi = SETTINGS.layout.dpi
    bio = io.BytesIO()
    if SETTINGS.out_format == "PNG":
        page.save(bio, format="PNG", optimize=True, dpi=(dpi, dpi))
    else:
        page.save(bio, format="JPEG", quality=SETTINGS.jpeg_quality,
                  subsampling=0, dpi=(dpi, dpi))
    return bio.getvalue()


FREE_SLOTS_LIMIT = 10


def _build(batch: list, force: bool):
    """CPU-heavy work, run in a worker thread: plan, compose and encode.

    Returns (composition, output, held, room): held are the batch indexes of
    the photos kept back from a last page with room for `room` more photos.
    composition and output are None when no page is left to send.
    """
    plan = plan_pages(batch, SETTINGS.layout)
    held: list = []
    room = 0
    if not force and SETTINGS.hold_partial_page:
        room = free_slots(plan, SETTINGS.layout, FREE_SLOTS_LIMIT)
        if room:
            held = [idx for idx, *_rest in plan.bins[-1]]
            plan.bins = plan.bins[:-1]
    if not plan.bins:
        return None, None, held, room

    comp = compose_pages(batch, SETTINGS.layout, plan)
    if SETTINGS.out_format == "PDF":
        result = build_pdfs(comp.pages, SETTINGS.layout.dpi, SETTINGS.max_pdf_bytes,
                            SETTINGS.jpeg_quality, SETTINGS.min_jpeg_quality,
                            SETTINGS.min_dpi)
        return comp, result, held, room
    return comp, [_encode_image(page) for page in comp.pages], held, room


async def _render_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                           batch: list, force: bool) -> list:
    """Send the pages; return the photos held back for a later page."""
    await context.bot.send_message(chat_id, f"🛠️ Packing {len(batch)} image(s)…")
    comp, output, held, room = await asyncio.to_thread(_build, batch, force)
    if comp is not None:
        await _send_pages(context, chat_id, comp, output, len(batch) - len(held))
    if held:
        more = f"{room}+" if room >= FREE_SLOTS_LIMIT else str(room)
        await context.bot.send_message(
            chat_id,
            f"📄 The last page would only have {len(held)} photo(s), with room for "
            f"about {more} more. I kept them in the queue.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🖨️ Generate it now", callback_data="gen_now"),
                InlineKeyboardButton("➕ Add more photos", callback_data="add_more"),
            ]]),
        )
    return [batch[idx] for idx in held]


async def _send_pages(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      comp, output, n_photos: int) -> None:
    n_pages = len(comp.pages)
    ts = datetime.now().strftime("%y%m%d%H%M%S")

    if SETTINGS.out_format == "PDF":
        files = output.files
        for i, data in enumerate(files, 1):
            suffix = f"_{i}of{len(files)}" if len(files) > 1 else ""
            caption = f"{n_photos} photo(s) on {n_pages} A4 page(s)"
            if len(files) > 1:
                caption += f", part {i}/{len(files)}"
            caption += f" · {output.dpi} DPI, JPEG quality {output.quality}"
            await context.bot.send_document(
                chat_id,
                document=InputFile(io.BytesIO(data), filename=f"picture_bot_{ts}{suffix}.pdf"),
                caption=caption,
            )
    else:
        ext = "png" if SETTINGS.out_format == "PNG" else "jpg"
        for i, data in enumerate(output, 1):
            await context.bot.send_document(
                chat_id,
                document=InputFile(io.BytesIO(data), filename=f"picture_bot_{ts}_{i}.{ext}"),
                caption=f"Page {i}/{n_pages}",
            )

    low = [(idx + 1, dpi) for idx, dpi in comp.print_dpi if dpi < SETTINGS.low_dpi_warning]
    if low:
        details = ", ".join(f"#{n} ({dpi} DPI)" for n, dpi in low)
        await context.bot.send_message(
            chat_id,
            f"⚠️ Low resolution, these may print blurry: {details}. "
            "Send the original file for a sharper print.",
        )

    log.info("Sent %d page(s) to chat %s", n_pages, chat_id)


def main() -> None:
    app = Application.builder().token(SETTINGS.bot_token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    # block=False: other updates keep flowing while a batch is being built;
    # photos are still handled one by one, so their order is kept.
    app.add_handler(CommandHandler(["process", "done"], process, block=False))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^(gen_now|add_more)$",
                                         block=False))
    app.add_handler(MessageHandler(
        filters.PHOTO
        | filters.Document.IMAGE
        # Some clients send HEIC files without an image/* MIME type.
        | filters.Document.FileExtension("heic")
        | filters.Document.FileExtension("heif"),
        collect,
    ))

    log.info("Bot started (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
