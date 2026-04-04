"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application.

ARCHITECTURE
============

Single-server deployment (f1.evoq.app = n1.evoq.app = same machine):
  - Docker containers run on f1, bound to 127.0.0.1:<port>:3000
  - nginx proxy.conf routes <domain> → nextjs_<safe> upstream → port
  - NginxReloadManager regenerates proxy.conf from Proxy() state
    periodically, wiping our custom upstream blocks

PERMANENT SOLUTION
==================

1. PERSISTENT REGISTRY — nfp_sites.json
   Stores {domain: port} for all deployed NFP sites.
   Written on every deploy/remove. Re-read on every agent startup.
   Location: nginx_directory/nfp_sites.json

2. MONKEY-PATCH Proxy._generate_proxy_config
   After every regeneration, immediately re-applies all registered
   NFP upstream blocks from nfp_sites.json. This runs in the
   nginx_reload_manager supervisor process as well as the web/worker
   processes, ensuring patches survive regardless of which process
   triggers a reload.

3. DIRECT NGINX RELOAD after patching
   Uses sudo nginx -s reload (not NginxReloadManager) so the patch
   takes effect without triggering another regeneration cycle.

4. PORT ISOLATION
   - Containers always start on PORT=3000 internally
   - Host ports auto-allocated from PORT_BASE=3100
   - PORT env var from Press (host port) is stripped before docker run
   - Next.js env var NEXT_PUBLIC_* vars are passed as build-args only

SUDOERS (required once):
   echo "frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload" \\
     | sudo tee /etc/sudoers.d/frappe-nginx-reload
   sudo chmod 440 /etc/sudoers.d/frappe-nginx-reload
"""
from __future__ import annotations

import fcntl
import glob
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
_bp = Blueprint("nfp_frontends", __name__)

# ── Constants ─────────────────────────────────────────────────────────
PORT_BASE = 3100          # First host port; increments per site
PORT_MAX  = 3199          # Upper bound (100 sites)
CONTAINER_PORT = 3000     # Next.js always listens on 3000 inside container

# Keys that must never be forwarded into the container as env vars
_BLOCKED_ENV_KEYS = {"PORT", "port", "HOST", "host", "HOSTNAME"}


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
    return os.path.join(_nginx_dir(), "proxy.conf")


def _registry_path() -> str:
    """Persistent JSON file: {domain: port} for all NFP sites."""
    return os.path.join(_nginx_dir(), "nfp_sites.json")


# ── Persistent registry ───────────────────────────────────────────────

def _registry_load() -> dict:
    """Load {domain: port} registry from disk. Thread-safe."""
    path = _registry_path()
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = _json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _registry_save(registry: dict) -> None:
    """Save {domain: port} registry to disk atomically. Thread-safe."""
    path = _registry_path()
    tmp  = path + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _json.dump(registry, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, path)


def _registry_add(domain: str, port: int) -> None:
    reg = _registry_load()
    reg[domain] = port
    _registry_save(reg)
    print(f"[NFP] Registry: {domain} → port {port} saved ✓")


def _registry_remove(domain: str) -> None:
    reg = _registry_load()
    if domain in reg:
        del reg[domain]
        _registry_save(reg)
        print(f"[NFP] Registry: {domain} removed ✓")


def _allocate_port(requested_port: int = 0) -> int:
    """
    Return a host port for a new site.
    If requested_port is in [PORT_BASE, PORT_MAX] and free → use it.
    Otherwise auto-allocate the next free port from PORT_BASE.
    """
    reg = _registry_load()
    used = set(reg.values())

    if PORT_BASE <= requested_port <= PORT_MAX and requested_port not in used:
        return requested_port

    for p in range(PORT_BASE, PORT_MAX + 1):
        if p not in used:
            return p

    raise RuntimeError(
        f"No free ports available in range {PORT_BASE}–{PORT_MAX}. "
        f"Currently used: {sorted(used)}"
    )


# ── Docker / shell helpers ────────────────────────────────────────────

def _run(cmd: str, check: bool = True) -> str:
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


# ── Proxy() state cleanup ─────────────────────────────────────────────

def _cleanup_proxy_state(domain: str) -> None:
    """
    Remove any Proxy() state files for this domain (from old versions
    of this code that wrote to upstreams/ and map.json).

    These cause Proxy() to emit a $actual_host map entry that transforms
    $host → "127.0.0.1", breaking upstream lookup entirely.
    """
    nginx_dir = _nginx_dir()

    for upstream_dir in glob.glob(os.path.join(nginx_dir, "upstreams", "*")):
        site_file = os.path.join(upstream_dir, domain)
        if os.path.isfile(site_file):
            try:
                os.remove(site_file)
                print(f"[NFP] Cleaned stale Proxy() state: {site_file}")
            except OSError as e:
                print(f"[NFP] Warning removing {site_file}: {e}")

    for map_path in glob.glob(os.path.join(nginx_dir, "hosts", "*", "map.json")):
        try:
            with open(map_path) as f:
                data = _json.load(f)
            if domain in data:
                del data[domain]
                with open(map_path, "w") as f:
                    _json.dump(data, f, indent=4)
                print(f"[NFP] Cleaned stale map.json entry: {domain} in {map_path}")
        except Exception as e:
            print(f"[NFP] Warning updating {map_path}: {e}")


# ── Nginx proxy.conf patching ─────────────────────────────────────────

def _safe_name(domain: str) -> str:
    return domain.replace(".", "_").replace("-", "_")


def _clean_stale_conf_files(domain: str) -> None:
    """Remove stale .nextjs.conf files left by old versions of this code."""
    nginx_dir = _nginx_dir()
    hosts_dir = os.path.join(nginx_dir, "hosts")
    subdomain  = domain.split(".")[0]
    for path in [
        os.path.join(nginx_dir, f"{subdomain}.nextjs.conf"),
        os.path.join(nginx_dir, f"{domain}.nextjs.conf"),
        os.path.join(hosts_dir, f"{subdomain}.nextjs.conf"),
        os.path.join(hosts_dir, f"{domain}.nextjs.conf"),
        os.path.join(hosts_dir, f"{domain}.conf"),
    ]:
        if os.path.isfile(path):
            try:
                os.remove(path)
                print(f"[NFP] Removed stale conf file: {path}")
            except OSError as e:
                print(f"[NFP] Warning removing {path}: {e}")


def _apply_patch_to_content(content: str, domain: str, port: int) -> str:
    """
    Apply NFP upstream + map entries for one domain to proxy.conf content.
    Returns the patched content string. Idempotent.
    """
    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    upstream_block = (
        f"upstream {upstream_name} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )
    map_entry = f"\t{domain} http://{upstream_name};"

    # Skip if already correctly patched
    if (f"server 127.0.0.1:{port};" in content and
            f"{domain} http://{upstream_name}" in content):
        return content

    # Remove stale entries
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content
    )
    # Remove any domain map entries (old upstream or wrong port)
    content = re.sub(
        rf"^\s*{re.escape(domain)}\s+\S+;\s*\n?", "", content, flags=re.MULTILINE
    )

    # Insert upstream block before first existing upstream
    first = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first:
        content = (
            content[:first.start()] + upstream_block + "\n" + content[first.start():]
        )
    else:
        content = upstream_block + "\n" + content

    # Add to $upstream_server_hash (first occurrence of default)
    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,
    )

    # Add to $socket_upstream_hash
    socket = re.search(
        r"(map \$actual_host \$socket_upstream_hash \{[^}]*?)"
        r"(default http://site_not_found;)",
        content, re.DOTALL,
    )
    if socket:
        entry = f"\t{domain} http://{upstream_name};\n"
        if entry.strip() not in content:
            pos = socket.start(2)
            content = content[:pos] + entry + "    " + content[pos:]

    return content


def _patch_proxy_conf(domain: str, port: int) -> bool:
    """Patch proxy.conf on disk for one domain. Returns True on success."""
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        print(f"[NFP] proxy.conf not found at {proxy_conf}")
        return False

    content = open(proxy_conf).read()
    content = _apply_patch_to_content(content, domain, port)
    open(proxy_conf, "w").write(content)
    print(f"[NFP] proxy.conf patched: {domain} → nextjs_{_safe_name(domain)} port {port} ✓")
    return True


def _patch_proxy_conf_all() -> None:
    """
    Re-apply ALL registered NFP sites to proxy.conf in one pass.
    Called after every NginxReloadManager cycle.
    """
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        return

    registry = _registry_load()
    if not registry:
        return

    content = open(proxy_conf).read()
    for domain, port in registry.items():
        content = _apply_patch_to_content(content, domain, port)

    open(proxy_conf, "w").write(content)
    print(f"[NFP] proxy.conf re-patched for {list(registry)} ✓")


def _unpatch_proxy_conf(domain: str) -> None:
    """Remove all NFP entries for a domain from proxy.conf."""
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        return
    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    content = open(proxy_conf).read()
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content
    )
    content = re.sub(
        rf"^\s*{re.escape(domain)}\s+\S+;\s*\n?", "", content, flags=re.MULTILINE
    )
    open(proxy_conf, "w").write(content)
    print(f"[NFP] proxy.conf: removed all entries for {domain}")


# ── Nginx reload ──────────────────────────────────────────────────────

def _reload_nginx_direct() -> bool:
    """
    Reload nginx directly WITHOUT triggering NginxReloadManager.
    Uses sudo (requires sudoers entry). Preserves our proxy.conf patch.
    """
    for cmd in [
        ["sudo", "/usr/sbin/nginx", "-s", "reload"],
        ["sudo", "nginx", "-s", "reload"],
        ["/usr/sbin/nginx", "-s", "reload"],
        ["nginx", "-s", "reload"],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            stderr = r.stderr.strip()
            is_fatal = r.returncode != 0 and (
                "emerg" in stderr or "[error]" in stderr.lower()
            )
            if not is_fatal:
                print(f"[NFP] nginx reloaded via: {' '.join(cmd)} ✓")
                return True
            print(f"[NFP] nginx reload failed ({' '.join(cmd)}): {stderr[:200]}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print("[NFP] WARNING: all nginx reload methods failed")
    return False


# ── Proxy() monkey-patch ──────────────────────────────────────────────

def _install_proxy_patch() -> None:
    """
    Monkey-patch Proxy._generate_proxy_config so that after EVERY
    NginxReloadManager cycle, all NFP sites are immediately re-patched
    from the persistent nfp_sites.json registry.

    This runs in EVERY process that imports this module:
      - agent-web (gunicorn workers)
      - agent-worker-0/1 (rq workers)
      - nginx_reload_manager (supervisor process)

    Since nginx_reload_manager.py imports agent.proxy directly, and
    web.py imports agent.frontend_patch which installs this patch, the
    monkey-patch is active in all three processes after agent restart.
    """
    try:
        from agent.proxy import Proxy

        if getattr(Proxy, "_nfp_patched", False):
            return

        _orig = Proxy._generate_proxy_config

        def _patched(self, *args, **kwargs):
            result = _orig(self, *args, **kwargs)
            # Re-apply all NFP sites from the persistent registry
            try:
                _patch_proxy_conf_all()
                _reload_nginx_direct()
            except Exception as exc:
                print(f"[NFP] Post-generate re-patch warning: {exc}")
            return result

        Proxy._generate_proxy_config = _patched
        Proxy._nfp_patched = True
        print("[NFP] Proxy._generate_proxy_config monkey-patched ✓")

    except Exception as exc:
        print(f"[NFP] Proxy monkey-patch skipped: {exc}")


# ── Main nginx write/remove ───────────────────────────────────────────

def _write_nginx(domain: str, port: int) -> None:
    """
    Full nginx routing setup for a Next.js domain:
      1. Clean stale files from old deploy approaches
      2. Remove any poisoned Proxy() state (map.json / upstreams/)
      3. Save to persistent registry (nfp_sites.json)
      4. Patch proxy.conf directly
      5. Reload nginx directly (not via NginxReloadManager)
    """
    print(f"[NFP] Configuring nginx: {domain} → 127.0.0.1:{port}")
    _clean_stale_conf_files(domain)
    _cleanup_proxy_state(domain)
    _registry_add(domain, port)
    _patch_proxy_conf(domain, port)
    _reload_nginx_direct()
    print(f"[NFP] nginx routing configured: {domain} → 127.0.0.1:{port} ✓")


def _remove_nginx(domain: str) -> None:
    """Remove domain from nginx routing."""
    _clean_stale_conf_files(domain)
    _cleanup_proxy_state(domain)
    _registry_remove(domain)
    _unpatch_proxy_conf(domain)
    # Let NginxReloadManager do a clean reload (our domain is gone from registry)
    try:
        from agent.nginx_reload_manager import NginxReloadManager
        NginxReloadManager().request_reload(request_id=f"nfp-rm-{os.getpid()}")
    except Exception:
        _reload_nginx_direct()
    print(f"[NFP] nginx routing removed: {domain}")


# ── JobModel helpers ──────────────────────────────────────────────────

def _job_model_create(job_id: str, label: str):
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
        url = (
            press_url
            + "/api/method/next_frontend_provisioner"
            + ".next_frontend_provisioner.api.agent_job_update"
        )
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
    RQ worker: clone → build → run container → configure nginx.

    Port handling:
      - `port` from Press is the requested HOST port (e.g. 3101)
      - We auto-allocate if not available or not in valid range
      - Container always runs PORT=3000 internally
      - Host binding: 127.0.0.1:<host_port>:3000
    """
    domain = site_name or name
    model  = _job_model_create(job_id, f"Deploy Frontend {name}")
    try:
        work_dir  = f"/tmp/nfp-{name}"
        image_tag = f"nfp-frontend-{name.lower()}:latest"

        # Allocate host port — use requested port if free, else auto-allocate
        host_port = _allocate_port(port)
        print(f"[NFP] Host port allocated: {host_port} (requested: {port})")

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

        # 3. Build Docker image (only NEXT_PUBLIC_* as build args)
        build_args = " ".join(
            f'--build-arg {k}="{str(v).replace(chr(34), chr(92)+chr(34))}"'
            for k, v in env_vars.items()
            if k.startswith("NEXT_PUBLIC_")
        )
        _run(f"docker build {build_args} -t {image_tag} {work_dir}")

        # 4. Stop old container and start new one
        _run(f"docker stop {name}", check=False)
        _run(f"docker rm   {name}", check=False)

        # Build env flags — strip PORT/HOST/HOSTNAME so Next.js uses PORT=3000
        env_flags = " ".join(
            f'-e {k}="{str(v).replace(chr(34), chr(92)+chr(34))}"'
            for k, v in env_vars.items()
            if k not in _BLOCKED_ENV_KEYS
        )
        _run(
            f"docker run -d --restart always"
            f" --name {name}"
            f" {env_flags}"
            f" -e PORT={CONTAINER_PORT}"          # Next.js always on 3000 inside
            f" -p 127.0.0.1:{host_port}:{CONTAINER_PORT}"
            f" {image_tag}"
        )

        # 5. Configure nginx routing
        _write_nginx(domain, host_port)

        _job_model_finish(model, success=True)
        _press_callback(
            job_id, domain, "Success",
            f"Container running: 127.0.0.1:{host_port}→{CONTAINER_PORT}"
        )
        print(f"[NFP] Deploy '{name}' ({domain}:{host_port}) completed successfully ✓")

    except Exception:
        tb = traceback.format_exc()
        print(f"[NFP] Deploy '{name}' FAILED:\n{tb}")
        _job_model_finish(model, success=False, tb=tb)
        _press_callback(job_id, domain, "Failure", tb[:300])
        raise


# ── RQ worker: remove ─────────────────────────────────────────────────

def _nfp_remove(name: str, job_id: str, site_name: str = ""):
    domain = site_name or name
    model  = _job_model_create(job_id, f"Remove Frontend {name}")
    try:
        _run(f"docker stop {name}", check=False)
        _run(f"docker rm   {name}", check=False)
        _remove_nginx(domain)
        _job_model_finish(model, success=True)
        print(f"[NFP] Remove '{name}' ({domain}) completed ✓")
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
    port            = int(data.get("port", PORT_BASE))
    env_vars        = data.get("env_vars") or data.get("env") or {}
    deployment_mode = data.get("deployment_mode", "Full Stack")
    backend_url     = data.get("backend_url", "")
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


@_bp.route("/frontends", methods=["GET"])
def nfp_list_frontends():
    """List all deployed NFP sites with their ports."""
    return jsonify(_registry_load())


# ── Register blueprint + install permanent monkey-patch ───────────────

def _register_blueprint():
    try:
        import sys
        web_module = sys.modules.get("agent.web")
        if web_module is None:
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

# Install monkey-patch — runs in web, worker, AND nginx_reload_manager processes
_install_proxy_patch()

# On startup: re-apply any existing NFP sites from registry to proxy.conf
# This handles the case where the agent restarted and proxy.conf was wiped
try:
    _registry = _registry_load()
    if _registry:
        print(f"[NFP] Startup: re-applying {len(_registry)} site(s) from registry: {list(_registry)}")
        _patch_proxy_conf_all()
        _reload_nginx_direct()
    else:
        print("[NFP] Startup: registry empty, nothing to re-apply")
except Exception as _exc:
    print(f"[NFP] Startup re-apply warning: {_exc}")
