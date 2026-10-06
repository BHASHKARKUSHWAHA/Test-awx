"""Agent factory (spec section 28).

A created agent is a *restriction* of the orchestrator: a name, a purpose, the
tool categories it may use and the highest permission level it may reach.  It
is not a second LLM process - on an 8 GB machine that would be the fastest way
to make everything unusable.

Limits come from ``policies/permissions.yaml`` -> ``agent_factory``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tools import registry
from tools.registry import Level

from .permissions import agent_factory_limits

STORE_PATH = os.environ.get("AGENT_REGISTRY_PATH", "/var/lib/smart-awx-ai/agents.json")
NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{2,48}$")
_guard = threading.Lock()


class FactoryError(ValueError):
    pass


@dataclass
class AgentDefinition:
    name: str
    purpose: str
    categories: List[str]
    max_level: str
    created_at: float = field(default_factory=time.time)
    created_by: str = "admin"
    builtin: bool = False

    def tools(self) -> List[str]:
        ceiling = Level.coerce(self.max_level)
        return sorted(
            tool.name
            for tool in registry.all_tools().values()
            if tool.category in self.categories and tool.level.rank <= ceiling.rank
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "categories": self.categories,
            "max_level": self.max_level,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "builtin": self.builtin,
            "tools": self.tools(),
            "tool_count": len(self.tools()),
        }


BUILTINS = [
    AgentDefinition("Linux Agent", "Host OS inspection and safe service recovery", ["linux"], "safe_write", builtin=True),
    AgentDefinition("Kubernetes Agent", "Cluster inspection, pod diagnosis and restarts", ["kubernetes"], "safe_write", builtin=True),
    AgentDefinition("AWX Agent", "AWX projects, templates and job orchestration", ["awx"], "safe_write", builtin=True),
    AgentDefinition("Network Agent", "Connectivity, DNS, ports and firewall state", ["networking"], "read_only", builtin=True),
    AgentDefinition("Monitoring Agent", "Cross-layer health and resource reporting", ["monitoring", "linux", "kubernetes"], "read_only", builtin=True),
    AgentDefinition("Security Agent", "Firewall, listening ports and auth log review", ["networking", "linux"], "read_only", builtin=True),
]


def _load() -> Dict[str, AgentDefinition]:
    created: Dict[str, AgentDefinition] = {}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as handle:
            for row in json.load(handle):
                created[row["name"]] = AgentDefinition(
                    name=row["name"],
                    purpose=row.get("purpose", ""),
                    categories=row.get("categories") or [],
                    max_level=row.get("max_level", "read_only"),
                    created_at=row.get("created_at", time.time()),
                    created_by=row.get("created_by", "admin"),
                )
    except (OSError, ValueError, KeyError):
        pass
    return created


def _save(created: Dict[str, AgentDefinition]) -> None:
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump([a.to_dict() for a in created.values()], handle, indent=2)
        os.replace(tmp, STORE_PATH)
        os.chmod(STORE_PATH, 0o640)
    except OSError:
        pass


def list_agents() -> List[Dict[str, Any]]:
    created = _load()
    return [a.to_dict() for a in BUILTINS] + [a.to_dict() for a in created.values()]


def get_agent(name: str) -> Optional[AgentDefinition]:
    for definition in BUILTINS:
        if definition.name.lower() == name.lower():
            return definition
    created = _load()
    for definition in created.values():
        if definition.name.lower() == name.lower():
            return definition
    return None


def create_agent(name: str, purpose: str, categories: List[str], max_level: str, created_by: str) -> Dict[str, Any]:
    """Create a restricted agent, enforcing every factory limit."""
    limits = agent_factory_limits()
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise FactoryError("agent name must be 2-48 characters of letters, digits, spaces, '-' or '_'")
    if get_agent(name):
        raise FactoryError("an agent named %r already exists" % name)

    allowed_categories = limits.get("allowed_tool_categories") or []
    categories = [c.strip().lower() for c in categories if c and c.strip()]
    if not categories:
        raise FactoryError("choose at least one tool category")
    unknown = [c for c in categories if allowed_categories and c not in allowed_categories]
    if unknown:
        raise FactoryError("category not permitted by policy: %s (allowed: %s)" % (", ".join(unknown), ", ".join(allowed_categories)))

    try:
        requested = Level.coerce(max_level)
    except ValueError:
        raise FactoryError("max_level must be one of read_only, safe_write, medium_risk, high_risk")
    ceiling = Level.coerce(limits.get("max_permission_level", "safe_write"))
    if requested.rank > ceiling.rank:
        raise FactoryError("policy caps created agents at %s; %s was requested" % (ceiling.value, requested.value))

    with _guard:
        created = _load()
        if len(created) >= int(limits.get("max_agents", 8)):
            raise FactoryError("agent limit reached (%s). Delete an agent before creating another." % limits.get("max_agents", 8))
        definition = AgentDefinition(
            name=name,
            purpose=purpose.strip()[:500],
            categories=categories,
            max_level=requested.value,
            created_by=created_by,
        )
        if not definition.tools():
            raise FactoryError("that combination of categories and permission level grants no tools")
        created[name] = definition
        _save(created)
    return definition.to_dict()


def delete_agent(name: str) -> bool:
    with _guard:
        created = _load()
        for key in list(created):
            if key.lower() == name.lower():
                del created[key]
                _save(created)
                return True
    return False
