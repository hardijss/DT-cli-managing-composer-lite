# HTTP API

**API version: 1.5** (see [Versioning](#versioning) at the end).

The dashboard, the native macOS app, and external job-composition tools are
all clients of the same localhost API served by `ltxq.py ui` (Flask, in
`server.py`). This document is the contract between them; changes are
additive (new optional fields/endpoints) so older clients keep working.

## Conventions

- **Base URL:** `http://127.0.0.1:<port>` (default port 8765). The server
  binds to loopback only; there is no authentication.
- **CSRF guard:** cross-origin browser POSTs (requests carrying an `Origin`
  header whose host differs from the request host) are rejected with 403.
  Non-browser clients (curl, scripts) send no `Origin` and are unaffected.
- **Job ids:** 10-char lowercase hex (`[0-9a-f]{10}`). Endpoints that take a
  `<jid>` validate the format and return 404 otherwise.
- **Errors:** JSON `{"error": "..."}` with a 4xx status; success is 200 with
  a JSON body unless noted.
- **Batches:** jobs may carry an optional free-text `batch` label (normalized
  to `[A-Za-z0-9._-]`, max 48 chars). It is metadata surfaced in the UI and
  the scope for the "continue from previous generation" feature: a chained
  job with a batch attaches the last frame of the newest finished job **in
  the same batch**; without a batch the scope is all finished jobs.

## Queue state

### `GET /api/state`

The polling endpoint (dashboard polls every 2s).

```json
{"jobs": [ {job}, ... ],          // newest first, limit 50
 "hosts": [ {"alias": "...", "models_dir": "...", "max_jobs": 1,
             "backend": null, "idle": {...}, "paused": false,
             "release_at": null, "conn": "ssh|local",
             "worker_alive": true, "in_flight": 0, "queued": 0,
             "cli_path": "..."} ],
 "merge": [ {"pos": 1, "jid": "a1b2c3d4e5", "name": "008",
             "gone": false, "video": "/path/to/out.mov"} ],   // merge tray, ordered
 "merge_run": {"running": false, "name": null, "out": null, "pct": 0,
               "error": null, "finished_at": null, "results": [...]},
 "engine": true,                  // engine loop running
 "now": 1757050000}
```

Job object fields include: `id, created_at, name, parent_id, host, model,
prompt, config_text, status, local_dir, local_out, pct, pct_ts, started_at,
finished_at, exit_code, total_gen_s, steps, step_avg_s, step_median_s,
assets (JSON string: [{flag, file, local}]), extra_args (JSON string:
[token, ...]), num_frames, fps, ext, backend, cur_off, log_tail, note,
chain, batch` plus computed `bar` (percent) and `elapsed` (seconds).
Statuses: `queued, uploading, running, collecting, suspect, cancelling,
cancelled, done, failed`.

### `GET /api/status`  *(since API 1.2)*

Read-only diagnostics snapshot for the dashboard's gear-icon overlay
("Settings & server status"): the components the server actually uses
(presence, resolved path, version), the effective `hosts.yaml` settings, and
an engine + per-host summary. Computed per request; tool versions are cached
for 10 minutes.

```json
{"server": {"port": 8765, "engine": true, "poll_secs": 10,
            "repo": "/path/to/ltxq", "db": "/path/to/ltxq.db",
            "db_bytes": 393216, "jobs": {"done": 79, "failed": 18},
            "git": "e3ca75d", "conf_path": "/path/to/hosts.yaml",
            "conf_error": null, "templates": 2},
 "components": [{"name": "ffmpeg", "present": true,
                 "path": "/opt/homebrew/bin/ffmpeg",
                 "version": "ffmpeg version 6.1.4 ..."},
                {"name": "ffprobe", ...},
                {"name": "python", "present": true, "path": "...",
                 "version": "3.14.3"},
                {"name": "flask", "present": true, "path": null,
                 "version": "3.1.3"},
                {"name": "pyyaml", ...}],
 "settings": {"cli_path": "~/tod-dt-cli", "poll_secs": 10, "stall_secs": 900,
              "remote_root": "genwork", "movies_dir": "~/Movies/generations",
              "use_pty": true, "keep_remote": false, "offline": false,
              "disable_preview": true, "download_missing": false},
 "hosts": [{"alias": "ltx-a", "enabled": true, "backend": null, "max_jobs": 1,
            "models_dir": "...", "conn": "ssh", "worker_alive": true,
            "cli_path": "~/tod-dt-cli"}]}
```

`conf_error` is non-null when `hosts.yaml` is missing (settings/hosts may then
be empty); `templates` counts `templates/*.json` presets.

## Submitting jobs

### `POST /api/add` — queue a new job (multipart/form-data)

Common form fields (all optional unless noted):

| field | meaning |
|---|---|
| `prompt` | required, prompt text |
| `model` | required, model id (no path separators) |
| `config_json` | JSON overlay applied on top of the base config (per-model template `templates/<model>.json`, else the last completed job's config) |
| `name` | display name; defaults to the first prompt words |
| `batch` | optional batch label (see above) |
| `host` | pin to a host alias; empty = any free host |
| `seed` / `new_seed` | explicit seed, or generate a fresh random one |
| `frames` | engine frame-count override, passed through as `--frames <n>` (overrides the config's `numFrames`); empty = config default |
| `ext` | `mov` (default), `mp4`, `png` |
| `backend` | `oneshot` or `serve`; empty = host default |
| `chain` | `first_frame` or `image` — continue from the previous finished job (batch-scoped when `batch` is set) |
| `extra_arg` | raw CLI tokens, one per line; use `@filename@` to reference an uploaded file — it is rewritten to the file's remote path at launch |

File parts: `image`, `audio`, `input_video`, `first_frame`,
`middle_frame`, `last_frame` (single file each, mapped to the matching
`--flag`), plus `upload` (repeatable, plain attachments — pair with
`extra_arg` tokens using `@filename@`). Multipart bodies are capped at 4 GiB.

Response: `{"jid": "<id>", "warnings": "..."}` (warnings may be empty).

Batch submission: POST once per job. Jobs carrying the same `batch` label
group in the UI, and dispatch order follows submission order
(`created_at, rowid`).

### `POST /api/regen/<jid>` (form)

Re-create a past job: same fields as `/api/add` except `prompt` (reused);
only overridden fields need to be sent. `batch` defaults to the original
job's batch.

## Per-job

- `GET /api/job/<jid>` → `{"job": {job}}`
- `GET /api/view/<jid>` → the collected video file (only for `done` jobs)
- `GET /api/asset/<jid>/<name>` → one of the job's input files
- `POST /api/cancel/<jid>` — request cancellation
- `POST /api/delete/<jid>` (JSON body `{"output": true}` to also delete the
  collected output directory) — removes the job row and work files; refused
  while the job is active

## Staging (frame extraction, re-attach)

- `POST /api/extract` (JSON `{"jid": "...", "frame": "12|first|last"}`) →
  extracts a frame of a finished job's video into the staging area →
  `{"staged": "stage_...", "frame": 12, "preview": "/api/stage/stage_..."}`
- `GET /api/stage/<name>` → a staged file (name must start with `stage_`)
- `POST /api/stage_from/<jid>/<name>` → copy a past job's input file into
  staging → `{"staged": "stage_...", "preview": "..."}`

Staged files are attached to a new job by sending `staged_<slot>` (or
`staged_upload`) form fields on `/api/add`.

## Merge tray *(since API 1.5)*

One implicit ordered playlist of finished jobs' collected videos, merged
locally with ffmpeg (stream-copy concat) on demand. All state is also
embedded in `GET /api/state` as `merge` (ordered items; `video` is null when
the collected file is missing, `gone` true when the job row was deleted) and
`merge_run` (current/last merge: `running`, `name`, `out`, `pct`, `error`,
`results` — the last five completed merges). Tray contents persist in the
`merge_items` db table. Every mutation publishes a `merge` SSE event.

- `POST /api/merge/add` (JSON `{"jid": "..."}`) — append a done job's video;
  refuses non-done jobs or missing video files
- `POST /api/merge/remove` (JSON `{"pos": 1}`) — delete an entry (remaining
  positions renumber)
- `POST /api/merge/move` (JSON `{"pos": 1, "dir": "up"|"down"}`) — swap with
  the neighbor; refuses moving past the ends
- `POST /api/merge/clear` — empty the tray
- `POST /api/merge/run` (JSON `{"name": "my-montage"}`) — start a merge:
  ffprobe-validates every clip (all must share codec/resolution/fps — there
  is no re-encode fallback), then concatenates with `-f concat -c copy` into
  `movies_dir/merges/<name>.<ext>` (extension follows the first clip;
  a numeric suffix avoids overwriting). Runs in a background thread —
  progress via `merge_run` in `/api/state` or the `merge` SSE event.
  → `{"ok": true, "name": "<output filename>"}`
- `GET /api/merge/file/<name>` — download a merge output (name is flattened
  to a basename; outputs live only under `movies_dir/merges/`)

## Hosts

- `GET /api/models/<alias>` → `{"models": [{"model", "name", "downloaded"}],
  "error": null, "raw": "..."}` — registry snapshot of downloaded models
- `POST /api/queue/<alias>/pause` (JSON `{"release_at": <epoch>}` optional)
  — pause dispatch to a host, optionally with a scheduled release
- `POST /api/queue/<alias>/resume` — resume dispatch immediately
- `POST /api/hosts/reload` *(since API 1.3)* — validate the on-disk
  `hosts.yaml` and apply it (re-sync the hosts table). Returns
  `{"path": "<effective hosts.yaml path>", "hosts": [{"alias", "dest",
  "enabled"}]}` on success; on a parse or validation error returns 400 with
  `{"error", "path"}` and leaves the running config untouched. The engine
  also watches the file and re-reads it automatically within one poll
  interval of a save, so this endpoint is for immediate feedback and error
  reporting (used by the macOS app's Settings → Apply Changes).

## Minimal client examples

```sh
# queue one job into a batch
curl -s -F prompt="a fox in snow" -F model=LTX-2.3 -F batch="fox-v1" \
     -F config_json='{"steps": 9}' -F image=@start.png \
     -F chain=first_frame http://127.0.0.1:8765/api/add

# batch of three: submit in order, same batch label
for v in a b c; do
  curl -s -F prompt="variant $v" -F model=LTX-2.3 -F batch="fox-v1" \
       http://127.0.0.1:8765/api/add
done
```

The CLI (`python3 ltxq.py add ...`) is the other supported submission path
and accepts the same fields (`--batch`, `--chain`, `--upload`,
`--extra-arg "@file@"`, ...); use whichever matches the client.

## Event stream (SSE)

### `GET /api/events`  *(since API 1.1)*

A [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
stream (`Content-Type: text/event-stream`) pushing state changes as they
happen, so clients don't have to poll `/api/state`. Polling remains fully
supported — this endpoint is additive and the two can be combined (SSE for
push, `/api/state` for reconciliation).

Behavior:

- **No replay.** Events are fire-and-forget; there is no history or
  `Last-Event-ID` support. On every (re)connect the stream first emits a
  `hello` event containing a full `/api/state` snapshot, so a client that
  reconnects simply re-bootstraps from it. The stream also emits `retry:
  2000` so browsers reconnect automatically.
- **Keep-alive.** A `: ping` comment is written every ~15 s when idle;
  treat a missing ping as a dead connection (EventSource does this via its
  own timeout logic; hand-rolled clients should too).
- **Order.** Events are delivered in the order they were published, per
  connection. Progress ticks (`pct` updates) arrive as `job` events, so a
  running job streams live progress.

Event types (`event:` field / `data:` JSON):

| event | data | when |
|---|---|---|
| `hello` | same JSON as `GET /api/state` (`{"jobs", "hosts", "engine", "now"}`) | first event after every connect |
| `job` | `{"job": {job}}` — the same object `GET /api/job/<jid>` returns | every committed job change (status, `pct`, `note`, ...), including creation via `/api/add` and the `queued → uploading` dispatch claim |
| `job_removed` | `{"id": "<jid>"}` | the job was deleted from the queue |
| `host` | `{"hosts": [...]}` — the same hosts array as `GET /api/state` | host health changed: worker liveness, queue depths, or pause state (pause/resume publishes immediately) |
| `engine` | `{"engine": true\|false}` | the engine loop started or stopped |
| `refresh` | `{"reason": "overflow"}` | this consumer fell behind and its buffer was dropped — re-fetch `GET /api/state` |

Example session:

```
$ curl -N http://127.0.0.1:8765/api/events
retry: 2000

event: hello
data: {"jobs": [...], "hosts": [...], "engine": true, "now": 1757080000}

event: job
data: {"job": {"id": "a1b2c3d4e5", "status": "running", "pct": 12, ...}}

event: host
data: {"hosts": [{"alias": "ltx-a", "worker_alive": true, ...}]}
```

## Versioning

- **1.0** — the original REST endpoints (state, add/regen, per-job, staging,
  hosts) as first documented.
- **1.1** — adds `GET /api/events` (SSE). No existing endpoint changed.
- **1.2** — adds `GET /api/status` (diagnostics snapshot). No existing
  endpoint changed.
- **1.3** — adds `POST /api/hosts/reload` (validate + apply hosts.yaml).
  No existing endpoint changed.
- **1.4** — adds the optional `frames` form field on `/api/add` and
  `/api/regen/<jid>`: the engine's `--frames <n>` frame-count override
  (the CLI `add`/`regen` commands gained a matching `--frames` flag).
  No existing endpoint changed.
- **1.5** — adds the merge tray (`/api/merge/*`, `GET /api/merge/file/<name>`)
  and the `merge`/`merge_run` fields on `/api/state` plus a `merge` SSE
  event. No existing endpoint changed.

Changes are additive: new endpoints, new optional request fields, new
response fields. Breaking changes would bump the major version and be
announced here; existing clients (the web dashboard, the native app) are
updated in the same commit that changes the API.
