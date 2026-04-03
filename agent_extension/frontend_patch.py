"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application without modifying web.py beyond
a single import line.

Nginx strategy — using the agent's own Proxy() API:
  Press manages proxy.conf through its Proxy() class, which maintains
  upstream state in a config file. NginxReloadManager reads this state
  to regenerate proxy.conf. We MUST use Proxy() methods — patching
  proxy.conf directly gets overwritten on every reload.

  The correct call sequence to add crm.evoq.app → port 3101:
    1. Proxy().add_upstream_job("nextjs_crm_evoq_app")
       → creates upstream block in proxy.conf
    2. Proxy().add_site_to_upstream_job("nextjs_crm_evoq_app", "crm.evoq.app")
       → adds crm.evoq.app to the $upstream_server_hash map

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


def _agent_password() -> str:
    """Read the agent's own password from config for self-calls."""
    return _agent_config().get("agent_password", "")


def _agent_port() -> int:
    return int(_agent_config().get("web_port", 25052))


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


# ── Nginx via Proxy() API ─────────────────────────────────────────────

def _safe_name(domain: str) -> str:
    """Convert domain to a valid nginx upstream name."""
    return "nextjs_" + domain.replace(".", "_").replace("-", "_")


def _call_agent_api(method: str, path: str, payload: dict = None) -> dict:
    """
    Call the local agent REST API (self-call).
    The agent authenticates with its own agent_password.
    """
    import urllib.request, urllib.error
    port = _agent_port()
    url = f"http://127.0.0.1:{port}{path}"
    password = _agent_password()
    data = _json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {password}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise RuntimeError(f"Agent API {method} {path} → HTTP {e.code}: {body}")


def _proxy_add_domain(domain: str, port: int) -> None:
    """
    Register domain with the agent's Proxy() upstream system.
    This writes to the persistent upstream config that proxy.conf is built from.

    Steps:
      1. Create upstream "nextjs_<safe>" pointing to 127.0.0.1:<port>
      2. Add site "<domain>" to that upstream (adds map entry)
    """
    upstream_name = _safe_name(domain)

    # ── Step 1: Create upstream (idempotent — agent handles duplicates) ──
    # We use Proxy() directly to avoid async job indirection
    try:
        from agent.proxy import Proxy
        proxy = Proxy()

        # Log current state for debugging
        try:
            all_upstreams = proxy.upstreams or []
            print(f"[NFP] Current upstreams: {[u.get('name') for u in all_upstreams]}")
        except Exception as e:
            print(f"[NFP] Could not list upstreams: {e}")
            all_upstreams = []

        # ── Step 1: Create upstream if it doesn't exist ───────────────────
        existing_names = {u.get("name") for u in all_upstreams}
        if upstream_name not in existing_names:
            # Try add_upstream (direct method, not the job wrapper add_upstream_job)
            try:
                proxy.add_upstream(upstream_name)
                print(f"[NFP] Created upstream: {upstream_name}")
            except AttributeError:
                # Method name may differ between agent versions
                try:
                    proxy.add_upstream_job(upstream_name)
                    print(f"[NFP] Created upstream via job: {upstream_name}")
                except Exception as e2:
                    print(f"[NFP] Could not create upstream: {e2}, using fallback")
                    raise
        else:
            print(f"[NFP] Upstream already exists: {upstream_name}")

        # ── Step 2: Add domain as a site (adds to $upstream_server_hash map) ─
        upstream_sites = set()
        for u in all_upstreams:
            if u.get("name") == upstream_name:
                upstream_sites = {s if isinstance(s, str) else s.get("name", "") 
                                  for s in u.get("sites", [])}
                break

        if domain not in upstream_sites:
            try:
                proxy.add_site_to_upstream(upstream_name, domain)
                print(f"[NFP] Added {domain} → {upstream_name}")
            except AttributeError:
                try:
                    proxy.add_site_to_upstream_job(upstream_name, domain)
                    print(f"[NFP] Added {domain} via job → {upstream_name}")
                except Exception as e2:
                    print(f"[NFP] Could not add site: {e2}, using fallback")
                    raise
        else:
            print(f"[NFP] {domain} already in upstream {upstream_name}")

        # ── Step 3: Patch the upstream server address to our container port ─
        # Proxy() creates the upstream with a default server (e.g. 127.0.0.1:80).
        # We overwrite it with our container port BEFORE nginx reload.
        _update_upstream_server(upstream_name, port)

        # ── Step 4: Reload nginx ──────────────────────────────────────────
        _reload_nginx()

    except ImportError:
        print("[NFP] agent.proxy not importable, falling back to direct proxy.conf patch")
        _patch_proxy_conf_fallback(domain, port)
        _reload_nginx()
    except Exception as exc:
        print(f"[NFP] Proxy() approach failed ({exc}), falling back to direct patch")
        import traceback as _tb2
        _tb2.print_exc()
        _patch_proxy_conf_fallback(domain, port)
        _reload_nginx()


def _update_upstream_server(upstream_name: str, port: int) -> None:
    """
    Set the upstream server address in proxy.conf to 127.0.0.1:<port>.
    
    The Proxy() class creates an upstream with a default or placeholder server.
    We need to ensure it points to our container's host port.
    We do this by directly patching the upstream block in proxy.conf,
    which is safe because we're only modifying the server address within
    an upstream block that Proxy() just created/confirmed exists.
    """
    conf_path = _proxy_conf_path()
    if not os.path.exists(conf_path):
        return

    content = open(conf_path).read()

    # Find and replace/add the server line in our specific upstream block
    pattern = rf"(upstream {re.escape(upstream_name)}\s*\{{)([^}}]*?)(\}})"

    def replace_server(m):
        block_open = m.group(1)
        block_body = m.group(2)
        block_close = m.group(3)

        # Remove existing server lines from this block
        new_body = re.sub(r'\n?\s*server [^\n]+;\n?', '', block_body)
        # Add our server line
        return f"{block_open}\n\tserver 127.0.0.1:{port};\n\tkeepalive 32;\n{block_close}"

    new_content, count = re.subn(pattern, replace_server, content, flags=re.DOTALL)
    if count > 0:
        with open(conf_path, "w") as f:
            f.write(new_content)
        print(f"[NFP] Updated upstream {upstream_name} → 127.0.0.1:{port}")
    else:
        print(f"[NFP] WARNING: upstream block {upstream_name} not found in proxy.conf")


def _proxy_remove_domain(domain: str) -> None:
    """Remove domain from the agent's upstream config."""
    upstream_name = _safe_name(domain)
    try:
        from agent.proxy import Proxy
        proxy = Proxy()
        try:
            proxy.remove_site_from_upstream(upstream_name, domain)
            print(f"[NFP] Removed {domain} from upstream {upstream_name}")
        except Exception:
            pass
        try:
            proxy.remove_upstream(upstream_name)
            print(f"[NFP] Removed upstream {upstream_name}")
        except Exception:
            pass
        _reload_nginx()
    except ImportError:
        _unpatch_proxy_conf_fallback(domain)
        _reload_nginx()


def _reload_nginx() -> None:
    """Reload nginx via NginxReloadManager."""
    try:
        from agent.nginx_reload_manager import NginxReloadManager
        mgr = NginxReloadManager()
        mgr.request_reload(request_id=f"nfp-{os.getpid()}")
        print("[NFP] nginx reload requested via NginxReloadManager")
        return
    except Exception as exc:
        print(f"[NFP] NginxReloadManager unavailable: {exc}")
    try:
        subprocess.run(["nginx", "-t"], check=True, capture_output=True)
        subprocess.run(["nginx", "-s", "reload"], check=True, capture_output=True)
        print("[NFP] nginx reloaded via nginx -s reload")
    except Exception as exc:
        print(f"[NFP] nginx reload failed (non-fatal): {exc}")


# ── Fallback: direct proxy.conf patching ─────────────────────────────
# Used only if Proxy() import fails (e.g. different agent version).

def _patch_proxy_conf_fallback(domain: str, port: int) -> None:
    """Direct proxy.conf patching — fallback when Proxy() is unavailable."""
    conf_path = _proxy_conf_path()
    if not os.path.exists(conf_path):
        print(f"[NFP] proxy.conf not found at {conf_path}")
        return

    safe = _safe_name(domain)
    upstream_block = (
        f"upstream {safe} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )
    map_entry = f"\t{domain} http://{safe};"

    content = open(conf_path).read()

    # Remove stale entries (idempotency)
    content = re.sub(rf"upstream {re.escape(safe)} \{{[^}}]*\}}\n?", "", content)
    content = re.sub(rf"^\t{re.escape(domain)}\s+http://\S+;\n?", "", content, flags=re.MULTILINE)

    # Insert upstream before first existing upstream
    first_up = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first_up:
        content = content[:first_up.start()] + upstream_block + "\n" + content[first_up.start():]
    else:
        content = upstream_block + "\n" + content

    # Add map entry before "default http://site_not_found;"
    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,
    )

    with open(conf_path, "w") as f:
        f.write(content)
    print(f"[NFP] proxy.conf patched (fallback): {domain} → {safe}:{port}")


def _unpatch_proxy_conf_fallback(domain: str) -> None:
    conf_path = _proxy_conf_path()
    if not os.path.exists(conf_path):
        return
    safe = _safe_name(domain)
    content = open(conf_path).read()
    content = re.sub(rf"upstream {re.escape(safe)} \{{[^}}]*\}}\n?", "", content)
    content = re.sub(rf"^\t{re.escape(domain)}\s+http://\S+;\n?", "", content, flags=re.MULTILINE)
    with open(conf_path, "w") as f:
        f.write(content)
    print(f"[NFP] proxy.conf unpatched (fallback): {domain}")


def _write_nginx(domain: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = "") -> None:
    """Configure nginx routing for the deployed domain."""
    try:
        _proxy_add_domain(domain, port)
        print(f"[NFP] nginx routing configured for {domain}")
    except Exception as exc:
        print(f"[NFP] nginx warning (non-fatal): {exc}")
        import traceback as _tb
        _tb.print_exc()


def _remove_nginx(domain: str) -> None:
    """Remove nginx routing for a domain."""
    try:
        _proxy_remove_domain(domain)
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
    RQ worker: clone → inject templates → build → run container → nginx.

    Args:
        name:      Docker container name / URL slug (e.g. "crm-evoq-app").
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
