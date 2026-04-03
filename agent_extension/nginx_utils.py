"""
nginx_utils.py
--------------
Writes nginx upstream configs on the agent server's nginx directory.

Key design decisions:
  - upstream uses 127.0.0.1:port when proxy and app server are co-located
    (the normal Frappe Cloud single-server setup). Pass app_server_ip
    explicitly when running a separate proxy tier.
  - conf_dir should be the agent's nginx_directory (e.g. /home/frappe/agent/nginx)
    NOT the hosts/ subdirectory — files in hosts/ are for per-site SSL certs,
    not upstream configs. The correct pattern for NFP is to patch proxy.conf
    directly (see frontend_patch.py). This file is kept for the agent_jobs.py
    mixin path (Press-dispatched jobs).
  - _reload_nginx() uses NginxReloadManager.request_reload() (Press pattern)
    falling back to nginx -s reload if unavailable.

Modes:
  Full Stack    — all traffic routed to the Next.js container
  Frontend Only — /api /files /private proxied to existing Frappe backend;
                  everything else goes to the Next.js container

Signature compatibility:
  Both old callers (container_name=...) and new callers (app_server_ip=...)
  are accepted. container_name is ignored — the upstream always uses
  app_server_ip:port, which defaults to 127.0.0.1 for co-located deployments.
"""
import os
import subprocess
from pathlib import Path
from string import Template
from urllib.parse import urlparse


# ── Templates ─────────────────────────────────────────────────────────
# upstream uses app_server_ip:port (reachable from nginx).
# For co-located deployments this is 127.0.0.1:<host_port>.

_FULL_STACK_TMPL = Template("""\
# Managed by next_frontend_provisioner — do not edit manually.
# Site: $site_name  Mode: Full Stack
upstream nextjs_$safe_name {
    server $app_server_ip:$port;
    keepalive 32;
}

server {
    listen 80;
    server_name $site_name;

    location / {
        proxy_pass         http://nextjs_$safe_name;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $$http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $$host;
        proxy_set_header   X-Real-IP         $$remote_addr;
        proxy_set_header   X-Forwarded-For   $$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $$scheme;
        proxy_read_timeout    60s;
        proxy_connect_timeout 10s;
    }

    location /_next/static/ {
        proxy_pass http://nextjs_$safe_name/_next/static/;
        add_header Cache-Control "public, max-age=31536000, immutable";
        access_log off;
    }
}
""")


_FRONTEND_ONLY_TMPL = Template("""\
# Managed by next_frontend_provisioner — do not edit manually.
# Site: $site_name  Mode: Frontend Only  Backend: $backend_url
upstream nextjs_$safe_name {
    server $app_server_ip:$port;
    keepalive 32;
}

server {
    listen 80;
    server_name $site_name;

    # Proxy Frappe API + file calls to the existing backend
    location ~* ^/(api|files|private/files)/ {
        proxy_pass         $backend_url;
        proxy_http_version 1.1;
        proxy_set_header   Host              $backend_host;
        proxy_set_header   X-Real-IP         $$remote_addr;
        proxy_set_header   X-Forwarded-For   $$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $$scheme;
        proxy_read_timeout    60s;
        proxy_connect_timeout 10s;
    }

    # Everything else -> Next.js container
    location / {
        proxy_pass         http://nextjs_$safe_name;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $$http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $$host;
        proxy_set_header   X-Real-IP         $$remote_addr;
        proxy_set_header   X-Forwarded-For   $$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $$scheme;
        proxy_read_timeout    60s;
        proxy_connect_timeout 10s;
    }

    location /_next/static/ {
        proxy_pass http://nextjs_$safe_name/_next/static/;
        add_header Cache-Control "public, max-age=31536000, immutable";
        access_log off;
    }
}
""")


# ── Public API ────────────────────────────────────────────────────────

def write_upstream(
    site_name: str,
    port: int,
    conf_dir: str = "",
    app_server_ip: str = "127.0.0.1",
    container_name: str = "",       # legacy compat — accepted but ignored
    deployment_mode: str = "Full Stack",
    backend_url: str = "",
) -> str:
    """
    Render and write the nginx upstream config for site_name.

    Args:
        site_name:       Domain / server_name (e.g. "crm.evoq.app").
        port:            Host port the Docker container is bound to (e.g. 3100).
        conf_dir:        Directory to write .conf file into. Defaults to the
                         agent's nginx_directory (read from config.json).
                         Pass explicitly to override.
        app_server_ip:   App server's IP as seen from nginx.
                         Defaults to "127.0.0.1" for co-located deployments.
        container_name:  Ignored. Kept for backward compatibility with callers
                         that pass container_name=<name>. The upstream always
                         uses app_server_ip:port, not a Docker network name.
        deployment_mode: "Full Stack" or "Frontend Only".
        backend_url:     Required for Frontend Only mode.

    Returns:
        Absolute path of the written config file.
    """
    # Resolve conf_dir from agent config if not supplied
    if not conf_dir:
        conf_dir = _resolve_nginx_dir()

    safe = _safe(site_name)

    if deployment_mode == "Frontend Only":
        if not backend_url:
            raise ValueError("backend_url is required for Frontend Only mode")
        backend_url  = backend_url.rstrip("/")
        backend_host = urlparse(backend_url).netloc
        content = _FRONTEND_ONLY_TMPL.substitute(
            site_name=site_name,
            safe_name=safe,
            app_server_ip=app_server_ip,
            port=port,
            backend_url=backend_url,
            backend_host=backend_host,
        )
    else:
        content = _FULL_STACK_TMPL.substitute(
            site_name=site_name,
            safe_name=safe,
            app_server_ip=app_server_ip,
            port=port,
        )

    Path(conf_dir).mkdir(parents=True, exist_ok=True)
    path = os.path.join(conf_dir, f"{site_name}.nextjs.conf")
    Path(path).write_text(content)
    print(f"[NFP] nginx config written: {path}")

    _reload_nginx()
    return path


def remove_upstream(site_name: str, conf_dir: str = "") -> None:
    """
    Remove the nginx upstream config file for site_name and reload nginx.

    Args:
        site_name: Domain (e.g. "crm.evoq.app").
        conf_dir:  Directory containing the .conf file. Defaults to the
                   agent's nginx_directory (read from config.json).
    """
    if not conf_dir:
        conf_dir = _resolve_nginx_dir()

    path = os.path.join(conf_dir, f"{site_name}.nextjs.conf")
    if os.path.exists(path):
        os.remove(path)
        print(f"[NFP] nginx config removed: {path}")
    else:
        print(f"[NFP] nginx config not found (already removed?): {path}")

    try:
        _reload_nginx()
    except Exception as exc:
        print(f"[NFP] nginx reload warning on remove: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def _resolve_nginx_dir() -> str:
    """Read nginx_directory from agent config.json, with fallback."""
    for cfg_path in ("/var/frappe/agent/config.json", "/home/frappe/agent/config.json"):
        try:
            import json
            with open(cfg_path) as f:
                cfg = json.load(f)
            nginx_dir = cfg.get("nginx_directory")
            if nginx_dir:
                return nginx_dir
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return "/home/frappe/agent/nginx"


def _reload_nginx() -> None:
    """
    Reload nginx. Tries NginxReloadManager first (Press pattern, runs as root),
    then falls back to nginx -s reload.

    NginxReloadManager is preferred because it has permission to read
    /etc/letsencrypt certs that the frappe user cannot access directly.
    """
    # Attempt 1: Press NginxReloadManager
    try:
        from agent.nginx_reload_manager import NginxReloadManager
        mgr = NginxReloadManager()
        mgr.request_reload(request_id=f"nfp-{os.getpid()}")
        print("[NFP] nginx reload requested via NginxReloadManager")
        return
    except Exception as exc:
        print(f"[NFP] NginxReloadManager unavailable ({exc}), falling back")

    # Attempt 2: nginx -s reload
    try:
        r = subprocess.run(["nginx", "-s", "reload"], capture_output=True, text=True)
        if r.returncode == 0:
            print("[NFP] nginx reloaded via nginx -s reload")
        else:
            print(f"[NFP] nginx -s reload failed (non-fatal): {r.stderr.strip()}")
    except Exception as exc:
        print(f"[NFP] nginx -s reload error (non-fatal): {exc}")
