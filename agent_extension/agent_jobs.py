"""
agent_jobs.py  →  copy to agent/nextjs_jobs.py
-----------------------------------------------
Mixin for frappe/agent's Server class.
Adds three @job methods that Press can dispatch:
  - provision_nextjs_site   ("Provision Next.js Site")
  - teardown_nextjs_site    ("Teardown Next.js Site")
  - redeploy_nextjs_site    ("Redeploy Next.js Site")

Wire into agent/server.py:

    from agent.nextjs_jobs import NextjsMixin

    class Server(NextjsMixin, Base):
        ...

The existing Server(Base) becomes Server(NextjsMixin, Base).
MRO keeps Base last so nothing breaks.
"""
from __future__ import annotations

import os
import subprocess
import time

import docker
import requests

from agent.job import job, step


# ── Shared helpers (module-level, no self needed) ─────────────────────

def _client():
    return docker.from_env()


def _cname(site_name: str) -> str:
    return f"nextjs_{site_name.replace('.', '_').replace('-', '_')}"


def _ensure_cache_dir(site_name: str) -> str:
    path = f"/home/frappe/nextjs_cache/{site_name}"
    os.makedirs(path, exist_ok=True)
    try:
        os.chown(path, 1001, 1001)
    except PermissionError:
        pass
    return path


def _remove_container(client, name: str, timeout: int = 10):
    try:
        c = client.containers.get(name)
        c.stop(timeout=timeout)
        c.remove()
    except docker.errors.NotFound:
        pass


def _wait_healthy(url: str, retries: int = 20, delay: float = 5.0):
    for _ in range(retries):
        try:
            if requests.get(url, timeout=4).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(delay)
    raise RuntimeError(f"Container not healthy after {retries * delay:.0f}s — {url}")


def _nginx_conf_dir(config: dict) -> str:
    """
    Resolve the directory where NFP writes its .nextjs.conf files.

    Uses the agent's nginx_directory from config.json. For co-located
    deployments (app server = proxy server) this is the nginx root dir
    itself, not the hosts/ subdirectory (hosts/ is for per-site SSL certs).
    """
    nginx_dir = config.get("nginx_directory", "/home/frappe/agent/nginx")
    return nginx_dir


# ── Mixin ─────────────────────────────────────────────────────────────

class NextjsMixin:
    """
    Drop-in mixin for agent/server.py's Server class.
    Provides three Press-dispatchable Next.js deployment jobs.
    """

    # ── Provision ─────────────────────────────────────────────────────

    @job("Provision Next.js Site")
    def provision_nextjs_site(
        self, site, repo_url, branch, container_port, env_vars,
        build_args=None, deployment_mode="Full Stack", backend_url="",
        app_server_private_ip="", proxy_hosts=None,
        press_callback_url="", press_callback_token="",
    ):
        name      = _cname(site)
        repo_dir  = self._nextjs_clone(site, repo_url, branch)
        self._nextjs_inject_templates(repo_dir, site, env_vars)
        tag       = self._nextjs_build(name, repo_dir, build_args or {})
        cache_dir = _ensure_cache_dir(site)
        self._nextjs_start_container(name, tag, container_port, env_vars, cache_dir)
        self._nextjs_wait_healthy(site, container_port)
        self._nextjs_write_nginx(site, container_port, deployment_mode, backend_url)
        self._nextjs_push_proxy(site, container_port, app_server_private_ip,
                                proxy_hosts or [], press_callback_url, press_callback_token)
        return {"status": "Running", "container": name, "port": container_port,
                "deployment_mode": deployment_mode}

    # ── Teardown ───────────────────────────────────────────────────────

    @job("Teardown Next.js Site")
    def teardown_nextjs_site(
        self, site, container_name=None,
        proxy_hosts=None, press_callback_url="", press_callback_token="",
    ):
        c    = _client()
        base = container_name or _cname(site)
        for name in [base, f"{base}_blue", f"{base}_green"]:
            _remove_container(c, name)
        self._nextjs_remove_nginx(site)
        if proxy_hosts:
            self._nextjs_remove_proxy(site, proxy_hosts, press_callback_url, press_callback_token)
        return {"status": "Stopped"}

    # ── Redeploy (blue/green) ──────────────────────────────────────────

    @job("Redeploy Next.js Site")
    def redeploy_nextjs_site(
        self, site, repo_url, branch, container_port, env_vars,
        build_args=None, deployment_mode="Full Stack", backend_url="",
        app_server_private_ip="", proxy_hosts=None,
        press_callback_url="", press_callback_token="",
    ):
        c    = _client()
        base = _cname(site)

        try:
            live         = c.containers.get(base)
            current_slot = live.labels.get("slot", "blue")
        except docker.errors.NotFound:
            current_slot = "blue"

        next_slot = "green" if current_slot == "blue" else "blue"
        next_name = f"{base}_{next_slot}"
        temp_port = container_port + 1

        repo_dir = self._nextjs_clone(site, repo_url, branch)
        self._nextjs_inject_templates(repo_dir, site, env_vars)
        new_tag  = self._nextjs_build(base, repo_dir, build_args or {},
                                      tag_suffix=next_slot)

        cache_dir = _ensure_cache_dir(site)
        _remove_container(c, next_name)
        temp_env = {**env_vars, "PORT": str(temp_port)}
        c.containers.run(
            image=new_tag, name=next_name, environment=temp_env,
            ports={"3000/tcp": temp_port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": next_slot},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )
        _wait_healthy(f"http://localhost:{temp_port}/api/health")
        self._nextjs_write_nginx(site, temp_port, deployment_mode, backend_url)

        # Drain old container, restart new one on canonical port + name
        _remove_container(c, base, timeout=15)
        _remove_container(c, next_name)
        c.containers.run(
            image=new_tag, name=base, environment=env_vars,
            ports={"3000/tcp": container_port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": next_slot},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )
        _wait_healthy(f"http://localhost:{container_port}/api/health")
        self._nextjs_write_nginx(site, container_port, deployment_mode, backend_url)
        self._nextjs_push_proxy(site, container_port, app_server_private_ip,
                                proxy_hosts or [], press_callback_url, press_callback_token)

        return {"status": "Running", "slot": next_slot, "port": container_port,
                "deployment_mode": deployment_mode}

    # ── Step helpers ───────────────────────────────────────────────────

    @step("Clone Next.js Repository")
    def _nextjs_clone(self, site: str, repo_url: str, branch: str) -> str:
        repo_dir = f"/home/frappe/nextjs/{site}"
        if os.path.exists(repo_dir):
            subprocess.run(["git", "-C", repo_dir, "fetch", "--all"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", repo_dir, "checkout", branch],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", repo_dir, "pull"],
                           check=True, capture_output=True)
        else:
            subprocess.run(
                ["git", "clone", "-b", branch, "--depth", "1", repo_url, repo_dir],
                check=True, capture_output=True,
            )
        return repo_dir

    @step("Inject Next.js Templates")
    def _nextjs_inject_templates(self, repo_dir: str, site: str, env_vars: dict):
        try:
            from agent.template_injector import inject_templates
            inject_templates(repo_dir, site, {"env_vars": env_vars})
        except ImportError:
            pass

    @step("Build Next.js Image")
    def _nextjs_build(self, name: str, repo_dir: str, build_args: dict,
                      tag_suffix: str = "latest") -> str:
        os.environ["DOCKER_BUILDKIT"] = "1"
        tag = f"{name}:{tag_suffix}"
        c = _client()
        _, logs = c.images.build(
            path=repo_dir, tag=tag,
            buildargs=build_args,
            rm=True, pull=True,
        )
        for _ in logs:
            pass
        return tag

    @step("Start Next.js Container")
    def _nextjs_start_container(self, name: str, tag: str, port: int,
                                env: dict, cache_dir: str):
        c = _client()
        _remove_container(c, name)
        c.containers.run(
            image=tag, name=name, environment=env,
            ports={"3000/tcp": port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": "blue"},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )

    @step("Wait for Next.js Health")
    def _nextjs_wait_healthy(self, site: str, port: int):
        _wait_healthy(f"http://localhost:{port}/api/health")

    @step("Write nginx Config")
    def _nextjs_write_nginx(self, site: str, port: int,
                            deployment_mode: str = "Full Stack",
                            backend_url: str = ""):
        """
        Write the nginx upstream config for this site.

        Uses app_server_ip="127.0.0.1" because in the standard Frappe Cloud
        setup the Docker container is bound to 127.0.0.1:<port> on the same
        host as nginx. Pass app_server_ip explicitly if your proxy is remote.

        conf_dir is resolved from the agent's nginx_directory config key.
        """
        from agent.nginx_utils import write_upstream
        conf_dir = _nginx_conf_dir(self.config)
        write_upstream(
            site_name=site,
            port=port,
            conf_dir=conf_dir,
            app_server_ip="127.0.0.1",
            deployment_mode=deployment_mode,
            backend_url=backend_url,
        )

    @step("Remove nginx Config")
    def _nextjs_remove_nginx(self, site: str):
        from agent.nginx_utils import remove_upstream
        conf_dir = _nginx_conf_dir(self.config)
        remove_upstream(site_name=site, conf_dir=conf_dir)

    @step("Push Proxy Config")
    def _nextjs_push_proxy(self, site: str, port: int, app_server_private_ip: str,
                           proxy_hosts: list, press_callback_url: str,
                           press_callback_token: str):
        if not proxy_hosts:
            return {}
        from agent.proxy_manager import run_proxy_playbook
        run_proxy_playbook(
            site_name             = site,
            container_port        = port,
            app_server_private_ip = app_server_private_ip,
            proxy_hosts           = proxy_hosts,
            press_callback_url    = press_callback_url,
            press_callback_token  = press_callback_token,
        )
        return {"proxy_hosts": len(proxy_hosts)}

    @step("Remove Proxy Config")
    def _nextjs_remove_proxy(self, site: str, proxy_hosts: list,
                             press_callback_url: str, press_callback_token: str):
        from agent.proxy_manager import remove_proxy_playbook
        remove_proxy_playbook(
            site_name            = site,
            proxy_hosts          = proxy_hosts,
            press_callback_url   = press_callback_url,
            press_callback_token = press_callback_token,
        )
        return {"proxy_hosts": len(proxy_hosts)}
