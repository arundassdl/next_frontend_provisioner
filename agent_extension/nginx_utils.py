"""
nginx_utils.py
--------------
Writes nginx upstream configs on the PROXY server's nginx directory.

Key design decisions:
  - upstream uses host_ip:port (the app server's reachable IP + host port)
    NOT container_name:3000 (which is only resolvable inside the app server)
  - conf_dir is the agent's nginx_directory (e.g. /home/frappe/agent/nginx/hosts)
    which Press's NginxReloadManager monitors
  - _reload_nginx() uses NginxReloadManager.request_reload() (Press pattern)
    falling back to systemctl if not available

Modes:
  Full Stack    — all traffic routed to the Next.js container
  Frontend Only — /api /files /private proxied to existing Frappe backend;
                  everything else goes to the Next.js container
"""
import os
import subprocess
from pathlib import Path
from string import Template
from urllib.parse import urlparse


# ── Templates ─────────────────────────────────────────────────────────
# CRITICAL: upstream uses app_server_ip:port — NOT container_name:3000.
# container_name is only resolvable on the app server's Docker network.
# From the proxy, we must use the app server's IP and the host-bound port.

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
    app_server_ip: str,
    port: int,
    conf_dir: str,
    deployment_mode: str = "Full Stack",
    backend_url: str = "",
) -> str:
    """
    Render and write the nginx upstream config for site_name.

    Args:
        site_name:       Domain / server_name (e.g. "crm.evoq.app").
        app_server_ip:   App server's IP as seen FROM the proxy.
                         Use "127.0.0.1" when proxy and app server are co-located.
        port:            Host port the Docker container is bound to (e.g. 3100).
        conf_dir:        Directory to write .conf file into. Should be the
                         agent's nginx_directory so NginxReloadManager picks it up.
        deployment_mode: "Full Stack" or "Frontend Only".
        backend_url:     Required for Frontend Only mode.

    Returns:
        Absolute path of the written config file.
    """
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


def remove_upstream(site_name: str, conf_dir: str) -> None:
    path = os.path.join(conf_dir, f"{site_name}.nextjs.conf")
    if os.path.exists(path):
        os.remove(path)
        print(f"[NFP] nginx config removed: {path}")
    try:
        _reload_nginx()
    except Exception as exc:
        print(f"[NFP] nginx reload warning on remove: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def _reload_nginx() -> None:
    """
    Reload nginx. Tries NginxReloadManager first (Press pattern),
    then falls back to systemctl.
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

    # Attempt 2: systemctl
    try:
        subprocess.run(["nginx", "-t"], check=True, capture_output=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True, capture_output=True)
        print("[NFP] nginx reloaded via systemctl")
        return
    except Exception as exc:
        print(f"[NFP] systemctl reload failed: {exc}")

    # Attempt 3: nginx -s reload
    try:
        subprocess.run(["nginx", "-s", "reload"], check=True, capture_output=True)
        print("[NFP] nginx reloaded via nginx -s reload")
    except Exception as exc:
        print(f"[NFP] nginx -s reload also failed (non-fatal): {exc}")
