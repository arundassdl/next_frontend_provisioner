#!/usr/bin/env bash
# install_agent_extension.sh
# ---------------------------
# Run this on the AGENT SERVER to install the Next.js job classes
# into the frappe/agent Python package.
#
# Usage:
#   sudo bash install_agent_extension.sh [AGENT_REPO_PATH]
#
# Default agent repo path: /var/frappe/agent/repo
#
# What it does:
#   1. Copies nextjs_jobs.py and nginx_utils.py into agent/
#   2. Patches agent/job.py to import and register the three job classes
#   3. Restarts the agent:web supervisord process

set -euo pipefail

AGENT_REPO="${1:-/var/frappe/agent/repo}"
AGENT_PKG="${AGENT_REPO}/agent"

echo "==> Agent repo: ${AGENT_REPO}"

# ── 1. Copy job module ────────────────────────────────────────────────
echo "==> Copying nextjs_jobs.py → ${AGENT_PKG}/nextjs_jobs.py"
cp "$(dirname "$0")/agent_extension/agent_jobs.py" "${AGENT_PKG}/nextjs_jobs.py"

# ── 2. Copy nginx_utils (agent-side version) ─────────────────────────
echo "==> Copying nginx_utils.py → ${AGENT_PKG}/nginx_utils.py"
cp "$(dirname "$0")/agent_extension/nginx_utils.py" "${AGENT_PKG}/nginx_utils.py"

# ── 3. Patch agent/job.py ─────────────────────────────────────────────
JOB_PY="${AGENT_PKG}/job.py"

IMPORT_BLOCK='from agent.nextjs_jobs import (
    ProvisionNextjsSiteJob,
    TeardownNextjsSiteJob,
    RedeployNextjsSiteJob,
)'

REGISTRATION_BLOCK='    "Provision Next.js Site": ProvisionNextjsSiteJob,
    "Teardown Next.js Site":  TeardownNextjsSiteJob,
    "Redeploy Next.js Site":  RedeployNextjsSiteJob,'

# Only patch if not already patched
if grep -q "nextjs_jobs" "${JOB_PY}"; then
    echo "==> agent/job.py already patched — skipping import injection"
else
    echo "==> Patching ${JOB_PY} — injecting import"
    # Insert import after the last existing 'from agent.' import line
    python3 - "${JOB_PY}" "${IMPORT_BLOCK}" << 'PYEOF'
import sys, re

path = sys.argv[1]
block = sys.argv[2]

text = open(path).read()

# Find position after last "from agent." import
matches = list(re.finditer(r'^from agent\.\S+.*$', text, re.MULTILINE))
if not matches:
    print("ERROR: Could not find any 'from agent.' imports in job.py", file=sys.stderr)
    sys.exit(1)

insert_pos = matches[-1].end()
new_text = text[:insert_pos] + "\n" + block + text[insert_pos:]
open(path, "w").write(new_text)
print("Import injected after:", matches[-1].group())
PYEOF
fi

# Inject job class registrations into JOB_CLASSES dict
if grep -q "Provision Next.js Site" "${JOB_PY}"; then
    echo "==> JOB_CLASSES already has Next.js entries — skipping registration injection"
else
    echo "==> Patching ${JOB_PY} — registering job classes in JOB_CLASSES"
    python3 - "${JOB_PY}" "${REGISTRATION_BLOCK}" << 'PYEOF'
import sys, re

path = sys.argv[1]
block = sys.argv[2]

text = open(path).read()

# Find the closing } of JOB_CLASSES = { ... }
# Strategy: find JOB_CLASSES = { then find its closing }
match = re.search(r'JOB_CLASSES\s*=\s*\{', text)
if not match:
    print("ERROR: JOB_CLASSES dict not found in job.py", file=sys.stderr)
    sys.exit(1)

# Walk forward from match to find the closing brace
start = match.end()
depth = 1
i = start
while i < len(text) and depth > 0:
    if text[i] == '{':
        depth += 1
    elif text[i] == '}':
        depth -= 1
    i += 1

closing_brace = i - 1  # position of the final }
new_text = text[:closing_brace] + "\n" + block + "\n" + text[closing_brace:]
open(path, "w").write(new_text)
print("Job classes registered in JOB_CLASSES")
PYEOF
fi

# ── 4. Restart agent:web ──────────────────────────────────────────────
echo "==> Restarting agent:web via supervisorctl"
supervisorctl restart agent:web

echo ""
echo "✓ Done. Verify with: supervisorctl status agent:web"
