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
- optionally per-segment image sidecars: same basename with an image
  extension (`Name_0001.png` …) — attached as that segment's `--image`. A
  manual `--image` (explicit start image for segment 1) wins over a sidecar;
  a segment with any `--image` skips the visual chain for that segment
  (manual attachments take precedence in `resolve_chain`).
- optionally per-segment config sidecars: same basename with a `.json`
  extension (`Name_0001.json` …) — validated and cumulatively merged as the
  config JSON overlay for that segment's job, carrying forward (sticky) to
  subsequent segments until another `.json` sidecar updates it.

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

**Status: Done (v1)** — shipped as `ltxq add-pairs <dir>` plus the dashboard's
Pairs batch panel (`/api/pairs/*`, API 1.7; 2026-09-25): two-phase
parse → review/edit → queue, model-first flow (fps/grid/strategy from the
model+host choice), per-dialect pair composition (`frame_slots` vs
`canvas_ref`), per-pair length overrides with grid snapping, per-pair
config/audio companions, ordering rules for unnumbered/gapped/mixed folders.
Not implemented: reassembly (Idea 4), `--fit` still normalization (warn-only
in v1), `--manifest` file input (the shipped manifest format already
anticipates it), LLM prompt synthesis (prompt factory below).

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

> **Shipped (2026-09-26), pair-vision subset.** The helper stage is live for
> pairs: named OpenAI-compatible endpoints in `llm.yaml` (Ollama / LM Studio,
> localhost or LAN, no keys — one client, no per-provider code), the steering
> directive ships as a **variant library** (`templates/llm_directives/`:
> `ltx-default` + `minimax-h3-fl2va` distilled from the original MiniMax
> guide; user-local variants next to hosts.yaml win by name, and the
> model-matched default auto-picks H3 vs LTX), and synthesis writes into the
> editable pairs manifest (`prompt_src: "llm"`, directive recorded as
> `llm_directive`) — two-phase, reviewable by construction. Server-computed
> `{{DUR}}`-style placeholders let variants demand exact alignment lines.
> Bulk runs are sequential, skip inline-typed prompts and continue past
> per-pair failures (the degradation rule). Deliberate deviations from the
> sketch above: prompts are composed self-contained per pair (no context
> carry — they must survive reordering), there is no hash cache (the
> persisted manifest *is* the cache), and stage 1 is the dashboard only, no
> CLI `--synthesize` yet. Text-only prompt enhancement **shipped as phase B
> (same day)**: ✨ buttons on the main form, the pairs shared prompt and
> every per-pair prompt, model-matched `enhance-ltx` / `enhance-minimax-h3`
> directives, one-click ↺ undo.

### Open questions

Resolved while shipping (v1):

- **Pairs** as the default composition (stride-1); triplets via
  `--middle-frame` stay a future mode. The review UI edits the pair list.
- **Warn, don't normalize**: still-vs-template dimension mismatch warns; a
  `--fit` ffmpeg pass stays future.
- **Separate `add-pairs`** subcommand (not an `add-batch` mode) — the flag
  surfaces barely overlap; the dashboard is a third composer panel.
- **Reviewable by construction**: the two-phase flow shipped with v1 — the
  parse produces the editable pairs manifest, queueing re-validates it
  server-side. LLM synthesis (own step, pairs manifest as its output format,
  context carry off by default) remains the follow-on; the manifest schema is
  the interface it will write.
- Companions are keyed by **pair index 1..N−1** (dense, stable when stills
  are deleted), not by still number.
- Model choice is the switch: fps, frame grid **and** the per-dialect CLI
  strategy (`frame_slots` vs `canvas_ref`) all derive from model+host before
  the gen list is built; per-pair lengths inherit the grid and can be
  overridden (snapped, ties up).

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

## Idea 7 — Optional path from a personal queue to unattended/shared operation

**Status: design guardrails only — deliberately not a current product goal.**

`ltxq` is intentionally a single-user, trusted-machine tool. Its localhost
dashboard, SSH credentials, and direct access to the user's Movies folder are
good trade-offs for that setting. This note records the small choices worth
making now so a future “shared render appliance” does not require rewriting
the queue; it does **not** propose exposing the current dashboard to a LAN or
adding accounts prematurely.

### Keep two futures distinct

There are two materially different expansions, and choosing one should happen
before implementing shared features:

1. **Unattended personal queue** — still one trusted owner, but expected to
   run for days and recover cleanly from restarts, host outages, and upgrades.
2. **Shared queue** — multiple people or services submit work. This adds
   identity, ownership, quotas, auditability, and an explicit network boundary;
   it is not merely an unattended queue with a public URL.

The first is a sensible incremental hardening path. The second should begin as
a separately planned deployment mode, with its own threat model and operator.

### Low-regret foundations to add while remaining single-user

- **One scheduling source of truth.** Keep job eligibility and state
  transitions in shared engine functions rather than copying queries between
  the CLI and dashboard loops. The batch-chain selector now follows this
  pattern; future policies (retries, priorities, host affinity) should do the
  same.
- **Executable lifecycle tests.** Add a small, fast SQLite-backed test suite
  covering claim/dispatch, cancellation, collection retry, chain ordering,
  restart recovery, and host-config reload. These tests are valuable even for
  one person because render failures are expensive and often long-running.
- **Explicit state transitions and attempt history.** Treat every job launch
  as an attempt with a timestamp, selected host/backend, command version, and
  terminal reason. Preserve prior attempts when retrying instead of replacing
  a note string. This makes an overnight failure explainable without logging
  into every host.
- **Idempotent recovery.** On startup, reconcile an `uploading` or `running`
  job against its remote manifest/pid/output before requeueing it. Use a
  per-attempt token in the remote work directory so a delayed SSH command
  cannot be mistaken for a newer attempt.
- **Named operational limits.** Put retry budgets, collection retry cadence,
  maximum queue length, maximum artifact age/size, and a host “offline after
  N failures” threshold in configuration. Defaults can remain personal-tool
  friendly; naming the limits early prevents implicit behavior from becoming
  an accidental contract.
- **Upgrade and backup discipline.** Version database migrations, offer a
  pre-upgrade SQLite backup, and record the engine/API version in job
  metadata. An unattended process needs a rollback story before it needs more
  features.

### If unattended personal operation becomes important

Prioritize in this order:

1. Structured local logs and a compact health summary (`last successful
   dispatch`, `last collection`, hosts unreachable, jobs stuck by state).
2. Bounded retries with exponential backoff for transport and collection
   failures, never blindly retrying an uncertain remote render.
3. Optional owner-selected notifications for meaningful events only: a failed
   job after its retry budget, a host going offline, disk-space risk, or queue
   completion. No routine “still running” noise.
4. Periodic, non-destructive reconciliation that can report orphaned remote
   directories, missing local artifacts, and stale workers; repair actions
   should remain explicit.
5. Retention controls for job inputs, logs, staging, and collected outputs,
   with dry-run reporting before any new cleanup policy removes material.

### Additional requirements before any shared deployment

Do not bind the existing Flask app beyond loopback. A shared mode needs all of
the following as a coherent package:

- authenticated users or service identities, with job ownership recorded in
  the database;
- authorization rules for job data, inputs, logs, artifacts, host controls,
  and destructive actions;
- TLS and a deliberately operated ingress/reverse proxy, rather than Flask's
  development server acting as an internet-facing service;
- per-owner quotas and fair scheduling so one batch cannot consume every host;
- immutable audit events for submission, cancellation, deletion, config
  changes, and operator actions;
- tenant-safe artifact storage and download URLs — never expose host paths or
  let one user's cleanup remove another user's output;
- secret management for render-host credentials, separate from application
  configuration and never returned by diagnostics APIs.

At that point, reassess the zero-footprint SSH model. It may remain suitable
for a small trusted fleet, but a worker/agent protocol with authenticated
heartbeats, capability reporting, and signed job manifests can be easier to
operate at shared scale. That would be a new architecture decision, not a
minor extension of `hosts.yaml`.

### Decision triggers

Revisit this idea when any of these becomes true: another person needs to
submit or view jobs; the process must run unattended for more than a few days;
render hosts are outside the owner's trusted network; or failures require
regular manual database/SSH intervention. Until then, preserve the current
single-user scope and choose the low-regret foundations only when they also
improve the personal workflow.

---

## Idea 8 — Audio batch E1: multi-image attachment & dialect-aware compilation

**Status: Done (v1)** — shipped as `ltxq add-batch-e1 <dir>` (+ `--dry-run`)
and the `/next` "Audio E1" tab (`/api/add-batch-e1/*`, 2026-10-17): sidecar
role suffixes (.i/.iN/.f/.m/.l/.kNN), per-dialect compilation (named slots on
dtcustom vs ordered --image on dtofficial), a parse → edit → queue editor with
a per-dialect compile preview, continuity with the first-frame-wins rule and
the last-frame → pinned-pair upgrade, plus the `multi_image` capability and
the `cli_cmd` `$HOME`-quoting fix. Steered roles (`.m`/`.l`/`.kNN`) are sent
as ordered `--image` references on dtofficial — continuity leads as image #1;
a lone reference still refuses. Not implemented: prompt rewriting for steered
roles (adding `<Picture N>` addressing), keyframe strength syntax (engine
default), a per-segment continuity inheritance UI (batch checkbox + per-row
toggle only), and a manifest file input (B).

### The scenario

An audio-segment batch needs more than the current one-image-per-segment rule:

- a segment pinned start→end (a first frame **and** a last frame);
- a segment that continues the previous generation (⛓ last frame) *and* adds
  an extra reference or an end frame;
- refs/moodboards referenced from the prompt (dtofficial / H3 path) instead
  of pinned frames (dtcustom path).

The two dialects express these differently — argument-carried roles
(`--first-frame` …) vs prompt-carried roles (`--image` order + `<Picture N>`,
see docs/reference/minimax-full-reference-guide.md) — so the composer keeps a
**dialect-neutral attachment model** and compiles per host dialect.

### Input contract (A — sidecar suffixes)

Files next to each `<stem>.wav` extend the existing same-stem image rule:

```
<stem>.png        ordered image #1   (legacy alias of .i — keeps today's behavior)
<stem>.i.png      ordered image #1   → --image            (either dialect)
<stem>.i2.png     ordered image #2   → --image, then .i3 … (order by number)
<stem>.f.png      first frame        → --first-frame      (dtcustom)
<stem>.m.png      middle frame       → --middle-frame     (dtcustom)
<stem>.l.png      last frame         → --last-frame       (dtcustom)
<stem>.k12.png    keyframe @ index 12 → --keyframe path:12:… (dtcustom)
<stem>.txt        prompt sidecar     (unchanged)
<stem>.json       config overlay     (unchanged)
```

Rules: role letters are **lowercase only** (macOS filesystems are
case-insensitive — case cannot be the signal); `.f`/`.m`/`.l` are single per
stem; `.kNN` repeats per index; multiple ordered images are numbered in the
stem (`.i`, `.i2`, `.i3`, … — no upper bound in the grammar; the dialect
compile decides usability). Unknown suffixes go to a visible "not attached"
tray — never silently ignored.

### Attachments model

Each segment carries an ordered list of attachments — `{file, target}` with
target ∈ `ordered | first | middle | last | keyframe(idx)` — plus a
**continuity ghost attachment** (⛓ previous segment's last frame → *start*),
resolved at launch as today. Filenames, editor chips, compile flags and the
existing semantic names (`FLAGMAP`) all speak this one vocabulary.

### Compilation (v1: flags only)

V1 keeps the existing flag/gate mechanics and adds **flagging**, not
translation machinery: the compile preview marks every attachment **exact /
steered / unsupported** for a dialect, and the capability gate keeps refusing
what a host cannot run.

| segment attachments | dtcustom compile | dtofficial compile |
|---|---|---|
| `.i` ordered #1 | `--image` | `--image` (#1 = primary) |
| `.i2`+ | none — at most one ordered image (engine crash beyond); flagged | `--image` #2..n, in order |
| `.f` alone | `--image` if the positional slot is free (start-pinned; a lone `--first-frame` is invalid) — flagged | steered-only (prompt-carried) — flagged |
| `.f` + `.l` | `--first-frame` + `--last-frame` (+ preflight) | start pinned + end steered (FL2VA-style prompt) — flagged steered |
| `.m` middle | `--middle-frame` | steered-only — flagged |
| `.l` alone | `--last-frame` | **indeterminable** (a lone image would read as the start) — flagged unsupported |
| `.kNN` keyframe | `--keyframe path:NN:default` | steered-only — flagged |
| continuity ⛓ (no `.f`) | start attachment: `--image`; `--first-frame` when paired with `.l` | start attachment `--image` #1 |
| continuity ⛓ + `.f` | **FF wins** — the continuity frame is omitted from the argument list, with a note on the segment's stem line | same |
| `--audio` | `--audio` | `--audio` |
| prompt addressing | not needed | prompt addresses `<Picture N>` / `<Subject N>` per the H3 guides |

Keyframe strength: engine default in v1 (`.kNN` carries the index only).

### The editor (C) — `/next` "Audio E1" tab

A clone of the audio-batch panel (old panel byte-frozen), pairs-panel DNA:
parse → review/edit → queue.

1. Drop/pick the segment folder (or a server path) → parse wavs, prompts,
   configs and role-suffixed images; unrecognized files listed in the tray.
2. One row per segment: image chips (thumbnail · target dropdown · remove),
   add/replace from a palette of the dropped folder's images, ordered-image
   reordering, keyframe index on the chip, and the continuity ghost chip
   (per-segment toggle, defaulted from the batch checkbox).
3. **Compile preview** — per segment and per dialect: the exact flags that
   will be issued, every attachment marked exact / steered / unsupported;
   continuity-omitted cases annotated on the stem line. Errors block the
   queue button; steered/unsupported items stay visible as flags.
4. Queue → jobs store semantic attachments; dispatch compiles per the target
   host's dialect (`auto` shows both branches in the preview).

### Validation taxonomy

- **error** (blocks the queue): two files claiming one single slot
  (two `.f`, two `.m`, two `.l`); two files claiming the same order number
  (`.i` + `.i1`); overfull dtcustom positionals (`.i2`+, or `.f`-as-`--image`
  together with an ordered `.i`).
- **warning** (visible flag): steered-only targets on dtofficial;
  continuity omitted because the segment targets `.f`; `.f` alone on dtcustom
  (compiles as `--image`).
- **info**: continuity attached; non-grid frame pads/trims (unchanged).

### API sketch (mirrors the pairs routes)

```
POST /api/add-batch-e1/preview              stage a folder / plan segments + attachments
GET  /api/add-batch-e1/<sid>                read the session (segments, attachments, warnings)
PUT  /api/add-batch-e1/<sid>/manifest       autosave the edited attachment truth
POST /api/add-batch-e1/<sid>/queue          validate + create jobs
GET  /api/add-batch-e1/<sid>/file/<name>    staged thumbs for the editor
POST /api/add-batch-e1/<sid>/discard
```

Plus a headless `ltxq add-batch-e1 <dir>` sharing parse + compile, so the
contract is testable without the UI (`test_e1*` alongside `test_pairs*`).

### Phasing

1. Parser + compiler + `add-batch-e1` CLI + tests (the contract).
2. Preview/queue API over the same functions.
3. Panel in `/next` (new tab ids, feature markers; old panel untouched).
4. Docs: the dialect translation matrix lands (see `docs/cli-dialects.md`
   area, `docs/dialect-translation.md` TBD), changelog entry, and the
   `ltxq-dialects` skill picks up the compile rules.

### Decisions locked (2026-10-13)

- V1 inputs: sidecars (A) + editor (C); no manifest file input (the editor
  state can serialize to one later).
- Tier handling v1: **flag only** — exact / steered / unsupported markings
  plus the existing capability gates; no auto-translation yet.
- Continuity: attaches as the start attachment unless the segment targets
  `.f` — then the FF image wins and the continuity frame is omitted with a
  stem-line note.
- Keyframe strength: engine default in v1.
- Placement: `/next`, "Audio E1" tab; the basic panel stays.

### Open questions

- When to promote steered/unsupported from "flag" to a translate/refuse
  policy — and whether dtofficial translation auto-writes the H3 prompt
  addressing.
- The dtofficial end-frame path (`.l` → FL2VA prompt): reuse the pairs
  composer's `minimax-h3-fl2va` directive machinery?
- Retiring the basic panel once E1 reaches parity.
- Per-segment chain default UX (inherit the batch checkbox vs explicit rows).

---

## Idea 9 — Portable batch export: validated asset package + launch script

**Status: idea draft (2026-10-19) — planning only, nothing built.** After a
batch validates in the Audio batch E1 editor, offer a second exit besides
queueing: export a self-contained package — media, prompts, configs and a
POSIX-sh launcher — that runs the engine CLI on whatever machine the folder is
copied to, with no ltxq, no database and no ssh involved.

### The scenario

An E1 batch has been parsed and reviewed: segments, fitted audio, prompts,
config overlays, image attachments with roles, continuity toggles — all
validated. Sometimes the batch should not run through this queue at all:

- the render box is offline / on another network, or ltxq isn't installed there;
- the batch should be handed to a collaborator together with its inputs;
- the exact commands should be reproducible later, outside the queue's state;
- **there is no communication line to the render side that can be kept up,
  monitored or trusted** — the package travels over whatever medium exists
  (USB stick, file share, a link) and the outputs come back the same way.

In all three cases what is wanted is the *validated output of the composer*,
frozen into a portable bundle.

### What the package contains

```
<batch>-<stamp>/
  manifest.json          package format + tool version + batch/model summary;
                         per job: frames, fps, prompt/config refs, attachment
                         roles, the compiled argv, sha256 of every media file
  README.txt             requirements, how to run, dialect/CLI notes, warnings
  run.sh                 master launcher (POSIX sh — see below)
  verify.sh              optional integrity check (shasum / sha256sum)
  jobs/
    0001-<segment>/
      prompt.txt  config.json  audio.wav        (fitted to the frame grid)
      <segment>.i.png …                          (role-named media as in E1)
      runner.sh                                  (same shape ltxq runs: cd +
                                                  CLI invocation, make_runner)
    0002-<segment>/ …
```

Media covers exactly what the plan references — the **already-cut segment
wavs** the composer receives (cutting/word extraction is an external pre-step,
out of scope for Ideas 1–9), fitted to the frame grid at export (pad/trim
applied), prompts, per-segment config JSONs (numFrames and the sticky overlay
baked), and the attachment images with their roles recorded in the manifest.
Checksums let the recipient prove the bundle survived copying. No credentials,
hosts.yaml content or queue state is packaged.

### The launcher

`run.sh` is generated, not hand-written, and reuses the two pieces ltxq's
oneshot dispatch already uses — `make_runner()` per job and the plan's
compiled argv — so the package runs **the same commands** the queue would
have launched:

- POSIX sh, no python, no ltxq, no database, no network.
- Single dialect per package, chosen when the batch is made (the same early
  decision as model/ext/fps): every job's argv is compiled for that dialect
  and the launcher takes exactly one engine binary (`--cli` / `$CLI`). An
  auto-hosted session must pick the target dialect before exporting.
- Sequential job loop with per-job logs; `--resume` skips jobs whose expected
  output already exists. A chained job whose predecessor's output is missing
  **fails with a clear message** rather than running without the start frame.
  A summary and a non-zero exit code close the run.
- Plain CLI invocation (no PTY): `use_pty` stays an ltxq-side runner detail; a
  `--pty` flag can be added later if a target engine needs it.
- `--merge` (optional): lossless ffmpeg concat of the per-job outputs in job
  order (job dirs are numbered deterministically) into one `<batch>-assembled`
  file — every per-segment output is kept as-is.
- `--pack-outputs` (optional): bundle the outputs into `outputs.tar.gz` plus a
  sha256 list and the run report, so the recipient can send one file back over
  the same ad-hoc channel.
- `--check` (no rendering): print the required engine CLI, model/LoRA file
  names (from the baked configs) and ffmpeg availability, so the recipient can
  confirm the box is ready before a long run.
- Models must already be present on the target (engine default models dir or
  `DRAWTHINGS_MODELS_DIR`); the package bakes `--offline` /
  `--no-download-missing` so a missing model fails loudly instead of
  downloading.
- **Continuity** stays supported: each chained job's runner references a fixed
  placeholder name (e.g. `chain_prev.png`, listed in the manifest's
  `runtime_files` — never in the checksum set, and no fake zero-byte files are
  shipped); between jobs the launcher extracts the previous output's last
  frame with ffmpeg into that file — mirroring `extract_frame()` and the
  FF-wins / `image_first` rules compiled into the argv. ffmpeg/ffprobe are
  therefore required only for continuity (and `verify.sh`).
- Nothing is written outside the package: outputs land beside each job's
  `config.json` (`out.<ext>`), exactly the runner's working directory.

### Handoff contract (offline, untrusted-by-default transport)

Because the package is the *only* channel to the render side, it must stand
alone and be checkable by the recipient without talking back:

- **Validation is complete at export** — there is no queue afterwards to catch
  a bad segment, so the export gate is the last line of defence (see below).
- **Recipient prerequisites are explicit**: the engine CLI, its models
  (manifest lists the model + LoRA file names), and ffmpeg/ffprobe only when
  continuity, `--merge` or `verify.sh` are used. No ltxq, no python, no
  network, no database.
- **Integrity before execution**: `verify.sh` checks the sha256 manifest; this
  is integrity, not authenticity (a detached signature could be added later if
  packages start crossing trust boundaries regularly).
- **Results are packable for return**: per-job logs + `run-report.json` always;
  `--pack-outputs` bundles the outputs + checksums for hand-back. Exit code and
  summary flag any failed segments.
- **Nothing sensitive travels**: no credentials, hosts.yaml content, ssh
  material, telemetry or callbacks.

### Validation is the point

Export re-runs the E1 planner on the stored session edits at export time —
never on a cached plan: any error blocks the export (the same list the editor
shows), warnings are copied into `README.txt` and `manifest.json`, and the
sha256 manifest is computed from the files actually written. The button stays
disabled while the editor shows errors, so "validated" means the same thing
for queueing and for export.

### API + UI sketch

```
POST /api/add-batch-e1/<sid>/export                build, return {name, path,
                                                   url, bytes}
GET  /api/add-batch-e1/<sid>/export/<name>.tar.gz  download
```

Packages build under `jobs/exports/e1_<sid>_<stamp>/` and are tarred to
`<name>.tar.gz`; old exports are pruned like sessions. The E1 panel gets an
"Export package" button next to "Queue batch" (same disabled-on-errors rule)
and shows the download link plus the server path. A headless counterpart
(`add-batch-e1 --export DIR`, nothing queued) reuses the same builder.

### Decisions (locked 2026-10-19)

- **Dialect is an early batch decision** (like model/ext/fps): the package is
  single-dialect and the launcher takes one engine binary. Auto-hosted
  sessions choose the target dialect before exporting.
- **Media = the cut segment wavs** the composer already receives (the cutter
  is an external pre-step, out of scope for Ideas 1–9), fitted to the grid at
  export; originals are not packaged.
- **Assembly is optional**: `run.sh --merge` lossless-concats the per-job
  outputs in job order, keeping every per-segment file. The full
  reassembly/replace flow (Idea 4) stays separate.
- **Entry points: both** — an "Export package" button in the E1 panel (via the
  session API) and a headless `add-batch-e1 --export DIR` sharing one builder;
  export-only, never queues in the same invocation.
- **Chain placeholders** live in the manifest's `runtime_files` and are
  excluded from checksums (no fake zero-byte files).
- **Resume with a missing predecessor output** fails that segment with a clear
  message (accepting the stricter behaviour; revisit after real use).
- **Plain CLI invocation** in exported runners (no PTY); `--pty` only if a
  target engine proves to need it.

### Still open

- **Authenticity**: add an optional detached signature (minisign/GPG) to the
  package? sha256 covers corruption, not origin; revisit if handoffs become
  routine.
- Build mode: packages are tens of MB; build synchronously first, move to a
  background thread only if it becomes slow.

---

## Idea 10 — Audio batch E1_variant: semi-interactive variant approval

**Status: Done (v1, 2026-10-20)** — shipped as the `/next` "E1 Variant" tab
with `/api/e1/release/*` and `ltxq.release_*` (schema: `vg`/`rseg`/`pick`/
`rating`/`release`/`chain_src` + a `releases` table; inputs frozen under
`jobs/_releases/<rid>/`). Per-variant prompt/config overrides are editable in
the prepare-rows step. Compose still happens in the Audio E1 tab, which gains
a single "Release interactive…" button; auto mode stays classic E1
queueing, so the E1 tab can be retired once parity is validated in real use.
Not implemented: the Idea 11 reprocess waterfall and the seed conveniences
(both deferred there). An interactive sibling of Idea 8's E1 panel: the batch
is composed and validated exactly as today, but "release for generation"
walks the stems one at a time, generating **variant sets** per stem and
waiting for a human pick before moving on. With `auto continue` on it
degrades to the classic E1 behaviour — the long-term goal is one panel with
two modes, replacing the separate E1 tab.

### The scenario

Some generation inputs are quality-critical in ways validation cannot settle:
the seed alone swings composition and content, and per-segment config tweaks
(steps, cfg, LoRA mix, prompt overlay) are often judged only by looking at the
result. Today the operator queues the whole batch, waits for everything, and
re-queues individual segments by hand — losing both the "same inputs,
different parameters" relationship and any record of which output was actually
chosen for the final assembly.

### The workflow

1. **Compose** (E1): parse, attachments, continuity, compile preview —
   unchanged.
2. **Release**: choose mode (`interactive` | `auto continue`) and the stem's
   **seed list** — a text field of exact seeds (comma/space separated; "good"
   seeds are reused deliberately because seed choice swings quality). Each
   entry becomes a variant row whose **prompt and config can be adjusted**
   (the stem's values are the defaults). An "add N random" convenience may
   append random seeds, but explicit numbers are the primary path. A release
   session is created over the validated plan.
3. **Per-stem loop** (interactive): the current stem's variant set is queued;
   as each variant completes (SSE), its tile appears with its seed/config and
   the output preview. The operator can:
   - rate a variant **0–5 (0 = reject, 5 = good)** and mark exactly one as
     **"use this variant"**;
   - adjust seed/config and **re-issue** — new variants join the same group;
   - generate **additional variants** of the same stem package;
   - press **"go and process next"** once the stem is settled.
4. The next stem's variants compile with the **picked variant as the
   continuity source** (its extracted last frame feeds the chain frame), so
   the walk respects the visual chain.
5. `auto continue`: **exactly one variant per stem** (the explicit seed, or
   the engine's random seed when none is given) — byte-for-byte the classic
   E1 flow: all stems queued, chain resolves as today, no review stops. With
   this mode proven, the E1 tab can be retired.

### Variant sets and re-issue

- A **variant group** = one stem's set of jobs generated from the same stem
  package (same fitted wav, same images and roles) with one **explicit seed**
  per variant plus optional **prompt and config overrides** — a variant may
  need a different prompt, not only a different seed. Re-used "good" seeds are
  first-class, not regenerated. Overrides live on the job itself (its prompt
  and config text) — no extra schema needed.
- **Re-issue** = a new job in the same group with an adjusted seed/config; it
  reuses the stored assets exactly (the `regen` path already rebuilds jobs
  from an existing one).
- Continuity inside a variant set: every variant in a group chains from the
  *picked* variant of the previous stem; variants never chain from a sibling
  in the same group.

### Data model (sketch)

- `jobs` gains nullable columns: `vg` (variant-group id), `rseg` (release
  segment index), `pick` (exactly one `1` per `vg`), `rating` (int 0–5:
  0 = reject, 5 = good), `release` (release id). Additive migrations — old
  jobs unaffected.
- `releases` table: `id, created_at, batch, plan_json, mode, cursor, state` —
  survives server restarts; the E1 session files stay the composer side.
- `chain_src` (job id) on jobs: when set, `resolve_chain()` extracts the frame
  from that exact job instead of "latest finished in the batch" — required so
  variants chain from the pick, not from a random sibling.
- Per-variant prompt/config overrides need no new columns: the variant *is* a
  job, so its prompt/config text carries them.
- History: extend the existing batch-run strips with variant-group sub-strips
  (reuse the run-grouping renderer); the picked variant gets a badge, the
  rating shows next to it.

### UI sketch (E1_variant tab, `/next` first)

- Left: stem list with per-stem state (compose → generating → review → done),
  the current stem highlighted.
- Center: variant tiles — inline `<video>`/thumbnail via `/api/view/<jid>`,
  seed/config diffs, status badges, rating control (0–5), "use this variant".
- Bottom: variant spec rows (seed list → one row per variant, each with
  editable prompt/config), "generate more", "re-issue with changes", "go and
  process next".
- Switch: `auto continue` — hides the review loop (classic behaviour).
- History: variant groups as described above; the picked variant is what
  downstream chain / Idea 9 export references.

### API sketch

```
POST /api/e1/release                       {sid, mode, variants:{k, seed_mode, configs[]}}
GET  /api/e1/release/<rid>                 stems, variant groups, statuses, picks
POST /api/e1/release/<rid>/segments/<n>/variants   queue K more variants
POST /api/e1/release/<rid>/segments/<n>/pick       {jid}
POST /api/e1/release/<rid>/segments/<n>/rate       {jid, rating}
POST /api/e1/release/<rid>/next            advance to the next stem
POST /api/e1/release/<rid>/abort | /finish
```

### Guardrails and edge cases

- A pick change after downstream stems exist **invalidates** their chain
  source. The policy — reprocessing from a selected stem, demoting downstream
  stems to yet-to-generate — is deliberately deferred to **Idea 11**; Idea
  10's model (per-stem pick + `chain_src`) is what makes it possible.
- All variants of a stem failing must allow adjust-and-retry or an explicit
  skip (with a note) — never a dead end.
- Auto mode must be classic E1, byte for byte: one job per segment, same
  argv, same History appearance apart from the variant grouping being trivial.
- The release controller persists in the DB, not process memory (restart-safe).
- Idea 9 export can later export the *picked* variants as the release manifest.

### Phasing (tentative)

1. Data model + release controller (queue one stem's variants, pick, next) + tests.
2. Interactive UI in `/next` (variant tiles, SSE updates, controls).
3. History variant grouping + picked/rating badges.
4. `auto continue` parity, then retire/replace the E1 tab once proven.

### Decisions (locked 2026-10-20)

- **Variant axis = explicit seeds**: a text field of exact seed numbers; each
  entry is one variant (good seeds deliberately re-used). Seed conveniences —
  the "append N random seeds" helper and the seed book — are deferred to
  Idea 11; v1 accepts exact numbers only.
- **Rating = 0–5 numeric** (0 = reject, 5 = good); the **pick** ("use this
  variant") is a separate explicit choice per stem.
- **Auto mode = one variant per stem** — exactly current E1 behaviour; once
  parity is validated the E1 tab can be retired (E1_variant supplants it).
- **Late pick changes / re-opening earlier stems = reprocess from the selected
  stem** (downstream stems revert to yet-to-generate). Deferred to **Idea 11**
  for elaboration; Idea 10 carries the data model it will need.
- **Placement**: a separate `E1_variant` tab next to E1 in `/next`; E1 stays
  untouched while Idea 10 proves itself, then it can be disabled/removed.
- **Serial, single-lane variants**: one variant at a time per release — no
  concurrent branching (simplest state machine and review loop).
- **Variant dimensions**: seed list + per-variant **prompt** and **config**
  overrides, editable per variant row; no matrix generation.

---

## Idea 11 — Reprocess from a selected stem (downstream waterfall)

**Status: raw sketch (2026-10-20) — deliberately deferred; not a build target
yet.** Idea 10 keeps one picked variant per stem and chains stem k+1 from stem
k's pick. Changing an earlier pick — or deciding to re-run from an earlier
stem — means everything after it was generated against a chain that no longer
exists.

Scenario: A → B → C → D generated and picked. The operator decides to
regenerate starting from B. Then **C and D must be demoted to "yet-to-
generate" stems**: their picks are cleared, their variant groups stay in
History as superseded material, and the review loop restarts at B.

What the elaboration will have to settle:

- a "reprocess from here" action on a stem: downstream picks cleared, chain_src
  references reset, downstream stems marked pending in the release state;
- History rendering for superseded downstream generations (keep grouped vs
  hidden; how to label them);
- whether reprocessing **forks a new release** (A/B′ vs A/B/C/D history) or
  mutates the current release in place;
- fate of superseded outputs: keep untouched, or auto-rate 0 ("rejected")?
- interaction with Idea 9 exports: an export taken before a reprocess is stale;
- seed conveniences moved here from Idea 10: an "append N random seeds"
  helper and a *seed book* (good seeds with short notes, one click to add to a
  variant row);
- cursor/state rebuild after a reprocess (release resumes at the selected
  stem, downstream regenerated in order).

Open questions: cap on how far back one may reprocess; whether partial reuse
(e.g. keep C's variant set as candidates for the new chain) is ever allowed —
note that outputs from a different chain seed must never be used as chain
sources.

---

## Idea 12 — Durable compose state: resume prepared batches after browser close / reboot

**Status: idea draft (2026-10-20) — planning only, nothing built.** A batch can
take real effort to prepare — parse, attachments, seeds, half a release walked
— and today a browser close or a reboot throws the *dashboard state* away even
when the files are still on disk.

### What survives today (inventory)

- **E1 compose**: the session dir `jobs/_tmp/e1_<sid>/` (manifest + edits +
  staged media) survives, but the dashboard keeps the sid **in memory only**
  → a reload shows an empty panel; the session is orphaned until its 7-day GC.
- **Classic audio batch and New-job**: form values and staged uploads have no
  resume path at all.
- **Pairs**: the sid lives in `localStorage`; another browser/profile loses it
  even though the session dir remains.
- **E1 Variant release**: the release (cursor, variants, picks) is in the DB
  and safe, but the UI finds it only through `localStorage` rid — no "recent
  releases" list if that is cleared.
- **Reboot**: the DB + staging dirs survive; the engine re-queues `uploading`
  jobs on start. It is purely the client-side state that is lost.

### What "saved" should mean

- **Saving is by intent.** Only the explicit **Save project** button writes
  the slot (and shows "saved HH:MM"). Batches/panels that are not explicitly
  saved keep today's volatile behaviour — a reload or crash discards them.
  No background autosave, no silent overwrites.
- **Server-side truth — one slot per panel = the panel's "current project"**.
  Think of it like an editor's open workspace: one saved batch at a time per
  panel. A slot is two things stored together: the *state* (form fields + the
  plan snapshot the user was looking at) and the *asset directory* that
  belongs to it. For `e1` and `pairs` that directory already exists (the
  session dir collects the wavs, prompts, configs and images) — the draft
  adopts and **pins** it instead of copying. Panels without a session dir
  would get `jobs/_drafts/<panel>/` with the staged uploads copied in.
  Starting a new parse replaces the slot (with a confirm), so "save" never
  has to name anything — the slot *is* the current project.
- **Restore = the exact state at the last save, not a re-derivation.** The
  draft stores the plan/edits/UI state as they were, so a restore does not
  re-parse changed files on disk and then drift. Referenced assets are
  validated first; on any problem the restore returns explicit reasons
  ("asset dir missing", "file X gone", "state version too new"), and the UI
  shows them (a fields-only partial restore can be offered).
- **UI restore on load**: each panel refills from its slot, shows "resumed
  … HH:MM" with a **start new / discard** action.
- **The draft pins its files**: session GC skips referenced dirs; discarding
  a draft deletes only ltxq-owned staging, never a path the user typed.
- **localStorage downgraded** to a fast-path pointer; the DB is authoritative
  (localhost single-user → last write wins).

### Design sketch

- Table `drafts(panel TEXT PRIMARY KEY, updated_at INT, label TEXT,
  state TEXT, files_dir TEXT)` (additive migration). One slot per panel; v1
  panels: `e1`, `e1variant`, `pairs` (later: `newjob`, `audio`, with copied
  `jobs/_drafts/<panel>/` asset dirs).
- API: `GET /api/drafts` (all slots), `GET|PUT|DELETE /api/drafts/<panel>`,
  `POST /api/drafts/<panel>/export` (streams the .zip) and
  `POST /api/drafts/import` (multipart .zip → validate → fresh session dir →
  register the slot). `state` is opaque JSON (schema-versioned); the server
  validates only `files_dir` (ltxq-owned staging) and timestamps.
- Per-panel payloads:
  - `e1`: form fields + `sid` — restore re-fetches
    `/api/add-batch-e1/<sid>` (plan, edits, images).
  - `pairs`: fields + `sid` — restore re-fetches `/api/pairs/<sid>`.
  - `audio` (later): fields + `dir` (staged dir or typed path) + start-image
    name.
  - `newjob` (later): fields + staged file refs; a small server option
    accepts already-staged uploads instead of re-uploading.
  - `e1variant`: `rid` + a new `GET /api/e1/release/recent` list, so the
    release is recoverable even without any local pointer.
- Save: the button writes the slot's `state` and adopts/pins its asset dir;
  `updated_at` bumps and the panel shows "saved HH:MM". Nothing is written in
  the background.
- **No timers on saved work**: a draft and its asset dir live until the user
  explicitly discards them. `_e1_gc` / `_pairs_gc` skip dirs referenced by a
  draft (adopted dirs become effectively permanent). Time-based GC remains
  only for never-saved, orphaned staging — nothing a draft points at.
- Reboot behaviour: nothing extra server-side (DB + files persist). UI
  restores slots; in-flight jobs keep today's semantics (queued resumes,
  `uploading` re-queues). Running/collecting reconciliation stays out of
  scope, noted as existing behaviour.
- **Scope v1**: `e1`, `e1variant`, `pairs` — the compose surfaces that already
  own a server-side session. Classic audio batch / New-job slots can follow
  later with `jobs/_drafts/<panel>/`.
- Small UX: a "resumed draft from HH:MM · discard" chip per panel; a shared
  "recent work" strip can come later.

### Portable save packages (.zip)

Optional on save ("Save & download .zip") or later from the saved slot:
produce a self-contained bundle that can be uploaded back here — or on another
machine running ltxq — and reopened as the same project.

```
<panel>-<label>-<stamp>.zip
  project.json        schema version, panel, created/updated, ltxq version,
                      the exact state snapshot (form fields, plan/edits)
  media/…             the session's asset files (wavs, prompts, configs,
                      images) under their original basenames
  checksums.sha256    every file in media/ + project.json
```

- **Import**: `POST /api/drafts/import` validates the checksum list, the state
  schema and the archive paths (zip-slip safe: only plain relative names
  under `media/`), unpacks into a fresh `jobs/_tmp/<panel>_<sid>/`, rewrites
  the state's session paths, then registers the panel slot and returns a
  restore report — the UI offers "Open imported project".
- **Scope**: compose drafts (`e1`, `pairs`) in v1. An `e1variant` slot is
  inherently server-side (releases live in the DB and reference generated
  jobs); exporting a whole release tree is out of scope here — Idea 9's
  export covers the runnable side.
- **Sizes**: media can be tens to hundreds of MB; build and stream the zip on
  demand (show its size); no background packaging in v1.
- **Compatibility**: a newer `schema` than this ltxq refuses with a clear
  reason; older versions are best-effort and report what could not restore.
- **Relationship to Idea 9**: same mechanics, different payloads — Idea 12
  zip = resumable *project* (state + inputs); Idea 9 = runnable *render
  package* (compiled argv + launcher). Share the pack/verify helper and the
  path-safety rules.

### Edge cases

- Draft references a session dir that already expired → show "draft expired"
  and keep the form fields so the user can re-parse quickly.
- Explicit discard → delete the slot + ltxq-owned staging dir; never a typed
  path.
- Two tabs on one slot → last write wins; optionally warn when `updated_at`
  jumped.
- Drafts are local-only (localhost): no prompts or media leave the machine.
- Queueing does not clear a draft automatically — proposal: keep it until
  "New" or discard, so a failed queue attempt can be retried with the same
  prepared state.

### Phasing (tentative)

1. Table + endpoints + the E1 slot/restore (the biggest practical win).
2. Pairs slot + the release pointer with a recent-releases list.
3. Classic batch + New-job slots (fields + staged-file refs).
4. GC pinning, discard UX, docs.

### Decisions (locked 2026-10-20)

- **One slot per panel** — the panel's current project; a new parse replaces
  it after a confirm. Saving collects the state + the asset directory the
  slot owns (session dir for e1/pairs, adopted and pinned; a copied
  `jobs/_drafts/<panel>/` for future panels without one).
- **Saving is by intent**: the explicit Save button is the only writer;
  unsaved batches are discarded on reload (today's behaviour).
- **Portable save**: a saved slot can be downloaded as a `.zip`
  (state + media + checksums) and uploaded back later — import validates and
  registers it as the panel's current project.
- **UI**: one **Save** button plus a secondary **"Download .zip"** on the
  saved slot (no separate "Save & download" flow).
- **Import replaces the current slot** (after a confirm) — the intent is one
  project at a time. A zip kept as a document is already the escape hatch if
  multi-project ever surfaces; that is not planned now.
- **Restore returns the exact saved state**; no silent re-derivation. If it
  cannot fully restore, it must report the reasons (missing dir/file,
  unsupported state version).
- **Explicit delete only** — no expiry timers on saved drafts or their
  assets. Orphaned, never-saved staging keeps the existing GC.
- **Scope v1**: `e1`, `e1variant`, `pairs`.
- Drafts survive across ltxq versions best-effort; the restore report says
  when it could not.

### Deferred (decide at implementation time)

- **Orphaned staging GC**: keep the existing timer for never-saved sessions
  (likely, since unsaved work is intentionally volatile) or disable GC
  entirely — revisit when it matters.
- Replace-slot confirmation wording ("start new draft — the current one will
  be overwritten" vs keeping the old draft until the new save).

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
