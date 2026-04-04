"""
frontend_patch.py
-----------------
Registers /frontends/<n>/deploy and /frontends/<n> (DELETE) routes
on the agent's Flask application.

NGINX STRATEGY — PURE proxy.conf PATCHING
==========================================

Root cause of all previous failures:
  NginxReloadManager calls Proxy()._generate_proxy_config() which reads
  state from nginx/upstreams/ and nginx/hosts/*/map.json and REGENERATES
  proxy.conf from scratch — wiping any direct patches.

  When we added crm.evoq.app to map.json pointing at 127.0.0.1, Proxy()
  generated a $actual_host map entry:
      crm.evoq.app 127.0.0.1;
  This transforms $host → "127.0.0.1" before the upstream lookup, which
  then resolves to upstream 2f8748abdbb42c16 (port 80) — breaking routing.

CORRECT APPROACH (used here):
  1. Do NOT touch Proxy() state files (upstreams/ or map.json).
     Proxy() state is for Frappe sites only — not Next.js containers.
  2. Patch proxy.conf AFTER NginxReloadManager has finished its cycle:
       a. Insert: upstream nextjs_<safe> { server 127.0.0.1:<port>; }
       b. Add/replace map entries in $upstream_server_hash
       c. Add/replace map entries in $socket_upstream_hash
       d. Remove any stale $actual_host map entry for this domain
          (Proxy() should not know about this domain at all)
  3. Reload nginx DIRECTLY via sudo (not via NginxReloadManager).
     Requires sudoers entry — see SUDOERS SETUP below.
  4. On the next unrelated NginxReloadManager run, our custom upstream
     block will be wiped again. This is acceptable because:
       - The state files don't exist → Proxy() won't add this domain
       - The next deploy of our site will re-patch proxy.conf
       - For long-running stability between deploys, see PERMANENT FIX.

PERMANENT FIX (optional, for production):
  Monkey-patch Proxy._generate_proxy_config to re-apply our custom
  upstream blocks after generation. See _install_proxy_patch() below.
  This ensures the patch survives ANY NginxReloadManager cycle.

SUDOERS SETUP (required once):
  echo "frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload" \
    | sudo tee /etc/sudoers.d/frappe-nginx-reload
  sudo chmod 440 /etc/sudoers.d/frappe-nginx-reload

CLEANUP REQUIRED (run once on server to fix poisoned state):
  python3 - << 'PY'
  import json, glob
  # Remove crm.evoq.app from map.json
  for p in glob.glob("/home/frappe/agent/nginx/hosts/*/map.json"):
      d = json.load(open(p))
      changed = False
      for k in list(d):
          if k not in ("default",) and not k.startswith("*"):
              # Remove NFP domains — Proxy() should not know about them
              pass  # only remove specific ones:
      if "crm.evoq.app" in d:
          del d["crm.evoq.app"]
          json.dump(d, open(p, "w"), indent=4)
          print("Cleaned map.json:", p)
  # Remove site file
  import os
  f = "/home/frappe/agent/nginx/upstreams/127.0.0.1/crm.evoq.app"
  if os.path.exists(f): os.remove(f); print("Removed:", f)
  PY
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

# ── In-memory registry of NFP domains → ports (for the monkey-patch) ─
# {domain: port}  e.g. {"crm.evoq.app": 3101}
_NFP_REGISTRY: dict[str, int] = {}


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


# ── Proxy() state cleanup ─────────────────────────────────────────────

def _cleanup_proxy_state(domain: str) -> None:
    """
    Remove any Proxy() state files for this domain.

    Previous versions of this code wrote:
      - upstreams/127.0.0.1/<domain>   (empty file)
      - hosts/*/map.json               (domain → 127.0.0.1 entry)

    These cause Proxy() to generate a $actual_host map entry that
    transforms $host → "127.0.0.1", breaking upstream lookup entirely.
    We must remove them so Proxy() never knows about NFP domains.
    """
    nginx_dir = _nginx_dir()

    # Remove site file from upstreams/
    for upstream_dir in glob.glob(os.path.join(nginx_dir, "upstreams", "*")):
        site_file = os.path.join(upstream_dir, domain)
        if os.path.isfile(site_file):
            try:
                os.remove(site_file)
                print(f"[NFP] Cleaned Proxy() state: removed {site_file}")
            except OSError as e:
                print(f"[NFP] Warning removing {site_file}: {e}")

    # Remove domain from map.json
    for map_path in glob.glob(os.path.join(nginx_dir, "hosts", "*", "map.json")):
        try:
            with open(map_path) as f:
                data = _json.load(f)
            if domain in data:
                del data[domain]
                with open(map_path, "w") as f:
                    _json.dump(data, f, indent=4)
                print(f"[NFP] Cleaned Proxy() state: removed {domain} from {map_path}")
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


def _patch_proxy_conf(domain: str, port: int) -> bool:
    """
    Patch proxy.conf to route domain → Next.js container port.

    Adds/updates:
      upstream nextjs_<safe> { server 127.0.0.1:<port>; keepalive 32; }
      <domain> http://nextjs_<safe>;  ← in $upstream_server_hash
      <domain> http://nextjs_<safe>;  ← in $socket_upstream_hash

    Also removes any stale $actual_host map entry for this domain
    (written by old versions of this code via map.json → Proxy()).

    Idempotent: safe to call on every deploy.
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
    map_entry = f"\t{domain} http://{upstream_name};"

    content = open(proxy_conf).read()

    # ── 1. Remove stale nextjs upstream block (idempotency) ──────────
    content = re.sub(
        rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?",
        "", content,
    )

    # ── 2. Remove stale $actual_host map entry (from old map.json approach) ──
    # This entry routes $host → "127.0.0.1" which breaks upstream lookup.
    content = re.sub(
        rf"^\s*{re.escape(domain)}\s+127\.0\.0\.1;\s*\n?",
        "", content, flags=re.MULTILINE,
    )

    # ── 3. Remove stale domain entries pointing to wrong upstream ─────
    # Replace any existing domain map entries (will re-add below)
    content = re.sub(
        rf"^\s*{re.escape(domain)}\s+http://\S+;\s*\n?",
        "", content, flags=re.MULTILINE,
    )

    # ── 4. Insert our custom upstream before first existing upstream ──
    first = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first:
        content = content[:first.start()] + upstream_block + "\n" + content[first.start():]
    else:
        content = upstream_block + "\n" + content

    # ── 5. Add entry to $upstream_server_hash ─────────────────────────
    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,  # first occurrence = upstream_server_hash map
    )

    # ── 6. Add entry to $socket_upstream_hash (websockets) ───────────
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
    """Remove all NFP entries for a domain from proxy.conf."""
    proxy_conf = _proxy_conf_path()
    if not os.path.exists(proxy_conf):
        return
    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    content = open(proxy_conf).read()
    content = re.sub(rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content)
    content = re.sub(rf"^\s*{re.escape(domain)}\s+\S+;\s*\n?", "", content, flags=re.MULTILINE)
    open(proxy_conf, "w").write(content)
    print(f"[NFP] proxy.conf: removed all entries for {domain}")


# ── Nginx reload ──────────────────────────────────────────────────────

def _reload_nginx_direct() -> bool:
    """
    Reload nginx directly WITHOUT NginxReloadManager.

    Uses sudo (requires sudoers entry). Falls back to plain nginx -s reload.
    Does NOT trigger NginxReloadManager, so proxy.conf stays patched.
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
            # returncode 0 = clean; non-zero with only ssl_stapling warnings = also OK
            is_fatal = r.returncode != 0 and (
                "emerg" in stderr or "[error]" in stderr.lower()
            )
            if not is_fatal:
                print(f"[NFP] nginx reloaded via: {' '.join(cmd)}")
                return True
            print(f"[NFP] nginx reload failed ({' '.join(cmd)}): {stderr[:300]}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print("[NFP] WARNING: all nginx reload methods failed")
    return False


# ── Proxy() monkey-patch (permanent fix) ─────────────────────────────

def _install_proxy_patch() -> None:
    """
    Monkey-patch Proxy._generate_proxy_config so that after every
    NginxReloadManager-triggered regeneration, our custom upstream
    blocks are immediately re-applied.

    This makes the routing survive ANY future NginxReloadManager cycle
    without requiring a re-deploy.
    """
    try:
        from agent.proxy import Proxy

        if getattr(Proxy, "_nfp_patched", False):
            return  # Already patched

        _original_generate = Proxy._generate_proxy_config

        def _patched_generate(self, *args, **kwargs):
            # Run the original regeneration first
            result = _original_generate(self, *args, **kwargs)
            # Re-apply all registered NFP domains
            if _NFP_REGISTRY:
                conf_path = os.path.join(self.nginx_directory, "proxy.conf")
                try:
                    for nfp_domain, nfp_port in list(_NFP_REGISTRY.items()):
                        _patch_proxy_conf_in_file(conf_path, nfp_domain, nfp_port)
                    print(f"[NFP] Proxy patch re-applied for: {list(_NFP_REGISTRY)}")
                except Exception as exc:
                    print(f"[NFP] Proxy patch re-apply warning: {exc}")
            return result

        Proxy._generate_proxy_config = _patched_generate
        Proxy._nfp_patched = True
        print("[NFP] Proxy._generate_proxy_config monkey-patched ✓")
    except Exception as exc:
        print(f"[NFP] Proxy monkey-patch failed (non-fatal): {exc}")


def _patch_proxy_conf_in_file(proxy_conf: str, domain: str, port: int) -> None:
    """Same as _patch_proxy_conf but operates on an explicit file path."""
    if not os.path.exists(proxy_conf):
        return
    safe          = _safe_name(domain)
    upstream_name = f"nextjs_{safe}"
    upstream_block = (
        f"upstream {upstream_name} {{\n"
        f"\tserver 127.0.0.1:{port};\n"
        f"\tkeepalive 32;\n"
        f"}}\n"
    )
    map_entry = f"\t{domain} http://{upstream_name};"
    content = open(proxy_conf).read()

    # Skip if already correctly patched (avoid redundant writes)
    if f"server 127.0.0.1:{port};" in content and f"{domain} http://{upstream_name}" in content:
        return

    content = re.sub(rf"upstream {re.escape(upstream_name)} \{{[^}}]*\}}\n?", "", content)
    content = re.sub(rf"^\s*{re.escape(domain)}\s+127\.0\.0\.1;\s*\n?", "", content, flags=re.MULTILINE)
    content = re.sub(rf"^\s*{re.escape(domain)}\s+http://\S+;\s*\n?", "", content, flags=re.MULTILINE)

    first = re.search(r"^upstream \w+", content, re.MULTILINE)
    if first:
        content = content[:first.start()] + upstream_block + "\n" + content[first.start():]
    else:
        content = upstream_block + "\n" + content

    content = content.replace(
        "\tdefault http://site_not_found;",
        f"{map_entry}\n\n\tdefault http://site_not_found;",
        1,
    )
    socket = re.search(
        r"(map \$actual_host \$socket_upstream_hash \{[^}]*?)(default http://site_not_found;)",
        content, re.DOTALL,
    )
    if socket:
        entry = f"\t{domain} http://{upstream_name};\n"
        if entry.strip() not in content:
            pos = socket.start(2)
            content = content[:pos] + entry + "    " + content[pos:]

    open(proxy_conf, "w").write(content)


# ── Main nginx write/remove ───────────────────────────────────────────

def _write_nginx(domain: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = "") -> None:
    """
    Configure nginx routing for a Next.js domain. Steps:
      1. Clean stale .nextjs.conf files from old deploy approaches
      2. Remove any Proxy() state files for this domain (map.json + upstreams/)
         so Proxy() never adds a $actual_host entry for it
      3. Patch proxy.conf directly with our custom upstream + map entries
      4. Reload nginx directly (not via NginxReloadManager)
      5. Register domain in _NFP_REGISTRY for the monkey-patch
    """
    print(f"[NFP] Configuring nginx: {domain} → 127.0.0.1:{port}")

    # 1. Clean stale files
    _clean_stale_conf_files(domain)

    # 2. Remove any Proxy() state that would poison $actual_host map
    _cleanup_proxy_state(domain)

    # 3. Patch proxy.conf
    _patch_proxy_conf(domain, port)

    # 4. Reload nginx directly (preserves our patch)
    _reload_nginx_direct()

    # 5. Register for monkey-patch persistence
    _NFP_REGISTRY[domain] = port

    print(f"[NFP] nginx routing configured: {domain} → 127.0.0.1:{port} ✓")


def _remove_nginx(domain: str) -> None:
    """Remove domain from nginx routing."""
    _clean_stale_conf_files(domain)
    _cleanup_proxy_state(domain)
    _unpatch_proxy_conf(domain)
    _NFP_REGISTRY.pop(domain, None)
    # Let NginxReloadManager do the final reload cleanly
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


# ── Register blueprint + install monkey-patch ─────────────────────────

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

# Install the Proxy() monkey-patch so our upstream blocks survive
# any future NginxReloadManager cycle automatically.
_install_proxy_patch()
