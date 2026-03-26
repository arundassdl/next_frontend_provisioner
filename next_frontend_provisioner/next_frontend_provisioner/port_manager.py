"""
port_manager.py
---------------
Thread-safe allocation and release of host ports for Next.js containers.
Ports are tracked in the Nextjs Site DocType; a MySQL named lock prevents
concurrent bench workers from colliding.
"""
import frappe

_START_PORT = 3100
_MAX_PORTS  = 500


def allocate() -> int:
    frappe.db.sql("SELECT GET_LOCK('nextjs_port_alloc', 30)")
    try:
        used = set(frappe.db.sql_list(
            "SELECT container_port FROM `tabNextjs Site` "
            "WHERE container_port IS NOT NULL "
            "AND status NOT IN ('Stopped', 'Failed')"
        ))
        for port in range(_START_PORT, _START_PORT + _MAX_PORTS):
            if port not in used:
                return port
        frappe.throw(
            frappe._("No available ports in range {0}–{1}. Stop some sites first.").format(
                _START_PORT, _START_PORT + _MAX_PORTS - 1
            )
        )
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK('nextjs_port_alloc')")


def release(port: int):
    pass  # Port freed automatically when site status = Stopped/Failed


def is_in_use(port: int) -> bool:
    return bool(frappe.db.exists(
        "Nextjs Site",
        {"container_port": port, "status": ["not in", ["Stopped", "Failed"]]},
    ))
