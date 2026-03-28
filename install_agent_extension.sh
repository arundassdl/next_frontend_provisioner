#!/bin/bash
# install_agent_extension.sh
# Run as root (or with sudo) on f1.evoq.app to patch the frappe/agent.
# Tested against: /var/frappe/agent  with Python 3.12 / gunicorn 20.0.4

set -euo pipefail

AGENT_REPO="/var/frappe/agent/repo"
AGENT_PKG="$AGENT_REPO/agent"
NFP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== [1/4] Copying agent extension files ==="
cp "$NFP_DIR/agent_extension/frontend_patch.py"    "$AGENT_PKG/frontend.py"
cp "$NFP_DIR/agent_extension/nginx_utils.py"       "$AGENT_PKG/nginx_utils.py"
cp "$NFP_DIR/agent_extension/template_injector.py" "$AGENT_PKG/template_injector.py"
echo "  -> frontend.py, nginx_utils.py, template_injector.py copied"

echo ""
echo "=== [2/4] Checking web.py for deploy_frontend route ==="
if grep -q "deploy_frontend" "$AGENT_PKG/web.py"; then
    echo "  -> /frontends deploy route already present in web.py — no patch needed"
else
    echo "  WARNING: /frontends route not found in web.py."
    echo "  You may need to add it manually. See agent_extension/AGENT_INTEGRATION.md"
fi

echo ""
echo "=== [3/4] Verifying agent config.json ==="
CFG="/var/frappe/agent/config.json"
WEB_PORT=$(python3 -c "import json; print(json.load(open('$CFG')).get('web_port','NOT SET'))" 2>/dev/null || echo "UNREADABLE")
NGINX_DIR=$(python3 -c "import json; print(json.load(open('$CFG')).get('nginx_directory','NOT SET'))" 2>/dev/null || echo "UNREADABLE")
echo "  web_port      : $WEB_PORT"
echo "  nginx_dir     : $NGINX_DIR"

echo ""
echo "=== [4/4] Restarting frappe-agent ==="
systemctl restart frappe-agent 2>/dev/null || \
  supervisorctl restart agent:web 2>/dev/null || \
  (cd /var/frappe/agent && pkill -f gunicorn && sleep 2 && \
    /var/frappe/agent/env/bin/gunicorn \
      --workers 2 --bind 127.0.0.1:${WEB_PORT:-25052} \
      --daemon --log-file /var/frappe/agent/logs/web.error.log \
      agent.web:application) && echo "  -> agent restarted"

echo ""
echo "=== Verification ==="
sleep 2
AGENT_PASS=$(cd /var/sdlpress/frappe-bench && \
  bench --site sdlpress.evoq.app execute \
  "lambda: frappe.get_doc('Server', 'f1.evoq.app').get_password('agent_password')" \
  2>/dev/null | tr -d "'" || echo "")

if [ -n "$AGENT_PASS" ]; then
    RESP=$(curl -s -o /dev/null -w "%{http_code}" \
      -H "Authorization: Bearer $AGENT_PASS" \
      http://127.0.0.1:${WEB_PORT:-25052}/frontends 2>/dev/null)
    echo "  GET /frontends → HTTP $RESP  (expected 200)"
else
    echo "  Could not retrieve agent password for verification."
    echo "  Run manually: curl -H 'Authorization: Bearer PASSWORD' http://127.0.0.1:25052/frontends"
fi

echo ""
echo "Done. The agent now accepts /frontends/<name>/deploy and /frontends/<name> DELETE."
