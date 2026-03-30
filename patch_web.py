"""
Applies the correct web.py patch for /frontends routes.
Run as: sudo python3 patch_web.py
"""
path = "/var/frappe/agent/repo/agent/web.py"
with open(path) as f:
    content = f.read()

# ------------------------------------------------------------------
# The new route implementation — no f-strings with newlines,
# no @job decorator dependency, no job_record needed.
# Uses RQ directly, same as the agent does internally.
# ------------------------------------------------------------------
NEW_ROUTES = '''
@application.route("/frontends/<string:name>/deploy", methods=["POST"])
def deploy_frontend(name):
    """
    Deploy a Next.js frontend.
    Patched by next_frontend_provisioner — uses RQ directly
    so no job_record initialization is needed.
    """
    import uuid
    data = request.json or {}
    env_vars        = data.get("env_vars") or data.get("env") or {}
    deployment_mode = data.get("deployment_mode", "Full Stack")
    backend_url     = data.get("backend_url", "")
    repo            = data.get("repo", "")
    branch          = data.get("branch", "main")
    port            = data.get("port", 3000)

    from agent.job import queue as _queue
    job_id = "nfp-" + name + "-" + uuid.uuid4().hex[:8]
    _queue("default").enqueue(
        _nfp_run_deploy,
        name, repo, branch, port, env_vars, deployment_mode, backend_url,
        job_id=job_id,
        job_timeout=1800,
        result_ttl=86400,
    )
    return {"job": job_id, "status": "queued"}


def _nfp_run_deploy(name, repo, branch, port,
                    env_vars, deployment_mode, backend_url):
    """RQ worker — runs Docker deploy steps. No job_record needed."""
    import json as _j
    import os
    import shutil
    import subprocess

    def _run(cmd, check=True):
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        print("[NFP]", cmd[:120])
        if r.stdout:
            print(r.stdout[:500])
        if r.returncode != 0:
            print("STDERR:", r.stderr[:500])
            if check:
                msg = "Command failed: " + cmd + " — " + r.stderr[:200]
                raise RuntimeError(msg)
        return r.stdout.strip()

    # ── 1. Clone / pull ──────────────────────────────────────────────
    work_dir = "/tmp/nfp-" + name
    if os.path.exists(os.path.join(work_dir, ".git")):
        _run("git -C " + work_dir + " fetch origin " + branch)
        _run("git -C " + work_dir + " checkout " + branch)
        _run("git -C " + work_dir + " reset --hard origin/" + branch)
    else:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
        _run("git clone --depth 1 --branch " + branch + " " + repo + " " + work_dir)

    # ── 2. Build Docker image ────────────────────────────────────────
    image_tag = "nfp-frontend-" + name.lower() + ":latest"
    build_args = ""
    for k, v in env_vars.items():
        if k.startswith("NEXT_PUBLIC_"):
            safe = str(v).replace('"', '\\"')
            build_args += " --build-arg " + k + '="' + safe + '"'
    _run("docker build" + build_args + " -t " + image_tag + " " + work_dir)

    # ── 3. Stop old container ────────────────────────────────────────
    _run("docker stop " + name, check=False)
    _run("docker rm   " + name, check=False)

    # ── 4. Start container ───────────────────────────────────────────
    env_flags = ""
    for k, v in env_vars.items():
        safe = str(v).replace('"', '\\"')
        env_flags += " -e " + k + '="' + safe + '"'
    _run(
        "docker run -d --restart always"
        + " --name " + name
        + env_flags
        + " -p 127.0.0.1:" + str(port) + ":3000"
        + " " + image_tag
    )

    # ── 5. Write nginx config ────────────────────────────────────────
    try:
        conf_dir = "/home/frappe/agent/nginx"
        for cfg_path in ("/var/frappe/agent/config.json",
                         "/home/frappe/agent/config.json"):
            try:
                cfg = _j.load(open(cfg_path))
                conf_dir = cfg.get("nginx_directory", conf_dir)
                break
            except Exception:
                pass
        from agent.nginx_utils import write_upstream
        write_upstream(
            site_name=name,
            container_name=name,
            port=port,
            conf_dir=conf_dir,
            deployment_mode=deployment_mode,
            backend_url=backend_url,
        )
        print("[NFP] nginx config written to " + conf_dir)
    except Exception as exc:
        print("[NFP] nginx warning (non-fatal): " + str(exc))


@application.route("/frontends/<string:name>", methods=["DELETE"])
def remove_frontend(name):
    import uuid
    from agent.job import queue as _queue
    job_id = "nfp-rm-" + name + "-" + uuid.uuid4().hex[:8]
    _queue("default").enqueue(
        _nfp_run_remove, name,
        job_id=job_id,
        job_timeout=120,
    )
    return {"job": job_id, "status": "queued"}


def _nfp_run_remove(name):
    import json as _j, subprocess
    subprocess.run("docker stop " + name, shell=True, capture_output=True)
    subprocess.run("docker rm   " + name, shell=True, capture_output=True)
    try:
        conf_dir = "/home/frappe/agent/nginx"
        for cfg_path in ("/var/frappe/agent/config.json",
                         "/home/frappe/agent/config.json"):
            try:
                cfg = _j.load(open(cfg_path))
                conf_dir = cfg.get("nginx_directory", conf_dir)
                break
            except Exception:
                pass
        from agent.nginx_utils import remove_upstream
        remove_upstream(name, conf_dir=conf_dir)
    except Exception as exc:
        print("[NFP] nginx remove warning: " + str(exc))

'''

# ------------------------------------------------------------------
# Find and replace the existing deploy/remove block (any version)
# ------------------------------------------------------------------
import re

# Pattern matches everything from the deploy route decorator
# through to the end of remove_frontend
pattern = re.compile(
    r'@application\.route\("/frontends/<string:name>/deploy".*?'
    r'(?=\n@application\.route|\nclass |\Z)',
    re.DOTALL
)

match = pattern.search(content)
if match:
    content = content[:match.start()] + NEW_ROUTES + content[match.end():]
    with open(path, "w") as f:
        f.write(content)
    print("SUCCESS: web.py patched cleanly")
    # Verify syntax
    import py_compile, sys
    try:
        py_compile.compile(path, doraise=True)
        print("Syntax check: PASSED")
    except py_compile.PyCompileError as e:
        print("Syntax check: FAILED —", e)
        sys.exit(1)
else:
    print("Pattern not found — showing frontend-related lines:")
    for i, line in enumerate(content.splitlines(), 1):
        if "frontend" in line.lower() and ("route" in line.lower() or "def " in line.lower()):
            print(f"  {i}: {line}")
