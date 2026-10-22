// Optional local extraction for PDF text layers and scanned pages.
// Invoked only by an explicitly configured macOS host. No network call or
// source bytes are retained by this helper; Python supplies a private temp PDF.
import AppKit
import Foundation
import PDFKit
import Vision

func fail(_ code: String) -> Never {
    fputs(code + "\n", stderr)
    exit(2)
}

// Arguments: the PDF's path and the page bound (1-200). Pages past the bound are not
// read; the PDF's page count is reported so Python records them as skipped.
guard CommandLine.arguments.count == 3, let bound = Int(CommandLine.arguments[2]),
      bound >= 1 && bound <= 200 else { fail("pdf_path_and_page_bound_required") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
// Locked: it needs a user password. An owner password alone (restricting editing or
// printing) leaves the document unlocked and readable.
guard let document = PDFDocument(url: url), !document.isLocked else {
    fail("pdf_unavailable_or_encrypted")
}
guard document.pageCount > 0 else { fail("pdf_page_count_out_of_bounds") }

var pages = [[String: Any]]()
var totalCharacters = 0
var imagePageCount = 0
for index in 0..<min(document.pageCount, bound) {
    guard let page = document.page(at: index) else { fail("pdf_page_unavailable") }
    var text = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    var method = "pdfkit-text"
    if text.isEmpty {
        imagePageCount += 1
        guard imagePageCount <= 40 else { fail("ocr_page_limit_exceeded") }
        let bounds = page.bounds(for: .mediaBox)
        let longest = max(bounds.width, bounds.height)
        guard longest.isFinite && longest > 0 && longest <= 20000 else {
            fail("pdf_page_dimensions_invalid")
        }
        let factor = min(2400.0 / longest, 3.0)
        let image = page.thumbnail(of: NSSize(width: bounds.width * factor,
                                             height: bounds.height * factor),
                                   for: .mediaBox)
        var rect = CGRect(origin: .zero, size: image.size)
        guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil,
                                         hints: nil) else {
            fail("pdf_page_render_failed")
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        do {
            try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
        } catch {
            fail("vision_ocr_failed")
        }
        text = (request.results ?? []).compactMap {
            $0.topCandidates(1).first?.string
        }.joined(separator: "\n")
        method = "vision-ocr"
    }
    totalCharacters += text.count + 1
    guard totalCharacters <= 100000 else { fail("pdf_text_limit_exceeded") }
    pages.append(["page": index + 1, "text": text, "method": method])
}
do {
    let data = try JSONSerialization.data(withJSONObject: ["pages": pages,
                                                           "page_count": document.pageCount],
                                          options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    fail("pdf_result_serialization_failed")
}
