# ltxq — remote LTX-2 render queue

`ltxq` queues LTX-2 (draw-things-cli) video/image generation jobs, runs them on
one or more render hosts over SSH (or locally), tracks live progress, and
collects finished outputs to your Movies folder. A built-in web dashboard
controls the queue: submit jobs, watch live progress bars, cancel, re-load past
jobs, extract frames from finished videos with ffmpeg, and manage history.

## What it does

- **Queue & dispatch**: `add` a job (model, prompt, config, optional keyframe/
  image assets); an engine loop dispatches to the first enabled host with free
  capacity (`max_jobs` per host), respecting an idle-consumer policy.
- **Two backends per host**:
  - `oneshot` — upload a `runner.sh` + inputs, run `tod-dt-cli generate` under
    a PTY (so progress % streams), poll, collect.
  - `serve` — a persistent worker (`tod-dt-cli serve`) reads newline-delimited
    JSON requests from a FIFO; model stays loaded between jobs (faster starts,
    RAM stays pinned).
- **Live progress**: percent, step counts/timing, phase; stall detection
  (`stall_secs`); cancel (oneshot kills the process; serve runs to completion
  and discards).
- **Collection**: outputs rsynced (or copied for local hosts) to
  `~/Movies/generations/<name>-<id>/` with `metadata.json` (full job record +
  sha256 + size). Remote work dirs cleaned unless `keep_remote`.
- **Web UI** (`ltxq.py ui`, http://127.0.0.1:8765): dashboard + queue control
  in one process (engine runs in a daemon thread; a lockfile prevents two
  engines from double-dispatching).
- **Idle-consumer policy**: hosts doing local work (Premiere etc.) take
  precedence — dispatch pauses on high load or heavy processes, and the serve
  worker is auto-stopped after sustained busyness to free model RAM, restarted
  when idle.
- **Multi-host**: `hosts.yaml` is the server registry (ssh or local transport,
  address, credentials via ssh config/opts, per-host CLI binary & version,
  models dir, job limits, per-host idle policy).

## Tech stack

- Python 3.14 (stdlib: sqlite3, argparse, subprocess, re, …)
- PyYAML, Flask (in `venv/`)
- One HTML/JS/CSS page (no framework, no build step) — vanilla fetch polling
- SSH (OpenSSH with ControlMaster multiplexing), rsync, tar-over-ssh, ffmpeg/ffprobe
- draw-things-cli (`tod-dt-cli`) on each render host

## Run

```sh
cd ~/ltxq
./venv/bin/python ltxq.py doctor          # self-checks (db, ssh, worker, flask)
./venv/bin/python ltxq.py ui              # dashboard + engine → http://127.0.0.1:8765
# or headless:
./venv/bin/python ltxq.py run             # engine loop only
```

Common CLI commands: `add`, `ls`, `check <id>`, `cancel <id>`, `regen <id>`,
`probe [alias]`, `models <alias>`, `stage <alias> <model>`, `serve-start/-stop
<alias>`, `reconcile`, `doctor`, `ui`.

Configuration lives in `hosts.yaml` (see the comments there). Jobs and their
inputs are stored under `jobs/<id>/`; state in `ltxq.db` (SQLite, WAL).

Docs: [architecture.md](architecture.md) · [environment.md](environment.md) ·
[features.md](features.md) · [decisions.md](decisions.md)
