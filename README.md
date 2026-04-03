# next_frontend_provisioner

A custom Frappe app that provisions, manages, and integrates Dockerized Next.js
frontends within an existing Frappe Press self-hosted deployment environment.

## Features

- DocType-driven provisioning of one Next.js container per Press Site
- Press Agent job dispatch for all Docker operations (never calls Docker from controller)
- Automatic Next.js build infrastructure injection via template_injector.py
- Blue/green zero-downtime redeploy
- Per-site environment variable management (build-time vs runtime split)
- Persistent ISR cache volume per site
- Ansible-based nginx upstream management on Press proxy servers
- Deployment Log with per-step audit trail
- Frappe desk form with action buttons + live log viewer

## Repository layout

```
next_frontend_provisioner/        ← Frappe app (install on Press controller bench)
  next_frontend_provisioner/      ← Python package
    hooks.py                      ← app metadata, doc_events, doctype_js
    press_hooks.py                ← Site lifecycle hooks
    api.py                        ← @whitelist REST endpoints
    provisioner.py                ← Agent job dispatcher
    port_manager.py               ← MySQL-locked port allocation
    nginx_utils.py                ← nginx config helpers (agent-side)
    docker_utils.py               ← Docker SDK wrappers (agent-side)
    template_injector.py          ← Repo file injection after git clone/pull
    doctype/                      ← Frappe DocTypes
    public/js/                    ← Desk form JS
    templates/                    ← Dockerfile, nginx conf, next.config.js
  ansible/                        ← Ansible playbooks for proxy server management
  agent_extension/                ← Copy into frappe/agent fork
  patches/
```

```
next_frontend_provisioner
├── agent_extension
│   ├── AGENT_INTEGRATION.md
│   ├── agent_jobs.py
│   ├── frontend_patch.py
│   ├── nginx_utils.py
│   ├── proxy_manager.py
│   ├── template_injector.py
│   └── web.py
├── ansible
│   ├── group_vars
│   │   └── proxies.yml
│   ├── inventory
│   │   └── proxies.ini
│   ├── proxy_nextjs_remove.yml
│   ├── proxy_nextjs_rollback.yml
│   ├── proxy_nextjs.yml
│   ├── roles
│   │   └── nextjs_proxy
│   │       ├── defaults
│   │       │   └── main.yml
│   │       ├── handlers
│   │       │   └── main.yml
│   │       └── tasks
│   │           ├── main.yml
│   │           ├── provision.yml
│   │           ├── remove.yml
│   │           └── rollback.yml
│   └── templates
│       └── nextjs_proxy.conf.j2
├── diagnose_nginx.sh
├── fix_agent.py
├── get_token.sh
├── inspect_agent_job.sh
├── install_agent_extension.sh
├── MANIFEST.in
├── next_frontend_provisioner
│   ├── hooks.py
│   ├── __init__.py
│   ├── module_def.json
│   ├── modules.txt
│   ├── next_frontend_provisioner
│   │   ├── api.py
│   │   ├── docker_utils.py
│   │   ├── doctype
│   │   │   ├── deployment_log
│   │   │   │   ├── deployment_log.json
│   │   │   │   ├── deployment_log.py
│   │   │   │   ├── __init__.py
│   │   │   ├── deployment_log_entry
│   │   │   │   ├── deployment_log_entry.json
│   │   │   │   ├── deployment_log_entry.py
│   │   │   │   ├── __init__.py
│   │   │   ├── __init__.py
│   │   │   ├── nextjs_env_variable
│   │   │   │   ├── __init__.py
│   │   │   │   ├── nextjs_env_variable.json
│   │   │   │   ├── nextjs_env_variable.py
│   │   │   └── nextjs_site
│   │   │       ├── __init__.py
│   │   │       ├── nextjs_site.json
│   │   │       └── nextjs_site.py
│   │   ├── __init__.py
│   │   ├── nginx_utils.py
│   │   ├── port_manager.py
│   │   ├── press_hooks.py
│   │   ├── provisioner.py
│   │   ├── template_injector.py
│   │   └── templates
│   │       ├── docker
│   │       │   ├── Dockerfile
│   │       │   ├── health_route
│   │       │   │   └── route.ts
│   │       │   └── next.config.js
│   │       └── nginx
│   │           └── nextjs_upstream.conf
│   ├── public
│   │   └── js
│   │       └── nextjs_site.js
├── patches
│   └── patches.txt
├── README.md
├── requirements.txt
└── setup.py

```
## Installation

```bash
# On the Press controller bench
bench get-app https://github.com/your-org/next_frontend_provisioner
bench --site your-press-site install-app next_frontend_provisioner
bench migrate

# Set callback token in site config
bench --site your-press-site set-config nfp_agent_callback_token "your-secret-token"

# Install Ansible (required on the agent server)
pip install ansible --break-system-packages

# Copy agent_extension/ files into your frappe/agent fork
cp agent_extension/agent_jobs.py     /path/to/agent/agent/nextjs_jobs.py
cp agent_extension/proxy_manager.py  /path/to/agent/agent/proxy_manager.py
cp agent_extension/template_injector.py /path/to/agent/agent/template_injector.py

# Register job classes in agent/job.py (see agent_extension/AGENT_INTEGRATION.md)
```

## Agent env vars

Set these in your agent server's `.env` or `Procfile`:

```
NFP_TEMPLATES_DIR=/path/to/agent/agent/templates/docker
NFP_ANSIBLE_DIR=/path/to/next_frontend_provisioner/ansible
```

## License

MIT
