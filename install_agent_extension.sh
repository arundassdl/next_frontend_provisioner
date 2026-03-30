#!/usr/bin/env bash
# install_agent_extension.sh
# ---------------------------
# Run on the AGENT SERVER to wire in Next.js frontend deployment support.
# Usage:  sudo bash install_agent_extension.sh [AGENT_REPO_PATH]
# Default: /var/frappe/agent/repo
#
# What this script does:
#   1. Copies nginx_utils.py, template_injector.py, frontend_patch.py
#      into the agent package — no core files are replaced.
#   2. Adds ONE import line to web.py:
#         import agent.frontend_patch  # noqa: F401
#      This import triggers _register_routes() which attaches:
#         POST   /frontends/<name>/deploy
#         DELETE /frontends/<name>
#      to the existing Flask application at startup.
#   3. Verifies the import works cleanly.
#   4. Restarts agent:web via supervisorctl.
#
# Idempotent — safe to run multiple times.

set -euo pipefail

AGENT_REPO="${1:-/var/frappe/agent/repo}"
AGENT_PKG="${AGENT_REPO}/agent"
WEB_PY="${AGENT_PKG}/web.py"
NFP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Agent repo : ${AGENT_REPO}"
echo "==> NFP dir    : ${NFP_DIR}"
echo ""

# ── 1. Undo old mixin approach (if previously applied) ────────────────
# The old approach patched server.py and job.py with NextjsMixin.
# That caused circular imports and is no longer used.
SERVER_PY="${AGENT_PKG}/server.py"
JOB_PY="${AGENT_PKG}/job.py"

echo "==> [1/4] Cleaning up any previous mixin-based installation"
python3 - "${SERVER_PY}" "${JOB_PY}" << 'PYEOF'
import re, sys

server_path, job_path = sys.argv[1], sys.argv[2]

# Clean server.py — remove NextjsMixin import and revert class signature
server = open(server_path).read()
original_server = server
server = re.sub(r'\nfrom agent\.nextjs_jobs import NextjsMixin\n', '\n', server)
server = re.sub(r'\bclass Server\(NextjsMixin,\s*Base\)', 'class Server(Base)', server)
if server != original_server:
    open(server_path, 'w').write(server)
    print("  Cleaned server.py (removed NextjsMixin)")
else:
    print("  server.py already clean")

# Clean job.py — remove any nextjs_jobs imports (caused circular import)
job = open(job_path).read()
original_job = job
job = re.sub(r'\nfrom agent\.nextjs_jobs import[^\n]*\n', '\n', job)
if job != original_job:
    open(job_path, 'w').write(job)
    print("  Cleaned job.py (removed stale nextjs_jobs import)")
else:
    print("  job.py already clean")
PYEOF

# ── 2. Copy extension files ───────────────────────────────────────────
echo ""
echo "==> [2/4] Copying extension files into agent package"

for f in nginx_utils.py template_injector.py frontend_patch.py; do
    src="${NFP_DIR}/agent_extension/${f}"
    dst="${AGENT_PKG}/${f}"
    if [ -f "${src}" ]; then
        cp "${src}" "${dst}"
        echo "  -> ${f}"
    else
        echo "  WARNING: ${src} not found — skipping"
    fi
done
echo "  NOTE: web.py, server.py, job.py are NOT replaced"

# ── 3. Inject single import line into web.py ─────────────────────────
echo ""
echo "==> [3/4] Injecting import into web.py"

IMPORT_LINE='import agent.frontend_patch  # noqa: F401 — registers /frontends routes'

if grep -qF "frontend_patch" "${WEB_PY}"; then
    echo "  -> already present — skipping"
else
    # Insert after the last 'from agent.' import line
    LAST_LINE=$(grep -n "^from agent\." "${WEB_PY}" | tail -1 | cut -d: -f1)
    if [ -n "${LAST_LINE}" ]; then
        sed -i "${LAST_LINE}a ${IMPORT_LINE}" "${WEB_PY}"
        echo "  -> inserted at line $((LAST_LINE + 1)) of web.py"
    else
        echo "${IMPORT_LINE}" >> "${WEB_PY}"
        echo "  -> appended to end of web.py"
    fi
fi

# Show the diff so it's clear exactly what changed
echo ""
echo "  web.py change:"
grep -n "frontend_patch" "${WEB_PY}" | sed 's/^/    /'

# ── 4. Verify and restart ─────────────────────────────────────────────
echo ""
echo "==> [4/4] Verifying import and restarting agent:web"

cd "${AGENT_REPO}"
/home/frappe/agent/env/bin/python -c "
import agent.frontend_patch
print('  ✓ Import OK — /frontends routes registered')
" || {
    echo ""
    echo "  ERROR: Import failed. The error above shows what went wrong."
    echo "  Fix the issue then re-run this script."
    exit 1
}

supervisorctl restart agent:web
sleep 2
supervisorctl status agent:web

echo ""
echo "✓ Installation complete."
echo "  Monitor deployments : tail -f ${AGENT_REPO}/../logs/worker.log"
echo "  Test the route      : curl -s http://127.0.0.1:25052/ping"
