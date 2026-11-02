# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""FlowGram-shaped API, served from inside the RELION agent.

The FlowGram canvas is a separate front end with its own vocabulary - projects,
workflows, runs, events. This package translates between it and RELION's -
pipelines, jobs, queue entries - so the canvas can be served by the same
FastAPI process as everything else.

Modules:

``layout``      pipeline -> canvas geometry (a port of the main UI's layout)
``translate``   pipeline -> FlowGram workflow document
``workspaces``  draft overlays, stored in the config directory
``journal``     queue states -> FlowGram runtime events
``routes``      the ``/fg`` router and its static mount
"""

from .routes import PREFIX, install

__all__ = ['PREFIX', 'install']
