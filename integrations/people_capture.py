"""What the mail, Teams and meetings captures read of the two excluded people (owner
decision 2026-11-21): everything. Their data is kept and shown to the owner alone, so the
captures no longer refuse their mailboxes, chats and meetings nor drop mail they send or
receive; who may read it is decided after capture, once per delta, by the people set
(``operating.pg.people_excluded``: the owner's alone, ``access_classes.OWNER_KEY``).

``IncludingExclusion`` is the captures' guard: the same ID list (``PeopleExclusion``), bound
and recorded as before (the tenant listing and its uncleared addresses,
``tenant_withheld_rows``, keep the excluded people's addresses offline), but it excludes no
one from a capture: ``is_excluded`` is False for every identity once the list loads, so
``refuse`` and ``excluded`` never refuse or drop. A list that does not load still fails
closed (``require``). A OneDrive stays refused (``refuse_onedrive_path`` reads the list
itself): the people set does not decide SharePoint items yet, so nothing of a OneDrive may
be captured where a full reader could read it.

On only where ``PLX_CAPTURE_PEOPLE`` is ``include`` (the capture units' environment, set
when the disk and the standby can take the re-capture: ``scripts/partners_recapture.py``);
otherwise the captures keep the old guard. Every other Graph reader (live reads, the org
and SharePoint captures) keeps the old guard always."""
from __future__ import annotations

from typing import Any, Mapping

from .people_exclusion import PeopleExclusion

ENV = "PLX_CAPTURE_PEOPLE"
INCLUDE = "include"


class IncludingExclusion(PeopleExclusion):
    """A ``PeopleExclusion`` that excludes no one from a capture (module docstring)."""

    def is_excluded(self, value: Any) -> bool:
        self.require()
        return False


def enabled(environ: Mapping[str, str]) -> bool:
    """Whether the captures include the excluded people now (``ENV`` is ``include``)."""
    return str(environ.get(ENV) or "").strip().lower() == INCLUDE


def capture_guard(environ: Mapping[str, str]) -> PeopleExclusion:
    """The guard a mail, Teams or meetings capture reads with: ``IncludingExclusion`` when
    ``enabled``, else the people exclusion as before."""
    cls = IncludingExclusion if enabled(environ) else PeopleExclusion
    return cls.from_env(environ)
