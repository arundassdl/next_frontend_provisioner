#!/bin/bash
# inspect_agent_job.sh
# Run on f1.evoq.app to test the /frontends deploy endpoint directly
# and diagnose any 500 errors before triggering from Press.

set -euo pipefail

AGENT_URL="http://127.0.0.1:25052"
SITE="${1:-crm.yourdomain.com}"
SLUG="${SITE//./-}"

# Get agent password from Press
AGENT_PASS=$(cd /var/sdlpress/frappe-bench && \
  bench --site sdlpress.evoq.app execute \
  "lambda: frappe.get_doc('Server', 'f1.evoq.app').get_password('agent_password')" \
  2>/dev/null | tr -d "'" || echo "")

if [ -z "$AGENT_PASS" ]; then
  echo "ERROR: Could not read agent_password from Press. Check bench access."
  exit 1
fi

echo "Agent URL  : $AGENT_URL"
echo "Site slug  : $SLUG"
echo "Password   : ${AGENT_PASS:0:6}...  (truncated)"
echo ""

echo "=== 1. GET /frontends ==="
curl -s -H "Authorization: Bearer $AGENT_PASS" \
  "$AGENT_URL/frontends" | python3 -m json.tool 2>/dev/null || echo "(non-JSON)"

echo ""
echo "=== 2. POST /frontends/$SLUG/deploy (dry run with minimal payload) ==="
curl -s -w "\nHTTP:%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $AGENT_PASS" \
  -H "Content-Type: application/json" \
  -d "{
    \"repo\":   \"https://github.com/arundassdl/crm-frontend.git\",
    \"branch\": \"main\",
    \"port\":   3100,
    \"env_vars\": {
      \"FRAPPE_URL\":           \"https://crmapp.evoq.app\",
      \"NEXT_PUBLIC_FRAPPE_URL\": \"https://crmapp.evoq.app\",
      \"NODE_ENV\":             \"production\",
      \"PORT\":                 \"3100\",
      \"HOSTNAME\":             \"0.0.0.0\"
    },
    \"deployment_mode\": \"Frontend Only\",
    \"backend_url\":     \"https://crmapp.evoq.app\"
  }" \
  "$AGENT_URL/frontends/$SLUG/deploy" | python3 -m json.tool 2>/dev/null || echo "(non-JSON)"

echo ""
echo "=== 3. Last 20 lines of agent error log ==="
tail -20 /var/frappe/agent/logs/web.error.log 2>/dev/null || echo "(log not readable)"
