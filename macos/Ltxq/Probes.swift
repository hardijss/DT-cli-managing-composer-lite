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
