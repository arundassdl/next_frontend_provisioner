"""
press_hooks.py
--------------
Called by Frappe when a Press `Site` document is created or deleted.
Only acts when a matching `Nextjs Site` record exists.

Frontend Only deployments are triggered directly from the Nextjs Site
DocType form (or API) — not from Press Site lifecycle events — because
no Press Site is created for them.
"""
import frappe


def on_site_create(doc, method=None):
    if not frappe.db.exists("Nextjs Site", doc.name):
        return

    nextjs_doc = frappe.get_doc("Nextjs Site", doc.name)

    # Frontend Only sites are not linked to a Press Site at all;
    # their provisioning is triggered manually from the form/API.
    if getattr(nextjs_doc, "deployment_mode", "Full Stack") == "Frontend Only":
        return

    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_provision",
        site_name=doc.name,
        queue="long",
        timeout=1200,
        job_id=f"nextjs_provision_{doc.name}",
    )


def on_site_delete(doc, method=None):
    if not frappe.db.exists("Nextjs Site", doc.name):
        return

    nextjs_doc = frappe.get_doc("Nextjs Site", doc.name)
    if nextjs_doc.deployment_mode == "Frontend Only":
        return

    # For Frontend Only, teardown the container even though there is no
    # Press Site — the Nextjs Site record itself triggers this on_trash.
    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_teardown",
        site_name=doc.name,
        queue="default",
        timeout=180,
        job_id=f"nextjs_teardown_{doc.name}",
    )


def on_nextjs_site_delete(doc, method=None):
    """
    Called when a Nextjs Site record is deleted directly.
    Triggers container teardown for Frontend Only records
    (which have no Press Site to fire on_site_delete).
    """
    if getattr(doc, "deployment_mode", "Full Stack") != "Frontend Only":
        return
    if doc.status in ("Stopped", "Failed", "Pending"):
        return
    frappe.enqueue(
        "next_frontend_provisioner.next_frontend_provisioner.provisioner.dispatch_teardown",
        site_name=doc.name,
        queue="default",
        timeout=180,
        job_id=f"nextjs_teardown_{doc.name}",
    )
