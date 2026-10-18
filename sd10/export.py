"""Export colours for other software."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional, Sequence, Union

_ACO_LAB = 7  # Adobe colour-space id for L*a*b*


def aco_bytes(colors: Sequence[Sequence[float]], names: Optional[Sequence[str]] = None) -> bytes:
    """Adobe Color Swatch (.aco) data, as the app's "Adobe Color Swatch" export: version 1 + version 2
    (with names) sections, colours stored as L*a*b* (Photoshop/Illustrator treat swatch Lab as D50).
    L* 0..100 -> 0..10000; a*, b* -> signed hundredths, clamped to -128..127."""
    if not colors:
        raise ValueError("no colours to export")
    if names is not None and len(names) != len(colors):
        raise ValueError("names must match colours")
    names = list(names) if names is not None else [f"Color {i}" for i in range(1, len(colors) + 1)]

    def entry(c):
        L, a, b = c[:3]
        return struct.pack(">HHhhH", _ACO_LAB, round(min(max(L, 0.0), 100.0) * 100),
                           round(min(max(a, -128.0), 127.0) * 100), round(min(max(b, -128.0), 127.0) * 100), 0)

    v1 = struct.pack(">HH", 1, len(colors)) + b"".join(entry(c) for c in colors)
    v2 = struct.pack(">HH", 2, len(colors))
    for c, name in zip(colors, names):
        text = name.encode("utf-16-be") + b"\0\0"
        v2 += entry(c) + struct.pack(">I", len(text) // 2) + text
    return v1 + v2


def export_aco(path: Union[str, Path], colors: Sequence[Sequence[float]],
               names: Optional[Sequence[str]] = None) -> Path:
    """Write an Adobe Color Swatch file for Photoshop / Illustrator."""
    path = Path(path)
    path.write_bytes(aco_bytes(colors, names))
    return path


def read_aco(path: Union[str, Path]) -> list[tuple[str, tuple[float, float, float]]]:
    """Read back the Lab swatches of an .aco file (version 2 names if present)."""
    data = Path(path).read_bytes()
    version, count = struct.unpack_from(">HH", data, 0)
    pos = 4
    v1 = []
    for _ in range(count):
        space, w, x, y, _z = struct.unpack_from(">HHhhH", data, pos)
        pos += 10
        v1.append((space, w, x, y))
    names = [f"Color {i}" for i in range(1, count + 1)]
    if version == 1 and pos < len(data):
        _, count2 = struct.unpack_from(">HH", data, pos)
        pos += 4
        for i in range(count2):
            pos += 10
            n, = struct.unpack_from(">I", data, pos)
            pos += 4
            names[i] = data[pos:pos + 2 * n].decode("utf-16-be").rstrip("\0")
            pos += 2 * n
    out = []
    for name, (space, w, x, y) in zip(names, v1):
        if space != _ACO_LAB:
            raise ValueError(f"swatch {name!r} is not Lab (colour space {space})")
        out.append((name, (w / 100, x / 100, y / 100)))
    return out
