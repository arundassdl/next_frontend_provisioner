#!/bin/bash
# install_agent_extension.sh  — run as root on f1.evoq.app
# Patches frappe/agent to support Next.js frontend deployments.
# Does NOT replace frontend.py — monkey-patches it instead.

set -euo pipefail

AGENT_REPO="/var/frappe/agent/repo"
AGENT_PKG="$AGENT_REPO/agent"
NFP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== [1/5] Copying nginx_utils.py and template_injector.py ==="
cp "$NFP_DIR/agent_extension/nginx_utils.py"       "$AGENT_PKG/nginx_utils.py"
cp "$NFP_DIR/agent_extension/template_injector.py" "$AGENT_PKG/template_injector.py"
cp "$NFP_DIR/agent_extension/frontend_patch.py"    "$AGENT_PKG/frontend_patch.py"
echo "  -> nginx_utils.py, template_injector.py, frontend_patch.py copied"
echo "  -> frontend.py left untouched (monkey-patch only)"

echo ""
echo "=== [2/5] Injecting patch import into web.py ==="
WEB="$AGENT_PKG/web.py"

if grep -q "frontend_patch" "$WEB"; then
    echo "  -> frontend_patch import already present in web.py — skipping"
else
    # Find the last 'from agent.' or 'import agent.' import line and insert after it
    LAST_IMPORT_LINE=$(grep -n "^from agent\.\|^import agent\." "$WEB" | tail -1 | cut -d: -f1)
    if [ -n "$LAST_IMPORT_LINE" ]; then
        sed -i "${LAST_IMPORT_LINE}a import agent.frontend_patch  # noqa: F401 — NFP monkey-patch" "$WEB"
        echo "  -> import agent.frontend_patch inserted at line $((LAST_IMPORT_LINE + 1)) of web.py"
    else
        # Fallback: prepend after the first import block
        sed -i '1s/^/import agent.frontend_patch  # noqa: F401 — NFP monkey-patch\n/' "$WEB"
        echo "  -> import agent.frontend_patch prepended to web.py"
    fi
fi

echo ""
echo "=== [3/5] Verifying /frontends deploy route exists in web.py ==="
if grep -q "deploy_frontend" "$WEB"; then
    LINE=$(grep -n "deploy_frontend" "$WEB" | head -3)
    echo "  -> Found: $LINE"
else
    echo "  ERROR: /frontends deploy route not found in web.py — manual fix required"
    exit 1
fi

echo ""
echo "=== [4/5] Verifying agent config ==="
CFG="/var/frappe/agent/config.json"
WEB_PORT=$(python3 -c "import json; print(json.load(open('$CFG')).get('web_port', 25052))")
NGINX_DIR=$(python3 -c "import json; print(json.load(open('$CFG')).get('nginx_directory',''))")
echo "  web_port      : $WEB_PORT"
echo "  nginx_dir     : $NGINX_DIR"

echo ""
echo "=== [5/5] Restarting frappe-agent ==="
supervisorctl restart agent:web 2>/dev/null && echo "  -> restarted via supervisorctl" \
  || systemctl restart frappe-agent 2>/dev/null && echo "  -> restarted via systemctl" \
  || echo "  WARNING: could not restart agent automatically — restart manually"

echo ""
echo "=== Verification (waiting 3s for startup) ==="
sleep 3

# Extract agent password using frappe python directly
PASS=$(cd /var/sdlpress/frappe-bench && \
  ./env/bin/python - << 'PY' 2>/dev/null
import sys, os
os.chdir('/var/sdlpress/frappe-bench')
sys.path.insert(0, 'apps/frappe')
import frappe
frappe.init(site='sdlpress.evoq.app', sites_path='sites')
frappe.connect()
doc = frappe.get_doc('Server', 'f1.evoq.app')
print(doc.get_password('agent_password'), end='')
frappe.destroy()
PY
)

if [ -z "$PASS" ]; then
    echo "  Could not extract agent password automatically."
    echo "  Run manually to verify:"
    echo "    curl -H 'Authorization: Bearer PASSWORD' http://127.0.0.1:$WEB_PORT/frontends/crm/deploy -X POST ..."
    exit 0
fi

echo "  Password extracted: ${PASS:0:4}...${PASS: -4}"

echo ""
echo "=== Test: POST /frontends/crm/deploy ==="
curl -s -w "\nHTTP:%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $PASS" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\":            \"https://github.com/arundassdl/crm-frontend.git\",
    \"branch\":          \"main\",
    \"port\":            3100,
    \"env_vars\":        {\"NODE_ENV\": \"production\", \"PORT\": \"3100\", \"HOSTNAME\": \"0.0.0.0\"},
    \"deployment_mode\": \"Frontend Only\",
    \"backend_url\":     \"https://crmapp.evoq.app\"
  }" \
  "http://127.0.0.1:$WEB_PORT/frontends/crm/deploy"

echo ""
echo ""
echo "If you see HTTP:200 or HTTP:202 above, the agent is ready."
echo "Click 'Deploy Frontend' in the Frappe desk."
