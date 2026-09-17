#!/usr/bin/env python3
"""Build a labeled contact sheet of QA renders."""
import os
import subprocess
import sys

import struct

def png_size(path):
    with open(path, 'rb') as f:
        f.read(8)  # PNG sig
        f.read(4)  # IHDR length
        f.read(4)  # IHDR type
        w, h = struct.unpack('>II', f.read(8))
    return w, h

def read_pgm(path):
    with open(path, 'rb') as f:
        data = f.read()
    # Parse PGM header
    parts = data.split(b'\n', 3)
    w, h = map(int, parts[1].split())
    maxv = int(parts[2])
    body = parts[3]
    return w, h, maxv, body

# Discover renders
files = sorted([f for f in os.listdir('/tmp') if f.startswith('qa_') and f.endswith('.pgm')])
print('renders:', files)

# Build contact sheet with Pillow
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print('no PIL, install:')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'Pillow'])
    from PIL import Image, ImageDraw, ImageFont

THUMB_W, THUMB_H = 240, 135   # 16:9 ratio at 240px
COLS = 5
ROWS = (len(files) + COLS - 1) // COLS
PAD = 6
LABEL_H = 18
W = COLS * (THUMB_W + PAD) + PAD
H = ROWS * (THUMB_H + LABEL_H + PAD) + PAD
sheet = Image.new('RGB', (W, H), (0, 0, 0))
draw = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 11)
except Exception:
    font = ImageFont.load_default()

for i, name in enumerate(files):
    col = i % COLS
    row = i // COLS
    x = PAD + col * (THUMB_W + PAD)
    y = PAD + row * (THUMB_H + LABEL_H + PAD)
    src = '/tmp/' + name
    img = Image.open(src).convert('L')
    img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
    iw, ih = img.size
    # Center inside the cell
    sheet.paste(img.convert('RGB'), (x + (THUMB_W - iw)//2, y + (THUMB_H - ih)//2))
    # Label
    label = name.replace('qa_', '').replace('.pgm', '').replace('_', ' ')
    draw.text((x + 4, y + THUMB_H + 2), label, fill=(255, 255, 255), font=font)

out = '/tmp/qa_sheet.png'
sheet.save(out, optimize=True)
print('saved', out, sheet.size)