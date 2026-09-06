# Expansion ideas

Scratchpad for ideas about what else `ltxq` could do. Nothing here is a
commitment — ideas land as raw sketches, get battered around, and when one is
actually implemented it graduates to [features.md](features.md) and the
[CHANGELOG](CHANGELOG.md), with its section here marked **Done**.

---

## Idea 1 — Manifest-driven audio-segment batch composition

**Status: Done (v1)** — shipped as `ltxq add-batch <segment-dir>` (audio mode;
2026-09-06): manifest-or-ffprobe frame resolution, sidecar prompts, grid
policy, per-job config numFrames, shared `create_job()` path, plus the
dashboard's audio-batch form (`/api/add-batch`, API 1.6 — folder picker/drop
or a server-side path). Not implemented: reassembly (Idea 4 territory).

### The scenario

An audio file has been pre-processed for LTX generation compliance by an
external cutting helper: cut into N segments frame-locked to the `numFrames`
grid (8n+1 frames at 25 fps — the LTX-2 constraint), each exported as a wav,
plus an optional manifest txt describing them. Today each segment needs a
separate manual `add`, with `numFrames` hand-edited in the config JSON per
segment. The idea: **compose/queue one job per segment automatically**, each
with its `config.json` `numFrames` set for that segment and its own segment
wav attached as `--audio` conditioning.

### Input contract

A segment directory containing:

- `Name_0001.wav … Name_NNNN.wav` — the segment files
- optionally a manifest next to them, as the helper writes it:

```
# 25 FPS Audio Segment Frame Length Manifest
# Original File: Banderos Cats-2.mp3
# Total Segments: 8
# Format: [Filename] -> [Frames at 25 FPS] (Duration, 8n+1 Status)

Banderos Cats-2_0001.wav: 217 frames (8.680s, 8n+1 (n=27))
Banderos Cats-2_0002.wav: 209 frames (8.360s, 8n+1 (n=26))
...
Banderos Cats-2_0008.wav: 181 frames (7.240s, Non-8n+1)

# Raw Frame Counts (one integer per line):
217
209
193
153
193
177
177
181
```

- optionally per-segment prompt sidecars: same basename as the wav with a
  `.txt` extension (`Name_0001.txt` …). The sidecar **is** that segment's
  prompt — spoken text changes per segment, so this is the primary prompt
  source, not an override footnote.

### Frames: where each job's numFrames comes from

1. **Manifest present** → parse the per-file lines (`<file>: <N> frames`) and
   take N **verbatim** as that job's `numFrames`. The helper owns grid
   compliance; the composer never re-derives from the manifest. Cross-checks,
   all refusing the batch on mismatch: number of per-file lines == number of
   wavs in the dir == `Total Segments` header; the Raw Frame Counts block
   must equal the per-file values in the same order; manifest header fps
   (e.g. `# 25 FPS …`) must equal the template config's `fps`.
2. **No manifest** → the wav is self-describing: ffprobe each segment,
   `frames = duration × template fps`, snapped onto the 8n+1 grid per the
   straggler policy below (default up + silence pad), and the wav is fitted
   locally with ffmpeg to `frames / fps` seconds into the job dir.
   (ffmpeg/ffprobe are already hard dependencies — chain extraction and the
   merge tray use them.)

**Non-8n+1 stragglers** (the helper can emit them — see `_0008` above):
default round **up** to the grid (181 → 185) and pad the wav with silence to
match (7.24 s → 7.40 s), noting it in the job record — the spoken content is
never cut, the model just holds ~0.16 s longer. `--on-non-grid round-down`
trims the tail instead (never invents duration, only loses ~0.16 s);
`--on-non-grid refuse` aborts the batch for anyone who wants the helper's
output strict.

### Prompts

Per segment: the sidecar `Name_NNNN.txt` if present, else the shared
`--prompt-file`. A segment with neither fails validation (every job needs a
prompt). Job `name` is derived from the **segment basename**, not the prompt
prefix — with per-segment prompts the usual prompt-derived name would make
the dashboard unnavigable.

### What the composer does

- A new command, e.g. `ltxq.py add-batch <segment-dir>` (plus — later — a
  bulk mode on the web form), taking the same shared arguments as `add`
  (model, `--config-file`/`--config-json` template, `--host`, `--seed`,
  `--ext`, `--backend`, shared `--prompt-file`, …).
- **Validate the whole batch up front, all-or-nothing** (counts, fps guard,
  grid policy, prompt coverage), reporting a per-item error list — a half
  batch is worse than none, and nothing dispatches until the engine runs
  anyway, so failing fast costs nothing.
- Per segment, one job where:
  - `config.json` is the template with `numFrames` set (config-level, not the
    engine `--frames` override — config is what `regen` reuses, what
    `metadata.json` duration comes from, and what the UI displays; the
    `--frames` override stays free for manual one-offs);
  - `prompt.txt` is the sidecar or the shared prompt;
  - `--audio` carries that segment's wav (silence-padded or trimmed per the
    grid policy);
  - `batch` label defaults to the `Original File` stem from the manifest
    header ("Banderos Cats-2"), else the directory name — the existing 📦
    badge and chain-scoping machinery apply for free.
- **Seed**: `--seed` applies to all segments; absent → random per segment
  (current `add` behavior, i.e. variety by default).
- Jobs are independent of each other, so multi-host dispatch "just works";
  only any final reassembly step would need to wait for the whole batch.

### Implementation shape (shared with the other batch ideas)

The loop body is `add`'s creation path — extract it into a `create_job()`
helper (used by `add`, the web API, and every batch mode), then each batch
mode is a **planner** that produces a list of
`{assets, prompt, num_frames, name}` items followed by one shared queueing
loop. That planner→queue split is what later batch ideas (keyframe pairs,
LLM prompt factory) plug into: their manifests are just serialized planner
output.

### Natural companion: reassembly pass

When the last job of a batch finishes, concatenate the finished segment
videos in segment order (ffmpeg concat; stream-copy when codecs match,
re-encode otherwise) into the full clip under `movies_dir/<batch>/`. This
closes the loop — one song in, one finished music video out — and could be a
"batch completion hook" rather than a queue job. Pairs naturally with a
**regen-one-segment** flow: re-run segment k, re-concat.

### Open questions

- Does the cutting/manifest step itself ever move into ltxq (see Idea 3), or
  does that stay an external tool feeding `add-batch`?

Resolved while shipping:

- CLI vs web form — both: `add-batch` shipped with the dashboard's audio
  batch form (`/api/add-batch`) in the same change.
- Visual continuity across segments — yes, shipped as `--chain` (and a
  checkbox on the form): segments 2..N get the previous segment's last frame
  attached as `--image` (not `--first-frame` — a lone first frame is invalid,
  it only pairs with `--last-frame`). To make that deterministic under
  parallel hosts, batch-chained jobs defer dispatch while a batch mate
  is still active, so a chained batch self-serializes (without `--chain`,
  segments stay independent and dispatch in parallel as before).

---

## Idea 2 — Keyframe-pair batch from a folder of stills

**Status: sketch**

### The scenario

A folder of zero-padded stills:

```
file-0001.png
file-0002.png
file-0003.png
...
```

The wanted automation composes one job per **consecutive pair** — job k
interpolates from still k to still k+1:

- job 1: `first_frame = file-0001`, `last_frame = file-0002`
- job 2: `first_frame = file-0002`, `last_frame = file-0003`
- … N stills → N−1 jobs.

This is the storyboard/keyframe-interpolation batch: every joint of the
sequence is pinned to a *given* still, not to a previous generation's output.

### Why this differs from the existing ⛓ chain feature

Chain continuation resolves the *previous finished render's* last frame at
launch time and is inherently serial (and racy with parallel hosts). Here all
inputs are known before anything is queued, so the whole batch is independent
jobs that can dispatch **in parallel across hosts**. The shared-still trick
gives continuity for free: still k is the last frame of job k−1 *and* the
first frame of job k, so any two adjacent renders join exactly at that
keyframe when concatenated.

### What the composer would do

- A new command, e.g. `ltxq.py add-pairs <dir>` (working name — could also be
  a `--mode` of a unified `add-batch`), taking the same shared arguments as
  `add` (model, prompt/prompt-file, `--config-file` template, backend,
  seed, …).
- Natural-sort the stills by their embedded number (no contiguity required;
  ignore hidden/non-image files), then queue N−1 jobs with
  `first_frame` / `last_frame` slots from the sorted list.
- `numFrames` comes from the config template (same for every pair → uniform
  pacing), optionally overridden by e.g. `--frames-per-pair`.
- `batch` label defaults to the folder name → 📦 grouping, per-job regen.

### Natural companion: reassembly

Same concat pass as Idea 1: when the batch finishes, ffmpeg-concat the job
outputs in pair order into one continuous clip — every seam is the shared
still, so the result reads as one camera move through the storyboard. A
regen-one-pair-and-replace flow fits identically (see backlog, Idea 4).

### Decisions to make

1. **Pairs vs triplets.** Default is stride-1 pairs. The `middle_frame` slot
   exists, so an alternative composition is stride-2 triplets
   (first = 2k−1, middle = 2k, last = 2k+1 → half the jobs, twice the motion
   per job). The CLI even accepts raw weighted keyframes via
   `--extra-arg "--keyframe kf.png:12:0.8"`, so the fully general form —
   arbitrary stills at arbitrary frame positions with weights — is reachable
   later.
2. **Order detection.** Extract the numeric suffix, sort numerically. What is
   the minimal accepted pattern — common prefix + `%0Nd`, or any files whose
   names end in digits? Proposal: require a common stem, tolerate gaps in
   numbering, refuse < 2 usable stills.
3. **Resolution/aspect guard.** Stills vs template `width`/`height` mismatch:
   pre-normalize with ffmpeg (scale/crop/pad into the job dirs) or warn and
   let Draw Things resize? Proposal: warn by default, opt-in `--fit` pass.
4. **Prompting.** See "Prompt sources" below — one shared prompt, a prompt
   list fed from a file, or per-pair prompts synthesized by a local LLM.
5. **Trailing/leading stills.** The first and last stills each appear in only
   one job. Optional "lead-in/lead-out" jobs (chain-style: start or end on a
   single still with no opposite keyframe) — probably out of scope for v1.

### Prompt sources: from one shared string to LLM synthesis

The prompt field is where this idea jumps in complexity. Three sources, in
increasing ambition:

1. **Shared prompt** — v1 as described above; every pair gets the same string.
2. **Prompt list from a file** — the composer accepts a list (one prompt per
   pair, in pair order; count must equal N−1, else refuse). This is also the
   format any external script can feed in.
3. **LLM-synthesized per-pair prompts** — a helper stage calls a local vision
   LLM to write each pair's prompt *against the actual stills*: describe the
   first-frame content, the last-frame content, and the motion/transition
   between them. This is the hard part done automatically instead of by hand.

#### The LLM helper stage (Ollama / LM Studio tie-in)

- **One client, two hosts.** Both Ollama (`127.0.0.1:11434`) and LM Studio
  (`127.0.0.1:1234`) expose OpenAI-compatible `/v1/chat/completions` accepting
  base64 image parts, and `/v1/models` for discovery. So the composer needs
  exactly one client with configurable `--llm-base-url` / `--llm-model` — no
  per-provider code. Plain localhost HTTP; `ltxq` itself gains no LLM
  dependency.
- **Instruction template as the steering wheel.** A user-editable prompt
  template (a style guide: voice, motion verbs, target length, "describe only
  what is visible in the two frames") drives every call; optionally a few-shot
  example. The template is the difference between usable and unusable output,
  so it ships as an editable default in the repo, not a hidden constant.
- **Context carry.** Option to feed the previous pair's generated prompt (or
  its end-frame description) forward, so consecutive pair prompts read as one
  continuous camera move rather than N unrelated descriptions. Trade-off:
  sequential dependency in synthesis (slower) vs narrative coherence.
- **Two-phase, reviewable.** Synthesis is its own step that writes a *pairs
  manifest* (pair order, still paths, generated prompt per pair) plus a
  sidecar txt per pair. The human edits anything that reads badly, then the
  composer queues from the manifest. Raw LLM output never renders without a
  checkpoint. (One-shot `--synthesize-and-queue` can exist later; two-phase
  is the trust-building default.)
- **Caching & determinism.** Low temperature; cache generated prompts by
  (still-hash, template-hash, model) so re-runs don't re-synthesize.
- **Degradation.** LLM unreachable or returning junk for a pair → fall back
  to the shared prompt for that pair and flag it in the manifest; never fail
  the whole batch over one bad call.

This component is deliberately not keyframe-specific: the same prompt factory
serves Idea 1 (per-segment prompts, e.g. from lyrics) and is the automated
form of backlog Idea 5.

### Open questions

- Pairs or triplets as the default composition?
- Pre-normalize still dimensions in the composer or leave it to Draw Things?
- Naming/UX: separate `add-pairs`, or one `add-batch` with modes
  (`--mode audio-manifest | keyframe-pairs | …`)?
- LLM synthesis: its own `synth-prompts` step (two-phase default), or a flag
  inside `add-pairs`? Where does the pairs manifest live — segment dir or
  jobs/_tmp?
- Context carry during synthesis: on or off by default?

---

## Idea 6 — Interactive merge playlist (manual output merging, local ffmpeg)

**Status: Done (v1)** — shipped as the dashboard's "Merge tray" (one implicit
playlist, per-entry ✕ remove and ↑/↓ reorder, "Merge…" with a name prompt),
backed by `merge_items` in the db, the `/api/merge/*` endpoints (API 1.5),
and a background ffmpeg stream-copy worker writing to
`movies_dir/merges/`. The open questions resolved as: single tray (no named
playlists yet), duplicates allowed, re-encode fallback refused with a clear
message (v1 merges only identical streams). Not implemented: the one-click
"merge whole batch" shortcut (Idea 4 territory).

### The scenario

Finished jobs are collected into `movies_dir/<slug>-<id>/`, but each job is
one clip. The user wants to stitch several of them into a single file, in an
order they pick by hand, on demand — without touching the engine or any
remote host. With ffmpeg present locally this is pure client-of-oneself work:
select job A0001 in the dashboard, hit "add to merge", keep collecting clips
into a playlist, then press "Merge" and get one file.

This is the **manual** sibling of Idea 4 (auto-concat on batch completion):
same concat pass, but user-triggered and user-ordered instead of firing
automatically. Both should share one `concat_videos(inputs, out)` helper.

### Why it's cheap here

Every output comes from the same engine with the same per-host
`--video-format` preset (hevc today), so the common case is a **stream-copy
concat**: `-f concat -safe 0 -i list.txt -c copy out.mp4` — seconds, no
re-encode, no quality loss. ffprobe each input first; on mismatch
(codec/resolution/fps/pix_fmt/container), surface a clear warning and either
refuse or offer a re-encode fallback (which does cost CPU — opt-in).

### Interface sketch

- **Data model** — two small tables: `playlists(id, name, created_at)` and
  `playlist_items(playlist_id, position, jid)`. Jobs are referenced by id;
  the actual video path is resolved from `movies_dir`/`metadata.json` at
  merge time so deleted/moved artifacts are caught up front (validate on
  merge, gray out missing items in the UI).
- **API** (Flask, alongside existing routes):
  - `POST /api/playlist` {name?} — create (or return the implicit tray)
  - `POST /api/playlist/<id>/add` {jid} — only `done` jobs with an existing video
  - `POST /api/playlist/<id>/remove` {position} / `POST …/reorder` [positions]
  - `POST /api/playlist/<id>/merge` {name} — spawn ffmpeg, return merge id;
    progress parsed from `time=` on stderr and published over the existing
    SSE bus (`/api/events`), reusing the job-event pattern
  - `GET /api/merge/<id>/download`
- **UI** — a "➕ merge" button on every `done` job card; a collapsible merge
  tray (cart metaphor) listing the ordered clips with up/down/remove; a
  "Merge…" button that asks for an output name and shows a progress bar with
  a download link when done. Batch bonus: a one-click "merge whole batch"
  that auto-fills the tray from the existing `batch` label, ordered by
  chain/creation order — the manual cousin of Idea 4's hook.
- **Save location** — a browser can't pick an arbitrary path, so: write to
  `movies_dir/merges/<name>.mp4` and offer a download link (browser decides
  where it lands). A free-text path field is possible later but is OS-dependent.

### Concurrency

Yes, it can run while the queue is going. The merge is a local subprocess
spawned by the Flask server process; it never touches `engine.lock`, the
dispatch loop, or remote hosts. DB touches are the same short WAL-mode
transactions web `add` already races with the engine today. Stream-copy
concat is I/O-light, so it doesn't meaningfully compete with rsync
collection; only the re-encode fallback would contend for CPU.

### Edge cases & open questions

- Missing artifact (video collected then deleted) → validate before merge,
  disable the button with a reason.
- Same job added twice → allow (dedupe on add? decide).
- Mixing containers (`mov` vs `mp4`) / formats across jobs → ffprobe gate.
- Concurrent merges → serialize behind a simple lock or allow parallel with
  distinct outputs (concat is read-only on inputs, so parallel is safe).
- One implicit tray vs multiple named playlists? Start with one tray,
  promote to named playlists if it earns it.
- Re-encode fallback: ship at all, or refuse mismatches in v1?

---

## Candidate backlog (one-liners, to be expanded when picked up)

- **Idea 3 — Built-in segmenter**: the producer side of Idea 1 — ffprobe/ffmpeg
  cutting of any audio file onto the 8n+1 frame grid at 25 fps + manifest
  writer, so ltxq owns the whole path from mp3 to queued batch.
- **Idea 4 — Batch reassembly & replace**: formalize the concat pass (shared
  companion of Ideas 1 and 2) + a per-segment regen-and-replace flow (regen
  segment k only, re-concat). Shares the concat helper with Idea 6.
- **Idea 5 — Per-segment prompt scripting**: derive per-segment prompts from
  timing/lyrics/section data instead of one shared prompt (the scripted,
  non-LLM cousin of Idea 2's prompt factory).
