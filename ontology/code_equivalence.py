"""Code proven equivalent to an earlier commit's, so a generation that commit built keeps
its reuse (the reviewed pattern of ``build_keys.ROWS_CODE_DIGESTS``).

Two digests decide what a build can take from an earlier generation beyond its rows code:

* a source's **reader digest** (``build_keys.reader_digest``): its relations' fingerprints,
  held watermarks and shard prints are taken only from a build whose reader code is this
  one's;
* the **derivation code** (``ontology.project.derivation_code``): part of every derived
  relation's fingerprint (``documentFields``, ``containerMembers``), of a deriving source's
  watermark and the key of the kept document fields (``document_fields.KeptFields``).

A change that reads and derives the same records still moves them, so the next build
would read every mail and SharePoint record and parse every document again. Each table
below names the exact differences from one earlier commit, definition by definition
(readers) or file by file (derivation), each with why it reads and derives the same
records. Anything else that differs (another definition, another file, another version
of a listed one, a module added to the derivation code) is no equivalence: the digests
are the plain ones, as before. ``ontology/tests/test_code_equivalence.py`` recomputes
both sides from that commit's tree.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

# The commit whose generation (gen 8 of the host) the tables below reach back to.
BASE_COMMIT = "39cf8a190c"

# ------------------------------------------------------------------ reader code

# (module, definition) -> (sha256 of its syntax tree text at BASE_COMMIT, or None: not
# reached there; sha256 now, or None: not reached now). ``build_keys.code_parts`` texts.
READER_CHANGES: Mapping[tuple[str, str], tuple[str | None, str | None]] = {
    # Running out of memory inside a container read is re-raised as MemoryError instead
    # of being recorded as an unreadable container (86130c2d5b, ccc6ff57b8); every other
    # outcome of these readers is as before.
    ("operating.use_case_parts", "_RESOURCE_WORDS"): (
        None, "120433a23acce95dd95544344b22ca1d7d7e2d95e713c2e4d93b1578c25b09a5"),
    ("operating.use_case_parts", "_resource"): (
        None, "387cbaa95052f0a9d21e2aa86eef9b2924f2140782cc5aa092083bd1be56b76e"),
    ("operating.use_case_parts", "_carried"): (
        "b14253e1460d9344cd8c13d91b84dd647b2b43f9b132ae5baa81b1ad0a55eec7",
        "2a02a41028592f3cc8fd367aadd8f7a44d8a2b90083fd2f5bca5e55474dd9ecb"),
    ("operating.use_case_parts", "_is_package"): (
        "55d52d44a1de34c8515866ee291c96c44cb95d4a8b1749c3d4fe658d018fe84d",
        "1ce3a8567cb89d56d2978670c0b8c70eaae09c850f4ad8cc50f88b1d4f58a615"),
    ("operating.use_case_parts", "_mail"): (
        "0ceda6c8d878ab59b40a24a3e138f64e8d36ea98553ca809a2574655d233567a",
        "6468d13ae8c010bda942b48d55006d904c9ec98d53bc87986052e6831bf4afe4"),
    ("operating.use_case_parts", "_member"): (
        "d383939494b5a440ff84156206b5b3ff66f1df55440c6fc97cfdf9984191ddee",
        "f87907f273eb6afd6aaf3f73a2455c04f2c63d8d7b17be4691b98d0812432e80"),
    ("operating.use_case_parts", "_mime_node"): (
        "77f0c04b1f3b6e6af406ca579fb5495da43de57ec395fe6187c3c97acf829bd7",
        "784285609b01c8bffcf5e2cff01f3f748a3dbf157ec6d3da579145d73bdcf210"),
    ("operating.use_case_parts", "_parser_failure"): (
        "50da8c602775ff62eee3e890b45b981a3e7aa148f7f8dd55620c74443e4cd1a7",
        "96d0bb5e8e104d2444b86cf8137f0c1876d0552fe33fe964a9b002a51c45a623"),
    ("operating.use_case_parts", "_subject_name"): (
        "c3fd6806d688034dbed0efb0c860038ccf31ec1a46f03ffd8a08fa1ba3c85f17",
        "87bb3bd04908f3357dac632a920845ad9421546148eacf8417714ecc8571627f"),
    ("operating.use_case_parts", "_zip"): (
        "c45cec3531288ae74821d859cc0adce37d990bf0f0b5ec0c1afdc333d7041a5d",
        "587d753455518abfd3190368770b29af5eb8bdf25951cf3367d4f613b8aee4ad"),
    # The mail attachments read by mailbox ranges on threads of their own, under the
    # snapshot's file marks: the same rows in the same order, else one
    # read as before (ontology/tests/test_build_prints.py SplitMailReadTests); and its
    # freshness description asks for a truncated attachment listing only once every folder
    # is complete, stopping at the first (232ffd993a): the same freshness, no row read.
    ("ontology.project", "MailCaptures"): (
        "8a8512cebea3f48b03cc546271ac5bc89ea16509cf4db0393cad63644039b1c9",
        "1273574965b8d5fafe301a5682c12df7794d5907695173e03209cef3f2cf9c00"),
    # Mail images worth OCR-reading (1aeab3f6af, the mail-image pass): new definitions of
    # integrations.captured_text the readers reach with the module but never call;
    # capture_text, the text every reader takes, is unchanged (STEP_FILE_CHANGES).
    ("integrations.captured_text", "MAIL_IMAGE_MIN_BYTES"): (
        None, "069517eb20f1a60f173afa11d17f50ba902565b7878b6b33ce0fba67ca486eca"),
    ("integrations.captured_text", "MAIL_IMAGE_MIN_HEIGHT"): (
        None, "065b3debc2c8358f31f93c99fae680c1f2d6716b416225e4995a39b072aa653f"),
    ("integrations.captured_text", "MAIL_IMAGE_MIN_PIXELS"): (
        None, "94e0856499c8d30c530a1daaddc8568dd28443e308e3ece5242277ba8ff4707a"),
    ("integrations.captured_text", "MAIL_IMAGE_MIN_WIDTH"): (
        None, "77491c6a321ddfcfd65423a21f429ee1435d54e97098b8e06c7fe7a5f407f080"),
    ("integrations.captured_text", "image_worth_reading"): (
        None, "49d74fe8d0edaefd66ce44a2b2b65a150f6d5119bcd7251dc6609290ace8e375"),
    # The parallel projection's scheduling: the bindings over one derived relation are one
    # task that reads it once, a sharded binding cut into pieces (dd6d87e5a3, ROWS_CODE_DIGESTS); the relation
    # names it groups by (already defined at BASE_COMMIT, newly reached).
    ("ontology.project_parallel", "DERIVED"): (
        None, "826f3090ac4c6742e8692cc310ae68487bfeafc0636a390ec20ea10ce0027061"),
    ("ontology.project_parallel", "pieces"): (
        None, "a31719e8de6f6bc7f25199a8da446469db53962278c53176ccd9d330dbdccbc5"),
    ("ontology.project_parallel", "tasks"): (
        "65dcd74451503e86085714a3487cc1472812b1b8743397b3ad899b516e9b31a1",
        "fbafc8d7268bc804839824ae937edb856324238681e37968d0edcc6d63f83b31"),
    ("ontology.schema", "DOCUMENT_FIELDS_RELATION"): (
        None, "30137535e71d8be5728473db7a51eef8bc4b777bdcfe931c5774d672db621fdb"),
    # The kept readings a worker is given: container member readings join them, served
    # by their own store's view (dc966fe2b1, 499508cf46: a kept reading is the reading).
    ("ontology.project_parallel", "_KEPT"): (
        "c3ff99757be7e7f1d36ebc77011aedb54d11281b0006305f80968014bb8a7c20",
        "3730b37a92498c3a2571e148d15a10cf4d50a939389cbecbcf6a423cb3f8b7ad"),
    ("ontology.project_parallel", "_kept_views"): (
        "6578da922409f9f5fcb3e232757ae085a4b2393a82a66e7bd9ceb911d4a28932",
        "ccbbef74660c1be66f8c17d02d4de33d8907935f17cf0b5b1e53e2c2f7ef0a71"),
    # The meetings capture (integrations.capture_meetings): its GET templates and id types
    # join the closed grammar and its document kinds the mention documents; no mail or
    # SharePoint read builds, matches or yields anything else.
    ("integrations.live_source_grammar", "TEMPLATES"): (
        "2ffe6b781e45edc501595d9e2b7be9888d3a46be7fc5ec02a94022a4938c8930",
        "d7a7ade08ec77f470dca7fa2be22b30fe04897927ccefcc67b7b00b522bc65a7"),
    ("integrations.live_source_grammar", "TYPES"): (
        "59c809c40e8fc1627f06b786796268a8563d344c69ff85131f6a9a302b5d7439",
        "44e82ec9704db963966f2e65af50d4f394e12eca9a1d4e0582a7fa64ac7aded1"),
    ("ontology.mentions", "DOCUMENT_KINDS"): (
        "bfdbd2ac8d056231f5d7d38d2937dcff8d965791ffc47e050cf0877866718171",
        "d5654fffe9c1b439fa7cba402907d127d0998c674bd1d93fea8cb8a5620f8a3f"),
    # The Portal relations whose ETL copies a binding collapses gain
    # public.app_sales_order_line (finding 6 of the 2026-11-06 data audit): a Portal
    # binding's records, never what a mail or SharePoint reader reads or fingerprints.
    ("ontology.project", "ETL_REINSERTED"): (
        "fe224d4907c33f214e42a12e00db9b0399fb07fb190aeac3fb3b011dcdb41605",
        "a40d1e8d80af8b7cb4648210c376daafe3341871c105b369af61b9a70a024ef7"),
    # The document group key's code keeps the earlier commit's value where it is proven
    # equivalent (VALUE_EQUIVALENTS); otherwise the same digest as before.
    ("ontology.project", "document_code"): (
        "7a88280e104b0e79cb293601b528965b4684e13905281b738b0913875e11e00e",
        "8d1e0367e93939fed226b50a74eaf958118653ee41ce15c618ce2504059137e1"),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reader_differences(there: Mapping[tuple[str, str], str] | None,
                       here: Mapping[tuple[str, str], str] | None
                       ) -> dict[tuple[str, str], tuple[str | None, str | None]] | None:
    """(module, definition) -> (sha there, sha here) of every definition two reader codes
    (``build_keys.reader_parts``) differ in; None when either cannot be read."""
    if there is None or here is None:
        return None
    return {key: (_sha(there[key]) if key in there else None,
                  _sha(here[key]) if key in here else None)
            for key in sorted(set(there) | set(here)) if there.get(key) != here.get(key)}


def reader_equivalent(source: Any, tree: Path) -> bool:
    """Whether ``source``'s reader code in ``tree`` (BASE_COMMIT's, extracted) differs
    from this one's only by ``READER_CHANGES``, each exactly as listed."""
    from ontology import build_keys

    diff = reader_differences(build_keys.reader_parts(source, tree),
                              build_keys.reader_parts(source))
    return diff is not None and all(READER_CHANGES.get(key) == pair
                                    for key, pair in diff.items())


# ------------------------------------------------------------------ derivation code

# The files BASE_COMMIT's derivation code covered, with their sha256, in its order
# (that commit's ``derivation_code``: every module its roots import and every schema
# file); its value is recomputed from them with this Python and these libraries.
BASE_FILES = Path(__file__).with_name(f"derivation_code_{BASE_COMMIT}.json")

# path -> (sha256 at BASE_COMMIT or None: not there, sha256 now): every file of this
# derivation code (``project.derivation_files``) whose bytes differ from BASE_COMMIT's.
# Every other file it covers must be byte for byte BASE_COMMIT's. BASE_COMMIT covered
# more (every schema file, every module any import reached): build-speed5 narrowed it to
# the modules a derived record is computed by (``project._NOT_DERIVATION``).
DERIVATION_CHANGES: Mapping[str, tuple[str | None, str]] = {
    # derivation_code narrowed (build-speed5) and kept to this table; the generation's
    # finish and counts (finish_generation, projected_counts) are not derivation; the
    # projector writing only the chosen objects' rows of an object-local binding and
    # keeping each object's report entries apart (Projector, _Report, object_unit): it projects records, never derives one; the meetings capture as a
    # capture source of its own (CAPTURE_SOURCES, _NOT_DERIVATION: another source's reader);
    # the mail attachments read by mailbox ranges side by side (MailCaptures, ontology/tests/test_build_prints.py)
    # and ETL_REINSERTED naming app_sales_order_line (which Portal records one binding
    # collapses as ETL copies at projection, never how a document record is derived)
    "ontology/project.py": (
        "2409495cb9bd2b9bac667ca4724a471698c748db7b0305de24e9692b8710efaf",
        "85dd65249e37b8dae4999a31cb9310ee3f1f3729d1fdee8296f1f77ec2e46842"),
    # the mention step's failures per document (DocumentFailures), the store's windows in
    # its memo key and a read-ahead load that fails left to the step: the mention step,
    # which derives no documentFields or containerMembers record; the meetings document
    # kinds (DOCUMENT_KINDS: calendar events, Planner tasks, transcripts)
    "ontology/mentions.py": (
        "a5f523b02e9e4636202774376240554310955d99fe35b8fb14c1f64f7495ebd4",
        "57cd64be51766fd4ea27d2fd7e85b6d78ae0c1d27e1d83dabd68ba9e849893fb"),
    # the capture-kind registry names the meetings kinds (ADAPTER_MODULES, _load_adapters):
    # a registry, never a derivation
    "integrations/capture_store.py": (
        "b99f340bdbee65c3b13776178d5d64469945bcf67ac9ebbdbc72302e9e15dc55",
        "14c49ba55543bd70d41aa21cdbe27a60f01883ddca7eea2b26acaa212c333c25"),
    # the meetings mention link rules (MEETINGS_MENTION_LINK_RULES): link types from the new
    # meetings types, no documentFields or containerMembers record derived otherwise
    "ontology/schema.py": (
        "da11353ec3d3795d74387b8b9d13715c6900a8062e2d3265d683ec165b4e3c0e",
        "ccc9bf7bbf5314ee8dc0475d1e4f088e6c461d90d5ecba1868f3e35073c00343"),
    "ontology/document_failures.py": (None,
        "5ff79dde6579d1aec38dd2fa99edbb32236e1b9eefeba53ee8f87f39aa0da651"),
    # scheduling, spills (records read back as read), merge order, kept member views, an
    # incremental build's changed units merged into its seed with their object parts:
    # rows equal a serial build's (ROWS_CODE_DIGESTS); _derive and _parse unchanged
    "ontology/project_parallel.py": (
        "962c7549bb4a924881e196f491e5d970eb0c89e2ea2d0c17559d865a64c303ff",
        "1f5b18bcb4a0cc5cf23bddda3a28d8601af88f81f571ab789763942c4f50ebee"),
    # the builder's checkpoint to resume (d5be43dd12) and its commit's invariant queries
    # run by threads over the committed rows (StoreBuilder): writes, never derives
    "ontology/store.py": (
        "e71e2bac47757c0cdf3472e85c0bb2ec03abfcfba96145b48661cb4beaff63af",
        "b84f54fa7fa34e4c0605824bb71721be5ae4e9b3f48e48f1a518f8d3193f0f20"),
    # out of memory re-raised, never recorded as an unreadable container (READER_CHANGES)
    "operating/use_case_parts.py": (
        "9a232a4d5ba845247b89faa13b978c72965838d43aa0fd44ba272df1d75e495b",
        "b844520bee721656c1db306fdfa4fabdbd92cd625c8fadde42875079d23abf61"),
}


def _base_files() -> list[tuple[str, str]] | None:
    try:
        data = json.loads(BASE_FILES.read_text(encoding="utf-8"))
        files = [(str(path), str(sha)) for path, sha in data["files"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if data.get("commit") != BASE_COMMIT or not files:
        return None
    return files


def derivation_differences(files: Iterable[tuple[str, str]]
                           ) -> dict[str, tuple[str | None, str]] | None:
    """path -> (sha at BASE_COMMIT or None, sha now) of every file of ``files`` (this
    derivation code's, ``project.derivation_files``) whose bytes differ from
    BASE_COMMIT's; None without BASE_COMMIT's list."""
    base = _base_files()
    if base is None:
        return None
    then = dict(base)
    return {path: (then.get(path), sha) for path, sha in files if then.get(path) != sha}


def equivalent_derivation_code(files: Iterable[tuple[str, str]]) -> str | None:
    """BASE_COMMIT's derivation code value (with this Python and libraries) when
    ``files`` differ from its files only by ``DERIVATION_CHANGES``, each exactly as
    listed; else None (the plain digest)."""
    diff = derivation_differences(files)
    base = _base_files()
    if diff is None or base is None or \
            any(DERIVATION_CHANGES.get(path) != pair for path, pair in diff.items()):
        return None
    from ontology.project import environment_digest
    return environment_digest(base)


# ------------------------------------------------------------------ step memo code

# path -> the definitions in which a file the finishing steps' memos rest on differs from
# BASE_COMMIT's; every other file they rest on is byte for byte BASE_COMMIT's
# (``ontology/tests/test_code_equivalence.py``). captured_text.py gains the mail-image
# size test (1aeab3f6af): no step calls it, and capture_text is unchanged.
STEP_FILE_CHANGES: Mapping[str, frozenset[str]] = {
    "integrations/captured_text.py": frozenset({
        "MAIL_IMAGE_MIN_BYTES", "MAIL_IMAGE_MIN_HEIGHT", "MAIL_IMAGE_MIN_PIXELS",
        "MAIL_IMAGE_MIN_WIDTH", "image_worth_reading"}),
}

# kind -> {the value this code gives: BASE_COMMIT's value}: the keys the finishing steps'
# memos (``ontology.mention_memo``) hold their entries under. Each value digests the
# bytes of whole files, so a pair holds only for these exact files (and, for
# ``document_code``, this derivation value and Python/libraries, ``/data/venv`` of the
# host); ``ontology/tests/test_code_equivalence.py`` names the definitions they differ
# in, none of which changes what is kept:
# * ``mention_memo`` (``mentions._memo_code``): the step's per-document failures, its
#   read-ahead load left to the step, ``held_stretches`` (unchanged) now in the key; the
#   meetings document kinds and the meetings types' own mention link types (new source
#   types, none a target: a kept mail or file entry resolves to the same links);
# * ``text_index_memo`` (``text_index._memo_code``): per-document failures, chunks taken
#   from a base generation indexed under the same code; the facts kept are as before;
# * ``document_code`` (``project.document_code``: ``document_groups`` keys): the files
#   above and ``DERIVATION_CHANGES``' ``project.py`` (its object-by-object writes
#   included: they decide rows, never a document group; the meetings capture source and
#   its readers kept out of the derivation code).
VALUE_EQUIVALENTS: Mapping[str, Mapping[str, str]] = {
    "mention_memo": {
        "acd7b63c6429d6a7cbf79d33a3c51095eebb1ed981659f962ed979c8f51c516c":
            "d704849ed7b6a53bfa3ca3f4a03b14c1403a64a34d2e8bca9b5438ac3f287e04"},
    "text_index_memo": {
        "007b84688e82bd1a4d107d25aa6699cc439c0d075e87828e713d1e1faca24f20":
            "da8970329aaf3a1bdfec1504df6b5839cc7a831d2621f9cbaa6250ceb4254d94"},
    "document_code": {
        "ac3518f51d2daf3330d6a1628b232172bd5a9c54712d06ddfe33800ac51e81a0":
            "85c6895875d0ea6260a3f9934c50bb0dd489e491e9e962c79338fa13cc168891"},
}


def earlier_value(kind: str, value: str) -> str:
    """BASE_COMMIT's value of a memo code when ``value`` (this code's) is listed for it
    (``VALUE_EQUIVALENTS``); else ``value``."""
    return VALUE_EQUIVALENTS.get(kind, {}).get(value, value)


__all__ = ["BASE_COMMIT", "STEP_FILE_CHANGES", "VALUE_EQUIVALENTS", "earlier_value", "DERIVATION_CHANGES", "READER_CHANGES", "derivation_differences",
           "equivalent_derivation_code", "reader_differences", "reader_equivalent"]
