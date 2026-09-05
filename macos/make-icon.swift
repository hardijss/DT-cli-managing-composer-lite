// Renders macos/Ltxq/Assets.xcassets/AppIcon.appiconset/icon_1024.png
// Run: venv not needed — plain `swift macos/make-icon.swift` (AppKit).
import AppKit

let size = NSSize(width: 1024, height: 1024)
let image = NSImage(size: size)
image.lockFocus()

let inset: CGFloat = 100
let radius: CGFloat = 185
let rect = NSRect(x: inset, y: inset, width: size.width - inset * 2, height: size.height - inset * 2)
let shape = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
shape.addClip()

let gradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.16, green: 0.22, blue: 0.62, alpha: 1),
    NSColor(calibratedRed: 0.42, green: 0.16, blue: 0.72, alpha: 1),
])!
gradient.draw(in: shape, angle: -55)

// Subtle top sheen
NSColor(white: 1.0, alpha: 0.08).setFill()
NSBezierPath(roundedRect: NSRect(x: rect.minX, y: rect.midY, width: rect.width, height: rect.height / 2),
             xRadius: radius, yRadius: radius).fill()

var symbol = NSImage(systemSymbolName: "film.stack", accessibilityDescription: nil)
if symbol == nil { symbol = NSImage(systemSymbolName: "film", accessibilityDescription: nil) }
if let sym = symbol {
    let config = NSImage.SymbolConfiguration(paletteColors: [.white])
        .applying(.init(pointSize: 400, weight: .medium))
    if let tinted = sym.withSymbolConfiguration(config) {
        let dim: CGFloat = 520
        let drawRect = NSRect(x: rect.midX - dim / 2, y: rect.midY - dim / 2 + 20, width: dim, height: dim)
        tinted.draw(in: drawRect, from: .zero, operation: .sourceOver, fraction: 1.0,
                    respectFlipped: true, hints: [.interpolation: NSImageInterpolation.high.rawValue])
    }
}

image.unlockFocus()

// Export at exactly 1024×1024 pixels regardless of backing scale.
let outRep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024,
                              bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                              colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: outRep)
image.draw(in: NSRect(x: 0, y: 0, width: 1024, height: 1024))
NSGraphicsContext.restoreGraphicsState()

guard let png = outRep.representation(using: .png, properties: [:]) else {
    fatalError("png encode failed")
}
let out = URL(fileURLWithPath: CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "Ltxq/Assets.xcassets/AppIcon.appiconset/icon_1024.png")
try! png.write(to: out)
print("wrote \(out.path)")
