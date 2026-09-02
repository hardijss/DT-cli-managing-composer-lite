# Architecture overview

## Folder structure

```
ltxq/
├── ltxq.py            # engine + CLI: db schema, ssh/local transport, dispatch,
│                      # launch (oneshot/serve), polling, collect, all commands
├── server.py          # Flask app (web UI API), engine thread, idle policy,
│                      # ffmpeg frame extraction, staging area
├── static/index.html  # single-page dashboard (vanilla JS, 2s polling)
├── hosts.yaml         # server registry + global + per-host config
├── ltxq.db            # SQLite state (WAL mode; -shm/-wal side files)
├── engine.lock        # flock guarding single engine (run or ui)
├── jobs/<id>/         # per-job work dir: prompt.txt, config.json, runner.sh,
│                      # copied input assets, <id>.req (serve requests)
├── jobs/_tmp/         # staging: web-form uploads + ffmpeg-extracted frames
│                      # (stage_* files, referenced by /api/stage)
├── templates/         # optional per-model default configs (<model>.json)
└── venv/              # Python env (flask, pyyaml)
```

On each **ssh render host** (paths derived from `$HOME/<remote_root>`):
```
~/genwork/worker/     # serve worker: fifo, worker.pid, holder.pid, worker.log
~/genwork/jobs/<id>/  # per-job: runner.sh (oneshot), inputs, out.mov, log.txt,
                      # pid, cli_pid, exit_code
```

## Data flow

1. **Submit** — CLI `add` or `POST /api/add`: job row (status `queued`) +
   `jobs/<id>/` dir with prompt/config/copied assets.
2. **Dispatch** — engine loop (thread in `ui`, or `run`): for each enabled
   host (yaml order = preference), idle-policy gate → free capacity check →
   atomic claim (`queued→uploading`) → `launch`:
   - *oneshot*: upload files, `nohup sh runner.sh` remotely (PTY-wrapped CLI
     so progress streams to log.txt).
   - *serve*: ensure worker alive (`serve-start` boots it: plain-pipe stdin
     from FIFO, log rotated), upload inputs, write the JSON request to the
     FIFO, record the worker.log offset (`cur_off`).
3. **Poll** (every `poll_secs`): oneshot parses log tail/exit_code; serve
   tails worker.log from `cur_off` for percent/steps/`DT_WORKER_EVENT`s.
   Terminal states: `collecting` (exit 0 + `Wrote:` paths), `failed`,
   `cancelled`, `suspect` (stall).
4. **Collect** — rsync (ssh) or copy (local) outputs to
   `~/Movies/generations/<slug>-<id>/`, verify count + sha256, write
   `metadata.json`, mark `done`, clean remote dir.
5. **Dashboard** — polls `/api/state` every 2s; engine publishes per-host
   health (worker alive, queue depth, idle status) and idle-gate decisions.

## External services / dependencies

- **draw-things-cli** (`tod-dt-cli`) on each render host — actual generation;
  per-host `cli_path` allows different versions. Models resolved from a pinned
  per-host `models_dir`.
- **OpenSSH** — transport for ssh hosts (BatchMode, ControlMaster mux sockets
  in `.ssh_mux/`, 10-min persist); credentials via standard ssh config/keys,
  extra options per host via `ssh_opts`.
- **rsync / tar** — collection and upload.
- **ffmpeg / ffprobe** — frame extraction from finished videos (local system).
- **SQLite** — single-file state, WAL, `busy_timeout=5s`.

## Concurrency model

One engine at a time, enforced by `flock` on `engine.lock` (`run` and `ui`
both acquire it). Within the engine, dispatch uses an atomic
`UPDATE…WHERE status='queued'` claim so a web `add` racing the loop can't
double-launch. The Flask server and engine share the process; DB access is
short-transaction, WAL-mode.
