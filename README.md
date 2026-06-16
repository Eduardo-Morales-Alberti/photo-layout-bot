# picturetelegram

A Telegram bot that receives photos and packs them onto **A4 pages** as tightly
as possible (each image fits a **13x16 cm** / **16x13 cm** box, aspect ratio
kept), then sends each page back as a high-quality image.

Pages are returned **as documents**, so Telegram does not recompress them.
Default output is lossless **PNG** at 300 DPI with the correct print size embedded.

## How it works

1. Each image is scaled to fit the max box (13x16 cm) keeping its aspect ratio.
2. When you send `/process`, the whole batch is packed onto A4 sheets with a
   MaxRects bin-packing algorithm: **no margins or gaps**, images **rotated 90°**
   when that helps, and the order tried several ways to fit the **most images
   per page**.
3. To cut whitespace, images may **shrink toward a minimum box** (12x9 cm). The
   bot searches box sizes from max down to min and keeps the layout with the
   fewest pages; among equal results it keeps the largest size, so images only
   shrink as much as is needed to fit another one. Set `MIN_*` equal to `MAX_*`
   to disable this.
4. After packing, each page's images are **grown uniformly to fill** the leftover
   space until they reach an edge (`FILL_PAGE`). Growth is capped at the **13x16
   max box**, so images never print larger than the maximum. Because aspect
   ratios are kept, only one axis (bottom *or* right) is filled; a page that is
   already at max size keeps its slack.
5. Each finished A4 page is sent back as a `page_N.png` document.

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
  *File*), so Telegram doesn't pre-compress them. Take your time.
- When you're done adding images, send `/process` to build and receive the pages.
- `/reset` — discard the current batch.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** Token from BotFather. |
| `DPI` | `300` | Print resolution. Higher = larger files. |
| `MAX_LONG_CM` | `16` | Longest side of each image's box. |
| `MAX_SHORT_CM` | `13` | Shortest side of each image's box. |
| `MIN_LONG_CM` | `12` | Smallest the long side may shrink to (to fit more). |
| `MIN_SHORT_CM` | `9` | Smallest the short side may shrink to. |
| `MARGIN_CM` | `0` | Page margin (0 = fill to the edge). |
| `GAP_CM` | `0` | Spacing between images (0 = touching). |
| `ALLOW_UPSCALE` | `true` | Scale small images up to fill the box. |
| `ALLOW_ROTATE` | `true` | Rotate images 90° to pack more per page. |
| `FILL_PAGE` | `true` | Grow images to fill leftover space (never beyond max box). |
| `OUT_FORMAT` | `PNG` | `PNG` (lossless) or `JPEG`. |
| `JPEG_QUALITY` | `95` | Quality when `OUT_FORMAT=JPEG`. |
| `DEBOUNCE_SECONDS` | `2.5` | Delay before the "N images in batch" acknowledgement. |
