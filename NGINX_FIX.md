# Nginx Routing Fix

## Root Cause

The container is running correctly (`127.0.0.1:3100->3000/tcp`), but
`crm.evoq.app` is unreachable because **no nginx config exists on the proxy
server** (`n1.evoq.app`) for that domain.

**What was wrong:**

`frontend_patch.py` called `nginx_utils.write_upstream()` which wrote a config
like this to the **app server's** local nginx directory:

```nginx
upstream nextjs_crm {
    server crm:3000;   # WRONG — "crm" is a Docker container hostname,
}                      # only resolvable on the app server's Docker network
```

Two problems:
1. Written to the **app server** — but external traffic hits the **proxy server**
2. Used `container_name:3000` — not reachable from the proxy anyway

**What it should be (on the proxy server):**

```nginx
upstream nextjs_crm {
    server <app_server_private_ip>:3100;  # host port, reachable from proxy
}
```

---

## Immediate Fix (for the running crm site)

SSH into the **proxy server** (`n1.evoq.app`) and create the nginx config manually:

```bash
# On n1.evoq.app (the proxy server):
sudo tee /etc/nginx/conf.d/crm.evoq.app.nextjs.conf << 'NGINX'
# Managed by next_frontend_provisioner
upstream nextjs_crm_evoq_app {
    server <APP_SERVER_PRIVATE_IP>:3100;
    keepalive 32;
}

server {
    listen 80;
    server_name crm.evoq.app;

    # Proxy Frappe API calls to the existing backend
    location ~* ^/(api|files|private/files)/ {
        proxy_pass         https://crmapp.evoq.app;
        proxy_http_version 1.1;
        proxy_set_header   Host              crmapp.evoq.app;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout    60s;
        proxy_connect_timeout 10s;
    }

    # Everything else -> Next.js container
    location / {
        proxy_pass         http://nextjs_crm_evoq_app;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout    60s;
        proxy_connect_timeout 10s;
    }
}
NGINX

# Replace <APP_SERVER_PRIVATE_IP> with the actual IP of f1.evoq.app
# You can find it with: grep private_ip /var/frappe/agent/config.json
# or: ping f1.evoq.app

sudo nginx -t && sudo systemctl reload nginx
```

After this, `http://crm.evoq.app` should load the Next.js frontend.

---

## Files Changed in This Release

### `agent_extension/nginx_utils.py`
- **`write_upstream()`** now takes `app_server_ip` instead of `container_name`
- Upstream uses `server {app_server_ip}:{port}` — the host-bound port reachable from the proxy
- `_reload_nginx()` tries `NginxReloadManager` first, then `systemctl`

### `agent_extension/frontend_patch.py`
- `_nginx_dir()` now writes to `{nginx_directory}/hosts/` — the directory Press's NginxReloadManager monitors
- Added `_app_server_ip()` — reads the app server's IP from agent config
- `_write_nginx()` passes `app_server_ip=_app_server_ip()` to `write_upstream()`

### `agent_extension/proxy_manager.py`
- `run_proxy_playbook()` now accepts `deployment_mode` and `backend_url`
- Passes `backend_url` and `backend_host` as Ansible extra vars

### `ansible/templates/nextjs_proxy.conf.j2`
- Added `Frontend Only` mode: Jinja2 `{% if deployment_mode == 'Frontend Only' %}` block
- Frontend Only: `/api`, `/files`, `/private/files` → Frappe backend; everything else → Next.js
- Full Stack: all traffic → Next.js

---

## Deploy Steps

```bash
# On the agent server (f1.evoq.app):
sudo cp agent_extension/nginx_utils.py  /var/frappe/agent/repo/agent/nginx_utils.py
sudo cp agent_extension/frontend_patch.py /var/frappe/agent/repo/agent/frontend_patch.py
sudo cp agent_extension/proxy_manager.py  /var/frappe/agent/repo/agent/proxy_manager.py
sudo supervisorctl restart agent:web agent:worker

# Also update the ansible template on the Press controller:
cp ansible/templates/nextjs_proxy.conf.j2 \
   /var/sdlpress/frappe-bench/apps/next_frontend_provisioner/ansible/templates/nextjs_proxy.conf.j2
```

---

## Why the Agent Writes to `nginx_directory/hosts/`

Press's `NginxReloadManager` monitors `{nginx_directory}/hosts/` on the app server.
When a new `.conf` file appears there, it:
1. Syncs the config to the proxy server(s) via its own mechanism
2. Tests and reloads nginx on the proxy

This means writing to that directory automatically triggers proxy-side nginx reload —
no direct SSH to the proxy is needed from `frontend_patch.py`.

If `NginxReloadManager` is not available (e.g. older agent version), the fallback
`systemctl reload nginx` runs locally, which works when proxy and app server are
co-located on the same machine.
