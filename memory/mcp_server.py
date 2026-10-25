#!/usr/bin/env python3
"""
Simple JSON-RPC MCP Server for Graphiti Temporal Memory.

Uses stdin/stdout JSON-RPC protocol without the mcp library dependency.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Graphiti
from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

# Config
CONFIG_PATH = Path(__file__).parent / "config.json"
config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

NEO4J_URI = os.getenv("NEO4J_URI", config.get("neo4j", {}).get("uri", "bolt://localhost:7687"))
NEO4J_USER = os.getenv("NEO4J_USER", config.get("neo4j", {}).get("user", "neo4j"))
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", config.get("neo4j", {}).get("password", "password"))

# Global
graphiti: Optional[Graphiti] = None


async def get_graphiti() -> Graphiti:
    """Get or create Graphiti client."""
    global graphiti
    if graphiti is None:
        graphiti = Graphiti(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        await graphiti.build_indices_and_constraints()
    return graphiti


TOOLS = [
    {
        "name": "memory_write",
        "description": "Store an episode in temporal memory. Use for facts, observations, decisions worth remembering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The content to remember"},
                "source": {"type": "string", "description": "Source identifier", "default": "agent"},
                "episode_type": {"type": "string", "enum": ["message", "action", "observation", "reflection"], "default": "observation"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_recall",
        "description": "Query temporal memory. Returns relevant facts with bi-temporal validity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "top_k": {"type": "integer", "description": "Number of results", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_ambient",
        "description": "Get compressed ambient hints. Returns 8-12 word summaries for quick context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {"type": "string", "description": "Current context to find relevant memories"},
            },
            "required": ["context"],
        },
    },
]


async def handle_tool_call(name: str, arguments: dict) -> str:
    """Handle tool invocation."""
    try:
        g = await get_graphiti()

        if name == "memory_write":
            content = arguments.get("content", "")
            source = arguments.get("source", "agent")
            ep_type_str = arguments.get("episode_type", "observation")

            episode_types = {
                "message": EpisodeType.message,
                "action": EpisodeType.action,
                "observation": EpisodeType.json,
                "reflection": EpisodeType.text,
            }
            ep_type = episode_types.get(ep_type_str, EpisodeType.message)
            ref_time = datetime.now(timezone.utc)

            await g.add_episode(
                name=f"{source}:{ref_time.isoformat()}",
                episode_body=content,
                source_description=source,
                reference_time=ref_time,
                episode_type=ep_type,
            )

            return f"Stored: {content[:100]}..."

        elif name == "memory_recall":
            query = arguments.get("query", "")
            top_k = arguments.get("top_k", 5)

            results = await g.search(query=query, num_results=top_k)

            output = []
            for edge in results:
                fact = edge.fact if hasattr(edge, 'fact') else str(edge)
                output.append(f"• {fact}")

            return "\n".join(output) if output else "No relevant memories found."

        elif name == "memory_ambient":
            context = arguments.get("context", "")

            results = await g.search(query=context, num_results=3)

            hints = []
            for edge in results:
                fact = edge.fact if hasattr(edge, 'fact') else str(edge)
                words = fact.split()[:12]
                compressed = " ".join(words)
                if len(words) == 12:
                    compressed += "..."
                hints.append(compressed)

            return "[ambient] " + " | ".join(hints) if hints else ""

        return f"Unknown tool: {name}"

    except Exception as e:
        return f"Error: {e}"


def send_response(id, result=None, error=None):
    """Send JSON-RPC response."""
    resp = {"jsonrpc": "2.0", "id": id}
    if error:
        resp["error"] = {"code": -32000, "message": str(error)}
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


async def handle_request(request: dict):
    """Handle incoming JSON-RPC request."""
    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        send_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "graphiti-memory", "version": "1.0.0"},
        })

    elif method == "tools/list":
        send_response(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = await handle_tool_call(tool_name, arguments)
        send_response(req_id, {
            "content": [{"type": "text", "text": result}],
            "isError": False,
        })

    elif method == "notifications/initialized":
        # No response needed for notifications
        pass

    else:
        send_response(req_id, error=f"Unknown method: {method}")


async def main():
    """Run the MCP server."""
    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            request = json.loads(line.strip())
            await handle_request(request)

        except json.JSONDecodeError:
            continue
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    asyncio.run(main())
