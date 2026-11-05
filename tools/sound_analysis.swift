import AVFoundation
import Foundation
import SoundAnalysis

struct SoundLabel: Codable {
    let label: String
    let confidence: Double
}

struct SoundSegment: Codable {
    let start: Double
    let duration: Double
    let labels: [SoundLabel]
}

struct SoundOutput: Codable {
    let ok: Bool
    let path: String
    let windowSeconds: Double
    let segments: [SoundSegment]
    let error: String?
}

func emit(_ output: SoundOutput) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(output) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
    exit(output.ok ? 0 : 1)
}

final class Observer: NSObject, SNResultsObserving {
    var segments: [SoundSegment] = []
    var errorMessage: String?
    let topK: Int

    init(topK: Int) {
        self.topK = topK
    }

    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let classification = result as? SNClassificationResult else { return }
        let labels = classification.classifications.prefix(topK).map {
            SoundLabel(label: $0.identifier, confidence: Double($0.confidence))
        }
        segments.append(SoundSegment(
            start: CMTimeGetSeconds(classification.timeRange.start),
            duration: CMTimeGetSeconds(classification.timeRange.duration),
            labels: labels
        ))
    }

    func request(_ request: SNRequest, didFailWithError error: Error) {
        errorMessage = error.localizedDescription
    }

    func requestDidComplete(_ request: SNRequest) {}
}

let arguments = Array(CommandLine.arguments.dropFirst())
let path = arguments.first ?? ""
let window = max(1, Double(arguments.dropFirst().first ?? "5") ?? 5)
let topK = max(1, Int(arguments.dropFirst(2).first ?? "8") ?? 8)
guard !path.isEmpty else {
    emit(SoundOutput(ok: false, path: path, windowSeconds: window, segments: [], error: "缺少音频/视频路径"))
}

let analyzer: SNAudioFileAnalyzer
do {
    analyzer = try SNAudioFileAnalyzer(url: URL(fileURLWithPath: path))
} catch {
    emit(SoundOutput(ok: false, path: path, windowSeconds: window, segments: [], error: error.localizedDescription))
}

let request: SNClassifySoundRequest
do {
    request = try SNClassifySoundRequest(classifierIdentifier: .version1)
} catch {
    emit(SoundOutput(ok: false, path: path, windowSeconds: window, segments: [], error: error.localizedDescription))
}
request.windowDuration = CMTime(seconds: window, preferredTimescale: 44100)
request.overlapFactor = 0
let observer = Observer(topK: topK)
do {
    try analyzer.add(request, withObserver: observer)
} catch {
    emit(SoundOutput(ok: false, path: path, windowSeconds: window, segments: [], error: error.localizedDescription))
}
let semaphore = DispatchSemaphore(value: 0)
analyzer.analyze { _ in semaphore.signal() }
semaphore.wait()
emit(SoundOutput(
    ok: observer.errorMessage == nil,
    path: path,
    windowSeconds: window,
    segments: observer.segments,
    error: observer.errorMessage
))
