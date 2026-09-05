# ltxq.app (native macOS shell)

Dock-launchable Mac app that starts (or attaches to) the ltxq engine and shows
the existing web dashboard in a real app window. Phase 0 of
[the macOS app plan](../docs/macos-app-plan.md); SwiftUI + WKWebView, no
native UI yet, zero changes to `server.py`.

## Build

```sh
cd macos
./build.sh            # Release → build/products/Release/ltxq.app
./build.sh Debug      # Debug
```

Then `open build/products/Release/ltxq.app`. The project can also be opened in
Xcode (`Ltxq.xcodeproj`) — there is no shared scheme; Xcode creates one
automatically on first open. Ad-hoc signed (`CODE_SIGN_IDENTITY=-`), no team
or paid developer account needed. Target macOS 14+.

## What it does at launch

1. Probes `127.0.0.1:<port>` (default 8765, then +1…+10) for a server whose
   `/api/state` matches ltxq's shape → **attaches** to it and manages nothing.
2. Otherwise checks `engine.lock` (BSD flock — held ⇒ a live `run`/`ui`
   exists): held → spawns `ltxq.py ui --no-engine --port <p>` (UI only); free
   → spawns the full `ltxq.py ui --port <p>` as a child process.
3. Health-waits up to 30s for the port, then shows the dashboard.

Quitting the app terminates only the engine child it spawned itself
(SIGTERM, 3s grace, then SIGKILL) — unless Settings → "Keep engine running
when the app quits" is on. An engine that was already running before launch
is never touched.

## Settings (UserDefaults, `defaults write local.ltxq.app …`)

| Key | Default | Meaning |
|---|---|---|
| `repoPath` | `~/ltxq` | Repo containing `ltxq.py` + `venv/bin/python` |
| `port` | `8765` | Preferred dashboard port (scan proceeds upward if busy) |
| `keepEngineRunningOnQuit` | `false` | Leave an app-spawned engine alive on quit |

## Files

- `Ltxq/EngineManager.swift` — lifecycle state machine (probe/attach/spawn/
  health-wait/teardown), child output tail for diagnostics
- `Ltxq/Probes.swift` — `/api/state` fingerprint + flock probe
- `Ltxq/RootView.swift` — dashboard window vs. status/error screen
- `Ltxq/SettingsView.swift`, `Ltxq/Settings.swift` — persisted settings
- `make-icon.swift` — regenerates the app icon PNG (`swift make-icon.swift`)
