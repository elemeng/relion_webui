# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""The canvas: FlowGram's workflow UI, driven by this agent.

The vendored FlowGram app is a static bundle served at `/`, and its API is the
agent's own `/api` namespace plus a handful of canvas-only routes this package
adds (`/api/workflows`, `/api/runs`, `/api/events`, `/api/bootstrap`). There is
one set of routes, not two: anything the agent already answers - projects,
hosts, queue, job schema, browse - is used directly.

Modules:

``layout``      pipeline -> canvas geometry
``translate``   pipeline -> FlowGram workflow document
``forms``       RELION job fields -> FlowGram editor definitions
``workspaces``  draft overlays, stored in the config directory
``journal``     queue states -> FlowGram runtime events
``routes``      the canvas-only routes and the static mount
"""

from .routes import install, mount_static

__all__ = ['install', 'mount_static']
