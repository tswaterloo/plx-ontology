"""Code proven to derive and read what an earlier commit's code did keeps that commit's
digests (the pattern of generation 8's tables, 2026-10, which went with DI-02 phase 3b).

Three digests key what the served store keeps beyond its rows and step code:

* the **derivation code** (``ontology.project.derivation_code``): the kept document fields
  (``document_fields.KeptFields``, the delta's ``BoundedFields``), the delta layers'
  ``derivation`` stamp and its checkpoint key, and part of ``document_code``;
* the **readings code** (``integrations.captured_text.readings_code``): the kept readings of
  captured files (``KeptReadings``: the API's, the delta's, the primer's, page counts) and
  part of ``document_code``;
* ``document_code`` (``ontology.project``: ``document_groups`` keys), which digests the two
  above with six whole files.

A change that derives and reads the same records still moves them, so every kept field and
reading would be read again. Each table below names, file by file, how this tree differs
from BASE_COMMIT's, with why it derives and reads the same records. Only those differences
are undone before digesting (each must be exactly as listed, or the digest is the plain
one); a file differing in any other way is digested as it is, so any other change moves
the digest exactly as it would without this table. ``ontology/tests/test_code_equivalence.py``
recomputes BASE_COMMIT's side from its own tree.

This module's own bytes are not part of what it pins: like ``project._NOT_DERIVATION``'s
entries for it, what it changes is the value it returns.

Stdlib only.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# Train 3's PIN (2026-11-15): the served code the tables below reach back to.
BASE_COMMIT = "c387ba7453"

_SELF = "ontology/code_equivalence.py"

# blob-zstd: the bytes form of a captured file may be held
# as ``<sha256>.zst``, one zstd frame of the same bytes (``integrations.blob_zstd``). Every
# ``blob_pack`` read of the bytes form (read, open_blob, head, size, exists) falls back to it
# and gives the bytes themselves; ``captured_text`` reads the bytes and tests a held form
# through ``blob_pack`` (read_blob, stored_text, _may_contain, reading_kept, the container
# memo's present test, blob_held) in place of the path, and so does ``document_fields``
# (``_bytes_held``; ``_bytes_kept`` through reading_kept). A test or read differs only for a form held packed or ``.zst`` and not
# as a file: none of the documents' blobs on the served store (counted 2026-11-15), and a ``.zst`` the compress pass writes holds the
# bytes its file held (verified before the file goes). So each file is read into the same
# bytes, the same reading and the same derived records.
_ZSTD: Mapping[str, tuple[str | None, str]] = {
    "integrations/blob_pack.py": (
        "5a7625b2bbb1c4222c779559402eec047686df80ea481496a4e8e07665d64e47",
        "1119f92bad61b3613c8595ae878054e9dc1b2e10ed25897404e21102e45ffa10"),
    "integrations/blob_zstd.py": (
        None,
        "f55f8f77d650a91571e97ec3b20a2f30df0e1debfae259561bd60321d35f2c13"),
    "integrations/captured_text.py": (
        "438700feeb874b844c36b31333e03b625f90d951652234330a518d4feb358ae3",
        "9f2d87f1aa11639528a3a562e3c684b7c5606d7605eecdf382c49702ac7c3f16"),
    "ontology/document_fields.py": (
        "3b9d6bca6910f9e729d24dec3d75db04e5f7b19a71fd8e07fbd5961c560d002d",
        "42a978e26ccf8db64257228ce7ff3d6bface52696782ea48e0930e2d9f61a426"),
}

# The store clock as a property (328f87f04b):
# ``CaptureStore.clock`` returns the store's own clock for another connection to the same
# file; nothing reads it while deriving, and no other line of capture_store moved.
_CLOCK: Mapping[str, tuple[str | None, str]] = {
    "integrations/capture_store.py": (
        "467694d274ff2907f5d96c175599dc5304dd322d1bcf240caf26992d9f67a964",
        "0fb04b98894ddc8573345b9233f311172a9a17d60b655719f65a59d912da85b3"),
}

# The image OCR reading: ``document_understanding.local_image_text``
# prepares an image before tesseract reads it (EXIF orientation, transparency flattened,
# size, an orientation retry; every page of a TIFF). Only the captures run it:
# ``captured_text.capture_text`` with ``image_ocr`` (capture_mail, capture_sharepoint,
# attach_fetch) and the case intake's ``use_case_parts.local_image_ocr``. Nothing that
# derives or reads a kept record does: an image's kept reading is read from its pinned
# OCR text (``captured_text.ocr_digest`` is in its reading key), a container member is
# read by ``read_text`` without image OCR, and an image without pinned text is read
# without OCR, as before. A newly captured image gets the new reading as its pinned
# text: data the delta reads as any changed text, not a re-keying. No other line of
# document_understanding moved, and no function the derivation, reading, member,
# projection, delta or mention code can reach runs the image OCR (call graph:
# ontology/tests/test_code_equivalence.py ImageOcrPinTest).
_IMAGE_OCR: Mapping[str, tuple[str | None, str]] = {
    "integrations/document_understanding.py": (
        "29818b68078b2724ae8753ca76651cf10f005ce72352fd5a5abe9a8944bb53a8",
        "fa8c5c80af674319c8e0e12c9417664627e00fc01684e455af1835deb9367e6c"),
}

# kind -> groups of {file: (sha256 at BASE_COMMIT or None: not there, sha256 now)}, each
# group undone only while every file it lists is exactly its listed version.
# ``derivation``: ``project.derivation_files``
# paths; ``readings_code``: ``captured_text.READING_MODULES`` module names (of which only
# captured_text moved).
CHANGES: Mapping[str, tuple[Mapping[str, tuple[str | None, str]], ...]] = {
    "derivation": (_ZSTD, _CLOCK, _IMAGE_OCR),
    "readings_code": ({"integrations.captured_text": _ZSTD["integrations/captured_text.py"]},
                      {"integrations.document_understanding":
                       _IMAGE_OCR["integrations/document_understanding.py"]}),
}


def earlier_files(kind: str, files: Iterable[tuple[str, str | None]]
                  ) -> list[tuple[str, str | None]]:
    """``files`` (name, sha256 or None: missing) as BASE_COMMIT's tree had them, as far
    as ``CHANGES[kind]`` proves: each group whose files are all exactly their listed
    versions now is put back to its BASE_COMMIT versions (a file BASE_COMMIT had not
    dropped); every other file as given, in the given order."""
    files = [(name, sha) for name, sha in files if name != _SELF]
    held = dict(files)
    undo: dict[str, str | None] = {}
    for group in CHANGES.get(kind) or ():
        if all(held.get(name, "") == now for name, (_, now) in group.items()):
            undo.update({name: then for name, (then, _) in group.items()})
    return [(name, undo[name] if name in undo else sha) for name, sha in files
            if name not in undo or undo[name] is not None]


def equivalent_derivation_code(files: Iterable[tuple[str, str]]) -> str | None:
    """The derivation code of ``files`` (``project.derivation_files``) with the proven
    changes undone (:func:`earlier_files`, with this Python and libraries); None (the plain
    digest) when they do not apply."""
    files = list(files)
    earlier = earlier_files("derivation", files)
    if earlier == [(name, sha) for name, sha in files if name != _SELF]:
        return None
    from ontology.project import environment_digest
    return environment_digest(earlier)


def earlier_value(kind: str, value: str) -> str:
    """``value`` itself: no memo code is kept by value (``mention_memo`` and
    ``text_index_memo`` digest no file the tables change; ``document_code`` keeps
    BASE_COMMIT's through the derivation and readings codes it digests)."""
    return value


__all__ = ["BASE_COMMIT", "CHANGES", "earlier_files", "earlier_value",
           "equivalent_derivation_code"]
