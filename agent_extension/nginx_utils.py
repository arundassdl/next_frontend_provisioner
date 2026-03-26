"""
nginx_utils.py
--------------
Helpers for rendering and writing nginx configuration on the
application server.  Imported by agent_extension/agent_jobs.py.
"""
import os
import subprocess
from pathlib import Path
from string import Template


_UPSTREAM_TMPL = Template("""\
# Managed by next_frontend_provisioner — do not edit manually.
# Site: $site_name
upstream nextjs_$safe_name {
    server $container_name:3000;
    keepalive 32;
}

server {
    listen 80;
    server_name $site_name;

    location /app/ {
        proxy_pass         http://nextjs_$safe_name/;
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
    }
}
""")


def write_upstream(site_name: str, container_name: str, port: int,
                   conf_dir: str = "/etc/nginx/conf.d") -> str:
    safe    = _safe(site_name)
    content = _UPSTREAM_TMPL.substitute(
        site_name=site_name, safe_name=safe, container_name=container_name, port=port
    )
    path = os.path.join(conf_dir, f"{site_name}.nextjs.conf")
    Path(path).write_text(content)
    _reload_nginx()
    return path


def remove_upstream(site_name: str, conf_dir: str = "/etc/nginx/conf.d"):
    path = os.path.join(conf_dir, f"{site_name}.nextjs.conf")
    if os.path.exists(path):
        os.remove(path)
    try:
        _reload_nginx()
    except subprocess.CalledProcessError:
        pass


def _safe(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")


def _reload_nginx():
    subprocess.run(["nginx", "-t"], check=True, capture_output=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True, capture_output=True)
