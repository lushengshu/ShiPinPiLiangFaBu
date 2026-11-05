import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

struct VisionFrame: Encodable {
    let timestamp: Double
    let path: String
    let ocr: [String]
    let ocrConfidence: [Double]
    let labels: [[String: String]]
}

struct VisionResult: Encodable {
    let ok: Bool
    let video: String
    let duration: Double
    let frames: [VisionFrame]
    let error: String?
}

func fail(_ video: String, _ message: String) -> Never {
    let result = VisionResult(ok: false, video: video, duration: 0, frames: [], error: message)
    let encoder = JSONEncoder()
    if let data = try? encoder.encode(result) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
    exit(1)
}

func writeJPEG(_ image: CGImage, to url: URL) throws {
    guard let destination = CGImageDestinationCreateWithURL(
        url as CFURL,
        UTType.jpeg.identifier as CFString,
        1,
        nil
    ) else {
        throw NSError(domain: "VideoVision", code: 2, userInfo: [NSLocalizedDescriptionKey: "无法创建 JPEG 输出"])
    }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.78] as CFDictionary)
    guard CGImageDestinationFinalize(destination) else {
        throw NSError(domain: "VideoVision", code: 3, userInfo: [NSLocalizedDescriptionKey: "无法写入 JPEG 输出"])
    }
}

func recognize(_ image: CGImage) -> (ocr: [String], ocrConfidence: [Double], labels: [[String: String]]) {
    var text: [String] = []
    var textConfidence: [Double] = []
    var labels: [[String: String]] = []
    let textRequest = VNRecognizeTextRequest { request, _ in
        guard let observations = request.results as? [VNRecognizedTextObservation] else { return }
        for observation in observations {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let value = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else { continue }
            text.append(value)
            textConfidence.append(Double(candidate.confidence))
        }
    }
    textRequest.recognitionLevel = .accurate
    textRequest.usesLanguageCorrection = true
    textRequest.recognitionLanguages = ["zh-Hans", "en-US"]
    textRequest.minimumTextHeight = 0.012

    let classifyRequest = VNClassifyImageRequest { request, _ in
        guard let observations = request.results as? [VNClassificationObservation] else { return }
        labels = observations.prefix(8).map {
            ["identifier": $0.identifier, "confidence": String(format: "%.3f", $0.confidence)]
        }
    }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try? handler.perform([textRequest, classifyRequest])
    return (text, textConfidence, labels)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count >= 2 else {
    fail("", "用法：video_vision <视频路径> <输出目录> [帧数]")
}
let videoURL = URL(fileURLWithPath: arguments[0]).standardizedFileURL
let outputURL = URL(fileURLWithPath: arguments[1], isDirectory: true).standardizedFileURL
let frameCount = max(3, min(Int(arguments.dropFirst(2).first ?? "8") ?? 8, 24))

try? FileManager.default.createDirectory(at: outputURL, withIntermediateDirectories: true)
let asset = AVURLAsset(url: videoURL)
let duration = CMTimeGetSeconds(asset.duration)
guard duration.isFinite, duration > 0 else {
    fail(videoURL.path, "无法读取视频时长")
}
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.maximumSize = CGSize(width: 768, height: 768)
generator.requestedTimeToleranceBefore = CMTime(seconds: 0.25, preferredTimescale: 600)
generator.requestedTimeToleranceAfter = CMTime(seconds: 0.25, preferredTimescale: 600)

var frames: [VisionFrame] = []
var seenRequestedTicks = Set<Int>()
var seenActualTicks = Set<Int>()
for index in 0..<frameCount {
    // Avoid the first/last GOP boundary; evenly spaced samples provide a
    // temporal spread instead of treating one thumbnail as the whole video.
    let fraction = (Double(index) + 0.5) / Double(frameCount)
    let seconds = min(max(duration * fraction, 0), max(duration - 0.05, 0))
    let requestedTicks = Int((seconds * 100.0).rounded())
    if seenRequestedTicks.contains(requestedTicks) {
        continue
    }
    seenRequestedTicks.insert(requestedTicks)
    let requested = CMTime(seconds: seconds, preferredTimescale: 600)
    do {
        var actual = CMTime.zero
        let image = try generator.copyCGImage(at: requested, actualTime: &actual)
        let actualSeconds = CMTimeGetSeconds(actual).isFinite ? CMTimeGetSeconds(actual) : seconds
        // Different requested times can resolve to one key frame on short
        // clips.  Keep only the first occurrence at a 10ms resolution.
        let actualTicks = Int((actualSeconds * 100.0).rounded())
        if seenActualTicks.contains(actualTicks) {
            continue
        }
        seenActualTicks.insert(actualTicks)
        let filename = String(format: "frame_%02d_%06.2f.jpg", frames.count + 1, actualSeconds)
        let frameURL = outputURL.appendingPathComponent(filename)
        try writeJPEG(image, to: frameURL)
        let result = recognize(image)
        frames.append(VisionFrame(timestamp: actualSeconds, path: frameURL.path, ocr: result.ocr, ocrConfidence: result.ocrConfidence, labels: result.labels))
    } catch {
        // A damaged GOP should not discard evidence from the other frames.
        continue
    }
}

let result = VisionResult(ok: !frames.isEmpty, video: videoURL.path, duration: duration, frames: frames, error: frames.isEmpty ? "没有成功提取视频帧" : nil)
let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
if let data = try? encoder.encode(result) {
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}
