"""
provisioner.py
--------------
Dispatches Next.js deployment operations to the frappe/agent REST API.

Agent details confirmed from /var/frappe/agent/config.json:
  Base URL : http://127.0.0.1:25052  (web_port, loopback — same host)
  Auth     : Authorization: Bearer <plain-text agent_password>
              (agent hashes it via pbkdf2 and compares against access_token)

Agent endpoints (confirmed from web.py grep):
  POST   /frontends/<name>/deploy   — clone + build + run container + nginx
  DELETE /frontends/<name>          — stop container + remove nginx

Agent handler signature (confirmed from frontend.py lines 30-84):
  deploy_frontend(repo, branch, port, env_vars=None,
                  deployment_mode="Full Stack", backend_url="")

  The <name> in the URL becomes Frontend.name — used as the Docker
  container name and nginx upstream name.  Must be a valid identifier:
  dots replaced with hyphens (agent convention seen in URL pattern).

Deployment modes
----------------
Full Stack    — requires a matching Press Site. Server resolved via
                Site → Bench → Server chain.
Frontend Only — no Press Site required. Server resolved from
                doc.target_server, else first active Server.
                FRAPPE_URL set from doc.backend_url.
"""
import json as _json

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime


# ── Server resolution ─────────────────────────────────────────────────

def _get_server(doc) -> object:
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
    """True when Press controller and app server are on the same host."""
    return (getattr(server, "private_ip", None) or "").strip() in (
        "127.0.0.1", "localhost", "::1"
    )


def _get_server_ip(server) -> str:
    if _is_colocated(server):
        return "127.0.0.1"
    for attr in ("private_ip", "ip"):
        val = (getattr(server, attr, None) or "").strip()
        if val and val not in ("127.0.0.1", "localhost", "::1"):
            return val
    if server.name:
        return server.name
    frappe.throw(_("Cannot resolve IP for server {0}.").format(server.name))


def _get_agent_port(server) -> int:
    """
    Port resolution order:
      1. Explicit agent_port on Server doc
      2. nfp_agent_port in site_config
      3. web_port from agent config.json  (most reliable for co-located)
      4. 25052 default
    """
    for attr in ("agent_port", "frappe_agent_port"):
        val = getattr(server, attr, None)
        if val:
            return int(val)

    cfg_port = frappe.conf.get("nfp_agent_port")
    if cfg_port:
        return int(cfg_port)

    for path in (
        "/var/frappe/agent/config.json",
        "/home/frappe/agent/config.json",
    ):
        try:
            with open(path) as f:
                cfg = _json.load(f)
            p = cfg.get("web_port") or cfg.get("port") or cfg.get("agent_port")
            if p:
                return int(p)
        except (FileNotFoundError, PermissionError, ValueError):
            continue

    return 25052


def _get_agent_token(server) -> str:
    """
    Plain-text agent_password from the Server doc.
    The agent receives this and compares it against the pbkdf2 hash
    stored in config.json access_token.
    """
    try:
        return server.get_password("agent_password")
    except Exception:
        return frappe.conf.get("nfp_agent_password", "")


def _agent_conn(server) -> tuple:
    """Return (base_url, headers) for all agent requests."""
    ip    = _get_server_ip(server)
    port  = _get_agent_port(server)
    base  = f"http://{ip}:{port}"
    token = _get_agent_token(server)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    return base, headers


# ── Name slug ─────────────────────────────────────────────────────────

def _slug(site_name: str) -> str:
    """
    Convert site_name to the slug used in the agent URL and as the
    Docker container name.  The agent uses the name directly as the
    container name — it must be a valid Docker identifier.
    Dots → hyphens (consistent with URL path component convention).
    """
    return site_name.replace(".", "-")


# ── Logging ───────────────────────────────────────────────────────────

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


# ── Port allocation ───────────────────────────────────────────────────

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


# ── Env builders ──────────────────────────────────────────────────────

def _build_env_vars(doc) -> dict:
    """
    Build the env_vars dict sent to the agent's deploy_frontend handler.

    The agent passes these directly to `docker run -e`, so all vars —
    both runtime and build-time — go into a single flat dict.
    NEXT_PUBLIC_* vars are included here because docker build --build-arg
    is handled separately; for simplicity we pass them as env vars too
    so next.config.js can read them at runtime as well.
    """
    env = {}

    # User-defined runtime vars
    for r in doc.env_variables:
        env[r.key] = r.value

    # FRAPPE_URL: user-supplied > backend_url > site_name
    if "FRAPPE_URL" not in env:
        if doc.deployment_mode == "Frontend Only" and doc.backend_url:
            env["FRAPPE_URL"] = doc.backend_url.rstrip("/")
        else:
            env["FRAPPE_URL"] = f"https://{doc.site_name}"

    env.setdefault("FRAPPE_HOSTNAME", doc.site_name)

    # NEXT_PUBLIC_FRAPPE_URL: same priority
    if "NEXT_PUBLIC_FRAPPE_URL" not in env:
        env["NEXT_PUBLIC_FRAPPE_URL"] = env["FRAPPE_URL"]

    env.update({
        "PORT":     str(doc.container_port),
        "HOSTNAME": "0.0.0.0",
        "NODE_ENV": "production",
    })
    return env


# ── Proxy hosts (for Ansible) ─────────────────────────────────────────

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
                    "user": p.get("ssh_user") or "root",
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
        "/api/method/next_frontend_provisioner"
        ".next_frontend_provisioner.api.agent_job_update"
    )


# ── HTTP helpers ──────────────────────────────────────────────────────

def _post(url: str, headers: dict, payload: dict, timeout: int = 30) -> dict:
    """POST to the agent with clear error messages."""
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        frappe.throw(
            _(
                "Cannot reach the Press agent at {0}.\n"
                "Check: sudo systemctl status frappe-agent  or  "
                "ps aux | grep gunicorn\n"
                "Error: {1}"
            ).format(url, str(exc))
        )

    if not resp.ok:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:400]
        frappe.throw(
            _(
                "Agent returned {0} for {1}.\n"
                "Body: {2}\n"
                "Check: /var/frappe/agent/logs/web.error.log"
            ).format(resp.status_code, url, str(body))
        )

    try:
        return resp.json()
    except Exception:
        return {}


def _delete(url: str, headers: dict, timeout: int = 15) -> dict:
    """DELETE to the agent."""
    try:
        resp = requests.delete(url, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        frappe.throw(_("Cannot reach agent at {0}: {1}").format(url, str(exc)))

    if not resp.ok:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:400]
        frappe.throw(
            _("Agent DELETE error {0} for {1}: {2}").format(resp.status_code, url, str(body))
        )

    try:
        return resp.json()
    except Exception:
        return {}


# ── Public dispatch functions ─────────────────────────────────────────

def dispatch_provision(site_name: str):
    """
    Trigger a deploy via POST /frontends/<slug>/deploy.

    Payload matches frontend.py deploy_frontend signature:
      repo, branch, port, env_vars, deployment_mode, backend_url
    """
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)

    if not doc.container_port:
        doc.db_set("container_port", _allocate_port())
        doc.reload()

    base_url, headers = _agent_conn(server)
    slug = _slug(site_name)
    url  = f"{base_url}/frontends/{slug}/deploy"

    # Exact payload keys expected by frontend.py deploy_frontend()
    payload = {
        "repo":            doc.repo_url,
        "branch":          doc.branch or "main",
        "port":            doc.container_port,
        "env_vars":        _build_env_vars(doc),
        "deployment_mode": doc.deployment_mode,
        "backend_url":     (doc.backend_url or "").rstrip("/"),
    }

    mode_tag    = f"[{doc.deployment_mode}]"
    backend_tag = f" → {doc.backend_url}" if doc.deployment_mode == "Frontend Only" else ""
    _log_event(site_name, f"{mode_tag} Dispatching to {url}{backend_tag}")

    result       = _post(url, headers, payload)
    agent_job_id = result.get("job") or result.get("id") or result.get("name") or ""

    doc.db_set("agent_job", str(agent_job_id))
    doc.db_set("status", "Pending")
    _log_event(
        site_name,
        f"{mode_tag} Deploy triggered — agent job: {agent_job_id}{backend_tag}",
    )


def dispatch_teardown(site_name: str):
    """Tear down via DELETE /frontends/<slug>."""
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)

    base_url, headers = _agent_conn(server)
    slug = _slug(site_name)
    url  = f"{base_url}/frontends/{slug}"

    _log_event(site_name, f"Teardown dispatched to {url}")
    _delete(url, headers)

    doc.db_set("status", "Stopped")
    _log_event(site_name, "Teardown complete")


def dispatch_redeploy(site_name: str):
    """Redeploy — same as provision, agent handles blue/green internally."""
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)

    base_url, headers = _agent_conn(server)
    slug = _slug(site_name)
    url  = f"{base_url}/frontends/{slug}/deploy"

    payload = {
        "repo":            doc.repo_url,
        "branch":          doc.branch or "main",
        "port":            doc.container_port,
        "env_vars":        _build_env_vars(doc),
        "deployment_mode": doc.deployment_mode,
        "backend_url":     (doc.backend_url or "").rstrip("/"),
    }

    _log_event(site_name, f"[{doc.deployment_mode}] Redeploy dispatched to {url}")

    result       = _post(url, headers, payload)
    agent_job_id = result.get("job") or result.get("id") or result.get("name") or ""

    doc.db_set("agent_job", str(agent_job_id))
    doc.db_set("status", "Deploying")
    _log_event(site_name, f"Redeploy triggered — agent job: {agent_job_id}")
