"""
provisioner.py
--------------
Orchestrates all operations by dispatching Agent Jobs to the Press
agent running on the application server.  Never calls Docker directly.

Agent communication mirrors frappe/press internals:
  POST http://{server_private_ip}:{agent_port}/agent/job
  Basic auth: ("agent", agent_password)

Deployment modes
----------------
Full Stack    — requires a matching Press Site. Resolves server via
                Site → Bench → Server chain.
Frontend Only — no Press Site required. Resolves server from:
                  1. doc.target_server  (explicit, preferred)
                  2. First active Application Server  (fallback)
                FRAPPE_URL is set from doc.backend_url.
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
        # Fallback: first active application server
        servers = frappe.get_all("Server", filters={"status": "Active"}, limit=1)
        if not servers:
            frappe.throw(_("No active Application Server found. Cannot dispatch agent job."))
        return frappe.get_doc("Server", servers[0].name)
    else:
        # Full Stack — traverse Press Site → Bench → Server
        site  = frappe.get_doc("Site", doc.site_name)
        bench = frappe.get_doc("Bench", site.bench)
        return frappe.get_doc("Server", bench.server)


def _get_agent_conn_from_server(server) -> tuple:
    base = f"http://{server.private_ip}:{getattr(server, 'agent_port', 2022)}"
    return base, ("agent", server.get_password("agent_password"))


def _log_event(site_name: str, message: str, level: str = "Info"):
    """Append an entry to the Deployment Log for this site."""
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
    """Thread-safe host-port allocation using a MySQL named lock."""
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
    """
    Assemble runtime env vars for the container.

    FRAPPE_URL priority:
      1. Explicit Runtime env variable named FRAPPE_URL (user-supplied)
      2. doc.backend_url  (Frontend Only)
      3. https://{site_name}  (Full Stack fallback)
    """
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
    # Ensure NEXT_PUBLIC_FRAPPE_URL is available for standalone builds
    if "NEXT_PUBLIC_FRAPPE_URL" not in args:
        if doc.deployment_mode == "Frontend Only" and doc.backend_url:
            args["NEXT_PUBLIC_FRAPPE_URL"] = doc.backend_url.rstrip("/")
        else:
            args["NEXT_PUBLIC_FRAPPE_URL"] = f"https://{doc.site_name}"
    return args


def _get_proxy_hosts() -> list:
    """Return all active Press Proxy Server records as Ansible host dicts."""
    try:
        proxies = frappe.get_all(
            "Proxy Server",
            filters={"status": "Active"},
            fields=["private_ip", "ssh_user", "ssh_key_path"],
        )
        return [
            {
                "host": p.private_ip,
                "user": p.get("ssh_user") or "frappe",
                "key":  p.get("ssh_key_path") or "/home/frappe/.ssh/id_rsa",
            }
            for p in proxies
        ]
    except Exception:
        return []


def _get_callback_token() -> str:
    return frappe.conf.get("nfp_agent_callback_token", "")


def _callback_url() -> str:
    return (
        f"https://{frappe.local.site}"
        "/api/method/next_frontend_provisioner.next_frontend_provisioner.api.agent_job_update"
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
            "app_server_private_ip": server.private_ip,
            "proxy_hosts":           _get_proxy_hosts(),
            "press_callback_url":    _callback_url(),
            "press_callback_token":  _get_callback_token(),
        },
    }

    try:
        resp = requests.post(f"{base_url}/agent/job", json=payload, auth=auth, timeout=15)
        resp.raise_for_status()
        agent_job_id = resp.json().get("name", "")
        doc.db_set("agent_job", agent_job_id)
        doc.db_set("status", "Pending")
        mode_tag = f"[{doc.deployment_mode}]"
        backend_tag = f" → backend: {doc.backend_url}" if doc.deployment_mode == "Frontend Only" else ""
        _log_event(site_name, f"{mode_tag} Provision dispatched — agent job {agent_job_id}{backend_tag}")
    except Exception as exc:
        doc.db_set("status", "Failed")
        _log_event(site_name, f"Provision dispatch failed: {exc}", "Error")
        raise


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

    try:
        resp = requests.post(f"{base_url}/agent/job", json=payload, auth=auth, timeout=15)
        resp.raise_for_status()
        doc.db_set("status", "Stopped")
        _log_event(site_name, "Teardown job dispatched")
    except Exception as exc:
        _log_event(site_name, f"Teardown failed: {exc}", "Error")
        raise


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
            "app_server_private_ip": server.private_ip,
            "proxy_hosts":           _get_proxy_hosts(),
            "press_callback_url":    _callback_url(),
            "press_callback_token":  _get_callback_token(),
        },
    }

    try:
        resp = requests.post(f"{base_url}/agent/job", json=payload, auth=auth, timeout=15)
        resp.raise_for_status()
        agent_job_id = resp.json().get("name", "")
        doc.db_set("agent_job", agent_job_id)
        doc.db_set("status", "Deploying")
        _log_event(site_name, f"Redeploy dispatched — agent job {agent_job_id}")
    except Exception as exc:
        doc.db_set("status", "Failed")
        _log_event(site_name, f"Redeploy failed: {exc}", "Error")
        raise
