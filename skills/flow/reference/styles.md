# Style JSON

Built-in styles live in `<repo>/styles/*.json`; custom styles live in `~/.flow/styles/*.json`. A custom
style usually only lists its differences and sets `"extends": "<built-in id>"`.
`flow style-save --base <id>` sets that for you.

```jsonc
{
  "name": "Jan Loud", "description": "My fast talking-head style", "extends": "talking-head-plus",
  "pacing":   {"max_gap": 0.15, "pad_in": 0.04, "pad_out": 0.07, "remove_fillers": true, "speed": 1.05},
  "framing":  {"width": 1080, "height": 1920, "reframe": "subject"},     // subject | smart (Studio) | center
  "captions": {"enabled": true, "max_words": 2, "max_chars": 16, "case": "upper", "strip_punctuation": true,
               "font": "Montserrat", "font_style": "ExtraBold", "size": 0.08, "color": "#FFFFFF",
               "accent": "#3DFF7A", "highlight_emphasis": true,
               "outline": {"color": "#000000", "width": 0.08},
               "shadow": {"color": "#000000", "opacity": 0.5, "offset": [0, -0.006], "softness": 0.02},
               "box": null, "position": [0.5, 0.32], "animation": "pop_strong", "min_frames": 6, "hold": 0.25},
  "hook":     {"enabled": true, "duration": 2.5, "font_style": "Black", "size": 0.07, "color": "#000000",
               "box": {"color": "#FFFFFF", "opacity": 1}, "position": [0.5, 0.74], "animation": "pop"},
  "titles":   {"title": {"font_style": "Black"}, "lower_third": {"box": {"color": "#111111", "opacity": 0.7}}},
  "zoom":     {"mode": "auto", "on_cuts": 1.15, "on_emphasis": 1.28, "every": 3.5, "max": 1.35},
  "ramps":    {"auto": false, "min_gap": 1.0, "peak": 300, "steps": 5},
  "effects":  {"auto": ["shake@emphasis", "flash@hook_end"], "max_per_minute": 8, "intensity": 1.0},
  "masks":    {"auto": []},
  "transitions": {"type": "none"},                                      // none | smooth_cut | dissolve
  "grade":    {"look": "orange-teal", "intensity": 0.7, "balance": true},   // or "lut": "/path.cube"
  "audio":    {"normalize": true, "target_lufs": -14, "voice_isolation": false}
}
```
Positions are Fusion space: `[x, y]`, 0..1, with y measured from the bottom.

Optional sections (defaults apply when missing):

```jsonc
"split_edits": {"mode": "auto", "type": "J", "max": 0.2, "min_frames": 3, "mid_sentence": "none"},
              // mode auto|none · type J|L|alternate (at sentence starts) · mid_sentence none|J|L
"broll": {"mode": "full", "motion": "push_in", "push": 1.06, "min": 1.0, "max": 3.5, "every": 5.0,
          "max_coverage": 0.4, "avoid_hook": true, "audio": "mute", "nat_db": -20, "fade": 0,
          "pip_size": 0.86, "pip_pos": [0.5, 0.3]}
```
