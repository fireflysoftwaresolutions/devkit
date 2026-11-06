# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Git hosts, CI/CD and cloud providers a developer works with: which CLIs they need, whether those are installed
and signed in, and what this Mac already shows (repositories under the workspace, config folders, CLIs).

The wizard asks for them (`hosts`, `clouds` in the devkit config), `dev install` installs their Homebrew group
(Brewfile `# @<id>`), `dev doctor` checks installation and sign-in, and `dev ci status` uses the same credentials.
All checks are local and fast: they read the CLIs' own credential caches, never the network.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import core
from .core import HOME

# id -> label, CLI, local sign-in check (argv, or "env:VAR|VAR2+VAR3"), how to sign in, Brewfile group
HOSTS = {
    "github": ("GitHub · Actions", "gh", ["gh", "auth", "status"], "gh auth login", None),
    "gitlab": ("GitLab · GitLab CI", "glab", ["glab", "auth", "status"], "glab auth login", "gitlab"),
    "bitbucket": ("Bitbucket · Pipelines", None, "env:BITBUCKET_TOKEN|BITBUCKET_USERNAME+BITBUCKET_APP_PASSWORD",
                  "create an access token with pipeline:read and export BITBUCKET_TOKEN (no official CLI)", None),
    "azure-devops": ("Azure DevOps · Pipelines", "az", "env:AZURE_DEVOPS_EXT_PAT",
                     "az extension add --name azure-devops; export AZURE_DEVOPS_EXT_PAT (Build: Read)", "azure-devops"),
}
CLOUDS = {
    "aws": ("AWS", "aws", ["aws", "configure", "list-profiles"], "aws configure sso", "aws"),
    "azure": ("Microsoft Azure", "az", ["az", "account", "show", "--only-show-errors"], "az login", "azure"),
    "gcp": ("Google Cloud", "gcloud", ["gcloud", "auth", "list", "--format=value(account)"], "gcloud auth login", "gcp"),
    "digitalocean": ("DigitalOcean", "doctl", ["doctl", "auth", "list"], "doctl auth init", "digitalocean"),
    "kubernetes": ("Kubernetes · Helm", "kubectl", ["kubectl", "config", "current-context"],
                   "get a kubeconfig from your cluster provider", "kubernetes"),
    "terraform": ("OpenTofu / Terraform", "tofu", ["tofu", "version"], "-", "terraform"),
}
ALL = {**HOSTS, **CLOUDS}


def selected(cfg=None) -> list:
    cfg = cfg or core.CFG
    return list(dict.fromkeys(["github"] + list(cfg.get("hosts") or []) + list(cfg.get("clouds") or [])))


def groups(cfg=None) -> list:
    """Brewfile groups for the selected hosts and clouds."""
    return [ALL[k][4] for k in selected(cfg) if k in ALL and ALL[k][4]]


def detect() -> dict:
    """What this Mac already uses: {"hosts": [...], "clouds": [...]}, with the evidence for each."""
    found = {}
    dev = core.DEV
    host_dirs = {e.name.lower() for e in os.scandir(dev) if e.is_dir()} if dev.exists() else set()
    if any("gitlab" in h for h in host_dirs) or core.have("glab"):
        found["gitlab"] = "GitLab repositories or glab"
    if "bitbucket.org" in host_dirs or os.environ.get("BITBUCKET_TOKEN"):
        found["bitbucket"] = "Bitbucket repositories or token"
    if "dev.azure.com" in host_dirs or os.environ.get("AZURE_DEVOPS_EXT_PAT"):
        found["azure-devops"] = "Azure DevOps repositories or token"
    for k, path in (("aws", HOME / ".aws"), ("azure", HOME / ".azure"), ("gcp", HOME / ".config" / "gcloud"),
                    ("digitalocean", HOME / ".config" / "doctl"), ("kubernetes", HOME / ".kube" / "config")):
        if path.exists():
            found[k] = f"{core.tilde(path)} exists"
    for k in ("aws", "gcp", "digitalocean", "kubernetes", "terraform"):
        if k not in found and core.have(ALL[k][1]):
            found[k] = f"{ALL[k][1]} installed"
    if "terraform" not in found and core.have("terraform"):
        found["terraform"] = "terraform installed"
    return {"hosts": [k for k in HOSTS if k in found or k == "github"],
            "clouds": [k for k in CLOUDS if k in found], "evidence": found}


def check(key: str) -> dict:
    """{installed, signed_in, detail, fix} — local checks only."""
    label, cli, probe, how, _ = ALL[key]
    installed = core.have(cli) if cli else True
    if key == "terraform" and not installed and core.have("terraform"):
        installed, cli = True, "terraform"
        probe = ["terraform", "version"]
    if not installed:
        return {"installed": False, "signed_in": False, "detail": f"{cli} not installed",
                "fix": "dev install --only tools"}
    if isinstance(probe, str) and probe.startswith("env:"):
        options = probe[4:].split("|")
        ok = any(all(os.environ.get(v) for v in opt.split("+")) for opt in options)
        extra = ""
        if key == "azure-devops" and cli and core.sh(["az", "extension", "show", "--name", "azure-devops",
                                                      "--only-show-errors"], timeout=20)[0] != 0:
            return {"installed": True, "signed_in": ok, "detail": "az devops extension missing",
                    "fix": "az extension add --name azure-devops"}
        return {"installed": True, "signed_in": ok,
                "detail": ("token set" if ok else f"no token ({' or '.join(options)})") + extra, "fix": how}
    rc, out, err = core.sh(probe, timeout=20)
    ok = rc == 0 and bool((out or err).strip()) if key in ("aws", "gcp") else rc == 0
    if key == "terraform":
        return {"installed": True, "signed_in": True, "detail": (out.splitlines() or [""])[0], "fix": ""}
    detail = (out or err).strip().splitlines()[0][:80] if (out or err).strip() else ""
    if key == "aws" and ok:
        detail = f"{len(out.split())} profile(s)"
    if key == "azure" and ok:
        detail = "signed in"
    return {"installed": True, "signed_in": ok, "detail": detail or ("signed in" if ok else "not signed in"),
            "fix": how}


def postinstall(keys) -> list:
    """Extra steps after Homebrew (e.g. the Azure DevOps CLI extension). -> lines describing what was done."""
    out = []
    if "azure-devops" in keys and core.have("az"):
        rc, _, _ = core.sh(["az", "extension", "show", "--name", "azure-devops", "--only-show-errors"], timeout=30)
        if rc != 0:
            rc, _, err = core.sh(["az", "extension", "add", "--name", "azure-devops", "--only-show-errors"], timeout=300)
            out.append("az devops extension installed" if rc == 0 else f"az devops extension: {err[:120]}")
    return out
