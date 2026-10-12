"""Write merlin/merlin.png from merlin/merlin.ico (its 256 x 256 picture).

Merlin's icons are Dan's own artwork, kept as he made them: merlin.ico (for
Windows, the window, the taskbar and Merlin.exe) and merlin.svg (Linux's
desktop icon). merlin.png, which Linux installs and the installer windows
show, is taken from the .ico, so the three always agree. Run this after
putting a new merlin.ico in place:

    python3 tools/png-from-ico.py

(Until 1.8.26 Merlin's logo was drawn by a script, tools/make-logo.py; it is
gone, so nothing can draw over these.)
"""
import os
import struct
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "merlin")


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication, QImage

    app = QGuiApplication(sys.argv[:1])                  # noqa: F841
    ico = os.path.join(HERE, "merlin.ico")
    data = open(ico, "rb").read()
    count = struct.unpack("<HHH", data[:6])[2]
    best = None
    for i in range(count):
        width, height = data[6 + 16 * i], data[7 + 16 * i]
        size = width or 256
        if best is None or size > best:
            best = size
    image = QImage(ico)                                  # Qt picks the largest
    sizes = []
    from PyQt6.QtGui import QImageReader

    reader = QImageReader(ico)
    for n in range(reader.imageCount()):
        reader.jumpToImage(n)
        frame = reader.read()
        sizes.append(frame.width())
        if frame.width() == best:
            image = frame
    if image.isNull():
        print("Qt could not read merlin.ico")
        return 1
    image.save(os.path.join(HERE, "merlin.png"), "PNG")
    print(f"merlin.png written, {image.width()} x {image.height()} (the .ico holds {sorted(sizes)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
