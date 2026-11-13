"""Render a captured terminal session as an animated GIF.

    pip install pillow
    python -m tools.walkthrough > /tmp/walk.txt
    python tools/record.py /tmp/walk.txt docs/walkthrough.gif

Not in requirements-dev.txt: this draws a picture, it does not test anything,
and nothing in CI needs Pillow.

The transcript is real output from a real run -- the only thing synthesised is
the typing, which arrives one character at a time because a wall of text
appearing at once is unreadable. ANSI colours in the transcript are honoured,
so the GIF shows what the terminal showed.
"""

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SIZE = 15
PAD = 18
BG = (13, 17, 23)            # the dark grey GitHub renders against
FG = (201, 209, 217)
PROMPT = (126, 231, 135)
COLOURS = {"31": (255, 123, 114), "32": (126, 231, 135), "33": (233, 196, 106),
           "36": (121, 192, 255), "2": (110, 118, 129)}
ANSI = re.compile(r"\033\[([0-9;]*)m")
COMMAND = "python -m tools.walkthrough"


def spans(text):
    """Split an ANSI line into (text, colour, bold) runs."""
    out, colour, bold, at = [], FG, False, 0
    for match in ANSI.finditer(text):
        if match.start() > at:
            out.append((text[at:match.start()], colour, bold))
        for code in (match.group(1) or "0").split(";"):
            if code in ("", "0"):
                colour, bold = FG, False
            elif code == "1":
                bold = True
            elif code in COLOURS:
                colour = COLOURS[code]
        at = match.end()
    if at < len(text):
        out.append((text[at:], colour, bold))
    return out


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip())
        return 2
    transcript, target = Path(argv[1]), Path(argv[2])
    lines = [spans(line.rstrip("\n")) for line in transcript.read_text().splitlines()]

    font = ImageFont.truetype(FONT, SIZE)
    bold = ImageFont.truetype(BOLD_FONT, SIZE)
    cw = font.getlength("M")
    lh = SIZE + 5

    width = int(PAD * 2 + cw * (2 + max(
        [len(COMMAND) + 2] + [sum(len(t) for t, _, _ in line) for line in lines])))
    height = PAD * 2 + lh * (len(lines) + 2)

    def frame(shown, typed):
        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)
        draw.text((PAD, PAD), "$ ", font=bold, fill=PROMPT)
        draw.text((PAD + cw * 2, PAD), COMMAND[:typed], font=font, fill=FG)
        if typed < len(COMMAND):
            draw.rectangle([PAD + cw * (2 + typed), PAD + 2,
                            PAD + cw * (3 + typed), PAD + SIZE + 2], fill=FG)
        for row, line in enumerate(lines[:shown], start=1):
            x = PAD
            for text, colour, is_bold in line:
                face = bold if is_bold else font
                draw.text((x, PAD + lh * row), text, font=face, fill=colour)
                x += cw * len(text)
        return image

    plain = ["".join(t for t, _, _ in line) for line in lines]
    frames, delays = [], []
    for typed in range(0, len(COMMAND) + 1, 2):          # type the command
        frames.append(frame(0, typed))
        delays.append(45)
    delays[-1] = 600                                      # a beat before it runs
    for shown in range(1, len(lines) + 1):
        frames.append(frame(shown, len(COMMAND)))
        text = plain[shown - 1]
        if not text.strip():
            delays.append(120)                            # the gap between beats
        elif text.lstrip().startswith("["):
            delays.append(700)                            # a heading: let it land
        else:
            delays.append(380)                            # a line of output, read at speed
    delays[-1] = 5000                                     # hold the final frame

    target.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=delays,
                   loop=0, optimize=True)
    size_kb = target.stat().st_size // 1024
    print(f"{target}: {len(frames)} frames, {width}x{height}, {size_kb} KB, "
          f"{sum(delays) / 1000:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
