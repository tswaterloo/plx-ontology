"""The Business Central write twin (owner directive 746d96497341), test mode only.

* ``twin.bc.odata.BcTwin``: BC's API v2.0 (OData) per company, with etags, generated from
  gen-bc's pinned ``$metadata``; its records are typed relations of its own isolated owner,
  so the catalog's isolated-owner types target a BC twin record through the action path.
* ``twin.bc.lifecycle``: a BC-targeted fixture action through propose, MC approval,
  dispatch, read-back and audit, in test mode.

Evidence label: ``isolated-fixture``; nothing here reaches BC or any network.
"""

from .odata import BcTwin, load_captures

__all__ = ["BcTwin", "load_captures"]
