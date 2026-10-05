"""Transforms and animations for Merlin Engine: the arithmetic, kept apart.

A transform is a list of (function, arguments) as css.parse_transform_ops
gives them. matrix() turns one into a 4 by 4 matrix for a box; projected()
turns that, with a parent's perspective, into the 3 by 3 projective matrix
Qt draws with, so a card turned with rotateY() is foreshortened as in a
browser. sample() gives an animation's transform and opacity at a time.
"""
from __future__ import annotations

import math

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _translate(x, y, z=0.0):
    m = [row[:] for row in IDENTITY]
    m[0][3], m[1][3], m[2][3] = x, y, z
    return m


def _length(arg, size: float) -> float:
    """A length argument: ("px", n) or ("%", n) of size; or a plain number."""
    if isinstance(arg, tuple):
        if arg[0] == "mix":                       # calc() of a percentage and pixels
            return arg[1] * size / 100 + arg[2]
        return arg[1] * size / 100 if arg[0] == "%" else arg[1]
    return float(arg)


def function_matrix(name: str, args: list, width: float, height: float):
    """The 4 by 4 matrix of one transform function, for a box width by height."""
    m = [row[:] for row in IDENTITY]
    if name in ("translate", "translatex", "translatey", "translatez", "translate3d"):
        x = _length(args[0], width) if name in ("translate", "translatex", "translate3d") and args else 0.0
        y = (_length(args[1], height) if name in ("translate", "translate3d") and len(args) > 1 else
             _length(args[0], height) if name == "translatey" and args else 0.0)
        z = (_length(args[2], 0) if name == "translate3d" and len(args) > 2 else
             _length(args[0], 0) if name == "translatez" and args else 0.0)
        return _translate(x, y, z)
    if name in ("scale", "scalex", "scaley", "scalez", "scale3d"):
        sx = args[0] if name in ("scale", "scalex", "scale3d") and args else 1.0
        sy = (args[1] if len(args) > 1 else args[0]) if name == "scale" and args else \
            (args[1] if name == "scale3d" and len(args) > 1 else args[0] if name == "scaley" and args else 1.0)
        sz = args[2] if name == "scale3d" and len(args) > 2 else args[0] if name == "scalez" and args else 1.0
        m[0][0], m[1][1], m[2][2] = float(sx), float(sy), float(sz)
        return m
    if name in ("rotate", "rotatez", "rotatex", "rotatey", "rotate3d"):
        if name == "rotate3d":
            x, y, z, angle = (list(args) + [0, 0, 1, 0])[:4]
        else:
            angle = args[0] if args else 0.0
            x, y, z = {"rotatex": (1, 0, 0), "rotatey": (0, 1, 0)}.get(name, (0, 0, 1))
        norm = math.sqrt(x * x + y * y + z * z) or 1.0
        x, y, z = x / norm, y / norm, z / norm
        a = math.radians(angle)
        c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
        return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0.0],
                [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0.0],
                [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0.0],
                [0.0, 0.0, 0.0, 1.0]]
    if name in ("skew", "skewx", "skewy"):
        ax = args[0] if name in ("skew", "skewx") and args else 0.0
        ay = (args[1] if len(args) > 1 else 0.0) if name == "skew" else (args[0] if name == "skewy" and args else 0.0)
        m[0][1], m[1][0] = math.tan(math.radians(ax)), math.tan(math.radians(ay))
        return m
    if name == "matrix" and len(args) == 6:
        a, b, c, d, e, f = args
        return [[a, c, 0.0, e], [b, d, 0.0, f], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    if name == "matrix3d" and len(args) == 16:
        return [[float(args[j * 4 + i]) for j in range(4)] for i in range(4)]
    if name == "perspective" and args:
        depth = _length(args[0], 0)
        if depth > 0:
            m[3][2] = -1.0 / depth
        return m
    return m


def matrix(ops: list, box, origin) -> list:
    """The whole transform, about its origin, in page coordinates."""
    result = [row[:] for row in IDENTITY]
    width, height = box[2], box[3]
    for name, args in ops or []:
        result = multiply(result, function_matrix(name, args, width, height))
    ox, oy, oz = origin
    return multiply(multiply(_translate(ox, oy, oz), result), _translate(-ox, -oy, -oz))


def with_perspective(m: list, perspective) -> list:
    """m seen through a parent's perspective: (depth, origin x, origin y)."""
    if not perspective or perspective[0] <= 0:
        return m
    depth, px, py = perspective
    p = [row[:] for row in IDENTITY]
    p[3][2] = -1.0 / depth
    return multiply(multiply(multiply(_translate(px, py), p), _translate(-px, -py)), m)


def projected(m: list):
    """The 3 by 3 projective part Qt draws with: QTransform's nine numbers.

    A point (x, y) on the box's plane goes to (X/W, Y/W); Qt multiplies row
    vectors, so the matrix is laid out transposed.
    """
    return (m[0][0], m[1][0], m[3][0], m[0][1], m[1][1], m[3][1], m[0][3], m[1][3], m[3][3])


def facing_away(m: list) -> bool:
    """Whether the box's front is turned from the viewer (backface-visibility)."""
    return m[2][2] < 0 and abs(m[2][2]) > 1e-6


# ---------------------------------------------------------------- timing
def _bezier(x1, y1, x2, y2):
    def sample(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def solve(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        low, high = 0.0, 1.0
        for _ in range(30):
            mid = (low + high) / 2
            if sample(mid, x1, x2) < x:
                low = mid
            else:
                high = mid
        return sample((low + high) / 2, y1, y2)
    return solve


NAMED_TIMING = {"linear": None, "ease": (0.25, 0.1, 0.25, 1.0), "ease-in": (0.42, 0.0, 1.0, 1.0),
                "ease-out": (0.0, 0.0, 0.58, 1.0), "ease-in-out": (0.42, 0.0, 0.58, 1.0)}


def timing(text: str):
    """A CSS timing function as a function of 0..1."""
    text = (text or "ease").strip().lower()
    if text in NAMED_TIMING:
        points = NAMED_TIMING[text]
        return (lambda t: t) if points is None else _bezier(*points)
    if text.startswith("cubic-bezier("):
        try:
            points = [float(p) for p in text[13:-1].split(",")]
            return _bezier(*points)
        except (ValueError, TypeError):
            return _bezier(0.25, 0.1, 0.25, 1.0)
    if text in ("step-start", "step-end") or text.startswith("steps("):
        if text == "step-start":
            count, position = 1, "start"
        elif text == "step-end":
            count, position = 1, "end"
        else:
            parts = [p.strip() for p in text[6:-1].split(",")]
            count = max(1, int(float(parts[0])))
            position = parts[1] if len(parts) > 1 else "end"
        # as CSS Easing gives it: how many jumps, and whether one comes first
        jumps = {"jump-none": max(1, count - 1), "jump-both": count + 1}.get(position, count)
        early = position in ("start", "jump-start", "jump-both")

        def step(t):
            current = math.floor(t * count) + (1 if early else 0)
            return max(0, min(jumps, current)) / jumps
        return step
    return lambda t: t                       # linear(...) and the rest: even


def _number(value: float, other: float, share: float) -> float:
    return value + (other - value) * share


def _identity_for(name: str, args: list) -> list:
    if name.startswith("scale"):
        return [1.0] * len(args)
    return [("px", 0.0) if isinstance(a, tuple) else 0.0 for a in args]


def blend_ops(a: list, b: list, share: float, box) -> list:
    """Two transforms part way, as CSS interpolates them: function by function
    when they match; none counts as the identity of the other's functions;
    otherwise their matrices, entry by entry, near enough for drawing."""
    a, b = a or [], b or []
    if not a and b:
        a = [(name, _identity_for(name, args)) for name, args in b]
    if not b and a:
        b = [(name, _identity_for(name, args)) for name, args in a]
    if len(a) == len(b) and all(x[0] == y[0] and len(x[1]) == len(y[1]) for x, y in zip(a, b)):
        out = []
        for (name, args1), (_name, args2) in zip(a, b):
            mixed = []
            for p, q in zip(args1, args2):
                if isinstance(p, tuple) and isinstance(q, tuple) and p[0] == q[0] and p[0] != "mix":
                    mixed.append((p[0], _number(p[1], q[1], share)))
                elif isinstance(p, tuple) or isinstance(q, tuple):
                    size = box[2] if name in ("translate", "translatex", "translate3d") else box[3]
                    mixed.append(("px", _number(_length(p, size), _length(q, size), share)))
                else:
                    mixed.append(_number(float(p), float(q), share))
            out.append((name, mixed))
        return out
    ma = matrix(a, box, (0.0, 0.0, 0.0))
    mb = matrix(b, box, (0.0, 0.0, 0.0))
    mixed = [[_number(ma[i][j], mb[i][j], share) for j in range(4)] for i in range(4)]
    return [("matrix3d", [mixed[i][j] for j in range(4) for i in range(4)])]


def sample(animation: dict, now: float, base_ops: list, base_opacity: float, box):
    """An animation's transform and opacity at now (seconds since the page
    began), or (None, None) where it has no effect then."""
    duration = animation.get("duration", 0.0)
    delay = animation.get("delay", 0.0)
    iterations = animation.get("iterations", 1.0)
    direction = animation.get("direction", "normal")
    fill = animation.get("fill", "none")
    elapsed = (0.0 if animation.get("paused") else now) - delay
    if duration <= 0:
        # no duration: over as soon as it begins, as CSS has it; only the fill
        # mode decides what is shown. (Hugging Face's, an infinite one with
        # its duration in a var(), had made every frame fail.)
        if elapsed < 0:
            if animation.get("fill", "none") not in ("backwards", "both"):
                return None, None
            progress_total, elapsed = 0.0, -1.0
        else:
            if animation.get("fill", "none") not in ("forwards", "both"):
                return None, None
            iterations = 1.0
            progress_total = 1.0
    else:
        progress_total = elapsed / duration
    if elapsed < 0:
        if fill not in ("backwards", "both"):
            return None, None
        iteration, fraction = 0, 0.0
    elif iterations != float("inf") and progress_total >= iterations:
        if fill not in ("forwards", "both"):
            return None, None
        iteration = max(0, math.ceil(iterations) - 1)
        fraction = iterations - math.floor(iterations) or 1.0
    else:
        iteration = int(progress_total)
        fraction = progress_total - iteration
    backwards = direction == "reverse" or (direction == "alternate" and iteration % 2 == 1) or \
        (direction == "alternate-reverse" and iteration % 2 == 0)
    if backwards:
        fraction = 1.0 - fraction
    frames = animation.get("frames") or []
    ops = opacity = None
    for prop in ("transform", "opacity"):
        stops = [(f[0], f[1][prop], f[2]) for f in frames if prop in f[1]]
        if not stops:
            continue
        base = base_ops if prop == "transform" else base_opacity
        if stops[0][0] > 0:
            stops.insert(0, (0.0, base, None))
        if stops[-1][0] < 1:
            stops.append((1.0, base, None))
        before = stops[0]
        after = stops[-1]
        for i in range(len(stops) - 1):
            if stops[i][0] <= fraction <= stops[i + 1][0]:
                before, after = stops[i], stops[i + 1]
                break
        span = after[0] - before[0]
        local = (fraction - before[0]) / span if span > 0 else 1.0
        ease = timing(before[2] or animation.get("timing", "ease"))
        share = ease(max(0.0, min(1.0, local)))
        if prop == "transform":
            ops = blend_ops(before[1], after[1], share, box)
        else:
            opacity = max(0.0, min(1.0, _number(float(before[1]), float(after[1]), share)))
    return ops, opacity
