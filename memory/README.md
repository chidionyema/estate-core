# Memory Substrate

Temporal knowledge graph for agent memory using Graphiti + Neo4j.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Memory Substrate                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐              │
│  │  A2M REST    │    │  MCP Server  │    │   Dream      │              │
│  │  (port 8787) │    │  (stdio)     │    │   (cron)     │              │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘              │
│         │                   │                   │                       │
│         └───────────────────┼───────────────────┘                       │
│                             │                                           │
│                      ┌──────▼───────┐                                   │
│                      │   Graphiti   │                                   │
│                      │   (Python)   │                                   │
│                      └──────┬───────┘                                   │
│                             │                                           │
│                      ┌──────▼───────┐                                   │
│                      │    Neo4j     │                                   │
│                      │   (Docker)   │                                   │
│                      └──────────────┘                                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start Neo4j

```bash
docker run -d \
  --name memory-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  neo4j:5
```

### 2. Set OpenAI API Key

Graphiti uses OpenAI for entity extraction and embeddings:

```bash
export OPENAI_API_KEY=your-key
```

### 3. Start A2M Server

```bash
./bin/memory-a2m
```

### 4. Write and Query

```bash
# Write memory
curl -X POST http://localhost:8787/memory \
  -H "Content-Type: application/json" \
  -d '{"content": "User prefers dark mode in all IDEs", "source": "observation"}'

# Query memory
curl -X POST http://localhost:8787/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "IDE preferences", "top_k": 3}'

# Ambient recall (compressed hints)
curl "http://localhost:8787/ambient?query=IDE+settings&top_k=3"
```

## Protocols

### A2M (Agent-to-Memory Protocol)

Vendor-neutral REST interface. Any agent framework can use it.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/memory` | POST | Store an episode |
| `/recall` | POST | Query the graph |
| `/ambient` | GET | Get compressed hints |
| `/dream` | POST | Trigger consolidation |
| `/health` | GET | Health check |

### MCP Server

Model Context Protocol for Claude Code integration.

```bash
./bin/memory-mcp  # stdio JSON-RPC
```

Tools:
- `memory_write`: Store an episode
- `memory_recall`: Query the graph
- `memory_ambient`: Get compressed hints

## Memory Operations

### Write

Episodes have bi-temporal validity:
- `valid_from`: When the fact became true
- `expired_at`: When the fact stopped being true (null = still valid)

```python
{
    "content": "The API endpoint is /v2/users",
    "source": "agent-session-123",
    "episode_type": "observation",  # message, action, observation, reflection
    "reference_time": "2024-01-15T10:00:00Z"  # optional
}
```

### Recall

Hybrid search: semantic + BM25 + graph traversal.
No query-time LLM calls - sub-second retrieval.

```python
{
    "query": "API endpoints",
    "top_k": 5,
    "center_node_uuid": "optional-uuid"  # for graph-centric search
}
```

### Ambient

Conservative injection pattern:
- Top 3 hits
- 8-12 words per hit
- File path + offsets for navigation

The agent isn't intentionally searching - memory surfaces automatically.

## Dream (Consolidation)

Background process that runs on schedule (not during agent turns):

### Merge
Newer memory contains all of older + more → older marked as merged, hidden from search.

### Supersede
Newer replaces older fact → older marked as superseded, still searchable for history.

### Synthesize
Related observations → new summary memory with source IDs.

Run manually:
```bash
./bin/memory-dream --dry-run  # Preview
./bin/memory-dream --run       # Execute
```

Add to cron:
```bash
0 3 * * * /path/to/bin/memory-dream --run >> /var/log/dream.log 2>&1
```

## Configuration

Edit `.memory/config.json`:

```json
{
  "neo4j": {
    "uri": "bolt://localhost:7687",
    "user": "neo4j",
    "password": "password"
  },
  "openai": {
    "model": "text-embedding-3-small"
  },
  "ambient": {
    "top_k": 3,
    "max_words": 12
  },
  "dream": {
    "schedule": "0 3 * * *",
    "merge_threshold": 0.85,
    "synthesize_min_observations": 5
  }
}
```

## Integration with growmos

Memory substrate complements growmos knowledge graph:

| | growmos | Memory Substrate |
|---|---------|------------------|
| **Data** | Project structure, entities, relations | Episodes, temporal facts, observations |
| **Scope** | This codebase | Any context (user, sessions, external) |
| **Query** | Graph traversal, symbol lookup | Semantic search, bi-temporal validity |
| **Update** | Automatic (file watch) | Explicit (API calls) |

Both can be queried through ambient recall for comprehensive context.

## Files

```
.memory/
├── .venv/              # Python 3.12 virtualenv
├── config.json         # Configuration
├── a2m_server.py       # A2M REST server
├── mcp_server.py       # MCP stdio server
├── dream.py            # Consolidation script
└── README.md           # This file

bin/
├── memory-mcp          # Launch MCP server
├── memory-a2m          # Launch A2M server
└── memory-dream        # Run consolidation
```

## Dependencies

- Python 3.12+
- Neo4j 5.x
- graphiti-core
- fastapi, uvicorn (A2M server)
- OpenAI API key (for embeddings)
