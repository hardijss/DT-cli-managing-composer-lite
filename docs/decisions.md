# Decisions log

Key choices made during the build, and why. Ordered roughly chronologically.

## 1. PTY for oneshot, plain pipe for serve
`draw-things-cli` prints progress with `\r`-style updates that only flush
through a terminal. Oneshot jobs wrap the CLI in `script` (PTY) so percent
streams to `log.txt`. The serve worker initially reused the PTY wrapper — but
`script` mangles the worker's newline-delimited JSON stdin protocol, so the
worker never signaled ready. Decision: serve always launches with plain-pipe
stdin; PTY is oneshot-only (`use_pty` config, per launch type).

## 2. Serve worker = FIFO + long-lived holder process
The remote CLI's serve mode reads requests from stdin. To keep stdin open
across ssh sessions, a FIFO is held open by a `sleep 100000` process
(`holder.pid`). Ugly but robust: requests are written with a one-line
`cat req > fifo`, and the worker survives ssh disconnects via `nohup`.

## 3. Poll by tailing logs, not a daemon
No agent is installed on render hosts. All state comes from SSH one-liners:
exit_code/pid/log tail for oneshot; `worker.log` byte offset (`cur_off`) for
serve. This keeps hosts zero-footprint — any Mac with ssh + the CLI works.
Cost: remote shell quirks (see #7) and polling latency bounded by
`poll_secs`.

## 4. SQLite, WAL, single engine
State is one file (`ltxq.db`), WAL + `busy_timeout` so the web server and
engine can share it. The web UI runs the engine **in-process** (daemon
thread) rather than as a separate service: avoids double-dispatch races
without distributed locking. A `flock` on `engine.lock` still guards against
`run` + `ui` together — added after a forgotten background `run` actually
caused a double-dispatch that OOM-killed two renders.

## 5. Atomic dispatch claims
Dispatch does `UPDATE jobs SET status='uploading' WHERE id=? AND
status='queued'` and checks rowcount before launching — cheap insurance
against two dispatchers (or an add racing the loop) launching the same job.

## 6. hosts.yaml as the server registry
Connection type (ssh/local), address, ssh options, per-host CLI binary and
models dir, job limits, idle policy — all declarative in yaml, order =
routing preference. Rationale: adding a server is a config edit, not code;
per-host `cli_path` exists because CLI versions/capabilities differ per
machine.

## 7. Remote commands are shell-portable or dead
The remote login shell is zsh; `$((N)+1)` inside command strings was a parse
error that silently killed serve polling. Decision: compute values in Python
wherever possible, keep remote snippets boring, and have `doctor` test remote
shell arithmetic on every host.

## 8. Idle-consumer policy (local workload wins)
Render hosts are also editing machines. Before dispatch, the engine checks
load + a heavy-process list (Premiere etc.). Busy → pause dispatch with a
reason; busy for `pause_release_secs` → shut the serve worker down to free
pinned model RAM; idle again → restart it. In-flight renders are never
killed — aborting mid-render wastes the work already spent.

## 9. Web form config = overlay on a base config
`config_json` from the UI merges over a base (per-model template, else the
last completed job's config). Keeps the form small while full configs remain
available via the JSON textarea.

## 10. `@file@` placeholder for keyframe args
`--keyframe path:index` needs a remote path that differs per backend
(bare name for oneshot, absolute job-dir path for serve). Assets are copied
into the job dir; the extra-arg token carries `@name@` which `resolve_extra()`
rewrites at launch time per backend. Regex-based because the placeholder is
embedded mid-token (`@kf.png@:12:0.8`).

## 11. Staging area for UI attachments
Web uploads and ffmpeg-extracted frames land in `jobs/_tmp/stage_*`; names
are validated (prefix + parent dir) before the API will attach or serve them.
Enables "extract frame from finished video → attach to next job" without the
browser re-uploading anything.

## 12. Load > delete for history
Delete removes the DB record and `jobs/<id>/` work files but **keeps**
`~/Movies/generations` output (with its `metadata.json`). Videos are the
product; prompts/configs/inputs are re-loadable only while the record exists,
so Load-before-Delete is the documented safety net.

## 13. Regen became Load
Regen-as-submit duplicated a job verbatim — near-useless for a stochastic
generator. Replaced with Load: populate the form (prompt, config, model,
backend, inputs re-attached via staging) for editing before submission.
