"""The write twin: in-process emulators of the owning systems the action path writes, for
test mode only (owner directive 746d96497341).

* ``twin.portal.PortalTwin``: the Portal's cataloged write routes and its reporting read
  view, built from the generator's facts (``ontology/schema/generated/meta``
  ``portal_catalog.json`` and ``portal_prisma.json``) and the action catalog's
  ``real_capability`` routes, seeded from fixture captures (``twin/fixtures``).
* ``twin.mc.McTwin``: Mission Control's MCP tools as the generator pins them
  (``ontology/schema/generated/meta/mcp_tools.json``), on the isolated MC of
  ``operating.goal2_exec``, with its own MC role roster.
* ``twin.bc.BcTwin``: Business Central's API v2.0 (OData) per company, with etags, built
  from gen-bc's pinned ``$metadata`` (``ontology/schema/generated/meta/bc_metadata.json``),
  seeded from fixture captures; its records are typed relations of its own isolated owner.
* ``twin.transport.TwinTransport`` and ``twin.mode``: the ``twin-transport`` label, admitted
  only in test mode and never on a host holding a business credential.
* ``twin.world``: a test-mode world whose dispatcher registry sends every catalog action
  type to the twin (or refuses it as the catalog declares), and the lifecycle of each type.

Nothing here opens a network connection or reads a credential; a twin outcome is labelled
``twin-transport`` (Portal) or ``isolated-fixture`` (isolated owner, MC, BC), never live.
"""
