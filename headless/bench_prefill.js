#!/usr/bin/env node
// Synthetic prefill benchmark. Generates prompts at varying sizes, prepends a
// fresh UUID nonce so the engine cannot serve from KV cache, and asks for
// max_tokens=1 so the wall time is dominated by prefill+overhead.
//
// Usage:  PROXY_KEY=... node bench_prefill.js [model]
// Default model is nemotron.

import fs from 'node:fs';
import crypto from 'node:crypto';

const PROXY  = process.env.PROXY_ENDPOINT || 'https://open-llm-proxy.nrp-nautilus.io/v1';
const MODEL  = process.argv[2] || 'nemotron';
const ORIGIN = process.env.ORIGIN || `https://benchmark.nrp-nautilus.io/prefill_${MODEL}`;
const KEY    = process.env.PROXY_KEY;
if (!KEY) { console.error('PROXY_KEY required'); process.exit(2); }

const SIZES   = [2000, 10000, 25000, 50000, 100000];
const TRIALS  = 3;
const MAX_TOK = 1;

// Approx 1 token ≈ 0.75 words for English. Pad up and let the tokenizer report.
const FILLER = 'The quick brown fox jumps over the lazy dog. Sphinx of black quartz, judge my vow. Pack my box with five dozen liquor jugs. ';
function makePrompt(targetTokens) {
    const wordsPerSentence = FILLER.split(/\s+/).filter(Boolean).length;
    const wordsNeeded = Math.ceil(targetTokens / 0.75);
    const repeats = Math.ceil(wordsNeeded / wordsPerSentence);
    const nonce = crypto.randomUUID();
    return `[unique-nonce ${nonce}] ${FILLER.repeat(repeats)}`;
}

async function bench(prompt) {
    const t0 = Date.now();
    let res, data;
    try {
        res = await fetch(`${PROXY}/chat/completions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${KEY}`,
                'Origin': ORIGIN,
            },
            body: JSON.stringify({
                model: MODEL,
                messages: [{ role: 'user', content: prompt }],
                max_tokens: MAX_TOK,
                temperature: 0,
            }),
            signal: AbortSignal.timeout(600_000),
        });
        data = await res.json();
    } catch (e) {
        return { ms: Date.now()-t0, ok: false, error: e.message };
    }
    return {
        ms: Date.now() - t0,
        ok: res.ok,
        status: res.status,
        usage: data.usage,
        error: data.error?.message,
    };
}

async function main() {
    console.log(`model: ${MODEL}   endpoint: ${PROXY}   origin: ${ORIGIN}`);
    console.log(`sizes: ${SIZES.join(', ')}   trials: ${TRIALS}   max_tokens: ${MAX_TOK}\n`);
    console.log(`${'target'.padStart(7)}  ${'trial'.padStart(5)}  ${'in_tok'.padStart(7)}  ${'out_tok'.padStart(7)}  ${'ms'.padStart(7)}  ${'prefill_tok/s'.padStart(13)}`);

    const results = [];
    // Warm-up: one small request to kick the endpoint
    await bench(makePrompt(500));

    for (const size of SIZES) {
        for (let t = 1; t <= TRIALS; t++) {
            const prompt = makePrompt(size);
            const r = await bench(prompt);
            const inTok = r.usage?.prompt_tokens;
            const outTok = r.usage?.completion_tokens;
            // Prefill rate ≈ input_tokens / (ms/1000), since output is 1 token (decode≈negligible)
            const rate = (typeof inTok === 'number' && r.ms > 0) ? inTok / (r.ms/1000) : null;
            console.log(
                `${String(size).padStart(7)}  ${String(t).padStart(5)}  ` +
                `${(inTok ?? '?').toString().padStart(7)}  ${(outTok ?? '?').toString().padStart(7)}  ` +
                `${String(r.ms).padStart(7)}  ${(rate?.toFixed(0) ?? 'n/a').padStart(13)}`
            );
            if (r.error) console.error(`  err: ${r.error}`);
            results.push({ target_size: size, trial: t, ...r, prefill_rate: rate });
        }
    }

    const out = `runs/bench_prefill_${MODEL}_${Date.now()}.json`;
    fs.writeFileSync(out, JSON.stringify({ model: MODEL, endpoint: PROXY, results }, null, 2));
    console.log(`\nResults → ${out}`);

    // Summary: avg prefill rate per size
    console.log('\nSummary (avg per target size):');
    for (const size of SIZES) {
        const rs = results.filter(r => r.target_size === size && r.prefill_rate);
        if (!rs.length) continue;
        const avgRate = rs.reduce((a,b) => a + b.prefill_rate, 0) / rs.length;
        const avgMs = rs.reduce((a,b) => a + b.ms, 0) / rs.length;
        const avgIn = rs.reduce((a,b) => a + (b.usage?.prompt_tokens || 0), 0) / rs.length;
        console.log(`  size~${size}: ${rs.length} runs, avg_in=${avgIn.toFixed(0)} tok, avg_ms=${avgMs.toFixed(0)}, avg_prefill=${avgRate.toFixed(0)} tok/s`);
    }
}

main().catch(e => { console.error(e); process.exit(1); });
