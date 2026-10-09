"""``nisar-db mcp``: the MCP tools over stdio, for a local AI client."""

from __future__ import annotations

from pathlib import Path

import click


@click.command()
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=Path(".qa_helper_cache"),
    show_default=True,
    help="Where rebuilt views are read from and jobs run.",
)
@click.option(
    "--viewer-html",
    default=None,
    help="The 'published' dataset's page (default: the checkout's).",
)
@click.option(
    "--viewer-url",
    default="http://127.0.0.1:8797",
    show_default=True,
    help="Where viewer links point (a running `nisar-db serve`).",
)
@click.option("--max-jobs", type=int, default=2, show_default=True)
def mcp(
    cache_dir: Path, viewer_html: str | None, viewer_url: str, max_jobs: int
) -> None:
    """Serve the nisar_db tools to an AI client over stdio (MCP).

    The client starts this command itself; see docs/mcp.md for Claude Code and
    Claude Desktop. Nothing but the protocol goes to stdout.
    """
    try:
        from nisar_db.api.helper import default_viewer
        from nisar_db.api.jobs import Jobs
        from nisar_db.api.mcp_tools import McpContext, build_mcp
        from nisar_db.api.settings import Settings
        from nisar_db.api.store import FrameStore
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise click.ClickException(
            f"the MCP server needs the 'api' extra: pip install 'nisar_db[api]' ({exc})"
        ) from exc
    settings = Settings.for_mode(
        "local", cache_dir=cache_dir, viewer_html=viewer_html, max_jobs=max_jobs
    )
    published = Path(viewer_html) if viewer_html else default_viewer(settings)
    ctx = McpContext(
        settings=settings,
        frames=FrameStore(published, cache_dir / "views"),
        jobs=Jobs(cache_dir / "jobs", max_jobs=max_jobs),
        viewer_url=viewer_url,
    )
    build_mcp(ctx).run("stdio")
