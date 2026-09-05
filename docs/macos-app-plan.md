# Target goals: native macOS app (plan of record)

> **Status: Phase 0 complete** (see `macos/README.md`); Phase 1 complete
> (`GET /api/events` SSE + `docs/api.md` v1.1); Phases 2+ not started.
> This document is the starter plan for the ltxq native macOS app. It is
> written to be self-contained: paste or attach it into a fresh chat and work
> can begin from any phase without other context. Each phase ends at a
> usable checkpoint; nothing later breaks or freezes anything earlier.

## Background (one paragraph)

`ltxq` queues LTX-2 video/image generation jobs and runs them on render hosts
(SSH or local) via `tod-dt-cli`. It consists of a Python engine (`ltxq.py`:
SQLite state, ssh/rsync transport, PTY log-tailing, ffmpeg), a Flask API
server (`server.py`, binds `127.0.0.1`), and a single-file vanilla-JS web
dashboard (`static/index.html`, 2s polling of `/api/state`). See
[architecture.md](architecture.md) for details.

## Decisions already made

1. **Mac-only.** No iOS/iPadOS targets. This removes the hard constraint that
   the engine cannot leave macOS, so the app may *host* the engine instead of
   being a thin remote client.
2. **Tier 2 architecture:** native SwiftUI UI + the existing Python engine
   launched by the app as a child process. The engine is **not** ported to
   Swift. (Tier 1 = WebView shell was rejected as the end state; tier 3 =
   full Swift engine port rejected as not worth the effort for a personal
   tool.)
3. **Monorepo.** The Xcode project lives in `macos/` inside this repo; app
   and engine versions advance together; the HTTP API is the narrow coupling
   point and is documented in `docs/api.md` (created in Phase 1).
4. **No App Store, no sandbox.** The app spawns `ssh`/`rsync` subprocesses
   and writes `~/Movies`, so it must be non-sandboxed and is distributed
   outside the Mac App Store. Personal use: no paid Apple Developer account
   needed (local build / ad-hoc signing).
5. **The web dashboard stays** as a permanent fallback. All server changes
   are additive (no breaking API changes); the existing UI must keep working
   at every checkpoint.

## Ground rules for all phases

- Engine behavior is unchanged except for explicitly additive API endpoints
  (Phase 1) and their `docs/CHANGELOG.md` entries (repo policy).
- Every phase ends in a state usable for daily work; a phase can be the last
  one with no loss.
- App assumes the ltxq repo + `venv/` exist on the machine (it launches
  `venv/bin/python ltxq.py ui`); the app never bundles Python.
- Target macOS 14+; App Sandbox off; Swift/SwiftUI only, no third-party
  dependencies for Phases 0–2.

---

## Phase 0 — Checkpoint 1: "the app exists and is usable"

**Goal.** A Dock-launchable `ltxq.app` that starts/owns the engine and shows
the existing dashboard in a real app window. No native UI yet. Zero changes
to `server.py`.

### Deliverables

- [x] `macos/` Xcode project, single SwiftUI app target.
- [x] Engine lifecycle manager: spawn `venv/bin/python ltxq.py ui` as a child
      process, wait until `127.0.0.1:<port>` responds, terminate it on app
      quit.
- [x] Graceful handling of `engine.lock` already held (another `ui`/`run`
      running): attach to the running instance instead of failing, or a
      clear "already running" state.
- [x] Main window = `WKWebView` loading `http://127.0.0.1:<port>/`.
- [x] Settings (persisted): path to repo, venv, port. Defaults sensible for
      this machine.
- [x] App icon, standard window behavior (restore, quit = engine stops —
      with a user-visible toggle for "keep engine running on quit").

### Done when

- [x] Launching the app from Finder brings up the dashboard with the engine
      running; quitting the app (with the toggle on) leaves no orphaned
      `ltxq.py`/Flask process.
- [x] With `ltxq.py run` already running in a terminal, the app starts and
      attaches to it without double-dispatch errors.

### Risks / do first

The only real unknown is process lifecycle (crash cleanup, attach-vs-start,
port discovery). Build and harden that piece first; the rest of Phase 0 is
trivial.

---

## Phase 1 — Server prep (serves the app *and* the web dashboard)

**Goal.** Make the API ready to be a first-class contract: event stream
instead of polling only, and a written API spec.

### Deliverables

- [x] `GET /api/events` — SSE endpoint streaming job-state changes (event
      payload = the same job object `/api/job/<jid>` returns, plus host
      health changes). Purely additive; 2s polling keeps working.
- [x] `docs/api.md` — every endpoint (request/response shapes, error
      semantics, the SSE event format), marked with an API version.
- [x] Decision record appended to `docs/decisions.md` (Mac-only, tier 2,
      monorepo — i.e. the Decisions section above).
- [x] `docs/CHANGELOG.md` entry for the SSE endpoint.

### Done when

- `curl -N /api/events` shows a live event per state transition while a job
  runs, and the web dashboard behaves exactly as before.

---

## Phase 2 — Checkpoint 2: native UI at parity

**Goal.** A SwiftUI interface that replaces the web dashboard for daily use.
The HTML dashboard remains but is no longer needed.

### Deliverables

- [ ] Queue list (SwiftUI table/list) from `/api/state`: progress bars,
      phase, host, stall/suspect indicators; live updates via
      `/api/events` (fall back to polling).
- [ ] Job actions: cancel, delete, regen, re-load into form
      (`/api/job/<jid>`), per-host pause/resume.
- [ ] Submit form: prompt, model picker (`/api/models/<alias>`), host
      picker, config editor; media slots via `fileImporter` /
      drag-and-drop; multipart `POST /api/add`; staging thumbnails
      (`/api/stage`).
- [ ] Job detail view: log tail (`/api/view/<jid>`), assets
      (`/api/asset/…`), extract-frames trigger (`/api/extract`).
- [ ] Finished-output playback: `AVPlayer` on `/api/asset/<jid>/…`.

### Done when

- A full user workflow — submit with a keyframe image, watch progress,
  cancel one job, replay a finished video, re-load a past job — works
  without opening a browser. The web dashboard still works unchanged.

---

## Phase 3 — Checkpoint 3: Mac-native polish (the reason an app exists)

**Goal.** The OS-integration features a browser tab can't provide.

### Deliverables

- [ ] Native notifications (`UNUserNotificationCenter`): job done / failed /
      suspect (stall), clickable to open the job.
- [ ] Menu bar extra (`MenuBarExtra`): queue depth badge, per-host
      pause/resume, "open main window".
- [ ] "Reveal in Finder" for collected outputs
      (`~/Movies/generations/<name>-<id>/`).
- [ ] Drag files onto the Dock icon to stage media.
- [ ] Launch-at-login (`SMAppService`); polished settings window.
- [ ] Final app icon + about panel.

### Done when

- The queue is observable and controllable without the app window being
  open (menu bar + notifications), and finished generations are one click
  from Finder.

---

## Phase 4 — Optional / later (not scheduled)

- Host management UI (view/edit `hosts.yaml`), models-dir browser,
  chain-continuation UI.
- Only if a second Mac ever needs to control this one, the deferred
  remote-access work returns in full: bearer token in `hosts.yaml`, LAN
  bind option, HTTPS/pinning, Bonjour advertisement (`_ltxq._tcp`), and the
  iOS-style client conversation reopens.

---

## Effort shape (rough, not a commitment)

| Phase | Size |
|---|---|
| 0 | a weekend |
| 1 | 1–2 days |
| 2 | 1–2 weeks of evenings |
| 3 | ~1 week of evenings |
| 4 | unscheduled |
