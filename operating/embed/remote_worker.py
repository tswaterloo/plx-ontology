"""The embedding worker the remote pass (``operating.embed.remote``) runs on a GPU host.

Shipped as this one file to the host and run there with its own Python (torch and
sentence-transformers installed beside it); it imports nothing from the repository.
Reads requests on stdin and answers on stdout, one at a time, in order:

* request: 4-byte big-endian length, then UTF-8 JSON ``{"texts": [...]}``; length 0 ends.
* answer: 4-byte length, JSON header ``{"n": N, "dim": D}``, then ``N * D`` little-endian
  float32 values (row-major); or a header ``{"error": "..."}`` and nothing after it.

The first answer, before any request, is the hello: ``{"model", "dim", "device",
"precision", "max_seq_length", "max_chars"}``, which the caller checks against its own
settings before it sends a single text.

The vectors are those of ``operating.embed.models``' local provider: the same model and
revision, each text cut to ``LOCAL_MAX_CHARS`` characters (with the e5 ``passage: ``
prefix where that model is used), ``max_seq_length`` 512, normalised by the model and
again in float32 (cosine = dot). Nothing here logs a text.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import Any, BinaryIO, Callable, Sequence

# Kept equal to ``operating.embed.models`` (a test holds them equal).
LOCAL_MODELS = {"BAAI/bge-m3": 1024, "intfloat/e5-large-v2": 1024}
LOCAL_MAX_CHARS = 6000
MAX_SEQ_LENGTH = 512
MAX_REQUEST_BYTES = 256 * 1024 * 1024
# The revision the indexing host's model cache holds (``/data/models``): vectors from another
# revision would differ in kind from the stored ones.
REVISIONS = {"BAAI/bge-m3": "5617a9f61b028005a4858fdac845db406aefb181"}


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


def serve(stdin: BinaryIO, stdout: BinaryIO, encode: Callable[[Sequence[str]], Any],
          hello: dict[str, Any]) -> int:
    """Answer requests until the caller ends; ``encode`` returns an (n, dim) array."""
    import numpy as np
    write_frame(stdout, json.dumps(hello).encode("utf-8"))
    stdout.flush()
    served = 0
    while True:
        frame = read_frame(stdin)
        if frame is None:
            return served
        try:
            texts = json.loads(frame.decode("utf-8"))["texts"]
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                raise ValueError("texts_not_a_list_of_strings")
            out = np.asarray(encode(texts), dtype=np.float32) if texts else \
                np.zeros((0, hello["dim"]), dtype=np.float32)
            norms = np.linalg.norm(out, axis=1, keepdims=True)
            out = (out / np.where(norms == 0, 1, norms)).astype("<f4")
            if out.shape != (len(texts), hello["dim"]) or not np.isfinite(out).all():
                raise ValueError(f"bad_vectors:{out.shape}")
        except Exception as error:  # noqa: BLE001 - reported to the caller, who fails
            write_frame(stdout, json.dumps({"error": f"{type(error).__name__}: {error}"[:500]})
                        .encode("utf-8"))
            stdout.flush()
            continue
        write_frame(stdout, json.dumps({"n": len(texts), "dim": hello["dim"]}).encode("utf-8"))
        stdout.write(out.tobytes())
        stdout.flush()
        served += 1


def model_encoder(model: str, precision: str, batch: int, memory_fraction: float
                  ) -> tuple[Callable[[Sequence[str]], Any], dict[str, Any]]:
    if model not in LOCAL_MODELS:
        raise SystemExit(f"model_unknown:{model}")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")
    import torch
    from sentence_transformers import SentenceTransformer
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and memory_fraction:
        # a host shared with other GPU users: this process never takes more than its share
        torch.cuda.set_per_process_memory_fraction(memory_fraction)
    st = SentenceTransformer(model, device=device, revision=REVISIONS.get(model))
    st.max_seq_length = MAX_SEQ_LENGTH
    dtype = {"fp32": None, "fp16": torch.float16, "bf16": torch.bfloat16}[precision]
    if dtype is not None:
        st = st.to(dtype)
    prefix = "passage: " if model.startswith("intfloat/e5") else ""

    def encode(texts: Sequence[str]) -> Any:
        cut = [prefix + t[:LOCAL_MAX_CHARS] for t in texts]
        with torch.inference_mode():
            out = st.encode(cut, batch_size=batch, normalize_embeddings=True,
                            convert_to_tensor=True, show_progress_bar=False)
            return out.float().cpu().numpy()

    hello = {"model": model, "dim": LOCAL_MODELS[model], "device": device,
             "precision": precision, "max_seq_length": MAX_SEQ_LENGTH,
             "max_chars": LOCAL_MAX_CHARS, "revision": REVISIONS.get(model)}
    return encode, hello


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="remote_worker.py")
    p.add_argument("--model", required=True)
    p.add_argument("--precision", choices=("fp32", "fp16", "bf16"), default="fp32")
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--memory-fraction", type=float, default=0.2,
                   help="the GPU memory share this process may take (0: no cap)")
    args = p.parse_args(argv)
    encode, hello = model_encoder(args.model, args.precision, args.batch, args.memory_fraction)
    serve(sys.stdin.buffer, sys.stdout.buffer, encode, hello)
    return 0


if __name__ == "__main__":
    sys.exit(main())
