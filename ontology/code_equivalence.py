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
        "c81d0316d9dd4754acc476430bb1096d0795f11b923da5ab4b45515e520b2dec"),
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
        "8fb955ecf5b5efe45cb78ca9a035f57c361f0adc5e0dc494703da6d58c2ff1ad"),
    # The Portal relations whose ETL copies a binding collapses gain
    # public.app_sales_order_line (finding 6 of the 2026-11-06 data audit): a Portal
    # binding's records, never what a mail or SharePoint reader reads or fingerprints.
    ("ontology.project", "ETL_REINSERTED"): (
        "fe224d4907c33f214e42a12e00db9b0399fb07fb190aeac3fb3b011dcdb41605",
        "a40d1e8d80af8b7cb4648210c376daafe3341871c105b369af61b9a70a024ef7"),
    # Document fields read a value beside or below its label and the mention links a
    # build step writes are generated by kind: the typed
    # fields of a captured file are keyed by document_code (derivation_code), which these
    # files change, and the mention links by the step migration; what a mail or
    # SharePoint reader reads and fingerprints is as before.
    ("integrations.document_understanding", "MAX_LAYOUT_PAGES"): (
        None, "9312934e7332fe10dcb5222ce1276c4a6f5eb0020ebbeeabd7c37d98aca91ffb"),
    ("integrations.document_understanding", "pdf_layout_pages"): (
        None, "e9cf3291993615fc5a1191fd9cfcff1496e280549f5fec1978e69feb6e30c15a"),
    ("ontology.document_fields", "MAX_BLOCK_LINES"): (
        None, "e170fb0f6f689f8f39af740c9e7dedc55c54d777de87a1440261626d62d665c3"),
    ("ontology.document_fields", "MAX_PLACED_GAP"): (
        None, "77df27cf5735e2afc9ea09282870dc4e354bced106d6f4bcffbe176307326eb2"),
    ("ontology.document_fields", "PLACED_HOW"): (
        None, "a53732cc1b27d9ffd831b886667856704000266bafbf2e4906e027947bcd7ea9"),
    ("ontology.document_fields", "_CONSONANT_RUN"): (
        None, "e682344dd2ba6fe3bac5ee1e04b99048d89a9cce1333ffa8b0baad92f5dd9c73"),
    ("ontology.document_fields", "_DATE_SHAPED"): (
        None, "37b58ee70225fc6fc77a94a3259af12ee730d5d28ded7c32f6a4714909ef7270"),
    ("ontology.document_fields", "_EDGE_MARKS"): (
        None, "c207a5b0f39ada65fd1185f78536262a6fb9596828dae503a060f391d2e3453c"),
    ("ontology.document_fields", "_FIELD_KEYS"): (
        "0149910850209f42525c8361cc2c3f0fedf11182f6c72d9c739623a167a811d2",
        "b49cca18f4525d213989bf3d7e2508916d26c2ffabee5eac74243c09419377d0"),
    ("ontology.document_fields", "_LABELLED_LINE"): (
        None, "0d2ce59abef2d40aa67e7e86df691ca8f462cade07546d0101b983ecfc4b0f2b"),
    ("ontology.document_fields", "_NUMBER_WORDS"): (
        None, "6a1fe2152c5d20c97e15cd01881f32e2b91f50dea118153aea63d700c1078d4e"),
    ("ontology.document_fields", "_PLACED_GAPS"): (
        None, "e981a36a2a60577c5c5f60b8695e204741285ea47da3b60269f6ae401f993bea"),
    ("ontology.document_fields", "_PLACED_IDENTIFIER"): (
        None, "6e9caf184a6fd6c41711761a2ae8c1ecf3b83eff3fdab32c6c4282e6f6ce4907"),
    ("ontology.document_fields", "_PROSE_VALUE"): (
        None, "71879e391c995a94f8f2c25279411179265eb9613311f10f072f489233b2c312"),
    ("ontology.document_fields", "_View"): (
        None, "2cbcdf7385d87f3b4647cf65ac6139fceedd95724feccef8bdf2fbfe42f6e4f8"),
    ("ontology.document_fields", "_WORDS"): (
        None, "2932d5cf277fdbf375db732d6ec3b3cb2d40eb35301ca75fae38fd54b0439052"),
    ("ontology.document_fields", "_bytes_held"): (
        None, "9772363cefaa4f770a671a4914c179c28799cb1d8537cca28bcd2dc4397fbdf6"),
    ("ontology.document_fields", "_cell_label"): (
        None, "0036822810beb4deff8702573f065e3129f389195caa234203b40b5b8bc8e53a"),
    ("ontology.document_fields", "_documents"): (
        "b877dc8d9fded9e4b1e2b688d7da92d46ded1e61bcfdb1703260e8779cd96e04",
        "9b63b721e0aa454735a4004a4df7a725ec5d7554bc9481e1ddb974ba04d0742b"),
    ("ontology.document_fields", "_field_label_words"): (
        None, "c7fe5495480af96821e240915255cef819ceb1551ef6d40c493d5a2ca3792844"),
    ("ontology.document_fields", "_fields"): (
        "e2cd70ebba9f37e7a37280ae2f809fc19800effd5c69618b5aa44341c237c24e",
        "8f4d0559a0c3bb3c4eabf0d6ed81f0cd84cdc9869b791907ef838292e720906d"),
    ("ontology.document_fields", "_fields_of"): (
        "dac2ba3958a852c90cf4cc7ff1dec7cdb75543027b10a1e38dd33130095a8042",
        "1a2bc1bc0ec87f666fb92be85a7a559c9f5a8d4ec7d462f7298497d42b1ff4e5"),
    ("ontology.document_fields", "_fields_read"): (
        "b1ec528fe3a4015ce5ead712054dc7debd9e99a945eadd3f9addbdd1b493c79e",
        "b42c9ac272cc8b9108321c56641fe4bcae6d8ec10f9cf331be6dc68776becedc"),
    ("ontology.document_fields", "_held_layout"): (
        None, "33d3d2fad62b7ffef5b8efa44eb77ef1e9d6275946728e43692810f2ce059509"),
    ("ontology.document_fields", "_label_like"): (
        None, "437426122f42396fd00fc8808961d79be6b90dd806bc8b912a7fbb8f3ef6f8d1"),
    ("ontology.document_fields", "_label_vocabulary"): (
        None, "9e9d88a0fae63f99a5bb8148018ee39588f23e15475cda63e5d9ebc710905942"),
    ("ontology.document_fields", "_label_words"): (
        None, "e287b6c6b9170b3fa4b8f741794e6c2a95cec9087c14bf62f10eee1aa1b794e4"),
    ("ontology.document_fields", "_layout_of"): (
        None, "d7c9b756f031c6acd0c4b9a639c3d1aa9fbeb677bcaddc3ca4405ffb4698ff29"),
    ("ontology.document_fields", "_locate"): (
        None, "9179f0b01ab26dffae4e1bad5f783b6544d864aba076ae311448ca990bf137f0"),
    ("ontology.document_fields", "_names_other_field"): (
        None, "91dc66ca38f8c13dcab6a994f1d8c19bb4d229097fee3db64ca10e36ac781de0"),
    ("ontology.document_fields", "_ocr_shaped"): (
        None, "09b592042c35b2aca55d2e057565276f8cb4324c904e0b59f3cab7da765c2c8c"),
    ("ontology.document_fields", "_placed_reads"): (
        None, "ef3767ab3b83f94f90cec8364175692bd938bb48b83be51c33da5474127d6234"),
    ("ontology.document_fields", "_placed_value"): (
        None, "fdd72f0ed8b7305b59ebdd1380cb55d1bd008d343a212b3f40e1fa902c943055"),
    ("ontology.document_fields", "_placement"): (
        None, "ef38f43fd7c686039b283573ef236a27de6861c4c39eec8f7787f736eda9b518"),
    ("ontology.document_fields", "_views"): (
        None, "e2b3ba0dc1abc3732f02bca512c92be6026c6d14685c267513acfeef5cfbaa07"),
    ("ontology.document_fields", "_written_currency"): (
        None, "75b2b56e82606ea96e4ea9abdb4fd2c22425748a0e9f9dac2c628de2e42abea8"),
    ("ontology.document_fields", "_written_pattern"): (
        None, "6c6392885af0aed035a54a58eca9c1c7b706ed36ea881ae56ce96ab659bd8edc"),
    ("ontology.document_fields", "extract_fields"): (
        "1fa1f1e9de7cac2579d362e728bbe28f45e025895021d7988c9d5d361c3a1d55",
        "57da1aad6320e5b6bcadc7b322e58119cf2cf9471323ce512f230f9aa6755125"),
    # identifier kinds may declare source_prefixes, and a value glued to "_<digit>" is no
    # mention (STEP_FILE_CHANGES): a mail or
    # SharePoint reader's records and fingerprints are as before
    ("ontology.identifiers", "_source_unprefixed"): (
        None, "857971c8c57c95a91558cdb644a7ee9a8d9182e8b382763efb9ba31df73373dd"),
    ("ontology.identifiers", "_GLUED"): (
        None, "a26741853ebc7eefd3ea36fc6344cd49d82abecbdf902dc127c93573921cb3eb"),
    ("ontology.identifiers", "find_identifiers"): (
        "bad29ee980f94f113bfcc830a90ba6f5f2f68e330f368aef59bcb4a6a45dbf3d",
        "44c43232a9550973973c02bb5635b9d52df2553c56aa75782f042e6dc4eeb7d3"),
    ("ontology.identifiers", "parse"): (
        "28a7814b689c44b82b8b9bd5d38b98cf40cc5bdf6c5b8b20cbec24ba834eb179",
        "42098a27946ec29350166c6cbee371c4a94c3cfa44a29f514e33c57adb1881ee"),
    ("ontology.mentions", "mention_link_types"): (
        "24536868da02a81021ecddf3677450730ae1e886eafe609f630f649b53626425",
        "28808431a3df259de4a77f714cbdd8ec92643aff4e030fc9d7ec6ad6993bb7e7"),
    # SharePoint list items are mention documents too (one group per item version, its
    # text field values, ontology/list_documents.py), and a confirming path is no mention
    # pair: the records a mail or SharePoint reader yields and fingerprints are as before.
    ("ontology.project", "GROUP_LOADS"): (
        "11ae9c6b7ed5722e7fdbdc7709934fc6ebd7d3bf2a1ffaccab598aca37ee820e",
        "a3362be93311b36063825d4e894b74f8aaf8db1b6188a11af0a102b60fc97c83"),
    ("ontology.list_documents", "KIND"): (
        None, "e08619209b9cce7eb951f64e29c727f7145f072af18f8ab4a629a56031005b36"),
    ("ontology.list_documents", "code"): (
        None, "d403e185ade74985a1527849d5a685d4aaabca4c9089718fdab01940fd7f285f"),
    ("ontology.list_documents", "document_rows"): (
        None, "cdcf42fba3f9a66d4b20b6638b440b9fb2efaf356f1f0eaec254097f9508329e"),
    ("ontology.list_documents", "list_item_document"): (
        None, "2c1e6c38a3f14069d10bf86a133b159d0f6d464339cb2d15f9b4ab1fe3b69187"),
    ("ontology.project", "SharePointCaptures"): (
        "361fb03752074815c843c446158e7b4419c408fe2d10564645ee5ca4a04da116",
        "f1765f987385dc0e86c9bd4f6b3083504a976261e1b55c5e297fe7353acce527"),
    # The document group key's code keeps the earlier commit's value where it is proven
    # equivalent (VALUE_EQUIVALENTS); otherwise the same digest as before.
    ("ontology.project", "document_code"): (
        "7a88280e104b0e79cb293601b528965b4684e13905281b738b0913875e11e00e",
        "8d1e0367e93939fed226b50a74eaf958118653ee41ce15c618ce2504059137e1"),
    # Relation people (ontology/people_directory.py): staff and outside contacts from the
    # mail capture, decided by the people exclusion first; messages, attachments and the
    # documents a mail reader yields and fingerprints are as before.
    ("integrations.capture_mail", "_AUTOMATED"): (
        None, "0b99979c7023f3be81dc911cc8215c8d2ee1a635c982a609c99a5518e64cf989"),
    ("integrations.capture_mail", "automated_sender"): (
        None, "805a2e0fbd7e5e0c926298a092ae72fe419fc461cfa14dc26e77ae28214649d6"),
    ("integrations.live_source_guard", "NotVisible"): (
        None, "1b90fa5d30f4cfd00ed960e44f6b04ccb101927c598c384b68ecd20f58f5f6fc"),
    ("integrations.people_exclusion", "CODES"): (
        None, "9baf9f42abcad41390702b49a0d4c72aa6d1fba27a1780026ad4f40b491d94f1"),
    ("integrations.people_exclusion", "CONFIG"): (
        None, "f657f385586fa57d998738cf067a0adfa97b124772e6966565c944f9c49fb10f"),
    ("integrations.people_exclusion", "ENV_FILE"): (
        None, "d159cc4bae92f675d716085607ebb8890891c416e20fb494b0c406bdc0a52a82"),
    ("integrations.people_exclusion", "ENV_IDS"): (
        None, "07f89ed201a9be25e2b208fc59bc0361fa3a77e5b160aa74ccd798666db00ea0"),
    ("integrations.people_exclusion", "ExclusionUnavailable"): (
        None, "64240e4d67118ba76ef4cd930e7123b998bcba1b776b094052bccf3bdb16dbcb"),
    ("integrations.people_exclusion", "KINDS"): (
        None, "dc5c6ad8687b79e723c7ecb644f6d73a859a4d4c0c77c0726f516d772296a13c"),
    ("integrations.people_exclusion", "LATER_ASK"): (
        None, "9db222d2a91b78a19a93c0d87d1401919985a1dd80d1d43e82c931c27cf58237"),
    ("integrations.people_exclusion", "PeopleExcluded"): (
        None, "218fc9847330031c5a338e7cd0e0f1fd744d3b2989e6d9b4700824025859f7c5"),
    ("integrations.people_exclusion", "PeopleExclusion"): (
        None, "0862c17f9d270fa1a523becd99cc7ad85cc6e2344510b83f56fbcde26c3837ab"),
    ("integrations.people_exclusion", "SCHEMA"): (
        None, "e38887565bfb85f369e8efc5855d813508d5d5db1430779a6bfc8c8651172773"),
    ("integrations.people_exclusion", "_DIRECTORY_SELECT"): (
        None, "8f2a343a42f7f4a2cc368fa5a6ff5356e0884ff728983a30fc9418e34a444c0e"),
    ("integrations.people_exclusion", "_GUID"): (
        None, "ac368c68923a20a1da23d7b07245726a5e8d026233568e3d120cf63d8b9ea7d0"),
    ("integrations.people_exclusion", "_config_ids"): (
        None, "125d317bb509d6f7301df5b9832e90d2774ef133403b10854920c0e173487201"),
    ("integrations.people_exclusion", "_ids_from"): (
        None, "e85be2ce2b57d847e24ebcc0cd9e1bb3e187afe4265516cd8a314d7b643533e8"),
    ("integrations.people_exclusion", "_inside_git"): (
        None, "1aa42cab1bf5e6c828980aa7bd4c68902aff667c33ab1549d7dad6cfd59cc8ed"),
    ("integrations.people_exclusion", "is_object_id"): (
        None, "430bd37920ba2f3496299e8c47024233d3aa7bfbed0911860eb7024f800efea2"),
    ("integrations.people_exclusion", "onedrive_path"): (
        None, "27255a04f7ef59b87be021aa5ee1c6b4b26e2ad3e47707d05fa05a45c3bc938a"),
    ("ontology.mentions", "SERVICE_MAIL_HOSTS"): (
        None, "e671aa100bd967e6422aa7947a1386639773766f2bb678a90e51ff79eb6d8f86"),
    ("ontology.mentions", "SHARED_MAIL_BRANDS"): (
        None, "5df61e3977251e1f591ea2f0de7813b8cb00bdc58054d33e8737b95a2796e94d"),
    ("ontology.mentions", "SHARED_MAIL_DOMAINS"): (
        None, "dc0e6be73647b2cbf0b7ce600694094b50686d8ff3224677a19864b6026d4cf5"),
    ("ontology.mentions", "_OWN_LABEL_MIN"): (
        None, "3bdef42963a1c078b0d63f3cb73d4f0f8f223bf31b3404808a518ce4c350a4dd"),
    ("ontology.mentions", "_PUBLIC_SECOND_LEVEL"): (
        None, "912a2d2a5ccfc9c07820f4191b08321c101595baec4442d34453d0d626e52950"),
    ("ontology.mentions", "own_mail_domains"): (
        None, "49a92971303b9700bb75734a8d723d03dedc00511f19be1a1aaa068488b4445b"),
    ("ontology.mentions", "registrable_domain"): (
        None, "8c4bf0ee3d2ba5bdf28be7eb178eb1234bda6b532328dbb1a8153bcc639c82b1"),
    ("ontology.mentions", "sent_from_own_domain"): (
        None, "49546173b7499a9f55e7761092263b4e754a0a7b2500e202b256a805b10bcd73"),
    ("ontology.mentions", "shared_mail_domain"): (
        None, "8e11969ff6f6e79f4945908211e933388bf0ece9c4247260d36bfa0fd02260d9"),
    ("ontology.people_directory", "HANDLE_PART"): (
        None, "bcfa97f4a000ba703368808526544fdde65a7b2c1b5490f4e75426313c6df1bc"),
    ("ontology.people_directory", "Mailbox"): (
        None, "d5e3f3945da23438c045b08b1ff68af6caffefdb8a49b06e05760e83cfd90563"),
    ("ontology.people_directory", "SCAN_READERS"): (
        None, "bf14675c191395a27a28cb3cf7f00c6f1646db77b6bf506db0780d94901de4b9"),
    ("ontology.people_directory", "_ADDRESS"): (
        None, "597b1b40d335d746517a4cdb5bbaa5c84e6dc1ec2dad1e8acf61e96c1777cf1a"),
    ("ontology.people_directory", "_OID"): (
        None, "46f3abb94ba23af2dc742032b1a9b9604b576716f4269bb604375a6fd8152007"),
    ("ontology.people_directory", "_canonical_digest"): (
        None, "ce96664e689a13f750154a261522f079052a9b0ce6ad14603415c588ae4c9cec"),
    ("ontology.people_directory", "_scan_range"): (
        None, "6d97b66ec1fee31a82b8f34649d7022de160d1b4e6e0c9fd2a34d744e9ed6881"),
    # A person is seen through every mailbox it corresponds in (from, to or cc): the
    # census reads recipients too and the people relation's audience keys name every
    # such mailbox (``mail-any:``). Only the people relation's records change (its own
    # fingerprint moves); every other relation reads the same records.
    ("ontology.people_directory", "COMPOSITE"): (
        None, "a287a71b75ffe63fec55b0f4a7798b709bf1ae074f7a9f58e54dc5289e42497e"),
    ("ontology.people_directory", "_CENSUS"): (
        None, "37586da62f356dcfc77305c5750ccd1c3f41f5f562a57668ce4f3354e6776f74"),
    ("ontology.people_directory", "_check"): (
        None, "da07492e228d2b3b59a5d9d5be63f9f6375c42b65136325b835f03d22d381112"),
    ("ontology.people_directory", "audience_key"): (
        None, "6262dd2ba6bdbc2cbaf8d1da00aa1de1939e949d931546b64557498689956587"),
    ("ontology.people_directory", "address"): (
        None, "62b762d8ec9da18bfd8e1576d2940f7ee246c59e424050f6e4e50617f765b6c6"),
    ("ontology.people_directory", "address_key"): (
        None, "7bd5ccf485e6553f0d5f88b6d23dff77e4bd17100ce67d904106a7e423dbe65b"),
    ("ontology.people_directory", "mailboxes"): (
        None, "711c019f99e2743fb3f4f685051d07ad95a5a95df5f0430df8794fc201b60335"),
    ("ontology.people_directory", "object_id"): (
        None, "b3bd943f97a98e0fde013dc3ba340b4fc32215038caf1d7cd1e770fa63c49117"),
    ("ontology.people_directory", "people"): (
        None, "0094749cfbd1ea30bae87125f171cac18d8de7694823043c4514a9b6c102b50e"),
    # An outside contact is cited graph-mail://people/person/<key> (no mailbox: a mailbox
    # id is an employee's) and seen by readers of the mailboxes it wrote to: the people
    # relation's handles and audience keys move (its own fingerprint), nothing else.
    ("ontology.people_directory", "PEOPLE_HOST"): (
        None, "401fc9fab2964e3cbc01e1c242681908b382149b0505b5cc3420f5f7dd8c1ce5"),
    ("ontology.people_directory", "_ADDRESS_KEY"): (
        None, "22de96b0129d9788382f9482a11d837ce573fefa5f3a5b3ac28019b3e2dd9b5c"),
    ("ontology.people_directory", "person_handle"): (
        None, "1804cec8225006b6460b81aaf96e4ed35ec45cadd07d131eaf1c69c70744ea8c"),
    ("ontology.people_directory", "sender_counts"): (
        None, "503dd9d5d604409ebeef447b08bda53691123622a6973b4d58eba5f3108b48ef"),
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
    # a kind may declare source_prefixes (a source's qualifier before the key, Sage's
    # MAKE: before an ORDUNIQ, the Portal's sage-dist- before a Sage item No.): parse
    # drops one before matching; text spans never carry one (the patterns are unchanged)
    "ontology/identifiers.py": frozenset({"_GLUED", "_source_unprefixed", "find_identifiers",
                                          "parse"}),
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
