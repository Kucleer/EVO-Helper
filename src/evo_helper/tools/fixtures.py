"""Synthetic vision fixture generator (pure Python, no image frameworks)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    payload = chunk_type + data
    return (
        struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    """Write an RGB PNG from raw pixel rows."""
    raw = b"".join(b"\x00" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        header + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")
    )


def draw_rect(
    pixels: bytearray, width: int, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]
) -> None:
    for row in range(y, y + h):
        for col in range(x, x + w):
            if 0 <= row < 512 and 0 <= col < width:
                offset = (row * width + col) * 3
                pixels[offset : offset + 3] = bytes(rgb)


def mail_list_fixture(path: Path, *, items: int = 3, page: str = "2") -> None:
    """A synthetic mail list screenshot (512x512) with simple bars."""
    width, height = 512, 512
    pixels = bytearray(width * height * 3)
    for y in range(height):
        base = y * width * 3
        for x in range(width):
            pixels[base + x * 3 : base + x * 3 + 3] = bytes((238, 240, 244))
    for index in range(items):
        draw_rect(pixels, width, 24, 48 + index * 96, 240, 18, (180, 200, 230))
        draw_rect(pixels, width, 24, 70 + index * 96, 120, 12, (120, 140, 170))
    draw_rect(pixels, width, 24, 460, 90, 16, (220, 220, 220))
    write_png(path, width, height, bytes(pixels))


def battle_detail_fixture(path: Path) -> None:
    width, height = 512, 512
    pixels = bytearray(width * height * 3)
    for y in range(height):
        base = y * width * 3
        for x in range(width):
            pixels[base + x * 3 : base + x * 3 + 3] = bytes((250, 250, 250))
    draw_rect(pixels, width, 24, 60, 220, 20, (190, 210, 240))
    draw_rect(pixels, width, 24, 130, 220, 20, (190, 210, 240))
    draw_rect(pixels, width, 24, 220, 200, 16, (200, 200, 200))
    draw_rect(pixels, width, 24, 300, 200, 16, (200, 200, 200))
    write_png(path, width, height, bytes(pixels))
