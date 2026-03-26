# Agent fork integration guide

## 1. Copy these files into your frappe/agent fork

```
agent_extension/agent_jobs.py        → agent/nextjs_jobs.py
agent_extension/proxy_manager.py     → agent/proxy_manager.py
agent_extension/template_injector.py → agent/template_injector.py
agent_extension/nginx_utils.py       → agent/nginx_utils.py
```

Also copy the entire `ansible/` directory to the agent server:
```bash
scp -r ansible/ frappe@agent-server:/home/frappe/agent/ansible/
```

## 2. Register job classes in agent/job.py

Find the JOB_CLASSES dict in agent/job.py and extend it:

```python
from agent.nextjs_jobs import (
    ProvisionNextjsSiteJob,
    TeardownNextjsSiteJob,
    RedeployNextjsSiteJob,
)

JOB_CLASSES = {
    **JOB_CLASSES,
    "Provision Next.js Site": ProvisionNextjsSiteJob,
    "Teardown Next.js Site":  TeardownNextjsSiteJob,
    "Redeploy Next.js Site":  RedeployNextjsSiteJob,
}
```

## 3. Set environment variables on the agent server

Add to /home/frappe/agent/.env or your agent Procfile:

```
NFP_TEMPLATES_DIR=/home/frappe/agent/agent/templates/docker
NFP_ANSIBLE_DIR=/home/frappe/agent/ansible
```

## 4. Copy Docker templates to agent

```bash
cp -r next_frontend_provisioner/templates/docker \
      /home/frappe/agent/agent/templates/docker
```

## 5. Install dependencies on agent server

```bash
pip install docker requests ansible --break-system-packages
```

## 6. Test SSH from agent server to each proxy server

```bash
ansible all -i /home/frappe/agent/ansible/inventory/proxies.ini -m ping
```

## 7. Set callback token on the Press controller bench

```bash
bench --site your-press-site set-config nfp_agent_callback_token "your-secret-token"
```
