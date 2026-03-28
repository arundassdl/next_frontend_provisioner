"""
api.py
------
Whitelisted REST endpoints.  All require System Manager unless noted.
"""
import frappe
from frappe import _
from . import provisioner


@frappe.whitelist()
def provision(site_name: str):
    frappe.only_for("System Manager")
    _assert_exists(site_name)
    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_provision",
        site_name=site_name, queue="long", timeout=1200,
        job_id=f"nextjs_provision_{site_name}",
    )
    return {"status": "queued", "site": site_name}


@frappe.whitelist()
def redeploy(site_name: str):
    frappe.only_for("System Manager")
    _assert_exists(site_name)
    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_redeploy",
        site_name=site_name, queue="long", timeout=1200,
        job_id=f"nextjs_redeploy_{site_name}",
    )
    return {"status": "queued", "site": site_name}


@frappe.whitelist()
def teardown(site_name: str):
    frappe.only_for("System Manager")
    _assert_exists(site_name)
    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_teardown",
        site_name=site_name, queue="default", timeout=180,
        job_id=f"nextjs_teardown_{site_name}",
    )
    return {"status": "queued", "site": site_name}


@frappe.whitelist()
def status(site_name: str):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("Nextjs Site", site_name)
    return {
        "site":           site_name,
        "status":         doc.status,
        "container_port": doc.container_port,
        "agent_job":      doc.agent_job,
        "last_deployed":  str(doc.modified),
    }


@frappe.whitelist()
def list_sites():
    frappe.only_for("System Manager")
    return frappe.get_all(
        "Nextjs Site",
        fields=["name", "status", "container_port", "repo_url", "branch", "modified"],
        order_by="modified desc",
    )


@frappe.whitelist()
def deployment_logs(site_name: str):
    frappe.only_for("System Manager")
    logs = frappe.get_all(
        "Deployment Log",
        filters={"site": site_name},
        fields=["name", "creation"],
        order_by="creation desc",
        limit=1,
    )
    if not logs:
        return []
    log_doc = frappe.get_doc("Deployment Log", logs[0]["name"])
    entries = [
        {"timestamp": e.timestamp, "level": e.level, "message": e.message}
        for e in log_doc.log_entries
    ]
    return sorted(entries, key=lambda x: x["timestamp"], reverse=True)


@frappe.whitelist(allow_guest=False)
def agent_job_update():
    """
    Callback endpoint — called by the Ansible playbook and agent jobs
    to report job completion back to the Press controller.

    Expected POST body (JSON):
        { "job_name": str, "site": str, "status": "Success"|"Failure", "output": str }
    """
    data       = frappe.local.form_dict
    site_name  = data.get("site")
    job_name   = data.get("job_name", "")
    job_status = data.get("status", "")
    output     = data.get("output", "")

    if not site_name or not frappe.db.exists("Nextjs Site", site_name):
        return {"ok": False, "reason": "site not found"}

    provisioner._log_event(
        site_name,
        f"[{job_name}] {job_status}: {output}",
        level="Error" if job_status == "Failure" else "Info",
    )

    if job_status == "Success":
        frappe.db.set_value("Nextjs Site", site_name, "status", "Running")
    elif job_status == "Failure":
        frappe.db.set_value("Nextjs Site", site_name, "status", "Failed")

    frappe.db.commit()
    return {"ok": True}


def _assert_exists(site_name: str):
    if not frappe.db.exists("Nextjs Site", site_name):
        frappe.throw(
            _("No Nextjs Site record found for {0}").format(site_name),
            frappe.DoesNotExistError,
        )


@frappe.whitelist()
def debug_agent_connection(site_name: str):
    """
    Diagnostic endpoint — returns the resolved IP, port, and agent URL
    for a Nextjs Site without dispatching any job.

    Usage from bench console:
        frappe.call("next_frontend_provisioner.next_frontend_provisioner.api.debug_agent_connection",
                    site_name="crm.yourdomain.com")

    Or set via bench config to override port:
        bench --site your-site set-config nfp_agent_port 2222
    """
    from next_frontend_provisioner.next_frontend_provisioner.provisioner import (
        _get_server, _get_server_ip, _get_agent_port, _is_colocated
    )
    _assert_exists(site_name)
    doc    = frappe.get_doc("Nextjs Site", site_name)
    server = _get_server(doc)
    ip     = _get_server_ip(server)
    port   = _get_agent_port(server)
    return {
        "server":      server.name,
        "private_ip":  server.private_ip,
        "public_ip":   server.ip,
        "colocated":   _is_colocated(server),
        "resolved_ip": ip,
        "resolved_port": port,
        "agent_url":   f"http://{ip}:{port}/agent/job",
    }
