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
  new-seed, frames (`--frames` override), backend/host/ext, image &
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

### CLI
- `add/ls/run/cancel/regen/check/probe/models/stage/reconcile/
  serve-start/serve-stop/doctor/ui`
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
- Chain continuation assumes serial execution: with several hosts dispatching
  in parallel, two flagged jobs claimed in the same instant both continue from
  the same predecessor.

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
