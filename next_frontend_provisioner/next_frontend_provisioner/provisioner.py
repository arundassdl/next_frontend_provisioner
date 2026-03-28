"""
provisioner.py
--------------
Orchestrates all operations by dispatching Agent Jobs to the Press
agent running on the application server.  Never calls Docker directly.
"""
import frappe
import requests
from frappe import _
from frappe.utils import now_datetime


# ── Internal helpers ──────────────────────────────────────────────────

def _get_server(doc) -> object:
    """
    Resolve the Press Server for a Nextjs Site document.

    Frontend Only: uses doc.target_server if set, else first active Server.
    Full Stack:    traverses Site → Bench → Server.
    """
    if doc.deployment_mode == "Frontend Only":
        if doc.target_server:
            return frappe.get_doc("Server", doc.target_server)
        servers = frappe.get_all("Server", filters={"status": "Active"}, limit=1)
        if not servers:
            frappe.throw(_("No active Application Server found."))
        return frappe.get_doc("Server", servers[0].name)
    else:
        site  = frappe.get_doc("Site", doc.site_name)
        bench = frappe.get_doc("Bench", site.bench)
        return frappe.get_doc("Server", bench.server)


def _is_colocated(server) -> bool:
    """
    Returns True when the Press controller and the application server are
    the same machine.  This happens in single-server self-hosted setups
    where private_ip is set to 127.0.0.1.
    """
    private_ip = getattr(server, "private_ip", None) or ""
    return private_ip.strip() in ("127.0.0.1", "localhost", "::1")


def _get_server_ip(server) -> str:
    """
    Resolve the IP address used to reach the agent on this server.

    Co-located (single-server) setup:
        private_ip == 127.0.0.1 — Press and the agent are on the same host,
        so 127.0.0.1 is exactly right.

    Separate-server setup:
        Use private_ip (internal network) first, then fall back to public ip,
        then the server name (which is usually a hostname).
    """
    if _is_colocated(server):
        return "127.0.0.1"

    for attr in ("private_ip", "ip"):
        val = (getattr(server, attr, None) or "").strip()
        if val and val not in ("127.0.0.1", "localhost", "::1", ""):
            return val

    # Hostname fallback — server.name is typically the FQDN in Press
    name = (server.name or "").strip()
    if name:
        return name

    frappe.throw(
        _(
            "Could not resolve a reachable IP for server {0}. "
            "Check that the Server record has a valid 'ip' or 'private_ip' field."
        ).format(server.name)
    )


def _get_agent_port(server) -> int:
    """
    Resolve the port the Press agent is listening on.

    Resolution order:
      1. Explicit agent_port / frappe_agent_port on the Server doc
      2. nfp_agent_port in site config (bench set-config override)
      3. Port read from the agent's own config.json (most reliable for
         co-located setups where the file is readable)
      4. Press agent default: 2222
    """
    # 1. Explicit field on Server doc
    for attr in ("agent_port", "frappe_agent_port"):
        val = getattr(server, attr, None)
        if val:
            return int(val)

    # 2. bench set-config override
    port = frappe.conf.get("nfp_agent_port")
    if port:
        return int(port)

    # 3. Read from agent config.json (works on co-located setups)
    import json as _json, os as _os
    for candidate in (
        "/var/frappe/agent/repo/config.json",
	"/var/frappe/agent/config.json",
        "/home/frappe/agent/config.json",
        "/home/frappe/frappe-bench/apps/agent/config.json",
    ):
        try:
            with open(candidate) as f:
                cfg = _json.load(f)
            p = cfg.get("port") or cfg.get("agent_port")
            if p:
                return int(p)
        except (FileNotFoundError, KeyError, ValueError):
            continue

    # 4. Press agent default
    return 2222


def _get_agent_conn_from_server(server) -> tuple:
    """Return (base_url, (user, password)) for the Press agent on this server."""
    ip   = _get_server_ip(server)
    port = _get_agent_port(server)
    base = f"http://{ip}:{port}"
    try:
        password = server.get_password("agent_password")
    except Exception:
        password = frappe.conf.get("nfp_agent_password", "")
    return base, ("agent", password)


def _log_event(site_name: str, message: str, level: str = "Info"):
    try:
        log = frappe.get_doc("Deployment Log", {"site": site_name})
    except frappe.DoesNotExistError:
        log = frappe.new_doc("Deployment Log")
        log.site = site_name
        log.insert(ignore_permissions=True)
    log.append("log_entries", {
        "timestamp": str(now_datetime()),
        "level":     level,
        "message":   message,
    })
    log.save(ignore_permissions=True)
    frappe.db.commit()


def _allocate_port() -> int:
    frappe.db.sql("SELECT GET_LOCK('nextjs_port_alloc', 30)")
    try:
        used = set(frappe.db.sql_list(
            "SELECT container_port FROM `tabNextjs Site` "
            "WHERE container_port IS NOT NULL "
            "AND status NOT IN ('Stopped','Failed')"
        ))
        port = 3100
        while port in used:
            port += 1
        return port
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK('nextjs_port_alloc')")


def _build_runtime_env(doc) -> dict:
    env = {r.key: r.value for r in doc.env_variables if r.variable_type == "Runtime"}
    if "FRAPPE_URL" not in env:
        if doc.deployment_mode == "Frontend Only" and doc.backend_url:
            env["FRAPPE_URL"] = doc.backend_url.rstrip("/")
        else:
            env["FRAPPE_URL"] = f"https://{doc.site_name}"
    env.setdefault("FRAPPE_HOSTNAME", doc.site_name)
    env.update({
        "PORT":     str(doc.container_port),
        "HOSTNAME": "0.0.0.0",
        "NODE_ENV": "production",
    })
    return env


def _build_build_args(doc) -> dict:
    args = {r.key: r.value for r in doc.env_variables if r.variable_type == "Build"}
    if "NEXT_PUBLIC_FRAPPE_URL" not in args:
        if doc.deployment_mode == "Frontend Only" and doc.backend_url:
            args["NEXT_PUBLIC_FRAPPE_URL"] = doc.backend_url.rstrip("/")
        else:
            args["NEXT_PUBLIC_FRAPPE_URL"] = f"https://{doc.site_name}"
    return args


def _get_proxy_hosts() -> list:
    try:
        proxies = frappe.get_all(
            "Proxy Server",
            filters={"status": "Active"},
            fields=["private_ip", "ip", "ssh_user", "ssh_key_path"],
        )
        hosts = []
        for p in proxies:
            host_ip = p.get("private_ip") or p.get("ip")
            if host_ip:
                hosts.append({
                    "host": host_ip,
                    "user": p.get("ssh_user") or "frappe",
                    "key":  p.get("ssh_key_path") or "/home/frappe/.ssh/id_rsa",
                })
        return hosts
    except Exception:
        return []


def _get_callback_token() -> str:
    return frappe.conf.get("nfp_agent_callback_token", "")


def _callback_url() -> str:
    return (
        f"https://{frappe.local.site}"
        "/api/method/next_frontend_provisioner.next_frontend_provisioner.api.agent_job_update"
    )


def _dispatch(base_url: str, auth: tuple, payload: dict):
    """
    POST a job to the agent with a clear error if the connection fails.
    Raises a user-visible frappe.ValidationError instead of the raw
    requests exception so the desk shows a readable message.
    """
    try:
        resp = requests.post(
            f"{base_url}/agent/job",
            json=payload,
            auth=auth,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError as exc:
        frappe.throw(
            _(
                "Cannot reach the Press agent at {0}. "
                "Verify that:\n"
                "  1. The frappe/agent process is running on the application server\n"
                "  2. The Server record has the correct IP address\n"
                "  3. Port {1} is open between this server and the agent\n\n"
                "Run on the agent server: honcho start\n"
                "Check agent logs: journalctl -u frappe-agent -n 50\n\n"
                "Raw error: {2}"
            ).format(base_url, base_url.split(":")[-1], str(exc))
        )
    except requests.exceptions.HTTPError as exc:
        frappe.throw(
            _("Agent returned an error: {0}. Check agent logs on the application server.").format(str(exc))
        )


# ── Public dispatch functions ─────────────────────────────────────────

def dispatch_provision(site_name: str):
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)

    if not doc.container_port:
        doc.db_set("container_port", _allocate_port())
        doc.reload()

    base_url, auth = _get_agent_conn_from_server(server)

    payload = {
        "job_type": "Provision Next.js Site",
        "site":     site_name,
        "params": {
            "repo_url":              doc.repo_url,
            "branch":                doc.branch or "main",
            "container_port":        doc.container_port,
            "env_vars":              _build_runtime_env(doc),
            "build_args":            _build_build_args(doc),
            "deployment_mode":       doc.deployment_mode,
            "backend_url":           doc.backend_url or "",
            "app_server_private_ip": _get_server_ip(server),
            "proxy_hosts":           _get_proxy_hosts(),
            "press_callback_url":    _callback_url(),
            "press_callback_token":  _get_callback_token(),
        },
    }

    result = _dispatch(base_url, auth, payload)
    if result:
        agent_job_id = result.get("name", "")
        doc.db_set("agent_job", agent_job_id)
        doc.db_set("status", "Pending")
        mode_tag   = f"[{doc.deployment_mode}]"
        backend_tag = f" → backend: {doc.backend_url}" if doc.deployment_mode == "Frontend Only" else ""
        _log_event(site_name, f"{mode_tag} Provision dispatched to {base_url} — agent job {agent_job_id}{backend_tag}")
    else:
        doc.db_set("status", "Failed")
        _log_event(site_name, f"Provision dispatch returned no result from {base_url}", "Error")


def dispatch_teardown(site_name: str):
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)
    base_url, auth = _get_agent_conn_from_server(server)

    payload = {
        "job_type": "Teardown Next.js Site",
        "site":     site_name,
        "params": {
            "container_name":       f"nextjs_{site_name.replace('.', '_')}",
            "proxy_hosts":          _get_proxy_hosts(),
            "press_callback_url":   _callback_url(),
            "press_callback_token": _get_callback_token(),
        },
    }

    result = _dispatch(base_url, auth, payload)
    if result:
        doc.db_set("status", "Stopped")
        _log_event(site_name, "Teardown job dispatched")


def dispatch_redeploy(site_name: str):
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)
    base_url, auth = _get_agent_conn_from_server(server)

    payload = {
        "job_type": "Redeploy Next.js Site",
        "site":     site_name,
        "params": {
            "repo_url":              doc.repo_url,
            "branch":                doc.branch or "main",
            "container_port":        doc.container_port,
            "env_vars":              _build_runtime_env(doc),
            "build_args":            _build_build_args(doc),
            "deployment_mode":       doc.deployment_mode,
            "backend_url":           doc.backend_url or "",
            "app_server_private_ip": _get_server_ip(server),
            "proxy_hosts":           _get_proxy_hosts(),
            "press_callback_url":    _callback_url(),
            "press_callback_token":  _get_callback_token(),
        },
    }

    result = _dispatch(base_url, auth, payload)
    if result:
        agent_job_id = result.get("name", "")
        doc.db_set("agent_job", agent_job_id)
        doc.db_set("status", "Deploying")
        _log_event(site_name, f"Redeploy dispatched — agent job {agent_job_id}")
    else:
        doc.db_set("status", "Failed")
        _log_event(site_name, "Redeploy dispatch returned no result", "Error")
