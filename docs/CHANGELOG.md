# Changelog

All notable changes, bug fixes, and feature additions to `ltxq` are documented here.

## [Unreleased] - 2026-09-05

### Changed
- **Media slots: drag & drop + paste + click-to-browse (`static/index.html`)**: The six media file inputs (image, audio, input video, first/middle/last frame) and the dynamic keyframe rows are replaced by drop-tile slots. Clicking a tile still opens the OS file dialog via the unchanged hidden `<input type=file>` (so `accept` filtering and the multipart submit path are untouched), files can now be dragged from Finder onto a tile, and a paste with a file on the clipboard (e.g. a screenshot) fills the last-clicked/hovered tile, defaulting to `--image` when none was touched. Wrong-type drops/pastes are refused with a flash message instead of silently filling the slot; filled tiles show the filename, an image thumbnail for images, and keep the ✕ clear button. Text drag/paste into the prompt/config fields is unaffected, and the staging-badge area (Extract & attach / + media) is unchanged.

### Added
- **"+ media" attach button on history rows (`static/index.html`)**: Variant of the history Load button that pulls a past job's input files (`--image`, `--first-frame`, `--middle-frame`, `--last-frame`, `--audio`, `--input-video`, keyframes, plain uploads) into the staging area *on top of* the current form — without overwriting the prompt/config or wiping already-staged files. A slot the job also provides (e.g. first-frame) replaces its earlier staged entry so submit uses exactly what the badges show; keyframes/uploads accumulate. The re-staging logic was factored out of Load into a shared `attachJobAssets(j, replace)` used by both buttons (Load keeps its full-replace behavior).

### Changed
- **"+ media" button hidden pending revisit (`static/index.html`)**: The history-row button added above is now gated behind `const ATTACH_MEDIA = false` and no longer rendered; the shared `attachJobAssets()` plumbing and its Load-button use are untouched, so flipping the flag to `true` restores the button with no other changes.

### Fixed
- **No way to clear a selected media file (`static/index.html`)**: Each media slot on the new-job form (image, audio, input video, first/middle/last frame) now shows a ✕ button while a file is chosen; clicking it clears that input. Previously a picked file was stuck in its slot — the only way to drop it was reloading the page (re-entering prompt/config by hand) or submitting an unwanted attachment. Extracted/staged files already had per-badge ✕; keyframe rows are cleared by removing the row.

- **URL-safety of asset/staging links (`static/index.html`)**: History input thumbnails/links, staged-file previews, and the `/api/stage_from` re-attach calls now `encodeURIComponent` the filename — filenames with spaces previously relied on implicit browser encoding, and names containing `#`/`%`/`?` would silently truncate the request path. Verified against a live UI (`/api/stage_from` and `/api/stage/<name>` return the copied file for names containing spaces).

- **File-descriptor exhaustion after hours of idling (`ltxq.py`, `server.py`)**: Every `db()` call dropped its SQLite connection without `close()`; in request/engine threads (Python 3.14) those connections are only reclaimed at the next GC pass, so with the dashboard polling `/api/state` every 2 s the process leaked its `ltxq.db`/`ltxq.db-wal` file descriptors (~2 per request) until `OSError: [Errno 24] Too many open files` / `sqlite3.OperationalError: unable to open database file` after roughly 5–7 hours. All request handlers and the `cmd_*` entry points reachable from the ui process (`cmd_add`, `cmd_regen`, `cmd_cancel`, `cmd_models`, `cmd_serve_start`) now close their connections deterministically, the engine loop closes its long-lived connection on stop and runs `gc.collect()` in the 600 s maintenance cycle, and `is_local()` resolves `conn_type` from the in-memory `HOSTDEST` cache (extended by `load_dests()`) instead of opening a connection per call — it had been called on every `ssh()`/engine tick. Schema creation and migrations now also run once per process instead of on every `db()` call. Verified: fd count stays flat (50 → 49 over 150 requests + API round-trip, vs 52 → 280 before).

## [Unreleased] - 2026-09-03

### Added
- **Generation chain / continuation (`ltxq.py`, `server.py`, `static/index.html`)**: New "Continue from previous generation" checkbox with a first-frame/image slot picker on the web form (CLI: `add --chain {first_frame,image}`). Flagged jobs resolve at launch time: the engine extracts the last frame of the most recently finished generation and attaches it as `--first-frame` or `--image`, so batches of segments can be queued while earlier segments are still rendering and each continues from the one before. A missing source (no finished job, deleted video, ffmpeg failure) or a slot already filled manually degrades to running without the frame, with an explanatory note — the render is never failed over the chain. The ffprobe/ffmpeg extraction moved into a shared `ltxq.extract_frame()` (now also backing `/api/extract`); new `jobs.chain` column (auto-migrated); ⛓ badge marks chained jobs in the dashboard, checkbox state is remembered between submissions.

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
