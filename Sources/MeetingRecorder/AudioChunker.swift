import AVFoundation
import AudioToolbox
import CoreMedia
import Darwin
import Foundation

private final class ChunkEventWriter: @unchecked Sendable {
    private let fd: Int32
    private let lock = NSLock()

    init(path: String) throws {
        fd = open(path, O_CREAT | O_APPEND | O_WRONLY, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        _ = fchmod(fd, S_IRUSR | S_IWUSR)
    }

    deinit { close(fd) }

    func append(event: String, fields: [String: Any] = [:]) throws {
        try lock.withLock {
            var object = fields
            object["schema"] = "meeting-recorder.chunk-event.v1"
            object["event"] = event
            object["event_id"] = UUID().uuidString.lowercased()
            object["occurred_at"] = ISO8601DateFormatter().string(from: Date())
            guard JSONSerialization.isValidJSONObject(object) else {
                throw NSError(domain: "MeetingRecorder.ChunkEventWriter", code: 1)
            }
            var data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
            data.append(0x0a)
            let count = data.withUnsafeBytes { bytes -> Int in
                guard let base = bytes.baseAddress else { return -1 }
                return Darwin.write(fd, base, bytes.count)
            }
            guard count == data.count else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
            guard fsync(fd) == 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        }
    }
}

private struct QueuedAudioSample: @unchecked Sendable {
    let sampleBuffer: CMSampleBuffer
    let source: AudioChunker.Source
}

private struct DroppedAudioGap: Sendable {
    let source: AudioChunker.Source
    let reason: String
    var duration: Double
    let pts: Double
    var buffers: Int
}

private final class ChunkAccumulator {
    let id: Int
    let startFrame: Int64
    var system: [Float]
    var microphone: [Float]
    var firstPTS: Double?

    init(id: Int, framesPerChunk: Int) {
        self.id = id
        startFrame = Int64(id * framesPerChunk)
        system = Array(repeating: 0, count: framesPerChunk)
        microphone = Array(repeating: 0, count: framesPerChunk)
    }
}

final class AudioChunker: @unchecked Sendable {
    enum Source: String { case systemAudio = "system_audio", microphone }

    struct DrainResult {
        let ok: Bool
        let chunkCount: Int
        let error: String?
    }

    private static let sampleRate = 48_000
    private static let chunkSeconds = 120
    private static let framesPerChunk = sampleRate * chunkSeconds
    private static let finalizeSlackFrames = sampleRate

    private let condition = NSCondition()
    private let stopped = DispatchSemaphore(value: 0)
    private let queueLimit: Int
    private var pending: [QueuedAudioSample] = []
    private var pendingGaps: [DroppedAudioGap] = []
    private var accepting = true
    private var sessionStartPTS: CMTime?
    private var audioOffsetWritten = false
    private var workerThread: Thread?
    private var accumulators: [Int: ChunkAccumulator] = [:]
    private var finalizedThrough = -1
    private var maximumFrame: Int64 = 0
    private var maximumFrameBySource: [Source: Int64] = [:]
    private var droppedMaximumEndFrameBySource: [Source: Int64] = [:]
    private var chunkCount = 0
    private var failure: Error?
    private let chunksDirectory: URL
    private let stateDirectory: URL
    private let events: ChunkEventWriter

    static func effectiveWatermark(
        processed: Int64?,
        dropped: Int64?,
        pendingMinimum: Int64?
    ) -> Int64? {
        guard let observed = [processed, dropped].compactMap({ $0 }).max() else { return nil }
        return pendingMinimum.map { min(observed, $0) } ?? observed
    }

    init(runDirectory: URL, queueLimit: Int = 512) throws {
        self.queueLimit = queueLimit
        chunksDirectory = runDirectory.appendingPathComponent("audio-chunks", isDirectory: true)
        stateDirectory = runDirectory.appendingPathComponent("chunks", isDirectory: true)
        try FileManager.default.createDirectory(at: chunksDirectory, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: stateDirectory, withIntermediateDirectories: true)
        events = try ChunkEventWriter(path: stateDirectory.appendingPathComponent("recorder.events.jsonl").path)
        let thread = Thread { [weak self] in self?.consume() }
        thread.name = "meeting-recorder.audio-chunker"
        thread.qualityOfService = .utility
        workerThread = thread
        thread.start()
    }

    func setSessionStartPTS(_ pts: CMTime) {
        condition.withLock {
            if sessionStartPTS == nil { sessionStartPTS = pts }
        }
    }

    func submit(_ sampleBuffer: CMSampleBuffer, source: Source) {
        var copied: CMSampleBuffer?
        guard CMSampleBufferCreateCopy(allocator: kCFAllocatorDefault, sampleBuffer: sampleBuffer, sampleBufferOut: &copied) == noErr,
              let copied
        else {
            recordDrop(sampleBuffer, source: source, reason: "copy_failed")
            return
        }
        condition.lock()
        guard accepting else {
            condition.unlock()
            return
        }
        if pending.count >= queueLimit {
            condition.unlock()
            recordDrop(copied, source: source, reason: "queue_full")
            return
        }
        pending.append(QueuedAudioSample(sampleBuffer: copied, source: source))
        condition.signal()
        condition.unlock()
    }

    func stopAndDrain() -> DrainResult {
        condition.withLock {
            accepting = false
            condition.broadcast()
        }
        stopped.wait()
        workerThread = nil
        do { try createAtomicSentinel(named: "END") } catch { failure = failure ?? error }
        return DrainResult(ok: failure == nil, chunkCount: chunkCount, error: failure?.localizedDescription)
    }

    private func consume() {
        defer { stopped.signal() }
        while true {
            let work: (QueuedAudioSample?, DroppedAudioGap?) = condition.withLock {
                while pending.isEmpty && pendingGaps.isEmpty && accepting { condition.wait() }
                if !pendingGaps.isEmpty { return (nil, pendingGaps.removeFirst()) }
                if !pending.isEmpty {
                    let index = pending.indices.min {
                        CMTimeCompare(
                            CMSampleBufferGetPresentationTimeStamp(pending[$0].sampleBuffer),
                            CMSampleBufferGetPresentationTimeStamp(pending[$1].sampleBuffer)
                        ) < 0
                    } ?? pending.startIndex
                    return (pending.remove(at: index), nil)
                }
                return (nil, nil)
            }
            if let gap = work.1 {
                do {
                    try events.append(event: "chunk_drop_gap", fields: [
                        "source": gap.source.rawValue,
                        "reason": gap.reason,
                        "gap_seconds": gap.duration,
                        "pts_seconds": gap.pts.isFinite ? gap.pts : -1,
                        "queue_limit": queueLimit,
                        "dropped_buffers": gap.buffers,
                    ])
                } catch {
                    failure = error
                }
                continue
            }
            guard let item = work.0 else { break }
            guard failure == nil else { continue }
            do { try process(item) } catch {
                failure = error
                try? events.append(event: "chunker_failed", fields: ["error": error.localizedDescription])
            }
        }
        guard failure == nil else { return }
        do { try finalizeAll() } catch {
            failure = error
            try? events.append(event: "chunker_failed", fields: ["error": error.localizedDescription])
        }
    }

    private func process(_ item: QueuedAudioSample) throws {
        guard let origin = condition.withLock({ sessionStartPTS }) else {
            try events.append(event: "chunk_drop_gap", fields: [
                "source": item.source.rawValue,
                "reason": "session_origin_unavailable",
                "gap_seconds": sampleDuration(item.sampleBuffer),
            ])
            return
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(item.sampleBuffer)
        let offset = CMTimeGetSeconds(CMTimeSubtract(pts, origin))
        guard offset.isFinite else { throw NSError(domain: "MeetingRecorder.AudioChunker", code: 2) }
        if !audioOffsetWritten {
            audioOffsetWritten = true
            try events.append(event: "session_offset", fields: [
                "offset_seconds": offset,
                "audio_start_pts_seconds": CMTimeGetSeconds(pts),
                "session_start_pts_seconds": CMTimeGetSeconds(origin),
            ])
        }
        let samples = try Self.monoSamples(from: item.sampleBuffer)
        var absoluteFrame = Int64((offset * Double(Self.sampleRate)).rounded())
        var sourceIndex = 0
        if absoluteFrame < 0 {
            sourceIndex = min(samples.count, Int(-absoluteFrame))
            absoluteFrame = 0
        }
        while sourceIndex < samples.count {
            let chunkID = Int(absoluteFrame / Int64(Self.framesPerChunk))
            if chunkID <= finalizedThrough {
                let remaining = samples.count - sourceIndex
                try events.append(event: "chunk_drop_gap", fields: [
                    "source": item.source.rawValue,
                    "reason": "late_after_finalize",
                    "gap_seconds": Double(remaining) / Double(Self.sampleRate),
                    "start_abs": Double(absoluteFrame) / Double(Self.sampleRate),
                ])
                break
            }
            let accumulator = accumulators[chunkID] ?? ChunkAccumulator(id: chunkID, framesPerChunk: Self.framesPerChunk)
            accumulators[chunkID] = accumulator
            let offsetInChunk = Int(absoluteFrame - accumulator.startFrame)
            let length = min(samples.count - sourceIndex, Self.framesPerChunk - offsetInChunk)
            if accumulator.firstPTS == nil { accumulator.firstPTS = CMTimeGetSeconds(pts) }
            if item.source == .systemAudio {
                accumulator.system.replaceSubrange(offsetInChunk..<(offsetInChunk + length), with: samples[sourceIndex..<(sourceIndex + length)])
            } else {
                accumulator.microphone.replaceSubrange(offsetInChunk..<(offsetInChunk + length), with: samples[sourceIndex..<(sourceIndex + length)])
            }
            sourceIndex += length
            absoluteFrame += Int64(length)
            maximumFrame = max(maximumFrame, absoluteFrame)
            maximumFrameBySource[item.source] = max(maximumFrameBySource[item.source, default: 0], absoluteFrame)
        }
        try finalizeEligibleChunks()
    }

    private func finalizeEligibleChunks() throws {
        let (dropped, pendingMinimum): ([Source: Int64], [Source: Int64]) = condition.withLock {
            var minimum: [Source: Int64] = [:]
            if let origin = sessionStartPTS {
                for item in pending {
                    let relative = CMTimeGetSeconds(CMTimeSubtract(
                        CMSampleBufferGetPresentationTimeStamp(item.sampleBuffer), origin
                    ))
                    guard relative.isFinite else { continue }
                    let frame = Int64((relative * Double(Self.sampleRate)).rounded())
                    minimum[item.source] = min(minimum[item.source, default: frame], frame)
                }
            }
            return (droppedMaximumEndFrameBySource, minimum)
        }
        guard let systemWatermark = Self.effectiveWatermark(
                  processed: maximumFrameBySource[.systemAudio],
                  dropped: dropped[.systemAudio],
                  pendingMinimum: pendingMinimum[.systemAudio]
              ),
              let microphoneWatermark = Self.effectiveWatermark(
                  processed: maximumFrameBySource[.microphone],
                  dropped: dropped[.microphone],
                  pendingMinimum: pendingMinimum[.microphone]
              )
        else { return }
        let safeWatermark = min(systemWatermark, microphoneWatermark) - Int64(Self.finalizeSlackFrames)
        let eligibleThrough = Int(safeWatermark / Int64(Self.framesPerChunk)) - 1
        guard eligibleThrough > finalizedThrough else { return }
        for id in (finalizedThrough + 1)...eligibleThrough { try finalize(id: id, frameCount: Self.framesPerChunk) }
    }

    private func finalizeAll() throws {
        let dropped = condition.withLock { droppedMaximumEndFrameBySource.values.max() ?? 0 }
        maximumFrame = max(maximumFrame, dropped)
        guard maximumFrame > 0 else { return }
        let lastID = Int((maximumFrame - 1) / Int64(Self.framesPerChunk))
        for id in (finalizedThrough + 1)...lastID {
            let frameCount = id == lastID ? Int(maximumFrame - Int64(id * Self.framesPerChunk)) : Self.framesPerChunk
            try finalize(id: id, frameCount: frameCount)
        }
    }

    private func finalize(id: Int, frameCount: Int) throws {
        let accumulator = accumulators.removeValue(forKey: id) ?? ChunkAccumulator(id: id, framesPerChunk: Self.framesPerChunk)
        let mixed = zip(accumulator.system.prefix(frameCount), accumulator.microphone.prefix(frameCount)).map { system, microphone in
            max(-1, min(1, system + microphone))
        }
        let name = String(format: "chunk_%04d.wav", id)
        let destination = chunksDirectory.appendingPathComponent(name)
        let temporary = chunksDirectory.appendingPathComponent(".\(name).tmp")
        try Self.writeWAV(samples: mixed, to: temporary)
        if rename(temporary.path, destination.path) != 0 { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        let directoryFD = open(chunksDirectory.path, O_RDONLY)
        if directoryFD >= 0 { _ = fsync(directoryFD); close(directoryFD) }
        let startAbs = Double(accumulator.startFrame) / Double(Self.sampleRate)
        var fields: [String: Any] = [
            "chunk_id": id,
            "path": destination.path,
            "start_abs": startAbs,
            "duration_seconds": Double(frameCount) / Double(Self.sampleRate),
            "sample_rate": Self.sampleRate,
        ]
        if let firstPTS = accumulator.firstPTS { fields["measured_first_pts_seconds"] = firstPTS }
        try events.append(event: "chunk_ready", fields: fields)
        finalizedThrough = id
        chunkCount += 1
    }

    private func recordDrop(_ sampleBuffer: CMSampleBuffer, source: Source, reason: String) {
        let duration = sampleDuration(sampleBuffer)
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        condition.lock()
        if let origin = sessionStartPTS {
            let relativePTS = CMTimeGetSeconds(CMTimeSubtract(CMSampleBufferGetPresentationTimeStamp(sampleBuffer), origin))
            if relativePTS.isFinite {
                let endFrame = Int64(((relativePTS + duration) * Double(Self.sampleRate)).rounded())
                droppedMaximumEndFrameBySource[source] = max(droppedMaximumEndFrameBySource[source, default: 0], endFrame)
            }
        }
        if pendingGaps.count >= 1_024,
           let index = pendingGaps.firstIndex(where: { $0.source == source && $0.reason == reason }) {
            pendingGaps[index].duration += duration
            pendingGaps[index].buffers += 1
        } else {
            pendingGaps.append(DroppedAudioGap(source: source, reason: reason, duration: duration, pts: pts, buffers: 1))
        }
        condition.signal()
        condition.unlock()
    }

    private func sampleDuration(_ sampleBuffer: CMSampleBuffer) -> Double {
        let duration = CMTimeGetSeconds(CMSampleBufferGetDuration(sampleBuffer))
        if duration.isFinite && duration > 0 { return duration }
        return Double(CMSampleBufferGetNumSamples(sampleBuffer)) / Double(Self.sampleRate)
    }

    private func createAtomicSentinel(named name: String) throws {
        let destination = stateDirectory.appendingPathComponent(name)
        let temporary = stateDirectory.appendingPathComponent(".\(name).\(UUID().uuidString).tmp")
        let fd = open(temporary.path, O_CREAT | O_EXCL | O_WRONLY, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        guard fsync(fd) == 0 else { close(fd); throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        close(fd)
        guard rename(temporary.path, destination.path) == 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        let directoryFD = open(stateDirectory.path, O_RDONLY)
        if directoryFD >= 0 { _ = fsync(directoryFD); close(directoryFD) }
    }

    private static func monoSamples(from sampleBuffer: CMSampleBuffer) throws -> [Float] {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let inputFormat = AVAudioFormat(streamDescription: streamDescription)
        else { throw NSError(domain: "MeetingRecorder.AudioChunker", code: 3) }
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0, let input = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frameCount) else { return [] }
        input.frameLength = frameCount
        var retainedBlock: CMBlockBuffer?
        let bufferListSize = MemoryLayout<AudioBufferList>.size
            + max(0, Int(inputFormat.channelCount) - 1) * MemoryLayout<AudioBuffer>.size
        let rawList = UnsafeMutableRawPointer.allocate(
            byteCount: bufferListSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { rawList.deallocate() }
        let sourceList = rawList.bindMemory(to: AudioBufferList.self, capacity: 1)
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: sourceList,
            bufferListSize: bufferListSize,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0,
            blockBufferOut: &retainedBlock
        )
        guard status == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        let sourceBuffers = UnsafeMutableAudioBufferListPointer(sourceList)
        let destinationBuffers = UnsafeMutableAudioBufferListPointer(input.mutableAudioBufferList)
        guard sourceBuffers.count == destinationBuffers.count else { throw NSError(domain: "MeetingRecorder.AudioChunker", code: 4) }
        for index in 0..<sourceBuffers.count {
            let source = sourceBuffers[index]
            let bytes = min(Int(source.mDataByteSize), Int(destinationBuffers[index].mDataByteSize))
            guard let sourceData = source.mData, let destinationData = destinationBuffers[index].mData else { continue }
            memcpy(destinationData, sourceData, bytes)
        }
        guard let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: Double(sampleRate), channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
        else { throw NSError(domain: "MeetingRecorder.AudioChunker", code: 5) }
        let ratio = outputFormat.sampleRate / inputFormat.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(frameCount) * ratio)) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { throw NSError(domain: "MeetingRecorder.AudioChunker", code: 6) }
        var supplied = false
        var conversionError: NSError?
        let conversionStatus = converter.convert(to: output, error: &conversionError) { _, statusPointer in
            if supplied {
                statusPointer.pointee = .endOfStream
                return nil
            }
            supplied = true
            statusPointer.pointee = .haveData
            return input
        }
        guard conversionStatus != .error, conversionError == nil, let channel = output.floatChannelData?[0] else {
            throw conversionError ?? NSError(domain: "MeetingRecorder.AudioChunker", code: 7)
        }
        return Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
    }

    private static func writeWAV(samples: [Float], to path: URL) throws {
        var data = Data(capacity: 44 + samples.count * 2)
        func appendASCII(_ string: String) { data.append(string.data(using: .ascii)!) }
        func appendLE<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        let payloadBytes = UInt32(samples.count * 2)
        appendASCII("RIFF"); appendLE(UInt32(36) + payloadBytes); appendASCII("WAVE")
        appendASCII("fmt "); appendLE(UInt32(16)); appendLE(UInt16(1)); appendLE(UInt16(1))
        appendLE(UInt32(sampleRate)); appendLE(UInt32(sampleRate * 2)); appendLE(UInt16(2)); appendLE(UInt16(16))
        appendASCII("data"); appendLE(payloadBytes)
        for sample in samples { appendLE(Int16(max(-32768, min(32767, Int((sample * 32767).rounded()))))) }
        try data.write(to: path, options: [])
        let fd = open(path.path, O_RDONLY)
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        guard fsync(fd) == 0 else { close(fd); throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        close(fd)
    }
}
