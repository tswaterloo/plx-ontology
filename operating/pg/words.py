"""The word index on Postgres (migration 0010): FTS5 queries over it, and the document
words it holds.

The SQLite store's ``value_text`` (``ontology/store_ddl.py`` ``TEXT_FTS``: ``unicode61
remove_diacritics 2``, ``detail='none'``) is on Postgres a GIN index over the value cells
of ``ontology.sq_words``, the same tokenizer in SQL (``docs/pg-cutover.md``). Its
``chunk_text`` (the document words) is the table ``ontology.chunk_words``: each chunk's
distinct words as SQLite's own FTS5 tokenizer cuts them (:func:`text_words`), so a
chunk matches on Postgres exactly where it matches in the SQLite store.

:func:`tsquery` translates the FTS5 query the read tools write (quoted or bare terms, a
``*`` prefix, ``AND`` / ``OR`` / ``NOT``, parentheses, adjacency as AND) into a
``tsquery`` of those terms (``'term':*`` for a prefix); each term is folded by SQLite's
own tokenizer, as FTS5 folds a query. What FTS5 refuses on this table (a phrase of several
words, a column filter, ``NEAR(...)``, a leading ``NOT``) or what is not that grammar
raises ``sqlite3.OperationalError``, as the SQLite store would: the tools then report a
cut search, never an absence. An empty phrase (``""``) and a term over
``MAX_LEXEME_BYTES`` raise too.
"""

from __future__ import annotations

from contextlib import closing
import functools
import re
import sqlite3
from typing import Iterable, Iterator, Sequence

from ontology.store_ddl import TEXT_TOKENIZER

# Postgres: a lexeme is shorter than 2 KB.
MAX_LEXEME_BYTES = 2046
_ASCII_WORD = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------- queries
_QUERY_TOKEN = re.compile(r'''\s*(?:(?P<str>"(?:[^"]|"")*")|(?P<op>[()*])|'''
                          r'''(?P<bare>[A-Za-z0-9_\u0080-\U0010ffff]+)|(?P<bad>\S))''')


class _Parser:
    """FTS5's query grammar as the tools use it: or := and (OR and)*;
    and := not ((AND)? not)*; not := primary (NOT primary)*;
    primary := '(' or ')' | phrase ['*']."""

    def __init__(self, query: str) -> None:
        self.tokens: list[tuple[str, str]] = []
        for match in _QUERY_TOKEN.finditer(query):
            kind = match.lastgroup
            if kind is None:
                continue
            text = match.group(kind)
            if kind == "bad":
                raise sqlite3.OperationalError(f"fts5: syntax error near \"{text}\"")
            if kind == "op":
                kind = text
            elif kind == "bare" and text in ("AND", "OR", "NOT"):
                kind = text
            self.tokens.append((kind, text))
        for at, (kind, text) in enumerate(self.tokens[:-1]):
            if kind == "bare" and text == "NEAR" and self.tokens[at + 1][0] == "(":
                raise sqlite3.OperationalError("fts5: NEAR is not supported (detail=none)")
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at][0] if self.at < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def parse(self) -> str:
        if not self.tokens:
            raise sqlite3.OperationalError("fts5: empty query")
        out = self.or_()
        if self.at != len(self.tokens):
            raise sqlite3.OperationalError(
                f"fts5: syntax error near \"{self.tokens[self.at][1]}\"")
        return out

    def or_(self) -> str:
        parts = [self.and_()]
        while self.peek() == "OR":
            self.take()
            parts.append(self.and_())
        return parts[0] if len(parts) == 1 else "(" + " | ".join(parts) + ")"

    def and_(self) -> str:
        parts = [self.not_()]
        while True:
            if self.peek() == "AND":
                self.take()
            elif self.peek() not in ("str", "bare"):
                break
            elif self.tokens[self.at - 1][0] == ")":   # FTS5: ``(a) b`` is an error
                raise sqlite3.OperationalError(
                    f"fts5: syntax error near \"{self.tokens[self.at][1]}\"")
            parts.append(self.not_())
        return parts[0] if len(parts) == 1 else "(" + " & ".join(parts) + ")"

    def not_(self) -> str:
        out = self.primary()
        while self.peek() == "NOT":
            self.take()
            out = f"({out} & !{self.primary()})"
        return out

    def primary(self) -> str:
        kind = self.peek()
        if kind == "(":
            self.take()
            inner = self.or_()
            if self.peek() != ")":
                raise sqlite3.OperationalError("fts5: syntax error (unclosed parenthesis)")
            self.take()
            return inner
        if kind not in ("str", "bare"):
            near = self.tokens[self.at][1] if kind else "end of query"
            raise sqlite3.OperationalError(f"fts5: syntax error near \"{near}\"")
        _kind, text = self.take()
        if text.startswith('"'):
            text = text[1:-1].replace('""', '"')
        prefix = self.peek() == "*"
        if prefix:
            self.take()
        terms = _ordered_terms(text)
        if not terms:
            raise sqlite3.OperationalError("fts5: a phrase without a word")
        if len(terms) > 1:
            raise sqlite3.OperationalError(
                "fts5: phrase queries are not supported (detail!=full)")
        term = terms[0]
        if len(term.encode("utf-8")) > MAX_LEXEME_BYTES:
            raise sqlite3.OperationalError("fts5: a term longer than the index holds")
        return "'" + term.replace("\\", "\\\\").replace("'", "''") + "'" + (":*" if prefix else "")


def _ordered_terms(text: str) -> list[str]:
    """Every word of ``text`` with repeats (``"a a"`` is a phrase of two words in FTS5)."""
    if text.isascii():
        return _ASCII_WORD.findall(text.lower())
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE VIRTUAL TABLE w USING fts5(x, tokenize='{TEXT_TOKENIZER}')")
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(w, 'instance')")
        conn.execute("INSERT INTO w (rowid, x) VALUES (1, ?)", (text,))
        return [row[0] for row in conn.execute("SELECT term FROM v")]


@functools.lru_cache(maxsize=4096)
def tsquery(query: str) -> str:
    """The ``tsquery`` text of an FTS5 query over the word index (module docstring);
    raises ``sqlite3.OperationalError`` where FTS5 would, or where it is no query of the
    grammar the tools write."""
    if not isinstance(query, str):
        raise sqlite3.OperationalError("fts5: the query is no text")
    return _Parser(query).parse()


# --------------------------------------------------------------------- document words
_TOKENIZERS: dict[int, sqlite3.Connection] = {}


def _tokenizer() -> sqlite3.Connection:
    """An FTS5 table of the store's tokenizer and its ``instance`` vocabulary (one per
    thread: ``detail='none'`` keeps one row per distinct word of a row)."""
    import threading
    key = threading.get_ident()
    conn = _TOKENIZERS.get(key)
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("CREATE VIRTUAL TABLE w USING fts5(x, detail='none', "
                     f"tokenize='{TEXT_TOKENIZER}')")
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(w, 'instance')")
        _TOKENIZERS[key] = conn
    return conn


def text_words(texts: Sequence[str]) -> list[list[str]]:
    """Each text's distinct words as FTS5 indexes them (``chunk_text``), sorted; an ASCII
    text by the same rule in Python (letters and digits, lower case), the others through
    SQLite's tokenizer. A word Postgres cannot hold (over ``MAX_LEXEME_BYTES``) is left
    out, as no query names it (``tsquery`` refuses such a term)."""
    out: list[list[str]] = [[] for _ in texts]
    other: list[tuple[int, str]] = []
    for at, text in enumerate(texts):
        if text.isascii():
            out[at] = sorted({w for w in _ASCII_WORD.findall(text.lower())
                              if len(w) <= MAX_LEXEME_BYTES})
        else:
            other.append((at, text))
    if other:
        conn = _tokenizer()
        with conn:
            conn.execute("DELETE FROM w")
            conn.executemany("INSERT INTO w (rowid, x) VALUES (?, ?)",
                             [(at + 1, text) for at, text in other])
            for doc, term in conn.execute("SELECT doc, term FROM v"):
                if len(term.encode("utf-8")) <= MAX_LEXEME_BYTES:
                    out[doc - 1].append(term)
            conn.execute("DELETE FROM w")
        for at, _text in other:
            out[at].sort()
    return out


def tsvector_text(words: Sequence[str]) -> str:
    """``words`` as ``tsvector`` input (each quoted: no word is parsed again)."""
    return " ".join("'" + w.replace("\\", "\\\\").replace("'", "''") + "'" for w in words)


def chunk_word_rows(chunks: Iterable[tuple[int, bytes]], batch: int = 2000
                    ) -> Iterator[tuple[int, str]]:
    """``(chunk_id, tsvector text)`` of each ``(chunk_id, zlib body)``: the rows of
    ``ontology.chunk_words``."""
    from ontology.text_index import chunk_text
    held: list[tuple[int, bytes]] = []

    def flush() -> Iterator[tuple[int, str]]:
        words = text_words([chunk_text(body) for _id, body in held])
        for (chunk_id, _body), terms in zip(held, words):
            yield chunk_id, tsvector_text(terms)
        held.clear()

    for row in chunks:
        held.append((row[0], bytes(row[1])))
        if len(held) >= batch:
            yield from flush()
    yield from flush()


__all__ = ["MAX_LEXEME_BYTES", "chunk_word_rows", "text_words", "tsquery",
           "tsvector_text"]
