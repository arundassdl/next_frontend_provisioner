"""
frontend_patch.py
-----------------
Monkey-patches the existing agent/frontend.py Frontend class to add
deployment_mode and backend_url support WITHOUT replacing the class.

This preserves the agent's job_record initialization pipeline — which
is what caused 'Frontend has no attribute job_record' when we replaced
the class entirely.

Apply by adding one import to /var/frappe/agent/repo/agent/web.py,
BEFORE the Frontend class is first used:

    # At the top of web.py, after existing imports:
    import agent.frontend_patch  # noqa: F401 — applies monkey-patch

Or call apply_patch() from an __init__ or startup hook.
"""
from __future__ import annotations
import os
import json as _json


def _read_nginx_dir() -> str:
    """Read nginx_directory from agent config.json."""
    for path in ("/var/frappe/agent/config.json", "/home/frappe/agent/config.json"):
        try:
            with open(path) as f:
                return _json.load(f).get("nginx_directory", "/etc/nginx/conf.d")
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return "/etc/nginx/conf.d"


def _patched_deploy_frontend(self, repo, branch, port, env_vars=None,
                              deployment_mode="Full Stack", backend_url=""):
    """
    Replacement for Frontend.deploy_frontend that adds:
      - deployment_mode awareness
      - mode-specific nginx config (Frontend Only proxies /api to backend_url)

    The @step decorator on the original is preserved because we copy it
    from the original method below.
    """
    work_dir  = os.path.join("/tmp", self.name)
    image_tag = f"frontend-{self.name.lower()}:latest"

    # 1. Clone or pull latest code
    if os.path.exists(work_dir):
        self.execute(f"git -C {work_dir} fetch origin {branch}")
        self.execute(f"git -C {work_dir} checkout {branch}")
        self.execute(f"git -C {work_dir} pull origin {branch}")
    else:
        self.execute(f"git clone --branch {branch} {repo} {work_dir}")

    # 2. Build Docker image
    self.execute(f"docker build -t {image_tag} {work_dir}")

    # 3. Stop and remove existing container (ignore errors if not running)
    self.execute(f"docker stop {self.name}", non_zero_throw=False)
    self.execute(f"docker rm   {self.name}", non_zero_throw=False)

    # 4. Start container
    env_cmd = ""
    if env_vars:
        for key, value in env_vars.items():
            safe_val = str(value).replace('"', '\\"')
            env_cmd += f' -e {key}="{safe_val}"'

    self.execute(
        f"docker run -d --restart always "
        f"--name {self.name} "
        f"{env_cmd} "
        f"-p 127.0.0.1:{port}:3000 "
        f"{image_tag}"
    )

    # 5. Write mode-aware nginx config
    _write_nginx(
        site_name       = self.name,
        port            = port,
        deployment_mode = deployment_mode,
        backend_url     = backend_url,
    )


def _write_nginx(site_name: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = ""):
    """Write nginx upstream config for the frontend."""
    conf_dir = _read_nginx_dir()
    try:
        from agent.nginx_utils import write_upstream
        write_upstream(
            site_name       = site_name,
            container_name  = site_name,
            port            = port,
            conf_dir        = conf_dir,
            deployment_mode = deployment_mode,
            backend_url     = backend_url,
        )
    except Exception as exc:
        # Non-fatal — container is running; nginx can be fixed separately
        print(f"[NFP] nginx config warning for {site_name}: {exc}")


def apply_patch():
    """
    Patch Frontend.deploy_frontend in-place.
    The @step decorator and job_record pipeline are untouched.
    Only the body of deploy_frontend is replaced.
    """
    try:
        from agent.frontend import Frontend
        from agent.job import step

        # Preserve the @step decorator by re-decorating our replacement
        decorated = step("Deploy Frontend")(_patched_deploy_frontend)
        Frontend.deploy_frontend = decorated
        print("[NFP] frontend_patch applied — deploy_frontend patched with mode-aware logic")
    except ImportError as exc:
        print(f"[NFP] frontend_patch skipped (import error): {exc}")


# Apply automatically on import
apply_patch()
