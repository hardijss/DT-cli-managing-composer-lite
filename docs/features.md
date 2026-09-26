# Features, known issues, TODOs

## Feature list

### Queue & execution
- Multi-host registry (ssh or local transport), per-host CLI binary/version,
  models dir, job limits, connection options
- Routing: yaml order = preference; unpinned jobs go to first host with
  capacity; per-job host pin
- Backends: `oneshot` (PTY progress streaming) and `serve` (persistent worker,
  model stays loaded, FIFO JSON protocol)
- Live progress: percent, steps, avg/median step time, total time; stall →
  `suspect`
- Cancel: queued (instant), oneshot (remote kill), serve (mark; runs to
  completion, result discarded)
- Idle-consumer policy: load + heavy-process gate on dispatch; auto
  serve-stop after sustained busyness, auto restart when idle
- Collection with verification (count, sha256), `metadata.json` per output,
  remote cleanup
- Bad-input handling: bad model fails fast (exit code surfaced), argparse
  validation errors annotated, upload failures requeue
- Audio input support: `--audio` attachment accepted on the web form, carried
  through staging and regen (Load) like other assets
- Generation chain: jobs flagged with `chain` (first_frame/image slot) resolve
  at launch — the last frame of the newest finished render is extracted
  (ffmpeg) and attached to that slot, so queued batches continue from each
  other; missing source or a manually-filled slot degrades to running without
  the frame (noted), never failing the render. Jobs carrying a `batch` label
  scope the lookup to the previous finished job *in the same batch*;
  unbatched jobs use all finished generations
- FFLF preflight: oneshot jobs carrying both `--first-frame` and
  `--last-frame` (chain-resolved included) get one extra `generate
  --fflf-preflight` invocation at dispatch — the engine validates the frame
  inputs and resolves indices without rendering (full output in the job dir's
  `preflight.txt`, summary in the job note). A failing check fails the job
  with the engine's message instead of burning a render; transport errors
  skip the check and render anyway. Serve-backend jobs and first-frame-only
  jobs (the engine preflight validates the pair) are not preflighted
- Batch groups: jobs may carry an optional `batch` label (CLI `--batch`, form
  "Batch" field) — surfaced as a 📦 badge, kept across regen, and used as the
  chain scope above; dispatch order within a submission burst follows
  submission order (`created_at, rowid`)
- Housekeeping: staging artifacts in `jobs/_tmp/` older than 24h pruned;
  remote `worker.log` rotated when it exceeds 10MB (during idle periods) —
  runs in the engine loop and `run`/`reconcile`

### Web UI (http://127.0.0.1:8765)
- Dashboard: active/queued cards (bar, elapsed, phase, note, log tail),
  cancel
- Add form: per-host model dropdown, prompt, config JSON overlay, seed /
  new-seed (a negative seed means random), frames (`--frames` override), backend/host/ext, image &
  input-video uploads
- LTX keyframes: first/middle/last-frame slots, repeated keyframes with
  `index[:strength[:attention]]`, default strengths
- ffmpeg frame extraction: pick a finished video + frame #/first/last →
  staged thumbnail → attach as any slot or keyframe
- Chain continuation checkbox: auto-attach the previous generation's last
  frame as first-frame/image (resolved when the job launches, so batches
  chain; scoped to the Batch field when set); state remembered between
  submissions; ⛓ badge on chained jobs, 📦 badge on batched jobs
- History: status badges, input thumbnails, View (opens video), Load
  (populates the whole form incl. re-attaching past inputs), Delete
  (record + work files; collected video kept)
- Host panel: connection type, CLI path, in-flight/queued counts, worker
  up/down, idle-pause reason, per-host dispatch pause/resume
- Per-host queue pause: ⏸ Pause / ▶ Resume buttons, optional timed release
  (formats like `22:00`, `+2h`, or `2026-09-02 22:00`) with a live countdown
  banner; pauses dispatch only — in-flight jobs keep running
- Merge tray: one ordered playlist of finished jobs' collected videos — ➕ on
  done history rows appends, ↑/↓ reorders, ✕ removes, Clear empties; "Merge…"
  concatenates locally with ffmpeg (stream-copy, `-f concat -c copy`) into
  `movies_dir/merges/<name>.<ext>` with a live progress bar and a download
  link. Inputs are ffprobe-validated first (all clips must share codec /
  resolution / fps — no re-encode fallback in v1); missing artifacts disable
  the button. Runs as a local subprocess in the server process — safe to use
  while the queue renders
- Audio batch form: pick or drop a segment folder (or paste a server-side
  path) and queue one job per `.wav` with the same frame/prompt/grid rules as
  `add-batch` — model/host/ext/seed/config-overlay/batch-label fields, shared
  fallback prompt, non-grid policy selector, visual-continuity checkbox
  (previous segment's last frame as `--image`), a start-image dropzone for
  segment 1, and a "Copy from New job" button (model, host, ext, seed,
  config overlay, prompt); validation failures list every offending segment
  and queue nothing
- **Pairs batch panel**: parse a stills folder (drop/pick or a server-side
  path) into an editable pair sequence — the pair list is the editable truth
  (reorder ↑/↓, delete with a seam warning, append-and-move inserts, assign
  stills from a palette to empty slots, inline per-pair prompts, per-pair
  length snapped to the chosen model's grid); extra folders append their
  parsed pairs, single images join the palette; changing model/host re-plans
  the derived layer and keeps the edited sequence; the session autosaves and
  survives a page refresh; queue re-validates server-side. Runs the same
  planner as `add-pairs` (`/api/pairs/*`, API 1.7)

### CLI
- `add/add-batch/ls/run/cancel/regen/check/probe/models/stage/reconcile/
  serve-start/serve-stop/doctor/ui`
- `add-batch <dir>`: audio-segment batch composer (see
  expansion-of-this-idea.md Idea 1) — one job per `.wav` in a segment dir,
  `numFrames` per job from the cutting helper's manifest (verbatim,
  cross-checked) or ffprobed from the wav and snapped onto the model's frame grid
  (8n+1 LTX/WAN, 17n+5 H3)
  (non-grid lengths default to round-up + silence pad; `--on-non-grid
  round-down|refuse` for the alternatives); prompts from same-basename
  `.txt` sidecars falling back to `--prompt-file`; batch label defaults to
  the manifest's Original File stem; the whole batch is validated before
  anything is queued; `--chain` gives visual continuity — segments 2..N
  get the previous segment's last frame attached as `--image` (a lone
  first-frame is invalid; it only pairs with `--last-frame`). Batch-chained
  jobs defer dispatch until the rest of the batch is done, so the batch
  self-serializes across any number of hosts; `--image` attaches a start
  image to segment 1 (the chain anchor), and same-named
  `<stem>.png/jpg/jpeg/webp` files next to the wavs are picked up as
  per-segment `--image` (a manual `--image` wins over the chain for that
  segment); same-named `<stem>.json` files are picked up as per-segment
  config overlays, validated upfront and cumulatively carried forward (sticky)
  until another `.json` sidecar appears
- `add-pairs <dir>`: keyframe-pair batch composer (expansion-of-this-idea.md
  Idea 2) — one independent job per consecutive still pair, `--first-frame
  still-k` + `--last-frame still-k+1` (both endpoints pinned, so adjacent
  renders join exactly at the shared still and the batch dispatches in
  parallel). **Model-first:** the model/host choice fixes fps, the frame grid
  (8n+1 LTX/WAN, 17n+5 H3) and the composition strategy (see
  cli-dialects.md — `frame_slots` on dtcustom, `canvas_ref` — repeated
  `--image`, end frame steered not pinned — on a pinned dtofficial host).
  Still order: `--order number` (default; embedded numbers are relative order
  only — gaps, non-padded and shuffled files fine; duplicate numbers and mixed
  prefixes refuse) or `--order name` (alphabetical). Per-pair companions by
  pair index: `prompt0001.txt`/`config0001.json`/`audio0001.wav` for pair 1
  (integer matching, orphans warn); length precedence per pair: explicit
  override > audio sidecar (ffprobe × fps, snapped, wav fitted) > config
  sidecar `numFrames` > `--frames-per-pair` > template `numFrames`; frames go
  into the config, never `--frames`. Empty prompts and one-sided pairs are
  warnings, not blockers: a pair with only a last still composes as a lone
  `--last-frame` (end pinned, start free — unvalidated against the engine),
  only a first still rides as `--image` (a lone `--first-frame` is invalid),
  a pair with no stills at all still refuses. The whole batch is validated
  before anything is queued. Unnumbered stills refuse with a rename hint.
- `doctor`: db self-checks (set_job single-row), ssh reachability, remote
  shell arithmetic, worker liveness, flask presence

## Known issues
 
- **Serve cancel is coarse**: no per-job kill; request runs to completion and
  the output is thrown away. Wastes a render slot on long jobs.
- **Idle policy load metric** is 1-min load average only; no GPU telemetry,
  and threshold is absolute (not scaled to cores).
- **UI progress %** is raw CLI percent (jumpy by design of the CLI); step
  counter exists but the bar doesn't derive from it.
- **History cap 50** in UI/`ls`; older jobs only visible via sqlite.
- Delete removes input copies with the record (by design, but easy to regret;
  Load-before-delete is the mitigation).
- Remote host going away mid-job → `failed: process gone`; no auto-retry.
- Chain continuation with a batch label is race-free: batch-chained jobs
  defer dispatch while a batch mate is still active, so each launches only
  after its predecessor finished (the batch runs serially). Unbatched chain
  jobs keep the global "most recent finished" scope, which still assumes
  serial execution: two claimed in the same instant continue from the same
  predecessor.

## TODOs / ideas

- Serve backend: per-job cancel via worker protocol (if the CLI ever supports
  it), or job-level timeout
- UI toggle for host `enabled`
- Retry/backoff for transient ssh failures; host "unreachable" state in panel
- Monotonic step-based progress bar; ETA from `step_avg_s`
- Per-host page or filters in history; pagination beyond 50
- Auth/binding choice if the UI ever needs to leave localhost
- Optional: routing hints beyond order (e.g. spill-to-second-host),
  cost/queue-aware scheduling
- Templates dir is empty — seed per-model default configs to make `add`
  work without `--config-file`
- Live sampling preview in the dashboard: the engine CLI emits preview frames
  during sampling; until ltxq renders them somewhere (active-job card), the
  `--disable-preview` flag is included with every `generate` invocation
  (hosts.yaml `disable_preview: true`, also the default) to save the
  bandwidth/pty churn. Implementing preview means capturing the pty/serve
  preview stream per job and pushing it over the SSE event stream — only then
  would the flag default flip
