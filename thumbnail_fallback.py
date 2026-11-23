# SPDX-License-Identifier: GPL-3.0-only
"""Miniatura isométrica ortográfica para BrickLayers Web."""
import math
from io import BytesIO
from PIL import Image, ImageDraw


def generate_gcode_thumbnail(gcode_bytes, parser, size=(600, 400),
                             max_segments=120000):
    """Devuelve PNG o None a partir de los recorridos del parser existente.

    Proyección isométrica ortográfica: conserva la Z, sin perspectiva.
    Es un dibujo de recorridos, no un render sólido con oclusión exacta.
    """
    if not gcode_bytes:
        return None
    width, height = map(int, size)
    if width < 64 or height < 64 or max_segments < 1:
        raise ValueError("Tamaño o límite de segmentos no válido")
    max_segments = int(max_segments)
    layers, _ = parser(gcode_bytes)
    sqrt2, sqrt6 = math.sqrt(2), math.sqrt(6)
    sqrt23 = math.sqrt(2 / 3)

    def segments():
        for layer in layers:
            for segment in layer.get("segments", []):
                points = segment.get("points", [])
                for a, b in zip(points, points[1:]):
                    if len(a) < 3 or len(b) < 3:
                        continue
                    if not all(math.isfinite(v) for v in (*a[:3], *b[:3])):
                        continue
                    if sum((b[i] - a[i]) ** 2 for i in range(3)) <= 1e-18:
                        continue
                    yield a, b

    def project(p):
        x, y, z = p[:3]
        return (x - y) / sqrt2, z * sqrt23 - (x + y) / sqrt6

    umin = vmin = float("inf")
    umax = vmax = -float("inf")
    zmin, zmax = float("inf"), -float("inf")
    count = 0
    for a, b in segments():
        for p in (a, b):
            u, v = project(p)
            umin, vmin = min(umin, u), min(vmin, v)
            umax, vmax = max(umax, u), max(vmax, v)
            zmin, zmax = min(zmin, p[2]), max(zmax, p[2])
        count += 1
    if count == 0:
        return None

    factor = 2
    w, h = width * factor, height * factor
    margin = 20 * factor
    scale = min((w - 2 * margin) / max(umax - umin, 1e-9),
                (h - 2 * margin) / max(vmax - vmin, 1e-9))
    cu, cv = (umin + umax) / 2, (vmin + vmax) / 2
    selected_count = min(count, max_segments)
    drawing = []
    for index, (a, b) in enumerate(segments()):
        if ((index + 1) * selected_count // count ==
                index * selected_count // count):
            continue
        ua, va = project(a)
        ub, vb = project(b)
        depth = sum(a[:3]) / 2 + sum(b[:3]) / 2
        height_ratio = ((a[2] + b[2]) / 2 - zmin) / max(zmax - zmin, 1e-9)
        color = (255, round(147 + 65 * height_ratio),
                 round(30 + 45 * height_ratio))
        drawing.append((depth, (
            (ua - cu) * scale + w / 2,
            h / 2 - (va - cv) * scale,
            (ub - cu) * scale + w / 2,
            h / 2 - (vb - cv) * scale,
        ), color))

    drawing.sort(key=lambda item: item[0])
    image = Image.new("RGB", (w, h), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    for _, points, color in drawing:
        draw.line(points, fill=color, width=2 * factor)
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
