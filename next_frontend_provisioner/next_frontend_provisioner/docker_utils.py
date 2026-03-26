"""
docker_utils.py
---------------
Docker SDK wrappers.  Runs on the Press Agent, not the controller.
Imported from agent_extension/agent_jobs.py.
"""
import os
import time

import docker
import requests


def client() -> docker.DockerClient:
    return docker.from_env()


def build_image(repo_dir: str, tag: str, buildargs: dict = None, pull: bool = True) -> str:
    os.environ["DOCKER_BUILDKIT"] = "1"
    c = client()
    _, logs = c.images.build(path=repo_dir, tag=tag, buildargs=buildargs or {}, rm=True, pull=pull)
    for _ in logs:
        pass
    return tag


def start_container(name: str, tag: str, port: int, env: dict,
                    cache_dir: str, network: str = "frappe_net", slot: str = "blue"):
    c = client()
    _remove_if_exists(c, name)
    return c.containers.run(
        image=tag, name=name, environment=env,
        ports={"3000/tcp": port}, network=network,
        volumes={cache_dir: {"bind": "/app/.next/cache", "mode": "rw"}},
        labels={"managed_by": "next_frontend_provisioner", "slot": slot},
        detach=True, restart_policy={"Name": "unless-stopped"},
    )


def stop_and_remove(name: str, timeout: int = 15):
    _remove_if_exists(client(), name, timeout=timeout)


def wait_healthy(url: str, retries: int = 20, delay: float = 5.0, timeout: float = 4.0):
    for _ in range(retries):
        try:
            if requests.get(url, timeout=timeout).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(delay)
    raise RuntimeError(f"Container at {url} did not become healthy after {retries * delay:.0f}s")


def ensure_cache_dir(site_name: str) -> str:
    path = f"/home/frappe/nextjs_cache/{site_name}"
    os.makedirs(path, exist_ok=True)
    try:
        os.chown(path, 1001, 1001)
    except PermissionError:
        pass
    return path


def _remove_if_exists(c: docker.DockerClient, name: str, timeout: int = 10):
    try:
        ct = c.containers.get(name)
        ct.stop(timeout=timeout)
        ct.remove()
    except docker.errors.NotFound:
        pass
