# Fixed context cost of Blender MCP servers

Every MCP client sends the server's tool definitions (and any server instructions) to the model
before the first prompt, and again on every request after that. That cost is paid even when the
agent never touches Blender. This benchmark measures it.

Competitors measured on 2026-10-13, Frugal Blender MCP re-measured on 2026-10-25, Windows 11, same script, tokenized with tiktoken. Raw output: [tool-context.json](tool-context.json).

| Server | Tools | Definition tokens | Instruction tokens | Total (o200k) | vs. this repo |
|---|---:|---:|---:|---:|---:|
| **Frugal Blender MCP** (this repo) | **4** | 261 | 36 | **297** | 1x |
| [Blender Lab official](https://projects.blender.org/lab/blender_mcp) blender-mcp 1.0.1 | 26 | 3,487 | 1,235 | 4,722 | 15.9x |
| [ahujasid mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) 2.0.0 | 31 | 7,111 | 353 | 7,464 | 25.1x |

`cl100k_base` gives nearly identical numbers (289 / 4,651 / 7,393).

## What our design costs later

This server keeps operation details out of the tool list. The agent fetches them with `discover`
when it needs them, and those results enter the conversation too. Counting them:

| Discovery | Extra tokens | Running total | vs. official | vs. ahujasid |
|---|---:|---:|---:|---:|
| None (tool list only) | 0 | 297 | 15.9x less | 25.1x less |
| Operation summaries (`discover()`) | 288 | 585 | 8.1x less | 12.8x less |
| Summaries plus a typical scene's six schemas: primitive, material, assign_material, array, light, camera | 563 | 860 | 5.5x less | 8.7x less |
| Worst case: summaries plus every operation schema (22 operations) | 1,384 | 1,681 | 2.8x less | 4.4x less |

## How the definitions stay small

FastMCP generates JSON Schema from Python signatures, and that schema carries noise the model never
reads. Before listing tools, the server removes it:

| Removed | Example | Why it's safe |
|---|---|---|
| Generated titles | `"title": "Names"`, `"title": "inspectArguments"` | Names already identify each parameter. |
| `Optional[X] = None` unions | `"anyOf": [X, {"type": "null"}], "default": null` becomes `X` | Leaving the argument out already means `None`. |
| `additionalProperties: true` | on each `execute` step | It is the JSON Schema default. |
| Docstring indentation and blank lines | `"\n\n    dry_run checks..."` | Whitespace carries no meaning for the model. |

That took the four definitions from 406 to 289 tokens, even after adding capture's `engine`
argument. `tests/test_server.py` fails if they grow past 300 tokens or the noise comes back.

## Method

1. Launch each server over stdio with the official MCP Python SDK.
2. Call `initialize` and `tools/list`. No Blender instance is involved, and nothing is executed.
3. Serialize `[{name, description, inputSchema}]` as compact JSON and count tokens. Server
   `instructions` from `initialize` are counted separately.

Reproduce with [scripts/bench_tool_context.py](../scripts/bench_tool_context.py):

```sh
uv run python scripts/bench_tool_context.py servers.json --out docs/tool-context.json
```

where `servers.json` lists the commands to launch, for example:

```json
[
  {"label": "Frugal Blender MCP", "command": "uvx", "args": ["frugal-blender-mcp"]},
  {"label": "ahujasid mcp-for-blender", "command": "uvx", "args": ["mcp-for-blender"]},
  {"label": "Blender Lab official", "command": "uv", "args": ["run", "--directory", "/path/to/blender_mcp", "blender-mcp"]}
]
```

## What this does not show

- **Clients wrap definitions differently.** Exact counts vary by client and model tokenizer. The ratios
  are what matter.
- **Some clients load tools lazily.** Clients with tool search or deferred loading (for example
  Claude Code with many tools) only pay for the tools they load, which shrinks the gap. Clients that
  load every definition up front pay the full cost on every request.
- **Prompt caching lowers the price** of repeated definitions but not the context window they occupy.
- **Fewer tools are not automatically better.** The other servers offer things this one does not,
  such as documentation search (official) and asset-provider integrations (ahujasid). This benchmark
  measures one cost, not overall capability or output quality.
