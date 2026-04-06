#!/usr/bin/env python3
"""
nfp_proxy_patch.py
------------------
Drop-in wrapper that monkey-patches agent.proxy.Proxy._generate_proxy_config
BEFORE starting the real NginxReloadManager main loop.

This means every time NginxReloadManager regenerates proxy.conf, our NFP
upstream blocks are re-applied immediately after — without touching any
agent core file.

Supervisor config change (the ONLY file that needs editing):
    [program:nginx_reload_manager]
    command=bash -c "/var/frappe/agent/repo/wait-for-it.sh redis://127.0.0.1:25025 \
        && exec /var/frappe/agent/env/bin/python \
           /var/frappe/agent/repo/agent/nfp_proxy_patch.py"

Install:
    cp nfp_proxy_patch.py /var/frappe/agent/repo/agent/nfp_proxy_patch.py
    chmod +x /var/frappe/agent/repo/agent/nfp_proxy_patch.py
    sudo supervisorctl restart agent:nginx_reload_manager
"""
from __future__ import annotations

import json
import os
import re
import sys

# ── Ensure agent packages are importable ─────────────────────────────
# nginx_reload_manager.py uses: exec /path/to/python /path/to/nginx_reload_manager.py
# so sys.path already includes the repo. Mirror that here.
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR  = os.path.dirname(_AGENT_DIR)
for _p in [_AGENT_DIR, _REPO_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── NFP registry helpers ──────────────────────────────────────────────

def _nfp_registry_path(nginx_directory: str) -> str:
    return os.path.join(nginx_directory, "nfp_sites.json")


def _nfp_load_registry(nginx_directory: str) -> dict:
    path = _nfp_registry_path(nginx_directory)
    try:
        return json.load(open(path))
    except (FileNotFoundError, ValueError):
        return {}


def _nfp_get_port(entry) -> int:
    """Extract integer port from registry entry (supports both int and dict)."""
    if isinstance(entry, dict):
        return int(entry.get("port", 0))
    return int(entry)


def _nfp_apply_patches(proxy_conf_path: str, registry: dict) -> None:
    """
    Re-apply all NFP upstream blocks to proxy.conf after Proxy() regenerates it.
    Idempotent — skips domains already correctly patched.
    """
    if not registry:
        return
    if not os.path.exists(proxy_conf_path):
        return

    content = open(proxy_conf_path).read()
    changed = False

    for domain, entry in registry.items():
        port = _nfp_get_port(entry)
        if port == 0:
            print(f"[NFP-wrapper] Skipping {domain}: could not extract port from {entry!r}", flush=True)
            continue
        safe  = domain.replace(".", "_").replace("-", "_")
        uname = f"nextjs_{safe}"

        # Already correctly patched — skip
        if (f"server 127.0.0.1:{port};" in content and
                f"{domain} http://{uname}" in content):
            continue

        changed = True
        block = (
            f"upstream {uname} {{\n"
            f"\tserver 127.0.0.1:{port};\n"
            f"\tkeepalive 32;\n"
            f"}}\n"
        )
        entry = f"\t{domain} http://{uname};"

        # Remove stale upstream block
        content = re.sub(
            rf"upstream {re.escape(uname)} \{{[^}}]*\}}\n?",
            "", content,
        )
        # Remove stale map entries for this domain
        content = re.sub(
            rf"^\s*{re.escape(domain)}\s+\S+;\s*\n?",
            "", content, flags=re.MULTILINE,
        )

        # Insert upstream block before first existing upstream
        first = re.search(r"^upstream \w+", content, re.MULTILINE)
        if first:
            content = (
                content[:first.start()] + block + "\n" + content[first.start():]
            )
        else:
            content = block + "\n" + content

        # Add to $upstream_server_hash (first occurrence of default)
        content = content.replace(
            "\tdefault http://site_not_found;",
            f"{entry}\n\n\tdefault http://site_not_found;",
            1,
        )

        # Add to $socket_upstream_hash
        sock = re.search(
            r"(map \$actual_host \$socket_upstream_hash \{[^}]*?)"
            r"(default http://site_not_found;)",
            content, re.DOTALL,
        )
        if sock:
            se = f"\t{domain} http://{uname};\n"
            if se.strip() not in content:
                pos = sock.start(2)
                content = content[:pos] + se + "    " + content[pos:]

    if changed:
        open(proxy_conf_path, "w").write(content)
        print(f"[NFP-wrapper] proxy.conf re-patched for: {list(registry)}", flush=True)
    else:
        print(f"[NFP-wrapper] proxy.conf already up-to-date for: {list(registry)}", flush=True)


# ── Monkey-patch Proxy._generate_proxy_config ─────────────────────────

def _install_patch() -> None:
    """
    Wrap Proxy._generate_proxy_config so that after every call,
    NFP upstream blocks are re-applied from nfp_sites.json.

    This runs inside the nginx_reload_manager process, so it fires
    on every NginxReloadManager reload cycle automatically.
    """
    try:
        from agent.proxy import Proxy

        if getattr(Proxy, "_nfp_wrapper_patched", False):
            print("[NFP-wrapper] Already patched, skipping.", flush=True)
            return

        _orig = Proxy._generate_proxy_config

        def _patched_generate_proxy_config(self, *args, **kwargs):
            # Run original — this wipes our custom upstreams
            result = _orig(self, *args, **kwargs)

            # Re-apply NFP patches immediately after
            try:
                registry     = _nfp_load_registry(self.nginx_directory)
                proxy_conf   = os.path.join(self.nginx_directory, "proxy.conf")
                _nfp_apply_patches(proxy_conf, registry)
            except Exception as exc:
                # Non-fatal — log and continue so nginx reload still happens
                print(f"[NFP-wrapper] Re-patch warning: {exc}", flush=True)

            return result

        Proxy._generate_proxy_config   = _patched_generate_proxy_config
        Proxy._nfp_wrapper_patched     = True
        print("[NFP-wrapper] Proxy._generate_proxy_config patched ✓", flush=True)

    except Exception as exc:
        print(f"[NFP-wrapper] Patch install failed: {exc}", flush=True)
        raise


# ── Entry point: patch then hand off to real NginxReloadManager ───────

if __name__ == "__main__":
    print("[NFP-wrapper] Starting NFP proxy patch wrapper...", flush=True)

    # Install the monkey-patch BEFORE the manager's main loop starts
    _install_patch()

    # Now run the real NginxReloadManager main loop exactly as supervisor would.
    # Import and call its __main__ block directly — same process, same PID,
    # same signal handling, same Redis connection. No subprocess overhead.
    print("[NFP-wrapper] Handing off to NginxReloadManager...", flush=True)

    # nginx_reload_manager.py uses `if __name__ == "__main__":` guard.
    # We run it by exec'ing its code with __name__ == "__main__".
    _mgr_path = os.path.join(_AGENT_DIR, "nginx_reload_manager.py")

    with open(_mgr_path) as _f:
        _mgr_source = _f.read()

    # Execute in the current process — patch is already installed on Proxy class
    exec(compile(_mgr_source, _mgr_path, "exec"), {"__name__": "__main__", "__file__": _mgr_path})
