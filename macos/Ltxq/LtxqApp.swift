import SwiftUI

@main
struct LtxqApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup("ltxq") {
            RootView()
                .frame(minWidth: 960, minHeight: 640)
                .environmentObject(EngineManager.shared)
        }
        Settings {
            SettingsView()
                .environmentObject(EngineManager.shared)
        }
        .commands {
            CommandMenu("Engine") {
                Button("Restart Engine") {
                    EngineManager.shared.restart()
                }
                Button("Stop Engine") {
                    EngineManager.shared.stop()
                }
                .disabled(!EngineManager.shared.canStopOwnedEngine)
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Quit must run applicationWillTerminate reliably so the engine child
        // is never orphaned behind a SIGKILL.
        ProcessInfo.processInfo.disableSuddenTermination()
        AppSettings.registerDefaults()
        EngineManager.shared.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        EngineManager.shared.stopOnQuit()
    }
}
