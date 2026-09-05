import Foundation

/// Persistent app settings (UserDefaults-backed).
///
/// The app assumes the ltxq repo + `venv/` already exist on this machine; it
/// launches `venv/bin/python ltxq.py ui` and never bundles Python.
enum AppSettings {
    static let repoPathKey = "repoPath"
    static let portKey = "port"
    static let keepEngineRunningOnQuitKey = "keepEngineRunningOnQuit"

    static func registerDefaults() {
        UserDefaults.standard.register(defaults: [
            repoPathKey: "~/ltxq",
            portKey: 8765,
            keepEngineRunningOnQuitKey: false,
        ])
    }

    static var repoURL: URL {
        let raw = UserDefaults.standard.string(forKey: repoPathKey) ?? "~/ltxq"
        return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
    }

    static var pythonURL: URL {
        repoURL.appendingPathComponent("venv/bin/python")
    }

    static var scriptURL: URL {
        repoURL.appendingPathComponent("ltxq.py")
    }

    static var preferredPort: Int {
        UserDefaults.standard.integer(forKey: portKey)
    }

    static var keepEngineRunningOnQuit: Bool {
        UserDefaults.standard.bool(forKey: keepEngineRunningOnQuitKey)
    }
}
