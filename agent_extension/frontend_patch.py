"""
frontend_patch.py
-----------------
Drop-in replacement for agent/frontend.py in your frappe/agent fork.

Changes from the original:
  - deploy_frontend_job / deploy_frontend accept deployment_mode + backend_url
  - After container start, writes nginx config via nginx_utils.write_upstream()
    Full Stack  : all traffic → container
    Frontend Only: /api|/files|/private → backend_url, rest → container
  - remove_frontend_job also removes the nginx config
"""
from __future__ import annotations

import os

from agent.base import Base
from agent.job import job, step


class Frontend(Base):
    def __init__(self, name):
        super().__init__()
        self.name      = name
        self.directory = os.getcwd()

    # ── Deploy ────────────────────────────────────────────────────────

    @job("Deploy Frontend")
    def deploy_frontend_job(self, repo, branch, port, env_vars=None,
                            deployment_mode="Full Stack", backend_url=""):
        self.deploy_frontend(repo, branch, port, env_vars,
                             deployment_mode, backend_url)
        return {"status": "Success"}

    @step("Deploy Frontend")
    def deploy_frontend(self, repo, branch, port, env_vars=None,
                        deployment_mode="Full Stack", backend_url=""):
        work_dir  = os.path.join("/tmp", self.name)
        image_tag = f"frontend-{self.name.lower()}:latest"

        # 1. Clone or update repo
        if os.path.exists(work_dir):
            self.execute(f"git -C {work_dir} fetch origin {branch}")
            self.execute(f"git -C {work_dir} checkout {branch}")
            self.execute(f"git -C {work_dir} pull origin {branch}")
        else:
            self.execute(f"git clone --branch {branch} {repo} {work_dir}")

        # 2. Build Docker image
        self.execute(f"docker build -t {image_tag} {work_dir}")

        # 3. Stop and remove existing container
        self.execute(f"docker stop {self.name}", non_zero_throw=False)
        self.execute(f"docker rm   {self.name}", non_zero_throw=False)

        # 4. Run container
        env_cmd = ""
        if env_vars:
            for key, value in env_vars.items():
                # Shell-safe quoting for values
                safe_val = str(value).replace('"', '\\"')
                env_cmd += f' -e {key}="{safe_val}"'

        self.execute(
            f"docker run -d --restart always "
            f"--name {self.name} "
            f"{env_cmd} "
            f"-p 127.0.0.1:{port}:3000 "
            f"{image_tag}"
        )

        # 5. Write nginx config (mode-aware)
        self._write_nginx(port, deployment_mode, backend_url)

    # ── Remove ────────────────────────────────────────────────────────

    @job("Remove Frontend")
    def remove_frontend_job(self):
        self.remove_frontend()
        return {"status": "Success"}

    @step("Remove Frontend Container")
    def remove_frontend(self):
        try:
            self.execute(f"docker stop {self.name}", non_zero_throw=False)
            self.execute(f"docker rm   {self.name}", non_zero_throw=False)
        except Exception:
            pass
        self._remove_nginx()

    # ── nginx helpers ─────────────────────────────────────────────────

    def _write_nginx(self, port: int,
                     deployment_mode: str = "Full Stack",
                     backend_url: str = ""):
        try:
            from agent.nginx_utils import write_upstream
            write_upstream(
                site_name       = self.name,
                container_name  = self.name,
                port            = port,
                deployment_mode = deployment_mode,
                backend_url     = backend_url,
            )
        except Exception as exc:
            # Non-fatal — container is running; nginx can be fixed manually
            print(f"[NFP] nginx config warning: {exc}")

    def _remove_nginx(self):
        try:
            from agent.nginx_utils import remove_upstream
            remove_upstream(self.name)
        except Exception:
            pass
