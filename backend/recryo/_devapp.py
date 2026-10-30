# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""App factory for ``--reload``.

``uvicorn --reload`` re-imports an import string in a fresh process on every
change, so the app cannot be a pre-built object: nothing would carry the CLI's
arguments across the restart. The CLI therefore serialises its settings into
``RECRYO_*`` environment variables and points uvicorn at ``app`` here, which
rebuilds exactly the same app from them.

Kept in its own module so the ordinary path (no reload) never imports it, and
so the environment contract has one obvious place to read.
"""

import json
import os
from pathlib import Path
from typing import List

from .app import create_app
from .auth import AuthManager
from .projects import ProjectRegistry


def _roots() -> List[Path]:
    raw = os.environ.get('RECRYO_ROOTS', '[]')
    return [Path(item) for item in json.loads(raw) if item]


def app():
    """Rebuild the agent from the environment ``recryo --reload`` set.

    The project registry lives in the config directory, which the parent process
    already populated, so the added projects carry across the restart without
    being passed again; only the path roots need restoring.
    """
    config = os.environ.get('RECRYO_CONFIG')
    projects = ProjectRegistry(Path(config) if config else None)

    auth = None
    if os.environ.get('RECRYO_AUTH', '0') == '1':
        hours = float(os.environ.get('RECRYO_SESSION_HOURS', '12'))
        auth = AuthManager(
            session_lifetime=hours * 3600,
            ssh_options={
                'host': os.environ.get('RECRYO_SSH_HOST', '127.0.0.1'),
                'port': int(os.environ.get('RECRYO_SSH_PORT', '22')),
            },
        )

    assistant_config = None
    if os.environ.get('RECRYO_ASSISTANT') == '1' or os.environ.get('RECRYO_MCP') == '1':
        from . import assistant as assistant_module

        assistant_config = assistant_module.config_from_env()

    application = create_app(
        roots=_roots(),
        poll_interval=float(os.environ.get('RECRYO_POLL', '0.5')),
        auth=auth,
        projects=projects,
        enable_assistant=os.environ.get('RECRYO_ASSISTANT') == '1',
        enable_mcp=os.environ.get('RECRYO_MCP') == '1',
        assistant_config=assistant_config,
        mcp_hosts=[os.environ.get('RECRYO_HOST', '127.0.0.1'), '127.0.0.1', 'localhost', '::1'],
        mcp_allow_writes=os.environ.get('RECRYO_MCP_READ_ONLY') != '1',
        host_nodes=json.loads(os.environ.get('RECRYO_HOST_NODES') or '[]'),
    )
    application.state.initial_project = (
        os.environ.get('RECRYO_INITIAL_PROJECT') or None
    )
    return application
