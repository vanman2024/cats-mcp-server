"""CATS MCP - a universal MCP adapter for the CATS (CatsOne) API v3.

This package is an adapter and a tool server. It owns CATS authentication,
endpoint coverage, tool schemas, discovery metadata, response shaping,
pagination, rate-limit handling and safety classification.

It deliberately does not own recruiting workflows, agent orchestration, memory,
outreach, scheduling, candidate-ranking policy, or any consumer's product state.
Those belong to the orchestrator (StaffHive/Mastra, Google ADK, or any other
MCP client).
"""

__version__ = "0.2.0"
