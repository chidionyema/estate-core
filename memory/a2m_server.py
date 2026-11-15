#!/usr/bin/env python3
"""
A2M (Agent-to-Memory Protocol) REST server.

Thin protocol layer over Graphiti temporal knowledge graph.
Any agent framework can read/write through this single REST interface.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add parent for imports
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

# Graphiti imports
from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

# Config
CONFIG_PATH = Path(__file__).parent / "config.json"
config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}

NEO4J_URI = os.getenv("NEO4J_URI", config.get("neo4j", {}).get("uri", "bolt://localhost:7687"))
NEO4J_USER = os.getenv("NEO4J_USER", config.get("neo4j", {}).get("user", "neo4j"))
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", config.get("neo4j", {}).get("password", "password"))

# Global client
graphiti: Optional[Graphiti] = None


# === Models ===

class MemoryWrite(BaseModel):
    """Write an episode to the temporal knowledge graph."""
    content: str = Field(..., description="The content to remember")
    source: str = Field(default="agent", description="Source identifier (agent name, session id)")
    episode_type: str = Field(default="message", description="message, action, observation, reflection")
    reference_time: Optional[str] = Field(default=None, description="ISO timestamp for bi-temporal validity")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


class MemoryQuery(BaseModel):
    """Query the temporal knowledge graph."""
    query: str = Field(..., description="Natural language query")
    top_k: int = Field(default=3, description="Number of results")
    center_node_uuid: Optional[str] = Field(default=None, description="Optional center node for graph traversal")


class MemoryRecall(BaseModel):
    """Ambient recall result."""
    content: str
    relevance: float
    source: str
    timestamp: str
    file_path: Optional[str] = None
    line_offset: Optional[int] = None


class DreamOperation(BaseModel):
    """Background consolidation operation."""
    operation: str = Field(..., description="merge, supersede, or synthesize")
    source_ids: list[str] = Field(default_factory=list)
    target_id: Optional[str] = None
    reason: Optional[str] = None


# === Lifespan ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    global graphiti
    try:
        graphiti = Graphiti(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        await graphiti.build_indices_and_constraints()
        yield
    finally:
        if graphiti:
            await graphiti.close()


# === App ===

app = FastAPI(
    title="A2M - Agent to Memory Protocol",
    description="Vendor-neutral REST interface for temporal agent memory",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok", "graphiti": graphiti is not None}


@app.post("/memory")
async def write_memory(mem: MemoryWrite):
    """
    Write an episode to the temporal knowledge graph.

    Episodes are discrete units of information with bi-temporal validity:
    - valid_from: when the fact became true
    - expired_at: when the fact stopped being true (null = still valid)
    """
    if not graphiti:
        raise HTTPException(status_code=503, detail="Graphiti not initialized")

    # Map episode type
    episode_types = {
        "message": EpisodeType.message,
        "action": EpisodeType.action,
        "observation": EpisodeType.json,
        "reflection": EpisodeType.text,
    }
    ep_type = episode_types.get(mem.episode_type, EpisodeType.message)

    # Parse reference time
    ref_time = None
    if mem.reference_time:
        ref_time = datetime.fromisoformat(mem.reference_time.replace("Z", "+00:00"))
    else:
        ref_time = datetime.now(timezone.utc)

    # Add episode
    await graphiti.add_episode(
        name=f"{mem.source}:{ref_time.isoformat()}",
        episode_body=mem.content,
        source_description=mem.source,
        reference_time=ref_time,
        episode_type=ep_type,
    )

    return {"status": "ok", "timestamp": ref_time.isoformat()}


@app.post("/recall", response_model=list[MemoryRecall])
async def recall_memory(query: MemoryQuery):
    """
    Query the temporal knowledge graph.

    Uses hybrid search: semantic + BM25 + graph traversal.
    No query-time LLM calls - sub-second retrieval.
    """
    if not graphiti:
        raise HTTPException(status_code=503, detail="Graphiti not initialized")

    # Search
    results = await graphiti.search(
        query=query.query,
        num_results=query.top_k,
        center_node_uuid=query.center_node_uuid,
    )

    # Format results
    recalls = []
    for edge in results:
        recalls.append(MemoryRecall(
            content=edge.fact if hasattr(edge, 'fact') else str(edge),
            relevance=edge.score if hasattr(edge, 'score') else 1.0,
            source=edge.source_description if hasattr(edge, 'source_description') else "unknown",
            timestamp=edge.created_at.isoformat() if hasattr(edge, 'created_at') else datetime.now(timezone.utc).isoformat(),
            file_path=edge.metadata.get("file_path") if hasattr(edge, 'metadata') else None,
            line_offset=edge.metadata.get("line_offset") if hasattr(edge, 'metadata') else None,
        ))

    return recalls


@app.get("/ambient")
async def ambient_recall(query: str, top_k: int = 3, max_words: int = 12):
    """
    Ambient associative memory pattern.

    Called on every tool call. Returns compressed hints:
    - Top 3 hits
    - 8-12 words per hit
    - File path + offsets for navigation

    Conservative injection on purpose.
    """
    if not graphiti:
        return {"hints": [], "status": "graphiti_unavailable"}

    try:
        results = await graphiti.search(query=query, num_results=top_k)

        hints = []
        for edge in results:
            content = edge.fact if hasattr(edge, 'fact') else str(edge)
            # Compress to max_words
            words = content.split()[:max_words]
            compressed = " ".join(words)
            if len(words) == max_words:
                compressed += "..."

            hint = {"text": compressed}
            if hasattr(edge, 'metadata'):
                if edge.metadata.get("file_path"):
                    hint["path"] = edge.metadata["file_path"]
                if edge.metadata.get("line_offset"):
                    hint["line"] = edge.metadata["line_offset"]

            hints.append(hint)

        return {"hints": hints, "status": "ok"}

    except Exception as e:
        return {"hints": [], "status": f"error: {e}"}


@app.post("/dream")
async def dream_consolidation(op: DreamOperation):
    """
    Background consolidation (Dream pattern).

    Three operations:
    - merge: newer memory contains all of older + more → older marked as merged
    - supersede: newer replaces older fact → older marked as superseded
    - synthesize: related observations → new summary memory

    Runs on schedule (cron), not during agent turns.
    """
    if not graphiti:
        raise HTTPException(status_code=503, detail="Graphiti not initialized")

    # Dream operations would manipulate the graph
    # For now, log the intent - full implementation needs graph mutation APIs
    return {
        "status": "queued",
        "operation": op.operation,
        "source_count": len(op.source_ids),
        "reason": op.reason,
    }


@app.get("/stats")
async def get_stats():
    """Get memory substrate statistics."""
    if not graphiti:
        return {"status": "graphiti_unavailable"}

    # Would query neo4j for counts
    return {
        "status": "ok",
        "store": "graphiti",
        "backend": "neo4j",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("A2M_PORT", "8787"))
    uvicorn.run(app, host="0.0.0.0", port=port)
