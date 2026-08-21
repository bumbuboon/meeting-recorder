import Foundation

@main
enum AudioChunkerWatermarkTest {
    static func main() {
        precondition(AudioChunker.effectiveWatermark(processed: 100, dropped: 122, pendingMinimum: 90) == 90)
        precondition(AudioChunker.effectiveWatermark(processed: 100, dropped: 122, pendingMinimum: nil) == 122)
        precondition(AudioChunker.effectiveWatermark(processed: 100, dropped: nil, pendingMinimum: 95) == 95)
        precondition(AudioChunker.effectiveWatermark(processed: nil, dropped: nil, pendingMinimum: 90) == nil)
    }
}
