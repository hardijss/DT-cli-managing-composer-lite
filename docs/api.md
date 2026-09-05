# HTTP API

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

## Hosts

- `GET /api/models/<alias>` → `{"models": [{"model", "name", "downloaded"}],
  "error": null, "raw": "..."}` — registry snapshot of downloaded models
- `POST /api/queue/<alias>/pause` (JSON `{"release_at": <epoch>}` optional)
  — pause dispatch to a host, optionally with a scheduled release
- `POST /api/queue/<alias>/resume` — resume dispatch immediately

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
