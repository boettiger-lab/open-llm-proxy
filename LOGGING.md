# LLM Proxy Logging

## Where logs live

Logs are written to **pod stdout** (via `print(..., flush=True)`). They are accessible via `kubectl` while the pod is running. Logs are lost on pod restart.

S3 persistence is not yet implemented.

## Access pattern

```bash
# Live tail
kubectl -n biodiversity logs deployment/open-llm-proxy -f

# Recent history (adjust --tail as needed)
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=500

# Filter to a specific app by origin
kubectl -n biodiversity logs deployment/open-llm-proxy --tail=1000 \
  | grep '"origin":"https://padus.nrp-nautilus.io"'

# All pods (if replicas > 1)
for pod in $(kubectl -n biodiversity get pods -l app=open-llm-proxy -o name); do
  kubectl -n biodiversity logs $pod --tail=500
done | sort
```

## Log format

Each LLM call produces two JSON entries on stdout: a `REQUEST` line when the call arrives and a `RESPONSE` line when it completes.

### REQUEST entry

```
📥 REQUEST: {"timestamp": "...", "type": "request", ...}
```

| Field | Description |
|---|---|
| `timestamp` | UTC ISO8601 |
| `type` | `"request"` |
| `request_id` | 8-char hex — correlates this request to its response |
| `provider` | `"nrp"`, `"openrouter"`, or `"nimbus"` |
| `model` | Model name as sent by the client |
| `origin` | Origin or Referer header — identifies which app sent the request |
| `message_count` | Total messages in the conversation at this turn |
| `tools_count` | Number of tools available to the LLM |
| `user_question` | First `role: user` message in the conversation — the human's original question, stable across all turns of a tool-use loop |
| `tool_results_this_turn` | Array of `{tool_call_id, content}` for any `role: tool` messages appended since the last assistant turn. Captures results from both local geo-agent tools (e.g. `list_datasets`, `get_dataset_details`) and remote MCP tools (e.g. `query`). `null` on the first turn. |

### RESPONSE entry

```
✓ RESPONSE: {"timestamp": "...", "type": "response", ...}
✗ RESPONSE: {"timestamp": "...", "type": "response", "error": "...", ...}
```

| Field | Description |
|---|---|
| `timestamp` | UTC ISO8601 |
| `type` | `"response"` |
| `request_id` | Matches the corresponding request entry |
| `provider` | Provider that handled the request |
| `model` | Model used |
| `origin` | Same as request — identifies which app |
| `latency_ms` | End-to-end latency in milliseconds |
| `has_content` | Whether the LLM returned text content |
| `has_tool_calls` | Whether the LLM made tool calls |
| `content_preview` | First 200 chars of text response |
| `tool_calls` | Array of `{name, arguments}` — full tool call arguments including SQL query strings |
| `tokens` | Token usage object from the provider (`prompt_tokens`, `completion_tokens`, `total_tokens`) |
| `error` | Error detail string (only present on failed requests) |

## Reconstructing a conversation

Each POST to `/v1/chat/completions` is one LLM turn. A single user session produces multiple request/response pairs. To reconstruct a session:

1. Match by `origin` to isolate one app
2. Group turns by `user_question` (same question = same session)
3. Sort by `timestamp`
4. Interleave: each request's `tool_results_this_turn` shows what the previous turn's tool calls returned; each response's `tool_calls` shows what the LLM decided to call next

Use `request_id` to pair each request with its response when log lines are interleaved under concurrent load.

## Example session reconstruction

```
REQUEST  msg=2  user_question="Tell me about datasets"  tool_results=null
RESPONSE tool_calls=[{name: list_datasets, arguments: {}}]

REQUEST  msg=4  user_question="Tell me about datasets"  tool_results=[{content: "[{id: pad-us...}]"}]
RESPONSE tool_calls=[{name: get_dataset_details, arguments: {dataset_id: "pad-us-4.1-fee"}}]

REQUEST  msg=6  user_question="Tell me about datasets"  tool_results=[{content: "{columns: [...]}"}]
RESPONSE has_content=true  content_preview="The PAD-US dataset contains..."
```

Note: `list_datasets` and `get_dataset_details` are local geo-agent tools — their results appear in `tool_results_this_turn` on the proxy but never reach the MCP server.

## What the MCP server logs add

The DuckDB MCP server logs SQL execution separately. MCP logs are only needed for:
- SQL execution errors not visible to the LLM (failed queries that return an error string)
- Exact query timing at the database layer

For conversation-level analysis (what users asked, what the LLM decided, what tools returned), proxy logs are self-sufficient.
