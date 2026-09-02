# Environment / configuration inventory

`ltxq` deliberately uses **no OS environment variables** for its own
configuration — everything comes from `hosts.yaml`, CLI flags, and fixed
conventions. What exists:

## OS environment variables read

| Name | Where | Purpose |
|---|---|---|
| `HOME` | transport layer | `~/...` paths in yaml and commands are expanded remotely via `$HOME` (ssh) or the local shell (`/bin/zsh -c`); probe records each host's home in the DB |
| `PATH` | remote/local shell | locating `ssh`, `rsync`, `tar`, `ffmpeg`, `ffprobe` |
| `DRAWTHINGS_MODELS_DIR` | remote (read-only) | probe reports it as a models-dir candidate; yaml pin wins regardless |

SSH itself also reads the usual `~/.ssh/config`, agent, and known-hosts
environment — that *is* the credentials mechanism (no passwords in yaml).

## hosts.yaml — global keys (via `conf()`, with defaults)

| Key | Default | Purpose |
|---|---|---|
| `cli_path` | `~/tod-dt-cli` | draw-things-cli binary (overridable per host) |
| `poll_secs` | `10` | engine loop cadence (poll, dispatch, idle checks) |
| `stall_secs` | `900` | no-progress time before a job goes `suspect` |
| `remote_root` | `genwork` | remote work root (worker + job dirs) |
| `movies_dir` | `~/Movies/generations` | collection destination |
| `use_pty` | `true` | wrap oneshot CLI in `script` for live progress |
| `keep_remote` | `false` | keep remote job dirs after collect/cancel |
| `download_missing` | `false` | pass `--no-download-missing` |
| `disable_preview` | `true` | pass `--disable-preview` |
| `offline` | `false` | pass `--offline` |

## hosts.yaml — per-host keys

| Key | Default | Purpose |
|---|---|---|
| `alias` | — | host name used everywhere (required) |
| `dest` | alias | ssh destination (`user@host`) |
| `conn_type` | `ssh` | `ssh` or `local` (local = same machine, no ssh) |
| `enabled` | `true` | participate in dispatch / UI |
| `max_jobs` | `1` | concurrent in-flight jobs |
| `models_dir` | probed | pinned models directory |
| `cli_path` | global | per-host binary/version override |
| `ssh_opts` | `[]` | extra ssh options |
| `mux` | `true` | use ControlMaster multiplexing |
| `idle_policy.enabled` | `false` | gate dispatch on host business |
| `idle_policy.max_load` | `0` (off) | 1-min load threshold |
| `idle_policy.heavy_processes` | `[]` | pgrep patterns (Premiere etc.) |
| `idle_policy.pause_release_secs` | `300` | sustained busyness before serve-stop |

## CLI flags of note

- `ltxq.py ui [--port 8765] [--no-engine]` — dashboard bind is `127.0.0.1`
  only; `--no-engine` when running `run` separately.
- `serve-stop <alias> [--force]`, `stage … [--allow-download]`,
  `models <alias> [--catalog]`.

## Hardcoded values (not configurable without code edit)

UI bind `127.0.0.1`; page poll 2s; history cap 50; ssh timeouts (10s connect,
60s commands); log tails 4000B / rescan 200KB; rsync timeout 120s / transfer
7200s; serve-start ready wait 30s; SQLite timeouts 5s; staging dir `jobs/_tmp`
and `stage_` prefix; sqlite/rsync/ssh socket paths under the project dir.
