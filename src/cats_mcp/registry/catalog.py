"""The full CATS tool catalog - the single source of truth for tool counts."""

from __future__ import annotations

from cats_mcp.registry.models import Registry
from cats_mcp.registry.specs import activities as _activities
from cats_mcp.registry.specs import attachments as _attachments
from cats_mcp.registry.specs import backups as _backups
from cats_mcp.registry.specs import candidates as _candidates
from cats_mcp.registry.specs import companies as _companies
from cats_mcp.registry.specs import contacts as _contacts
from cats_mcp.registry.specs import context as _context
from cats_mcp.registry.specs import events as _events
from cats_mcp.registry.specs import jobs as _jobs
from cats_mcp.registry.specs import pipelines as _pipelines
from cats_mcp.registry.specs import portals as _portals
from cats_mcp.registry.specs import tags as _tags
from cats_mcp.registry.specs import tasks as _tasks
from cats_mcp.registry.specs import triggers as _triggers
from cats_mcp.registry.specs import users as _users
from cats_mcp.registry.specs import webhooks as _webhooks
from cats_mcp.registry.specs import work_history as _work_history


def build_registry() -> Registry:
    """Assemble every tool spec into one registry."""
    registry = Registry()
    registry.extend(_activities.SPECS)
    registry.extend(_attachments.SPECS)
    registry.extend(_backups.SPECS)
    registry.extend(_candidates.SPECS)
    registry.extend(_companies.SPECS)
    registry.extend(_contacts.SPECS)
    registry.extend(_context.SPECS)
    registry.extend(_events.SPECS)
    registry.extend(_jobs.SPECS)
    registry.extend(_pipelines.SPECS)
    registry.extend(_portals.SPECS)
    registry.extend(_tags.SPECS)
    registry.extend(_tasks.SPECS)
    registry.extend(_triggers.SPECS)
    registry.extend(_users.SPECS)
    registry.extend(_webhooks.SPECS)
    registry.extend(_work_history.SPECS)
    return registry


#: Module-level singleton. Counts in docs, logs and tests read from this.
REGISTRY = build_registry()
