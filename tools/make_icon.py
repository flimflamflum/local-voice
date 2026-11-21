"""Generates ui/jarvis.ico - a glowing orb matching the app's idle colours."""
import struct
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "ui" / "jarvis.ico"


def render(size):
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2
    d = np.hypot(x - c, y - c) / (size / 2)  # 0 centre .. 1 edge
    core = np.array([79, 209, 255], np.float32)
    edge = np.array([124, 92, 255], np.float32)
    t = np.clip(d / 0.72, 0, 1)[..., None]
    rgb = core * (1 - t) + edge * t
    # glossy highlight up-left
    hl = np.exp(-(((x - c * 0.7) ** 2 + (y - c * 0.62) ** 2) / (2 * (size * 0.13) ** 2)))[..., None]
    rgb = rgb * (1 - hl * 0.8) + 255 * hl * 0.8
    sphere = np.clip((0.72 - d) / 0.03, 0, 1)            # solid orb with soft edge
    glow = np.clip(1 - (d - 0.72) / 0.28, 0, 1) ** 2 * 0.55  # halo outside it
    alpha = np.maximum(sphere, np.where(d > 0.72, glow, 0))
    return np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)


def png_bytes(img):
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[row].tobytes() for row in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    sizes = [16, 32, 48, 256]
    pngs = [png_bytes(render(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, pngs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    OUT.write_bytes(header + entries + b"".join(pngs))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
