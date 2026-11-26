"""Export launcher, window and tray icons from one approved RGBA master."""

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def export(master):
    image = Image.open(master).convert("RGBA")
    # Ignore near-transparent generation speckles when measuring optical bounds.
    bounds = image.getchannel("A").point(lambda alpha: 255 if alpha >= 128 else 0).getbbox()
    if bounds is None:
        raise ValueError("Icon master is empty")
    left, top, right, bottom = bounds
    bleed = 3
    image = image.crop((left - bleed, top - bleed, right + bleed, bottom + bleed))
    def render(content_size):
        scale = content_size / max(image.size)
        resized = image.resize(
            tuple(round(edge * scale) for edge in image.size), Image.Resampling.LANCZOS
        )
        canvas = Image.new("RGBA", (512, 512))
        canvas.alpha_composite(resized, ((512 - resized.width) // 2, (512 - resized.height) // 2))
        return canvas

    render(450).save(ROOT / "assets/geodomdesk.png")
    render(500).save(
        ROOT / "assets/geodomdesk.ico",
        sizes=[(size, size) for size in [16, 20, 24, 32, 40, 48, 64, 128, 256]],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path)
    export(parser.parse_args().master)
