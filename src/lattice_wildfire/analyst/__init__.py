"""App 4 Kodros Analyst: a taskable Lattice agent that writes wildfire intel briefs.

It publishes itself as a ``kodros-analyst`` asset advertising Investigate, takes tasks from
the agent stream, gathers facts (target entity, NWS forecast and alerts, FHSZ, nearest
stations and hospitals, nearby fire entities), asks the local Kodros cluster model for a
six-line facts-only brief and uploads it as ``lattice-wildfire/briefs/{task_id}.md|.json``.
"""

from .agent import INVESTIGATE_URL, AnalystAgent, build_agent_entity, default_entity_id
from .facts import AnalystInputs, SourceRecord, Target, gather_facts
from .gather import Brief, LiveSources, make_brief

__all__ = [
    "INVESTIGATE_URL",
    "AnalystAgent",
    "AnalystInputs",
    "Brief",
    "LiveSources",
    "SourceRecord",
    "Target",
    "build_agent_entity",
    "default_entity_id",
    "gather_facts",
    "make_brief",
]
