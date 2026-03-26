"""
proxy_manager.py  →  copy to agent/proxy_manager.py in your frappe/agent fork
------------------------------------------------------------------------------
Renders a dynamic Ansible inventory from Press Server DocTypes and invokes
the proxy playbook as a subprocess from the Press agent.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

_ANSIBLE_DIR = Path(os.environ.get(
    "NFP_ANSIBLE_DIR",
    Path(__file__).parent.parent / "ansible",
))


def run_proxy_playbook(
    site_name: str,
    container_port: int,
    app_server_private_ip: str,
    proxy_hosts: list,
    press_callback_url: str,
    press_callback_token: str,
) -> subprocess.CompletedProcess:
    """Render inventory + vars and run proxy_nextjs.yml."""
    return _run(
        playbook="proxy_nextjs.yml",
        inventory_content=_render_inventory(proxy_hosts),
        extra_vars={
            "site_name":             site_name,
            "safe_name":             _safe(site_name),
            "container_port":        container_port,
            "app_server_private_ip": app_server_private_ip,
            "press_callback_url":    press_callback_url,
            "press_callback_token":  press_callback_token,
        },
    )


def remove_proxy_playbook(
    site_name: str,
    proxy_hosts: list,
    press_callback_url: str,
    press_callback_token: str,
    deployment_mode: str = "Full Stack",
    backend_url: str = "",
) -> subprocess.CompletedProcess:
    """Run proxy_nextjs_remove.yml to clean up a site's upstream."""
    return _run(
        playbook="proxy_nextjs_remove.yml",
        inventory_content=_render_inventory(proxy_hosts),
        extra_vars={
            "site_name":            site_name,
            "safe_name":            _safe(site_name),
            "press_callback_url":   press_callback_url,
            "press_callback_token": press_callback_token,
            "deployment_mode":      deployment_mode,
            "backend_url":          backend_url,
        },
    )


def rollback_proxy_playbook(
    site_name: str,
    proxy_hosts: list,
) -> subprocess.CompletedProcess:
    """Restore the previous nginx config backup on all proxy servers."""
    return _run(
        playbook="proxy_nextjs_rollback.yml",
        inventory_content=_render_inventory(proxy_hosts),
        extra_vars={
            "site_name": site_name,
            "safe_name": _safe(site_name),
        },
    )


# ── Private ──────────────────────────────────────────────────────────

def _run(playbook: str, inventory_content: str, extra_vars: dict) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="nfp_ansible_") as tmpdir:
        inv_path  = Path(tmpdir) / "inventory.ini"
        vars_path = Path(tmpdir) / "extra_vars.json"
        inv_path.write_text(inventory_content)
        vars_path.write_text(json.dumps(extra_vars))

        cmd = [
            "ansible-playbook",
            str(_ANSIBLE_DIR / playbook),
            "-i", str(inv_path),
            "-e", f"@{vars_path}",
            "--ssh-extra-args", "-o StrictHostKeyChecking=accept-new",
        ]

        result = subprocess.run(
            cmd,
            cwd=str(_ANSIBLE_DIR),
            text=True,
            env={
                **os.environ,
                "ANSIBLE_FORCE_COLOR":        "0",
                "ANSIBLE_HOST_KEY_CHECKING":  "False",
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                "ANSIBLE_GATHERING":          "smart",
            },
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"ansible-playbook failed for {extra_vars.get('site_name')} "
                f"(exit {result.returncode})"
            )
        return result


def _render_inventory(proxy_hosts: list) -> str:
    lines = ["[proxies]"]
    for i, h in enumerate(proxy_hosts):
        lines.append(
            f"proxy{i+1} "
            f"ansible_host={h['host']} "
            f"ansible_user={h.get('user','frappe')} "
            f"ansible_ssh_private_key_file={h.get('key','/home/frappe/.ssh/id_rsa')}"
        )
    lines += ["", "[proxies:vars]", "ansible_python_interpreter=/usr/bin/python3"]
    return "\n".join(lines)


def _safe(name: str) -> str:
    return name.replace(".", "_").replace("-", "_")
