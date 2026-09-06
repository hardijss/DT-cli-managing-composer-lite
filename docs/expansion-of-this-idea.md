# Expansion ideas

Scratchpad for ideas about what else `ltxq` could do. Nothing here is a
commitment — ideas land as raw sketches, get battered around, and when one is
actually implemented it graduates to [features.md](features.md) and the
[CHANGELOG](CHANGELOG.md), with its section here marked **Done**.

---

## Idea 1 — Manifest-driven audio-segment batch composition

**Status: sketch**

### The scenario

An audio file has been pre-processed for LTX generation compliance: cut into
N segments whose lengths are frame-locked to the `numFrames` grid
(8n+1 frames at 25 fps — the LTX-2 constraint), each segment exported as a
wav, plus a manifest txt describing them. Today each of those segments needs a
separate manual `add`, with `numFrames` hand-edited in the config JSON per
segment. The idea: **read the manifest and compose/queue one job per segment
automatically**, each with its `config.json` `numFrames` set from the manifest
and its own segment wav attached as `--audio` conditioning.

### Input contract (as produced today)

A directory containing:

- `Name_0001.wav … Name_NNNN.wav` — the segment files
- a manifest like:

```
# 25 FPS Audio Segment Frame Length Manifest
# Original File: Banderos Cats-2.mp3
# Total Segments: 8
# Format: [Filename] -> [Frames at 25 FPS] (Duration, 8n+1 Status)

Banderos Cats-2_0001.wav: 217 frames (8.680s, 8n+1 (n=27))
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

### What the composer would do

- A new command, e.g. `ltxq.py add-batch <segment-dir>` (plus — later — a
  bulk mode on the web form), taking the same shared arguments as `add`
  (prompt / prompt-file, model, `--config-file` template, backend, …).
- Parse the manifest; for each segment create one job where:
  - `config.numFrames` is overridden with the segment's frame count
    (everything else in the config template is left untouched);
  - `--audio` carries that segment's wav;
  - an optional `batch` label defaults to the original file name from the
    manifest header ("Banderos Cats-2") — the existing 📦 badge and
    chain-scoping machinery then apply for free.
- Jobs are independent of each other, so multi-host dispatch "just works";
  only any final reassembly step would need to wait for the whole batch.

### Decisions to make

1. **Manifest parsing.** Prefer the "Raw Frame Counts" integer block (one per
   line, order matches the zero-padded segment numbering); use the per-file
   lines only to cross-check names. Sanity-check: number of integers ==
   number of wavs in the dir == `Total Segments`. Refuse on mismatch.
2. **Non-8n+1 stragglers.** The manifest flags them (e.g. the 181-frame last
   segment above = 8·22+5). Options:
   - round **down** to the nearest 8n+1 (181 → 177) and trim the wav to
     match (7.24 s → 7.08 s);
   - round **up** (181 → 185) and pad the wav with silence;
   - refuse the batch and demand a re-cut.
   Proposal: round down + trim, note it in the job record — never invents
   duration, only loses ~0.16 s. Final call open.
3. **Seed policy.** One shared seed for all segments (consistent look across
   the song) vs per-segment random (variety, current `add` behavior).
   Proposal: pass `--seed` through; absent → random per segment as today.
4. **fps guard.** The manifest states its fps (25); assert the template
   config's `fps` matches, else refuse — mismatched fps would silently
   mis-time every segment.
5. **Per-segment prompts (later).** Optional sidecar `Name_0001.txt` next to
   the wav overrides the shared prompt for that segment.

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
- CLI-only first, or bulk drop on the web form from the start?
- Is visual continuity across segments ever wanted (chained first-frames),
  given each segment has its own audio conditioning? Probably not for v1.

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

## Candidate backlog (one-liners, to be expanded when picked up)

- **Idea 3 — Built-in segmenter**: the producer side of Idea 1 — ffprobe/ffmpeg
  cutting of any audio file onto the 8n+1 frame grid at 25 fps + manifest
  writer, so ltxq owns the whole path from mp3 to queued batch.
- **Idea 4 — Batch reassembly & replace**: formalize the concat pass (shared
  companion of Ideas 1 and 2) + a per-segment regen-and-replace flow (regen
  segment k only, re-concat).
- **Idea 5 — Per-segment prompt scripting**: derive per-segment prompts from
  timing/lyrics/section data instead of one shared prompt (the scripted,
  non-LLM cousin of Idea 2's prompt factory).
