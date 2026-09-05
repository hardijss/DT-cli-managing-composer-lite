import SwiftUI

struct RootView: View {
    @EnvironmentObject private var engine: EngineManager

    var body: some View {
        switch engine.state {
        case .running:
            DashboardView(url: engine.dashboardURL)
                .id(engine.dashboardURL)
        default:
            StatusView()
        }
    }
}

/// Shown while probing/starting and on failure, with the engine's captured
/// output tail for diagnosis.
struct StatusView: View {
    @EnvironmentObject private var engine: EngineManager
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        VStack(spacing: 16) {
            switch engine.state {
            case .probing:
                ProgressView()
                Text("Looking for a running ltxq engine…")
                    .foregroundStyle(.secondary)
            case .starting:
                ProgressView()
                Text("Starting engine…")
                    .foregroundStyle(.secondary)
            case .idle:
                Image(systemName: "film.stack")
                    .font(.system(size: 48))
                    .foregroundStyle(.secondary)
                Text("Engine not started.")
                    .foregroundStyle(.secondary)
            case .stopped:
                Image(systemName: "stop.circle")
                    .font(.system(size: 48))
                    .foregroundStyle(.secondary)
                Text("Engine stopped.")
                    .foregroundStyle(.secondary)
            case .failed(let reason):
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 48))
                    .foregroundStyle(.orange)
                Text(reason)
                    .font(.callout)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: 560, alignment: .leading)
            case .running:
                EmptyView()
            }

            HStack {
                if showsStartButton {
                    Button("Start Engine") { engine.start() }
                        .keyboardShortcut(.defaultAction)
                }
                Button("Settings…") { openSettings() }
            }
            .padding(.top, 4)

            if !engine.logTail.isEmpty {
                ScrollView {
                    Text(engine.logTail)
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxWidth: 720, maxHeight: 220)
                .padding(8)
                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var showsStartButton: Bool {
        switch engine.state {
        case .failed, .stopped, .idle: return true
        default: return false
        }
    }
}
