#!/bin/bash
# Run on f1.evoq.app to diagnose the nginx routing issue

echo "=== 1. Agent nginx_directory from config ==="
python3 -c "import json; cfg=json.load(open('/var/frappe/agent/config.json')); print('nginx_directory:', cfg.get('nginx_directory'))"

echo ""
echo "=== 2. NFP conf file on APP SERVER (f1.evoq.app) ==="
NGINX_DIR=$(python3 -c "import json; cfg=json.load(open('/var/frappe/agent/config.json')); print(cfg.get('nginx_directory',''))")
echo "nginx_dir: $NGINX_DIR"
ls -la "$NGINX_DIR"/ 2>/dev/null | grep -i "crm\|nfp\|nextjs" || echo "No NFP conf files found in $NGINX_DIR"

echo ""
echo "=== 3. Check all nginx dirs for NFP conf ==="
for dir in /etc/nginx/conf.d /home/frappe/agent/nginx /home/frappe/agent/nginx/hosts /home/frappe/agent/nginx/upstreams; do
    if [ -d "$dir" ]; then
        echo "--- $dir ---"
        ls -la "$dir"/ 2>/dev/null | grep -i "crm\|nfp\|nextjs" || echo "  (no NFP files)"
    fi
done

echo ""
echo "=== 4. Container port binding ==="
docker inspect crm 2>/dev/null | python3 -c "
import json,sys
data=json.load(sys.stdin)
ports=data[0]['NetworkSettings']['Ports']
print('Ports:', ports)
bindings=data[0]['HostConfig']['PortBindings']
print('HostConfig PortBindings:', bindings)
" 2>/dev/null || echo "Container 'crm' not found"

echo ""
echo "=== 5. Test container is reachable locally ==="
curl -s -o /dev/null -w "HTTP %{http_code}" http://127.0.0.1:3100/ 2>/dev/null && echo "" || echo "Cannot reach 127.0.0.1:3100"

echo ""
echo "=== 6. Proxy server nginx config (if accessible) ==="
echo "proxy_server from Press Server doc: n1.evoq.app"
echo "The NFP nginx conf must exist on n1.evoq.app, not f1.evoq.app"
echo "Check: ssh n1.evoq.app 'ls /home/frappe/agent/nginx/ | grep crm'"

echo ""
echo "=== 7. What nginx conf was actually written ==="
find /home/frappe /etc/nginx /var/frappe -name "*.conf" 2>/dev/null | \
  xargs grep -l "crm\|nextjs_crm\|nfp" 2>/dev/null || echo "No nginx conf mentioning crm found"

echo ""
echo "=== 8. nginx_utils write_upstream was called with these args ==="
grep -r "crm\|nextjs_crm" /home/frappe/agent/nginx/ 2>/dev/null | head -20 || echo "Nothing found"
