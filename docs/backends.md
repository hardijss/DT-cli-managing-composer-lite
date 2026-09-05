# Backends: oneshot vs serve

Every ltxq job runs on a host through one of two *backends* — execution
strategies that wrap the same `tod-dt-cli` binary in different process
models. The backend is chosen per job with this precedence:

1. **job backend** — `add --backend serve|oneshot`, or the Backend dropdown
   on the web form (empty = inherit)
2. **host default** — `backend:` key on the host in `hosts.yaml`
   (empty = `oneshot`)

Both backends work identically over ssh and `local` transport; the only
difference there is how files move (tar-over-ssh/rsync vs local copy).
The web dashboard's host panel shows each host's backend default and —
for serve — whether the worker is up.

## At a glance

| | oneshot | serve |
|---|---|---|
| Process model | fresh `tod-dt-cli generate` process per job | one persistent `tod-dt-cli serve` worker on the host |
| Setup | none (default) | `ltxq.py serve-start <alias>` once, before queueing serve jobs |
| Model load | paid on **every** job | paid once at worker boot; model stays resident |
| Job start latency | CLI startup + model load before step 1 | near-instant after the first job |
| RAM between jobs | released when the process exits | pinned by the loaded model until the worker stops |
| Cancel | remote kill — stops within seconds | no per-job kill; the request runs to completion and the output is discarded |
| Progress source | job's `log.txt` (PTY-wrapped so % streams) | `worker.log` tail from a recorded byte offset + `DT_WORKER_EVENT`s |
| Failure isolation | crash affects only that job | worker death fails the in-flight job |
| Concurrency | each job is its own process; `max_jobs` processes = `max_jobs` model copies in RAM | the worker executes requests one at a time (FIFO order) |

**Rule of thumb**: occasional or long single renders, machines that also do
interactive work, or the need to hard-cancel → `oneshot`. Batches, chains,
regen/tweak loops, or a dedicated render box → `serve`.

## oneshot (default)

Each job is fully self-contained: nothing persists on the host between jobs.

**Setup** — none. It's the fallback backend everywhere. Tuning knobs are the
global `use_pty` (default `true`, wraps the CLI in `script` so progress
percent streams to the log) and the usual `poll_secs` / `stall_secs`.

**Lifecycle** (what the engine does per job):

1. Writes `runner.sh` into the local job dir — the exact CLI invocation:
   model path, `config.json`, `prompt.txt`, `out.<ext>`, asset flags, and
   the global switches (`--no-download-missing`, `--disable-preview`,
   `--offline`).
2. Uploads runner + inputs to `<home>/<remote_root>/jobs/<id>/`
   (tar-over-ssh, or a local copy).
3. Pre-checks the host *before* burning the upload: CLI binary exists and is
   executable, `models_dir` present.
4. Launches `nohup sh runner.sh > launch.log` and records the PID.
5. Polls every `poll_secs`: parses log tail for percent/steps, watches
   `exit_code`; stall beyond `stall_secs` → `suspect`.
6. On exit 0: collects outputs (rsync / local copy), verifies count +
   sha256, writes `metadata.json`, cleans the remote dir.

**Performance profile** — every job pays CLI startup plus the full model
load from `models_dir` before the first generation step runs. For short
jobs that load dominates wall-clock; for long video renders it's amortized.
RAM is released the moment the process exits, and a crashed/killed job can't
affect anything else on the host. Running `max_jobs` oneshot jobs
concurrently means `max_jobs` independent model copies in RAM — scale with
care on smaller machines.

## serve (persistent warm worker)

The worker is a single long-lived `tod-dt-cli serve` process whose stdin is
a named FIFO; ltxq submits jobs by writing newline-delimited JSON requests
(`{"id": …, "args": […]}`) into it. A tiny holder process (`sleep`) keeps
the FIFO write-end open so a submission never blocks.

**Setup**:

```sh
./venv/bin/python ltxq.py probe <alias>        # pin models_dir (serve-start does this too)
./venv/bin/python ltxq.py serve-start <alias>  # boots worker, waits up to 30s for its "ready" event
```

- The worker lives on the host in `~/genwork/worker/` (`fifo`, `worker.pid`,
  `holder.pid`, `worker.log`); logs are rotated before each boot and at 10 MB
  during idle periods.
- Queue jobs with backend `serve` (dropdown, `--backend serve`, or host key
  `backend: serve`). If a serve job reaches dispatch while the worker is
  down, it is **requeued with a note** telling you to run `serve-start` —
  the queue never boots the worker on its own (the one exception is the
  idle-policy auto-restart below).
- `serve-stop <alias>` writes a shutdown request into the FIFO; it refuses
  while a serve job is in flight unless `--force`.
- `doctor` and the host panel report worker liveness.

**Request-path constraint**: the worker is not started inside the job dir,
so path arguments (model, config, prompt, output, assets) must be
**absolute on the host**. Oneshot resolves bare names against the job dir;
for serve, ltxq rewrites `@asset@` tokens to absolute job-dir paths and
rejects the launch if a path flag would be non-absolute (job fails fast
with a `ValidationError`-style note).

**Lifecycle** (per job): verify worker alive → record the current
`worker.log` byte offset → upload inputs → write the request into the FIFO →
poll tails `worker.log` from that offset, tracking percent/steps and the
`started` / `completed` / `failed` worker events tagged with the job id →
collect on `completed` + `Wrote:` lines. If the worker dies mid-job, the job
fails with a note pointing at the `worker.log` tail.

**Performance profile** — the first job after boot pays the model load;
every subsequent job skips it and starts generating almost immediately.
That's the whole win, and it compounds: a 10-job batch or a chain saves the
load time ten times. The price is that the loaded model's RAM stays pinned
between jobs, so an idle serve host is "holding" memory until the worker is
stopped.

**Interaction with the idle policy** (per-host `idle_policy.enabled`): when
dispatch is paused because the host got busy (load threshold / heavy
processes) for longer than `pause_release_secs` (default 300 s), ltxq sends
the worker a shutdown to free model RAM — and automatically runs
`serve-start` again when the host goes idle with serve jobs still queued.
Without an idle policy the worker runs indefinitely at your discretion
(`serve-start` / `serve-stop`).

## Mixing backends on one host

Both backends can be used on the same host, and per-job overrides mean a
mostly-serve host can still take a one-off oneshot job. Two cautions:

- **RAM**: a serve worker holding a loaded model plus a concurrent oneshot
  job loading its own copy can exceed what the machine offers — on
  RAM-constrained hosts avoid mixing while jobs are in flight.
- **Cancel semantics differ per job**: oneshot jobs kill on cancel; serve
  jobs run to completion and are discarded. The queue marks this in the job
  note at cancel time.
