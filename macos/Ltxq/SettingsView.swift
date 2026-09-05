import SwiftUI

/// One row of the Settings diagnostics pane: a component the app/engine needs
/// and where it actually resolved to (path + version when found).
struct DiagRow: Identifiable {
    let id: String
    let ok: Bool
    let detail: String
}

struct SettingsView: View {
    @AppStorage(AppSettings.repoPathKey) private var repoPath = "~/ltxq"
    @AppStorage(AppSettings.portKey) private var port = 8765
    @AppStorage(AppSettings.keepEngineRunningOnQuitKey) private var keepEngineRunningOnQuit = false
    @EnvironmentObject private var engine: EngineManager
    @State private var diagnostics: [DiagRow] = []
    @State private var applying = false
    @State private var applyResult: ApplyResult?

    private struct ApplyResult {
        let ok: Bool
        let message: String
    }

    private struct HostsReloadResponse: Decodable {
        let error: String?
        let path: String?
        let hosts: [HostSummary]?
        struct HostSummary: Decodable {
            let alias: String
            let enabled: Bool
        }
    }

    var body: some View {
        Form {
            Section("Paths") {
                TextField("ltxq repo folder", text: $repoPath, prompt: Text("~/ltxq"))
                    .textFieldStyle(.roundedBorder)
                Text("Must contain `ltxq.py` and `venv/bin/python`. The app launches the engine from this repo and never bundles Python.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("hosts.yaml") {
                Text(AppSettings.hostsYamlURL.path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                HStack {
                    Button("Edit hosts.yaml…") { editHostsYaml() }
                    Button("Reveal in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting(
                            [ensureHostsYamlExists()])
                    }
                    Spacer()
                    Button("Apply Changes") { applyHostsYaml() }
                        .disabled(applying || !engine.state.isRunning)
                }
                if let applyResult {
                    Text(applyResult.message)
                        .font(.footnote)
                        .foregroundStyle(applyResult.ok ? Color.green : Color.red)
                        .textSelection(.enabled)
                }
                Text("The engine re-reads this file automatically within a few "
                     + "seconds of saving. Apply re-validates it immediately and "
                     + "reports any YAML or alias errors.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("Server") {
                TextField("Port", value: $port, format: .number.grouping(.never))
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 120)
                if engine.effectivePort != port {
                    Text("Using port \(engine.effectivePort) for this session (configured port was busy).")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            Section("Quit behavior") {
                Toggle("Keep engine running when the app quits", isOn: $keepEngineRunningOnQuit)
                Text("When off, quitting the app terminates the engine it started. This never affects an engine the app found already running.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("Diagnostics") {
                ForEach(diagnostics) { row in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: row.ok ? "checkmark.circle.fill"
                                                 : "xmark.circle.fill")
                            .foregroundStyle(row.ok ? Color.green : Color.red)
                        Text(row.id)
                            .frame(width: 76, alignment: .leading)
                        Text(row.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                Button("Refresh Diagnostics") {
                    diagnostics = collectDiagnostics()
                }
                Text("The same inventory is available in the dashboard via the ⚙ button next to the engine badge. Editable preferences are planned.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 560)
        .padding(.vertical, 8)
        .onAppear { diagnostics = collectDiagnostics() }
    }

    /// The engine seeds the per-user hosts.yaml from the bundled repo example
    /// on first start; mirror that here so Edit/Reveal always have a file.
    private func ensureHostsYamlExists() -> URL {
        let url = AppSettings.hostsYamlURL
        let fm = FileManager.default
        guard !fm.fileExists(atPath: url.path) else { return url }
        try? fm.createDirectory(at: url.deletingLastPathComponent(),
                                withIntermediateDirectories: true)
        let example = AppSettings.repoURL.appendingPathComponent("hosts.yaml.example")
        let contents = (try? Data(contentsOf: example))
            ?? Data("hosts:\n  # copy a host block from hosts.yaml.example in the repo\n".utf8)
        try? contents.write(to: url)
        return url
    }

    private func editHostsYaml() {
        NSWorkspace.shared.open(ensureHostsYamlExists())
    }

    private func applyHostsYaml() {
        applying = true
        applyResult = nil
        Task {
            defer { applying = false }
            guard let url = URL(string:
                "http://127.0.0.1:\(engine.effectivePort)/api/hosts/reload") else { return }
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                guard let http = response as? HTTPURLResponse else { return }
                let body = try? JSONDecoder().decode(HostsReloadResponse.self, from: data)
                if http.statusCode == 200, let body, let path = body.path {
                    let names = (body.hosts ?? []).map {
                        $0.alias + ($0.enabled ? "" : " (disabled)")
                    }.joined(separator: ", ")
                    applyResult = ApplyResult(
                        ok: true,
                        message: "Applied — \(body.hosts?.count ?? 0) host(s) from \(path)"
                            + (names.isEmpty ? "" : ": \(names)"))
                } else {
                    applyResult = ApplyResult(
                        ok: false,
                        message: body?.error
                            ?? "reload failed (HTTP \(http.statusCode))")
                }
            } catch {
                applyResult = ApplyResult(ok: false, message: "reload failed: \(error.localizedDescription)")
            }
        }
    }

    private func collectDiagnostics() -> [DiagRow] {
        var rows: [DiagRow] = []
        func add(_ id: String, _ ok: Bool, _ detail: String) {
            rows.append(DiagRow(id: id, ok: ok, detail: detail))
        }
        for tool in ["ffmpeg", "ffprobe"] {
            if let path = ToolProbe.locate(tool) {
                let version = ToolProbe.version(of: path)
                add(tool, true, version.map { "\(path) — \($0)" } ?? path)
            } else {
                add(tool, false, "not found — install with `brew install ffmpeg`")
            }
        }
        let fm = FileManager.default
        let python = AppSettings.pythonURL
        add("python", fm.fileExists(atPath: python.path), python.path)
        let script = AppSettings.scriptURL
        add("ltxq.py", fm.fileExists(atPath: script.path), script.path)
        let hostsYaml = AppSettings.hostsYamlURL
        add("hosts.yaml", fm.fileExists(atPath: hostsYaml.path), hostsYaml.path)
        add("engine", engine.state.isRunning,
            "port \(engine.effectivePort) — \(engine.state.description)")
        return rows
    }
}
