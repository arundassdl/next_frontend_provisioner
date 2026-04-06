# Agent Integration Guide

## Overview

The Next.js Frontend Provisioner extends the Press agent by registering two
HTTP routes on the agent's Flask application:

| Route | Method | Purpose |
|---|---|---|
| `/frontends/<name>/deploy` | `POST` | Clone → build → start container → configure nginx |
| `/frontends/<name>` | `DELETE` | Stop container → remove nginx routing |
| `/frontends` | `GET` | List all deployed NFP sites and their ports |

The integration uses a **Flask Blueprint** registered via a single import line
in `web.py`. No agent core files are modified beyond that one line.

---

## Architecture

### Single-server layout (co-located app + proxy)

```
Press Controller (cloud.evoq.app)
    │  dispatches RQ jobs via agent HTTP API
    ▼
Agent Server (f1.evoq.app = n1.evoq.app — same IP)
    ├── gunicorn (agent:web)          ← Flask app, port 25052
    ├── rq worker × 2 (agent:worker)  ← executes _nfp_deploy / _nfp_remove
    ├── NginxReloadManager            ← regenerates proxy.conf periodically
    ├── Docker containers             ← Next.js apps on 127.0.0.1:310x:3000
    └── nginx                         ← routes *.evoq.app → containers
```

### Port allocation

| Site | Container port | Host port |
|---|---|---|
| `crm.evoq.app` | 3000 (always) | 3101 |
| `fsm.evoq.app` | 3000 (always) | 3102 |
| `erp.evoq.app` | 3000 (always) | 3103 |

Ports are auto-allocated from `PORT_BASE = 3100` and stored in
`nginx_directory/nfp_sites.json`. The `PORT` env var sent by Press (host port)
is **never** passed into the container — Next.js always binds to port 3000
inside, regardless of the host-side mapping.

### nginx routing strategy

The agent's `NginxReloadManager` runs `Proxy()._generate_proxy_config()` on
every reload cycle, regenerating `proxy.conf` from its internal state — which
**only knows about Frappe sites** and wipes any custom Next.js upstream blocks.

NFP solves this with three layers:

1. **Persistent registry** — `nfp_sites.json` stores `{domain: port}` for all
   deployed sites. Written on every deploy/remove; read on every agent startup.

2. **Startup re-apply** — when `frontend_patch.py` is imported at agent start,
   it immediately re-patches `proxy.conf` from the registry and reloads nginx.

3. **NginxReloadManager wrapper** — `nfp_proxy_patch.py` replaces the manager
   process (via supervisor config). It monkey-patches
   `Proxy._generate_proxy_config` before handing off to the real manager, so
   NFP upstream blocks are re-applied after every regeneration cycle — without
   touching `nginx_reload_manager.py`.

---

## Prerequisites

Run these once on the **agent server** before installation.

### Sudoers entry (required for nginx reload)

The `frappe` user must be able to reload nginx without a password:

```bash
echo "frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload" \
  | sudo tee /etc/sudoers.d/frappe-nginx-reload
sudo chmod 440 /etc/sudoers.d/frappe-nginx-reload
sudo visudo -c   # verify no syntax errors
```

### Verify Docker access

```bash
sudo -u frappe docker ps   # must work without password
```

---

## Installation

### Step 1 — Run on the agent server

From the `next_frontend_provisioner` app directory:

```bash
sudo bash install_agent_extension.sh
# Optional: specify a custom agent repo path
sudo bash install_agent_extension.sh /var/frappe/agent/repo
```

The script performs these steps automatically:

| Step | Action |
|---|---|
| 1 | Removes any old NextjsMixin installation from `server.py` / `job.py` |
| 2 | Copies `frontend_patch.py`, `nginx_utils.py`, `template_injector.py` into the agent package |
| 3 | Writes Dockerfile template and health route into `agent/templates/docker/` |
| 4 | Injects one import line into `web.py` |
| 5 | Syntax-checks all modified files, restarts `agent:web`, runs smoke test |

**Files modified by the installer:**

| File | Change |
|---|---|
| `agent/web.py` | One import line added: `import agent.frontend_patch` |
| `agent/frontend_patch.py` | Created (Blueprint + deploy logic) |
| `agent/nginx_utils.py` | Created (nginx config helpers) |
| `agent/template_injector.py` | Created (Dockerfile + health route injection) |
| `agent/templates/docker/` | Created (Dockerfile, route.ts) |

**Files never touched:**

`server.py`, `job.py`, `base.py`, `proxy.py`, `nginx_reload_manager.py`

---

### Step 2 — Install the NginxReloadManager wrapper

This makes nginx routing **permanent** across all NginxReloadManager cycles.

```bash
# Copy the wrapper into the agent package
cp agent_extension/nfp_proxy_patch.py /var/frappe/agent/repo/agent/nfp_proxy_patch.py

# Point supervisor at the wrapper instead of the real manager
sudo sed -i \
  's|exec /var/frappe/agent/env/bin/python /var/frappe/agent/repo/agent/nginx_reload_manager.py|exec /var/frappe/agent/env/bin/python /var/frappe/agent/repo/agent/nfp_proxy_patch.py|' \
  /etc/supervisor/conf.d/agent.conf

# Verify the change
grep "nfp_proxy_patch\|nginx_reload_manager" /etc/supervisor/conf.d/agent.conf

# Reload supervisor and restart the manager
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart agent:nginx_reload_manager
```

Confirm the wrapper started:

```bash
sudo tail -10 /var/frappe/agent/logs/nginx_reload_manager.log
# Expected:
# [NFP-wrapper] Starting NFP proxy patch wrapper...
# [NFP-wrapper] Proxy._generate_proxy_config patched ✓
# [NFP-wrapper] Handing off to NginxReloadManager...
```

---

### Step 3 — Seed the registry (first-time only)

If sites were deployed before the registry existed, seed it manually:

```bash
python3 -c "
import json
# Add all currently-running NFP sites here
data = {
    'crm.evoq.app': 3101,
    # 'fsm.evoq.app': 3102,
}
json.dump(data, open('/home/frappe/agent/nginx/nfp_sites.json', 'w'), indent=2)
print('Seeded:', data)
"
```

---

### Step 4 — Set the callback token on the Press bench

```bash
bench --site your-press-site set-config nfp_agent_callback_token "your-secret-token"
```

The same token is validated by `api.agent_job_update` when the agent posts
job completion callbacks.

---

## Verifying the installation

```bash
# 1. Agent web process running
sudo supervisorctl status agent:web

# 2. Blueprint routes registered (check web log at startup)
grep "frontends routes registered" /var/frappe/agent/logs/web.log | tail -3

# 3. Monkey-patch active in NginxReloadManager
grep "NFP-wrapper" /var/frappe/agent/logs/nginx_reload_manager.log | tail -5

# 4. Registry file exists
cat /home/frappe/agent/nginx/nfp_sites.json

# 5. proxy.conf has NFP entries
grep "nextjs_\|nfp" /home/frappe/agent/nginx/proxy.conf

# 6. Agent HTTP API responds
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $(cat /var/frappe/agent/config.json | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("agent_password",""))')" \
  http://127.0.0.1:25052/ping
# Expected: 200
```

---

## How a deploy works end-to-end

```
Press desk → "Deploy Frontend" button
    │
    ▼
Press controller
    POST /frontends/crm-evoq-app/deploy
    body: {repo, branch, port, env_vars, deployment_mode, backend_url, site_name}
    │
    ▼
Agent (gunicorn / agent:web)
    Enqueues _nfp_deploy() to RQ "default" queue
    Returns: {"job": "nfp-crm-evoq-app-xxxxxxxx", "status": "queued"}
    │
    ▼
RQ Worker (agent:worker)
    1. git clone / git pull
    2. template_injector: inject Dockerfile + health route
    3. docker build (NEXT_PUBLIC_* vars as --build-arg only)
    4. docker stop + docker rm (old container)
    5. docker run -d --restart always
          -e PORT=3000          ← always 3000 inside
          -p 127.0.0.1:3101:3000
    6. _write_nginx():
          a. Clean stale .nextjs.conf files
          b. Clean poisoned Proxy() state (map.json / upstreams/)
          c. Save domain:port to nfp_sites.json
          d. Patch proxy.conf (upstream block + map entries)
          e. sudo nginx -s reload
    7. POST callback to Press → status "Success"
    │
    ▼
NginxReloadManager (any future cycle)
    proxy._generate_proxy_config()   ← would wipe NFP entries
    [NFP-wrapper intercepts]
    reads nfp_sites.json             ← re-applies all NFP entries
    sudo nginx -s reload             ← reloads with correct config
```

---

## Multi-site port allocation

Ports are auto-allocated from `PORT_BASE = 3100` up to `PORT_MAX = 3199`
(100 sites). The `port` value in the Press deploy request is used as a
**hint** — if it's free and in range, it's used; otherwise the next free
port is allocated automatically.

To see current allocations:

```bash
cat /home/frappe/agent/nginx/nfp_sites.json
# or via API:
curl http://127.0.0.1:25052/frontends
```

---

## Environment variable handling

| Variable type | How passed |
|---|---|
| `NEXT_PUBLIC_*` | As `--build-arg` at build time only (baked into the image) |
| Runtime vars (`FRAPPE_URL`, `FRAPPE_HOSTNAME`, etc.) | As `-e KEY=value` at `docker run` |
| `PORT`, `HOST`, `HOSTNAME` | **Stripped** — never passed into container |
| `PORT=3000` | Always forced inside container via `-e PORT=3000` |

This separation ensures Next.js always binds to port 3000 inside the container
regardless of which host port Press allocates.

---

## Troubleshooting

### Site redirects to `evoq.app/dashboard/#/sites/new`

`proxy.conf` is missing the domain entry. Re-patch manually:

```bash
python3 /var/frappe/agent/repo/agent/nfp_proxy_patch.py   # if run as script
# or trigger a deploy from Press — startup re-apply will patch it
```

### 502 Bad Gateway

```bash
# Check container is running and on port 3000 internally
docker ps | grep <site-slug>
docker logs <site-slug> --tail 20
# Should show: Local: http://localhost:3000

# Test direct connection from host
curl http://127.0.0.1:<host-port>/
```

Common cause: `PORT` env var leaked into container, making Next.js bind to
the host port number instead of 3000. Fixed by current `frontend_patch.py`
which strips `PORT` from `env_vars` before `docker run`.

### proxy.conf entries disappear after NginxReloadManager runs

Confirm the wrapper is active:

```bash
grep "NFP-wrapper" /var/frappe/agent/logs/nginx_reload_manager.log
# If empty: wrapper not installed — redo Step 2
```

Check supervisor is pointing at the wrapper:

```bash
grep "command" /etc/supervisor/conf.d/agent.conf | grep nginx
# Should contain: nfp_proxy_patch.py  (not nginx_reload_manager.py)
```

### Build fails: `Response.json` TypeScript error

The health route template uses `NextResponse.json()` for Next.js 14
compatibility. If the build fails with this error, check:

```bash
grep -n "Response.json\|NextResponse" \
  /var/frappe/agent/repo/agent/templates/docker/health_route/route.ts
# Should use: return NextResponse.json(...)
```

Re-run `install_agent_extension.sh` to update the template.

### nginx reload permission denied (`/etc/letsencrypt`)

The `frappe` user cannot read letsencrypt certs directly. Ensure the sudoers
entry from the Prerequisites section is installed:

```bash
sudo cat /etc/sudoers.d/frappe-nginx-reload
# Expected: frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload
```

---

## File reference

| File | Location | Purpose |
|---|---|---|
| `frontend_patch.py` | `agent/` | Blueprint routes, deploy/remove workers, persistent registry, startup re-apply |
| `nfp_proxy_patch.py` | `agent/` | NginxReloadManager wrapper — monkey-patches Proxy before manager starts |
| `nginx_utils.py` | `agent/` | nginx upstream config helpers (used by agent_jobs mixin path) |
| `template_injector.py` | `agent/` | Injects Dockerfile + health route into cloned repo |
| `nfp_sites.json` | `nginx_directory/` | Persistent `{domain: port}` registry — survives agent restarts |
| `agent.conf` | `/etc/supervisor/conf.d/` | One-line change: points `nginx_reload_manager` at `nfp_proxy_patch.py` |

---

## Uninstalling

```bash
# 1. Remove all NFP containers
cat /home/frappe/agent/nginx/nfp_sites.json | python3 -c "
import json, sys, subprocess
for domain in json.load(sys.stdin):
    slug = domain.replace('.', '-').replace('_', '-')
    subprocess.run(['docker', 'stop', slug], check=False)
    subprocess.run(['docker', 'rm',   slug], check=False)
    print('Removed container:', slug)
"

# 2. Restore supervisor to original nginx_reload_manager
sudo sed -i \
  's|nfp_proxy_patch.py|nginx_reload_manager.py|' \
  /etc/supervisor/conf.d/agent.conf
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl restart agent:nginx_reload_manager

# 3. Remove NFP files from agent
rm /var/frappe/agent/repo/agent/frontend_patch.py
rm /var/frappe/agent/repo/agent/nfp_proxy_patch.py
rm /var/frappe/agent/repo/agent/nginx_utils.py
rm /var/frappe/agent/repo/agent/template_injector.py
rm /home/frappe/agent/nginx/nfp_sites.json

# 4. Remove the import line from web.py
sed -i '/frontend_patch/d' /var/frappe/agent/repo/agent/web.py

# 5. Remove the sudoers entry
sudo rm /etc/sudoers.d/frappe-nginx-reload

# 6. Restart agent
sudo supervisorctl restart agent:agent-web agent:agent-worker-0 agent:agent-worker-1
```
