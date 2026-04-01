"""
template_injector.py
--------------------
Selectively injects Next.js build infrastructure files into a tenant
repo after git clone/pull, before docker build.

Injection policy:
  Dockerfile            — injected if absent; overwritten only if it carries our header
  next.config.*         — injected if absent; patched to add output:'standalone' if missing
  src/app/api/health/   — always injected (required for container health checks)
  pages/api/health.ts   — injected if Pages Router detected and file absent
  .nfp-manifest.json    — always written (audit trail, add to .gitignore)
"""
import json
import os
import re
import shutil
import textwrap
from datetime import datetime, timezone
from pathlib import Path

_HERE         = Path(__file__).parent
TEMPLATES_DIR = Path(os.environ.get(
    "NFP_TEMPLATES_DIR",
    _HERE / "templates" / "docker",
))

_CONFIG_FILES       = ["next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"]
_STANDALONE_PATTERN = re.compile(r"""output\s*:\s*['"]standalone['"]""", re.IGNORECASE)

_HEALTH_APP = textwrap.dedent("""\
    // Injected by next_frontend_provisioner — do not remove.
    // Required for container health checks.
    export async function GET() {
      return Response.json({ status: 'ok', ts: Date.now() })
    }
""")

_HEALTH_PAGES = textwrap.dedent("""\
    // Injected by next_frontend_provisioner — do not remove.
    import type { NextApiRequest, NextApiResponse } from 'next'

    export default function handler(_req: NextApiRequest, res: NextApiResponse) {
      res.status(200).json({ status: 'ok', ts: Date.now() })
    }
""")


def inject_templates(repo_dir: str, site_name: str, params: dict):
    """Entry point — called immediately after git clone/pull."""
    root = Path(repo_dir)
    _inject_dockerfile(root)
    _inject_or_patch_next_config(root, site_name)
    _inject_health_route(root)
    _write_manifest(root, site_name)


# ── Dockerfile ───────────────────────────────────────────────────────

def _inject_dockerfile(root: Path):
    dest    = root / "Dockerfile"
    opt_out = root / ".nfp-dockerfile"
    src     = TEMPLATES_DIR / "Dockerfile"

    if not src.exists():
        return
    if dest.exists() and not opt_out.exists():
        existing = dest.read_text()
        if "next_frontend_provisioner" in existing or "managed by" in existing.lower():
            shutil.copy2(src, dest)
        # else: tenant custom Dockerfile — leave alone
    elif not dest.exists():
        shutil.copy2(src, dest)


# ── next.config.* ────────────────────────────────────────────────────

def _inject_or_patch_next_config(root: Path, site_name: str):
    # copy app-config file
    shutil.copy2(root /"services" /"config" / "app-config-example.ts", root /"services" /"config" / "app-config.ts")
    existing = _find_config(root)

    if existing is None:
        src     = TEMPLATES_DIR / "next.config.js"
        dest    = root / "next.config.js"
        content = src.read_text() if src.exists() else _default_next_config(site_name)
        content = content.replace(
            "process.env.FRAPPE_HOSTNAME || 'localhost'",
            f"process.env.FRAPPE_HOSTNAME || '{site_name}'",
        )
        dest.write_text(content)
        return

    config_path = root / existing
    content     = config_path.read_text()
    if _STANDALONE_PATTERN.search(content):
        return  # already correct

    patched = _patch_standalone(content)
    if patched:
        config_path.write_text(patched)
    else:
        _write_sidecar(root, existing)


def _find_config(root: Path):
    for name in _CONFIG_FILES:
        if (root / name).exists():
            return name
    return None


def _patch_standalone(content: str):
    pattern = re.compile(
        r"""((?:module\.exports\s*=|export\s+default)\s*(?:\w+\s*\()?\s*\{)""",
        re.MULTILINE,
    )
    m = pattern.search(content)
    if not m:
        return None
    injection = "\n  output: 'standalone',  // injected by next_frontend_provisioner"
    return content[:m.end()] + injection + content[m.end():]


def _write_sidecar(root: Path, original: str):
    (root / "next.config.js").write_text(textwrap.dedent(f"""\
        // next_frontend_provisioner: standalone wrapper
        const userConfig = require('./{original}');
        const base = typeof userConfig === 'function'
            ? userConfig()
            : userConfig?.default ?? userConfig;
        /** @type {{import('next').NextConfig}} */
        module.exports = {{ ...base, output: 'standalone' }};
    """))


def _default_next_config(site_name: str) -> str:
    return textwrap.dedent(f"""\
        /** @type {{import('next').NextConfig}} */
        const nextConfig = {{
          output: 'standalone',
          images: {{
            remotePatterns: [{{
              protocol: 'https',
              hostname: process.env.FRAPPE_HOSTNAME || '{site_name}',
            }}],
          }},
        }}
        module.exports = nextConfig
    """)


# ── Health route ─────────────────────────────────────────────────────

def _inject_health_route(root: Path):
    uses_app   = (root / "src" / "app").exists() or (root / "app").exists()
    uses_pages = (root / "pages").exists()

    if uses_app:
        app_root = root / "src" / "app" if (root / "src" / "app").exists() else root / "app"
        dest = app_root / "api" / "health" / "route.ts"        
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_HEALTH_APP)

    if uses_pages and not uses_app:
        dest = root / "pages" / "api" / "health.ts"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(_HEALTH_PAGES)


# ── Manifest ─────────────────────────────────────────────────────────

def _write_manifest(root: Path, site_name: str):
    path = root / ".nfp-manifest.json"
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass

    managed = [
        f for f in [
            "Dockerfile", "next.config.js",
            "src/app/api/health/route.ts",
            "app/api/health/route.ts",
            "pages/api/health.ts",
        ] if (root / f).exists()
    ]

    path.write_text(json.dumps({
        **existing,
        "site":          site_name,
        "last_injected": datetime.now(timezone.utc).isoformat(),
        "managed_files": managed,
        "version":       "0.1.0",
    }, indent=2))
