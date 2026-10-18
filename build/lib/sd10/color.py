"""Spectrum -> CIE L*a*b*, matching the Epson Spectrometer app.

The SD-10 only reports a 31-band reflectance spectrum; the app converts it on the phone with
librmcc.so (RIPMCCONVAPI_ConvertSpectralToLab). That conversion is linear in the spectrum up to XYZ,
so it reduces to white-normalised weights W[k][i] (X/Xn, Y/Yn, Z/Zn) per illuminant/observer. The
weights in data/lab_weights.json were recovered from librmcc and reproduce it to ~1e-6 dE for all
12 illuminants x 2 observers. As in the app, bands outside 400-700 nm count as zero (so a perfect
white is L* 99.99, not exactly 100).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from importlib import resources
from typing import NamedTuple, Sequence, Union

WAVELENGTHS: tuple[int, ...] = tuple(range(400, 701, 10))  # 31 bands

# Illuminant name -> device/app code (EPSPM_LIGHTSOURCE)
ILLUMINANTS = {"D50": 0, "D65": 1, "A": 2, "F1": 17, "F2": 18, "F3": 19, "F7": 23,
               "F8": 24, "F9": 25, "F10": 26, "F11": 27, "F12": 28}
ILLUMINANT_NAMES = {v: k for k, v in ILLUMINANTS.items()}
OBSERVERS = (2, 10)


class LCh(NamedTuple):
    L: float
    C: float
    h: float  # degrees, 0..360


class Lab(NamedTuple):
    L: float
    a: float
    b: float

    @property
    def lch(self) -> LCh:
        return LCh(self.L, math.hypot(self.a, self.b), math.degrees(math.atan2(self.b, self.a)) % 360.0)

    def delta_e(self, other: Sequence[float], formula: str = "2000") -> float:
        """Colour difference to ``other`` (formula "2000" or "76")."""
        if formula in ("2000", "00", "DE2000"):
            return delta_e_2000(self, other)
        if formula in ("76", "DE76"):
            return delta_e_76(self, other)
        raise ValueError(f"unknown formula {formula!r}")

    def __str__(self) -> str:
        return f"L*={self.L:.2f} a*={self.a:.2f} b*={self.b:.2f}"


@lru_cache(maxsize=None)
def _weights() -> dict:
    return json.loads(resources.files(__package__).joinpath("data/lab_weights.json").read_text())


def _illuminant_name(illuminant: Union[str, int]) -> str:
    if isinstance(illuminant, int):
        try:
            return ILLUMINANT_NAMES[illuminant]
        except KeyError:
            raise ValueError(f"unknown illuminant code {illuminant}") from None
    name = illuminant.upper()
    if name not in ILLUMINANTS:
        raise ValueError(f"unknown illuminant {illuminant!r}; choose from {', '.join(ILLUMINANTS)}")
    return name


def _f(t: float) -> float:
    return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29


def spectrum_to_lab(spectrum: Sequence[float], illuminant: Union[str, int] = "D50", observer: int = 2) -> Lab:
    """Convert 31 reflectance values (400..700 nm, 10 nm step, 0..1) to CIE L*a*b*."""
    if len(spectrum) != len(WAVELENGTHS):
        raise ValueError(f"spectrum must have {len(WAVELENGTHS)} values (400-700 nm, 10 nm step)")
    if observer not in OBSERVERS:
        raise ValueError("observer must be 2 or 10")
    W = _weights()[f"{_illuminant_name(illuminant)}/{observer}"]
    fx, fy, fz = (_f(sum(r * w for r, w in zip(spectrum, row))) for row in W)
    return Lab(116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_76(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    return math.dist(lab1[:3], lab2[:3])


def delta_e_2000(lab1: Sequence[float], lab2: Sequence[float], kL: float = 1.0, kC: float = 1.0,
                 kH: float = 1.0) -> float:
    """CIEDE2000 (Sharma, Wu & Dalal 2005)."""
    L1, a1, b1 = lab1[:3]
    L2, a2, b2 = lab2[:3]
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar ** 7 / (c_bar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0

    d_lp, d_cp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        d_hp = 0.0
    else:
        d_hp = h2p - h1p
        if d_hp > 180:
            d_hp -= 360
        elif d_hp < -180:
            d_hp += 360
    d_Hp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(d_hp / 2))

    l_bar, c_barp = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    else:
        h_bar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2

    t = (1 - 0.17 * math.cos(math.radians(h_bar - 30)) + 0.24 * math.cos(math.radians(2 * h_bar))
         + 0.32 * math.cos(math.radians(3 * h_bar + 6)) - 0.20 * math.cos(math.radians(4 * h_bar - 63)))
    d_theta = 30 * math.exp(-((h_bar - 275) / 25) ** 2)
    r_c = 2 * math.sqrt(c_barp ** 7 / (c_barp ** 7 + 25 ** 7))
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * c_barp
    s_h = 1 + 0.015 * c_barp * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    dl, dc, dh = d_lp / (kL * s_l), d_cp / (kC * s_c), d_Hp / (kH * s_h)
    return math.sqrt(dl * dl + dc * dc + dh * dh + r_t * dc * dh)
