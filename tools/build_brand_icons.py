"""Pack the pre-rendered fluentpill PNGs into multi-size .ico files.

The 16 px entry comes from the pixel-grid "small" master, the rest from the
full symbol. PNGs are rendered with the logo-design engine (render_png.py);
this script only packs them, so it has no dependency beyond the stdlib.

Usage: python tools/build_brand_icons.py
"""
import struct
from pathlib import Path

BRAND = Path(__file__).resolve().parent.parent / "assets" / "brand"
SIZES = (16, 24, 32, 48, 256)


def pack_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> None:
    for variant, out in (("cyan", "fluentpill.ico"), ("teal", "fluentpill-teal.ico")):
        pngs = [(n, (BRAND / "_r" / f"{variant}-{n}.png").read_bytes()) for n in SIZES]
        (BRAND / out).write_bytes(pack_ico(pngs))
        print("wrote", BRAND / out)


if __name__ == "__main__":
    main()
