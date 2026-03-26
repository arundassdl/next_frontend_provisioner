"""
agent_jobs.py  →  copy to agent/nextjs_jobs.py in your frappe/agent fork
------------------------------------------------------------------------
Three job classes registered with the Press agent:
  - ProvisionNextjsSiteJob
  - TeardownNextjsSiteJob
  - RedeployNextjsSiteJob  (blue/green zero-downtime)

Register in agent/job.py:
    from agent.nextjs_jobs import (
        ProvisionNextjsSiteJob, TeardownNextjsSiteJob, RedeployNextjsSiteJob
    )
    JOB_CLASSES = {
        **JOB_CLASSES,
        "Provision Next.js Site": ProvisionNextjsSiteJob,
        "Teardown Next.js Site":  TeardownNextjsSiteJob,
        "Redeploy Next.js Site":  RedeployNextjsSiteJob,
    }
"""
import os
import subprocess
import time

import docker
import requests


# ── Shared helpers ────────────────────────────────────────────────────

def _client():
    return docker.from_env()


def _cname(site_name: str) -> str:
    return f"nextjs_{site_name.replace('.', '_')}"


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


def _write_nginx(site_name: str, container_name: str, port: int):
    from agent.nginx_utils import write_upstream
    write_upstream(site_name, container_name, port)


# ── Provision ─────────────────────────────────────────────────────────

class ProvisionNextjsSiteJob:
    job_type = "Provision Next.js Site"

    def __init__(self, job, server):
        self.job    = job
        self.server = server
        self.params = job.get("params", {})

    def run(self):
        site   = self.job["site"]
        p      = self.params
        port   = p["container_port"]
        name   = _cname(site)

        repo_dir  = self._clone(site, p)
        self._inject_templates(repo_dir, site, p)
        tag       = self._build(name, repo_dir, p)
        cache_dir = _ensure_cache_dir(site)
        self._start(name, tag, port, p["env_vars"], cache_dir)
        _wait_healthy(f"http://localhost:{port}/api/health")
        _write_nginx(site, name, port)
        self._push_proxy(site, port, p)

        return {"status": "Running", "container": name, "port": port}

    def _clone(self, site: str, p: dict) -> str:
        repo_dir = f"/home/frappe/nextjs/{site}"
        if os.path.exists(repo_dir):
            subprocess.run(["git","-C",repo_dir,"fetch","--all"], check=True, capture_output=True)
            subprocess.run(["git","-C",repo_dir,"checkout",p.get("branch","main")], check=True, capture_output=True)
            subprocess.run(["git","-C",repo_dir,"pull"], check=True, capture_output=True)
        else:
            subprocess.run(
                ["git","clone","-b",p.get("branch","main"),"--depth","1",p["repo_url"],repo_dir],
                check=True, capture_output=True,
            )
        return repo_dir

    def _inject_templates(self, repo_dir: str, site: str, p: dict):
        try:
            from agent.template_injector import inject_templates
            inject_templates(repo_dir, site, p)
        except ImportError:
            pass  # template_injector optional

    def _build(self, name: str, repo_dir: str, p: dict) -> str:
        os.environ["DOCKER_BUILDKIT"] = "1"
        tag = f"{name}:latest"
        c = _client()
        _, logs = c.images.build(
            path=repo_dir, tag=tag,
            buildargs=p.get("build_args", {}),
            rm=True, pull=True,
        )
        for _ in logs:
            pass
        return tag

    def _start(self, name: str, tag: str, port: int, env: dict, cache_dir: str):
        c = _client()
        _remove_container(c, name)
        c.containers.run(
            image=tag, name=name, environment=env,
            ports={"3000/tcp": port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": "blue"},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )

    def _push_proxy(self, site: str, port: int, p: dict):
        proxy_hosts = p.get("proxy_hosts", [])
        if not proxy_hosts:
            return
        from agent.proxy_manager import run_proxy_playbook
        run_proxy_playbook(
            site_name             = site,
            container_port        = port,
            app_server_private_ip = p.get("app_server_private_ip", ""),
            proxy_hosts           = proxy_hosts,
            press_callback_url    = p.get("press_callback_url", ""),
            press_callback_token  = p.get("press_callback_token", ""),
            deployment_mode       = p.get("deployment_mode", "Full Stack"),
            backend_url           = p.get("backend_url", ""),
        )


# ── Teardown ──────────────────────────────────────────────────────────

class TeardownNextjsSiteJob:
    job_type = "Teardown Next.js Site"

    def __init__(self, job, server):
        self.job    = job
        self.server = server
        self.params = job.get("params", {})

    def run(self):
        site   = self.job["site"]
        p      = self.params
        c      = _client()
        base   = p.get("container_name", _cname(site))

        for name in [base, f"{base}_blue", f"{base}_green"]:
            _remove_container(c, name)

        from agent.nginx_utils import remove_upstream
        remove_upstream(site)

        proxy_hosts = p.get("proxy_hosts", [])
        if proxy_hosts:
            from agent.proxy_manager import remove_proxy_playbook
            remove_proxy_playbook(
                site_name            = site,
                proxy_hosts          = proxy_hosts,
                press_callback_url   = p.get("press_callback_url", ""),
                press_callback_token = p.get("press_callback_token", ""),
            )

        return {"status": "Stopped"}


# ── Redeploy (blue/green) ─────────────────────────────────────────────

class RedeployNextjsSiteJob:
    job_type = "Redeploy Next.js Site"

    def __init__(self, job, server):
        self.job    = job
        self.server = server
        self.params = job.get("params", {})

    def run(self):
        site   = self.job["site"]
        p      = self.params
        port   = p["container_port"]
        c      = _client()
        base   = _cname(site)

        # Determine current live slot
        try:
            live = c.containers.get(base)
            current_slot = live.labels.get("slot", "blue")
        except docker.errors.NotFound:
            current_slot = "blue"

        next_slot  = "green" if current_slot == "blue" else "blue"
        next_name  = f"{base}_{next_slot}"
        temp_port  = port + 1

        # Pull latest code and build new image
        prov = ProvisionNextjsSiteJob(self.job, self.server)
        repo_dir = prov._clone(site, p)
        prov._inject_templates(repo_dir, site, p)

        os.environ["DOCKER_BUILDKIT"] = "1"
        new_tag = f"{base}:{next_slot}"
        _, logs = c.images.build(
            path=repo_dir, tag=new_tag,
            buildargs=p.get("build_args", {}), rm=True, pull=True,
        )
        for _ in logs:
            pass

        # Start new slot on temp port
        cache_dir = _ensure_cache_dir(site)
        _remove_container(c, next_name)
        env = {**p["env_vars"], "PORT": str(temp_port)}
        c.containers.run(
            image=new_tag, name=next_name, environment=env,
            ports={"3000/tcp": temp_port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": next_slot},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )

        # Health-check new slot, then cut nginx over
        _wait_healthy(f"http://localhost:{temp_port}/api/health")
        _write_nginx(site, next_name, temp_port)

        # Drain old live container
        _remove_container(c, base, timeout=15)

        # Restart new slot on canonical port with canonical name
        _remove_container(c, next_name)
        c.containers.run(
            image=new_tag, name=base, environment=p["env_vars"],
            ports={"3000/tcp": port}, network="frappe_net",
            volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
            labels={"managed_by": "next_frontend_provisioner", "slot": next_slot},
            detach=True, restart_policy={"Name": "unless-stopped"},
        )
        _wait_healthy(f"http://localhost:{port}/api/health")
        _write_nginx(site, base, port)
        self._push_proxy(site, port, p)

        return {"status": "Running", "slot": next_slot, "port": port}

    def _push_proxy(self, site: str, port: int, p: dict):
        proxy_hosts = p.get("proxy_hosts", [])
        if not proxy_hosts:
            return
        from agent.proxy_manager import run_proxy_playbook
        run_proxy_playbook(
            site_name             = site,
            container_port        = port,
            app_server_private_ip = p.get("app_server_private_ip", ""),
            proxy_hosts           = proxy_hosts,
            press_callback_url    = p.get("press_callback_url", ""),
            press_callback_token  = p.get("press_callback_token", ""),
            deployment_mode       = p.get("deployment_mode", "Full Stack"),
            backend_url           = p.get("backend_url", ""),
        )
