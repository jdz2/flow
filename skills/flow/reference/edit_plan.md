# edit_plan.json (Reel mode)

The plan lives at `~/.flow/jobs/<job>/edit_plan.json`. Start from `edit_plan.draft.json`, which already
lists the high-confidence removals and the detected emphasis words.

Times come in four kinds. Use the one that fits:

- **word indices** (`w37` in review.txt), for anything about speech
- `at_src` / `from_src` / `to_src`: **source seconds**, for production notes anchored to the original clip
- `at` / `from` / `to`: **output seconds**, on the finished reel
- `start` / `end`: source seconds, for `ramps` and `slowmo` only (they must cover non-speech moments)

```jsonc
{
  "version": 1,
  "style": "talking-head-plus",          // id or name: clean, talking-head, talking-head-plus, business, shorts, cinematic, or a custom id
  "font": "Inter",                       // or {"family": "Montserrat", "style": "ExtraBold"}
  "grade": {"look": "orange-teal", "intensity": 0.8, "balance": true},
                                         // {} = the style's look. look: natural|orange-teal|film|moody|warm|cool|bw|vintage,
                                         // or null = "Leave it" (no grade at all)
                                         // node tree (default): every clip gets 01 Input › 02 Balance › 03 Exposure ›
                                         // 04 Look › 05 Skin › 06 Vignette. Options: "nodes": false = one LUT on node 1,
                                         // "vignette": 0–0.8 (edge darkening on node 06), "stills": false = no Gallery stills
                                         // or {"lut": "/path/to/My.cube", "balance": true}
  "remove": [                            // inclusive word ranges; reason shows on the red marker
    {"words": [15, 17], "reason": "false start, redone in S03"}
  ],
  "restore": [],                         // ranges to keep even if auto-removed (e.g. a meaningful "um")
  "keep_fillers": false,                 // true keeps every um/uh
  "speed": null,                         // global speech speed, e.g. 1.08 (pitch-corrected)

  "emphasis": [14, 25, 41],              // highlight color, emphasis punch-ins, shake@emphasis
  "hook": {"text": "Edit reels in seconds", "duration": 2.5},   // null = style default, false = no hook
  "cold_open": {"words": [120, 131], "mode": "move"},          // optional; "copy" keeps it in place too

  "punch_ins": "auto",                   // "auto" (style rules), "none", or a list:
                                         // [{"at_word": 40, "zoom": 1.2, "until_word": 52}]
  "subject": {"x": 0.48, "y": 0.36},     // face position in the SOURCE frame (0..1 from the top left)

  "ramps":  [{"start": 22.6, "end": 24.6, "peak": 300}],   // speed ramp through a non-speech moment
  "slowmo": [{"start": 30.1, "end": 31.0, "speed": 50}],   // constant slow motion, non-speech, video only

  "split_edits": "auto",                 // J/L cuts at jump cuts: "auto" (style rules), "none", or a list:
                                         // [{"at_word": 40, "type": "J", "sec": 0.2}, {"at_word": 77, "type": "L"}]
                                         // J: the next sentence's sound starts under the previous picture
                                         // L: the previous sound runs on under the next picture
                                         // at_word = first word after the cut. The sound edit never moves;
                                         // the picture borrows silent source frames (never visible talking)

  "broll": [                             // B-roll from the indexed library (flow broll); "auto" = keyword match,
                                         // "none"/[] = no B-roll
    {"clip": "B3", "from_word": 40, "to_word": 52},        // covers exactly those words (+ a short tail)
    {"clip": "B1", "at_word": 61, "duration": 2.0, "src_in": 4.5, "speed": 50},   // slow-mo B-roll from 4.5 s
    {"clip": "B5", "at_word": 90, "duration": 2.5, "mode": "pip", "pos": [0.5, 0.28], "size": 0.86},
    {"clip": "B2", "from_word": 101, "to_word": 108, "audio": "nat", "j": 0.3, "l": 0.2, "nat_db": -20},
    {"clip": "R1"},                                        // AI restyle: covers its source window, cut like the edit
    {"clip": "R2", "src_range": [12.0, 15.5]},             // only part of a restyled window
    {"clip": "G1", "at_word": 120, "duration": 3.0}        // AI-generated shot: placed like any B-roll
  ],                                     // options: mode full|pip · motion push_in|pull_out|none · push 1.06
                                         // x/y focus (full) · fade frames · lead s · over_hook true · note
                                         // restyles: sync "source" (default) | "none" (place like B-roll)

  "effects": [                           // image effects + overlays, see effects.md
    {"type": "shake", "at_word": 77, "duration": 0.35},
    {"type": "glow", "from_word": 31, "to_word": 42},
    {"type": "letterbox", "from": 0, "to": 999, "bar": 0.09},
    {"type": "flash", "at_src": 12.4}
  ],
  "masks": [
    {"type": "spotlight", "from_word": 43, "to_word": 51, "size": 0.5, "strength": 0.6}
  ],
  "titles": [                            // the titles system (see effects.md)
    {"kind": "location", "text": "Berlin · Nov 2026", "at_src": 3.0, "duration": 2.5, "note": "N2"},
    {"kind": "lower_third", "text": "Jan Dz", "subtext": "Director", "at_word": 20},
    {"kind": "end_card", "text": "Follow for part 2"}
  ],

  "overrides": {                         // deep-merged into the style for this edit only
    "captions": {"size": 0.075, "position": [0.5, 0.36], "max_words": 2},
    "hook": {"box": {"color": "#FF3B3B", "opacity": 1}},
    "zoom": {"on_cuts": 1.1}
  },

  "notes": [                             // one record per production note N#
    {"id": "N1", "status": "done", "action": "removed the second intro take"},
    {"id": "N2", "status": "done", "action": "location title at 0:03"},
    {"id": "N3", "status": "needs_user", "reason": "asks for drone B-roll; none in this clip"}
  ],
  "notes_text": ""
}
```

## Judgment rules
- **Bad takes.** Remove a phrase that gets restarted, plus spoken retake cues. Accept candidates with
  conf ≥ 0.8 unless the repeat is intentional (rhetoric, a list). Check every 0.55–0.8 candidate in
  context.
- **Never cut a word in half.** Ranges are whole words. Silences between kept words are trimmed
  automatically by the style's `pacing.max_gap`.
- **Emphasis.** Pick the words a viewer should feel: numbers, outcomes, contrasts. Skip filler and
  function words.
- **Hook.** It must be true to the clip. No clickbait the video doesn't pay off.
- **Production notes override style defaults.** If a note asks for something Flow can't do, mark it
  `needs_user` and say why.
- **B-roll.** Match what is *said* to what a clip *shows*: "our new studio" → the studio shot. Cover
  1.2–3.5 s per insert, start on the word it illustrates, and end on a word boundary. Prefer windows
  that hide a jump cut (Flow counts those). Keep the speaker on screen for the hook, punchlines and
  direct asks ("follow", "comment"). Use roughly one insert every 4–6 s, never more than half the reel.
  Each clip once unless the user wants repeats. Use `src_in` to skip a shaky start or reach the
  moment you saw on the sheet. Use `speed` 40–60 only for 50/60 fps clips (`pip` when the whole frame
  matters, like a screen recording or product shot). Use `audio: "nat"` with `j` when the clip's own
  sound sells it (a crowd, a pour, an engine).
- **J/L cuts.** J at a new sentence or topic (the viewer hears what's next before seeing it), L
  when a reaction or the end of a thought should linger. 0.15–0.3 s is plenty for a talking head.
  Skip them on fast Shorts pacing.
- **Extras the user switched off** (hook, zooms, effects, ramps) stay off: set `"hook": false`,
  `"punch_ins": "none"` and empty lists, and override `effects.auto` / `masks.auto` to `[]`.
