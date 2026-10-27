"""The fixed context cost is the product. A new feature that raises it must be a conscious decision."""

import asyncio
import json

import tiktoken

from compact_mcp.server import mcp

LIMIT = 297


def test_fixed_context_stays_within_budget():
    tools = asyncio.run(mcp.list_tools())
    definitions = [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools
    ]
    text = json.dumps(definitions, separators=(",", ":"), ensure_ascii=False)
    encoding = tiktoken.get_encoding("o200k_base")
    total = len(encoding.encode(text)) + len(encoding.encode(mcp.instructions))
    assert len(tools) == 4
    assert total <= LIMIT, f"Fixed context is {total} tokens; the budget is {LIMIT}"
