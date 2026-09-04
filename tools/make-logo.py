import os, struct, math

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "merlin") + os.sep
os.environ["QT_QPA_PLATFORM"]="offscreen"
from PyQt6.QtGui import (QImage, QPainter, QColor, QPainterPath, QBrush, QPen,
                         QLinearGradient, QRadialGradient)
from PyQt6.QtCore import Qt, QPointF, QBuffer, QIODevice, QRectF

BG_A, BG_B = "#2a2350", "#12101f"
RING   = "#7b5cf0"
HAT_HI, HAT_LO = "#9273f5", "#4b32ab"
BRIM   = "#3a2694"
BEARD_HI, BEARD_LO = "#ffffff", "#cfd0e6"
SHADOW = "#1b1636"
GOLD   = "#f0c04a"
EYE    = "#7de3ff"

def mirror(path, axis):
    """Reflect a half-shape so the face is exactly symmetrical, which is what
    gives the Brave mark its balance.

    The axis must be in the SAME space the path was built in. The paths here
    are already multiplied by the scale factor, so passing the unit-space 32
    mirrors about the wrong line at every size except 64px.
    """
    from PyQt6.QtGui import QTransform

    t = QTransform().translate(axis, 0).scale(-1, 1).translate(-axis, 0)
    return t.map(path)

# The artwork was drawn to fit inside a 30-unit-radius disc. With the disc gone
# it fills the square instead.
#
# Measured, not guessed: rendered unscaled the wizard occupies y 3.6..58.3 in
# 64-unit space, so it is 54.7 tall and its centre is at 30.95, a little above
# the canvas centre. It is shifted down by that difference first, then scaled
# about the middle of the canvas, which keeps the top and bottom margins equal.
# 30/27.35 would touch both edges exactly; the rest is clearance.
ART_SHIFT_Y = 1.05
# Measured, not guessed: 1.15 leaves 2px clear top and bottom at 256, which is
# as large as the wizard goes without the hat's point or the beard touching the
# edge. Anything above this clips, which is why the number is not rounder.
FILL_SCALE = 1.15


def render(size, compact=False):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    S = size/64.0
    def P(x, y): return QPointF(x*S, y*S)
    def R(x, y, w, h): return QRectF(x*S, y*S, w*S, h*S)

    # No background disc and no ring. The wizard is the mark, so it can use
    # the whole canvas instead of being inset inside a circle.
    #
    # Everything below is drawn in the original 64-unit coordinates, which were
    # laid out to sit inside a 30-unit-radius circle. One transform around the
    # face's centre scales that artwork up to fill the square.
    # The default pen is a black hairline. The removed background block used to
    # clear it, so without this every shape gained a black outline, which at 16
    # pixels was most of the icon.
    p.setPen(Qt.PenStyle.NoPen)

    grow = FILL_SCALE * (1.06 if compact else 1.0)
    p.save()
    p.translate(32*S, 32*S)
    p.scale(grow, grow)
    p.translate(-32*S, -32*S)
    p.translate(0, ART_SHIFT_Y*S)

    # ---- face: a shadow for the eyes to sit in ----
    #
    # Skipped entirely at 16 and 24 pixels. It used to sit against the
    # background disc; with the disc gone it had nothing to contrast against
    # and filled most of a small icon, leaving a black blob with a white chin.
    # The hat and beard are what carry the shape at that size, and the eyes
    # read perfectly well against the beard.
    if not compact:
        face = QPainterPath()
        face.addEllipse(R(21.5, 22.0, 21.0, 20.0))
        p.setBrush(QBrush(QColor(SHADOW))); p.drawPath(face)

    # ---- beard: one broad symmetrical mass, pointed at the chin ----
    beard = QPainterPath()
    beard.setFillRule(Qt.FillRule.WindingFill)
    beard.moveTo(P(17.0, 30.0))
    beard.cubicTo(P(14.6, 41.0), P(18.2, 52.0), P(24.2, 56.2))   # left outline
    beard.cubicTo(P(27.8, 58.8), P(30.0, 58.2), P(32.0, 58.0))   # chin, left half
    beard.cubicTo(P(34.0, 58.2), P(36.2, 58.8), P(40.0, 56.4))
    beard.cubicTo(P(45.8, 52.0), P(49.4, 41.0), P(47.0, 30.0))   # right outline
    if compact:
        # No mouth gap at 16 and 24 pixels. The gap is thinner than a pixel
        # there, so all it did was expose the dark face behind and leave the
        # icon reading as a black blob with a white chin. Solid, the beard is
        # what you actually recognise at that size.
        beard.lineTo(P(17.0, 30.0))
    else:
        beard.cubicTo(P(45.0, 36.0), P(39.0, 42.4), P(32.0, 42.4))  # mouth gap
        beard.cubicTo(P(25.0, 42.4), P(19.0, 36.0), P(17.0, 30.0))
    beard.closeSubpath()
    bg = QLinearGradient(P(20, 30), P(40, 58))
    bg.setColorAt(0.0, QColor(BEARD_HI)); bg.setColorAt(1.0, QColor(BEARD_LO))
    p.setBrush(QBrush(bg)); p.drawPath(beard)

    # beard parting, drawn as two strokes rather than a filled gap
    if not compact:
        pen = QPen(QColor(120, 122, 155, 70)); pen.setWidthF(1.1*S)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        for dx in (-6.0, 6.0):
            strand = QPainterPath()
            strand.moveTo(P(32 + dx*0.85, 45))
            strand.cubicTo(P(32 + dx, 48.5), P(32 + dx, 51), P(32 + dx*0.85, 53))
            p.drawPath(strand)
        p.setPen(Qt.PenStyle.NoPen)

    # ---- moustache over the face bottom ----
    # fuller, with the tip sweeping out and flicking up, so it reads as hair
    # rather than as a drawn-on mouth line
    half = QPainterPath()
    half.moveTo(P(32.0, 34.4))
    half.cubicTo(P(27.4, 34.0), P(22.4, 35.2), P(19.8, 39.2))   # upper edge
    half.cubicTo(P(18.9, 40.8), P(19.6, 42.0), P(21.0, 41.6))   # upturned tip
    half.cubicTo(P(22.6, 41.0), P(23.4, 40.0), P(24.6, 39.4))   # underside of tip
    half.cubicTo(P(27.0, 38.2), P(29.6, 38.4), P(32.0, 38.6))   # back to centre
    half.closeSubpath()
    mous = QPainterPath(half)
    mous.addPath(mirror(half, 32*S))
    mous.setFillRule(Qt.FillRule.WindingFill)
    p.save()
    p.translate(0, 1.1*S)
    p.setBrush(QBrush(QColor(90, 92, 125, 60)))
    p.drawPath(mous)
    p.restore()
    p.setBrush(QBrush(QColor(BEARD_HI))); p.drawPath(mous)

    # ---- brows ----
    brow = QPainterPath()
    brow.moveTo(P(21.6, 28.2))
    brow.cubicTo(P(23.4, 25.0), P(27.6, 24.4), P(30.4, 26.2))
    brow.cubicTo(P(28.0, 25.8), P(24.6, 26.6), P(22.8, 29.4))
    brow.closeSubpath()
    brows = QPainterPath(brow)
    brows.addPath(mirror(brow, 32*S))
    brows.setFillRule(Qt.FillRule.WindingFill)
    p.setBrush(QBrush(QColor(BEARD_HI))); p.drawPath(brows)

    # ---- eyes ----
    if not compact:
        p.setBrush(QBrush(QColor(EYE)))
        for ex in (26.6, 37.4):
            p.drawEllipse(QPointF(ex*S, 29.6*S), 2.05*S, 2.05*S)
        p.setBrush(QBrush(QColor(255, 255, 255, 210)))
        for ex in (26.6, 37.4):
            p.drawEllipse(QPointF((ex-0.55)*S, 29.0*S), 0.72*S, 0.72*S)
    else:
        p.setBrush(QBrush(QColor(EYE)))
        for ex in (26.4, 37.6):
            p.drawEllipse(QPointF(ex*S, 29.6*S), 2.5*S, 2.5*S)

    # ---- hat: brim then cone, sharing one gradient so there is no seam ----
    p.setBrush(QBrush(QColor(BRIM)))
    brim = QPainterPath(); brim.addEllipse(R(11.0, 19.0, 42.0, 9.4))
    p.drawPath(brim)

    cone = QPainterPath()
    cone.moveTo(P(15.2, 23.6))
    cone.cubicTo(P(18.4, 15.0), P(23.4, 7.6), P(30.2, 4.4))    # left edge
    cone.cubicTo(P(35.0, 2.4), P(40.2, 4.6), P(38.8, 8.6))     # curl over
    cone.cubicTo(P(38.0, 11.0), P(35.6, 12.2), P(33.8, 12.8))  # curl under
    cone.cubicTo(P(39.4, 15.6), P(45.2, 19.0), P(48.8, 23.6))  # right edge
    cone.cubicTo(P(43.0, 26.4), P(21.0, 26.4), P(15.2, 23.6))  # base, on the brim
    cone.closeSubpath()
    hg = QLinearGradient(P(14, 4), P(50, 26))
    hg.setColorAt(0.0, QColor(HAT_HI)); hg.setColorAt(1.0, QColor(HAT_LO))
    p.setBrush(QBrush(hg)); p.drawPath(cone)

    # hat band, clipped to the cone so it cannot overhang
    p.save(); p.setClipPath(cone)
    p.setBrush(QBrush(QColor(GOLD)))
    band = QPainterPath(); band.addEllipse(R(11.0, 17.4, 42.0, 9.4))
    p.drawPath(band)
    p.restore()

    # star on the band
    star = QPainterPath()
    for i in range(10):
        rad = (3.0 if i % 2 == 0 else 3.0*0.382)*S
        a = -math.pi/2 + i*math.pi/5
        pt = QPointF(32*S + rad*math.cos(a), 21.6*S + rad*math.sin(a))
        star.moveTo(pt) if i == 0 else star.lineTo(pt)
    star.closeSubpath()
    p.setBrush(QBrush(QColor("#fff3cf"))); p.drawPath(star)

    p.restore()
    p.end()
    return img

def png_bytes(img):
    buf = QBuffer(); buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG"); return bytes(buf.data())


def dib_bytes(img):
    """A 32-bit BGRA icon image in DIB form: header, XOR pixels, AND mask.

    Small icon entries have to be DIB rather than PNG. Windows only guarantees
    PNG-compressed frames for the large sizes, and LoadImage rejects PNG at
    16x16, at which point the shell abandons the file and falls back to the
    executable's own icon.

    The bitmap height is doubled in the header to cover both the colour data
    and the mask, and the rows run bottom-up, which is how the format has
    always worked.
    """
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()

    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        w, h * 2,    # biWidth, biHeight (colour + mask)
        1, 32,       # biPlanes, biBitCount
        0,           # biCompression = BI_RGB
        w * h * 4,   # biSizeImage
        0, 0, 0, 0,
    )

    xor = bytearray()
    for y in range(h - 1, -1, -1):                       # bottom-up
        for x in range(w):
            c = img.pixelColor(x, y)
            xor += bytes((c.blue(), c.green(), c.red(), c.alpha()))

    # 1bpp AND mask, rows padded to 4 bytes. Transparent pixels set the bit.
    stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(stride)
        for x in range(w):
            if img.pixelColor(x, y).alpha() == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row

    return bytes(header) + bytes(xor) + bytes(mask)

def build():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for sz in sizes:
        img = render(sz, compact=sz < 32)
        # DIB for the small sizes Windows is fussy about, PNG for the big ones
        # where it keeps the file from bloating
        payload = png_bytes(img) if sz >= 128 else dib_bytes(img)
        images.append((sz, payload))

    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16*len(images); entries = b""; blobs = b""
    for sz, data in images:
        w = 0 if sz >= 256 else sz
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        blobs += data; offset += len(data)
    open(ICON_DIR + "merlin.ico","wb").write(out + entries + blobs)
    render(256).save(ICON_DIR + "merlin.png", "PNG")
    for s in (16, 24, 32, 64):
        render(s, compact=s < 32).save(f"/tmp/pv{s}.png", "PNG")

# --------------------------------------------------------------- SVG export
def path_to_d(path):
    """Serialise a QPainterPath to SVG path data, so the SVG is generated from
    exactly the same geometry as the raster icons instead of being hand-copied
    and drifting out of sync."""
    from PyQt6.QtGui import QPainterPath as PP
    out = []
    i = 0
    n = path.elementCount()
    while i < n:
        e = path.elementAt(i)
        if e.type == PP.ElementType.MoveToElement:
            out.append(f"M{e.x:.2f} {e.y:.2f}")
            i += 1
        elif e.type == PP.ElementType.LineToElement:
            out.append(f"L{e.x:.2f} {e.y:.2f}")
            i += 1
        elif e.type == PP.ElementType.CurveToElement:
            c1 = path.elementAt(i); c2 = path.elementAt(i+1); ep = path.elementAt(i+2)
            out.append(f"C{c1.x:.2f} {c1.y:.2f} {c2.x:.2f} {c2.y:.2f} "
                       f"{ep.x:.2f} {ep.y:.2f}")
            i += 3
        else:
            i += 1
    return " ".join(out) + " Z"


def build_svg():
    S = 1.0
    def P(x, y): return QPointF(x, y)
    def R(x, y, w, h): return QRectF(x, y, w, h)

    beard = QPainterPath(); beard.setFillRule(Qt.FillRule.WindingFill)
    beard.moveTo(P(17.0, 30.0))
    beard.cubicTo(P(14.6, 41.0), P(18.2, 52.0), P(24.2, 56.2))
    beard.cubicTo(P(27.8, 58.8), P(30.0, 58.2), P(32.0, 58.0))
    beard.cubicTo(P(34.0, 58.2), P(36.2, 58.8), P(40.0, 56.4))
    beard.cubicTo(P(45.8, 52.0), P(49.4, 41.0), P(47.0, 30.0))
    beard.cubicTo(P(45.0, 36.0), P(39.0, 42.4), P(32.0, 42.4))
    beard.cubicTo(P(25.0, 42.4), P(19.0, 36.0), P(17.0, 30.0))
    beard.closeSubpath()

    half = QPainterPath()
    half.moveTo(P(32.0, 34.4))
    half.cubicTo(P(27.4, 34.0), P(22.4, 35.2), P(19.8, 39.2))
    half.cubicTo(P(18.9, 40.8), P(19.6, 42.0), P(21.0, 41.6))
    half.cubicTo(P(22.6, 41.0), P(23.4, 40.0), P(24.6, 39.4))
    half.cubicTo(P(27.0, 38.2), P(29.6, 38.4), P(32.0, 38.6))
    half.closeSubpath()
    mous = QPainterPath(half); mous.addPath(mirror(half, 32.0))
    mous.setFillRule(Qt.FillRule.WindingFill)

    brow = QPainterPath()
    brow.moveTo(P(21.6, 28.2))
    brow.cubicTo(P(23.4, 25.0), P(27.6, 24.4), P(30.4, 26.2))
    brow.cubicTo(P(28.0, 25.8), P(24.6, 26.6), P(22.8, 29.4))
    brow.closeSubpath()
    brows = QPainterPath(brow); brows.addPath(mirror(brow, 32.0))
    brows.setFillRule(Qt.FillRule.WindingFill)

    cone = QPainterPath()
    cone.moveTo(P(15.2, 23.6))
    cone.cubicTo(P(18.4, 15.0), P(23.4, 7.6), P(30.2, 4.4))
    cone.cubicTo(P(35.0, 2.4), P(40.2, 4.6), P(38.8, 8.6))
    cone.cubicTo(P(38.0, 11.0), P(35.6, 12.2), P(33.8, 12.8))
    cone.cubicTo(P(39.4, 15.6), P(45.2, 19.0), P(48.8, 23.6))
    cone.cubicTo(P(43.0, 26.4), P(21.0, 26.4), P(15.2, 23.6))
    cone.closeSubpath()

    band_full = QPainterPath(); band_full.addEllipse(R(11.0, 17.4, 42.0, 9.4))
    band = band_full.intersected(cone)          # baked, since Qt ignores clip-path

    star = QPainterPath()
    for i in range(10):
        rad = 3.0 if i % 2 == 0 else 3.0*0.382
        a = -math.pi/2 + i*math.pi/5
        pt = QPointF(32 + rad*math.cos(a), 21.6 + rad*math.sin(a))
        star.moveTo(pt) if i == 0 else star.lineTo(pt)
    star.closeSubpath()

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <!-- Generated from the same geometry as merlin.ico and merlin.png. The hat
       band is a baked intersection rather than a clip-path, because Qt renders
       SVG Tiny and silently ignores clip-path, which flattens the mark. -->
  <defs>
    <radialGradient id="sky" cx="24" cy="16" r="48" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{BG_A}"/><stop offset="1" stop-color="{BG_B}"/>
    </radialGradient>
    <linearGradient id="felt" x1="14" y1="4" x2="50" y2="26" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{HAT_HI}"/><stop offset="1" stop-color="{HAT_LO}"/>
    </linearGradient>
    <linearGradient id="hair" x1="20" y1="30" x2="40" y2="58" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{BEARD_HI}"/><stop offset="1" stop-color="{BEARD_LO}"/>
    </linearGradient>
  </defs>
  <g transform="translate(32 32) scale({FILL_SCALE}) translate(-32 -32) translate(0 {ART_SHIFT_Y})">
  <ellipse cx="32" cy="32" rx="10.5" ry="10" fill="{SHADOW}"/>
  <path d="{path_to_d(beard)}" fill="url(#hair)" fill-rule="nonzero"/>
  <path d="{path_to_d(brows)}" fill="{BEARD_HI}" fill-rule="nonzero"/>
  <circle cx="26.6" cy="29.6" r="2.05" fill="{EYE}"/>
  <circle cx="37.4" cy="29.6" r="2.05" fill="{EYE}"/>
  <circle cx="26.05" cy="29.0" r="0.72" fill="#ffffff" fill-opacity="0.82"/>
  <circle cx="36.85" cy="29.0" r="0.72" fill="#ffffff" fill-opacity="0.82"/>
  <path d="{path_to_d(mous)}" fill="{BEARD_HI}" fill-rule="nonzero"/>
  <ellipse cx="32" cy="23.7" rx="21" ry="4.7" fill="{BRIM}"/>
  <path d="{path_to_d(cone)}" fill="url(#felt)"/>
  <path d="{path_to_d(band)}" fill="{GOLD}"/>
  <path d="{path_to_d(star)}" fill="#fff3cf"/>
  </g>
</svg>
'''
    open(ICON_DIR + "merlin.svg", "w").write(svg)
    return svg


if __name__ == "__main__":
    # The entry point sits at the bottom so that everything it calls is already
    # defined. It used to be in the middle of the file, above build_svg, which
    # meant the SVG was simply never regenerated: merlin.svg had been stale for
    # a long time and had drifted away from the icons it is supposed to match.
    build()
    build_svg()
    print("built merlin.ico, merlin.png and merlin.svg")
