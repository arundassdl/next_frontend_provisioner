frappe.ui.form.on('Nextjs Site', {
    refresh(frm) {
        const colours = {
            Running: 'green',
            Pending: 'orange',
            Deploying: 'blue',
            Failed: 'red',
            Stopped: 'grey',
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, colours[frm.doc.status] || 'grey');
        }

        if (!frm.is_new()) {
            const is_frontend_only = frm.doc.deployment_mode === 'Frontend Only';

            frm.add_custom_button(
                is_frontend_only ? __('Deploy Frontend') : __('Provision'),
                () => {
                    const msg = is_frontend_only
                        ? __('Deploy Next.js frontend for {0}?\nBackend: {1}',
                            [frm.doc.name, frm.doc.backend_url || '(none)'])
                        : __('Provision Next.js container for {0}?', [frm.doc.name]);
                    frappe.confirm(msg, () => _call(frm, 'provision'));
                },
                __('Actions')
            );

            frm.add_custom_button(__('Redeploy'), () => {
                frappe.confirm(
                    __('Zero-downtime redeploy for {0}?', [frm.doc.name]),
                    () => _call(frm, 'redeploy')
                );
            }, __('Actions'));

            frm.add_custom_button(__('Teardown'), () => {
                frappe.confirm(
                    __('Stop and remove container for {0}?', [frm.doc.name]),
                    () => _call(frm, 'teardown')
                );
            }, __('Actions'));

            frm.add_custom_button(__('View Logs'), () => _show_logs(frm));

            // Info banner for Frontend Only mode
            if (is_frontend_only && frm.doc.backend_url) {
                frm.dashboard.set_headline(
                    __('Frontend Only — API proxied to <a href="{0}" target="_blank">{0}</a>',
                        [frm.doc.backend_url])
                );
            }
        }

        // Show/hide target_server based on deployment_mode
        frm.toggle_reqd('target_server', frm.doc.deployment_mode === 'Frontend Only');
        frm.toggle_display('target_server', frm.doc.deployment_mode === 'Frontend Only');
    },

    deployment_mode(frm) {
        if (frm.doc.deployment_mode === 'Full Stack') {
            frm.set_value('backend_url', '');
            frm.set_value('target_server', '');
        }
        frm.trigger('refresh');
    },

    backend_url(frm) {
        // Auto-populate NEXT_PUBLIC_FRAPPE_URL build arg hint
        if (frm.doc.deployment_mode === 'Frontend Only' && frm.doc.backend_url) {
            const has_build_arg = (frm.doc.env_variables || []).some(
                r => r.key === 'NEXT_PUBLIC_FRAPPE_URL' && r.variable_type === 'Build'
            );
            if (!has_build_arg) {
                frappe.show_alert({
                    message: __('Tip: Add NEXT_PUBLIC_FRAPPE_URL as a Build variable → {0}',
                        [frm.doc.backend_url]),
                    indicator: 'blue',
                });
            }
        }
    },
});

function _call(frm, action) {
    // Correct dotted path: app.module.api.method
    frappe.call({
        method: `next_frontend_provisioner.next_frontend_provisioner.api.${action}`,
        args: { site_name: frm.doc.name },
        callback(r) {
            if (r.message) {
                frappe.show_alert({
                    message: __('Job queued for {0}', [frm.doc.name]),
                    indicator: 'green',
                });
                frm.reload_doc();
            }
        },
    });
}

function _show_logs(frm) {
    const d = new frappe.ui.Dialog({
        title: __('Deployment Logs — {0}', [frm.doc.name]),
        size: 'large',
    });
    d.show();
    d.$body.html('<p class="text-muted">Loading…</p>');
    frappe.call({
        method: 'next_frontend_provisioner.next_frontend_provisioner.api.deployment_logs',
        args: { site_name: frm.doc.name },
        callback(r) {
            const entries = r.message || [];
            if (!entries.length) {
                d.$body.html('<p class="text-muted">No log entries yet.</p>');
                return;
            }
            const colours = {
                Info: 'text-muted',
                Warning: 'text-warning',
                Error: 'text-danger',
            };
            const rows = entries.map(e => `<tr>
                <td style="white-space:nowrap;font-size:11px">${e.timestamp}</td>
                <td><span class="${colours[e.level] || ''}">${e.level}</span></td>
                <td style="font-family:monospace;font-size:12px;word-break:break-all">
                    ${frappe.utils.escape_html(e.message)}
                </td>
            </tr>`).join('');
            d.$body.html(`
                <table class="table table-condensed">
                    <thead>
                        <tr><th>Time</th><th>Level</th><th>Message</th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `);
        },
    });
}
