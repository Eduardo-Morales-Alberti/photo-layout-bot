"""Application settings, loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

from layout import Settings


@dataclass
class AppSettings:
    bot_token: str
    debounce_seconds: float
    out_format: str          # "PDF", "PNG" or "JPEG"
    jpeg_quality: int        # JPEG quality (the starting quality for PDF)
    max_pdf_bytes: int
    min_jpeg_quality: int    # PDF: lowest quality tried to stay under the limit
    min_dpi: int             # PDF: lowest DPI tried to stay under the limit
    low_dpi_warning: int     # warn when an image prints below this DPI
    hold_partial_page: bool  # keep a not-full last page queued and ask first
    layout: Settings


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> AppSettings:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN environment variable is required.")

    layout = Settings(
        dpi=_i("DPI", 300),
        max_long_cm=_f("MAX_LONG_CM", 16.0),
        max_short_cm=_f("MAX_SHORT_CM", 13.0),
        min_long_cm=_f("MIN_LONG_CM", 12.0),
        min_short_cm=_f("MIN_SHORT_CM", 9.0),
        margin_cm=_f("MARGIN_CM", 0.0),
        gap_cm=_f("GAP_CM", 0.0),
        allow_upscale=_b("ALLOW_UPSCALE", True),
        allow_rotate=_b("ALLOW_ROTATE", True),
        fill_page=_b("FILL_PAGE", True),
    )

    out_format = os.getenv("OUT_FORMAT", "PDF").strip().upper()
    if out_format not in ("PDF", "PNG", "JPEG"):
        out_format = "PDF"

    return AppSettings(
        bot_token=token,
        debounce_seconds=_f("DEBOUNCE_SECONDS", 1.5),
        out_format=out_format,
        jpeg_quality=_i("JPEG_QUALITY", 95),
        # Bots can upload documents of up to 50 MB; keep a safety margin.
        max_pdf_bytes=int(_f("MAX_PDF_MB", 45) * 1024 * 1024),
        min_jpeg_quality=_i("MIN_JPEG_QUALITY", 75),
        min_dpi=_i("MIN_DPI", 200),
        low_dpi_warning=_i("LOW_DPI_WARNING", 150),
        hold_partial_page=_b("HOLD_PARTIAL_PAGE", True),
        layout=layout,
    )
