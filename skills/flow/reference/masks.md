# Masks: the grading ruleset (Finish mode)

Turn it on with `grade_clips.masks` (and/or `grade_clips.reference`). Every graded clip then gets the
**mask tree** on the Color page instead of the plain six-node tree:

| # | node | kind | what it does |
|---|---|---|---|
| 01 | Input | LUT | the clip's own conversion LUT (camera log), kept |
| 02 | Balance | CDL | (reserved, neutral) |
| 03 | Exposure | CDL | (reserved, neutral) |
| 04 | Look | LUT | the reference match, or the named Flow look |
| 05 | Skin | qualifier | skin hues pulled to the skin line, saturation kept natural |
| 06 | Greens | qualifier | foliage pulled toward olive and desaturated |
| 07 | Highlights | qualifier | soft shoulder + warm tint on the top end |
| 08 | Sky | window | graduated ellipse from the top edge to the horizon: density, sky colour, saturation |
| 09 | Subject | window | soft ellipse on the subject, lifted so it separates from the background |
| 10 | Vignette | window | inverted ellipse, centred between frame centre and the subject |

Windows are real Resolve power windows (written into a per-clip .drx; you can drag them on the Color
page). Qualifiers are soft-keyed LUTs that do what an HSL qualifier + correction does; a colorist can
swap them for a live qualifier. A node whose rule doesn't fire stays in the tree, **disabled**, so all
clips share one layout.

## How Flow decides (per clip, from its own neutral frame)

| rule | fires when | default correction |
|---|---|---|
| `sky` | ≥ 8 % of the frame is bright, smooth, non-green and touches the top edge | density 0.18, half-way toward the reference sky colour, sat 1.12 |
| `subject` | saliency confidence ≥ 0.2 (or a point given in `subjects`) | +0.14 slope (more in dark shots, less in bright), sat 1.04 |
| `vignette` | always (amount > 0) | slope 1 − amount (0.22), follows the subject 60 % |
| `greens` | ≥ 4 % green foliage | hue → 55°, sat × 0.62, luma × 0.94 |
| `skin` | 1.2–15 % skin-hued pixels (above 15 % it is warm light on surfaces, not people) | hue → 22°, sat clamped 0.22–0.55 |
| `highlights` | ≥ 1.5 % of pixels above 0.9 | shoulder from 0.8, tint 1.03 / 1.0 / 0.95 |

The report (`grade_report.json`) records, per clip, the measurements (`regions`), which nodes are on
(`mask_nodes_on`), and **why** each node is on or off (`masks`). `grade/masks/<clip>_windows.jpg` shows the
window outlines on the analysed frame. Read a few before reporting.

## Plan keys

```jsonc
"grade_clips": {
  "reference": "/path/to/still.jpg",   // match the look to this image (04 Look); its sky colour feeds the Sky rule
  "reference_strength": {"tone": 0.55, "color": 0.7, "max_shift": 0.06},   // optional
  "masks": true,                       // or overrides: {"greens": false, "sky": {"density": 0.3}, "vignette": {"amount": 0.15}}
  "subjects": {"V1#4": [0.62, 0.48]},  // where the eye should go (x, y from the top-left, 0–1); beats detection
  "vignette": 0.22,
  "replace_existing": true             // re-grade clips that already carry a Flow tree (a colorist's grade is kept unless this is set)
}
```

Without `reference`, `look` names a Flow look (orange-teal, film…) for 04 Look.

## When to tune

- Overcooked sky (flat colour, banding): lower `sky.density` or `sky.tint`; check the horizon in the preview.
- Subject window off target: give `subjects` for that clip (read the contact sheet), Flow's detector is a
  centre-weighted saliency guess.
- The look should stay greener (spring, golf course): `{"greens": false}` or `{"greens": {"sat": 0.85}}`.
- Night / club footage: keep `skin`, turn `sky` off, and prefer `look: "nightlife"` without a reference.

## Reference grading vs. Resolve's own tools

Resolve's **Shot Match** only matches clips on the timeline to each other and has no API; **Color Match**
needs a chart. Flow's reference match is its scriptable replacement: a partial tone-curve match, colour
shifts per luma band and capped saturation (see `flow/refmatch.py`), so skies keep their gradient. To compare
by eye, put the reference still in the Gallery and use split-screen.

## Limits

- Windows are static (no tracking): fine for most shots; for a fast-moving subject, track the Subject window
  on the Color page (one click on the node).
- Magic Mask: `TimelineItem.CreateMagicMask` exists, but needs strokes drawn by hand first, so Flow doesn't
  use it.
- Window units were measured on Resolve 21 (`tests/drx_lab.py` re-measures them if a release changes them).
