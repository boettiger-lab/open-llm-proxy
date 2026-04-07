#!/usr/bin/env python3
"""
agent_runner.py — Run a geo-agent-style agentic session against open-llm-proxy + MCP.

Reproduces what the browser-based geo-agent does: connects to an MCP server to get
tools, calls an OpenAI-compatible LLM proxy, and drives the tool-use loop to completion.
Useful for reproducing failures and testing model behavior without a browser.

Usage:
    python agent_runner.py "Rank states by fraction that is GAP 1+2 (terrestrial only)"

    python agent_runner.py "How many MPAs have IUCN category II?" \\
        --model nemotron \\
        --origin https://bosl-high-seas.nrp-nautilus.io \\
        --system-prompt /path/to/system-prompt.md

Dependencies:
    pip install openai mcp httpx
"""

import argparse
import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import httpx
from openai import AsyncOpenAI

try:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
except ImportError:
    print("ERROR: mcp package not found. Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_PROXY_URL = "https://open-llm-proxy.nrp-nautilus.io/v1"
DEFAULT_MCP_URL   = "https://duckdb-mcp.nrp-nautilus.io/mcp"
DEFAULT_MODEL     = "nemotron"
DEFAULT_MAX_TURNS = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def mcp_tool_to_openai(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


def load_system_prompt(path_or_url: str | None) -> str:
    if not path_or_url:
        return ""
    if path_or_url.startswith("http"):
        r = httpx.get(path_or_url, follow_redirects=True, timeout=10)
        r.raise_for_status()
        return r.text
    return Path(path_or_url).read_text()


def print_separator(label: str = "", width: int = 72):
    if label:
        pad = (width - len(label) - 2) // 2
        print(f"\n{'─' * pad} {label} {'─' * pad}")
    else:
        print("─" * width)


def print_tool_call(name: str, arguments: str, turn: int):
    try:
        args = json.loads(arguments)
    except Exception:
        args = arguments
    print(f"\n[Turn {turn}] TOOL CALL → {name}")
    if isinstance(args, dict) and "sql_query" in args:
        sql = args["sql_query"]
        print(f"  SQL:\n{textwrap.indent(sql, '    ')}")
    else:
        arg_str = json.dumps(args, indent=2) if isinstance(args, dict) else str(args)
        print(textwrap.indent(arg_str, "  "))


def print_tool_result(name: str, result: str, elapsed: float):
    preview = result[:500] + ("…" if len(result) > 500 else "")
    print(f"\n  RESULT ({elapsed:.1f}s):\n{textwrap.indent(preview, '    ')}")


# ── Main agent loop ───────────────────────────────────────────────────────────

async def run(args: argparse.Namespace):
    # Load system prompt
    system_prompt = load_system_prompt(args.system_prompt)

    # Build extra headers for the proxy (Origin is used by proxy for logging)
    extra_headers = {}
    if args.origin:
        extra_headers["Origin"] = args.origin

    llm = AsyncOpenAI(
        base_url=args.proxy_url,
        api_key=args.api_key or os.environ.get("OPENAI_API_KEY", "EMPTY"),
        default_headers=extra_headers,
    )

    print_separator("geo-agent session")
    print(f"  model      : {args.model}")
    print(f"  proxy      : {args.proxy_url}")
    print(f"  mcp        : {args.mcp_url}")
    print(f"  origin     : {args.origin or '(none)'}")
    print(f"  max turns  : {args.max_turns}")
    print(f"  question   : {args.question}")
    print_separator()

    async with streamablehttp_client(args.mcp_url) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()

            tools_result = await mcp.list_tools()
            openai_tools = [mcp_tool_to_openai(t) for t in tools_result.tools]
            print(f"MCP tools available: {[t.name for t in tools_result.tools]}")

            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": args.question})

            turn = 0
            session_start = time.monotonic()

            while turn < args.max_turns:
                t0 = time.monotonic()
                response = await llm.chat.completions.create(
                    model=args.model,
                    messages=messages,
                    tools=openai_tools or None,
                    tool_choice="auto" if openai_tools else None,
                )
                llm_elapsed = time.monotonic() - t0
                msg = response.choices[0].message

                if not msg.tool_calls:
                    # Final text response
                    print_separator("ANSWER")
                    print(msg.content or "(no content)")
                    print_separator()
                    total = time.monotonic() - session_start
                    print(f"Done in {turn} tool-call turns, {total:.1f}s total.")
                    return

                # Tool calls
                turn += 1
                messages.append(msg.model_dump(exclude_none=True))

                for tc in msg.tool_calls:
                    print_tool_call(tc.function.name, tc.function.arguments, turn)
                    t1 = time.monotonic()
                    result = await mcp.call_tool(
                        tc.function.name,
                        json.loads(tc.function.arguments),
                    )
                    elapsed = time.monotonic() - t1
                    content = result.content[0].text if result.content else ""
                    print_tool_result(tc.function.name, content, elapsed)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

            # Hit limit
            print_separator("MAX TURNS REACHED")
            print(f"Hit --max-turns={args.max_turns} without a final answer.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run a geo-agent-style session against open-llm-proxy + MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          # Reproduce the nemotron territory-ranking session
          python agent_runner.py "Rank states by fraction of the state that is gap 1+2 (terrestrial only)"

          # Use a different model with a specific app's system prompt
          python agent_runner.py "How many MPAs have IUCN category II?" \\
              --model anthropic/claude-sonnet-4-5 \\
              --origin https://bosl-high-seas.nrp-nautilus.io \\
              --system-prompt ~/repos/bosl-high-seas/system-prompt.md
        """),
    )
    parser.add_argument("question", help="The question to ask the agent")
    parser.add_argument("--model",         default=DEFAULT_MODEL,     help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--proxy-url",     default=DEFAULT_PROXY_URL, help="OpenAI-compatible proxy base URL")
    parser.add_argument("--api-key",       default=None,              help="API key (or set OPENAI_API_KEY env var)")
    parser.add_argument("--mcp-url",       default=DEFAULT_MCP_URL,   help="MCP server URL (Streamable HTTP)")
    parser.add_argument("--system-prompt", default=None,              help="Path or URL to system prompt (markdown)")
    parser.add_argument("--origin",        default=None,              help="Origin header for proxy logging (e.g. https://padus.nrp-nautilus.io)")
    parser.add_argument("--max-turns",     default=DEFAULT_MAX_TURNS, type=int, help=f"Max tool-call iterations (default: {DEFAULT_MAX_TURNS})")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
