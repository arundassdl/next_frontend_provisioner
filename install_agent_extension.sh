#!/usr/bin/env bash
# install_agent_extension.sh
# ---------------------------
# Run on the AGENT SERVER to wire in the Next.js job mixin.
#
# Usage:
#   sudo bash install_agent_extension.sh [AGENT_REPO_PATH]
#
# Default: /var/frappe/agent/repo

set -euo pipefail

AGENT_REPO="${1:-/var/frappe/agent/repo}"
AGENT_PKG="${AGENT_REPO}/agent"
SERVER_PY="${AGENT_PKG}/server.py"
JOB_PY="${AGENT_PKG}/job.py"

echo "==> Agent repo: ${AGENT_REPO}"

# ── 1. Copy nextjs_jobs.py ────────────────────────────────────────────
echo "==> Copying nextjs_jobs.py → ${AGENT_PKG}/nextjs_jobs.py"
cp "$(dirname "$0")/agent_extension/agent_jobs.py" "${AGENT_PKG}/nextjs_jobs.py"

# ── 2. Copy nginx_utils.py ────────────────────────────────────────────
echo "==> Copying nginx_utils.py → ${AGENT_PKG}/nginx_utils.py"
cp "$(dirname "$0")/agent_extension/nginx_utils.py" "${AGENT_PKG}/nginx_utils.py"

# ── 3. Patch agent/job.py — remove stale import if it was injected ────
#    The new pattern doesn't need job.py patched (mixin lives in server.py)
#    but we keep the import there so the module stays importable cleanly.
if grep -q "from agent.nextjs_jobs import" "${JOB_PY}"; then
    echo "==> job.py already has nextjs_jobs import — OK"
else
    echo "==> Injecting nextjs_jobs import into job.py"
    python3 - "${JOB_PY}" << 'PYEOF'
import sys, re
path = sys.argv[1]
text = open(path).read()
matches = list(re.finditer(r'^from agent\.\S+.*$', text, re.MULTILINE))
if not matches:
    print("ERROR: no 'from agent.' imports found", file=sys.stderr); sys.exit(1)
insert_pos = matches[-1].end()
block = "\nfrom agent.nextjs_jobs import NextjsMixin"
open(path, "w").write(text[:insert_pos] + block + text[insert_pos:])
print("Injected import after:", matches[-1].group())
PYEOF
fi

# ── 4. Patch agent/server.py — add NextjsMixin to Server base classes ─
if grep -q "NextjsMixin" "${SERVER_PY}"; then
    echo "==> server.py already has NextjsMixin — skipping"
else
    echo "==> Patching server.py — adding NextjsMixin to Server"
    python3 - "${SERVER_PY}" << 'PYEOF'
import sys, re

path = sys.argv[1]
text = open(path).read()

# 1. Add import after the last 'from agent.' import line
import_block = "\nfrom agent.nextjs_jobs import NextjsMixin"
matches = list(re.finditer(r'^from agent\.\S+.*$', text, re.MULTILINE))
if not matches:
    print("ERROR: no 'from agent.' imports found in server.py", file=sys.stderr)
    sys.exit(1)
insert_pos = matches[-1].end()
text = text[:insert_pos] + import_block + text[insert_pos:]

# 2. Change 'class Server(Base):' → 'class Server(NextjsMixin, Base):'
new_text, n = re.subn(
    r'\bclass Server\(Base\)\s*:',
    'class Server(NextjsMixin, Base):',
    text
)
if n == 0:
    print("ERROR: could not find 'class Server(Base):' in server.py", file=sys.stderr)
    sys.exit(1)

open(path, "w").write(new_text)
print(f"Patched server.py: Server(Base) → Server(NextjsMixin, Base)")
PYEOF
fi

# ── 5. Restart agent:web ──────────────────────────────────────────────
echo "==> Restarting agent:web"
supervisorctl restart agent:web

echo ""
echo "✓ Done. Verifying..."
sleep 2
supervisorctl status agent:web
