import Foundation
import Combine

/// How the running server relates to this app.
enum EngineMode: Equatable {
    /// We spawned the `ltxq.py ui` child (engine loop inside it, or `--no-engine`).
    case owned
    /// A server was already listening before we started; not ours to manage.
    case external
}

enum EngineState: Equatable {
    case idle
    case probing
    case starting
    case running(port: Int, mode: EngineMode)
    case stopped
    case failed(reason: String)
}

/// Owns the engine child process: probe → attach to an existing server, or
/// spawn `ltxq.py ui` (full engine when the flock is free, `--no-engine` when
/// another `run`/`ui` holds it). Health-waits for the port, tails child
/// output for diagnostics, and tears the child down on quit unless the user
/// asked to keep it running.
@MainActor
final class EngineManager: ObservableObject {
    static let shared = EngineManager()

    @Published private(set) var state: EngineState = .idle
    @Published private(set) var effectivePort: Int = 8765
    @Published private(set) var logTail: String = ""
    /// Non-fatal startup findings (e.g. ffmpeg missing) shown as a banner
    /// over the dashboard; cleared on each launch.
    @Published private(set) var warnings: [String] = []

    private var process: Process?
    private var stoppingIntentionally = false
    private var logLines: [String] = []
    private let maxLogLines = 80
    /// Ports the launcher will scan past the configured one when it is taken
    /// by a foreign service (or by a ltxq server we can simply attach to).
    private let portScanRange = 1...10

    var dashboardURL: URL {
        URL(string: "http://127.0.0.1:\(effectivePort)/")!
    }

    var canStopOwnedEngine: Bool {
        if case .running(_, .owned) = state { return true }
        return false
    }

    // MARK: - Lifecycle entry points

    func start() {
        state = .probing
        Task { await launch() }
    }

    func restart() {
        if canStopOwnedEngine { stopChild(intentional: true) }
        start()
    }

    func stop() {
        if canStopOwnedEngine {
            stopChild(intentional: true)
        } else {
            state = .stopped
        }
    }

    /// Called from applicationWillTerminate. Synchronous on purpose: quit
    /// must not orphan the engine unless the user opted into keeping it.
    func stopOnQuit() {
        guard let child = process, child.isRunning else { return }
        guard !AppSettings.keepEngineRunningOnQuit else { return }
        terminateAndWait(child)
    }

    // MARK: - Launch sequence

    private func launch() async {
        let repo = AppSettings.repoURL
        let python = AppSettings.pythonURL
        let script = AppSettings.scriptURL

        warnings = []
        let missing = ToolProbe.missingTools()
        if !missing.isEmpty {
            warnings.append("Not found: \(missing.joined(separator: ", ")). "
                + "Frame extraction and chain continuation will fail "
                + "(generation is unaffected). Install with `brew install ffmpeg`.")
            appendLog("[app] startup probe: " + warnings[0] + "\n")
        }
        guard FileManager.default.fileExists(atPath: python.path) else {
            state = .failed(reason: "Python interpreter not found:\n\(python.path)\n\n"
                + "Set the repo path in Settings (it must contain venv/bin/python).")
            return
        }
        guard FileManager.default.fileExists(atPath: script.path) else {
            state = .failed(reason: "ltxq.py not found in:\n\(repo.path)\n\n"
                + "Set the repo path in Settings.")
            return
        }

        let preferred = AppSettings.preferredPort

        // 1. A ltxq server already on the configured port → attach, start nothing.
        if await LtxqProbe.isLtxqServer(port: preferred) {
            finishAttach(port: preferred)
            return
        }

        // 2. Maybe it moved (foreign service squatting on the configured port,
        //    or the user changed the port): scan a small range for a ltxq server.
        for offset in portScanRange {
            let port = preferred + offset
            if await LtxqProbe.isLtxqServer(port: port) {
                finishAttach(port: port)
                return
            }
        }

        // 3. Pick a spawn port that isn't occupied by a foreign service.
        var spawnPort = preferred
        if await LtxqProbe.check(port: preferred).occupied {
            guard let free = await firstFreePort(from: preferred + 1) else {
                state = .failed(reason: "Port \(preferred) is in use by another "
                    + "application, and ports \(preferred + 1)…\(preferred + portScanRange.upperBound) "
                    + "are all taken too. Free a port or change the port in Settings.")
                return
            }
            spawnPort = free
        }

        // 4. Engine lock decides owned mode: full engine vs UI-only attach.
        let lockHeld = LockProbe.isEngineLockHeld(repo: repo)
        spawn(port: spawnPort, engineInside: !lockHeld, python: python,
              script: script, repo: repo)
        guard await waitForHealth(port: spawnPort, child: process) else { return }
        // waitForHealth left `state` set on failure; success path:
    }

    private func finishAttach(port: Int) {
        effectivePort = port
        state = .running(port: port, mode: .external)
    }

    private func firstFreePort(from start: Int) async -> Int? {
        for port in start...(start + portScanRange.upperBound) {
            if await !LtxqProbe.check(port: port).occupied { return port }
        }
        return nil
    }

    private func spawn(port: Int, engineInside: Bool, python: URL,
                       script: URL, repo: URL) {
        state = .starting
        stoppingIntentionally = false
        clearLog()

        var arguments = [script.path, "ui", "--port", String(port)]
        if !engineInside {
            arguments.append("--no-engine")
        }
        appendLog("[app] spawning: venv/bin/python \(arguments.dropFirst().joined(separator: " "))\n")
        if !engineInside {
            appendLog("[app] engine.lock is held — starting UI only (--no-engine); "
                + "the run loop stays with the existing engine.\n")
        }

        let child = Process()
        child.executableURL = python
        child.arguments = arguments
        child.currentDirectoryURL = repo
        // GUI apps inherit a minimal PATH that misses Homebrew's bin dirs, where
        // ffmpeg/ffprobe live — extend it so engine-side subprocesses find them.
        var env = ProcessInfo.processInfo.environment
        let toolDirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
        env["PATH"] = (toolDirs.joined(separator: ":")) + ":" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        child.environment = env
        let outPipe = Pipe()
        let errPipe = Pipe()
        child.standardOutput = outPipe
        child.standardError = errPipe
        observe(pipe: outPipe)
        observe(pipe: errPipe)
        child.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                self?.childDidExit(status: terminated.terminationStatus)
            }
        }
        do {
            try child.run()
        } catch {
            state = .failed(reason: "Failed to launch engine:\n\(error.localizedDescription)")
            return
        }
        process = child
        effectivePort = port
    }

    private func observe(pipe: Pipe) {
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let chunk = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                self.appendLog(chunk)
            }
        }
    }

    /// Poll the dashboard port until the Flask server answers, watching the
    /// child for early death. Leaves `state` as failed/stopped on failure.
    private func waitForHealth(port: Int, child: Process?) async -> Bool {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            try? await Task.sleep(nanoseconds: 400_000_000)
            if let child, !child.isRunning { return false } // handler reported it
            if await LtxqProbe.isLtxqServer(port: port) {
                state = .running(port: port, mode: .owned)
                return true
            }
        }
        appendLog("[app] engine did not come up within 30s — stopping it.\n")
        state = .failed(reason: "Engine started but never answered on port \(port) "
            + "within 30s.\n\nCommon cause: something else took the port, or the "
            + "engine crashed at startup. Engine output:\n\n\(logTail)")
        if let child, child.isRunning {
            terminateAndWait(child)
        }
        return false
    }

    // MARK: - Child process events

    private func childDidExit(status: Int32) {
        process = nil
        if stoppingIntentionally {
            if case .running = state { state = .stopped }
            return
        }
        let reason = "Engine process exited (code \(status)).\n\nEngine output:\n\n\(logTail)"
        state = .failed(reason: reason)
    }

    private func terminateAndWait(_ child: Process) {
        guard child.isRunning else { return }
        stoppingIntentionally = true
        child.terminate() // SIGTERM; Flask exits, flock is released by the kernel
        let exited = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            child.waitUntilExit()
            exited.signal()
        }
        if exited.wait(timeout: .now() + 3) == .timedOut {
            kill(child.processIdentifier, SIGKILL)
            _ = exited.wait(timeout: .now() + 2)
        }
    }

    /// Stop the engine we own. `intentional` distinguishes a user/quit-driven
    /// stop from a crash: only the latter surfaces as a failure.
    private func stopChild(intentional: Bool) {
        guard let child = process, child.isRunning else {
            process = nil
            return
        }
        terminateAndWait(child)
        process = nil
    }

    // MARK: - Log capture

    private func clearLog() {
        logLines = []
        logTail = ""
    }

    private func appendLog(_ chunk: String) {
        // Werkzeug logs every request; the dashboard polls /api/state every 2s,
        // which would drown real errors out of the tail.
        let filtered = chunk.components(separatedBy: .newlines)
            .filter { !$0.isEmpty && !$0.contains("/api/state") && !$0.contains("GET / HTTP") }
        guard !filtered.isEmpty else { return }
        logLines.append(contentsOf: filtered)
        if logLines.count > maxLogLines {
            logLines.removeFirst(logLines.count - maxLogLines)
        }
        logTail = logLines.joined(separator: "\n")
    }
}
