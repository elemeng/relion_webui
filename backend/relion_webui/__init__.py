# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Web interface for RELION cryo-ET data management and viewing."""

from .app import create_app

__all__ = ['create_app']
