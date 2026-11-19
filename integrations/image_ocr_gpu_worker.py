"""The image OCR worker the GPU pass (``integrations.image_ocr_gpu``) runs on a Spark.

Shipped as this one file to the host and run there with its own Python (vLLM, Pillow and
the model's weights in the host's cache beside it); it imports nothing from the
repository and makes no network call (``HF_HUB_OFFLINE=1``, the weights already cached).
Reads requests on stdin and answers on stdout, one at a time, in order:

* request: a frame (4-byte big-endian length, then the body) of UTF-8 JSON
  ``{"n": N}``, then N frames, each one image's raw bytes; a length 0 ends.
* answer: one frame of JSON ``{"n": N, "results": [{"status", "text", "pages",
  "truncated"}, ...]}`` in request order, or ``{"error": "..."}``.

The first answer, before any request, is the hello: the engine, model, revision, prompt
digest, pixel and token bounds and ``worker`` version, which the caller checks against
its own settings (:data:`ENGINE`) before it sends a single image.

Each image is read as the capture's tesseract path prepares it: every frame of a TIFF,
its EXIF orientation applied, transparency flattened (onto black when the page is mostly
transparent with light visible pixels, else white), shrunk to at most ``MAX_PIXELS``.
The model is asked for a verbatim transcription; its answer ``NO_TEXT`` (or nothing) is
``no_text``. A page whose answer hit ``MAX_NEW_TOKENS`` is marked ``truncated`` (a
repetition loop or an over-long page): the caller keeps the earlier reading of such an
image. An image Pillow cannot open, or over ``MAX_SOURCE_PIXELS``, is ``unreadable``.
Nothing here logs a text or an image.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
from typing import Any, BinaryIO, Callable, Sequence

WORKER_VERSION = 2   # 2: loops trimmed only on a page cut at the token bound
# The engine the pass expects; a host that answers another hello is refused.
ENGINE = {"engine": "vllm", "model": "Qwen/Qwen3-VL-2B-Instruct",
          "revision": "89644892e4d85e24eaac8bacfd4f463576704203"}
PROMPT = ("Transcribe every piece of text visible in this image exactly as written, "
          "line by line, in natural reading order, including small print, codes, numbers "
          "and dates. Output only the transcribed text. If the image contains no text, "
          "output exactly: NO_TEXT")
NO_TEXT = "NO_TEXT"
MAX_PIXELS = 1280 * 1280   # dev split: 0.944 at 1280², 0.939 at 1600² (and 35% less prefill)
MAX_NEW_TOKENS = 1024
# A repetition loop (a model re-reading one row forever) is the slowest answer there is.
REPETITION_PENALTY = 1.1
MAX_SOURCE_PIXELS = 200_000_000
MAX_FRAMES = 60
MAX_REQUEST_BYTES = 256 * 1024 * 1024
MAX_IMAGES = 256


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


def hello_of(device: str) -> dict[str, Any]:
    return {**ENGINE, "prompt_sha256": prompt_sha256(), "max_pixels": MAX_PIXELS,
            "max_new_tokens": MAX_NEW_TOKENS, "repetition_penalty": REPETITION_PENALTY,
            "worker": WORKER_VERSION, "device": device}


def read_frame(stream: BinaryIO) -> bytes | None:
    """One length-prefixed frame; None at a clean end (length 0 or end of stream)."""
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack(">I", head)
    if size == 0:
        return None
    if size > MAX_REQUEST_BYTES:
        raise ValueError(f"frame_too_large:{size}")
    body = stream.read(size)
    if len(body) != size:
        raise EOFError("frame_truncated")
    return body


def write_frame(stream: BinaryIO, body: bytes) -> None:
    stream.write(struct.pack(">I", len(body)))
    stream.write(body)


def pages_of(content: bytes) -> list[Any] | None:
    """The image's pages as upright RGB Pillow images of at most MAX_PIXELS, or None
    when Pillow cannot read it (or it is over MAX_SOURCE_PIXELS / MAX_FRAMES)."""
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
    try:
        image = Image.open(io.BytesIO(content))
        frames = int(getattr(image, "n_frames", 1) or 1)
        if frames > MAX_FRAMES:
            return None
        out = []
        for index in range(frames):
            image.seek(index)
            page = ImageOps.exif_transpose(image.copy())
            out.append(_shrunk(_flattened(page)))
        return out
    except Exception:  # noqa: BLE001 - an unreadable image is a status, not a failure
        return None


def _flattened(page: Any) -> Any:
    from PIL import Image
    if page.mode not in ("RGBA", "LA", "PA") and not (page.mode == "P"
                                                     and "transparency" in page.info):
        return page.convert("RGB")
    rgba = page.convert("RGBA")
    alpha = rgba.getchannel("A")
    hist = alpha.histogram()
    transparent = sum(hist[:128]) / max(1, rgba.width * rgba.height)
    grey = rgba.convert("L")
    visible = [g for g, a in zip(grey.getdata(), alpha.getdata()) if a >= 128] \
        if transparent > 0.5 else []
    light = bool(visible) and sum(visible) / len(visible) > 160
    ground = (0, 0, 0, 255) if transparent > 0.5 and light else (255, 255, 255, 255)
    base = Image.new("RGBA", rgba.size, ground)
    base.alpha_composite(rgba)
    return base.convert("RGB")


def _shrunk(page: Any) -> Any:
    width, height = page.size
    if width * height <= MAX_PIXELS:
        return page
    scale = (MAX_PIXELS / (width * height)) ** 0.5
    return page.resize((max(32, int(width * scale)), max(32, int(height * scale))))


# whole words only: "100000" or "ABABABABAB" is a value, never a loop
_RUN = re.compile(r"(?<!\S)(\S(?:.{0,60}?\S)?)(?:\s+\1){4,}(?!\S)", re.S)
MAX_LINE_REPEATS = 3


def trim_loops(text: str) -> str:
    """A model's repetition loop cut back: a phrase repeated five or more times in a row
    is kept once, and a line already seen ``MAX_LINE_REPEATS`` times is dropped. Only for
    a page whose reading hit the token bound (``results_of``): a loop never ends on its
    own, while a page the model finished keeps every row, so a packing list's identical
    rows all stay and can be counted."""
    seen: dict[str, int] = {}
    kept = []
    for line in _RUN.sub(r"\1", text).split("\n"):
        key = line.strip()
        seen[key] = seen.get(key, 0) + 1
        if key and seen[key] > MAX_LINE_REPEATS:
            continue
        kept.append(line)
    return "\n".join(kept)


def results_of(images: Sequence[bytes], generate: Callable[[list[Any]], list[tuple[str, bool]]]
               ) -> list[dict[str, Any]]:
    """Each image's result: its pages read in one ``generate`` call over all pages of all
    images (``generate``: pages -> [(text, hit_token_bound)] in order), pages joined as
    tesseract joins a multi-page image (page text, form feed; the whole stripped)."""
    prepared = [pages_of(content) for content in images]
    flat = [page for pages in prepared if pages for page in pages]
    answers = iter(generate(flat) if flat else [])
    results = []
    for pages in prepared:
        if not pages:
            results.append({"status": "unreadable", "text": None, "pages": 0,
                            "truncated": False})
            continue
        texts, truncated = [], False
        for _ in pages:
            text, cut = next(answers)
            text = (text or "").strip()
            if cut:   # only a reading that ran into the bound can be a loop
                text = trim_loops(text).strip()
            texts.append("" if text == NO_TEXT else text)
            truncated = truncated or cut
        joined = "".join(t + "\f" for t in texts).strip()
        results.append({"status": "text" if joined else "no_text",
                        "text": joined or None, "pages": len(pages),
                        "truncated": truncated})
    return results


def serve(stdin: BinaryIO, stdout: BinaryIO,
          generate: Callable[[list[Any]], list[tuple[str, bool]]],
          hello: dict[str, Any]) -> int:
    """Answer requests until the caller ends."""
    write_frame(stdout, json.dumps(hello).encode("utf-8"))
    stdout.flush()
    served = 0
    while True:
        frame = read_frame(stdin)
        if frame is None:
            return served
        try:
            n = json.loads(frame.decode("utf-8"))["n"]
            if not isinstance(n, int) or not 0 <= n <= MAX_IMAGES:
                raise ValueError("bad_count")
            images = []
            for _ in range(n):
                body = read_frame(stdin)
                if body is None:
                    raise EOFError("images_missing")
                images.append(body)
            answer = {"n": n, "results": results_of(images, generate)}
        except (EOFError, ValueError) as error:
            # the stream is out of step: answer once and end (the caller drops the host)
            write_frame(stdout, json.dumps({"error": f"{type(error).__name__}: {error}"[:300]})
                        .encode("utf-8"))
            stdout.flush()
            return served
        except Exception as error:  # noqa: BLE001 - reported to the caller, who fails the batch
            answer = {"error": f"{type(error).__name__}: {error}"[:300]}
        write_frame(stdout, json.dumps(answer).encode("utf-8"))
        stdout.flush()
        served += 1


def vllm_generator(gpu_memory: float, max_seqs: int
                   ) -> tuple[Callable[[list[Any]], list[tuple[str, bool]]], str]:
    os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from vllm import LLM, SamplingParams
    llm = LLM(model=ENGINE["model"], revision=ENGINE["revision"], dtype="bfloat16",
              max_model_len=8192, gpu_memory_utilization=gpu_memory,
              limit_mm_per_prompt={"image": 1}, mm_processor_kwargs={"max_pixels": MAX_PIXELS},
              max_num_seqs=max_seqs, seed=0)
    params = SamplingParams(temperature=0, max_tokens=MAX_NEW_TOKENS, repetition_penalty=REPETITION_PENALTY)

    def generate(pages: list[Any]) -> list[tuple[str, bool]]:
        chats = [[{"role": "user", "content": [{"type": "image_pil", "image_pil": page},
                                               {"type": "text", "text": PROMPT}]}]
                 for page in pages]
        outs = llm.chat(chats, params, use_tqdm=False)
        return [(o.outputs[0].text, o.outputs[0].finish_reason == "length") for o in outs]

    return generate, "cuda"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="image_ocr_gpu_worker.py")
    p.add_argument("--gpu-memory", type=float, default=0.25,
                   help="share of the GPU's memory this process may take (a shared host)")
    p.add_argument("--max-seqs", type=int, default=32)
    args = p.parse_args(argv)
    # The protocol owns the real stdout; everything else written to fd 1 (vLLM's and
    # native libraries' progress lines) goes to stderr, before any of them loads.
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    generate, device = vllm_generator(args.gpu_memory, args.max_seqs)
    return 0 if serve(sys.stdin.buffer, out, generate, hello_of(device)) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
