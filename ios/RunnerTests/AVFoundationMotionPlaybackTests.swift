import Libmpv
import UIKit
import XCTest

@testable import Pleazyng

final class AVFoundationMotionPlaybackTests: XCTestCase {
  private let nativeCompletionTimeout: TimeInterval = 10
  private let playbackStartTimeout: TimeInterval = 60
  private let playbackAdvanceTimeout: TimeInterval = 30
  private let failure = NSError(
    domain: "AVFoundationMotionPlaybackTests",
    code: 1,
    userInfo: [NSLocalizedDescriptionKey: "controlled failure"]
  )

  func testVideoMotionOptionsCanChangeOnAnOpenAVFoundationStream() throws {
    let clip = try XCTUnwrap(
      Bundle(for: type(of: self)).url(forResource: "Butterfly-209", withExtension: "mp4")
    )
    let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 320, height: 180))
    window.isHidden = false
    let core = MpvPlayerCore()
    core.experimentalDolbyVisionRequested = false
    XCTAssertTrue(core.initialize(in: window))
    defer {
      let destroyed = expectation(description: "MPV video output disposed")
      core.dispose()
      core.queue.async { destroyed.fulfill() }
      wait(for: [destroyed], timeout: nativeCompletionTimeout)
      window.isHidden = true
    }
    core.setVisible(true)
    // The app subscribes through the plugin. Its visualization-only observer
    // uses a separate clock and does not populate the core's playback cache.
    core.observeProperty("time-pos", format: "double")
    XCTAssertSuccess(awaitProperty(core, name: "loop-file", value: "inf"))
    #if targetEnvironment(simulator)
      // The hosted simulator's VideoToolbox and host-audio services have been
      // unstable during startup. Keep this video-only integration check on the
      // real AVFoundation output path while decoding in software and omitting
      // an audio track that the test does not exercise.
      XCTAssertSuccess(awaitProperty(core, name: "aid", value: "no"))
      XCTAssertSuccess(awaitProperty(core, name: "hwdec", value: "no"))
    #endif
    // Set play intent before loading so the first decoded frame does not race
    // AVFoundation output startup against a pause-property write.
    XCTAssertSuccess(awaitProperty(core, name: "pause", value: "no"))
    _ = try runMotionCommand(core, ["loadfile", clip.path])
    try requirePlaybackAdvance(core, from: 0, timeout: playbackStartTimeout)
    XCTAssertEqual(try awaitGetProperty(core, name: "current-vo").get(), "avfoundation")
    let entry = try XCTUnwrap(try awaitGetProperty(core, name: "playlist/0/id").get())
    for preset in ["performance", "quality"] {
      let before = core.timePos
      XCTAssertSuccess(awaitProperty(core, name: "avfoundation-motion-preset", value: preset))
      XCTAssertSuccess(awaitProperty(core, name: "avfoundation-motion-interpolation", value: "yes"))
      try requirePlaybackAdvance(core, from: before, timeout: playbackAdvanceTimeout)
      XCTAssertEqual(try awaitGetProperty(core, name: "playlist/0/id").get(), entry)
    }
    XCTAssertSuccess(awaitProperty(core, name: "avfoundation-motion-interpolation", value: "no"))
    let before = core.timePos
    try requirePlaybackAdvance(core, from: before, timeout: playbackAdvanceTimeout)
    XCTAssertEqual(try awaitGetProperty(core, name: "playlist/0/id").get(), entry)

    try verifyFallbackFrameMixing(core, entry: entry)
  }

  private func runMotionCommand(
    _ core: MpvPlayerCore,
    _ args: [String],
    timeout: TimeInterval? = nil
  ) throws -> Int64? {
    let accepted = expectation(description: args[0])
    var result: Result<Int64?, Error>?
    core.commandAsync(args) { reply in
      result = reply
      accepted.fulfill()
    }
    wait(for: [accepted], timeout: timeout ?? nativeCompletionTimeout)
    return try XCTUnwrap(result).get()
  }

  private func verifyFallbackFrameMixing(_ core: MpvPlayerCore, entry: String) throws {
    guard try awaitGetProperty(core, name: "pleazyng-vt-motion-supported").get() == "no" else { return }
    let label = "@plezy-apple-motion"
    let cpu = "minterpolate=fps=60:mi_mode=blend"
    let originalHwdec = try awaitGetProperty(core, name: "hwdec").get() ?? "auto"
    if originalHwdec != "no" {
      XCTAssertSuccess(awaitProperty(core, name: "hwdec", value: "videotoolbox-copy"))
    }
    let provider: String
    #if targetEnvironment(simulator)
      // The iOS Simulator exposes MoltenVK, but libplacebo's mapped AVFrames can
      // hit an XPC shared-memory trap inside the simulator's Metal device. Keep
      // the playback/fallback integration check deterministic there; real Apple
      // devices still exercise the GPU route below.
      _ = try runMotionCommand(core, ["vf", "add", "\(label):lavfi=[\(cpu)]"])
      provider = "FFmpeg CPU (iOS Simulator)"
    #else
      let gpu = "libplacebo=fps=60:frame_mixer=oversample:format=yuv420p"
      do {
        _ = try runMotionCommand(core, ["vf", "add", "\(label):lavfi=[\(gpu)]"], timeout: 60)
        provider = "libplacebo GPU"
      } catch let error as NSError where error.domain == "mpv" {
        _ = try runMotionCommand(core, ["vf", "add", "\(label):lavfi=[\(cpu)]"])
        provider = "FFmpeg CPU"
      }
    #endif
    let deadline = Date().addingTimeInterval(nativeCompletionTimeout)
    var measured = 0.0
    repeat {
      pump(0.1)
      if let rate = try? awaitGetProperty(core, name: "estimated-vf-fps").get() {
        measured = Double(rate) ?? 0
      }
    } while measured < 45 && Date() < deadline
    XCTAssertGreaterThanOrEqual(measured, 45, "The 29.97 fps fixture must produce intermediate frames")
    print("Live motion mixer: \(provider), measured \(measured) fps")
    _ = try runMotionCommand(core, ["vf", "remove", label], timeout: 60)
    if originalHwdec != "no" {
      XCTAssertSuccess(awaitProperty(core, name: "hwdec", value: originalHwdec))
    }
    XCTAssertEqual(try awaitGetProperty(core, name: "playlist/0/id").get(), entry)
    XCTAssertFalse((try awaitGetProperty(core, name: "vf").get() ?? "").contains(label))
    try requirePlaybackAdvance(core, from: core.timePos, timeout: playbackAdvanceTimeout)
  }

  private func requirePlaybackAdvance(
    _ core: MpvPlayerCore,
    from previous: Double,
    timeout: TimeInterval
  ) throws {
    let deadline = ProcessInfo.processInfo.systemUptime + timeout
    // Read the subscribed cache without issuing extra native requests while
    // the first frame and audio output are starting.
    repeat {
      if hasAdvanced(core.timePos, from: previous) { return }
      pump(0.05)
    } while ProcessInfo.processInfo.systemUptime < deadline
    throw NSError(
      domain: "AVFoundationMotionPlaybackTests",
      code: 2,
      userInfo: [NSLocalizedDescriptionKey: "The open clip did not advance from \(previous) in \(timeout) s"]
    )
  }

  private func hasAdvanced(_ current: Double, from previous: Double) -> Bool {
    current > previous + 0.05 || (current > 0 && current < previous)
  }

  private func pump(_ seconds: TimeInterval) {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: seconds))
  }

  private func awaitProperty(
    _ core: MpvPlayerCoreBase,
    name: String,
    value: String
  ) -> Result<Void, Error> {
    let completion = expectation(description: "set \(name)")
    var result: Result<Void, Error>?
    core.setPropertyAsync(name, value: value) {
      result = $0
      completion.fulfill()
    }
    wait(for: [completion], timeout: nativeCompletionTimeout)
    return result ?? .failure(failure)
  }

  private func awaitGetProperty(_ core: MpvPlayerCoreBase, name: String) -> Result<String?, Error> {
    let completion = expectation(description: "get \(name)")
    var result: Result<String?, Error>?
    core.getPropertyAsync(name) {
      result = $0
      completion.fulfill()
    }
    wait(for: [completion], timeout: nativeCompletionTimeout)
    return result ?? .failure(failure)
  }

  private func XCTAssertSuccess(
    _ result: Result<Void, Error>,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    if case .failure(let error) = result {
      XCTFail("Expected success, received \(error)", file: file, line: line)
    }
  }
}
