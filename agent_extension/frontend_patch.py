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


# ── Nginx helpers ─────────────────────────────────────────────────────

def _write_nginx(name: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = ""):
    try:
        from agent.nginx_utils import write_upstream
        write_upstream(
            site_name=name,
            container_name=name,
            port=port,
            conf_dir=_nginx_dir(),
            deployment_mode=deployment_mode,
            backend_url=backend_url,
        )
        print(f"[NFP] nginx config written for {name}")
    except Exception as exc:
        print(f"[NFP] nginx warning (non-fatal): {exc}")


def _remove_nginx(name: str):
    try:
        from agent.nginx_utils import remove_upstream
        remove_upstream(name, conf_dir=_nginx_dir())
        print(f"[NFP] nginx config removed for {name}")
    except Exception as exc:
        print(f"[NFP] nginx remove warning: {exc}")


# ── JobModel helpers ──────────────────────────────────────────────────

def _job_model_create(job_id: str, label: str):
    """Create a JobModel record so Press can poll the job status."""
    try:
        from agent.job import JobModel, agent_database
        agent_database.connect(reuse_if_open=True)
        return JobModel.create(
            name=label,
            status="Running",
            agent_job_id=job_id,
        )
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


# ── RQ worker: deploy ─────────────────────────────────────────────────

def _nfp_deploy(name: str, repo: str, branch: str, port: int,
                env_vars: dict, deployment_mode: str, backend_url: str,
                job_id: str):
    """
    RQ worker: clone → inject templates → build → run container → nginx.
    Runs inside the agent's RQ worker process.
    """
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

        # 3. Build Docker image (pass NEXT_PUBLIC_* as build args)
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

        # 5. Write nginx config
        _write_nginx(name, port, deployment_mode, backend_url)

        _job_model_finish(model, success=True)
        print(f"[NFP] Deploy '{name}' completed successfully")

    except Exception:
        tb = traceback.format_exc()
        print(f"[NFP] Deploy '{name}' FAILED:\n{tb}")
        _job_model_finish(model, success=False, tb=tb)
        raise


# ── RQ worker: remove ─────────────────────────────────────────────────

def _nfp_remove(name: str, job_id: str):
    """RQ worker: stop container + remove nginx config."""
    model = _job_model_create(job_id, f"Remove Frontend {name}")
    try:
        _run(f"docker stop {name}", check=False)
        _run(f"docker rm   {name}", check=False)
        _remove_nginx(name)
        _job_model_finish(model, success=True)
        print(f"[NFP] Remove '{name}' completed")
    except Exception:
        tb = traceback.format_exc()
        _job_model_finish(model, success=False, tb=tb)
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

    from agent.job import queue as _queue
    job_id = f"nfp-{name}-{uuid.uuid4().hex[:8]}"
    _queue("default").enqueue(
        _nfp_deploy,
        name, repo, branch, port, env_vars, deployment_mode, backend_url, job_id,
        job_id=job_id,
        job_timeout=1800,
        result_ttl=86400,
    )
    return jsonify({"job": job_id, "status": "queued"})


@_bp.route("/frontends/<string:name>", methods=["DELETE"])
def nfp_remove_frontend(name):
    from agent.job import queue as _queue
    job_id = f"nfp-rm-{name}-{uuid.uuid4().hex[:8]}"
    _queue("default").enqueue(
        _nfp_remove,
        name, job_id,
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
