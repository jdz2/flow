"""Procedural color looks written as 3D .cube LUTs, plus auto-balance (CDL) math.

Everything works in display-referred Rec.709 gamma space on 0..1 RGB, which
matches phone and most camera footage. Log footage should get its conversion
LUT in Resolve first; Flow's looks are creative, not technical, transforms.
"""
from __future__ import annotations

import colorsys
import math
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

RGB = Tuple[float, float, float]

LUT_SIZE = 33


# ---------------------------------------------------------------- building blocks
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def luma(r: float, g: float, b: float) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def smoothstep(e0: float, e1: float, x: float) -> float:
    t = clamp((x - e0) / (e1 - e0)) if e1 != e0 else 0.0
    return t * t * (3 - 2 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def s_curve(x: float, amount: float, pivot: float = 0.45) -> float:
    """Contrast curve that keeps 0 -> 0 and 1 -> 1."""
    x = clamp(x)
    if amount == 0:
        return x
    if x < pivot:
        t = x / pivot
        y = pivot * (t ** (1 + amount))
    else:
        t = (x - pivot) / (1 - pivot)
        y = pivot + (1 - pivot) * (1 - (1 - t) ** (1 + amount))
    return y


def saturate(c: RGB, s: float) -> RGB:
    y = luma(*c)
    return (y + (c[0] - y) * s, y + (c[1] - y) * s, y + (c[2] - y) * s)


def fade(x: float, black: float, white: float) -> float:
    """Lift blacks / pull whites (matte film look)."""
    return black + x * (white - black)


def split_tone(c: RGB, shadow: RGB, highlight: RGB, amount: float = 1.0) -> RGB:
    y = luma(*c)
    hw = smoothstep(0.35, 0.9, y)
    sw = 1.0 - smoothstep(0.1, 0.55, y)
    return tuple(  # type: ignore[return-value]
        c[i] + amount * (shadow[i] * sw + highlight[i] * hw) for i in range(3)
    )


def hue_shift_toward(c: RGB, src_lo: float, src_hi: float, target: float, strength: float,
                     sat_mult: float = 1.0) -> RGB:
    """Pull hues within [src_lo, src_hi] degrees toward `target` degrees."""
    h, l, s = colorsys.rgb_to_hls(clamp(c[0]), clamp(c[1]), clamp(c[2]))
    deg = h * 360.0
    if src_lo <= deg <= src_hi and s > 0.02:
        mid = (src_lo + src_hi) / 2
        half = (src_hi - src_lo) / 2
        w = 1.0 - abs(deg - mid) / half  # 1 at band center, 0 at edges
        w = smoothstep(0.0, 0.6, w) * strength * smoothstep(0.03, 0.25, s)
        deg = lerp(deg, target, w)
        s = clamp(lerp(s, s * sat_mult, w))
        r, g, b = colorsys.hls_to_rgb((deg % 360) / 360.0, l, s)
        return (r, g, b)
    return c


def per_channel(c: RGB, fn: Callable[[float], float]) -> RGB:
    return (fn(c[0]), fn(c[1]), fn(c[2]))


# ---------------------------------------------------------------- looks
def look_identity(c: RGB) -> RGB:
    return c


def look_natural(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.12))
    c = saturate(c, 1.08)
    return (c[0] * 1.008, c[1], c[2] * 0.992)


def look_orange_teal(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.22))
    # Pull blues/cyans/greens toward teal and protect/enrich skin.
    c = hue_shift_toward(c, 70, 260, 188, 0.55, 1.05)
    c = hue_shift_toward(c, 5, 55, 28, 0.35, 1.10)
    c = split_tone(c, shadow=(-0.035, 0.012, 0.045), highlight=(0.045, 0.012, -0.04))
    return saturate(c, 1.06)


def look_film(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.25, 0.42))
    c = per_channel(c, lambda x: fade(x, 0.035, 0.965))
    c = split_tone(c, shadow=(-0.012, 0.012, 0.018), highlight=(0.03, 0.012, -0.022))
    return saturate(c, 0.9)


def look_moody(c: RGB) -> RGB:
    c = per_channel(c, lambda x: clamp(x) ** 1.15)
    c = per_channel(c, lambda x: s_curve(x, 0.3, 0.4))
    c = split_tone(c, shadow=(-0.02, 0.0, 0.035), highlight=(0.015, 0.005, -0.01))
    c = saturate(c, 0.78)
    return per_channel(c, lambda x: fade(x, 0.02, 0.97))


def look_warm(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.1))
    c = (c[0] * 1.05, c[1] * 1.01, c[2] * 0.9)
    c = split_tone(c, shadow=(0.01, 0.0, -0.01), highlight=(0.03, 0.015, -0.02))
    return saturate(c, 1.05)


def look_cool(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.12))
    c = (c[0] * 0.93, c[1] * 1.0, c[2] * 1.07)
    c = split_tone(c, shadow=(-0.01, 0.0, 0.03), highlight=(-0.01, 0.0, 0.015))
    return saturate(c, 0.95)


def look_bw(c: RGB) -> RGB:
    # Slight red filter feel: weight reds a bit more for nicer skin.
    y = 0.30 * c[0] + 0.59 * c[1] + 0.11 * c[2]
    y = s_curve(y, 0.3, 0.45)
    return (y, y, y)


def look_vintage(c: RGB) -> RGB:
    c = per_channel(c, lambda x: s_curve(x, 0.15))
    c = per_channel(c, lambda x: fade(x, 0.07, 0.93))
    c = split_tone(c, shadow=(0.02, -0.005, 0.03), highlight=(0.035, 0.03, -0.035))
    return saturate(c, 0.82)


# Nightlife: club light cleanup into a teal/orange world, skin kept natural.
# Hue remap (degrees in -> out). Blue/violet and green move to teal, magenta to warm red,
# yellow to orange. The steep stretches (yellow-green, violet-magenta) are also desaturated
# so the remap can't tear.
# Violet is split at 275 deg: blue-violet goes to teal, pink-violet goes to warm red (the skin side).
# Saturation drops to zero right at the split, so nothing can come out purple.
NIGHT_HUE = [(0, 4), (20, 22), (35, 30), (50, 38), (65, 46), (100, 160), (140, 174), (180, 184),
             (210, 189), (240, 193), (275, 200)]
NIGHT_HUE_WARM = [(275, 348), (310, 354), (345, 360), (360, 364)]
NIGHT_SAT = [(0, 1.0), (40, 1.0), (60, 0.9), (80, 0.7), (100, 0.62), (160, 0.66), (180, 0.82), (200, 0.72),
             (240, 0.55), (260, 0.3), (275, 0.0), (290, 0.3), (310, 0.5), (330, 0.68), (345, 0.85), (360, 1.0)]


def night_hue(deg: float) -> float:
    return _interp(NIGHT_HUE, deg) if deg < 275.0 else _interp(NIGHT_HUE_WARM, deg)


def _interp(table, x: float) -> float:
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0) if x1 != x0 else 0.0)
    return table[-1][1]


def _hue_dist(a: float, b: float) -> float:
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def nightlife_transform(keep_green: bool = False, warm: float = 0.0, skin_boost: float = 1.3,
                        skin_hue: float = 24.0, soft: float = 0.0) -> Callable[[RGB], RGB]:
    """Nightlife look, tuned per clip.

    Skin is detected on the incoming pixel and handled separately: it is not hue-remapped,
    gets no teal shadow tint (pink + teal = purple), and is rebuilt on an orange skin line
    with a minimum saturation so it reads warm and alive.

    keep_green  leave green hues alone (e.g. a prop that really is green)
    warm        0..1 warm-light correction: cools the cast, tames neon red/orange light,
                deepens teal shadows (skin is rebuilt warm afterwards)
    skin_boost  saturation lift for skin
    soft        0..1 "soft teal / soft orange": paler teal, gentler orange skin, neon green/blue/
                purple light pulled down harder, a little less contrast
    """
    warm = clamp(warm)
    soft = clamp(soft)
    skin_boost = skin_boost - 0.18 * soft
    skin_max = 0.58 - 0.12 * soft
    cool_sat = 1.0 - 0.3 * soft          # extra saturation cut for everything on the teal side

    def tf(c: RGB) -> RGB:
        r, g, b = clamp(c[0]), clamp(c[1]), clamp(c[2])
        if warm > 0:  # counter the warm cast first
            r, g, b = r * (1.0 - 0.10 * warm), g * (1.0 - 0.015 * warm), clamp(b * (1.0 + 0.14 * warm) + 0.01 * warm)
        y0 = luma(r, g, b)
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        deg = h * 360.0
        # skin, judged on the incoming pixel: pinkish-red .. orange, not neon, not black/white
        d0 = _hue_dist(deg, 22.0)
        if d0 >= 0:   # toward yellow
            hue_w = 1.0 - smoothstep(30.0, 48.0, d0)
            sat_hi = (0.62, 0.8)
        else:         # toward pink/magenta: magenta-lit faces count, saturated magenta light doesn't
            hue_w = 1.0 - smoothstep(48.0, 66.0, -d0)
            sat_hi = (0.5, 0.66) if d0 < -30.0 else (0.62, 0.8)
        sk = (hue_w * smoothstep(0.03, 0.12, s) * (1.0 - smoothstep(sat_hi[0], sat_hi[1], s))
              * smoothstep(0.08, 0.22, l) * (1.0 - smoothstep(0.86, 0.96, l)))
        violet = smoothstep(240.0, 255.0, deg) * (1.0 - smoothstep(325.0, 340.0, deg))
        # neutrals and skin keep their hue, except lilac casts which always get resolved
        w = max(smoothstep(0.04, 0.3, s), violet * smoothstep(0.015, 0.08, s)) * (1.0 - sk)
        w_sat = max(w, violet * (1.0 - sk))
        green_kept = 0.0
        if keep_green and 55.0 <= deg <= 195.0:
            green_kept = smoothstep(55.0, 70.0, deg) * (1.0 - smoothstep(180.0, 195.0, deg))
            w *= 1.0 - green_kept
        nd = deg + (night_hue(deg) - deg) * w
        if soft > 0:  # soft: green light lands on teal, not on teal-green
            nd += 8.0 * soft * smoothstep(85.0, 110.0, deg) * (1.0 - smoothstep(165.0, 190.0, deg)) * w
        ns = s * (1.0 + (_interp(NIGHT_SAT, deg) - 1.0) * w_sat)
        if soft > 0 and 60.0 <= deg < 290.0:  # green, cyan, blue, violet light -> soft teal
            band = smoothstep(60.0, 80.0, deg) * (1.0 - smoothstep(270.0, 290.0, deg))
            ns *= 1.0 + (cool_sat - 1.0) * band * w_sat * (1.0 - 0.5 * green_kept)
            # lime / yellow-green light: the remap would throw it to green, so it is mostly removed
            lime = smoothstep(52.0, 66.0, deg) * (1.0 - smoothstep(98.0, 112.0, deg))
            ns *= 1.0 - 0.6 * soft * lime * (1.0 - green_kept)
        nd %= 360.0
        # skin that only shows up after the remap (violet/pink-lit faces now on the warm side)
        d1 = _hue_dist(nd, 22.0)
        sk2 = ((1.0 - smoothstep(30.0, 48.0, abs(d1))) * smoothstep(0.02, 0.1, ns)
               * (1.0 - smoothstep(0.55, 0.72, ns)) * smoothstep(0.08, 0.22, l) * (1.0 - smoothstep(0.88, 0.97, l))
               * smoothstep(268.0, 285.0, deg))
        sk = max(sk, sk2)
        # pale green/cyan wash on skin and whites (not real teal light): neutralize it
        if not green_kept:
            pale_green = (smoothstep(60.0, 75.0, deg) * (1.0 - smoothstep(195.0, 210.0, deg))
                          * (1.0 - smoothstep(0.12, 0.28, s)) * smoothstep(0.25, 0.4, l) * (1.0 - sk))
            ns *= 1.0 - 0.75 * pale_green
        knee = 0.62 - 0.12 * warm - 0.12 * soft  # tame neon light (warm clips harder)
        knee = knee + (1.0 - knee) * green_kept * (1.0 - 0.6 * soft)
        if ns > knee:
            ns = knee + (ns - knee) * (0.35 - 0.12 * soft)
        r2, g2, b2 = colorsys.hls_to_rgb(nd / 360.0, l, clamp(ns))
        y1 = luma(r2, g2, b2)  # keep brightness: a blue wash turned teal must not get brighter
        if y1 > 1e-4:
            k = 1.0 + (y0 / y1 - 1.0) * 0.85
            r2, g2, b2 = r2 * k, g2 * k, b2 * k
            mx = max(r2, g2, b2)
            if mx > 1.0:
                yy = luma(r2, g2, b2)
                t = (mx - 1.0) / max(1e-4, mx - yy)
                r2, g2, b2 = (r2 + (yy - r2) * t, g2 + (yy - g2) * t, b2 + (yy - b2) * t)
        c2 = per_channel((r2, g2, b2), lambda x: s_curve(x, 0.14 - 0.04 * soft, 0.42))
        tint = (1.0 + 1.5 * warm) * (1.0 - sk)
        c2 = split_tone(c2, shadow=(-0.02 * tint, 0.012 * tint, 0.016 * tint),
                        highlight=(0.03, 0.012, -0.022))
        c2 = per_channel(c2, lambda x: fade(x, 0.008, 0.99))
        if sk > 0.001:  # rebuild skin on the orange skin line
            hs, ls, ss = colorsys.rgb_to_hls(clamp(c2[0]), clamp(c2[1]), clamp(c2[2]))
            hd = hs * 360.0
            hd = (hd + _hue_dist(skin_hue, hd) * 0.8 * sk) % 360.0
            ss = ss + (min(max(ss, 0.26 - 0.04 * soft) * skin_boost, skin_max) - ss) * sk
            c2 = colorsys.hls_to_rgb(hd / 360.0, ls, clamp(ss))
        # purple guard: whatever happened above, nothing leaves this look violet/purple
        hg, lg, sg = colorsys.rgb_to_hls(clamp(c2[0]), clamp(c2[1]), clamp(c2[2]))
        dg = hg * 360.0
        if 245.0 <= dg <= 335.0 and sg > 0.1:
            band = smoothstep(245.0, 258.0, dg) * (1.0 - smoothstep(322.0, 335.0, dg))
            c2 = colorsys.hls_to_rgb(hg, lg, sg + (min(sg, 0.1) - sg) * band)
        return c2

    return tf


look_nightlife = nightlife_transform()


LOOKS: Dict[str, Tuple[str, Callable[[RGB], RGB]]] = {
    "natural": ("Natural", look_natural),
    "orange-teal": ("Orange & Teal", look_orange_teal),
    "film": ("Film", look_film),
    "moody": ("Moody", look_moody),
    "warm": ("Warm", look_warm),
    "cool": ("Cool", look_cool),
    "bw": ("B&W", look_bw),
    "vintage": ("Vintage", look_vintage),
    "nightlife": ("Nightlife", look_nightlife),
}

LOOK_ALIASES = {
    "orange & teal": "orange-teal", "orange and teal": "orange-teal", "teal-orange": "orange-teal",
    "teal & orange": "orange-teal", "b&w": "bw", "black & white": "bw", "black and white": "bw",
    "mono": "bw", "monochrome": "bw", "golden": "warm", "cinematic": "film",
    "club": "nightlife", "night": "nightlife", "party": "nightlife", "nightclub": "nightlife",
}


def normalize_look(name: Optional[str]) -> Optional[str]:
    """Map user words to a look id. None/'none'/'leave' -> None (no grade)."""
    if not name:
        return None
    key = name.strip().lower()
    if key in ("none", "leave", "leave it", "off", "no", "original"):
        return None
    key = LOOK_ALIASES.get(key, key).replace(" ", "-")
    if key not in LOOKS:
        raise ValueError(f"Unknown look '{name}'. Choose from: {', '.join(LOOKS)} or a .cube path.")
    return key


# ---------------------------------------------------------------- CDL balance
def cdl_apply(c: RGB, slope: RGB, offset: RGB, power: RGB, sat: float) -> RGB:
    out = []
    for i in range(3):
        v = c[i] * slope[i] + offset[i]
        v = clamp(v)
        out.append(v ** power[i] if v > 0 else 0.0)
    return saturate((out[0], out[1], out[2]), sat)


def balance_from_stats(color: dict, strength: float = 0.6) -> dict:
    """Gentle auto exposure / white balance / contrast fix as an ASC CDL.

    `color` comes from analyze: y_avg, y_low, y_high in 0..1 (full range),
    cb, cr offsets in -0.5..0.5, sat_avg 0..1.
    """
    y_avg = float(color.get("y_avg", 0.45))
    y_low = float(color.get("y_low", 0.05))
    y_high = float(color.get("y_high", 0.9))
    cb = float(color.get("cb", 0.0))
    cr = float(color.get("cr", 0.0))
    sat_avg = float(color.get("sat_avg", 0.25))

    # Levels: stretch toward 0.03..0.94 (only ever partially).
    lo_t, hi_t = 0.03, 0.94
    span = max(0.2, y_high - y_low)
    gain = clamp((hi_t - lo_t) / span, 0.85, 1.35)
    gain = lerp(1.0, gain, strength)
    lift = lerp(0.0, lo_t - y_low * gain, strength)

    # Exposure toward a mid-grey-ish average.
    mid_after = y_avg * gain + lift
    power = 1.0
    if mid_after > 0.02:
        target = 0.46
        p = math.log(target) / math.log(clamp(mid_after, 0.05, 0.95))
        power = lerp(1.0, clamp(p, 0.8, 1.25), strength)

    # White balance: remove part of the average chroma cast (BT.709 YCbCr->RGB).
    k = strength * 0.8
    dr = -k * (1.5748 * cr)
    dg = -k * (-0.1873 * cb - 0.4681 * cr)
    db = -k * (1.8556 * cb)

    sat = 1.0
    if sat_avg < 0.12:
        sat = lerp(1.0, 1.15, strength)
    elif sat_avg > 0.45:
        sat = lerp(1.0, 0.92, strength)

    slope = (round(gain, 4),) * 3
    offset = tuple(round(clamp(lift + d, -0.15, 0.15), 4) for d in (dr, dg, db))
    return {
        "slope": list(slope),
        "offset": list(offset),
        "power": [round(power, 4)] * 3,
        "saturation": round(sat, 4),
    }


def cdl_strings(cdl: dict) -> dict:
    """Format for Resolve's TimelineItem.SetCDL."""
    def f(v):
        return " ".join(f"{x:.4f}" for x in v)
    return {"Slope": f(cdl["slope"]), "Offset": f(cdl["offset"]), "Power": f(cdl["power"]),
            "Saturation": float(cdl["saturation"])}


# ---------------------------------------------------------------- .cube IO
def build_transform(look: Optional[str], intensity: float = 1.0,
                    cdl: Optional[dict] = None, fn: Optional[Callable[[RGB], RGB]] = None) -> Callable[[RGB], RGB]:
    fn = fn or (LOOKS[look][1] if look else look_identity)
    intensity = clamp(intensity, 0.0, 1.5)

    def tf(c: RGB) -> RGB:
        if cdl:
            c = cdl_apply(c, tuple(cdl["slope"]), tuple(cdl["offset"]), tuple(cdl["power"]),
                          float(cdl["saturation"]))
        graded = fn(c)
        return tuple(clamp(c[i] + (graded[i] - c[i]) * intensity) for i in range(3))  # type: ignore

    return tf


def write_cube(path: Path, transform: Callable[[RGB], RGB], title: str, size: int = LUT_SIZE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = size - 1
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0",
             "DOMAIN_MAX 1.0 1.0 1.0", ""]
    # .cube order: red changes fastest, then green, then blue.
    for bi in range(size):
        b = bi / n
        for gi in range(size):
            g = gi / n
            for ri in range(size):
                r = ri / n
                o = transform((r, g, b))
                lines.append(f"{o[0]:.6f} {o[1]:.6f} {o[2]:.6f}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
    tmp.replace(path)
    return path


def read_cube(path: Path) -> Tuple[int, list]:
    size = 0
    data = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
        elif line[0].isdigit() or line[0] in "-.":
            data.append(tuple(float(v) for v in line.split()[:3]))
    return size, data


def install_base_looks(dest: Path) -> list:
    """Write every look at 100% into Resolve's LUT/Flow folder."""
    written = []
    for key, (label, _) in LOOKS.items():
        p = dest / f"Flow {label.replace('&', 'and')}.cube"
        write_cube(p, build_transform(key, 1.0), f"Flow {label}")
        written.append(str(p))
    return written


def list_user_luts(root: Path) -> list:
    """All .cube files under Resolve's LUT folder (for 'use my LUT')."""
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*.cube")):
        if "Flow" in p.relative_to(root).parts[:1]:
            continue
        out.append(str(p))
    return out


def compose_cube(base: Path, transform: Callable[[RGB], RGB], out: Path, title: str) -> Path:
    """New LUT = transform applied after an existing 3D LUT (same grid; no resampling)."""
    size, data = read_cube(base)
    if not size or len(data) != size ** 3:
        raise ValueError(f"{base.name} is not a plain 3D .cube LUT")
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0", ""]
    for v in data:
        o = transform((clamp(v[0]), clamp(v[1]), clamp(v[2])))
        lines.append(f"{o[0]:.6f} {o[1]:.6f} {o[2]:.6f}")
    tmp = out.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
    tmp.replace(out)
    return out
