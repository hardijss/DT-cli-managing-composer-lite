# Changelog

All notable changes, bug fixes, and feature additions to `ltxq` are documented here.

## [Unreleased] - 2026-09-02

### Repo
- **GitHub setup**: Prepared the repository for GitHub (`hardijss/DT-cli-managing-composer-lite`, private): added `.gitignore`, `hosts.yaml.example`, `requirements.txt`, and a top-level `README.md`. The database, `jobs/` payloads, `venv/`, and the real `hosts.yaml` are excluded from version control.

### Docs
- **`docs/features.md`**: Added audio attachment support, staging GC / worker-log rotation, and per-host queue pause/resume (with timed release) to the feature list, matching the current code.

### Fixed
- **Log tail collapse on poll (`static/index.html`)**: Persisted the open/collapsed state of `<details>` elements (such as active job log tails and history input cards) across UI refresh polling cycles so uncollapsed logs remain open.
- **Host `enabled` sync (`ltxq.py`)**: Updated `sync_hosts()` to persist the `enabled` configuration from `hosts.yaml` into SQLite and automatically disable hosts removed from the YAML file.
- **Input focus loss on poll (`static/index.html`)**: Preserved input values, active element focus, and cursor selection range across the 2-second UI poll updates so typing in input fields (such as schedule release time) is uninterrupted.
- **Prompt retention on submit (`static/index.html`)**: Retained the prompt textarea contents upon submitting a new job instead of clearing it.

### Added
- **Garbage Collection & Log Rotation (`ltxq.py`, `server.py`)**: Added `clean_tmp()` to prune staging artifacts in `jobs/_tmp/` older than 24 hours, and `rotate_worker_logs()` to automatically rotate remote `worker.log` when exceeding 10MB during idle periods. Integrated periodic execution into the engine loop and CLI commands (`run`, `reconcile`).

- **Per-host queue pause/resume & timed release (`server.py`, `static/index.html`)**: Added `STATE["host_paused"]` (in-memory, per-host) and a `_dispatch_allowed()` guard in the engine loop so each host's dispatch can be independently paused. Two new API endpoints (`POST /api/queue/<alias>/pause` and `POST /api/queue/<alias>/resume`) expose this control; the pause endpoint accepts an optional `release_at` epoch for automatic timed resumption. The UI renders per-host controls (⏸ Pause / ▶ Resume / text input "Set & pause" supporting formats like `22:00`, `+2h`, or `2026-09-02 22:00`) with a live countdown banner reflecting current pause status.

- **Audio File Attachment Support (`static/index.html`, `server.py`, `ltxq.py`)**: Added `--audio` file attachment input to the Web UI form, updated `server.py` staged slot processing to include audio files, updated history rendering to display audio assets properly, and updated `cmd_regen` in `ltxq.py` to preserve asset flag associations upon job regeneration.
