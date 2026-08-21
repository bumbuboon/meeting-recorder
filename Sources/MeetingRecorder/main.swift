// MeetingRecorder.swift
// Records the main display + system audio + microphone into one .mp4
// using ScreenCaptureKit (macOS 15+). No virtual audio device needed.
// Output mp4 contains: 1 video track, audio track 0 = system audio,
// audio track 1 = microphone. Mix them afterwards with ffmpeg.
//
// Usage: MeetingRecorder <output.mp4> [--fps N] [--display N]
// Stop with SIGINT/SIGTERM for a clean finalize.

import AppKit
import AVFoundation
import CoreGraphics
import CoreMedia
import Darwin
import Foundation
import OSLog
import ScreenCaptureKit

final class EventLogger: @unchecked Sendable {
    private let lock = NSLock()
    private let handle: FileHandle
    private let system = Logger(subsystem: "local.meeting-recorder", category: "runtime")
    private let runID = UUID().uuidString.lowercased()
    private let startedAt = DispatchTime.now().uptimeNanoseconds
    private var sequence = 0

    init(path: String) throws {
        FileManager.default.createFile(atPath: path, contents: nil)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        handle = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
        try handle.seekToEnd()
    }

    @discardableResult
    func write(level: String, event: String, message: String, fields: [String: Any] = [:]) -> Bool {
        var succeeded = true
        lock.withLock {
            sequence += 1
            var record: [String: Any] = [
                "schema": "meeting-recorder.event.v1",
                "event": event,
                "event_id": UUID().uuidString.lowercased(),
                "run_id": runID,
                "seq": sequence,
                "occurred_at": ISO8601DateFormatter().string(from: Date()),
                "uptime_ns": DispatchTime.now().uptimeNanoseconds - startedAt,
                "severity": level,
                "message": message,
                "pid": ProcessInfo.processInfo.processIdentifier,
                "app_version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
            ]
            fields.forEach { record[$0.key] = $0.value }
            do {
                guard JSONSerialization.isValidJSONObject(record) else {
                    throw NSError(domain: "MeetingRecorder.EventLogger", code: 1, userInfo: [NSLocalizedDescriptionKey: "event is not valid JSON"])
                }
                var data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
                data.append(0x0A)
                try handle.write(contentsOf: data)
                try handle.synchronize()
            } catch {
                succeeded = false
                let fallback = "structured log write failed: \(error.localizedDescription)\n"
                FileHandle.standardError.write(fallback.data(using: .utf8)!)
            }
        }
        let systemMessage = "event=\(event) run_id=\(runID) severity=\(level)"
        switch level {
        case "error": system.error("\(systemMessage, privacy: .public)")
        case "warning": system.warning("\(systemMessage, privacy: .public)")
        default: system.info("\(systemMessage, privacy: .public)")
        }
        return succeeded
    }
}

var eventLogger: EventLogger?
private let loggingStateLock = NSLock()
private var structuredLoggingFailed = false

func log(_ s: String, event: String = "message", level: String = "info", fields: [String: Any] = [:]) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
    if let eventLogger, !eventLogger.write(level: level, event: event, message: s, fields: fields) {
        loggingStateLock.withLock { structuredLoggingFailed = true }
    }
}

func errorFields(_ error: Error) -> [String: Any] {
    let nsError = error as NSError
    var fields: [String: Any] = [
        "error_domain": nsError.domain,
        "error_code": nsError.code,
        "error_description": nsError.localizedDescription,
    ]
    if let underlying = nsError.userInfo[NSUnderlyingErrorKey] as? NSError {
        fields["underlying_domain"] = underlying.domain
        fields["underlying_code"] = underlying.code
        fields["underlying_description"] = underlying.localizedDescription
    }
    return fields
}

final class TranscriberWorker: @unchecked Sendable {
    struct DrainResult { let ok: Bool; let transcriptPath: String?; let reason: String }

    private let runDirectory: URL
    private let script: String
    private let lock = NSLock()
    private var process: Process?

    init(runDirectory: URL, scriptsDirectory: String) {
        self.runDirectory = runDirectory
        script = "\(scriptsDirectory)/transcriber_worker.py"
    }

    func start() {
        lock.withLock {
            guard process?.isRunning != true else { return }
            guard FileManager.default.isReadableFile(atPath: script) else {
                log("transcriber worker script is unavailable", event: "worker_launch_failed", level: "error", fields: ["script": script])
                return
            }
            let launched = Process()
            launched.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            launched.arguments = ["python3", script, "--run-dir", runDirectory.path]
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin:/usr/bin:/bin"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            launched.environment = environment
            let logURL = runDirectory.appendingPathComponent("transcriber-worker.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            if let handle = try? FileHandle(forWritingTo: logURL) {
                _ = try? handle.seekToEnd()
                launched.standardOutput = handle
                launched.standardError = handle
            }
            do {
                try launched.run()
                process = launched
                log("transcriber worker started", event: "worker_started", fields: ["pid": launched.processIdentifier])
            } catch {
                process = nil
                log("transcriber worker launch failed: \(error.localizedDescription)", event: "worker_launch_failed", level: "error", fields: errorFields(error))
            }
        }
    }

    func waitForDone(timeoutSeconds: TimeInterval) async -> DrainResult {
        let done = runDirectory.appendingPathComponent("chunks/WORKER_DONE")
        let transcript = runDirectory.appendingPathComponent("chunks/transcript.json")
        let failed = runDirectory.appendingPathComponent("chunks/WORKER_FAILED")
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        var restarted = false
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: done.path) {
                guard FileManager.default.fileExists(atPath: transcript.path) else {
                    return DrainResult(ok: false, transcriptPath: nil, reason: "ACK exists without a successful folded transcript")
                }
                return DrainResult(ok: true, transcriptPath: transcript.path, reason: "ok")
            }
            if FileManager.default.fileExists(atPath: failed.path) {
                return DrainResult(ok: false, transcriptPath: nil, reason: "one or more chunks failed after three attempts")
            }
            let running = lock.withLock { process?.isRunning == true }
            if !running && !restarted {
                restarted = true
                start()
            }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        return DrainResult(ok: false, transcriptPath: nil, reason: "WORKER_DONE deadline exceeded")
    }
}

final class RollingMinutesWorker: @unchecked Sendable {
    private let runDirectory: URL
    private let script: String
    private let lock = NSLock()
    private var process: Process?

    init(runDirectory: URL, scriptsDirectory: String) {
        self.runDirectory = runDirectory
        script = "\(scriptsDirectory)/rolling_minutes_worker.py"
    }

    func start() {
        lock.withLock {
            guard process?.isRunning != true else { return }
            guard FileManager.default.isReadableFile(atPath: script) else {
                log("rolling minutes worker script is unavailable", event: "minutes_worker_launch_failed", level: "warning", fields: ["script": script])
                return
            }
            let launched = Process()
            launched.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            launched.arguments = ["python3", script, "--run-dir", runDirectory.path]
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin:/usr/bin:/bin"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            launched.environment = environment
            let logURL = runDirectory.appendingPathComponent("rolling-minutes-worker.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            if let handle = try? FileHandle(forWritingTo: logURL) {
                _ = try? handle.seekToEnd()
                launched.standardOutput = handle
                launched.standardError = handle
            }
            do {
                try launched.run()
                process = launched
                log("rolling minutes worker started", event: "minutes_worker_started", fields: ["pid": launched.processIdentifier])
            } catch {
                process = nil
                log("rolling minutes worker launch failed: \(error.localizedDescription)", event: "minutes_worker_launch_failed", level: "warning", fields: errorFields(error))
            }
        }
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let writer: AVAssetWriter
    let videoInput: AVAssetWriterInput
    let systemAudioInput: AVAssetWriterInput
    let micInput: AVAssetWriterInput
    var stream: SCStream?
    private var sessionStarted = false
    private let queue = DispatchQueue(label: "recorder.samples")
    private let stateLock = NSLock()
    private var finishing = false
    private var failureReported = false
    private var lastAppendAt = Date()
    private var appendedSamples = 0
    private var receivedByTrack = ["screen": 0, "system_audio": 0, "microphone": 0]
    private var appendedByTrack = ["screen": 0, "system_audio": 0, "microphone": 0]
    private var notReadyByTrack = ["screen": 0, "system_audio": 0, "microphone": 0]
    private var invalidByTrack = ["screen": 0, "system_audio": 0, "microphone": 0]
    private var incompleteByTrack = ["screen": 0, "system_audio": 0, "microphone": 0]
    private var lastAppendByTrack = [String: Date]()
    private var healthStartedAt = Date()
    private var lastHealthLogAt = Date.distantPast
    private var healthTimer: DispatchSourceTimer?
    private let captureWidth: Int
    private let captureHeight: Int
    private let audioChunker: AudioChunker?
    private let transcriberWorker: TranscriberWorker?
    private let rollingMinutesWorker: RollingMinutesWorker?
    private let liveTranscriptionRequired: Bool

    init(outputURL: URL, width: Int, height: Int, runDirectory: URL?, scriptsDirectory: String) throws {
        captureWidth = width
        captureHeight = height
        liveTranscriptionRequired = runDirectory != nil
        writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
        _ = chmod(outputURL.path, 0o600)
        // Leave movieFragmentInterval disabled. The previous 5-second setting
        // repeatedly rebuilt MP4 fragment headers and failed in MovieHeaderMaker
        // (OSStatus -16341) during a real recording. A normal toggle stop still
        // finalizes the file through finishWriting().

        videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 4_000_000,
                AVVideoMaxKeyFrameIntervalKey: 60,
            ],
        ])
        let audioSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 128_000,
        ]
        systemAudioInput = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
        micInput = AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)
        for input in [videoInput, systemAudioInput, micInput] {
            input.expectsMediaDataInRealTime = true
            writer.add(input)
        }
        if let runDirectory {
            do {
                audioChunker = try AudioChunker(runDirectory: runDirectory)
                transcriberWorker = TranscriberWorker(runDirectory: runDirectory, scriptsDirectory: scriptsDirectory)
                rollingMinutesWorker = RollingMinutesWorker(runDirectory: runDirectory, scriptsDirectory: scriptsDirectory)
            } catch {
                audioChunker = nil
                transcriberWorker = nil
                rollingMinutesWorker = nil
                log("live transcription initialization failed: \(error.localizedDescription)", event: "chunker_failed", level: "error", fields: errorFields(error))
            }
        } else {
            audioChunker = nil
            transcriberWorker = nil
            rollingMinutesWorker = nil
        }
        super.init()
    }

    func start(display: SCDisplay, fps: Int) async throws {
        let config = SCStreamConfiguration()
        config.width = captureWidth
        config.height = captureHeight
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
        config.showsCursor = true
        config.capturesAudio = true
        config.sampleRate = 48_000
        config.channelCount = 2
        config.captureMicrophone = true
        config.excludesCurrentProcessAudio = true
        config.queueDepth = 5

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        self.stream = stream

        guard writer.startWriting() else {
            throw writer.error ?? NSError(domain: "MeetingRecorder", code: 1)
        }
        transcriberWorker?.start()
        rollingMinutesWorker?.start()
        do {
            try await stream.startCapture()
        } catch {
            self.stream = nil
            writer.cancelWriting()
            _ = audioChunker?.stopAndDrain()
            throw error
        }
        startHealthMonitor()
        log(
            "recording: started -> \(writer.outputURL.path)",
            event: "capture_started",
            fields: [
                "output": writer.outputURL.path,
                "fps": fps,
                "display_width": display.width,
                "display_height": display.height,
                "capture_width": captureWidth,
                "capture_height": captureHeight,
            ]
        )
    }

    private func describeWriterError(_ context: String) -> String {
        let status = writer.status.rawValue
        if let error = writer.error as NSError? {
            let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError
            let underlyingText = underlying.map { " underlying=\($0.domain)/\($0.code): \($0.localizedDescription)" } ?? ""
            return "\(context): writer status=\(status) error=\(error.domain)/\(error.code): \(error.localizedDescription)\(underlyingText)"
        }
        return "\(context): writer status=\(status) with no error object"
    }

    private func reportFailure(_ context: String) {
        stateLock.lock()
        let shouldReport = !failureReported && !finishing
        if shouldReport { failureReported = true }
        stateLock.unlock()
        guard shouldReport else { return }

        var fields: [String: Any] = ["context": context, "writer_status": writer.status.rawValue]
        if let error = writer.error { errorFields(error).forEach { fields[$0.key] = $0.value } }
        log("recording: \(describeWriterError(context))", event: "writer_failed", level: "error", fields: fields)
        Task { await self.finish(exitCode: 2) }
    }

    private func trackName(_ type: SCStreamOutputType) -> String {
        switch type {
        case .screen: return "screen"
        case .audio: return "system_audio"
        case .microphone: return "microphone"
        @unknown default: return "unknown"
        }
    }

    private func increment(_ keyPath: ReferenceWritableKeyPath<Recorder, [String: Int]>, track: String) {
        stateLock.withLock {
            self[keyPath: keyPath][track, default: 0] += 1
        }
    }

    private func noteAppend(track: String) {
        stateLock.lock()
        lastAppendAt = Date()
        appendedSamples += 1
        appendedByTrack[track, default: 0] += 1
        lastAppendByTrack[track] = Date()
        stateLock.unlock()
    }

    private func startHealthMonitor() {
        stateLock.withLock { healthStartedAt = Date() }
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        timer.schedule(deadline: .now() + 5, repeating: 5)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            if self.writer.status == .failed {
                self.reportFailure("writer failed asynchronously")
                return
            }
            self.stateLock.lock()
            let stalledFor = Date().timeIntervalSince(self.lastAppendAt)
            let isFinishing = self.finishing
            let sampleCount = self.appendedSamples
            let now = Date()
            var streams = [String: [String: Any]]()
            for track in ["screen", "system_audio", "microphone"] {
                streams[track] = [
                    "received": self.receivedByTrack[track, default: 0],
                    "appended": self.appendedByTrack[track, default: 0],
                    "not_ready": self.notReadyByTrack[track, default: 0],
                    "invalid": self.invalidByTrack[track, default: 0],
                    "incomplete": self.incompleteByTrack[track, default: 0],
                    "last_append_age_ms": self.lastAppendByTrack[track].map { Int(now.timeIntervalSince($0) * 1000) } ?? Int(now.timeIntervalSince(self.healthStartedAt) * 1000),
                ]
            }
            let shouldLogHealth = Date().timeIntervalSince(self.lastHealthLogAt) >= 30
            if shouldLogHealth { self.lastHealthLogAt = Date() }
            self.stateLock.unlock()
            if shouldLogHealth && !isFinishing {
                log(
                    String(format: "recording health: samples=%d stalled=%.1fs", sampleCount, stalledFor),
                    event: "sample_health",
                    fields: ["samples": sampleCount, "stalled_seconds": stalledFor, "writer_status": self.writer.status.rawValue, "streams": streams]
                )
            }
            if !isFinishing && stalledFor >= 15 {
                self.reportFailure(String(format: "no samples written for %.1f seconds", stalledFor))
                return
            }
            if !isFinishing {
                for track in ["screen", "system_audio", "microphone"] {
                    guard let stream = streams[track], let age = stream["last_append_age_ms"] as? Int else { continue }
                    let trackStalledFor = Double(age) / 1000
                    if trackStalledFor >= 15 {
                        self.reportFailure(String(format: "no %@ samples written for %.1f seconds", track, trackStalledFor))
                        return
                    }
                }
            }
        }
        healthTimer = timer
        timer.resume()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        let track = trackName(type)
        increment(\Recorder.receivedByTrack, track: track)
        guard sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else {
            increment(\Recorder.invalidByTrack, track: track)
            return
        }

        if writer.status == .failed {
            reportFailure("writer failed before appending \(type)")
            return
        }

        if type == .screen {
            // Skip incomplete/idle frames.
            guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
                  let statusRaw = attachments.first?[.status] as? Int,
                  statusRaw == SCFrameStatus.complete.rawValue
            else {
                increment(\Recorder.incompleteByTrack, track: track)
                return
            }
        }

        if !sessionStarted {
            // Anchor the timeline on the first usable buffer.
            let origin = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            writer.startSession(atSourceTime: origin)
            audioChunker?.setSessionStartPTS(origin)
            sessionStarted = true
        }

        let input: AVAssetWriterInput
        switch type {
        case .screen: input = videoInput
        case .audio: input = systemAudioInput
        case .microphone: input = micInput
        @unknown default: return
        }
        if input.isReadyForMoreMediaData {
            if input.append(sampleBuffer) {
                noteAppend(track: track)
            } else {
                reportFailure("append failed for \(type)")
            }
        } else {
            increment(\Recorder.notReadyByTrack, track: track)
        }
        if type == .audio {
            audioChunker?.submit(sampleBuffer, source: .systemAudio)
        } else if type == .microphone {
            audioChunker?.submit(sampleBuffer, source: .microphone)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log(
            "recording: stream stopped with error: \(error.localizedDescription)",
            event: "stream_failed",
            level: "error",
            fields: errorFields(error)
        )
        Task { await self.finish(exitCode: 2) }
    }

    func finish(exitCode: Int32) async {
        let shouldFinish: Bool = stateLock.withLock {
            guard !finishing else { return false }
            finishing = true
            return true
        }
        guard shouldFinish else { return }
        healthTimer?.cancel()
        healthTimer = nil

        if let stream {
            self.stream = nil
            do {
                try await stream.stopCapture()
            } catch {
                log("recording: stop capture failed: \(error.localizedDescription)", event: "stop_capture_failed", level: "warning", fields: errorFields(error))
            }
        }
        let snapshot: (sessionStarted: Bool, sampleCount: Int, tracks: [String: Int]) = queue.sync {
            self.videoInput.markAsFinished()
            self.systemAudioInput.markAsFinished()
            self.micInput.markAsFinished()
            return self.stateLock.withLock { (self.sessionStarted, self.appendedSamples, self.appendedByTrack) }
        }
        let chunkDrain = audioChunker?.stopAndDrain()
        if let chunkDrain, chunkDrain.ok {
            log("audio chunks drained", event: "chunk_drain_completed", fields: ["chunk_count": chunkDrain.chunkCount])
        }
        if snapshot.sessionStarted {
            await writer.finishWriting()
            if writer.status == .completed {
                let validation = await validateFinalizedOutput(appendedByTrack: snapshot.tracks)
                if validation.ok {
                    log(
                        "recording: finalized \(writer.outputURL.path) (samples=\(snapshot.sampleCount))",
                        event: "recording_finalized",
                        fields: ["output": writer.outputURL.path, "samples": snapshot.sampleCount, "tracks": snapshot.tracks].merging(validation.fields) { _, new in new }
                    )
                    if chunkDrain?.ok == true, let transcriberWorker {
                        let drained = await transcriberWorker.waitForDone(timeoutSeconds: 300)
                        if drained.ok {
                            log("transcription worker drained", event: "transcription_drained", fields: ["transcript": drained.transcriptPath ?? ""])
                            schedulePostProcess(exitCode)
                        } else {
                            log("transcription drain failed: \(drained.reason)", event: "postprocess_failed", level: "error", fields: ["stage": "transcription_drain"])
                            notify("文字起こしエラー", "逐次文字起こしの完了を確認できませんでした")
                            schedulePostProcess(exitCode == 0 ? 69 : exitCode)
                        }
                    } else if liveTranscriptionRequired {
                        let stage = chunkDrain == nil ? "chunker_unavailable" : "chunk_drain"
                        let detail = chunkDrain?.error ?? "live transcription was unavailable"
                        log("live transcription failed: \(detail)", event: "postprocess_failed", level: "error", fields: ["stage": stage])
                        schedulePostProcess(exitCode == 0 ? 69 : exitCode)
                    } else {
                        schedulePostProcess(exitCode)
                    }
                } else {
                    log(
                        "recording: finalized media validation failed: \(validation.reason)",
                        event: "finalized_media_invalid",
                        level: "error",
                        fields: ["samples": snapshot.sampleCount, "tracks": snapshot.tracks].merging(validation.fields) { _, new in new }
                    )
                    schedulePostProcess(exitCode == 0 ? 4 : exitCode)
                }
            } else {
                var fields: [String: Any] = ["samples": snapshot.sampleCount, "writer_status": writer.status.rawValue]
                if let error = writer.error { errorFields(error).forEach { fields[$0.key] = $0.value } }
                log(
                    "recording: \(describeWriterError("finalization failed")) (samples=\(snapshot.sampleCount))",
                    event: "finalization_failed",
                    level: "error",
                    fields: fields
                )
                schedulePostProcess(exitCode == 0 ? 2 : exitCode)
            }
        } else {
            writer.cancelWriting()
            log("recording: no frames captured, cancelled", event: "capture_empty", level: "error")
            schedulePostProcess(exitCode == 0 ? 3 : exitCode)
        }
    }

    private func validateFinalizedOutput(appendedByTrack: [String: Int]) async -> (ok: Bool, reason: String, fields: [String: Any]) {
        for track in ["screen", "system_audio", "microphone"] where appendedByTrack[track, default: 0] == 0 {
            return (false, "required track has no appended samples: \(track)", ["missing_track": track])
        }
        do {
            let asset = AVURLAsset(url: writer.outputURL)
            let videoTracks = try await asset.loadTracks(withMediaType: .video)
            let audioTracks = try await asset.loadTracks(withMediaType: .audio)
            let duration = try await asset.load(.duration)
            let seconds = CMTimeGetSeconds(duration)
            let fields: [String: Any] = [
                "video_track_count": videoTracks.count,
                "audio_track_count": audioTracks.count,
                "duration_seconds": seconds.isFinite ? seconds : -1,
            ]
            guard videoTracks.count == 1 else { return (false, "expected one video track", fields) }
            guard audioTracks.count >= 2 else { return (false, "expected system audio and microphone tracks", fields) }
            guard seconds.isFinite, seconds > 0 else { return (false, "media duration is not positive", fields) }
            return (true, "ok", fields)
        } catch {
            return (false, "could not inspect finalized media: \(error.localizedDescription)", errorFields(error))
        }
    }
}

// ---- helpers ----
func notify(_ subtitle: String, _ message: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = [
        "-e",
        "on run argv\ndisplay notification (item 2 of argv) with title \"Meeting Recorder\" subtitle (item 1 of argv)\nend run",
        subtitle,
        message,
    ]
    try? p.run()
}

// Menu bar indicator shown while recording ("🔴 REC mm:ss", click to stop).
final class StatusUI: NSObject {
    static let shared = StatusUI()
    private var item: NSStatusItem?
    private var timer: Timer?
    private var startedAt: Date?

    func showRecording() {
        DispatchQueue.main.async {
            self.startedAt = Date()
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            if let button = item.button {
                button.title = "🔴 REC"
                button.toolTip = "クリックで録画を停止して議事録を作成"
                button.target = self
                button.action = #selector(self.stopClicked)
            }
            self.item = item
            self.timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
                guard let start = self.startedAt else { return }
                let s = Int(Date().timeIntervalSince(start))
                self.item?.button?.title = String(format: "🔴 REC %02d:%02d", s / 60, s % 60)
            }
        }
    }

    func showProcessing() {
        DispatchQueue.main.async {
            self.timer?.invalidate()
            self.item?.button?.title = "📝 議事録作成中…"
            self.item?.button?.action = nil
        }
    }

    func hide() {
        DispatchQueue.main.async {
            self.timer?.invalidate()
            if let item = self.item { NSStatusBar.system.removeStatusItem(item) }
            self.item = nil
        }
    }

    @objc private func stopClicked() {
        requestStop(source: "menu_bar", signal: nil)
    }
}

let home = FileManager.default.homeDirectoryForCurrentUser.path
_ = umask(0o077)
let env = ProcessInfo.processInfo.environment
let baseDir = env["MEETING_RECORD_DIR"] ?? "\(home)/Movies/meeting-recordings"
let stateDir = "\(baseDir)/.state"
let pidFile = "\(stateDir)/current.pid"
let runFile = "\(stateDir)/current.run"
let lockFile = "\(stateDir)/current.lock"
let bundledScriptsDir = Bundle.main.resourceURL?.appendingPathComponent("Scripts").path
let scriptsDir = bundledScriptsDir ?? ""

func startResumeScan() {
    guard !scriptsDir.isEmpty else { return }
    let scanner = "\(scriptsDir)/resume_scan.py"
    guard FileManager.default.isReadableFile(atPath: scanner) else { return }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = ["python3", scanner, "--base-dir", baseDir]
    var environment = ProcessInfo.processInfo.environment
    environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin:/usr/bin:/bin"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process.environment = environment
    let scanLog = "\(stateDir)/resume-scan.log"
    FileManager.default.createFile(atPath: scanLog, contents: nil)
    if let handle = try? FileHandle(forWritingTo: URL(fileURLWithPath: scanLog)) {
        _ = try? handle.seekToEnd()
        process.standardOutput = handle
        process.standardError = handle
    }
    do {
        try process.run()
    } catch {
        FileHandle.standardError.write("resume scan launch failed: \(error.localizedDescription)\n".data(using: .utf8)!)
    }
}

// ---- main ----
var outputPath: String?
var fps = 10
var displayIndex = 0
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--fps":
        guard let value = args.first else {
            log("error: --fps requires a value", event: "invalid_arguments", level: "error")
            exit(64)
        }
        args.removeFirst()
        fps = Int(value) ?? 10
    case "--display":
        guard let value = args.first else {
            log("error: --display requires a value", event: "invalid_arguments", level: "error")
            exit(64)
        }
        args.removeFirst()
        displayIndex = Int(value) ?? 0
    default: outputPath = a
    }
}

var toggleMode = false
var runDir: String?
var instanceLockFD: Int32 = -1
if outputPath == nil {
    // App/toggle mode: stop a live recording, or start a new one.
    toggleMode = true
    try? FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
    instanceLockFD = open(lockFile, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
    guard instanceLockFD >= 0 else {
        log("error: could not open instance lock", event: "instance_lock_failed", level: "error")
        exit(73)
    }
    if flock(instanceLockFD, LOCK_EX | LOCK_NB) != 0 {
        if let pidStr = try? String(contentsOfFile: pidFile, encoding: .utf8),
           let pid = Int32(pidStr.trimmingCharacters(in: .whitespacesAndNewlines)),
           pid > 1,
           kill(pid, 0) == 0 {
            _ = kill(pid, SIGINT)
            exit(0)
        }
        log("error: another instance owns the lock but no valid recorder pid was found", event: "instance_state_invalid", level: "error")
        exit(74)
    }
    // Recovery is intentionally detached: stale runs must never delay a new recording.
    startResumeScan()
    let fmt = DateFormatter()
    fmt.dateFormat = "yyyyMMdd-HHmmss"
    let dir = "\(baseDir)/\(fmt.string(from: Date()))"
    do {
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    } catch {
        log("error: could not create run directory: \(error.localizedDescription)", event: "run_directory_failed", level: "error", fields: errorFields(error))
        exit(73)
    }
    _ = chmod(dir, 0o700)
    runDir = dir
    outputPath = "\(dir)/raw.mp4"
    freopen("\(dir)/recorder.log", "a", stderr)
    do {
        eventLogger = try EventLogger(path: "\(dir)/events.jsonl")
    } catch {
        structuredLoggingFailed = true
        FileHandle.standardError.write("structured logging unavailable: \(error.localizedDescription)\n".data(using: .utf8)!)
    }
    log(
        "run initialized",
        event: "run_started",
        fields: [
            "run_dir": dir,
            "app_version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
            "bundle_id": Bundle.main.bundleIdentifier ?? "unknown",
            "scripts_dir": scriptsDir,
        ]
    )
    try? "\(ProcessInfo.processInfo.processIdentifier)".write(toFile: pidFile, atomically: true, encoding: .utf8)
    try? dir.write(toFile: runFile, atomically: true, encoding: .utf8)
}

func collectDiagnostics(runDir: String, reason: String) {
    guard !scriptsDir.isEmpty else {
        log("diagnostics unavailable: bundled scripts directory missing", event: "diagnostics_failed", level: "error")
        return
    }
    log("diagnostics collection started", event: "diagnostics_started", fields: ["reason": reason])
    let diagnostics = Process()
    diagnostics.executableURL = URL(fileURLWithPath: "/bin/bash")
    diagnostics.arguments = ["\(scriptsDir)/collect_diagnostics.sh", runDir, reason]
    var diagnosticsEnvironment = ProcessInfo.processInfo.environment
    diagnosticsEnvironment["MEETING_RECORDER_VERSION"] = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development"
    diagnostics.environment = diagnosticsEnvironment
    do {
        try diagnostics.run()
        diagnostics.waitUntilExit()
        let event = diagnostics.terminationStatus == 0 ? "diagnostics_finished" : "diagnostics_failed"
        let level = diagnostics.terminationStatus == 0 ? "info" : "error"
        log(
            "diagnostics collection exited with \(diagnostics.terminationStatus)",
            event: event,
            level: level,
            fields: ["exit_code": diagnostics.terminationStatus]
        )
    } catch {
        log("diagnostics could not start: \(error.localizedDescription)", event: "diagnostics_failed", level: "error", fields: errorFields(error))
    }
}

func postProcess(exitCode: Int32) -> Never {
    var effectiveExitCode = exitCode
    if loggingStateLock.withLock({ structuredLoggingFailed }), effectiveExitCode == 0 {
        effectiveExitCode = 75
        if let runDir { collectDiagnostics(runDir: runDir, reason: "structured-logging-failed") }
    }
    if toggleMode {
        try? FileManager.default.removeItem(atPath: pidFile)
        try? FileManager.default.removeItem(atPath: runFile)
        if exitCode == 0, let runDir {
            StatusUI.shared.showProcessing()
            notify("録画停止", "議事録を作成しています...")
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = [
                "python3", "\(scriptsDir)/postprocess_runner.py",
                "--run-dir", runDir,
            ]
            var postprocessEnvironment = ProcessInfo.processInfo.environment
            postprocessEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"
            p.environment = postprocessEnvironment
            do {
                log("post-processing started", event: "postprocess_started")
                try p.run()
                p.waitUntilExit()
                if p.terminationStatus == 0 {
                    log("post-processing completed", event: "postprocess_completed")
                } else {
                    effectiveExitCode = 69
                    log(
                        "post-processing failed with exit \(p.terminationStatus)",
                        event: "postprocess_failed",
                        level: "error",
                        fields: ["exit_code": p.terminationStatus]
                    )
                    collectDiagnostics(runDir: runDir, reason: "postprocess-exit-\(p.terminationStatus)")
                }
            } catch {
                effectiveExitCode = 70
                log("post-processing could not start: \(error.localizedDescription)", event: "postprocess_launch_failed", level: "error", fields: errorFields(error))
                collectDiagnostics(runDir: runDir, reason: "postprocess-launch-failed")
                notify("エラー", "後処理を起動できません: \(error.localizedDescription)")
            }
        } else if exitCode != 0 {
            if let runDir { collectDiagnostics(runDir: runDir, reason: "exit-\(exitCode)") }
            if exitCode != 67 {
                notify("録画エラー", "録画を中断し診断ログを保存しました。recorder.log を確認してください")
            }
        }
        log("run finished with exit \(effectiveExitCode)", event: "run_finished", level: effectiveExitCode == 0 ? "info" : "error", fields: ["exit_code": effectiveExitCode])
    }
    exit(effectiveExitCode)
}

func schedulePostProcess(_ exitCode: Int32) {
    DispatchQueue.global(qos: .utility).async {
        postProcess(exitCode: exitCode)
    }
}

func captureDimensions(for display: SCDisplay) -> (width: Int, height: Int, scale: Double) {
    let screen = NSScreen.screens.first { screen in
        guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else { return false }
        return CGDirectDisplayID(number.uint32Value) == display.displayID
    }
    let scale = screen?.backingScaleFactor ?? 1.0
    return (
        max(1, Int((Double(display.width) * scale).rounded())),
        max(1, Int((Double(display.height) * scale).rounded())),
        scale
    )
}

func missingPostProcessDependencies() -> [String] {
    let searchPaths = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin", "/usr/bin", "/bin"]
    return ["ffmpeg", "ffprobe", "python3", "kanary"].filter { command in
        !searchPaths.contains { FileManager.default.isExecutableFile(atPath: "\($0)/\(command)") }
    }
}

let outputURL = URL(fileURLWithPath: outputPath!)
try? FileManager.default.removeItem(at: outputURL)

var recorderRef: Recorder?
private let lifecycleLock = NSLock()
private var stopPending = false

func requestStop(source: String, signal: Int32?) {
    let recorder = lifecycleLock.withLock { () -> Recorder? in
        stopPending = true
        return recorderRef
    }
    var fields: [String: Any] = ["source": source]
    if let signal { fields["signal"] = signal }
    log("recording: stop requested", event: "stop_requested", fields: fields)
    if let recorder {
        Task { await recorder.finish(exitCode: 0) }
    }
}

for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { requestStop(source: "signal", signal: sig) }
    src.resume()
    _ = Unmanaged.passRetained(src as AnyObject)
}

Task {
    do {
        // Don't gate on CGPreflightScreenCaptureAccess(): on macOS 15+ it can
        // report false even when ScreenCaptureKit capture is allowed. Try the
        // capture directly and fall back to a permission request on failure.
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            log(
                "shareable content failed: \(error.localizedDescription); requesting access...",
                event: "permission_failed",
                level: "error",
                fields: errorFields(error)
            )
            _ = CGRequestScreenCaptureAccess()
            notify("権限が必要です", "システム設定で Meeting Recorder に画面収録を許可し、再度お試しください")
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            p.arguments = ["x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"]
            try? p.run()
            postProcess(exitCode: 67)
        }
        guard displayIndex < content.displays.count else {
            log("error: display index \(displayIndex) not found (have \(content.displays.count))", event: "display_not_found", level: "error")
            postProcess(exitCode: 65)
        }
        let display = content.displays[displayIndex]
        if toggleMode {
            let missing = missingPostProcessDependencies()
            guard missing.isEmpty else {
                log("error: missing post-processing dependencies: \(missing.joined(separator: ", "))", event: "dependencies_missing", level: "error", fields: ["missing": missing])
                notify("準備が必要です", "必要なコマンドがありません: \(missing.joined(separator: ", "))")
                postProcess(exitCode: 69)
            }
        }
        let dimensions = captureDimensions(for: display)
        log("capture dimensions selected", event: "capture_dimensions", fields: ["width": dimensions.width, "height": dimensions.height, "scale": dimensions.scale])
        let rec = try Recorder(
            outputURL: outputURL,
            width: dimensions.width,
            height: dimensions.height,
            runDirectory: runDir.map { URL(fileURLWithPath: $0, isDirectory: true) },
            scriptsDirectory: scriptsDir
        )
        let shouldStop = lifecycleLock.withLock { () -> Bool in
            recorderRef = rec
            return stopPending
        }
        if shouldStop {
            log("recording: startup cancelled by pending stop", event: "startup_stop_applied")
            await rec.finish(exitCode: 0)
            return
        }
        try await rec.start(display: display, fps: fps)
        if lifecycleLock.withLock({ stopPending }) {
            await rec.finish(exitCode: 0)
            return
        }
        if toggleMode {
            notify("録画開始", "⌘⌥⌃⇧R かメニューバーの🔴で停止して議事録を作成します")
            StatusUI.shared.showRecording()
        }
    } catch {
        log("error: \(error.localizedDescription)", event: "startup_failed", level: "error", fields: errorFields(error))
        postProcess(exitCode: 66)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
