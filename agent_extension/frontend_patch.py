"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application.

HOW NGINX ROUTING WORKS IN THIS SYSTEM
=======================================

The NginxReloadManager is a supervisor process that:
  1. Calls Proxy()._generate_proxy_config()
  2. Proxy() reads its state from disk:
       nginx/upstreams/<server_ip>/<site_domain>  (empty files)
       nginx/hosts/*.evoq.app/map.json            (domain → server_ip)
  3. Generates proxy.conf from that state
  4. Runs nginx -s reload (as root, with letsencrypt cert access)

Proxy() only knows about server IPs (port 80). It has no concept of
custom ports. So we cannot use Proxy() to route crm.evoq.app → port 3101.

THE CORRECT 4-STEP APPROACH
============================

Step 1 — Write Proxy() state files for our domain:
    touch nginx/upstreams/127.0.0.1/crm.evoq.app
    map.json: {"crm.evoq.app": "127.0.0.1", "default": "$host"}

  This ensures NginxReloadManager always includes crm.evoq.app in
  proxy.conf (pointing to the hashed 127.0.0.1 upstream at port 80).
  Without this, NginxReloadManager wipes our domain on every run.

Step 2 — Let NginxReloadManager regenerate proxy.conf with our domain:
    mgr.request_reload() → Proxy()._generate_proxy_config() runs
    proxy.conf now has crm.evoq.app → http://<hash_of_127.0.0.1>

Step 3 — Wait for reload to complete, then patch proxy.conf:
  Replace the hashed upstream entry for crm.evoq.app with our custom
  nextjs upstream block pointing to 127.0.0.1:3101.

Step 4 — Reload nginx DIRECTLY (nginx -s reload via sudo):
  This reloads the patched proxy.conf WITHOUT triggering NginxReloadManager
  again (which would wipe our port patch).

SUDOERS REQUIREMENT
===================
For step 4 to work without password prompt, add this line:

    echo "frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload" \
      | sudo tee /etc/sudoers.d/frappe-nginx-reload
    sudo chmod 440 /etc/sudoers.d/frappe-nginx-reload
"""
from __future__ import annotations

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


# ── Proxy() state management ──────────────────────────────────────────
#
# Proxy() reads its upstream state from the filesystem:
#   upstreams/<server_ip>/          → directory per upstream IP
#   upstreams/<server_ip>/<domain>  → empty file per site on that upstream
#   hosts/*.evoq.app/map.json       → {"domain": "server_ip", "default": "$host"}
#
# We use 127.0.0.1 as the upstream IP. Proxy() will route the domain to
# its hashed 127.0.0.1 upstream (port 80). We then patch the port.

_UPSTREAM_IP = "127.0.0.1"


def _write_proxy_state(domain: str) -> None:
    """
    Write Proxy() state files so NginxReloadManager permanently includes
    this domain in every proxy.conf regeneration.
    """
    nginx_dir    = _nginx_dir()
    upstream_dir = os.path.join(nginx_dir, "upstreams", _UPSTREAM_IP)
    os.makedirs(upstream_dir, exist_ok=True)

    # Empty file — Proxy() reads the filename as the site name
    site_file = os.path.join(upstream_dir, domain)
    open(site_file, "a").close()  # touch
    print(f"[NFP] Proxy state: upstreams/{_UPSTREAM_IP}/{domain} ✓")

    # map.json: domain → upstream IP
    map_files = (
        glob.glob(os.path.join(nginx_dir, "hosts", "*.evoq.app", "map.json")) +
        glob.glob(os.path.join(nginx_dir, "hosts", "*",           "map.json"))
    )
    # Deduplicate
    map_files = list(dict.fromkeys(map_files))

    if not map_files:
        print(f"[NFP] Warning: no map.json found under {nginx_dir}/hosts/")
        return

    map_path = map_files[0]
    try:
        with open(map_path) as f:
            map_data = _json.load(f)
    except (ValueError, FileNotFoundError):
        map_data = {"default": "$host"}

    if map_data.get(domain) != _UPSTREAM_IP:
        map_data[domain] = _UPSTREAM_IP
        with open(map_path, "w") as f:
            _json.dump(map_data, f, indent=4)
        print(f"[NFP] Proxy state: map.json → {domain}: {_UPSTREAM_IP} ✓")
    else:
        print(f"[NFP] Proxy state: map.json already has {domain}")


def _remove_proxy_state(domain: str) -> None:
    """Remove Proxy() state files for a domain on teardown."""
    nginx_dir = _nginx_dir()

    site_file = os.path.join(nginx_dir, "upstreams", _UPSTREAM_IP, domain)
    if os.path.exists(site_file):
        os.remove(site_file)
        print(f"[NFP] Proxy state: removed upstreams/{_UPSTREAM_IP}/{domain}")

    for map_path in glob.glob(os.path.join(nginx_dir, "hosts", "*", "map.json")):
        try:
            with open(map_path) as f:
                data = _json.load(f)
            if domain in data:
                del data[domain]
                with open(map_path, "w") as f:
                    _json.dump(data, f, indent=4)
                print(f"[NFP] Proxy state: removed {domain} from {map_path}")
        except Exception as exc:
            print(f"[NFP] Warning updating {map_path}: {exc}")


# ── Nginx reload helpers ──────────────────────────────────────────────

def _trigger_nginxreloadmanager_and_wait() -> bool:
    """
    Ask NginxReloadManager to regenerate proxy.conf (synchronous wait).
    Returns True if successful.

    After this, proxy.conf will contain our domain but pointing to the
    hashed 127.0.0.1 upstream (port 80). We fix the port next.
    """
    try:
        from agent.nginx_reload_manager import NginxReloadManager
        mgr = NginxReloadManager()
        mgr.request_reload(request_id=f"nfp-{os.getpid()}")
        print("[NFP] NginxReloadManager: reload requested")
        # Give it time to regenerate proxy.conf and reload nginx
        time.sleep(3)
        print("[NFP] NginxReloadManager: cycle complete")
        return True
    except Exception as exc:
        print(f"[NFP] NginxReloadManager unavailable: {exc}")
        return False


def _reload_nginx_direct() -> bool:
    """
    Reload nginx directly WITHOUT going through NginxReloadManager.
    Tries sudo (requires sudoers entry) then plain nginx -s reload.
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
            # returncode 0 = success; non-zero with only warnings = also success
            if r.returncode == 0 or (r.returncode != 0 and "emerg" not in stderr and "error:" not in stderr.lower()):
                print(f"[NFP] nginx reloaded via: {' '.join(cmd)}")
                return True
            print(f"[NFP] nginx reload failed ({' '.join(cmd)}): {stderr[:200]}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print("[NFP] WARNING: all nginx reload methods failed")
    return False


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
            except OSError as exc:
                print(f"[NFP] Warning removing {path}: {exc}")


def _patch_proxy_conf_port(domain: str, port: int) -> bool:
    """
    After NginxReloadManager regenerates proxy.conf, our domain points to
    the hashed 127.0.0.1 upstream (port 80). This function:

      1. Inserts upstream nextjs_<safe> { server 127.0.0.1:<port>; }
      2. Replaces all "<domain> http://..." map entries to point to our
         custom upstream instead of the hashed 127.0.0.1 one.

    This is idempotent and safe to call repeatedly.
    """
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        print(f"[NFP] proxy.conf not found at {proxy_conf}")
        return False

    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    upstream_block = (
        f"upstream {upstream_name} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )

    content = open(proxy_conf).read()

    # Remove any stale nextjs block for this domain
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?",
        "", content,
    )

    # Insert custom upstream before first existing upstream
    first = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first:
        content = content[:first.start()] + upstream_block + "\n" + content[first.start():]
    else:
        content = upstream_block + "\n" + content

    # Replace domain's map entries: whatever IP/upstream they point to → our upstream
    # This covers both $upstream_server_hash and $socket_upstream_hash maps
    replaced = re.subn(
        rf"(^\s*{re.escape(domain)}\s+)http://\S+;",
        rf"\g<1>http://{upstream_name};",
        content,
        flags=re.MULTILINE,
    )
    content, n_replaced = replaced

    if n_replaced == 0:
        # Domain wasn't in proxy.conf yet — add entries manually
        print(f"[NFP] {domain} not found in proxy.conf maps, adding manually")
        map_entry = f"\t{domain} http://{upstream_name};"
        content = content.replace(
            "\tdefault http://site_not_found;",
            f"{map_entry}\n\n\tdefault http://site_not_found;",
            1,
        )
        # Socket map
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

    with open(proxy_conf, "w") as f:
        f.write(content)
    print(f"[NFP] proxy.conf patched: {domain} → {upstream_name} port {port} ✓")
    return True


def _unpatch_proxy_conf(domain: str) -> None:
    """Remove a domain's custom upstream block and map entries."""
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        return
    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    content = open(proxy_conf).read()
    content = re.sub(rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content)
    content = re.sub(rf"^\s*{re.escape(domain)}\s+http://\S+;\n?", "", content, flags=re.MULTILINE)
    open(proxy_conf, "w").write(content)
    print(f"[NFP] proxy.conf: removed {domain} entries")


def _write_nginx(domain: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = "") -> None:
    """
    Full nginx routing setup. Steps:
      1. Clean stale .nextjs.conf files
      2. Write Proxy() state (upstreams dir + map.json)
      3. Trigger NginxReloadManager → proxy.conf regenerated (domain present, port 80)
      4. Patch proxy.conf → domain now points to port <port>
      5. Reload nginx directly (not via NginxReloadManager) → patch takes effect
    """
    print(f"[NFP] Setting up nginx: {domain} → 127.0.0.1:{port}")

    # 1. Remove stale files from old deploy approaches
    _clean_stale_conf_files(domain)

    # 2. Write Proxy() state — domain will survive future NginxReloadManager runs
    _write_proxy_state(domain)

    # 3. Let NginxReloadManager regenerate proxy.conf with our domain
    #    (it does a nginx reload here too, but with wrong port — that's OK)
    _trigger_nginxreloadmanager_and_wait()

    # 4. Patch proxy.conf to correct port
    _patch_proxy_conf_port(domain, port)

    # 5. Reload nginx directly so the port patch takes effect
    #    (does NOT go through NginxReloadManager, so proxy.conf stays patched)
    _reload_nginx_direct()

    print(f"[NFP] nginx routing configured: {domain} → 127.0.0.1:{port}")


def _remove_nginx(domain: str) -> None:
    """Remove domain from nginx routing."""
    _clean_stale_conf_files(domain)
    _remove_proxy_state(domain)
    _unpatch_proxy_conf(domain)
    # NginxReloadManager will regenerate proxy.conf cleanly (domain removed from state)
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
    domain = site_name or name
    model  = _job_model_create(job_id, f"Deploy Frontend {name}")
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

        # 5. Configure nginx routing
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
    domain = site_name or name
    model  = _job_model_create(job_id, f"Remove Frontend {name}")
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
