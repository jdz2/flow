# Production notes — how to leave instructions for Flow

Flow reads everything below with `flow notes` (Reel mode) or `flow finish-scan` (Finish mode). Each
instruction becomes a note N1, N2, … with a time anchor and a guessed kind. Claude interprets the
text, completes the note, and reports it as ✓ done, skipped, or "Flow needs you".

## 1. Markers
Put a marker on the timeline, on a clip, or on the source clip in the media pool. Write the instruction
in the marker's **name or note**. Duration markers mark a range. Examples:
- `title: HALLOWEEN GAYWERK 2026` · `lower third: DJ Kasia — resident`
- `slow-mo here` · `speed ramp` · `punch in` · `shake on the drop`
- `cut this` · `bad take` · `keep this joke`
- `cinematic look` · `film grain` · `letterbox from here`
- `spotlight on her` · `blur the background`
- `title: something catchy about the night` (Flow writes the text)

If a marker has no keyword, its color is a hint:
Red = cut · Green = keep · Blue = title/text · Purple = cinematic/speed/effect · Yellow = hook · Cyan = zoom ·
Pink = look/grade · Fuchsia = mask · Lavender = B-roll.

## 2. Placeholders
- A **Text+** title whose text is a placeholder (`TITLE`, `[guest name]`, `TBD`, `TODO: …`, `XXX`,
  `Your title`, `Flow: …`). Flow writes the real text, styled.
- Any clip or generator (e.g. a Solid Color) **renamed** `Placeholder …`, `PH: …`, `TODO: …`, `Flow: …`,
  or `B-roll: crowd shot`. Flow turns it into a title, or fills it with the best matching unused
  media-pool clip (matched by clip name, keywords, description and comments) on a "Flow B-roll" track,
  then disables the placeholder.

## 3. Clip metadata
The Description, Comments and Keywords fields of the source clip (Metadata panel) are read as global
notes, for example "keep under 30 s, energetic".

## 4. A notes file
Pass `--file notes.md`, or put a sidecar next to the clip (`<clip>.notes.md`, `notes.md`,
`production notes.md`, `shotlist.md`). One instruction per line, optionally starting with a time or a
range:
```
0:00 title: HALLOWEEN GAYWERK — Berlin 2026
0:12-0:18 slow-mo on the confetti
1:05 lower third: DJ Kasia
cinematic look, film grain, no captions
end card: see you next year
```
In Reel mode, times are **source clip** times. In Finish mode, they are **timeline** times.
