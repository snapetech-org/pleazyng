import CoreGraphics
import ImageIO
import Vision
import XCTest

#if os(tvOS)
extension PleazyngStoreKitLocalTrialReviewUITests {
    private struct TVScreen {
      let screenshot: XCUIScreenshot
      let image: CGImage
      let lines: [ScreenText]
    }

    private func captureTVScreen(in app: XCUIApplication) throws -> TVScreen {
      let screenshot = app.screenshot()
      guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
            let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "PleazyngStoreKitReviewCapture", code: 20,
                      userInfo: [NSLocalizedDescriptionKey: "Could not read the live tvOS screenshot"])
      }
      return TVScreen(screenshot: screenshot, image: image, lines: try recognizeScreenText(in: image))
    }

    // On the fresh default palette the focused rail row has a teal border.
    // Look for long matching border segments immediately above and below
    // the Settings text. An unfocused label or an adjacent icon cannot match.
    private func hasTVRailFocus(_ row: CGRect, in image: CGImage) -> Bool {
      let width = CGFloat(image.width)
      let height = CGFloat(image.height)
      let span = CGRect(
        x: row.minX * width, y: (1 - row.maxY - row.height * 2) * height,
        width: max(row.width, 0.025) * width, height: row.height * 5 * height
      ).integral
      guard CGRect(x: 0, y: 0, width: width, height: height).contains(span),
            let slice = image.cropping(to: span),
            let canvas = CGContext(data: nil, width: slice.width, height: slice.height,
              bitsPerComponent: 8, bytesPerRow: slice.width * 4, space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue),
            let pixels = canvas.data?.assumingMemoryBound(to: UInt8.self) else { return false }
      canvas.draw(slice, in: CGRect(x: 0, y: 0, width: span.width, height: span.height))
      func border(in rows: Range<Int>) -> Bool {
        rows.contains { rowIndex in
          (1...5).allSatisfy { sampleIndex in
            let pixelX = sampleIndex * slice.width / 6
            let offset = rowIndex * canvas.bytesPerRow + pixelX * 4
            let red = Int(pixels[offset])
            let green = Int(pixels[offset + 1])
            let blue = Int(pixels[offset + 2])
            return green > 150 && green - red > 45 && blue - red > 35
          }
        }
      }
      let third = slice.height / 3
      return border(in: 0..<third) && border(in: (slice.height - third)..<slice.height)
    }

    func openTVSettings(in app: XCUIApplication) throws {
      // Menu moves focus from the library content to the expanded sidebar.
      XCUIRemote.shared.press(.menu)
      _ = try waitForTVScreen(
        ["Settings"], in: app, attachmentName: "pleazyng-diagnostic-sidebar-tvos", timeout: 15)
      for direction: XCUIRemote.Button in [.down, .up] {
        for _ in 0..<8 {
          let screen = try captureTVScreen(in: app)
          if let settings = screen.lines.first(where: {
            $0.text == "Settings" && $0.boundingBox.midX < 0.30
          }), hasTVRailFocus(settings.boundingBox, in: screen.image) {
            attachScreenshot(screen.screenshot, named: "pleazyng-diagnostic-sidebar-settings-focused-tvos")
            XCUIRemote.shared.press(.select)
            return
          }
          XCUIRemote.shared.press(direction)
          Thread.sleep(forTimeInterval: 0.3)
        }
      }
      try attachScreenshot(of: app, named: "pleazyng-diagnostic-sidebar-settings-unfocused-tvos")
      throw NSError(domain: "PleazyngStoreKitReviewCapture", code: 22,
                    userInfo: [NSLocalizedDescriptionKey: "The Settings row did not receive visible tvOS remote focus"])
    }

    func captureTVLocalLibrary(in app: XCUIApplication) throws {
      // Files and Books starts on Podcasts, one remote step above Open media.
      // Capture the playback options reachable from this library rather than
      // scrolling to a text-only empty state below the last focusable row.
      XCUIRemote.shared.press(.down)
      XCUIRemote.shared.press(.select)
      _ = try waitForTVScreen(
        ["Open media", "What would you like to open", "Stream address", "Media address"],
        in: app, attachmentName: "pleazyng-localtest-files-books-tvos", timeout: 30)
      XCUIRemote.shared.press(.menu)
      _ = try waitForTVScreen(
        ["Files and books", "Open media"], in: app,
        attachmentName: "pleazyng-diagnostic-files-books-return-tvos", timeout: 30)
    }

    // The tvOS Flutter engine can render controls without exposing them
    // to XCTest. Inspect the actual app screenshot after each remote action.
    func waitForTVScreen(
      _ markers: [String], in app: XCUIApplication, attachmentName: String, timeout: TimeInterval
    ) throws -> String {
      let deadline = Date().addingTimeInterval(timeout)
      while true {
        let screen = try captureTVScreen(in: app)
        let text = joinedText(screen.lines)
        if markers.allSatisfy({ text.localizedCaseInsensitiveContains($0) }) {
          attachScreenshot(screen.screenshot, named: attachmentName)
          attachText(text, named: "\(attachmentName)-visible-text")
          return text
        }
        if Date() >= deadline {
          attachScreenshot(screen.screenshot, named: "pleazyng-diagnostic-\(attachmentName)-timeout")
          attachText(text, named: "pleazyng-diagnostic-\(attachmentName)-visible-text")
          attachText(app.debugDescription, named: "pleazyng-diagnostic-\(attachmentName)-accessibility")
          throw NSError(
            domain: "PleazyngStoreKitReviewCapture",
            code: 21,
            userInfo: [NSLocalizedDescriptionKey:
              "Missing visible tvOS controls: \(markers.joined(separator: ", "))"]
          )
        }
        Thread.sleep(forTimeInterval: 0.5)
      }
    }

}
#endif
