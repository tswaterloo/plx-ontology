"""Authority-aware ordering: one set of priors for ``plx.documents`` (the keyword path,
``operating.ontology_tools_evidence._document_order``, and the captured SharePoint files
the case host adds, ``operating.case_tools_host``) and hybrid ranking
(``operating.search.core.rank``), read from the ``authority`` block of ``weights.json``.

A document's prior is the sum of the signals it carries (each weight in that block):

- ``use_case_document``: it is one of the use-case catalog's documents
  (``operating.use_case_catalog``: a case original entry or a SharePoint item, any
  version), the business's own map of a process;
- ``captured_original``: a captured case original (``original://``) or a process-map
  section (hybrid ``map_stage``);
- ``identifier_in_title``: its title or file name names an identifier the query names
  (the one grammar, ``ontology.mentions.extract``: kind and normalized value equal);
- ``names_record``: it holds, or is linked to a record holding, an identifier the query
  names (``plx.documents``' identifier rows and their links: a supplier's mail about the
  PO asked, not a mail that only shares a word with the question);
- ``automated_mail`` (negative): mail whose sender is a system sender
  (``automated_senders``: a local part such as ``no-reply`` or ``notifications``, a
  system domain, or a subdomain label such as ``noreply.``) with no reply from a person;
  ``automated_mail_replied`` (smaller) instead when a person (a sender that is not
  automated) wrote in the same conversation among the matches. Mail from a person is
  never demoted for being unanswered: a supplier's one message is evidence like any
  other. The capture keeps no ``List-Unsubscribe`` / ``Auto-Submitted`` /
  ``Precedence`` header (``integrations.graph_read`` records only the authentication
  results), so the sender is the only automation signal there is.

``plx.documents`` adds ``word_share`` x the share of the query's words a document's own
values hold (its subject, sender, name: a mail about the PO from the supplier the
question names), and ``relevance`` / its hybrid rank when PLX_SEARCH=hybrid ranked it;
hybrid ranking adds ``hybrid_scale`` x the prior to its final score.

The priors only order what the reader may already see: they never admit, drop or hide a
document, and every count and gap stays the search's own. A block that does not validate
is refused (``config.WeightsError``), as every other weight is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

SIGNALS = ("use_case_document", "captured_original", "identifier_in_title", "names_record",
           "automated_mail", "automated_mail_replied")
SENDER_KEYS = ("local_words", "local_contains", "domains", "domain_labels")
_LOCAL_SPLIT = re.compile(r"[._+-]+")


@dataclass(frozen=True)
class Authority:
    weights: Mapping[str, float]
    relevance: float            # plx.documents: the hybrid rank's weight beside the priors
    hybrid_scale: float         # hybrid: the priors' scale inside the final score
    word_share: float           # plx.documents: weight of the share of query words held
    local_words: frozenset[str]
    local_contains: tuple[str, ...]
    domains: frozenset[str]
    domain_labels: frozenset[str]

    def automated_sender(self, address: Any) -> bool:
        """Whether ``address`` (an e-mail address, or a bare domain) is a system sender:
        a word of its local part (split at ``.``, ``_``, ``+``, ``-``) is a system word
        (``seller-notification``), or its local part without separators holds one
        (``no-reply-forms``), or its domain is (under) a system domain, or a subdomain
        label is a system label (``noreply.example.com``)."""
        if not isinstance(address, str):
            return False
        text = address.strip().strip("<>").casefold()
        local, _, domain = text.rpartition("@") if "@" in text else ("", "", text)
        domain = domain.strip(". >")
        if not domain:
            return False
        if local:
            words = [w for w in _LOCAL_SPLIT.split(local) if w]
            compact = "".join(words)
            if any(w in self.local_words for w in words) \
                    or any(part in compact for part in self.local_contains):
                return True
        labels = domain.split(".")
        if any(domain == d or domain.endswith("." + d) for d in self.domains):
            return True
        # Subdomain labels only (``noreply.example.com``): a registrable domain that is
        # itself such a word (``mail.com``) is a mailbox provider, not a system sender.
        return any(label in self.domain_labels for label in labels[:-2])

    def prior(self, signals: Iterable[str]) -> float:
        return sum(self.weights.get(name, 0.0) for name in set(signals))


def parse(raw: Any) -> Authority:
    from operating.search.config import WeightsError, _number
    if not isinstance(raw, Mapping) or set(raw) != {*SIGNALS, "relevance", "hybrid_scale",
                                                    "word_share", "automated_senders"}:
        raise WeightsError("weights_invalid:authority")
    weights = {name: _number(raw[name], f"authority.{name}", -10, 10) for name in SIGNALS}
    if weights["automated_mail"] > 0 or weights["automated_mail_replied"] > 0:
        raise WeightsError("weights_invalid:authority.demotion_positive")
    if any(weights[name] < 0 for name in SIGNALS if not name.startswith("automated_")):
        raise WeightsError("weights_invalid:authority.boost_negative")
    senders = raw["automated_senders"]
    if not isinstance(senders, Mapping) or set(senders) != set(SENDER_KEYS) or not all(
            isinstance(senders[k], list) and all(isinstance(v, str) and v.strip()
                                                 for v in senders[k])
            for k in SENDER_KEYS):
        raise WeightsError("weights_invalid:authority.automated_senders")

    def folded(key: str) -> frozenset[str]:
        return frozenset(v.strip().casefold() for v in senders[key])

    return Authority(weights, _number(raw["relevance"], "authority.relevance", 0, 10),
                     _number(raw["hybrid_scale"], "authority.hybrid_scale", 0, 1),
                     _number(raw["word_share"], "authority.word_share", 0, 10),
                     folded("local_words"),
                     tuple(sorted(_LOCAL_SPLIT.sub("", v) for v in folded("local_contains"))),
                     folded("domains"), folded("domain_labels"))


def load() -> Authority:
    """The shipped (or ``PLX_SEARCH_WEIGHTS``) weights' authority block."""
    from operating.search.config import load_weights
    return load_weights().authority


# ---------------------------------------------------------------- catalog documents

def catalog_keys() -> frozenset[str]:
    """The use-case catalog's documents as version-free keys (:func:`document_key`); an
    unreadable catalog gives none (nothing is boosted, nothing else changes)."""
    from operating import use_case_catalog as ucc
    try:
        catalog = ucc.load()
    except ucc.CatalogError:
        return frozenset()
    keys = set()
    for case in catalog.use_cases:
        for document in case.documents:
            if "original" in document:
                keys.add("original://" + document["original"])
            else:
                drive, item = document["sharepoint"]
                keys.add(f"sharepoint://{drive}/{item}")
    return frozenset(keys)


def document_key(handle: Any) -> str | None:
    """A citation as a version-free document key: ``original://<entry>`` (page and
    fragment dropped), ``sharepoint://<drive>/<item>`` (drive unquoted, version dropped)."""
    if not isinstance(handle, str):
        return None
    base = handle.split("#", 1)[0]
    if base.startswith("original://"):
        return base
    if base.startswith("sharepoint://"):
        path = base[len("sharepoint://"):].split("@", 1)[0]
        drive, _, item = path.partition("/")
        if drive and item:
            return f"sharepoint://{unquote(drive)}/{unquote(item).split('/', 1)[0]}"
    return None


def query_identifiers(query: Any) -> frozenset[tuple[str, str]]:
    """(kind, normalized) of every identifier ``query`` names (the one grammar)."""
    if not isinstance(query, str) or not query.strip():
        return frozenset()
    from ontology import mentions as mn
    try:
        return frozenset((m["identifier_kind"], str(m["normalized"]))
                         for m in mn.extract(query) if m.get("normalized"))
    except Exception:  # noqa: BLE001 - a text the grammar refuses names nothing
        return frozenset()


def names_identifier(title: Any, wanted: frozenset[tuple[str, str]]) -> bool:
    """Whether ``title`` names one of ``wanted`` (kind and normalized value equal)."""
    if not wanted or not isinstance(title, str) or not title.strip():
        return False
    return bool(query_identifiers(title) & wanted)


def mail_signals(rule: Authority, senders: Mapping[str, Any],
                 conversations: Mapping[str, Any]) -> dict[str, list[str]]:
    """The mail signals of each message (object id -> signal names) from the matches'
    own sender and conversation values: no other message is read, so 'replied' means a
    person wrote in the conversation among the matches."""
    automated = {oid: rule.automated_sender(senders.get(oid)) for oid in senders}
    by_conversation: dict[str, list[str]] = {}
    for oid, conversation in conversations.items():
        if isinstance(conversation, str) and conversation:
            by_conversation.setdefault(conversation, []).append(oid)
    out: dict[str, list[str]] = {}
    for oid in senders:
        conversation = conversations.get(oid)
        members = by_conversation.get(conversation, [oid]) if isinstance(conversation, str) \
            else [oid]
        human = [m for m in members if not automated.get(m, False)
                 and isinstance(senders.get(m), str)]
        if automated[oid]:
            out[oid] = ["automated_mail_replied" if human else "automated_mail"]
    return out


def chunk_signals(rule: Authority, row: Mapping[str, Any], catalog: frozenset[str],
                  id_tokens: Sequence[str]) -> list[str]:
    """The signals of one hybrid candidate chunk (``search.chunks`` row): its kind, its
    source handle, its title and its author (``external (<domain>)``: the sender's
    domain, the only sender part a chunk keeps)."""
    out: list[str] = []
    key = document_key(row.get("source_handle"))
    if key is not None and key in catalog:
        out.append("use_case_document")
    if row.get("kind") in ("original", "map_stage"):
        out.append("captured_original")
    title = str(row.get("title") or "").casefold()
    if title and any(token.casefold() in title for token in id_tokens):
        out.append("identifier_in_title")
    if row.get("kind") == "mail":
        role = str(row.get("author_role") or "")
        domain = role[len("external ("):-1] if role.startswith("external (") \
            and role.endswith(")") else ""
        if domain and rule.automated_sender(domain):
            out.append("automated_mail")
    return out
