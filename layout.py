"""A4 page composition with tight rectangle packing.

Each image is scaled to fit the maximum print box (13x16 / 16x13 cm) keeping
its aspect ratio, then images are packed onto A4 pages as densely as possible:

  * no margins or gaps by default — images fill the sheet edge to edge,
  * each image may be rotated 90 deg if that packs better,
  * the whole batch is reordered (several orderings are tried) to fit the most
    images per page.

Packing uses the MaxRects algorithm, a well-known and effective 2D bin-packing
heuristic. Several placement rules, orderings and orientations are tried and
the layout with the fewest pages wins.

Images are handled as encoded bytes: only their header is read while planning,
and each one is decoded (EXIF-rotated, converted to sRGB) just before it is
drawn, so a large batch never sits decoded in memory.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PIL import Image, ImageCms, ImageOps

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


@dataclass
class Composition:
    """Result of compose_pages."""

    pages: List[Image.Image]
    # (image index, effective print DPI) for every image, in input order.
    print_dpi: List[Tuple[int, int]] = field(default_factory=list)


def cm_to_px(cm: float, dpi: int) -> int:
    """Convert centimetres to pixels at the given DPI."""
    return round(cm / 2.54 * dpi)


# EXIF orientations that swap width and height.
_TRANSPOSED_ORIENTATIONS = (5, 6, 7, 8)
_SRGB = ImageCms.createProfile("sRGB")


def image_size(data: bytes) -> Tuple[int, int]:
    """Displayed (EXIF-rotated) size of an encoded image, reading only its header."""
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if im.getexif().get(0x0112, 1) in _TRANSPOSED_ORIENTATIONS:
            w, h = h, w
    return w, h


def load_image(data: bytes) -> Image.Image:
    """Decode an image: apply EXIF rotation, convert to sRGB, flatten to RGB."""
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)

    icc = im.info.get("icc_profile")
    if icc and im.mode in ("RGB", "CMYK", "L"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, _SRGB, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError):
            pass  # broken profile: fall back to a plain conversion

    if im.mode in ("RGBA", "LA", "PA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


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
    # Round down: rounding up can make sizes designed to tile the page exactly
    # (e.g. 12 + 9 cm = 21 cm) overflow it by one pixel.
    return max(1, math.floor(w * scale + 1e-6)), max(1, math.floor(h * scale + 1e-6))


SCALE_STEP = 0.01


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
        f -= SCALE_STEP
    steps.append(round(f_min, 4))
    return steps


# --------------------------------------------------------------------------- #
# MaxRects bin packing
# --------------------------------------------------------------------------- #

Rect = Tuple[int, int, int, int]  # x, y, w, h


# Placement rules: Best-Short-Side-Fit, Best-Long-Side-Fit, Best-Area-Fit and
# Bottom-Left. No single rule wins on every batch, so all of them are tried.
HEURISTICS = ("bssf", "blsf", "baf", "bl")


def _fit_score(heuristic: str, free: Rect, rw: int, rh: int) -> Tuple[int, int]:
    """Score of placing a rw*rh rect in a free rect (lower is better)."""
    fx, fy, fw, fh = free
    leftover_h, leftover_v = fw - rw, fh - rh
    short, long_ = min(leftover_h, leftover_v), max(leftover_h, leftover_v)
    if heuristic == "bssf":
        return short, long_
    if heuristic == "blsf":
        return long_, short
    if heuristic == "baf":
        return fw * fh - rw * rh, short
    return fy + rh, fx  # bottom-left


class _MaxRects:
    """Single-bin MaxRects packer."""

    def __init__(self, width: int, height: int, heuristic: str = "bssf"):
        self.width = width
        self.height = height
        self.heuristic = heuristic
        self.free: List[Rect] = [(0, 0, width, height)]

    def insert(self, w: int, h: int, rotate: bool) -> Optional[Tuple[int, int, int, int, bool]]:
        best = None  # (score, x, y, w, h, rotated)
        orientations = [(w, h, False)]
        if rotate and w != h:
            orientations.append((h, w, True))

        for free in self.free:
            for rw, rh, rot in orientations:
                if rw <= free[2] and rh <= free[3]:
                    score = _fit_score(self.heuristic, free, rw, rh)
                    if best is None or score < best[0]:
                        best = (score, free[0], free[1], rw, rh, rot)

        if best is None:
            return None

        _, x, y, rw, rh, rot = best
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
                rotate: bool, heuristic: str) -> List[List[Placement]]:
    """Pack items (idx, w, h) into bins in the given order; new bin on overflow."""
    remaining = list(order)
    bins: List[List[Placement]] = []
    while remaining:
        packer = _MaxRects(bin_w, bin_h, heuristic)
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


def _min_pages(items: List[Tuple[int, int, int]], bin_w: int, bin_h: int) -> int:
    """Lower bound on the page count: total item area over page area."""
    return math.ceil(sum(w * h for _, w, h in items) / (bin_w * bin_h))


def _pack(items: List[Tuple[int, int, int]], bin_w: int, bin_h: int,
          s: Settings) -> List[List[Placement]]:
    """Try several rules, orderings and orientations; keep the fewest pages."""
    sort_keys = (
        lambda it: -(it[1] * it[2]),       # largest area first
        lambda it: -max(it[1], it[2]),     # longest side first
        lambda it: -it[2],                 # tallest first
        lambda it: -it[1],                 # widest first
    )
    # (items, may rotate). Pre-rotating every item to portrait or landscape
    # finds uniform grids that the greedy rules miss on their own.
    variants = [(items, s.allow_rotate)]
    if s.allow_rotate:
        portrait = [(i, min(w, h), max(w, h)) for i, w, h in items]
        landscape = [(i, max(w, h), min(w, h)) for i, w, h in items]
        variants += [(portrait, False), (landscape, False),
                     (portrait, True), (landscape, True)]
    natural = {i: (w, h) for i, w, h in items}
    lower_bound = _min_pages(items, bin_w, bin_h)

    best_bins: Optional[List[List[Placement]]] = None
    best_score: Optional[Tuple[int, int]] = None
    for heuristic in HEURISTICS:
        for variant, rotate in variants:
            for key in sort_keys:
                bins = _pack_order(sorted(variant, key=key), bin_w, bin_h,
                                   rotate, heuristic)
                if not bins:
                    continue
                last_fill = sum(p[3] * p[4] for p in bins[-1])
                score = (len(bins), last_fill)  # fewest pages, then emptiest last page
                if best_score is None or score < best_score:
                    best_score, best_bins = score, bins
                    if len(bins) <= lower_bound:
                        break  # cannot use fewer pages
            if best_score and best_score[0] <= lower_bound:
                break
        if best_score and best_score[0] <= lower_bound:
            break

    # Rotation flags must be relative to each image's own orientation, not to
    # the pre-rotated variant it was packed as.
    return [[(idx, x, y, w, h, (w, h) != natural[idx]) for idx, x, y, w, h, _ in page]
            for page in (best_bins or [])]


# Final placement after grow-to-fill: (idx, x, y, draw_w, draw_h, rotated)
# draw_w/draw_h are the unrotated draw size; rotation is applied at render time.
FinalPlacement = Tuple[int, int, int, int, int, bool]


def _grow_to_fill(bins: List[List[Placement]], raw: List[Tuple[int, int]],
                  scales: List[float], usable_w: int, usable_h: int,
                  enabled: bool) -> List[List[FinalPlacement]]:
    """Scale each page's content up uniformly until it touches an edge.

    The growth is capped so no image exceeds the maximum box (an image at box
    scale f can grow at most 1/f). Aspect ratios and the layout are kept, so
    growth stops at the first edge reached; the other axis may keep slack.
    """
    pages: List[List[FinalPlacement]] = []
    for page in bins:
        bbox_w = max(x + pw for _, x, _y, pw, _ph, _ in page)
        bbox_h = max(y + ph for _, _x, y, _pw, ph, _ in page)
        cap = min(1.0 / scales[idx] for idx, *_rest in page) if enabled else 1.0
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


def _render(pages: List[List[FinalPlacement]], sources: List[bytes],
            s: Settings, margin: int) -> List[Image.Image]:
    """Draw each packed page onto a white A4 canvas, decoding each source once."""
    page_w, page_h, *_ = _geometry(s)
    out: List[Image.Image] = []
    for page in pages:
        canvas = Image.new("RGB", (page_w, page_h), s.background)
        for idx, x, y, w, h, rot in page:
            with load_image(sources[idx]) as src:
                im = src.resize((w, h), Image.LANCZOS)
            if rot:
                im = im.transpose(Image.Transpose.ROTATE_90)
            canvas.paste(im, (margin + x, margin + y))
        out.append(canvas)
    return out


def _image_scale(w: int, h: int, s: Settings, f: float) -> float:
    """Box scale for one image: f, raised so that its short side does not drop
    below MIN_SHORT_CM, but never above the max box.

    Shrinking the box alone is not enough: a long, narrow photo (16:9, 2:1) is
    limited by its long side, so a 12 cm long side leaves a short side well
    under MIN_SHORT_CM (12 x 6.75 cm for 16:9). The long side needs no such
    rule: the box already keeps it at MIN_LONG_CM or more unless the photo is
    squarer than the box, and forcing it there would make square photos huge.
    """
    tw, th = _target_dims(w, h, s, 1.0)
    need = cm_to_px(s.min_short_cm, s.dpi) / min(tw, th)
    return min(1.0, max(f, need))


def _sizes_at(dims, s: Settings, f: float, usable_w: int, usable_h: int):
    """Per-image (w, h) in px at box-scale f, clamped to the usable page area,
    and the box scale each image actually got."""
    raw: List[Tuple[int, int]] = []
    scales: List[float] = []
    for w, h in dims:
        g = _image_scale(w, h, s, f)
        tw, th = _target_dims(w, h, s, g)
        fit = min(usable_w / tw, usable_h / th, 1.0)
        if fit < 1.0:
            tw, th = max(1, math.floor(tw * fit)), max(1, math.floor(th * fit))
        raw.append((tw, th))
        scales.append(g)
    return raw, scales


@dataclass
class Plan:
    """Chosen scale and packing for a batch (dimensions only, no pixels)."""

    dims: List[Tuple[int, int]]    # displayed size of each source image
    f: float                       # box scale factor used
    raw: List[Tuple[int, int]]     # packed size of each image at scale f
    scales: List[float]            # box scale each image got (>= f, see _image_scale)
    bins: List[List[Placement]]    # placements per page


def _plan(dims: List[Tuple[int, int]], s: Settings):
    """Choose the best scale and packing. Returns (f, raw_sizes, scales, bins).

    Tries box scales from the max box down to the min box and keeps the
    arrangement with the fewest pages (least whitespace); among ties it keeps
    the largest scale, so images stay as big as allowed. Only dimensions are
    used during the search; pixels are resized once, when rendering.
    """
    _, _, _, gap, usable_w, usable_h = _geometry(s)
    bin_w, bin_h = usable_w + gap, usable_h + gap
    scales = _scale_candidates(s)

    # No scale can beat the area bound of the smallest box: stop once reached.
    smallest, _ = _sizes_at(dims, s, scales[-1], usable_w, usable_h)
    floor_pages = _min_pages([(i, w + gap, h + gap) for i, (w, h) in enumerate(smallest)],
                             bin_w, bin_h)

    best = None  # (score, f, raw_sizes, scales, bins)
    for f in scales:
        raw, img_scales = _sizes_at(dims, s, f, usable_w, usable_h)
        items = [(i, w + gap, h + gap) for i, (w, h) in enumerate(raw)]
        bins = _pack(items, bin_w, bin_h, s)
        score = (len(bins), -f)  # fewest pages, then largest images
        if best is None or score < best[0]:
            best = (score, f, raw, img_scales, bins)
        if len(bins) <= floor_pages:
            break

    _, f, raw, img_scales, bins = best
    return f, raw, img_scales, bins


def plan_pages(images: List[bytes], s: Settings) -> Plan:
    """Pack encoded images onto A4 pages, reading only their headers."""
    dims = [image_size(data) for data in images]
    f, raw, scales, bins = _plan(dims, s)
    return Plan(dims, f, raw, scales, bins)


def free_slots(plan: Plan, s: Settings, limit: int = 10) -> int:
    """How many more images, shaped like the smallest one on the last page,
    would still fit on it (up to limit), letting images shrink toward the min
    box as a real batch would. 0 means the last page is full."""
    if not plan.bins:
        return 0
    dims = [plan.dims[idx] for idx, *_rest in plan.bins[-1]]
    smallest = min(dims, key=lambda d: d[0] * d[1])
    for extra in range(1, limit + 1):
        *_unused, bins = _plan(dims + [smallest] * extra, s)
        if len(bins) > 1:
            return extra - 1
    return limit


def compose_pages(images: List[bytes], s: Settings,
                  plan: Optional[Plan] = None) -> Composition:
    """Compose encoded images onto A4 pages (pack, grow-to-fill, render).

    With a plan, only its pages are rendered (images it leaves out are
    skipped).
    """
    _, _, margin, _, usable_w, usable_h = _geometry(s)
    if plan is None:
        plan = plan_pages(images, s)
    dims = plan.dims
    pages = _grow_to_fill(plan.bins, plan.raw, plan.scales, usable_w, usable_h, s.fill_page)

    print_dpi = []
    for page in pages:
        for idx, _x, _y, w, _h, _rot in page:
            print_dpi.append((idx, round(s.dpi * dims[idx][0] / w)))
    print_dpi.sort()

    return Composition(_render(pages, images, s, margin), print_dpi)
