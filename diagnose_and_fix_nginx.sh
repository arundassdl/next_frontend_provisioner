#!/bin/bash
# Run on f1.evoq.app (the agent server) as root or frappe user
# Diagnoses the proxy.conf state and shows exactly what is/isn't working

echo "=== 1. proxy.conf location and content ==="
NGINX_DIR=$(python3 -c "import json; print(json.load(open('/var/frappe/agent/config.json')).get('nginx_directory',''))" 2>/dev/null)
PROXY_CONF="$NGINX_DIR/proxy.conf"
echo "nginx_directory : $NGINX_DIR"
echo "proxy.conf path : $PROXY_CONF"
echo ""

if [ -f "$PROXY_CONF" ]; then
    echo "--- Current NFP entries in proxy.conf ---"
    grep -n "nextjs\|crm" "$PROXY_CONF" || echo "(none found)"
    echo ""
    echo "--- upstream_server_hash map block ---"
    grep -A 30 "upstream_server_hash" "$PROXY_CONF" | head -40
    echo ""
    echo "--- All upstream blocks ---"
    grep -A 4 "^upstream " "$PROXY_CONF" | head -40
else
    echo "ERROR: proxy.conf not found at $PROXY_CONF"
    echo "Searching..."
    find /home/frappe /etc/nginx -name "proxy.conf" 2>/dev/null | head -5
fi

echo ""
echo "=== 2. Container running ==="
docker ps --filter name=crm 2>/dev/null || echo "docker not accessible"

echo ""
echo "=== 3. nginx test ==="
nginx -t 2>&1 | tail -5

echo ""
echo "=== 4. DNS check: what does crm.evoq.app resolve to? ==="
dig +short crm.evoq.app 2>/dev/null || nslookup crm.evoq.app 2>/dev/null | grep Address | tail -1

echo ""
echo "=== 5. Is n1.evoq.app the proxy server? ==="
echo "Server record shows proxy_server = n1.evoq.app"
echo "Check if nginx on n1.evoq.app handles crm.evoq.app:"
curl -s -o /dev/null -w "HTTP %{http_code}" --connect-timeout 5 http://crm.evoq.app/ 2>/dev/null || echo "connection refused/timeout"
