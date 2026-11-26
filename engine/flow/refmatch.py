"""Match a look to a reference image: Flow's scriptable version of "grade it like this still".

Resolve's Shot Match only matches timeline clips to each other and has no API, so Flow builds the match
itself and writes it as a normal look LUT (04 Look); the mask tree (flow.masks) then does the spatial
part a single LUT can't: the sky gradient, the subject, the vignette.

The transfer is deliberately partial and capped, so skies keep their gradient and nothing turns into
one flat colour (what a full histogram match does):
  * tone: the pooled source luma curve is moved `tone` of the way toward the reference curve
  * colour: per luma band (shadows → highlights) the mean Cb/Cr shifts toward the reference, capped
  * saturation: per band chroma ratio toward the reference, clipped to [sat_lo, sat_hi]
Works in Rec.709 display space (log footage gets its conversion LUT on 01 Input first).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from flow import luts, masks

KR, KB = 0.2126, 0.0722
KG = 1 - KR - KB
BANDS = [0.0, 0.18, 0.38, 0.6, 0.8, 1.0]           # band centres for the colour / saturation tables


def to_ycc(c) -> Tuple[float, float, float]:
    r, g, b = c
    y = KR * r + KG * g + KB * b
    return y, (b - y) / (2 * (1 - KB)), (r - y) / (2 * (1 - KR))


def from_ycc(y: float, cb: float, cr: float) -> Tuple[float, float, float]:
    r = y + 2 * (1 - KR) * cr
    b = y + 2 * (1 - KB) * cb
    g = (y - KR * r - KB * b) / KG
    return luts.clamp(r), luts.clamp(g), luts.clamp(b)


def _pixels(paths: Sequence[Path]) -> List[Tuple[float, float, float]]:
    px = []
    for p in paths:
        img = masks.load_rgb(Path(p))
        for row in img:
            px.extend(row)
    return px


def _cdf_quantiles(ys: List[float], n: int = 65) -> List[float]:
    s = sorted(ys)
    return [s[min(len(s) - 1, int(i / (n - 1) * (len(s) - 1)))] for i in range(n)]


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / ((xs[i] - xs[i - 1]) or 1e-9)
            return ys[i - 1] + (ys[i] - ys[i - 1]) * t
    return ys[-1]


def _band_stats(px) -> List[Tuple[float, float, float, int]]:
    """Per band: (mean cb, mean cr, mean chroma, count), weighted by distance to the band centre."""
    acc = [[0.0, 0.0, 0.0, 0.0] for _ in BANDS]
    for c in px:
        y, cb, cr = to_ycc(c)
        for i, bc in enumerate(BANDS):
            w = max(0.0, 1 - abs(y - bc) / 0.2)
            if w:
                a = acc[i]
                a[0] += cb * w
                a[1] += cr * w
                a[2] += (cb * cb + cr * cr) ** 0.5 * w
                a[3] += w
    return [(a[0] / a[3], a[1] / a[3], a[2] / a[3], int(a[3])) if a[3] > 1 else (0.0, 0.0, 0.0, 0) for a in acc]


def match(reference: Path, sources: Sequence[Path], tone: float = 0.55, color: float = 0.7,
          max_shift: float = 0.06, sat_lo: float = 0.7, sat_hi: float = 1.15) -> Dict[str, object]:
    """-> {"transform": fn(rgb)->rgb, "stats": {...}, "sky": reference sky colour or None}."""
    ref_px = _pixels([reference])
    src_px = _pixels(sources)
    if not ref_px or not src_px:
        raise ValueError("reference or source frames could not be read")
    qr = _cdf_quantiles([to_ycc(c)[0] for c in ref_px])
    qs = _cdf_quantiles([to_ycc(c)[0] for c in src_px])
    # monotonic source -> reference curve, blended toward identity
    xs, ys = [0.0], [0.0]
    for a, b in zip(qs, qr):
        if a > xs[-1] + 1e-4:
            xs.append(a)
            ys.append(max(ys[-1], b))
    if xs[-1] < 1.0:
        xs.append(1.0)
        ys.append(max(ys[-1], qr[-1]))
    rs, ss = _band_stats(ref_px), _band_stats(src_px)
    dcb, dcr, sat = [], [], []
    for (rcb, rcr, rch, rn), (scb, scr, sch, sn) in zip(rs, ss):
        if rn and sn:
            dcb.append(max(-max_shift, min(max_shift, (rcb - scb) * color)))
            dcr.append(max(-max_shift, min(max_shift, (rcr - scr) * color)))
            ratio = rch / sch if sch > 1e-4 else 1.0
            sat.append(1 + (max(sat_lo, min(sat_hi, ratio)) - 1) * color)
        else:
            dcb.append(0.0)
            dcr.append(0.0)
            sat.append(1.0)

    def tf(c):
        y, cb, cr = to_ycc(c)
        y2 = luts.lerp(y, _interp(xs, ys, y), tone)
        k = _interp(BANDS, sat, y2)
        return from_ycc(y2, cb * k + _interp(BANDS, dcb, y2), cr * k + _interp(BANDS, dcr, y2))

    reg = masks.regions(masks.load_rgb(Path(reference)))
    sky = (reg.get("sky") or {}).get("rgb") if (reg.get("sky") or {}).get("area", 0) > 0.05 else None
    if sky is None:                      # no measurable sky in the reference: use its brightest band's colour
        y = 0.8
        sky = list(from_ycc(y, rs[-2][0], rs[-2][1]))
    return {"transform": tf, "sky": [round(v, 4) for v in sky],
            "stats": {"tone_curve": [[round(a, 3), round(b, 3)] for a, b in zip(xs[::8], ys[::8])],
                      "shift_cb": [round(v, 4) for v in dcb], "shift_cr": [round(v, 4) for v in dcr],
                      "sat": [round(v, 3) for v in sat]}}


def write_match_lut(reference: Path, sources: Sequence[Path], out: Path, **kw) -> Dict[str, object]:
    m = match(reference, sources, **kw)
    luts.write_cube(out, m["transform"], f"Flow reference match {Path(reference).stem}")
    return {"lut": str(out), "sky": m["sky"], "stats": m["stats"]}
