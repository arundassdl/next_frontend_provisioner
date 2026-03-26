import frappe
from frappe.model.document import Document

class NextjsSite(Document):
    def validate(self):
        # self._validate_press_site_exists()
        self._validate_env_keys()
        self._validate_backend_url()

    def _validate_press_site_exists(self):
        if not frappe.db.exists("Site", self.site_name):
            frappe.throw(frappe._("No Press Site named {0} found. Create it first.").format(self.site_name))

    def _validate_backend_url(self):
        """
        Ensure FRAPPE_URL is supplied as a Runtime env variable when no
        Press Site backs this record, so the Next.js app knows where its
        API lives.
        """
        keys = [r.key for r in self.env_variables if r.variable_type == "Runtime"]
        if "FRAPPE_URL" not in keys:
            frappe.msgprint(
                frappe._(
                    "No FRAPPE_URL runtime variable found. "
                    "Add it pointing to your backend, e.g. https://crmapp.yourdomain.com"
                ),
                indicator="orange",
                alert=True,
            )


    # def _validate_env_keys(self):
    #     seen = set()
    #     for row in self.env_variables:
    #         key = row.key.strip()
    #         if not key:
    #             frappe.throw(frappe._("Env variable key cannot be empty."))
    #         if key in seen:
    #             frappe.throw(frappe._("Duplicate env variable key: {0}").format(key))
    #         seen.add(key)
    #         if row.variable_type == "Build" and not key.startswith("NEXT_PUBLIC_"):
    #             frappe.msgprint(frappe._("Build-time var {0} should start with NEXT_PUBLIC_").format(key), indicator="orange", alert=True)
    def _validate_env_keys(self):
        seen = set()
        for row in self.env_variables:
            key = row.key.strip()
            if not key:
                frappe.throw(frappe._("Environment variable key cannot be empty."))
            if key in seen:
                frappe.throw(frappe._("Duplicate env variable key: {0}").format(key))
            seen.add(key)
            if row.variable_type == "Build" and not key.startswith("NEXT_PUBLIC_"):
                frappe.msgprint(
                    frappe._("Build-time var {0} should start with NEXT_PUBLIC_").format(key),
                    indicator="orange", alert=True,
                )

    def on_update(self):
        if self.status == "Running" and not frappe.db.exists("Deployment Log", {"site": self.name}):
            log = frappe.new_doc("Deployment Log")
            log.site = self.name
            log.insert(ignore_permissions=True)
