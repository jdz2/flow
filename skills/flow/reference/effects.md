# Effects, masks, titles, looks

## Image effects (built in a Fusion comp on each affected clip)
| type | what it does | params |
|---|---|---|
| `shake` | handheld impact shake (rotation, scale and position jitter, decaying) | `intensity` (1.0) |
| `glow` | soft glow on highlights, fading in and out | `intensity` |
| `halation` | warm film halation around highlights | `intensity` |
| `rgb_split` | chromatic glitch: red and blue channels slide apart | `intensity` |
| `blur_in` | starts blurred, sharpens | `intensity` |
| `blur_bg` | blurs everything outside an ellipse around the subject (fake shallow focus) | `size` (0.6), `center` |
| `push_in` / `zoom` | smooth eased zoom | `from` (1.0), `to` (1.08–1.3), `center` |

## Overlays (Fusion clips on "Flow FX" tracks above the video)
| type | what it does | params |
|---|---|---|
| `flash` | white flash | `intensity` |
| `letterbox` | cinematic bars | `bar` (0.08–0.14 per side) or `ratio` (visible width/height, e.g. 2.39 on 16:9) |
| `vignette` | soft dark edges | `intensity` |
| `spotlight` (mask) | darkens everything outside an ellipse on the subject | `size` (0.5), `strength` (0.6), `center` |
| `highlight_box` | rounded outline box | `center`, `width`, `height`, `thickness` |
| `circle_reveal` | iris opens from black | `center` |
| `grain` | animated film grain (Overlay blend) | — |
| `light_leak` | warm drifting light leak (Screen blend) | — |

`center` is in Fusion space: x 0..1 from the left, **y 0..1 from the bottom**. Flow fills it in from
`subject` when you leave it out.

Style auto rules (in a style's `effects.auto` / `masks.auto`): `shake@emphasis` (on emphasised words,
capped by `effects.max_per_minute`), `flash@hook_end` (where the hook title ends), `blur_in@start` (the
first frames), and `<type>@all` (the whole timeline, e.g. `grain@all`, `letterbox@all`, `vignette@all`).
`effects.intensity` scales every auto effect. No built-in style uses flash or letterbox any more; add
them in a custom style or per edit. Letterbox on a 9:16 reel throws away a lot of a narrow frame.

## Speed
- `ramps`: bell-shaped speed ramp (100% → peak → 100%) through a non-speech moment, with optical flow
  (plus Speed Warp on Studio).
- `slowmo`: constant slow motion (5–99%) over a non-speech moment.
- `speed`: global talking speed (e.g. 1.04) with pitch correction. Above ~1.05 voices start to sound
  processed and lips judder.
- Cinematic style: `ramps.auto` ramps through natural pauses ≥ 1 s instead of cutting them.

## Titles system
`titles: [{kind, text, subtext?, at|at_word|at_src, duration?, font?, font_style?, size?, color?, position?, animation?, box?, align?}]`

| kind | default look |
|---|---|
| `title` | big, uppercase, centered, tracking-in animation |
| `chapter` | medium, uppercase, centered, fade |
| `lower_third` | left-aligned name, subtext role, dark box, slide-in |
| `location` | small uppercase near the top, tracking-in |
| `end_card` | centered, last seconds (default anchor is the end) |
| `quote` | medium sentence case, centered, fade |

Animations: `pop`, `pop_strong`, `fade`, `slide`, `tracking`, `none`. Positions are Fusion space:
`[x, y]` with y measured from the bottom.

## Looks (`grade.look`)
`natural`, `orange-teal`, `film`, `moody`, `warm`, `cool`, `bw`, `vintage`, or `{"lut": "/path.cube"}`.
`intensity` runs 0–1.5. `balance: true` adds an automatic exposure, white-balance and contrast fix
(ASC CDL on node 1, so it stays editable in the Color page). The look itself is a LUT on node 1,
written to `LUT/Flow/Jobs/`. All looks are also installed as `LUT/Flow/Flow <Look>.cube` for manual use.

## Not possible through Resolve's API (tell the user)
Magic Mask / text-behind-subject, beat-synced cutting, generating music or footage, Fusion page
templates that aren't built from scripted nodes.
