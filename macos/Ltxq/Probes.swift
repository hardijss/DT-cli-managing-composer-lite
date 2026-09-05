import Foundation

/// Answers "is a ltxq Flask server already listening on this port?".
///
/// A port is only considered a ltxq server when `/api/state` returns the exact
/// shape `server.py` produces (jobs/hosts/engine/now) — anything else on the
/// port is treated as a foreign service we must route around.
enum LtxqProbe {
    struct Result {
        var occupied = false
        var isLtxq = false
    }

    static func check(port: Int) async -> Result {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/state") else {
            return Result()
        }
        var request = URLRequest(url: url,
                                 cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 2)
        request.httpShouldHandleCookies = false
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return Result() // connection refused → port is free
        }
        var result = Result()
        result.occupied = true
        if http.statusCode == 200,
           let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           obj["jobs"] is [Any], obj["hosts"] is [Any], obj["engine"] != nil {
            result.isLtxq = true
        }
        return result
    }

    static func isLtxqServer(port: Int) async -> Bool {
        await check(port: port).isLtxq
    }
}

/// Non-invasive check on `<repo>/engine.lock`: `ltxq.py` guards the engine
/// with a BSD `flock`, which the kernel releases when the holder dies, so a
/// contended lock always means a live engine (`run` or another `ui`).
/// Probing never leaves a lock behind — we only open + try-lock + close.
enum LockProbe {
    static func isEngineLockHeld(repo: URL) -> Bool {
        let path = repo.appendingPathComponent("engine.lock").path
        let fd = open(path, O_RDWR | O_CREAT, 0o644)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var fl = flock()
        fl.l_type = Int16(F_WRLCK)
        fl.l_whence = Int16(SEEK_SET)
        let rc = fcntl(fd, F_SETLK, &fl)
        return rc == -1 // EAGAIN/EACCES → someone else holds it
    }
}

/// Locates engine-side helper tools the way `ltxq.ff_tool()` does. A GUI app
/// inherits a minimal PATH, so `which` alone is not enough — the Homebrew and
/// MacPorts bin dirs are probed directly. Missing tools are non-fatal: frame
/// extraction / chain continuation degrade, generation is unaffected.
enum ToolProbe {
    static let searchDirs = ["/opt/homebrew/bin", "/usr/local/bin",
                             "/opt/local/bin", "/usr/bin", "/bin"]

    static func locate(_ name: String) -> String? {
        if let path = ProcessInfo.processInfo.environment["PATH"] {
            for dir in path.split(separator: ":") {
                let candidate = "\(dir)/\(name)"
                if FileManager.default.isExecutableFile(atPath: candidate) {
                    return candidate
                }
            }
        }
        for dir in searchDirs {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }

    static func find(_ name: String) -> Bool {
        locate(name) != nil
    }

    /// First line of `<tool> -version` (ffmpeg/ffprobe style), for the
    /// Settings diagnostics pane.
    static func version(of toolPath: String) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: toolPath)
        proc.arguments = ["-version"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        let output = String(decoding: data, as: UTF8.self)
        return output.split(separator: "\n", omittingEmptySubsequences: true)
            .first.map(String.init)
    }

    /// Names of required-for-features tools that could not be found.
    static func missingTools() -> [String] {
        ["ffmpeg", "ffprobe"].filter { !find($0) }
    }
}
