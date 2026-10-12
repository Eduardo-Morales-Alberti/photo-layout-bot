# picturetelegram

A Telegram bot that receives photos and packs them onto **A4 pages** as tightly
as possible (each image fits a **13x16 cm** / **16x13 cm** box, aspect ratio
kept), then sends them back as a single **PDF** ready to print.

The PDF is sent **as a document**, so Telegram does not recompress it. Pages
are 300 DPI with the real A4 size embedded, and the file is kept under a size
limit (45 MB by default; bots can upload at most 50 MB).

## How it works

1. Each image is scaled to fit the max box (13x16 cm) keeping its aspect ratio.
2. When you send `/process`, the whole batch is packed onto A4 sheets with a
   MaxRects bin-packing algorithm: **no margins or gaps**, images **rotated 90°**
   when that helps. Several placement rules, orderings and orientations are
   tried to fit the **most images per page**.
3. To cut whitespace, images may **shrink toward a minimum box** (12x9 cm). The
   bot searches box sizes from max down to min in 1% steps and keeps the layout with the
   fewest pages; among equal results it keeps the largest size, so images only
   shrink as much as is needed to fit another one. Set `MIN_*` equal to `MAX_*`
   to disable this.
4. After packing, each page's images are **grown uniformly to fill** the leftover
   space until they reach an edge (`FILL_PAGE`). Growth is capped at the **13x16
   max box**, so images never print larger than the maximum. Because aspect
   ratios are kept, only one axis (bottom *or* right) is filled; a page that is
   already at max size keeps its slack.
5. The pages are sent back as one PDF (`picture_bot_<timestamp>.pdf`) with the
   pages embedded as JPEG. If it would exceed `MAX_PDF_MB`, the bot lowers the
   JPEG quality first, then the DPI, and only as a last resort splits it into
   several PDFs. The caption shows the DPI and quality used.

Photos are rotated according to their EXIF data and converted to sRGB, so
iPhone (Display P3) or Adobe RGB photos print with the right colours. HEIC
files are supported. The bot warns about images that would print below
150 DPI.

## 1. Create a bot and get a token

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (name + username ending in `bot`).
3. BotFather replies with an **HTTP API token** like `123456789:AAAbExvD...`.
4. Copy it — that's your `BOT_TOKEN`.

## 2. Configure

```bash
cp .env.example .env
# edit .env and paste your BOT_TOKEN
```

All other settings are optional (see `.env.example`).

## 3. Run with Docker

```bash
docker compose up -d --build
docker compose logs -f        # watch logs
```

To stop:

```bash
docker compose down
```

### Run without Docker (optional)

```bash
pip install -r requirements.txt
BOT_TOKEN=your-token python bot.py
```

## 4. Use it

In Telegram, open your bot and:

- `/start` — instructions
- Send one or more photos. **For best quality send them as files** (paperclip →
  *File*), so Telegram doesn't pre-compress them. Each file can be at most
  20 MB (a Telegram limit for bots). Take your time.
- When you're done adding images, send `/process`. Full pages are sent right
  away. If the last page still has room for more photos, its photos stay in the
  queue and the bot asks: **Generate it now** or **Add more photos** (the
  queued photos are packed together with the new ones on the next `/process`).
- `/reset` — discard the current batch.

### Printing

Print the PDF at **100% / actual size**, never "fit to page", or the photos
will not have their real size. With `MARGIN_CM=0` the images reach the edge of
the sheet; most home printers leave 3 to 5 mm unprinted, so unless yours prints
borderless set `MARGIN_CM=0.4`.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** Token from BotFather. |
| `DPI` | `300` | Print resolution. Higher = larger files. |
| `MAX_LONG_CM` | `16` | Longest side of each image's box. |
| `MAX_SHORT_CM` | `13` | Shortest side of each image's box. |
| `MIN_LONG_CM` | `12` | Smallest the long side may shrink to (to fit more). |
| `MIN_SHORT_CM` | `9` | Smallest the short side of each photo may shrink to. Long, narrow photos (16:9) are kept larger to respect it; photos too narrow to reach it within `MAX_LONG_CM` (2:1) stay at their max size. |
| `MARGIN_CM` | `0` | Page margin (0 = fill to the edge). |
| `GAP_CM` | `0` | Spacing between images (0 = touching). |
| `ALLOW_UPSCALE` | `true` | Scale small images up to fill the box. |
| `ALLOW_ROTATE` | `true` | Rotate images 90° to pack more per page. |
| `FILL_PAGE` | `true` | Grow images to fill leftover space (never beyond max box). |
| `OUT_FORMAT` | `PDF` | `PDF` (one file), or `PNG` / `JPEG` (one file per page). |
| `JPEG_QUALITY` | `95` | JPEG quality; for PDF, the starting quality. |
| `MAX_PDF_MB` | `45` | Maximum size of each PDF (bots can upload up to 50 MB). |
| `MIN_JPEG_QUALITY` | `75` | Lowest quality used to stay under `MAX_PDF_MB`. |
| `MIN_DPI` | `200` | Lowest DPI used to stay under `MAX_PDF_MB`. |
| `LOW_DPI_WARNING` | `150` | Warn about images that print below this DPI. |
| `HOLD_PARTIAL_PAGE` | `true` | Keep a not-full last page queued and ask before printing it. |
| `DEBOUNCE_SECONDS` | `1.5` | Delay before the "N images in batch" acknowledgement. |
