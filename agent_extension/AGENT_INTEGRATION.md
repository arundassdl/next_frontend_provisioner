# Agent Integration Guide

## Overview

The Next.js provisioner extends the Press agent by adding three job types:
- `Provision Next.js Site`
- `Teardown Next.js Site`
- `Redeploy Next.js Site`

These are implemented as a mixin on the agent's `Server` class.

---

## 1. Copy files into your frappe/agent repo

Run on the **agent server** from the `next_frontend_provisioner` app directory:

```bash
AGENT_PKG=/var/frappe/agent/repo/agent

cp agent_extension/agent_jobs.py        ${AGENT_PKG}/nextjs_jobs.py
cp agent_extension/nginx_utils.py       ${AGENT_PKG}/nginx_utils.py
cp agent_extension/proxy_manager.py     ${AGENT_PKG}/proxy_manager.py
cp agent_extension/template_injector.py ${AGENT_PKG}/template_injector.py
```

Copy the Ansible playbooks to the agent server:

```bash
cp -r ansible/ /home/frappe/agent/ansible/
```

---

## 2. Patch agent/server.py

Two changes are needed in `/var/frappe/agent/repo/agent/server.py`:

**Add the import** near the other `from agent.` imports:

```python
from agent.nextjs_jobs import NextjsMixin
```

**Update the Server class definition:**

```python
# Before
class Server(Base):

# After
class Server(NextjsMixin, Base):
```

> **Important:** Do NOT add any import of `nextjs_jobs` to `agent/job.py`.
> The mixin must only be imported from `server.py` to avoid a circular import
> (`job.py` → `nextjs_jobs.py` → `job.py`).

---

## 3. Automated install (alternative to manual steps 1 & 2)

The included script handles steps 1 and 2 automatically:

```bash
sudo bash install_agent_extension.sh
# Optional: specify a custom agent repo path
sudo bash install_agent_extension.sh /var/frappe/agent/repo
```

If `job.py` was previously patched with a `nextjs_jobs` import, remove it manually:

```bash
grep -n "nextjs" /var/frappe/agent/repo/agent/job.py
# If any lines appear, remove them — only server.py should import NextjsMixin
```

---

## 4. Copy Docker templates to the agent

```bash
cp -r next_frontend_provisioner/next_frontend_provisioner/templates/docker \
      /var/frappe/agent/repo/agent/templates/docker
```

---

## 5. Install Python dependencies on the agent server

```bash
/home/frappe/agent/env/bin/pip install docker requests ansible
```

---

## 6. Set environment variables

Add to `/home/frappe/agent/.env` or the agent's Procfile:

```
NFP_TEMPLATES_DIR=/var/frappe/agent/repo/agent/templates/docker
NFP_ANSIBLE_DIR=/home/frappe/agent/ansible
```

---

## 7. Test the agent import

After all changes, verify the agent loads cleanly:

```bash
cd /var/frappe/agent
source env/bin/activate
python -c "from agent.server import Server; print('OK')"
```

Then restart:

```bash
sudo supervisorctl restart agent:web
sudo supervisorctl status agent:web   # should show RUNNING
```

---

## 8. Test Ansible connectivity to proxy servers

```bash
ansible all -i /home/frappe/agent/ansible/inventory/proxies.ini -m ping
```

---

## 9. Set the callback token on the Press bench

```bash
bench --site your-press-site set-config nfp_agent_callback_token "your-secret-token"
```

The same token must be set in `frappe.conf` on the Press controller site and is
validated by `api.agent_job_update` when the agent posts job completion callbacks.