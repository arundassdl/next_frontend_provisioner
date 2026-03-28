app_name        = "next_frontend_provisioner"
app_title       = "Next Frontend Provisioner"
app_publisher   = "SDL"
app_description = "Provision Dockerized Next.js frontends inside Frappe Press"
app_email       = "pm@socialdnalabs.com"
app_license     = "MIT"
app_version     = "0.1.0"

# ── Press Site lifecycle hooks ───────────────────────────────────────
doc_events = {
    "Site": {
        "after_insert": "next_frontend_provisioner.next_frontend_provisioner.press_hooks.on_site_create",
        "on_trash":     "next_frontend_provisioner.next_frontend_provisioner.press_hooks.on_site_delete",
    }
}

# ── Agent job type names registered with Press ───────────────────────
agent_job_types = [
    "Provision Next.js Site",
    "Teardown Next.js Site",
    "Redeploy Next.js Site",
]

# ── Desk form JS ─────────────────────────────────────────────────────
doctype_js = {
    "Nextjs Site": "next_frontend_provisioner/public/js/nextjs_site.js",
}

# Nextjs Site on_trash: teardown containers for Frontend Only records
# (Press Site on_trash won't fire for Frontend Only since no Press Site exists)
doc_events.update({
    "Nextjs Site": {
        "on_trash": "next_frontend_provisioner.next_frontend_provisioner.press_hooks.on_nextjs_site_delete",
    }
})
