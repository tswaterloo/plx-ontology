"""Hybrid retrieval and ranking over ``search.chunks`` (CONTRACT.md).

``search(understanding, actor, *, k=20, kinds=None) -> SearchResult``; see
``operating/search/core.py``. ``PLX_SEARCH=hybrid`` routes the free-text paths of
``plx.find`` and ``plx.documents`` through it (``operating/search/plug.py``).
"""
from operating.search.core import SearchResult, search
from operating.search.reader import SearchReader

__all__ = ["SearchReader", "SearchResult", "search"]
