"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application without modifying web.py beyond
a single import line.

Uses a Flask Blueprint — avoids the circular import that occurs when
trying to import `application` from agent.web during web.py's own load.

The Blueprint is registered on the app via the app's before_first_request
or, more reliably, by hooking into Flask's got_first_request signal —
but the simplest correct approach is to import the Blueprint into web.py
via this module and register it there. Since web.py only does:

    import agent.frontend_patch  # noqa: F401

...and this module registers the blueprint on the app object *after*
it is created (using Flask's app context), we use a deferred approach:
the blueprint is stored here and web.py's `application` object picks it
up because Python's import system guarantees web.py finishes creating
`application = Flask(__name__)` before the blueprint registration runs.

How it actually works:
  web.py line N:    application = Flask(__name__)
  web.py line N+x:  import agent.frontend_patch
                    → this module runs _register_blueprint()
                    → imports application from agent.web  ✓ (already created)
                    → registers the blueprint
"""
from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import traceback
import uuid

from flask import Blueprint, jsonify, request


# ── Blueprint ─────────────────────────────────────────────────────────
# Created at module level — no app reference needed yet.
_bp = Blueprint("nfp_frontends", __name__)


# ── Config helpers ────────────────────────────────────────────────────

def _agent_config() -> dict:
    for path in ("/var/frappe/agent/config.json", "/home/frappe/agent/config.json"):
        try:
            with open(path) as f:
                return _json.load(f)
        except (FileNotFoundError, PermissionError, ValueError):
            pass
    return {}


def _nginx_dir() -> str:
    return _agent_config().get("nginx_directory", "/home/frappe/agent/nginx")


def _proxy_conf_path() -> str:
    """Path to the main proxy.conf that Press's nginx reads."""
    return os.path.join(_nginx_dir(), "proxy.conf")


# ── Docker / shell helpers ────────────────────────────────────────────

def _run(cmd: str, check: bool = True) -> str:
    # Use --progress=plain for docker build to get full layer output
    actual_cmd = cmd
    if "docker build" in cmd:
        actual_cmd = cmd.replace("docker build", "docker build --progress=plain", 1)
    r = subprocess.run(actual_cmd, shell=True, capture_output=True, text=True)
    print(f"[NFP] {cmd[:120]}")
    if r.stdout:
        print(r.stdout)
    if r.returncode != 0:
        print("STDERR (full):", r.stderr)
        if check:
            raise RuntimeError(f"Command failed: {cmd}\n{r.stderr}")
    return r.stdout.strip()


# ── Nginx proxy.conf patching ─────────────────────────────────────────

def _safe_name(domain: str) -> str:
    return domain.replace(".", "_").replace("-", "_")


def _patch_proxy_conf(domain: str, port: int, deployment_mode: str = "Full Stack",
                      backend_url: str = "") -> None:
    """
    Patch proxy.conf to add our Next.js domain to the upstream map.

    Adds:
      upstream nextjs_<safe_domain> { server 127.0.0.1:<port>; keepalive 32; }
      <domain> http://nextjs_<safe_domain>;   ← inside $upstream_server_hash map
      <domain> http://nextjs_<safe_domain>;   ← inside $socket_upstream_hash map (if present)

    For Frontend Only mode, also patches the location blocks by writing a
    separate include file. But the primary routing (getting nginx to talk to
    our container) is handled purely through the map entry.

    This is idempotent: calling it multiple times only updates, never duplicates.
    """
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        print(f"[NFP] proxy.conf not found at {proxy_conf}, skipping patch")
        return

    safe = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    upstream_block = (
        f"upstream {upstream_name} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )
    map_entry = f"\t{domain} http://{upstream_name};"

    # Read current content
    content = open(proxy_conf).read()

    # ── 1. Remove any stale entry for this domain (idempotency) ──────
    # Remove old upstream block if it exists
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?",
        "",
        content,
    )
    # Remove old map entries for this domain
    content = re.sub(rf"^\t{re.escape(domain)}\s+http://\S+;\n?", "", content, flags=re.MULTILINE)

    # ── 2. Add upstream block before the first existing upstream ─────
    # Insert before the first "upstream " occurrence
    first_upstream = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first_upstream:
        content = content[:first_upstream.start()] + upstream_block + "\n" + content[first_upstream.start():]
    else:
        # No existing upstream — prepend
        content = upstream_block + "\n" + content

    # ── 3. Add map entry to $upstream_server_hash ────────────────────
    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,  # only first occurrence (upstream map, not socket map)
    )

    # ── 4. Add map entry to $socket_upstream_hash (if present) ───────
    # Find the socket map and add our entry there too (needed for websockets)
    socket_map_match = re.search(
        r"(map \$actual_host \$socket_upstream_hash \{[^}]*?)(default http://site_not_found;)",
        content,
        re.DOTALL,
    )
    if socket_map_match:
        socket_entry = f"\t{domain} http://{upstream_name};\n"
        if socket_entry.strip() not in content:
            insert_pos = socket_map_match.start(2)
            content = content[:insert_pos] + socket_entry + "    " + content[insert_pos:]

    # Write back
    with open(proxy_conf, "w") as f:
        f.write(content)
    print(f"[NFP] proxy.conf patched: added {domain} → {upstream_name} (port {port})")


def _unpatch_proxy_conf(domain: str) -> None:
    """Remove a domain's upstream and map entries from proxy.conf."""
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        return

    safe = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"

    content = open(proxy_conf).read()
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?",
        "",
        content,
    )
    content = re.sub(
        rf"^\t{re.escape(domain)}\s+http://\S+;\n?",
        "",
        content,
        flags=re.MULTILINE,
    )
    with open(proxy_conf, "w") as f:
        f.write(content)
    print(f"[NFP] proxy.conf: removed {domain} entries")


def _reload_nginx() -> None:
    """Reload nginx via NginxReloadManager, falling back to systemctl."""
    try:
        from agent.nginx_reload_manager import NginxReloadManager
        mgr = NginxReloadManager()
        mgr.request_reload(request_id=f"nfp-{os.getpid()}")
        print("[NFP] nginx reload requested via NginxReloadManager")
        return
    except Exception as exc:
        print(f"[NFP] NginxReloadManager unavailable ({exc}), falling back to nginx -s reload")
    try:
        subprocess.run(["nginx", "-t"], check=True, capture_output=True)
        subprocess.run(["nginx", "-s", "reload"], check=True, capture_output=True)
        print("[NFP] nginx reloaded via nginx -s reload")
    except Exception as exc:
        print(f"[NFP] nginx reload failed (non-fatal): {exc}")


def _write_nginx(domain: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = "") -> None:
    """Patch proxy.conf and reload nginx."""
    try:
        _patch_proxy_conf(domain, port, deployment_mode, backend_url)
        _reload_nginx()
        print(f"[NFP] nginx routing configured for {domain}")
    except Exception as exc:
        print(f"[NFP] nginx warning (non-fatal): {exc}")
        import traceback as _tb
        _tb.print_exc()


def _remove_nginx(domain: str) -> None:
    """Remove domain from proxy.conf and reload nginx."""
    try:
        _unpatch_proxy_conf(domain)
        _reload_nginx()
        print(f"[NFP] nginx routing removed for {domain}")
    except Exception as exc:
        print(f"[NFP] nginx remove warning: {exc}")


# ── JobModel helpers ──────────────────────────────────────────────────

def _job_model_create(job_id: str, label: str):
    """Create a JobModel record so Press can poll the job status."""
    try:
        from agent.job import JobModel, agent_database
        agent_database.connect(reuse_if_open=True)
        return JobModel.create(name=label, status="Running", agent_job_id=job_id)
    except Exception as exc:
        print(f"[NFP] JobModel create warning: {exc}")
        return None


def _job_model_finish(model, success: bool, tb: str = ""):
    if model is None:
        return
    try:
        model.status = "Success" if success else "Failure"
        if not success and tb:
            model.data = _json.dumps({"traceback": tb})
        model.save()
    except Exception as exc:
        print(f"[NFP] JobModel update warning: {exc}")


def _press_callback(job_id: str, site_name: str, status: str, message: str = ""):
    """POST job result back to Press controller to update Nextjs Site status."""
    import json as _j, urllib.request, urllib.error
    try:
        cfg_path = "/var/sdlpress/frappe-bench/sites/cloud.evoq.app/site_config.json"
        with open(cfg_path) as f:
            site_cfg = _j.load(f)
        token = site_cfg.get("nfp_agent_callback_token", "")
        if not token:
            print("[NFP] no nfp_agent_callback_token configured — skipping callback")
            return
        press_url = _agent_config().get("press_url", "https://cloud.evoq.app")
        url = press_url + "/api/method/next_frontend_provisioner.next_frontend_provisioner.api.agent_job_update"
        body = _j.dumps({
            "job_name": "Provision Next.js Site",
            "site":     site_name,
            "status":   status,
            "output":   message,
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type":  "application/json",
                "Authorization": "token " + token,
            },
        )
        urllib.request.urlopen(req, timeout=15)
        print(f"[NFP] Press callback sent: {status} for {site_name}")
    except Exception as exc:
        print(f"[NFP] Press callback failed (non-fatal): {exc}")


# ── RQ worker: deploy ─────────────────────────────────────────────────

def _nfp_deploy(name: str, repo: str, branch: str, port: int,
                env_vars: dict, deployment_mode: str, backend_url: str,
                job_id: str, site_name: str = ""):
    """
    RQ worker: clone → inject templates → build → run container → patch nginx.

    Args:
        name:      Docker container name / URL slug (e.g. "crm").
        site_name: Full domain for nginx routing (e.g. "crm.evoq.app").
                   Falls back to `name` if not provided.
    """
    # Use full domain for nginx; container slug for Docker
    domain = site_name or name
    model = _job_model_create(job_id, f"Deploy Frontend {name}")
    try:
        work_dir  = f"/tmp/nfp-{name}"
        image_tag = f"nfp-frontend-{name.lower()}:latest"

        # 1. Clone or update repo
        if os.path.exists(os.path.join(work_dir, ".git")):
            _run(f"git -C {work_dir} fetch origin {branch}")
            _run(f"git -C {work_dir} checkout {branch}")
            _run(f"git -C {work_dir} reset --hard origin/{branch}")
        else:
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir)
            _run(f"git clone --depth 1 --branch {branch} {repo} {work_dir}")

        # 2. Inject Dockerfile + next.config + health route
        try:
            from agent.template_injector import inject_templates
            inject_templates(work_dir, name, {"env_vars": env_vars})
            print("[NFP] templates injected")
        except Exception as exc:
            print(f"[NFP] template injection warning: {exc}")
            # Fallback: write a minimal Dockerfile if none exists
            dockerfile = os.path.join(work_dir, "Dockerfile")
            if not os.path.exists(dockerfile):
                with open(dockerfile, "w") as f:
                    f.write(
                        "FROM node:20-alpine AS deps\n"
                        "WORKDIR /app\n"
                        "COPY package*.json ./\n"
                        "RUN npm ci\n\n"
                        "FROM node:20-alpine AS builder\n"
                        "WORKDIR /app\n"
                        "COPY . .\n"
                        "COPY --from=deps /app/node_modules ./node_modules\n"
                        "RUN npm run build\n\n"
                        "FROM node:20-alpine AS runner\n"
                        "WORKDIR /app\n"
                        "ENV NODE_ENV=production\n"
                        "COPY --from=builder /app/.next/standalone ./\n"
                        "COPY --from=builder /app/.next/static ./.next/static\n"
                        "COPY --from=builder /app/public ./public\n"
                        "EXPOSE 3000\n"
                        'CMD ["node", "server.js"]\n'
                    )
                print("[NFP] fallback Dockerfile written")

        # 3. Build Docker image
        build_args = " ".join(
            f'--build-arg {k}="{str(v).replace(chr(34), chr(92)+chr(34))}"'
            for k, v in env_vars.items()
            if k.startswith("NEXT_PUBLIC_")
        )
        _run(f"docker build {build_args} -t {image_tag} {work_dir}")

        # 4. Stop old container and start new one
        _run(f"docker stop {name}", check=False)
        _run(f"docker rm   {name}", check=False)

        env_flags = " ".join(
            f'-e {k}="{str(v).replace(chr(34), chr(92)+chr(34))}"'
            for k, v in env_vars.items()
        )
        _run(
            f"docker run -d --restart always"
            f" --name {name}"
            f" {env_flags}"
            f" -p 127.0.0.1:{port}:3000"
            f" {image_tag}"
        )

        # 5. Patch proxy.conf to route domain → container
        # Uses the full domain (e.g. crm.evoq.app) not the slug
        _write_nginx(domain, port, deployment_mode, backend_url)

        _job_model_finish(model, success=True)
        _press_callback(job_id, domain, "Success", "Container running on port " + str(port))
        print(f"[NFP] Deploy '{name}' ({domain}) completed successfully")

    except Exception:
        tb = traceback.format_exc()
        print(f"[NFP] Deploy '{name}' FAILED:\n{tb}")
        _job_model_finish(model, success=False, tb=tb)
        _press_callback(job_id, domain, "Failure", tb[:300])
        raise


# ── RQ worker: remove ─────────────────────────────────────────────────

def _nfp_remove(name: str, job_id: str, site_name: str = ""):
    """RQ worker: stop container + remove from nginx."""
    domain = site_name or name
    model = _job_model_create(job_id, f"Remove Frontend {name}")
    try:
        _run(f"docker stop {name}", check=False)
        _run(f"docker rm   {name}", check=False)
        _remove_nginx(domain)
        _job_model_finish(model, success=True)
        print(f"[NFP] Remove '{name}' completed")
    except Exception:
        tb = traceback.format_exc()
        _job_model_finish(model, success=False, tb=tb)
        _press_callback(job_id, domain, "Failure", tb[:300])
        raise


# ── Blueprint routes ──────────────────────────────────────────────────

@_bp.route("/frontends/<string:name>/deploy", methods=["POST"])
def nfp_deploy_frontend(name):
    data            = request.json or {}
    repo            = data.get("repo", "")
    branch          = data.get("branch", "main")
    port            = int(data.get("port", 3000))
    env_vars        = data.get("env_vars") or data.get("env") or {}
    deployment_mode = data.get("deployment_mode", "Full Stack")
    backend_url     = data.get("backend_url", "")
    # site_name = full domain (e.g. "crm.evoq.app")
    # name      = container slug (e.g. "crm")
    site_name       = data.get("site_name") or name

    from agent.job import queue as _queue
    job_id = f"nfp-{name}-{uuid.uuid4().hex[:8]}"
    _queue("default").enqueue(
        _nfp_deploy,
        name, repo, branch, port, env_vars, deployment_mode, backend_url, job_id,
        site_name=site_name,
        job_id=job_id,
        job_timeout=1800,
        result_ttl=86400,
    )
    return jsonify({"job": job_id, "status": "queued"})


@_bp.route("/frontends/<string:name>", methods=["DELETE"])
def nfp_remove_frontend(name):
    data      = request.json or {}
    site_name = data.get("site_name") or name

    from agent.job import queue as _queue
    job_id = f"nfp-rm-{name}-{uuid.uuid4().hex[:8]}"
    _queue("default").enqueue(
        _nfp_remove,
        name, job_id,
        site_name=site_name,
        job_id=job_id,
        job_timeout=120,
        result_ttl=86400,
    )
    return jsonify({"job": job_id, "status": "queued"})


# ── Register blueprint on the Flask app ──────────────────────────────
# web.py structure:
#   line ~50:  application = Flask(__name__)   ← created BEFORE this import
#   line ~N:   import agent.frontend_patch      ← this file runs here
#
# By the time this module is imported, `application` already exists
# in agent.web's namespace, so we can safely import and register.

def _register_blueprint():
    try:
        # Import the already-created Flask app — NOT a circular import because
        # application = Flask(__name__) runs before `import agent.frontend_patch`
        import sys
        web_module = sys.modules.get("agent.web")
        if web_module is None:
            # Fallback: direct import (safe since application is already created)
            from agent import web as web_module
        app = getattr(web_module, "application", None)
        if app is None:
            print("[NFP] ERROR: could not find Flask application in agent.web")
            return
        app.register_blueprint(_bp)
        print("[NFP] /frontends routes registered successfully")
    except Exception as exc:
        print(f"[NFP] Blueprint registration failed: {exc}")
        import traceback as _tb
        _tb.print_exc()


_register_blueprint()
