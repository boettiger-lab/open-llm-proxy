#!/usr/bin/env node
// Headless geo-agent runner — reproduces the browser app's tool-use loop for
// scripted model comparisons. Imports Agent / DatasetCatalog / ToolRegistry /
// createMapTools from ../../geo-agent/app/ and vendors mcp-client.js locally.

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { MCPClient } from './mcp-client.js';
import { StubMapManager } from './stub-map-manager.js';

// Resolve the geo-agent app dir. Defaults to the sibling checkout, but
// GEO_AGENT_DIR lets the runner point at an isolated, always-fresh-from-main
// copy (see fresh-geoagent.sh) so a headless run never depends on whatever
// branch or in-progress edits are live in a shared dev checkout that other
// agents may be using. Static import specifiers can't be parameterized, so
// these four app modules load via dynamic import (top-level await). Only
// mcp-client.js stays vendored locally (it has a bare specifier that must
// resolve against this package's node_modules — see check-drift.sh).
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const GEO_AGENT_DIR = process.env.GEO_AGENT_DIR
    ? path.resolve(process.env.GEO_AGENT_DIR)
    : path.resolve(__dirname, '../../geo-agent');
const geoApp = (m) => import(pathToFileURL(path.join(GEO_AGENT_DIR, 'app', m)).href);
const { Agent } = await geoApp('agent.js');
const { DatasetCatalog } = await geoApp('dataset-catalog.js');
const { ToolRegistry } = await geoApp('tool-registry.js');
const { createMapTools } = await geoApp('map-tools.js');

function parseArgs(argv) {
    const args = { _positional: [] };
    let i = 0;
    while (i < argv.length) {
        const a = argv[i];
        if (a.startsWith('--')) {
            const key = a.slice(2).replace(/-/g, '_');
            const next = argv[i + 1];
            if (next === undefined || next.startsWith('--')) {
                args[key] = true;
                i += 1;
            } else {
                args[key] = next;
                i += 2;
            }
        } else {
            args._positional.push(a);
            i += 1;
        }
    }
    args.question = args._positional[0];
    return args;
}

function usage() {
    console.error(`Usage: node run.js "QUESTION" --config PATH --system-prompt PATH [options]

Required:
  --config PATH            Path to app's layers-input.json
  --system-prompt PATH     Path to app's system-prompt.md

Options:
  --model NAME             Model name (default: config.llm_model or "qwen3")
  --origin URL             Origin header for proxy logging
  --proxy-endpoint URL     OpenAI-compatible LLM endpoint
                           (default: https://open-llm-proxy.nrp-nautilus.io/v1)
  --api-key KEY            Proxy key (or env PROXY_KEY / OPENAI_API_KEY)
  --max-turns N            Max tool-call iterations (default: 20)
  --mcp-url URL            Override MCP server URL (default: config.mcp_url)
  --transcript PATH        Write full transcript JSON to PATH
  --run-timeout N          Hard wall-clock cap in seconds; writes partial
                           transcript with timed_out=true and exits 124
  --llm-timeout N          Per-LLM-call budget in seconds (agent's own timeout,
                           = geo-agent llm_timeout_seconds). Default 600. Raise
                           for slow-decode reasoning models (glm-5/kimi ON).
                           Env: LLM_TIMEOUT_SECONDS.
  --trial N                Trial number (metadata only; stored in transcript)
  --quiet                  Suppress per-turn output; print only final answer
`);
}

// Wrap fetch to (1) inject Origin (Node omits it, the proxy logs by it),
// (2) time proxy fetches, (3) extract OpenAI-compatible `usage` from the JSON
// body (cloned so the original stream still goes to the agent), and (4) add a
// hard per-request AbortSignal.timeout — composed with any caller-provided
// signal — so a stuck undici socket can't wedge libuv timers indefinitely.
//
// perFetchTimeoutMs MUST sit ABOVE the agent's own per-attempt budget
// (_llmTimeoutMs, default 600s). If it fires first (the old 310s default did on
// slow-decode reasoning models), the agent sees a mid-stream `fetch failed`,
// classifies it as a transient *network* error, and retries on the tight 90s
// floor instead of its full budget — so a legitimately-slow call crashes the
// run (#61). The caller derives this from --llm-timeout; it stays a backstop
// against a truly wedged socket, not a cap on normal slow generation.
function installFetchWrapper(proxyEndpoint, origin, onProxyFetch, perFetchTimeoutMs = 660_000) {
    const proxyOrigin = new URL(proxyEndpoint).origin;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (input, init = {}) => {
        const url = typeof input === 'string' ? input : input?.url;
        const isProxy = url && url.startsWith(proxyOrigin);

        // Compose existing signal (if any) with a hard timeout — applies to
        // both proxy and MCP fetches; both can stall.
        const limitSignal = AbortSignal.timeout(perFetchTimeoutMs);
        init = {
            ...init,
            signal: init.signal ? AbortSignal.any([init.signal, limitSignal]) : limitSignal,
        };

        if (isProxy && origin) {
            const headers = new Headers(init.headers || {});
            headers.set('Origin', origin);
            init = { ...init, headers };
        }
        // Reasoning on/off assessment (geo-agent#282/#283): inject the proxy's
        // top-level `enable_thinking` flag when ENABLE_THINKING is set, so a run
        // can be driven reasoning-ON vs -OFF without touching the agent. The
        // proxy translates it per-model (qwen3/glm-5/kimi have a thinking_key).
        // Unset → omit the field → model default. Only applied to proxy calls.
        if (isProxy && process.env.ENABLE_THINKING !== undefined && init.body) {
            try {
                const b = JSON.parse(init.body);
                b.enable_thinking = process.env.ENABLE_THINKING === 'true';
                init = { ...init, body: JSON.stringify(b) };
            } catch { /* non-JSON body; leave as-is */ }
        }
        if (!isProxy) return originalFetch(input, init);
        const t0 = Date.now();
        try {
            const res = await originalFetch(input, init);
            const ms = Date.now() - t0;
            let usage = null;
            try {
                const data = await res.clone().json();
                usage = data?.usage || null;
            } catch { /* non-JSON or already consumed; skip */ }
            onProxyFetch({ ms, ok: res.ok, status: res.status, usage });
            return res;
        } catch (e) {
            onProxyFetch({ ms: Date.now() - t0, ok: false, error: e.message });
            throw e;
        }
    };
}

function indent(text, prefix) {
    return String(text).split('\n').map(l => prefix + l).join('\n');
}

async function main() {
    const args = parseArgs(process.argv.slice(2));
    if (!args.question || !args.config || !args.system_prompt) {
        usage();
        process.exit(2);
    }

    const config = JSON.parse(await fs.readFile(path.resolve(args.config), 'utf8'));
    const baseSystemPrompt = await fs.readFile(path.resolve(args.system_prompt), 'utf8');

    const proxyEndpoint = args.proxy_endpoint || 'https://open-llm-proxy.nrp-nautilus.io/v1';
    const apiKey = args.api_key || process.env.PROXY_KEY || process.env.OPENAI_API_KEY;
    if (!apiKey) {
        console.error('ERROR: no proxy API key. Set PROXY_KEY env var or pass --api-key.');
        process.exit(2);
    }

    const model = args.model || config.llm_model || 'qwen3';
    const maxTurns = Number(args.max_turns) || 20;
    const mcpUrl = args.mcp_url || config.mcp_url || 'https://duckdb-mcp.nrp-nautilus.io/mcp';
    const quiet = !!args.quiet;
    const runTimeoutSec = Number(args.run_timeout) || 0;
    const trial = args.trial ? Number(args.trial) : null;

    // Agent's own per-LLM-call budget (geo-agent llm_timeout_seconds); default
    // matches geo-agent's own 600s default. The fetch wrapper's hard cap is
    // derived to sit above it (+60s) so the agent's clean timeout+full-budget
    // retry governs, never the wrapper's 90s network-retry floor (#61).
    // PER_FETCH_TIMEOUT_MS overrides the derived value for a manual backstop.
    const llmTimeoutSec = Number(args.llm_timeout ?? process.env.LLM_TIMEOUT_SECONDS) || 600;
    const perFetchTimeoutMs = Number(process.env.PER_FETCH_TIMEOUT_MS) || (llmTimeoutSec * 1000 + 60_000);

    const transcript = {
        model,
        trial,
        question: args.question,
        origin: args.origin || null,
        proxy_endpoint: proxyEndpoint,
        mcp_url: mcpUrl,
        max_turns: maxTurns,
        started_at: new Date().toISOString(),
        turns: [],
        llm_fetches: [],
    };

    let transcriptWritten = false;
    async function writeTranscript(extra = {}) {
        if (!args.transcript || transcriptWritten) return;
        transcriptWritten = true;
        Object.assign(transcript, extra);
        if (!transcript.finished_at) transcript.finished_at = new Date().toISOString();
        transcript.tool_call_count = transcript.turns.filter(t => t.type === 'call').length;
        transcript.llm_total_ms = transcript.llm_fetches.reduce((s, f) => s + f.ms, 0);
        const fetchesWithUsage = transcript.llm_fetches.filter(f => f.usage);
        transcript.prompt_tokens_total = fetchesWithUsage.reduce((s, f) => s + (f.usage.prompt_tokens || 0), 0);
        transcript.completion_tokens_total = fetchesWithUsage.reduce((s, f) => s + (f.usage.completion_tokens || 0), 0);
        const outMs = fetchesWithUsage.reduce((s, f) => s + f.ms, 0);
        transcript.output_tok_per_sec = outMs > 0
            ? Number((transcript.completion_tokens_total / (outMs / 1000)).toFixed(2))
            : null;
        try {
            await fs.writeFile(path.resolve(args.transcript), JSON.stringify(transcript, null, 2));
            console.error(`[headless] transcript → ${args.transcript}`);
        } catch (e) {
            console.error(`[headless] transcript write failed: ${e.message}`);
        }
    }

    let runTimeoutHandle = null;
    if (runTimeoutSec > 0) {
        runTimeoutHandle = setTimeout(() => {
            console.error(`\n[headless] --run-timeout ${runTimeoutSec}s reached, writing partial transcript and exiting`);
            // Synchronously force-exit if the async write is itself blocked
            // by the same hang that triggered us. Best-effort persist first.
            writeTranscript({ timed_out: true, error: transcript.error || `run-timeout ${runTimeoutSec}s` })
                .finally(() => process.exit(124));
            // Hard backstop: if writeTranscript can't resolve in 5s, exit anyway.
            setTimeout(() => process.exit(124), 5000).unref();
        }, runTimeoutSec * 1000);
        // Don't unref — we want this timer to fire even if the loop is otherwise
        // idle. We clear it explicitly on successful completion below.
    }

    installFetchWrapper(proxyEndpoint, args.origin, (sample) => {
        transcript.llm_fetches.push(sample);
    }, perFetchTimeoutMs);

    const log = (msg) => { if (!quiet) console.log(msg); };
    const err = (msg) => console.error(msg);

    err(`[headless] geo-agent: ${GEO_AGENT_DIR}`);
    err(`[headless] STAC catalog: ${config.catalog}`);
    const catalog = new DatasetCatalog();
    await catalog.load(config);
    err(`[headless] loaded ${catalog.getAll().length} datasets`);

    err(`[headless] MCP: ${mcpUrl}`);
    const mcp = new MCPClient(mcpUrl);

    const toolRegistry = new ToolRegistry();
    const stubMapManager = new StubMapManager(catalog);
    for (const tool of createMapTools(stubMapManager, catalog, mcp)) {
        toolRegistry.registerLocal(tool);
    }

    // Mirror main.js:231 — refresh remote tools after any reconnect so a
    // mid-run MCP restart that adds/removes tools doesn't leave the registry stale.
    mcp.setOnReconnect((tools) => {
        toolRegistry.clearRemote();
        toolRegistry.registerRemote(tools, mcp);
        err(`[headless] Refreshed MCP tools after reconnect: ${tools.length} tools`);
    });

    await mcp.connect();
    const mcpTools = await mcp.listTools();
    err(`[headless] MCP tools: ${mcpTools.map(t => t.name).join(', ')}`);
    toolRegistry.registerRemote(mcpTools, mcp);

    let systemPrompt = baseSystemPrompt + '\n\n' + catalog.generatePromptCatalog();
    try {
        const prompts = await mcp.listPrompts();
        const analyst = prompts.find(p => p.name === 'geospatial-analyst');
        if (analyst) {
            const content = await mcp.getPrompt(analyst.name);
            if (content) {
                systemPrompt += '\n\n' + content;
                err('[headless] appended MCP geospatial-analyst prompt');
            }
        }
    } catch (e) {
        err(`[headless] no MCP geospatial-analyst prompt (${e.message})`);
    }

    const agent = new Agent({
        auto_approve: true,
        llm_model: model,
        // Top-level per-call budget; geo-agent's _llmTimeoutMs falls back to
        // this when a model config has no llm_timeout_seconds of its own.
        llm_timeout_seconds: llmTimeoutSec,
        llm_models: [{
            value: model,
            label: model,
            endpoint: proxyEndpoint,
            api_key: apiKey,
        }],
    }, toolRegistry);
    agent.setSystemPrompt(systemPrompt);
    agent.maxToolCalls = maxTurns;

    let turnExecStart = null;
    agent.onToolProposal = (calls, displayContent, turn) => {
        turnExecStart = Date.now();
        for (const tc of calls) {
            let argStr;
            try { argStr = JSON.stringify(JSON.parse(tc.function.arguments), null, 2); }
            catch { argStr = tc.function.arguments; }
            log(`\n[turn ${turn}] → ${tc.function.name}`);
            log(indent(argStr, '    '));
            transcript.turns.push({
                turn,
                type: 'call',
                name: tc.function.name,
                arguments: tc.function.arguments,
                ts: Date.now(),
            });
        }
        return { approved: true };
    };

    agent.onToolResults = (results, turn) => {
        const execMs = turnExecStart ? Date.now() - turnExecStart : null;
        for (const r of results) {
            const full = (r.result || '').toString();
            const preview = full.slice(0, 500);
            log(`\n[turn ${turn}] ← ${r.name} (${r.source})`);
            log(indent(preview + (full.length > 500 ? ' …' : ''), '    '));
            transcript.turns.push({
                turn,
                type: 'result',
                name: r.name,
                source: r.source,
                result_preview: preview,
                result_length: full.length,
                ts: Date.now(),
                tool_exec_ms: execMs,
            });
        }
        turnExecStart = null;
    };

    agent.onError = (e) => {
        err(`[headless] ERROR: ${e.message || e}`);
        transcript.error = e.message || String(e);
    };

    console.log('='.repeat(72));
    console.log(`Q: ${args.question}`);
    console.log(`model=${model}  origin=${args.origin || '(none)'}  max_turns=${maxTurns}`);
    console.log(`llm_timeout=${llmTimeoutSec}s  fetch_cap=${Math.round(perFetchTimeoutMs / 1000)}s`);
    console.log('='.repeat(72));

    const t0 = Date.now();
    let final;
    try {
        final = await agent.processMessage(args.question);
    } catch (e) {
        err(`\n[headless] agent crashed: ${e.stack || e.message}`);
        transcript.error = e.message || String(e);
        process.exitCode = 1;
    }
    const elapsed = (Date.now() - t0) / 1000;

    if (final?.response) {
        console.log('\n' + '─'.repeat(72));
        console.log('ANSWER:');
        console.log(final.response);
        console.log('─'.repeat(72));
    }
    if (final?.cancelled) console.log('[headless] run cancelled');

    if (runTimeoutHandle) clearTimeout(runTimeoutHandle);

    const toolCallCount = transcript.turns.filter(t => t.type === 'call').length;
    const llmTotalMs = transcript.llm_fetches.reduce((s, f) => s + f.ms, 0);
    const toolTotalMs = transcript.turns
        .filter(t => t.type === 'result' && t.tool_exec_ms != null)
        .reduce((s, t) => s + t.tool_exec_ms, 0);
    const promptTok = transcript.llm_fetches.reduce((s, f) => s + (f.usage?.prompt_tokens || 0), 0);
    const completionTok = transcript.llm_fetches.reduce((s, f) => s + (f.usage?.completion_tokens || 0), 0);
    const outTokPerSec = llmTotalMs > 0 ? (completionTok / (llmTotalMs / 1000)) : 0;
    console.log(`[headless] ${elapsed.toFixed(1)}s total  llm=${(llmTotalMs/1000).toFixed(1)}s  tool=${(toolTotalMs/1000).toFixed(1)}s  ${toolCallCount} tool calls  in=${promptTok}tok out=${completionTok}tok (${outTokPerSec.toFixed(1)} tok/s)`);

    await writeTranscript({
        elapsed_sec: Number(elapsed.toFixed(2)),
        llm_total_ms: llmTotalMs,
        tool_total_ms: toolTotalMs,
        response: final?.response || null,
        cancelled: !!final?.cancelled,
    });

    await mcp.disconnect();
}

main().catch(e => {
    console.error(`[headless] fatal: ${e.stack || e.message}`);
    process.exit(1);
});
