# ltxq

![License](https://img.shields.io/badge/license-Apache--2.0-blue) ![Python](https://img.shields.io/badge/python-3.14-blue) ![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)

`ltxq` queues LTX-2 (draw-things-cli) video/image generation jobs, runs them on
one or more render hosts over SSH (or locally), tracks live progress, and
collects finished outputs to your Movies folder. A built-in web dashboard
controls the queue: submit jobs, watch live progress bars, cancel, re-load past
jobs, extract frames from finished videos with ffmpeg, and manage history.

Render hosts stay **zero-footprint**: no agent is installed — any Mac with ssh
and `tod-dt-cli` works. All state comes from SSH one-liners and log tailing.

> **Note on the CLI**: `tod-dt-cli` is
> [DrawOtherThings' CustomCLI](https://github.com/wee-todd/DrawOtherThings/tree/main/Documentation/CustomCLI) —
> a community fork of Draw Things' `draw-things-cli`. It is required because
> the official CLI does not (yet) support the LTX-2.3 features this queue is
> built around (keyframes, audio conditioning, the persistent serve worker).

## Features

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
- **Chain continuation**: flag a job to auto-attach the last frame of the
  previous finished generation as its image/first-frame (resolved at launch,
  so queued batches continue from each other).
- **Collection**: outputs rsynced (or copied for local hosts) to
  `~/Movies/generations/<name>-<id>/` with `metadata.json` (full job record +
  sha256 + size). Remote work dirs cleaned unless `keep_remote`.
- **Idle-consumer policy**: hosts doing local work (Premiere etc.) take
  precedence — dispatch pauses on high load or heavy processes, and the serve
  worker is auto-stopped after sustained busyness to free model RAM, restarted
  when idle.
- **Multi-host**: `hosts.yaml` is the server registry (ssh or local transport,
  address, credentials via ssh config/opts, per-host CLI binary & version,
  models dir, job limits, per-host idle policy).

## Prerequisites

- **macOS** (control machine and render hosts) — paths and tooling assume it
- **Python 3.14** on the control machine
- **OpenSSH** with key-based auth to each *remote* render host (`ssh <host>`
  must work without a password prompt; credentials come from `~/.ssh/config`,
  never from `hosts.yaml`). Local hosts (`conn_type: local`) need no ssh.
- **rsync** and **ffmpeg/ffprobe** (for collection and frame extraction)
- **[tod-dt-cli](https://github.com/wee-todd/DrawOtherThings/tree/main/Documentation/CustomCLI)**
  (DrawOtherThings CustomCLI) built from source on each render host — the
  official `draw-things-cli` lacks the LTX-2.3 support this project needs

## Install

```sh
git clone <this-repo> ltxq
cd ltxq
python3.14 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python ltxq.py doctor   # self-checks (db, ssh, worker, flask)
```

## Configure hosts

```sh
cp hosts.yaml.example hosts.yaml
```

Edit `hosts.yaml`: list your render hosts top-to-bottom in preference order —
a job without an explicit host goes to the first enabled host with free
capacity. Minimal example:

```yaml
cli_path: ~/tod-dt-cli
movies_dir: ~/Movies/generations
hosts:
  # example remote host — pre-disabled; fill in dest/models_dir and set
  # enabled: true when you use it
  - alias: ltx-a
    dest: user@192.168.0.10
    max_jobs: 1
    enabled: false
    models_dir: /path/to/models
  # this machine, no ssh — a full peer of ssh hosts when it can run tod-cli:
  # same commands, job flow, serve worker and idle policy (transfers are local
  # copies instead of ssh/rsync). Pre-enabled for local generation; set
  # enabled: false if this machine lacks the hardware.
  - alias: local
    conn_type: local
    dest: local
    enabled: true
```

All global and per-host keys (idle policy, per-host CLI binary, ssh options,
stall/poll timing, …) are documented in
[docs/environment.md](docs/environment.md) and commented in
`hosts.yaml.example`.

## Run

```sh
./venv/bin/python ltxq.py ui              # dashboard + engine → http://127.0.0.1:8765
# or headless:
./venv/bin/python ltxq.py run             # engine loop only
```

The web UI binds to `127.0.0.1` only; `ui --no-engine` when running `run`
separately.

Common CLI commands: `add`, `ls`, `check <id>`, `cancel <id>`, `regen <id>`,
`probe [alias]`, `models <alias>`, `stage <alias> <model>`, `serve-start/-stop
<alias>`, `reconcile`, `doctor`, `ui`.

## Troubleshooting

Run `./venv/bin/python ltxq.py doctor` — it self-checks the database, ssh
reachability, remote shell, worker liveness, and Flask presence. Job state
lives in `ltxq.db` (SQLite, WAL), job inputs in `jobs/<id>/`.

To reset the queue (wipe job history, keep the install): stop the engine,
then `rm -f ltxq.db ltxq.db-wal ltxq.db-shm` — see
[docs/architecture.md](docs/architecture.md) ("State on disk & cleanup")
for what each file is and what resetting does and doesn't delete.

## Uninstall

1. **Stop the engine** — quit the Ltxq app, or Ctrl-C any `ui` / `run`
   process.
2. **Stop serve workers** (if you used the serve backend):
   `./venv/bin/python ltxq.py serve-stop <alias>` per host — or just delete
   the scratch dir on each render host in step 5.
3. **Delete the repo checkout** — everything local lives inside it: the
   venv, `ltxq.db*` (queue state), `jobs/` (input copies), `.ssh_mux/`,
   `engine.lock`.
4. **Delete collected outputs** if you don't want them: the `movies_dir`
   from your `hosts.yaml` (default `~/Movies/generations/`). Note your
   Draw Things models are elsewhere and are not touched.
5. **Clean render hosts** (ssh hosts you no longer use): `rm -rf ~/genwork`
   on each — scratch space only, recreated if you ever return.
6. **macOS app** (if installed): remove `Ltxq.app` from `/Applications`,
   then delete its per-user files:
   `~/Library/Application Support/Ltxq/` (holds `hosts.yaml` if you used
   the app-managed config) and `~/Library/Preferences/local.ltxq.app.plist`
   (window/port preferences).

## Documentation

- [docs/architecture.md](docs/architecture.md) — folder structure, data flow, external dependencies
- [docs/backends.md](docs/backends.md) — oneshot vs serve backends: setup, lifecycle, performance trade-offs
- [docs/environment.md](docs/environment.md) — hosts.yaml keys, env vars, CLI flags
- [docs/features.md](docs/features.md) — full feature list, known issues, TODOs
- [docs/decisions.md](docs/decisions.md) — design decisions and rationale
- [docs/CHANGELOG.md](docs/CHANGELOG.md) — changelog

## License

[Apache-2.0](LICENSE)
