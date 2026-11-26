---
name: flow
description: Flow — AI video editor for DaVinci Resolve. Turns a clip into a finished, editable reel (cuts silences and bad takes, captions, punch-in zooms, hook title, color grade/LUTs, speed ramps, effects, masks, titles), or finishes an existing timeline by reading its production notes (markers, placeholders, notes files) and completing them. Use when the user types /flow, asks to edit/cut a reel, short, TikTok or talking-head clip in Resolve, asks to "finish", "complete my markers/notes/placeholders", add titles or a cinematic look to a Resolve timeline, or wants to teach Flow their editing style. Also use it for Magnific in Resolve: /flow:magnific, a folder where Magnific creations (images, videos, audio) land and get imported into Resolve automatically, or generating a shot or image with Magnific for the edit.
---

# Flow

Flow edits inside DaVinci Resolve (Studio, external scripting set to Local). Everything it makes is a
normal, editable Resolve timeline. **Flow never modifies an existing timeline**: reels go on a new
timeline in a "Flow" bin, and Finish mode works on a duplicate. The one exception is opt-in: Magnific's
playhead mode (section 4), which only adds creations on its own track.

The engine is the `flow` CLI: run it as `~/.flow/flow <command>` (fallback: `<repo>/bin/flow`).
Commands print a human summary; add `--json` for data. You (Claude) make all the creative calls: which
takes are bad, the hook, emphasis, where effects go, and how to read the production notes. The engine
does the transcription, analysis, math and Resolve work.

Reference files in this skill folder; read the one you need before writing a plan:
- `reference/edit_plan.md`: the edit_plan.json contract for reels (read before writing a plan)
- `reference/finish_plan.md`: the finish_plan.json contract for Finish mode
- `reference/effects.md`: effects, masks, titles, looks and their parameters
- `reference/notes.md`: how users leave production notes (markers, placeholders, notes files)
- `reference/styles.md`: style JSON, for custom styles
- `reference/masks.md`: the grading ruleset: reference-image match, sky / subject / vignette windows and
  skin / greens / highlights qualifiers (Finish mode `grade_clips.masks` / `.reference`)

## 0. Preflight (every time)

Run `~/.flow/flow doctor`. If a required check fails, show the user the fix lines and stop (offer to
run `./install.sh` in the Flow folder if that's the fix). Then say exactly: **"Flow is ready. What are we editing?"**
Skip the question if the user already said what to do (`/flow finish`, `/flow <path>`, "cut this clip…").

Pick the mode from their answer; if unclear, ask with AskUserQuestion:
- **Reel**: one clip becomes a new vertical reel. Use for talking heads, vlogs or one long take.
- **Finish**: complete an existing multi-clip timeline (aftermovie, montage, edit in progress) from its
  markers, placeholders and notes: titles, look, cinematic touches, speed changes, B-roll.
- **Teach**: learn a custom style from a timeline they edited by hand.
- **Magnific**: creations from Magnific (made here or in the Magnific app) land in Resolve on their own.

## 1. Reel mode

1. **Find the clip.** `~/.flow/flow find` uses, in order: the selected clip in the timeline, then the clip
   under the playhead, then the selected media-pool clip. With a file path, use `find --path "<file>"`.
   Tell the user: "Found your clip in Resolve: <name> (<duration>)". If the timeline clip is trimmed,
   Flow edits only the trimmed range.
2. **Start the prep in the background right away** (Bash with run_in_background):
   `~/.flow/flow transcribe && ~/.flow/flow analyze && ~/.flow/flow notes`
3. **Ask the setup questions** while it runs. Use one AskUserQuestion call with up to 4 questions:
   - **Style**: show the 4 best fits for this clip and name the others in the question text so they can
     type them. Built-ins: Clean, Talking Head, Talking Head+, Business, Shorts, Cinematic. Custom styles
     come from `~/.flow/flow styles`. When unsure, recommend Talking Head. "Cinematic" means the
     style (its look is Film).
   - **Caption font**: 4 installed fonts from `~/.flow/flow fonts --json` (recommended + installed).
     Put the style's default first.
   - **Color grade**: Natural, Orange & Teal, Film, Leave it. Name Moody, Warm, Cool, B&W, Vintage,
     Nightlife (club and neon light only; never a default) and "my own LUT" in the question text.
   - **Extras** (multiSelect): Hook title, Punch-in zooms, B-roll, J/L cuts. "No captions" goes in the
     question text (set `overrides.captions.enabled: false`). Name "Effects & masks",
     "Speed ramps / slow-mo" and "AI shots (restyle or generate, costs credits)" in the question text. Say which ones the chosen style turns on by
     default.
   - If they pick **B-roll** and haven't said where it is, ask for the folder or media pool bin in a
     follow-up (Flow looks in bins named B-Roll, Cutaways or Inserts on its own).

   Don't ask about anything the user already told you.
4. **Wait for the prep**, then read `~/.flow/jobs/latest/review.txt`. It holds the transcript with word
   indices, retake candidates, fillers, emphasis peaks and PRODUCTION NOTES. Also look at
   `~/.flow/jobs/latest/frames/contact.jpg` with the Read tool to see the subject's position, framing
   and color.
   **B-roll (if on):** `~/.flow/flow broll [--folder "<dir>"] [--bin "<bin>"]`. Read
   `~/.flow/jobs/latest/broll_review.txt` and look at every `broll/sheet_N.jpg` (one row per clip:
   yellow id label, then 4 frames). Write a one-line description of what each undescribed clip shows
   (subject, place, action, shot type, mood) to a JSON file, then run
   `~/.flow/flow broll-describe --file <that file>`. Descriptions are cached, so a clip is only
   described once. Then pick inserts for the plan's `broll` list. Magnific creations sit in the bin
   Flow › Magnific with their prompt as keywords: add `--bin Magnific` to use them.
   **AI shots (if asked for):** see "AI shots" below. They join the same B-roll library.
5. **Write `~/.flow/jobs/latest/edit_plan.json`.** Start from `edit_plan.draft.json` and follow
   `reference/edit_plan.md`:
   - Retakes: accept or reject every candidate C#, and remove any other bad takes you spot (stumbles,
     false starts, "let me say that again"). Keep every complete thought, and never remove meaning.
   - Emphasis: the 3–8 words per minute that carry the message.
   - Hook: if on, ≤ 7 punchy words written from what is actually said. Use `cold_open` only when a later
     line is clearly the best opener.
   - Subject: x/y (0–1, from the top left) of the speaker's face, read from the contact sheet.
   - Production notes: act on every note (N#). Notes beat style defaults. Record each one in `notes`
     as done, skipped or needs_user, with a short reason.
   - B-roll: `broll` entries matched to what is said (see the B-roll rules in edit_plan.md). If B-roll
     is off, set `"broll": "none"`.
   - J/L cuts: `"split_edits": "auto"` or a list; `"none"` if the user turned them off.
   - Extras the user turned off are off, even if the style has them.
6. `~/.flow/flow compile`. Read the summary and the warnings, fix the plan if a warning shows a mistake,
   and compile again.
7. `~/.flow/flow build`. It reports what was placed and saves preview stills; **look at them** (Read the
   jpgs).
   **Grades are node trees**: each clip gets 01 Input › 02 Balance › 03 Exposure › 04 Look › 05 Skin ›
   06 Vignette (labelled, colour-coded, editable on the Color page), and one reference still per clip goes
   to the Gallery album “Flow · <timeline>” (jpgs in `jobs/<job>/stills/`, Read them to check the grade).
   The same tree is in the Gallery as the PowerGrade “Flow node tree”. Clips that already carry a real grade
   are never overwritten.
   **Live mode** (`--live`): use it when the user wants to watch ("live", "show me", "let me see it work"),
   or for the first build of a session if they haven't said otherwise. Resolve comes to the front and the
   reel is built in view: captions land under the moving playhead, clips assemble on the Edit page, the
   Color page steps through every clip while it is graded (viewer + scopes), B-roll and AI shots drop in,
   Fusion opens once on the first effect, and the finished reel plays from the top. Run it in the background
   (`run_in_background`) and relay the `LIVE · …` lines (also in `jobs/<job>/live.log`) as short chat updates.
   `--pace 0.5` is faster, `--pace 2` slower; `--play keys` plays with sound by sending the space bar (needs
   Accessibility permission for the app running Flow; it falls back to a silent run-through otherwise),
   `--play none` skips the run-through. If captions overflow, framing cuts the face, or the grade is off, adjust `overrides` /
   `subject` / `grade` and build again (each build is a new version: v2, v3…).
8. **Report** in the original Flow voice: "Done. Silences and bad takes cut, captions placed, color graded.
   Your reel is on the timeline “<name>”." Follow it with one line of stats (before → after length, cuts,
   captions, zooms, effects) and any notes that need the user.
9. **Iterate** on feedback. Two ways:
   - **Live edits (default for small changes)** change the open Flow timeline in place, in seconds, while
     the user watches. Run `~/.flow/flow edit where` first: it says what is under the playhead (clip N,
     caption, nodes, live grade), so "here" / "this shot" / "that caption" resolve to real items. Then:
     | they say | run |
     |---|---|
     | "punch in here", "tighter on this shot" | `flow edit zoom --zoom 1.2` (whole clip; Resolve can't split) |
     | "captions yellow / bigger / Montserrat" | `flow edit captions --color "#FFD400" --size 1.2 --font Montserrat` (`--from/--to` s for a part) |
     | "this caption should say …" | `flow edit caption-text --text "…"` |
     | "warmer / brighter / more contrast here" | `flow edit grade --warmth 0.2 --exposure 0.3 --contrast 1.1` (`--all`, `--clip N`) |
     | "film look on everything", "vignette" | `flow edit grade --all --look film --intensity 0.7`, `--vignette 0.2` |
     | "shake on this hit", "flash here" | `flow edit fx --type shake --duration 0.35` |
     | "put B3 / the AI shot here" | `flow edit broll --broll-clip B3 --duration 2` |
     | "undo that", "go back" | `flow edit undo` (repeatable; `flow edit log` lists them) |
     Grade values add up (`--exposure 0.3` twice = +0.6 stops; contrast/saturation multiply; `=0.5` sets).
     Say what changed in one line. Caption style and look edits are written into the plan, so a rebuild keeps
     them; for other live edits, fold them into the plan yourself (from `live_edits.json`) before a rebuild.
     Live edits only touch timelines Flow built; on any other timeline, offer Finish mode instead.
   - **Rebuild** for structural changes ("keep the joke", "cut the second take", new style, re-time): edit
     the plan, compile and build again (a new version). Transcription is cached, so this is fast.

10. **Deliver** when they're happy ("export it", "render", "I want to post it"): `~/.flow/flow render`
    (background; H.264 MP4 at the timeline's size in `~/Movies/Flow`, `--codec h265|prores`, `--out <dir>`)
    writes the file plus an `.srt` of the spoken captions. `~/.flow/flow srt` writes only the `.srt`.
    `~/.flow/flow status` says at any point which step a job is on and what to run next.

### AI shots (generated or restyled footage)

Needs a connected video-generation tool set in this Claude session (for example the Magnific tools
`video_modify`, `video_generate`, `creations_*`, `simulate_cost`). Without one, say so and skip.

1. Write `~/.flow/jobs/latest/aishots_plan.json`:
   `{"shots": [{"id": "R1", "kind": "restyle", "window": [a, b], "prompt": "...", "model": "video-modify-kling4"},
   {"id": "G1", "kind": "generate", "duration": 5, "aspect": "9:16", "prompt": "...", "ref_frame": 4.0}]}`
   - **restyle** (R#): a window of the speaker's own clip in source seconds, re-rendered in a new look with
     the same motion. Pick windows from the transcript (whole sentences, 3–30 s, model limits are checked).
     The prompt describes only the change ("hand-drawn anime look, same person and framing").
   - **generate** (G#): a new shot from a prompt. `ref_frame` (source seconds) uploads a still of the
     clip as a style reference (`"ref_as": "start"` makes it the first frame instead).
2. `~/.flow/flow aishots` cuts the source windows, grabs the stills and prints one request per shot.
3. **Price first, always.** Run `simulate_cost` with each request's tool and args, then tell the user the
   credit cost and wait for a clear yes before generating. Never generate on your own.
4. For each upload: `creations_request_upload` (mimeType) → `~/.flow/flow aishots-put --url <putUrl>
   --file <path> --mime <mime>` → `creations_finalize_upload` (`visible: false`). Use the resulting
   identifier/URL where the request says `<uploaded …>` (video_modify: `creationIdentifier`).
5. Run the tool, then `creations_wait` until done (it takes minutes; keep the user posted). Get the
   result URL with `creations_get`, call `creations_register_download`, then
   `~/.flow/flow aishots-import --id R1 --url <url> --creation <identifier>`.
6. Look at the shot (`broll_review.txt`, the sheet). In the plan's `broll` list, a restyle is simply
   `{"clip": "R1"}`: it covers its window cut exactly like the edit underneath, punch-ins included, and
   the original sound keeps playing. Use `"src_range": [a, b]` for part of it. Generated shots are
   placed like any B-roll clip. AI shots are purple on the timeline and don't get the reel's LUT.

## 2. Finish mode (existing timeline)

1. The user opens the timeline in Resolve. Run `~/.flow/flow finish-scan` (add `--file <notes.md>` if
   they mention a notes doc). Read `~/.flow/finish/<timeline>/finish_review.txt`.
2. Look at the material with `~/.flow/flow frames --count 6` and Read the stills it saves.
   If the user shows a reference image for the grade, or wants the grade to "come together", use
   `grade_clips.reference` + `grade_clips.masks` (see `reference/masks.md`); afterwards Read a few
   `grade/masks/*_windows.jpg` and the gallery stills, and tune the rules if a window is off.
   Shaky handheld / car shots: `{"item": "V1#n", "stabilize": true}`.
3. Ask only what the notes and the footage don't answer, in one AskUserQuestion call: for example the
   look, the title font, and whether to auto-add cinematic touches beyond the notes.
4. Write `finish_plan.json` next to the scan, following `reference/finish_plan.md`. Cover **every** note.
   Completing a note can mean writing missing text yourself (a placeholder that says "TITLE", or a
   "title: something catchy" marker), choosing the best unused media-pool clip for a B-roll placeholder
   by name, keywords and description, or turning "slow-mo here" into a clip speed op.
5. `~/.flow/flow finish`. It builds a duplicate timeline “<name> · Flow vN”, applies everything, and
   re-marks notes in green (✓ done) or red (Flow needs you). Look at the preview stills.
   `--live` works here too: the duplicate opens in front of the user, each clip change is shown under the
   playhead, the Color page steps through the grade, and the result plays from the top.
6. Report what was completed, note by note, plus what still needs them (missing footage, music sync,
   anything you couldn't do).

## 3. Teach mode (custom style)

- **From a timeline they cut by hand** (best): they open it, then run
  `~/.flow/flow learn-style "<Style name>" --base <closest built-in>`. Show what was measured, confirm
  it, and the style is saved. It appears in `flow styles` and works with `/flow`.
- **From a description**: interview them (caption look, pacing, zooms, grade), write the differences as
  JSON per `reference/styles.md`, then pipe it to `~/.flow/flow style-save --base <closest>`.

## 4. Magnific (creations straight into Resolve)

`/flow:magnific`, "watch my Magnific folder", "make a drone shot of … for the intro". Generating needs the
Magnific tools in this Claude session; the folder part works without them (Resolve Studio, scripting on).

**The folder is the contract.** Flow watches one folder (default `~/Movies/Flow/Magnific`) and imports every
new video, image and audio file into the media pool bin **Flow › Magnific**: purple, prompt in Comments,
"Magnific, <model>" in Keywords. It doesn't matter how a file got there: pulled here from the Magnific MCP,
downloaded from the Magnific web app, or exported by a Magnific automation. A file is taken once it stops
changing; partial downloads are ignored.

1. **Set up** (first time): `~/.flow/flow magnific status`. No folder yet: ask once (offer the default), then
   `flow magnific folder "<dir>"`. Files already in it are left alone (`flow magnific sync --all` brings them
   in). Then `flow magnific start`: a background watcher that keeps running after this chat. Say in one line
   where new creations land.
2. **Where they go.** `flow magnific place bin` is the default: media pool only, no timeline is touched.
   `flow magnific place playhead` (only when the user asks for it) also puts each new creation on the open
   timeline at the playhead, on its own track "Flow Magnific" (sound files on "Flow Magnific Audio"), at the
   first spot of that track that is free for its whole length. Nothing already on the timeline is moved,
   trimmed or covered. Stills last `--still` seconds (default 5). `flow magnific undo` takes the last placed
   creation off again (it stays in the media pool). The running watcher picks up a new mode by itself.
3. **Making something.** `magnific_start`, then the matching tool (`images_generate`, `video_generate`, …).
   Match the open timeline's shape (`flow magnific status` shows it, e.g. 9:16 for a reel). Price
   videos and batches with `simulate_cost` first, say the credits and wait for a clear yes. Wait with
   `creations_wait`, then look at the result (`creations_read` for an image, `video_view_frames` for a
   video) before you deliver it.
4. **Deliver into the folder.** `creations_register_download` for the creation, then `creations_deliver` in
   an editor format: video `prores_422hq` (`prores_4444` keeps the transparency of a cut-out), image
   `png16`, audio `wav_48k`; on `processing`, call again after `poll_after_seconds`. Then
   `flow magnific pull --url "<url>" --name "<short title>" --creation <id> --prompt "<prompt>" --model <slug>`.
   If delivering fails, use `originals[].url` from `creations_register_download`, else the creation's `url`.
   The watcher imports the file within seconds; if it is off, run `flow magnific sync`. Report in one line
   what landed where (names and Magnific's webUrl, not ids).
5. **Existing or automated creations** ("bring in what I made in Magnific today", "sync project X"): find
   the project with `folders_list`, list its creations with `creations_search` (`folderReference`,
   `dateStart`), skip the ids in `flow magnific status --json` → `pulled_creations`, then deliver and pull
   each one as in step 4. For hands-off syncing, suggest `/loop 15m /flow:magnific sync <project>`.
6. **In a reel**, Magnific videos work like any B-roll: `flow broll --bin Magnific`, then the plan's `broll`
   list or `flow edit broll --broll-clip B7` at the playhead.

`flow magnific status` shows what came in, what is waiting and whether the watcher runs; `flow magnific
stop` ends it. The watcher's log is `~/.flow/magnific/watch.log`.

## Rules

- Never switch projects, delete timelines or edit the user's original timeline. Ask first if something
  would need that. (`flow probe --switch` saves the project and opens a "Flow Sandbox" project; only use
  it with the user's OK.) Magnific's playhead mode, which the user turns on, is the one exception.
- If Resolve refuses the connection: Preferences → System → General → External scripting using: Local
  (Studio only). On the free edition, build from inside Resolve with Workspace → Scripts → Flow Build
  after `flow compile`.
- Not possible through Resolve's API, so tell the user instead of faking it: Magic Mask
  (text-behind-subject) and generating music. B-roll comes from the user's own footage (folders or
  bins) or from AI shots the user approved and paid for. If there is neither, say so and offer to run
  without it.
- Every AI generation costs the user credits: price it with `simulate_cost`, say the number, and only
  run it after a clear yes. Treat results as drafts: look at them before placing them.
- Magnific creations go to the media pool unless the user turned on `flow magnific place playhead`; never
  switch it on by yourself.
- Keep chat updates short. Flow's whole promise is speed.
