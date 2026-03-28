"""
frontend_patch.py
-----------------
Monkey-patches agent/frontend.py's Frontend class to support
deployment_mode and backend_url WITHOUT replacing the class or
its job_record initialization pipeline.

Applied automatically on import. Add one line to web.py imports:

    import agent.frontend_patch  # noqa: F401
"""
from __future__ import annotations
import os
import json as _json


def _read_nginx_dir() -> str:
    for path in ("/var/frappe/agent/config.json", "/home/frappe/agent/config.json"):
        try:
            with open(path) as f:
                return _json.load(f).get("nginx_directory", "/etc/nginx/conf.d")
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return "/etc/nginx/conf.d"


def _write_nginx(site_name: str, port: int,
                 deployment_mode: str = "Full Stack",
                 backend_url: str = ""):
    conf_dir = _read_nginx_dir()
    try:
        from agent.nginx_utils import write_upstream
        write_upstream(
            site_name=site_name,
            container_name=site_name,
            port=port,
            conf_dir=conf_dir,
            deployment_mode=deployment_mode,
            backend_url=backend_url,
        )
    except Exception as exc:
        print(f"[NFP] nginx config warning for {site_name}: {exc}")


def _patched_deploy_frontend_job(self, repo, branch, port, env_vars=None,
                                  deployment_mode="Full Stack", backend_url=""):
    """
    Replacement for Frontend.deploy_frontend_job.
    Accepts the extra deployment_mode and backend_url params and passes
    them through to deploy_frontend.
    """
    self.deploy_frontend(repo, branch, port, env_vars, deployment_mode, backend_url)
    return {"status": "Success"}


def _patched_deploy_frontend(self, repo, branch, port, env_vars=None,
                              deployment_mode="Full Stack", backend_url=""):
    """
    Replacement for Frontend.deploy_frontend.
    Adds mode-aware nginx config after container start.
    """
    work_dir  = os.path.join("/tmp", self.name)
    image_tag = f"frontend-{self.name.lower()}:latest"

    # 1. Clone or pull
    if os.path.exists(work_dir):
        self.execute(f"git -C {work_dir} fetch origin {branch}")
        self.execute(f"git -C {work_dir} checkout {branch}")
        self.execute(f"git -C {work_dir} pull origin {branch}")
    else:
        self.execute(f"git clone --branch {branch} {repo} {work_dir}")

    # 2. Build
    self.execute(f"docker build -t {image_tag} {work_dir}")

    # 3. Stop old container
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

    # 5. Write nginx config
    _write_nginx(self.name, port, deployment_mode, backend_url)


def _patched_remove_frontend(self):
    """Replacement for Frontend.remove_frontend — also removes nginx config."""
    try:
        self.execute(f"docker stop {self.name}", non_zero_throw=False)
        self.execute(f"docker rm   {self.name}", non_zero_throw=False)
    except Exception:
        pass
    # Remove nginx config
    conf_dir = _read_nginx_dir()
    conf_file = os.path.join(conf_dir, f"{self.name}.nextjs.conf")
    try:
        if os.path.exists(conf_file):
            os.remove(conf_file)
            try:
                import subprocess
                subprocess.run(["nginx", "-s", "reload"], check=True, capture_output=True)
            except Exception:
                pass
    except Exception as exc:
        print(f"[NFP] nginx removal warning for {self.name}: {exc}")


def apply_patch():
    try:
        from agent.frontend import Frontend
        from agent.job import job, step

        # Patch deploy_frontend_job (@job decorator) — adds deployment_mode + backend_url params
        Frontend.deploy_frontend_job = job("Deploy Frontend")(_patched_deploy_frontend_job)

        # Patch deploy_frontend (@step decorator) — adds nginx config
        Frontend.deploy_frontend = step("Deploy Frontend")(_patched_deploy_frontend)

        # Patch remove_frontend (@step decorator) — adds nginx cleanup
        Frontend.remove_frontend = step("Remove Frontend Container")(_patched_remove_frontend)

        print("[NFP] frontend_patch applied successfully")
    except ImportError as exc:
        print(f"[NFP] frontend_patch skipped: {exc}")


apply_patch()
