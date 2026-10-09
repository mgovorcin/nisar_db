# MCP server for AI assistants

nisar_db includes a [Model Context Protocol](https://modelcontextprotocol.io)
(MCP) server. An AI assistant connected to it, such as Claude Code or Claude
Desktop, can answer questions about the NISAR frames by calling it:

- "Which frames got a 4005 acquisition in cycle 23?"
- "List the GUNW pairs over frame 34_19 since June."
- "When is frame 28533 blacked out?"
- "Search CMR for all GSLC granules on track 163 and give me the CSV."

It can also give you a link that opens the viewer on what it found.

The tools answer from the same frames as the [REST API](api.md) and the
[frame viewer](frame-viewer.md), and they run jobs through the same job runner,
so all three always agree.

## Two ways to connect

| | `nisar-db mcp` (stdio) | `/mcp` on `nisar-db serve` (HTTP) |
|---|---|---|
| Who starts it | the AI client, on your machine | you, once; clients connect by URL |
| Good for | Claude Code / Claude Desktop next to your checkout | a team server, remote clients |
| Keys | none | none locally; an API key in shared mode |
| Jobs run | on your machine | on the server |

Both need the `api` extra: `pip install "nisar_db[api]"`. In a checkout,
`pixi install` already includes it.

### Claude Code, local

Run this from your nisar_db checkout:

```bash
claude mcp add nisar-db -- "$(pixi run which nisar-db)" mcp \
    --cache-dir "$PWD/.qa_helper_cache"
```

Claude Code starts the server whenever it needs it. Check with `claude mcp
list`, or `/mcp` inside a session. Add `-s user` to make it available in every
project, not only this one.

### Claude Desktop, local

Add the server to `claude_desktop_config.json` (Settings → Developer → Edit
Config), using absolute paths:

```json
{
  "mcpServers": {
    "nisar-db": {
      "command": "/path/to/nisar_db/.pixi/envs/default/bin/nisar-db",
      "args": ["mcp", "--cache-dir", "/path/to/nisar_db/.qa_helper_cache"]
    }
  }
}
```

Then restart Claude Desktop.

### Over HTTP, local or shared

Every `nisar-db serve` also serves the tools at `/mcp`:

```bash
nisar-db serve                                          # http://127.0.0.1:8797/mcp
claude mcp add --transport http nisar-db http://127.0.0.1:8797/mcp
```

A shared service asks for an API key at `/mcp`, the same keys as the REST API
(see [Running a shared service](api.md#running-a-shared-service)):

```bash
claude mcp add --transport http nisar-db https://nisar-db.example.org/mcp \
    --header "X-API-Key: $NISAR_DB_KEY"
```

## The tools

| Tool | What it does |
|---|---|
| `list_datasets` | The datasets: `published` (North America) and rebuilt views such as `globe-...`, with their counts |
| `find_frames` | Frames matching the viewer's filters (track, frame, direction, bbox, cycle, dates, modes, polarizations, CRIDs, CalVal, land, rollout, consistent mode), one page at a time |
| `get_frame` | One frame's summary, by `8109` or `34_19` |
| `frame_granules` | A frame's GSLC granules, or its GUNW pairs |
| `frame_blackout` | A frame's blackout windows, the share of each month they cover, and its reference dates |
| `list_cycles` | Every cycle with its dates and frame count |
| `summarize` | Consistent-mode and rollout counts over filtered frames |
| `viewer_link` | A URL that opens the viewer in a given state |
| `list_job_kinds` | The `nisar-db` commands that run as jobs, with their parameters |
| `start_job` | Start one (CMR search, consistent-GSLC, blackout or reference dates, catalogs, ...) |
| `job_status` | A job's state, outputs and last log lines, or the recent jobs |
| `cancel_job` | Stop a job |
| `read_job_output` | Read a finished job's CSV / JSON output |

The server also gives the assistant short instructions: what a frame,
granule, pair and dataset are, and which tool to start with. You can just ask in
plain language.

## What the assistant may do

The tools can read the catalog and run `nisar-db` jobs. They cannot rebuild the
viewer, which is a crawl of all of CMR, or read files outside a job's folder.

| | Local (stdio, or `serve` in local mode) | Shared `serve` |
|---|---|---|
| Catalog tools | yes | any valid key |
| Job tools | yes | a key with the `jobs` scope |
| Whose jobs it sees | all | its key's own (`admin` sees all) |
| Downloads, S3 catalogs | yes | only with `--allow-job` |
| Job input files | any path | `catalog/`, rebuilt views, job outputs |

A refused call comes back to the assistant with the reason, for example "the
key 'reader' lacks the 'jobs' scope", so it can tell you what is missing.

## Options

`nisar-db mcp` takes:

| Option | Default | Meaning |
|---|---|---|
| `--cache-dir` | `.qa_helper_cache` | Where rebuilt views are read from and jobs run |
| `--viewer-html` | the checkout's page | The `published` dataset |
| `--viewer-url` | `http://127.0.0.1:8797` | Where viewer links point (a running `nisar-db serve`) |
| `--max-jobs` | 2 | Jobs running at once |

Over HTTP, links point to the server itself. Behind a proxy, give
`nisar-db serve` the address clients use with `--public-url`.
