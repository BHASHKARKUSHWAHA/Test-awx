"""Tool layer for the AWX AI Agent.

Every capability the agent has is a declared tool with a name, a description, an
input schema, a permission level, a timeout and an audit trail (spec section
50).  The LLM never gets a shell - it can only ask for tools that exist here.
"""

from . import registry  # noqa: F401  (import order matters: registry first)


def load_all() -> None:
    """Import every tool module so the registry is fully populated."""
    from . import linux, kubernetes, awx, monitoring, networking  # noqa: F401


__all__ = ["registry", "load_all"]
