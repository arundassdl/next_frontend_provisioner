#!/bin/bash
# fix_nginx_now.sh
# Run on f1.evoq.app as root or frappe user
# Fixes the $actual_host map.json entry for crm.evoq.app
# and reloads nginx so the site becomes reachable immediately.

set -euo pipefail

DOMAIN="${1:-crm.evoq.app}"
NGINX_DIR=$(python3 -c "import json; print(json.load(open('/var/frappe/agent/config.json')).get('nginx_directory',''))")
MAP_JSON=$(find "$NGINX_DIR/hosts" -name "map.json" 2>/dev/null | head -1)

echo "nginx_dir : $NGINX_DIR"
echo "map.json  : $MAP_JSON"
echo "domain    : $DOMAIN"
echo ""

if [ -z "$MAP_JSON" ]; then
    echo "ERROR: no map.json found under $NGINX_DIR/hosts/"
    exit 1
fi

echo "=== Current map.json ==="
cat "$MAP_JSON"
echo ""

echo "=== Fixing map.json: $DOMAIN → $DOMAIN (was: 127.0.0.1) ==="
python3 << PYEOF
import json
path = "$MAP_JSON"
with open(path) as f:
    data = json.load(f)

old_val = data.get("$DOMAIN")
# The value must be the domain itself, not 127.0.0.1
# When nginx processes the \$actual_host map, it needs to resolve
# crm.evoq.app → crm.evoq.app (identity) so \$upstream_server_hash
# can match the crm.evoq.app key we patched in proxy.conf.
data["$DOMAIN"] = "$DOMAIN"

with open(path, "w") as f:
    json.dump(data, f, indent=4)

print(f"Fixed: $DOMAIN was '{old_val}' → now '$DOMAIN'")
PYEOF

echo ""
echo "=== Updated map.json ==="
cat "$MAP_JSON"
echo ""

echo "=== Triggering NginxReloadManager to regenerate proxy.conf ==="
python3 << PYEOF2
import sys, os, time
sys.path.insert(0, "/var/frappe/agent/repo")
os.chdir("/var/frappe/agent/repo")
try:
    from agent.nginx_reload_manager import NginxReloadManager
    mgr = NginxReloadManager()
    mgr.request_reload(request_id="nfp-fix-manual")
    print("NginxReloadManager: reload requested")
    time.sleep(4)
    print("NginxReloadManager: cycle complete")
except Exception as e:
    print(f"NginxReloadManager error: {e}")
PYEOF2

echo ""
echo "=== Verifying proxy.conf entries for $DOMAIN ==="
grep -n "$DOMAIN\|nextjs_crm" "$NGINX_DIR/proxy.conf" || echo "(not found — will patch manually)"

echo ""
echo "=== Ensuring upstream block and map entries exist ==="
python3 << PYEOF3
import re, os, sys
sys.path.insert(0, "/var/frappe/agent/repo")
os.chdir("/var/frappe/agent/repo")

domain        = "$DOMAIN"
port          = 3101
nginx_dir     = "$NGINX_DIR"
proxy_conf    = os.path.join(nginx_dir, "proxy.conf")
safe          = domain.replace(".", "_").replace("-", "_")
upstream_name = f"nextjs_{safe}"
upstream_block = f"upstream {upstream_name} {{\n\tserver 127.0.0.1:{port};\n\tkeepalive 32;\n}}\n"
map_entry     = f"\t{domain} http://{upstream_name};"

with open(proxy_conf) as f:
    content = f.read()

changed = False

# Remove stale nextjs block
new = re.sub(rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content)
if new != content:
    content = new
    changed = True

# Remove stale map entries
new = re.sub(rf"^\s*{re.escape(domain)}\s+http://[^;]+;\n?", "", content, flags=re.MULTILINE)
if new != content:
    content = new
    changed = True

# Insert upstream block
first = re.search(r"^upstream \w+", content, re.MULTILINE)
if first:
    content = content[:first.start()] + upstream_block + "\n" + content[first.start():]
else:
    content = upstream_block + "\n" + content
changed = True

# Add to both maps using anchored regex
def add_to_map(text, map_var, entry):
    pattern = re.compile(
        rf"(map\s+\\\$actual_host\s+\\\${re.escape(map_var)}\s*\{{[^}}]*?)"
        rf"(\tdefault\s+http://[^;]+;)",
        re.DOTALL,
    )
    m = pattern.search(text)
    if m and entry.strip() not in m.group(0):
        return text[:m.start(2)] + entry + "\n" + text[m.start(2):]
    elif not m:
        # Fallback: first occurrence of default anchor
        anchor = "\tdefault http://site_not_found;"
        pos = text.find(anchor)
        if pos != -1 and entry.strip() not in text[:pos + 50]:
            return text[:pos] + entry + "\n" + text[pos:]
    return text

content = add_to_map(content, "upstream_server_hash", map_entry)
content = add_to_map(content, "socket_upstream_hash",  map_entry)

with open(proxy_conf, "w") as f:
    f.write(content)
print(f"proxy.conf patched: {domain} → {upstream_name} port {port}")
PYEOF3

echo ""
echo "=== Final check: nginx test ==="
nginx -t 2>&1 | tail -3

echo ""
echo "=== Reloading nginx directly ==="
sudo /usr/sbin/nginx -s reload 2>/dev/null && echo "nginx reloaded" || \
  nginx -s reload 2>/dev/null && echo "nginx reloaded" || \
  echo "WARNING: could not reload nginx — check sudo permissions"

echo ""
echo "=== Test: curl crm.evoq.app ==="
sleep 2
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 https://crm.evoq.app/ 2>/dev/null || echo "000")
echo "HTTPS response: HTTP $HTTP"
if [ "$HTTP" = "200" ] || [ "$HTTP" = "308" ]; then
    echo "SUCCESS: site is reachable!"
elif [ "$HTTP" = "302" ] || [ "$HTTP" = "301" ]; then
    echo "Redirect — check where it redirects to (may still be site_not_found)"
else
    echo "Still not reachable (HTTP $HTTP). Check proxy.conf manually."
fi
