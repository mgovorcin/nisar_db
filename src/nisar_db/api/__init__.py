"""REST API for nisar_db: catalog queries, jobs and the viewer (``nisar-db serve``).

Needs the ``api`` extra (``pip install nisar_db[api]``). The read model
(``nisar_db.api.store``) and the settings do not, so they import lazily here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Settings", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from nisar_db.api.app import create_app

        return create_app
    if name == "Settings":
        from nisar_db.api.settings import Settings

        return Settings
    raise AttributeError(name)
