import SwiftUI

struct SettingsView: View {
    @AppStorage(AppSettings.repoPathKey) private var repoPath = "~/ltxq"
    @AppStorage(AppSettings.portKey) private var port = 8765
    @AppStorage(AppSettings.keepEngineRunningOnQuitKey) private var keepEngineRunningOnQuit = false
    @EnvironmentObject private var engine: EngineManager

    var body: some View {
        Form {
            Section("Paths") {
                TextField("ltxq repo folder", text: $repoPath, prompt: Text("~/ltxq"))
                    .textFieldStyle(.roundedBorder)
                Text("Must contain `ltxq.py` and `venv/bin/python`. The app launches the engine from this repo and never bundles Python.")
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
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .padding(.vertical, 8)
    }
}
