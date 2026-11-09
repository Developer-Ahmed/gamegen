"""React Agent.

This module defines a custom reasoning and action agent graph.
It invokes tools in a simple loop.
"""

__all__ = ["agent"]


def __getattr__(name):
    if name == "agent":
        from .graph import agent

        return agent

    raise AttributeError(name)
