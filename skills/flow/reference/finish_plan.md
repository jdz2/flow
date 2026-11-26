# finish_plan.json (Finish mode)

Write it next to the scan: `~/.flow/finish/<timeline-slug>/finish_plan.json`. Clip refs (`V1#3` = third
clip on V1) and note ids (`N4`) come from `finish_review.txt`. All times are **timeline seconds**.
`flow finish` applies the plan to a **duplicate** timeline.

```jsonc
{
  "timeline": "Halloween Gaywerk Aftermovie RC v5",   // source timeline (exact name)
  "name": "Halloween Gaywerk Aftermovie",             // optional; result is "<name> · Flow vN"
  "font": "Inter",                                    // default family for titles
  "title_styles": {"title": {"font_style": "Black", "size": 0.09}},   // optional per-kind overrides

  "look": {"look": "film", "intensity": 0.7},         // or {"lut": "/path.cube"}; null = no look
                                                      // footage clips without a grade get the Flow node tree with
                                                      // this LUT on 04 Look; clips that already carry a LUT or a
                                                      // multi-node grade are left alone ("replace_existing": true
                                                      // overrides). "nodes": false = plain LUT on node 1,
                                                      // "vignette": 0–0.8.
  "grade_clips": {"look": "nightlife", "tracks": [1], "vignette": 0.15,
                  "keep_green": ["V1#7"], "match": [["V3#1", "V1#71"]]},
                                                      // per-clip grading: every clip measured from its own frame,
                                                      // own look strength / warm-light fix; node tree with the clip's
                                                      // own conversion LUT kept on 01 Input and the per-clip look on
                                                      // 04 Look ("nodes": false = old single composed LUT)
                                                      // "soft": 0–1 = soft teal / soft orange skin: paler teal, gentler
                                                      // skin, lime/green/blue/purple light pulled down harder
  "stills": true,                                     // one reference still per graded clip into the Gallery album
                                                      // "Flow · <timeline>" (jpgs in stills/ next to the plan)
  "titles": [
    {"kind": "title", "text": "HALLOWEEN GAYWERK", "subtext": "Berlin · 31.10.2026", "at": 0.5, "duration": 3.5},
    {"kind": "lower_third", "text": "DJ Kasia", "subtext": "resident", "at": 65.0},
    {"kind": "end_card", "text": "See you next year"}
  ],
  "effects": [                                        // overlays over a time range (see effects.md)
    {"type": "letterbox", "from": 0, "to": 9999, "ratio": 2.39},
    {"type": "grain"},                                // no from/to = whole timeline
    {"type": "flash", "at": 42.0},
    {"type": "light_leak", "from": 12, "to": 14}
  ],
  "clips": [                                          // per-clip ops on the duplicate
    {"item": "V1#7", "speed": 50, "note": "N3"},      // slow-mo that keeps the clip's slot (music sync holds)
    {"item": "V1#12", "zoom": 1.15},
    {"item": "V1#3", "push_in": 1.1},
    {"item": "V1#9", "effects": ["glow", {"type": "shake", "at": 0.4, "duration": 0.3}]},
    {"item": "V1#14", "retime": true},                    // Optical Flow (+ Speed Warp) on an existing speed change
    {"item": "V3#1", "look": "cool", "intensity": 0.6}    // look on this clip only (skipped if node 1 already has a LUT)
  ],
  "placeholders": [
    {"item": "V3#1", "action": "text", "text": "HALLOWEEN GAYWERK", "kind": "title"},   // Text+ placeholder: fill + style
    {"item": "V2#2", "action": "title", "kind": "chapter", "text": "AFTER MIDNIGHT"},    // generator placeholder → title
    {"item": "V2#4", "action": "replace", "clip": "C0042.MP4", "src_in": 3.0}           // B-roll from the media pool
  ],
  "notes": [
    {"id": "N1", "status": "done", "action": "opening title with date"},
    {"id": "N3", "status": "done", "action": "50% slow-mo, slot kept"},
    {"id": "N6", "status": "needs_user", "reason": "asks for a drone shot; no drone footage in the media pool"}
  ]
}
```

Grading with a reference image and masks (rule-based Sky / Subject / Vignette windows and Skin / Greens /
Highlights qualifiers on a ten-node tree): see `masks.md`. Short form:

```jsonc
"grade_clips": {"reference": "/path/still.jpg", "masks": true, "subjects": {"V1#4": [0.6, 0.5]},
                "vignette": 0.22, "replace_existing": true},
"clips": [{"item": "V1#6", "stabilize": true}]       // Resolve's stabilizer on a shaky clip
```

Rules:
- Cover every note. `needs_user` must say what is missing.
- To speed up a clip and keep its slot, the clip needs enough unused media after its out point. The
  report says when there isn't enough.
- Don't restyle or move the user's existing clips beyond what a note asks.
