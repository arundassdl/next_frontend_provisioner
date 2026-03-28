#!/usr/bin/env bash
# install_agent_extension.sh
# ---------------------------
# Run on the AGENT SERVER to wire in the Next.js job mixin.
# Usage:  sudo bash install_agent_extension.sh [AGENT_REPO_PATH]
# Default: /var/frappe/agent/repo

set -euo pipefail

AGENT_REPO="${1:-/var/frappe/agent/repo}"
AGENT_PKG="${AGENT_REPO}/agent"
SERVER_PY="${AGENT_PKG}/server.py"
JOB_PY="${AGENT_PKG}/job.py"

echo "==> Agent repo: ${AGENT_REPO}"

# 1. Copy nextjs_jobs.py
echo "==> Copying nextjs_jobs.py"
cp "$(dirname "$0")/agent_extension/agent_jobs.py" "${AGENT_PKG}/nextjs_jobs.py"

# 2. Copy nginx_utils.py
echo "==> Copying nginx_utils.py"
cp "$(dirname "$0")/agent_extension/nginx_utils.py" "${AGENT_PKG}/nginx_utils.py"

# 3. Copy frontend.py
echo "==> Copying frontend_patch.py → frontend.py"
cp "$(dirname "$0")/agent_extension/frontend_patch.py" "${AGENT_PKG}/frontend.py"

# 4. REMOVE any nextjs_jobs import from job.py — it causes a circular import.
#    NextjsMixin is imported ONLY in server.py.
echo "==> Cleaning job.py (removing any nextjs_jobs imports)"
python3 << PYEOF
import re
path = "${JOB_PY}"
text = open(path).read()
cleaned = re.sub(r'\nfrom agent\.nextjs_jobs import[^\n]*\n', '\n', text)
if cleaned != text:
    open(path, 'w').write(cleaned)
    print("  Removed stale nextjs_jobs import from job.py")
else:
    print("  job.py already clean")
PYEOF

# 5. Patch server.py — add NextjsMixin to Server
if grep -q "NextjsMixin" "${SERVER_PY}"; then
    echo "==> server.py already has NextjsMixin — skipping"
else
    echo "==> Patching server.py"
    python3 << PYEOF
import sys, re
path = "${SERVER_PY}"
text = open(path).read()
matches = list(re.finditer(r'^from agent\.\S+.*$', text, re.MULTILINE))
if not matches:
    print("ERROR: no 'from agent.' imports in server.py"); sys.exit(1)
text = text[:matches[-1].end()] + "\nfrom agent.nextjs_jobs import NextjsMixin" + text[matches[-1].end():]
new_text, n = re.subn(r'\bclass Server\(Base\)\s*:', 'class Server(NextjsMixin, Base):', text)
if n == 0:
    print("ERROR: class Server(Base) not found"); sys.exit(1)
open(path, 'w').write(new_text)
print("  Patched: Server(Base) → Server(NextjsMixin, Base)")
PYEOF
fi

# 6. Verify import before restarting
echo "==> Testing import..."
cd "${AGENT_REPO}"
/home/frappe/agent/env/bin/python -c "from agent.server import Server; print('  ✓ Import OK')" || {
    echo "ERROR: import failed — not restarting. Fix the error above."
    exit 1
}

# 7. Restart
echo "==> Restarting agent:web"
supervisorctl restart agent:web
sleep 2
supervisorctl status agent:web
