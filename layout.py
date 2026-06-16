"""A4 page composition with tight rectangle packing.

Each image is scaled to fit the maximum print box (13x16 / 16x13 cm) keeping
its aspect ratio, then images are packed onto A4 pages as densely as possible:

  * no margins or gaps by default — images fill the sheet edge to edge,
  * each image may be rotated 90 deg if that packs better,
  * the whole batch is reordered (several orderings are tried) to fit the most
    images per page.

Packing uses the MaxRects algorithm (Best-Short-Side-Fit), a well-known and
effective 2D bin-packing heuristic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageOps

# A4 paper, portrait orientation.
A4_WIDTH_CM = 21.0
A4_HEIGHT_CM = 29.7


@dataclass
class Settings:
    """Layout configuration (all distances in centimetres)."""

    dpi: int = 300
    max_long_cm: float = 16.0       # longest side of an image's bounding box
    max_short_cm: float = 13.0      # shortest side of an image's bounding box
    min_long_cm: float = 12.0       # smallest the long side may shrink to
    min_short_cm: float = 9.0       # smallest the short side may shrink to
    margin_cm: float = 0.0          # white margin around the page (0 = fill)
    gap_cm: float = 0.0             # spacing between images (0 = touching)
    allow_upscale: bool = True      # scale small images up to fill the box
    allow_rotate: bool = True       # rotate images 90 deg to pack better
    fill_page: bool = True          # grow a page's images to fill leftover space
    background: tuple = (255, 255, 255)


def cm_to_px(cm: float, dpi: int) -> int:
    """Convert centimetres to pixels at the given DPI."""
    return round(cm / 2.54 * dpi)


def _geometry(s: Settings):
    """Return (page_w, page_h, margin, gap, usable_w, usable_h) in pixels."""
    page_w = cm_to_px(A4_WIDTH_CM, s.dpi)
    page_h = cm_to_px(A4_HEIGHT_CM, s.dpi)
    margin = cm_to_px(s.margin_cm, s.dpi)
    gap = cm_to_px(s.gap_cm, s.dpi)
    return page_w, page_h, margin, gap, page_w - 2 * margin, page_h - 2 * margin


def _target_dims(w: int, h: int, s: Settings, f: float) -> Tuple[int, int]:
    """Size (px) of a w*h image scaled to fit the box shrunk by factor f."""
    max_long = cm_to_px(s.max_long_cm, s.dpi) * f
    max_short = cm_to_px(s.max_short_cm, s.dpi) * f

    # Align the image's long side with the box's long side.
    if w >= h:
        box_w, box_h = max_long, max_short
    else:
        box_w, box_h = max_short, max_long

    scale = min(box_w / w, box_h / h)
    if not s.allow_upscale:
        scale = min(scale, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


def _target_size(img: Image.Image, s: Settings) -> Tuple[int, int]:
    """Size (px) of an image at the maximum box (no shrink)."""
    return _target_dims(img.size[0], img.size[1], s, 1.0)


def _scale_candidates(s: Settings) -> List[float]:
    """Box scale factors to try, from 1.0 (max box) down to the min box."""
    fl = s.min_long_cm / s.max_long_cm if s.max_long_cm > 0 else 1.0
    fs = s.min_short_cm / s.max_short_cm if s.max_short_cm > 0 else 1.0
    f_min = min(1.0, max(0.05, fl, fs))  # respect both minimums
    if f_min >= 1.0:
        return [1.0]
    steps = []
    f = 1.0
    while f > f_min + 1e-9:
        steps.append(round(f, 4))
        f -= 0.05
    steps.append(round(f_min, 4))
    return steps


# --------------------------------------------------------------------------- #
# MaxRects bin packing
# --------------------------------------------------------------------------- #

Rect = Tuple[int, int, int, int]  # x, y, w, h


class _MaxRects:
    """Single-bin MaxRects packer using the Best-Short-Side-Fit heuristic."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.free: List[Rect] = [(0, 0, width, height)]

    def insert(self, w: int, h: int, rotate: bool) -> Optional[Tuple[int, int, int, int, bool]]:
        best: Optional[Tuple[int, int, int, int, int, bool]] = None  # s1,s2,x,y,..,rot
        orientations = [(w, h, False)]
        if rotate and w != h:
            orientations.append((h, w, True))

        for fx, fy, fw, fh in self.free:
            for rw, rh, rot in orientations:
                if rw <= fw and rh <= fh:
                    leftover_h = fw - rw
                    leftover_v = fh - rh
                    s1 = min(leftover_h, leftover_v)
                    s2 = max(leftover_h, leftover_v)
                    if best is None or (s1, s2) < (best[0], best[1]):
                        best = (s1, s2, fx, fy, rw, rh, rot)

        if best is None:
            return None

        _, _, x, y, rw, rh, rot = best
        self._place((x, y, rw, rh))
        return x, y, rw, rh, rot

    def _place(self, rect: Rect) -> None:
        new_free: List[Rect] = []
        for f in self.free:
            if self._overlaps(f, rect):
                new_free.extend(self._split(f, rect))
            else:
                new_free.append(f)
        self.free = self._prune(new_free)

    @staticmethod
    def _overlaps(f: Rect, r: Rect) -> bool:
        fx, fy, fw, fh = f
        rx, ry, rw, rh = r
        return not (rx >= fx + fw or rx + rw <= fx or ry >= fy + fh or ry + rh <= fy)

    @staticmethod
    def _split(f: Rect, r: Rect) -> List[Rect]:
        fx, fy, fw, fh = f
        rx, ry, rw, rh = r
        pieces: List[Rect] = []
        if rx > fx and rx < fx + fw:
            pieces.append((fx, fy, rx - fx, fh))                 # left slab
        if rx + rw < fx + fw and rx + rw > fx:
            pieces.append((rx + rw, fy, fx + fw - (rx + rw), fh))  # right slab
        if ry > fy and ry < fy + fh:
            pieces.append((fx, fy, fw, ry - fy))                 # top slab
        if ry + rh < fy + fh and ry + rh > fy:
            pieces.append((fx, ry + rh, fw, fy + fh - (ry + rh)))  # bottom slab
        return pieces

    @staticmethod
    def _contains(a: Rect, b: Rect) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return bx >= ax and by >= ay and bx + bw <= ax + aw and by + bh <= ay + ah

    def _prune(self, rects: List[Rect]) -> List[Rect]:
        remove = set()
        n = len(rects)
        for i in range(n):
            for j in range(i + 1, n):
                if i in remove or j in remove:
                    continue
                if self._contains(rects[j], rects[i]):
                    remove.add(i)
                elif self._contains(rects[i], rects[j]):
                    remove.add(j)
        return [r for k, r in enumerate(rects) if k not in remove]


# Placement on a page: (item_index, x, y, w, h, rotated)
Placement = Tuple[int, int, int, int, int, bool]


def _pack_order(order: List[Tuple[int, int, int]], bin_w: int, bin_h: int,
                rotate: bool) -> List[List[Placement]]:
    """Pack items (idx, w, h) into bins in the given order; new bin on overflow."""
    remaining = list(order)
    bins: List[List[Placement]] = []
    while remaining:
        packer = _MaxRects(bin_w, bin_h)
        placed: List[Placement] = []
        leftover: List[Tuple[int, int, int]] = []
        for idx, w, h in remaining:
            res = packer.insert(w, h, rotate)
            if res is None:
                leftover.append((idx, w, h))
            else:
                x, y, pw, ph, rot = res
                placed.append((idx, x, y, pw, ph, rot))
        if not placed:
            break  # safety: item larger than the bin (should not happen)
        bins.append(placed)
        remaining = leftover
    return bins


def _pack(items: List[Tuple[int, int, int]], bin_w: int, bin_h: int,
          s: Settings) -> List[List[Placement]]:
    """Try several orderings and keep the densest result (fewest pages)."""
    sort_keys = (
        lambda it: -(it[1] * it[2]),       # largest area first
        lambda it: -max(it[1], it[2]),     # longest side first
        lambda it: -it[2],                 # tallest first
        lambda it: -it[1],                 # widest first
    )
    best_bins: Optional[List[List[Placement]]] = None
    best_score: Optional[Tuple[int, int]] = None
    for key in sort_keys:
        bins = _pack_order(sorted(items, key=key), bin_w, bin_h, s.allow_rotate)
        if not bins:
            continue
        last_fill = sum(p[3] * p[4] for p in bins[-1])
        score = (len(bins), last_fill)  # fewest pages, then emptiest last page
        if best_score is None or score < best_score:
            best_score, best_bins = score, bins
    return best_bins or []


# Final placement after grow-to-fill: (idx, x, y, draw_w, draw_h, rotated)
# draw_w/draw_h are the unrotated draw size; rotation is applied at render time.
FinalPlacement = Tuple[int, int, int, int, int, bool]


def _grow_to_fill(bins: List[List[Placement]], raw: List[Tuple[int, int]],
                  f: float, usable_w: int, usable_h: int,
                  enabled: bool) -> List[List[FinalPlacement]]:
    """Scale each page's content up uniformly until it touches an edge.

    The growth is capped at 1/f so no image exceeds the maximum box (it can at
    most return to its full max size). Aspect ratios and the layout are kept,
    so growth stops at the first edge reached; the other axis may keep slack.
    """
    cap = (1.0 / f) if (enabled and f > 0) else 1.0
    pages: List[List[FinalPlacement]] = []
    for page in bins:
        bbox_w = max(x + pw for _, x, _y, pw, _ph, _ in page)
        bbox_h = max(y + ph for _, _x, y, _pw, ph, _ in page)
        k = min(usable_w / bbox_w, usable_h / bbox_h, cap)
        if k < 1.0:
            k = 1.0
        final: List[FinalPlacement] = []
        for idx, x, y, _pw, _ph, rot in page:
            rw, rh = raw[idx]
            final.append((idx, round(x * k), round(y * k),
                          max(1, round(rw * k)), max(1, round(rh * k)), rot))
        pages.append(final)
    return pages


def _render(pages: List[List[FinalPlacement]], sources: List[Image.Image],
            s: Settings, margin: int) -> List[Image.Image]:
    """Draw each packed page onto a white A4 canvas, resizing from source."""
    page_w, page_h, *_ = _geometry(s)
    out: List[Image.Image] = []
    for page in pages:
        canvas = Image.new("RGB", (page_w, page_h), s.background)
        for idx, x, y, w, h, rot in page:
            im = sources[idx].resize((w, h), Image.LANCZOS)
            if rot:
                im = im.transpose(Image.Transpose.ROTATE_90)
            ox, oy = margin + x, margin + y
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                canvas.paste(rgba, (ox, oy), rgba)
            else:
                canvas.paste(im, (ox, oy))
        out.append(canvas)
    return out


def _sizes_at(dims, s: Settings, f: float, usable_w: int, usable_h: int):
    """Per-image (w, h) in px at box-scale f, clamped to the usable page area."""
    raw: List[Tuple[int, int]] = []
    for w, h in dims:
        tw, th = _target_dims(w, h, s, f)
        fit = min(usable_w / tw, usable_h / th, 1.0)
        if fit < 1.0:
            tw, th = max(1, round(tw * fit)), max(1, round(th * fit))
        raw.append((tw, th))
    return raw


def compose_pages(images: List[Image.Image], s: Settings) -> List[Image.Image]:
    """Compose images onto A4 pages, shrinking within [min, max] box to fit more.

    Tries box scales from the max box down to the min box and keeps the
    arrangement with the fewest pages (least whitespace); among ties it keeps
    the largest scale, so images stay as big as allowed. Only dimensions are
    used during the search — pixels are resized once, for the chosen scale.
    """
    _, _, margin, gap, usable_w, usable_h = _geometry(s)

    oriented = [ImageOps.exif_transpose(img) for img in images]
    dims = [im.size for im in oriented]

    best = None  # (score, f, raw_sizes, bins)
    for f in _scale_candidates(s):
        raw = _sizes_at(dims, s, f, usable_w, usable_h)
        items = [(i, w + gap, h + gap) for i, (w, h) in enumerate(raw)]
        bins = _pack(items, usable_w + gap, usable_h + gap, s)
        score = (len(bins), -f)  # fewest pages, then largest images
        if best is None or score < best[0]:
            best = (score, f, raw, bins)

    _, f, raw, bins = best
    pages = _grow_to_fill(bins, raw, f, usable_w, usable_h, s.fill_page)
    return _render(pages, oriented, s, margin)
