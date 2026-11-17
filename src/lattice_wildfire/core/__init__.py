"""Shared foundation for every lattice-wildfire app: config, client, entities, geo, http, layers, llm.

Import from the submodules (``from lattice_wildfire.core.entities import provenance``); this package
re-exports nothing so that importing one helper never pulls in network or SDK setup it does not need.
"""
