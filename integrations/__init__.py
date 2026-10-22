"""Read-only source adapters. No PLX write authority is granted here.

Importing the package loads nothing else: callers import the client they need
(``integrations.bc_read``, ``integrations.graph_read``, ``integrations.mc`` ...),
so ``from integrations import bc_read`` never pulls in the Mission Control client.
"""
