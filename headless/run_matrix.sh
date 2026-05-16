#!/bin/bash
# run_matrix.sh — Run all (model × question × trial) combinations for an app.
#
# Usage:  run_matrix.sh APP_DIR
#   APP_DIR is the path to a geo-agent app repo (e.g. ../../tpl, ../../geo-agent-template).
#   Questions default to APP_DIR/layers-input.json welcome.examples (override with QUESTIONS_FILE env).
#   Models default to APP_DIR/k8s/configmap.yaml llm_models[].value (override with MODELS env).
#
# Env overrides:
#   TRIALS              number of trials per (model, question) — default 2
#   PER_RUN_TIMEOUT_SEC internal --run-timeout — default 900
#   MAX_TURNS           agent.maxToolCalls — default 20
#   ORIGIN              proxy log origin tag — default https://<APP_NAME>.nrp-nautilus.io/agent_runner
#   RUNS_DIR            output directory — default runs/<APP_NAME>
#   MODELS              space-separated override for the model list
#   QUESTIONS_FILE      path to a file with one question per line (blank lines and #-comments ignored)
set -u
cd "$(dirname "$0")"

APP_DIR="${1:-../../tpl}"
APP_DIR="${APP_DIR%/}"
APP_NAME="$(basename "$APP_DIR")"

CONFIG="$APP_DIR/layers-input.json"
SYSTEM_PROMPT="$APP_DIR/system-prompt.md"
CONFIGMAP="$APP_DIR/k8s/configmap.yaml"

if [ ! -s "$CONFIG" ] || [ ! -s "$SYSTEM_PROMPT" ]; then
    echo "ERROR: missing $CONFIG or $SYSTEM_PROMPT" >&2
    exit 2
fi
if [ ! -s /tmp/proxy_key ]; then
    echo "ERROR: /tmp/proxy_key missing or empty" >&2
    exit 2
fi
KEY="$(cat /tmp/proxy_key)"

# --- Resolve models ------------------------------------------------------
if [ -n "${MODELS:-}" ]; then
    read -ra MODEL_ARR <<< "$MODELS"
elif [ -s "$CONFIGMAP" ]; then
    mapfile -t MODEL_ARR < <(python3 -c "
import re, sys, yaml
try:
    docs = list(yaml.safe_load_all(open('$CONFIGMAP')))
except ImportError:
    docs = None
if docs:
    for d in docs:
        for v in (d or {}).get('data', {}).values():
            try:
                cfg = __import__('json').loads(v.replace(chr(0x24)+'{MCP_SERVER_URL}', 'x'))
            except Exception:
                continue
            for m in cfg.get('llm_models', []):
                if m.get('value'): print(m['value'])
else:
    text = open('$CONFIGMAP').read()
    in_models = False
    depth = 0
    for line in text.split(chr(10)):
        if 'llm_models' in line:
            in_models = True
            depth = 0
            continue
        if in_models:
            depth += line.count('[') - line.count(']')
            m = re.search(r'\"value\":\s*\"([^\"]+)\"', line)
            if m: print(m.group(1))
            if depth < 0: in_models = False
")
fi
if [ "${#MODEL_ARR[@]}" -eq 0 ]; then
    echo "ERROR: no models found (set MODELS env or provide $CONFIGMAP)" >&2
    exit 2
fi

# --- Resolve questions ---------------------------------------------------
if [ -n "${QUESTIONS_FILE:-}" ]; then
    if [ ! -s "$QUESTIONS_FILE" ]; then
        echo "ERROR: QUESTIONS_FILE=$QUESTIONS_FILE is missing or empty" >&2
        exit 2
    fi
    # Strip blank lines and # comments; keep order
    mapfile -t QUESTIONS < <(grep -vE '^[[:space:]]*(#|$)' "$QUESTIONS_FILE")
else
    mapfile -t QUESTIONS < <(python3 -c "
import json
d = json.load(open('$CONFIG'))
for q in d.get('welcome', {}).get('examples', []):
    print(q)
")
fi
if [ "${#QUESTIONS[@]}" -eq 0 ]; then
    echo "ERROR: no questions (set QUESTIONS_FILE or provide welcome.examples in $CONFIG)" >&2
    exit 2
fi

# --- Defaults ------------------------------------------------------------
TRIALS="${TRIALS:-2}"
PER_RUN_TIMEOUT_SEC="${PER_RUN_TIMEOUT_SEC:-900}"
BASH_TIMEOUT_SEC=$((PER_RUN_TIMEOUT_SEC + 60))
MAX_TURNS="${MAX_TURNS:-20}"
ORIGIN="${ORIGIN:-https://${APP_NAME}.nrp-nautilus.io/agent_runner}"
RUNS_DIR="${RUNS_DIR:-runs/${APP_NAME}}"
mkdir -p "$RUNS_DIR"

TOTAL=$(( ${#MODEL_ARR[@]} * ${#QUESTIONS[@]} * TRIALS ))
echo "=== matrix: app=${APP_NAME} models=${#MODEL_ARR[@]} questions=${#QUESTIONS[@]} trials=${TRIALS} → ${TOTAL} runs ==="
echo "models: ${MODEL_ARR[*]}"
echo "origin=${ORIGIN}  run-timeout=${PER_RUN_TIMEOUT_SEC}s/${BASH_TIMEOUT_SEC}s  max-turns=${MAX_TURNS}"
echo "runs:   ${RUNS_DIR}/"

START="$(date +%s)"

for trial in $(seq 1 "$TRIALS"); do
    for q_idx in "${!QUESTIONS[@]}"; do
        q="${QUESTIONS[$q_idx]}"
        qn="q$((q_idx+1))"
        for m in "${MODEL_ARR[@]}"; do
            msafe="${m//\//_}"
            name="${qn}__${msafe}__t${trial}"
            transcript="${RUNS_DIR}/${name}.json"
            log="${RUNS_DIR}/${name}.log"

            if [ -s "$transcript" ]; then
                echo "[$(date +%H:%M:%S)] SKIP    ${name}"
                continue
            fi

            echo "[$(date +%H:%M:%S)] START   ${name}"
            t0="$(date +%s)"
            # External wall-clock guard: detached nohup'd watchdog. Independent
            # of the parent shell's signal mask. After GNU `timeout(1)` was
            # observed escaping multiple times (runs of 1015s, 3071s, 3467s,
            # 27,441s past their cap), this approach is a clean SIGKILL by PID.
            (
                export PROXY_KEY="$KEY"
                node run.js "$q" \
                    --config "$CONFIG" \
                    --system-prompt "$SYSTEM_PROMPT" \
                    --model "$m" \
                    --origin "$ORIGIN" \
                    --max-turns "$MAX_TURNS" \
                    --run-timeout "$PER_RUN_TIMEOUT_SEC" \
                    --trial "$trial" \
                    --transcript "$transcript" \
                    --quiet &
                NODE_PID=$!
                nohup bash -c "
                    sleep $BASH_TIMEOUT_SEC
                    if kill -0 $NODE_PID 2>/dev/null; then
                        kill -TERM $NODE_PID 2>/dev/null
                        sleep 30
                        kill -KILL $NODE_PID 2>/dev/null
                    fi
                " </dev/null >/dev/null 2>&1 &
                WATCH_PID=$!
                disown $WATCH_PID
                wait $NODE_PID
                NODE_RC=$?
                kill -TERM $WATCH_PID 2>/dev/null
                exit $NODE_RC
            ) > "$log" 2>&1
            rc=$?
            dt=$(( $(date +%s) - t0 ))
            case $rc in
                0)   echo "[$(date +%H:%M:%S)] OK      ${name} (${dt}s)";;
                124) echo "[$(date +%H:%M:%S)] TIMEOUT ${name} (${dt}s)";;
                *)   echo "[$(date +%H:%M:%S)] FAIL    ${name} (rc=$rc, ${dt}s)";;
            esac
        done
    done
done

END="$(date +%s)"
echo ""
echo "=== matrix complete in $((END - START))s ==="

{
    printf 'trial\tquestion\tmodel\telapsed_s\tllm_s\ttool_s\ttool_calls\tprompt_tok\tcompletion_tok\ttok_per_sec\ttimed_out\tcancelled\terror\tanswer_len\n'
    for trial in $(seq 1 "$TRIALS"); do
        for q_idx in "${!QUESTIONS[@]}"; do
            qn="q$((q_idx+1))"
            for m in "${MODEL_ARR[@]}"; do
                msafe="${m//\//_}"
                f="${RUNS_DIR}/${qn}__${msafe}__t${trial}.json"
                if [ ! -s "$f" ]; then
                    printf '%d\t%s\t%s\t\t\t\t\t\t\t\t\t\tno_transcript\t\n' "$trial" "$qn" "$m"
                    continue
                fi
                python3 -c "
import json
d=json.load(open('$f'))
err=(d.get('error') or '').replace(chr(9),' ').replace(chr(10),' ')[:100]
print('\t'.join([
    '$trial', '$qn', '$m',
    str(d.get('elapsed_sec','')),
    '%.2f' % (d.get('llm_total_ms',0)/1000),
    '%.2f' % (d.get('tool_total_ms',0)/1000),
    str(d.get('tool_call_count','')),
    str(d.get('prompt_tokens_total') or ''),
    str(d.get('completion_tokens_total') or ''),
    str(d.get('output_tok_per_sec') or ''),
    str(d.get('timed_out') or False),
    str(d.get('cancelled') or False),
    err,
    str(len(d.get('response') or '')),
]))
"
            done
        done
    done
} | tee "${RUNS_DIR}/summary.tsv"

echo ""
echo "transcripts:  ${RUNS_DIR}/qN__model__tK.json"
echo "per-run logs: ${RUNS_DIR}/qN__model__tK.log"
echo "summary TSV:  ${RUNS_DIR}/summary.tsv"
