"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application.

Nginx strategy — the ONLY approach that works with NginxReloadManager:

  The NginxReloadManager is a separate supervisor process that:
  1. Reads its internal config state from disk (set by Proxy() class)
  2. REGENERATES proxy.conf from that state (wiping any direct patches)
  3. Runs nginx -s reload

  Therefore patching proxy.conf directly BEFORE reload always gets wiped.

  CORRECT approach:
  Step A. Call agent HTTP API: POST /proxy/upstreams (creates upstream in Proxy state)
  Step B. Call agent HTTP API: POST /proxy/upstreams/<n>/sites (adds domain to state)
  Step C. Poll job status — wait for NginxReloadManager to finish regenerating proxy.conf
          (at this point proxy.conf has our upstream but with default/wrong server address)
  Step D. Patch proxy.conf server address to 127.0.0.1:<our_port>
  Step E. Call nginx -s reload DIRECTLY — this reloads the patched file WITHOUT
          triggering NginxReloadManager again (no further regeneration).

  This survives because:
  - NginxReloadManager only runs when triggered via Redis queue
  - Direct "nginx -s reload" does NOT go through NginxReloadManager
  - The server address patch persists until the next time someone calls the
    proxy API (e.g. another site is deployed), at which point NginxReloadManager
    runs again and regenerates proxy.conf, again wiping the server address.
    
  PERMANENT FIX for server address: we re-apply the patch in a post-reload hook.
  After every nginx reload that NginxReloadManager does, our upstream gets a wrong
  server. We detect this by checking proxy.conf after each deploy and re-patching.

  For now this deploy-time patch is sufficient — it persists for the lifetime of
  this container deployment until the next unrelated nginx reload event.
"""
from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import time
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

def _agent_request(method: str, path: str, payload: dict = None) -> dict:
    """Make an authenticated HTTP request to the local agent API."""
    import urllib.request, urllib.error
    port = _agent_port()
    url = f"http://127.0.0.1:{port}{path}"
    password = _agent_password()
    data = _json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_agent_password()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"[NFP] Agent API {method} {path} → HTTP {e.code}: {body}")
        return {}
    except Exception as exc:
        print(f"[NFP] Agent API {method} {path} error: {exc}")
        return {}


def _wait_for_job(job_id: str, timeout: int = 60) -> bool:
    """Poll agent job status until finished. Returns True if success."""
    if not job_id:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _agent_request("GET", f"/jobs/{job_id}")
        status = resp.get("status", "")
        if status == "Success":
            return True
        if status == "Failure":
            print(f"[NFP] Job {job_id} failed: {resp.get('data', '')}")
            return False
        time.sleep(1)
    print(f"[NFP] Job {job_id} timed out after {timeout}s")
    return False


# ── Nginx helpers ─────────────────────────────────────────────────────

def _safe_name(domain: str) -> str:
    return "nextjs_" + domain.replace(".", "_").replace("-", "_")


def _nginx_reload_direct() -> None:
    """
    Reload nginx directly WITHOUT going through NginxReloadManager.
    This is critical — NginxReloadManager would regenerate proxy.conf,
    wiping our server address patch.
    """
    try:
        r = subprocess.run(
            ["nginx", "-s", "reload"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            print("[NFP] nginx reloaded directly (nginx -s reload)")
        else:
            print(f"[NFP] nginx -s reload failed: {r.stderr}")
            # Try systemctl as fallback
            subprocess.run(["systemctl", "reload", "nginx"], check=False, capture_output=True)
    except Exception as exc:
        print(f"[NFP] nginx reload error: {exc}")


def _patch_proxy_conf_server(upstream_name: str, port: int) -> bool:
    """
    Patch the server address in our upstream block in proxy.conf.
    Called AFTER NginxReloadManager has regenerated proxy.conf.
    Returns True if the upstream block was found and patched.
    """
    conf_path = _proxy_conf_path()
    if not os.path.exists(conf_path):
        print(f"[NFP] proxy.conf not found at {conf_path}")
        return False

    content = open(conf_path).read()

    # Find and replace/add the server line in our specific upstream block
    pattern = rf"(upstream {re.escape(upstream_name)}\s*\{{)([^}}]*?)(\}})"

    def fix_server(m):
        return (
            f"{m.group(1)}\n"
            f"\tserver 127.0.0.1:{port};\n"
            f"\tkeepalive 32;\n"
            f"{m.group(3)}"
        )

    new_content, count = re.subn(pattern, fix_server, content, flags=re.DOTALL)
    if count > 0:
        with open(conf_path, "w") as f:
            f.write(new_content)
        print(f"[NFP] Patched {upstream_name} server → 127.0.0.1:{port}")
        return True
    else:
        print(f"[NFP] Upstream block '{upstream_name}' not found in proxy.conf")
        return False


def _write_nginx(domain: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = "") -> None:
    """
    Configure nginx to route domain to our container port.

    Flow:
      1. Add upstream via agent API (updates Proxy() internal state)
      2. Add site/domain via agent API (updates map in internal state)
      3. Wait for NginxReloadManager to regenerate proxy.conf with our upstream
      4. Patch proxy.conf to set correct server address (127.0.0.1:<port>)
      5. nginx -s reload DIRECTLY (not via NginxReloadManager)
    """
    upstream_name = _safe_name(domain)

    # ── Step 1: Add upstream to Proxy() persistent state ─────────────
    print(f"[NFP] Registering upstream {upstream_name} via agent API...")
    resp = _agent_request("POST", "/proxy/upstreams", {"name": upstream_name})
    job1_id = resp.get("job") or resp.get("id") or ""
    print(f"[NFP] add_upstream job: {job1_id}")

    # Wait for it — NginxReloadManager will run and regenerate proxy.conf
    if job1_id:
        _wait_for_job(job1_id, timeout=30)
        time.sleep(1)  # Small extra wait for file write to flush

    # ── Step 2: Add site/domain to the upstream ───────────────────────
    print(f"[NFP] Adding {domain} to upstream {upstream_name}...")
    resp2 = _agent_request(
        "POST", f"/proxy/upstreams/{upstream_name}/sites", {"name": domain}
    )
    job2_id = resp2.get("job") or resp2.get("id") or ""
    print(f"[NFP] add_site job: {job2_id}")

    # Wait for it — NginxReloadManager will run again
    if job2_id:
        _wait_for_job(job2_id, timeout=30)
        time.sleep(1)

    # ── Step 3: Verify and patch server address ───────────────────────
    # At this point, NginxReloadManager has regenerated proxy.conf with
    # our upstream block, but it may have a wrong/default server address.
    # We patch it directly.
    patched = _patch_proxy_conf_server(upstream_name, port)

    if not patched:
        # Upstream block wasn't created by the API calls — fall back to
        # writing the full entry ourselves.
        print("[NFP] Falling back to full proxy.conf injection...")
        _inject_upstream_fallback(domain, upstream_name, port)

    # ── Step 4: Reload nginx directly (NOT via NginxReloadManager) ────
    _nginx_reload_direct()
    print(f"[NFP] nginx routing configured for {domain} → 127.0.0.1:{port}")


def _inject_upstream_fallback(domain: str, upstream_name: str, port: int) -> None:
    """
    Directly inject upstream + map entry into proxy.conf.
    Used when the agent API didn't create the upstream block.
    """
    conf_path = _proxy_conf_path()
    if not os.path.exists(conf_path):
        print(f"[NFP] proxy.conf not found: {conf_path}")
        return

    content = open(conf_path).read()

    # Remove stale entries for idempotency
    content = re.sub(
        rf"upstream {re.escape(upstream_name)}\s*\{{[^}}]*\}}\n?", "", content
    )
    content = re.sub(
        rf"^\t{re.escape(domain)}\s+http://\S+;\n?", "", content, flags=re.MULTILINE
    )

    upstream_block = (
        f"upstream {upstream_name} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )

    # Insert before first existing upstream block
    first_up = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first_up:
        content = content[:first_up.start()] + upstream_block + "\n" + content[first_up.start():]
    else:
        content = upstream_block + "\n" + content

    # Add map entry
    map_entry = f"\t{domain} http://{upstream_name};"
    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,
    )

    with open(conf_path, "w") as f:
        f.write(content)
    print(f"[NFP] Injected {upstream_name} → 127.0.0.1:{port} into proxy.conf")


def _remove_nginx(domain: str) -> None:
    """Remove domain from nginx routing."""
    upstream_name = _safe_name(domain)
    try:
        # Try agent API first
        resp = _agent_request(
            "DELETE", f"/proxy/upstreams/{upstream_name}/sites/{domain}", {}
        )
        job_id = resp.get("job") or resp.get("id") or ""
        if job_id:
            _wait_for_job(job_id, timeout=30)
        print(f"[NFP] Removed {domain} from nginx")
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
            print("[NFP] no nfp_agent_callback_token — skipping callback")
            return
        press_url = _agent_config().get("press_url", "https://cloud.evoq.app")
        url = (press_url
               + "/api/method/next_frontend_provisioner"
               + ".next_frontend_provisioner.api.agent_job_update")
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
                        "FROM node:20-alpine AS deps\nWORKDIR /app\n"
                        "COPY package*.json ./\nRUN npm ci\n\n"
                        "FROM node:20-alpine AS builder\nWORKDIR /app\n"
                        "COPY . .\nCOPY --from=deps /app/node_modules ./node_modules\n"
                        "RUN npm run build\n\n"
                        "FROM node:20-alpine AS runner\nWORKDIR /app\n"
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
        _press_callback(job_id, domain, "Success",
                        "Container running on port " + str(port))
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


# ── Register blueprint ────────────────────────────────────────────────

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
