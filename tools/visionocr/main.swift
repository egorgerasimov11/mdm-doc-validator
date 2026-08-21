// visionocr — Apple Vision OCR as a line-oriented CLI for the mdmdoc benchmark.
//
//   visionocr --mode legacy|document [--langs en-US,ko-KR,...] [--info]
//
// Reads image paths from stdin (one per line) and prints ONE JSON object per
// line: {"path","text","lines":[{"text","conf","bbox":[x,y,w,h]}],"markdown","ms","error"}
// bbox is in normalized top-left coordinates (x,y,w,h in 0..1).
//
//   legacy    VNRecognizeTextRequest, .accurate, language correction on, automatic
//             language detection — lines sorted into reading order by bbox.
//   document  RecognizeDocumentsRequest (macOS 26+): paragraphs / lists / tables in
//             document order; tables are emitted as Markdown pipe rows.
//
// Build:  xcrun swiftc -O -o visionocr main.swift   (see build.sh)

import Foundation
import Vision
import CoreImage

struct Line: Codable {
    var text: String
    var conf: Double
    var bbox: [Double]
}

struct Out: Codable {
    var path: String
    var text: String
    var lines: [Line]
    var markdown: String
    var ms: Int
    var error: String?
}

func jsonLine(_ o: Out) {
    let enc = JSONEncoder()
    enc.outputFormatting = [.withoutEscapingSlashes]
    if let d = try? enc.encode(o), let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func loadCGImage(_ path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

// Reading order: group by row (vertical overlap), then left→right.
func sortReadingOrder(_ ls: [Line]) -> [Line] {
    // bbox: [x, y(top), w, h]; rows are grouped when vertical centers are within 0.6*height
    let sorted = ls.sorted { ($0.bbox[1]) < ($1.bbox[1]) }
    var rows: [[Line]] = []
    for l in sorted {
        let cy = l.bbox[1] + l.bbox[3] / 2
        if var last = rows.last, let ref = last.first {
            let rcy = ref.bbox[1] + ref.bbox[3] / 2
            let tol = max(ref.bbox[3], l.bbox[3]) * 0.6
            if abs(cy - rcy) <= tol {
                last.append(l)
                rows[rows.count - 1] = last
                continue
            }
        }
        rows.append([l])
    }
    return rows.flatMap { $0.sorted { $0.bbox[0] < $1.bbox[0] } }
}

func legacy(_ path: String, langs: [String]) -> Out {
    let t0 = Date()
    guard let img = loadCGImage(path) else {
        return Out(path: path, text: "", lines: [], markdown: "", ms: 0, error: "cannot load image")
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = correct
    req.automaticallyDetectsLanguage = true
    if !langs.isEmpty { req.recognitionLanguages = langs }
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do {
        try handler.perform([req])
    } catch {
        return Out(path: path, text: "", lines: [], markdown: "", ms: 0, error: "\(error)")
    }
    var lines: [Line] = []
    for obs in req.results ?? [] {
        guard let cand = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox  // normalized, origin bottom-left
        lines.append(Line(text: cand.string, conf: Double(cand.confidence),
                          bbox: [Double(b.origin.x), Double(1 - b.origin.y - b.size.height),
                                 Double(b.size.width), Double(b.size.height)]))
    }
    lines = sortReadingOrder(lines)
    let text = lines.map { $0.text }.joined(separator: "\n")
    let ms = Int(Date().timeIntervalSince(t0) * 1000)
    return Out(path: path, text: text, lines: lines, markdown: text, ms: ms, error: nil)
}

@available(macOS 26.0, *)
func document(_ path: String, langs: [String]) async -> Out {
    let t0 = Date()
    let url = URL(fileURLWithPath: path)
    var req = RecognizeDocumentsRequest()
    if !langs.isEmpty {
        req.textRecognitionOptions.recognitionLanguages = langs.map { Locale.Language(identifier: $0) }
    } else {
        req.textRecognitionOptions.automaticallyDetectLanguage = true
    }
    req.textRecognitionOptions.useLanguageCorrection = correct
    do {
        let observations = try await req.perform(on: url)
        var lines: [Line] = []
        var blocks: [(Double, Double, [String])] = []   // (top y, x, markdown lines)
        var transcripts: [String] = []
        func topLeft(_ r: NormalizedRect) -> (Double, Double) {
            return (Double(1 - r.origin.y - r.height), Double(r.origin.x))
        }
        for obs in observations {
            let doc = obs.document
            transcripts.append(doc.text.transcript)
            for ln in doc.text.lines {
                let r = ln.boundingRegion.boundingBox
                let (y, x) = topLeft(r)
                lines.append(Line(text: ln.transcript, conf: 1.0,
                                  bbox: [x, y, Double(r.width), Double(r.height)]))
            }
            for p in doc.paragraphs {
                let (y, x) = topLeft(p.boundingRegion.boundingBox)
                blocks.append((y, x, [p.transcript]))
            }
            for l in doc.lists {
                let (y, x) = topLeft(l.boundingRegion.boundingBox)
                blocks.append((y, x, l.items.map { "- " + $0.content.text.transcript }))
            }
            for t in doc.tables {
                let (y, x) = topLeft(t.boundingRegion.boundingBox)
                var md: [String] = []
                var first = true
                for row in t.rows {
                    let cells = row.map { cell -> String in
                        cell.content.text.transcript.replacingOccurrences(of: "|", with: "\\|")
                            .replacingOccurrences(of: "\n", with: " ")
                    }
                    md.append("| " + cells.joined(separator: " | ") + " |")
                    if first {
                        md.append("|" + Array(repeating: "---|", count: max(1, cells.count)).joined())
                        first = false
                    }
                }
                blocks.append((y, x, md))
            }
            for b in doc.barcodes {
                let (y, x) = topLeft(b.boundingRegion.boundingBox)
                blocks.append((y, x, ["[barcode: \(b.payloadString ?? "")]"]))
            }
        }
        blocks.sort { ($0.0, $0.1) < ($1.0, $1.1) }
        let markdown = blocks.map { $0.2.joined(separator: "\n") }.joined(separator: "\n\n")
        let text = transcripts.joined(separator: "\n")
        let ms = Int(Date().timeIntervalSince(t0) * 1000)
        return Out(path: path, text: text, lines: lines, markdown: markdown, ms: ms, error: nil)
    } catch {
        return Out(path: path, text: "", lines: [], markdown: "", ms: 0, error: "\(error)")
    }
}

// ── main ──────────────────────────────────────────────────────────────────────

var mode = "legacy"
var langs: [String] = []
var info = false
var correct = true
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--mode": mode = args.isEmpty ? mode : args.removeFirst()
    case "--langs": langs = args.isEmpty ? [] : args.removeFirst().split(separator: ",").map(String.init)
    case "--info": info = true
    case "--nocorrect": correct = false
    default: break
    }
}

if info {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    let supported = (try? req.supportedRecognitionLanguages()) ?? []
    var docSupported = false
    if #available(macOS 26.0, *) { docSupported = true }
    let o: [String: Any] = ["legacy_languages": supported, "document_mode": docSupported,
                            "os": ProcessInfo.processInfo.operatingSystemVersionString]
    if let d = try? JSONSerialization.data(withJSONObject: o), let s = String(data: d, encoding: .utf8) { print(s) }
    exit(0)
}

let semaphore = DispatchSemaphore(value: 0)
Task {
    while let line = readLine(strippingNewline: true) {
        let path = line.trimmingCharacters(in: .whitespaces)
        if path.isEmpty { continue }
        if mode == "document" {
            if #available(macOS 26.0, *) {
                jsonLine(await document(path, langs: langs))
            } else {
                jsonLine(Out(path: path, text: "", lines: [], markdown: "", ms: 0,
                             error: "document mode needs macOS 26+"))
            }
        } else {
            jsonLine(legacy(path, langs: langs))
        }
    }
    semaphore.signal()
}
semaphore.wait()
