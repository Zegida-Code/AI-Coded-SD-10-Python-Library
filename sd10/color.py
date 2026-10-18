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

    def delta_e(self, other: Sequence[float], formula: str = "DE2000") -> float:
        """Colour difference to ``other``, with this colour as the reference
        (formula: DE2000, DE94GA, DE94T or DE76)."""
        return delta_e(self, other, formula)

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


DELTA_E_FORMULAS = ("DE2000", "DE94GA", "DE94T", "DE76")
_FORMULA_ALIASES = {"2000": "DE2000", "00": "DE2000", "DE00": "DE2000", "76": "DE76", "94": "DE94GA",
                    "DE94": "DE94GA", "94GA": "DE94GA", "94T": "DE94T"}


def delta_e(reference: Sequence[float], sample: Sequence[float], formula: str = "DE2000") -> float:
    """Colour difference with any of the device's formulas: DE2000, DE94GA (graphic arts),
    DE94T (textiles) or DE76. Matches Epson's EPMColorDifference; for DE94 the first argument is the
    reference (standard)."""
    f = formula.upper().replace("Δ", "D").replace("E ", "E")
    f = _FORMULA_ALIASES.get(f, f)
    if f == "DE2000":
        return delta_e_2000(reference, sample)
    if f == "DE76":
        return delta_e_76(reference, sample)
    if f in ("DE94GA", "DE94T"):
        return delta_e_94(reference, sample, textiles=(f == "DE94T"))
    raise ValueError(f"unknown formula {formula!r}; choose from {', '.join(DELTA_E_FORMULAS)}")


def delta_e_94(reference: Sequence[float], sample: Sequence[float], textiles: bool = False) -> float:
    """CIE94 colour difference (graphic arts by default, ``textiles=True`` for kL=2 / K1=0.048 / K2=0.014)."""
    kL, k1, k2 = (2.0, 0.048, 0.014) if textiles else (1.0, 0.045, 0.015)
    L1, a1, b1 = reference[:3]
    L2, a2, b2 = sample[:3]
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    dl, dc = L1 - L2, c1 - c2
    dh2 = max((a1 - a2) ** 2 + (b1 - b2) ** 2 - dc ** 2, 0.0)
    sc, sh = 1 + k1 * c1, 1 + k2 * c1
    return math.sqrt((dl / kL) ** 2 + (dc / sc) ** 2 + dh2 / sh ** 2)


def median_lab(labs: Sequence[Sequence[float]]) -> Lab:
    """Per-component median of several readings (mean of the middle two for an even count), as the
    app's "Median" (Controller::GetColorMedianValue)."""
    if not labs:
        raise ValueError("need at least one colour")

    def med(values):
        v = sorted(values)
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    return Lab(*(med([c[k] for c in labs]) for k in range(3)))


# ---- Lab <-> sRGB and colour schemes, as Epson's EPMLab2sRGB / EPMHarmonicLab -----------------
# Reference white (X, Y, Z) per illuminant/observer, from EPMWhitePointXYZ in libmunsell-lib.so.
WHITE_POINTS = {
    "D50/2": (0.964212, 1.0, 0.825188), "D50/10": (0.967206, 1.0, 0.81428),
    "D65/2": (0.95047, 1.0, 1.08883), "D65/10": (0.948097, 1.0, 1.07305),
    "A/2": (1.09847, 1.0, 0.355823), "A/10": (1.11142, 1.0, 0.351998),
    "F1/2": (0.928336, 1.0, 1.036647), "F1/10": (0.947913, 1.0, 1.031914),
    "F2/2": (0.991447, 1.0, 0.673159), "F2/10": (1.03245, 1.0, 0.689897),
    "F3/2": (1.037535, 1.0, 0.498605), "F3/10": (1.089683, 1.0, 0.519648),
    "F7/2": (0.950172, 1.0, 1.086296), "F7/10": (0.957797, 1.0, 1.076183),
    "F8/2": (0.964125, 1.0, 0.823331), "F8/10": (0.971146, 1.0, 0.811347),
    "F9/2": (1.003648, 1.0, 0.678684), "F9/10": (1.021163, 1.0, 0.678256),
    "F10/2": (0.961735, 1.0, 0.817123), "F10/10": (0.990012, 1.0, 0.83134),
    "F11/2": (1.008989, 1.0, 0.642617), "F11/10": (1.038197, 1.0, 0.65555),
    "F12/2": (1.080463, 1.0, 0.392275), "F12/10": (1.114284, 1.0, 0.40353),
}
_D65 = (0.95047, 1.0, 1.08883)
_BRADFORD = ((0.8951, 0.2664, -0.1614), (-0.7502, 1.7135, 0.0367), (0.0389, -0.0685, 1.0296))
_BRADFORD_INV = ((0.9869929, -0.1470543, 0.1599627), (0.4323053, 0.5183603, 0.0492912),
                 (-0.0085287, 0.0400428, 0.9684867))
_XYZ2RGB = ((3.2404542, -1.5371385, -0.4985314), (-0.9692660, 1.8760108, 0.0415560),
            (0.0556434, -0.2040259, 1.0572252))
_RGB2XYZ = ((0.4124564, 0.3575761, 0.1804375), (0.2126729, 0.7151522, 0.0721750),
            (0.0193339, 0.1191920, 0.9503041))
_EPS, _KAPPA = 0.008856, 7.787

COLOR_SCHEMES = {  # hue offsets in HSV degrees (table in libmunsell-lib.so)
    "complementary": (180,), "split_complementary": (150, 210), "analogous": (30, 330),
    "triadic": (120, 240), "tetradic": (60, 180, 240),
}


def _mul(m, v):
    return tuple(sum(m[i][j] * v[j] for j in range(3)) for i in range(3))


def _adapt(xyz, src, dst):
    s, d = _mul(_BRADFORD, src), _mul(_BRADFORD, dst)
    return _mul(_BRADFORD_INV, tuple(c * dd / ss for c, ss, dd in zip(_mul(_BRADFORD, xyz), s, d)))


def _white(illuminant, observer):
    if observer not in OBSERVERS:
        raise ValueError("observer must be 2 or 10")
    return WHITE_POINTS[f"{_illuminant_name(illuminant)}/{observer}"]


def _lab2xyz(lab, wp):
    L, a, b = lab[:3]
    fy = (L + 16) / 116

    def inv(f):
        return f ** 3 if f ** 3 > _EPS else (f - 16 / 116) / _KAPPA
    return tuple(w * inv(f) for w, f in zip(wp, (fy + a / 500, fy, fy - b / 200)))


def _xyz2lab(xyz, wp):
    def f(t):
        return t ** (1 / 3) if t > _EPS else _KAPPA * t + 16 / 116
    fx, fy, fz = (f(v / w) for v, w in zip(xyz, wp))
    return Lab(116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _gamma(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _ungamma(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lab_to_srgb(lab: Sequence[float], illuminant: Union[str, int] = "D50", observer: int = 2) -> tuple:
    """sRGB (0..1, *not* clipped - out-of-gamut colours fall outside) of an L*a*b* colour measured
    under the given illuminant/observer (Bradford-adapted to D65), as Epson's EPMLab2sRGB."""
    wp = _white(illuminant, observer)
    return tuple(_gamma(c) for c in _mul(_XYZ2RGB, _adapt(_lab2xyz(lab, wp), wp, _D65)))


def srgb_to_lab(rgb: Sequence[float], illuminant: Union[str, int] = "D50", observer: int = 2) -> Lab:
    wp = _white(illuminant, observer)
    return _xyz2lab(_adapt(_mul(_RGB2XYZ, tuple(_ungamma(c) for c in rgb)), _D65, wp), wp)


def lab_to_hex(lab: Sequence[float], illuminant: Union[str, int] = "D50", observer: int = 2) -> str:
    """'#RRGGBB' as shown by the app's HEX field (sRGB, clipped to the displayable range)."""
    return "#" + "".join(f"{round(min(max(c, 0.0), 1.0) * 255):02X}"
                         for c in lab_to_srgb(lab, illuminant, observer))


def color_scheme(lab: Sequence[float], scheme: str = "complementary", illuminant: Union[str, int] = "D50",
                 observer: int = 2) -> list[Lab]:
    """Harmonious colours for ``lab``, as the app's "Color Scheme" (EPMHarmonicLab): the colour is
    converted to sRGB/HSV, its hue rotated, and converted back. ``scheme``: complementary,
    split_complementary, analogous, triadic or tetradic."""
    import colorsys
    key = scheme.lower().replace("-", "_").replace(" ", "_")
    if key not in COLOR_SCHEMES:
        raise ValueError(f"unknown scheme {scheme!r}; choose from {', '.join(COLOR_SCHEMES)}")
    rgb = lab_to_srgb(lab, illuminant, observer)
    mx, mn = max(rgb), min(rgb)
    delta = mx - mn
    h = 0.0
    if delta > 0:
        r, g, b = rgb
        if mx == r:
            h = (g - b) / delta % 6
        elif mx == g:
            h = (b - r) / delta + 2
        else:
            h = (r - g) / delta + 4
    s, v = (delta / mx if mx else 0.0), mx
    return [srgb_to_lab(colorsys.hsv_to_rgb(((h * 60 + off) % 360) / 360, s, v), illuminant, observer)
            for off in COLOR_SCHEMES[key]]


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
