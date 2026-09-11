"""Arabic-safe text rendering.

ffmpeg's drawtext draws glyphs in code-point order with no shaping and no bidi,
which turns Arabic into disconnected, backwards letters. Correct output needs a
shaping engine (HarfBuzz) and a bidi pass (FriBidi). Rather than depend on one
specific stack being present, this module renders the caption to a transparent
PNG through whichever backend the box actually has, and the PNG is composited
by ffmpeg -- which it does correctly, because by then it is just pixels.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "overlays"


def available_backend() -> str | None:
    """Which renderer this machine can use, best first."""
    if shutil.which("pango-view"):
        return "pango-view"
    if shutil.which("magick") or shutil.which("convert"):
        return "imagemagick"
    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return "pillow"
    except ImportError:
        return None


def _escape_markup(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def font_family(font_path: str | Path) -> str:
    """The family name Pango knows a .ttf file by.

    Pango and ImageMagick select fonts by *family* ("Amiri"), not by filename
    ("Amiri-Regular.ttf"), and silently fall back to a default when handed a
    name that matches nothing -- so a wrong family here looks like the font
    setting simply being ignored. fc-query reads the real family out of the
    file; the stem is only a last resort.
    """
    path = Path(font_path)
    if path.is_file() and shutil.which("fc-query"):
        result = subprocess.run(
            ["fc-query", "--format=%{family}", str(path)],
            capture_output=True, text=True,
        )
        raw = result.stdout.strip()
        if raw:
            # A variable font reports one entry per named instance, so the same
            # family can come back repeated ("Reem KufiReem Kufi..."). Take the
            # first comma-separated entry, then collapse a whole-string repeat.
            family = raw.split(",")[0].strip()
            for size in range(1, len(family) // 2 + 1):
                unit = family[:size]
                if unit * (len(family) // size) == family and len(family) % size == 0:
                    return unit
            return family
    return path.stem


def _render_pango_view(text, font, size, color, out: Path) -> bool:
    # pango-view drives HarfBuzz + FriBidi directly, so shaping and RTL ordering
    # are handled by the same stack GTK apps use.
    cmd = [
        "pango-view", "--no-display", "-q",
        f"--font={font}",
        f"--foreground={color}",
        "--background=transparent",
        "--markup",
        "-o", str(out),
        "-t", f"<span size='{int(size) * 1024}'>{_escape_markup(text)}</span>",
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _render_imagemagick(text, font, size, color, out: Path) -> bool:
    binary = shutil.which("magick") or shutil.which("convert")
    # The pango: pseudo-format routes the string through Pango, which is what
    # makes this correct for Arabic; plain -annotate would not be.
    markup = f"<span font='{font} {int(size)}' foreground='{color}'>{_escape_markup(text)}</span>"
    cmd = [binary, "-background", "none", f"pango:{markup}", str(out)]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _render_pillow(text, font_path, size, color, out: Path) -> bool:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False

    # Reshape (contextual letter forms) then reorder (bidi) before drawing --
    # doing only one of the two still renders wrong.
    shaped = get_display(arabic_reshaper.reshape(text))
    font = ImageFont.truetype(str(font_path), int(size))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = probe.textbbox((0, 0), shaped, font=font)
    pad = int(size * 0.4)
    img = Image.new("RGBA", (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), shaped, font=font, fill=color)
    img.save(out)
    return True


def render(text: str, font: str | Path, size: int, color: str, out_path: Path) -> Path | None:
    """Render `text` to a transparent PNG. Returns the path, or None on failure."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    backend = available_backend()
    if backend is None:
        return None

    # Pillow loads the file directly; the others select by family name.
    font_arg = font if backend == "pillow" else font_family(font)
    ok = False
    if backend == "pango-view":
        ok = _render_pango_view(text, font_arg, size, color, out_path)
    elif backend == "imagemagick":
        ok = _render_imagemagick(text, font_arg, size, color, out_path)
    elif backend == "pillow":
        ok = _render_pillow(text, font, size, color, out_path)

    return out_path if ok and out_path.exists() else None
