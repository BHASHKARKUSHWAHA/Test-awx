"""Shared pytest fixtures.

The tests exercise the real modules against a temporary policy file and a
temporary audit log, so nothing under /etc, /var or /opt is touched.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


@pytest.fixture(scope="session", autouse=True)
def isolated_environment(tmp_path_factory):
    """Point every file-backed component at a throwaway directory."""
    sandbox = tmp_path_factory.mktemp("smart-awx-ai")
    policy_src = os.path.join(REPO_ROOT, "policies", "permissions.yaml")
    policy_dst = sandbox / "permissions.yaml"
    shutil.copy2(policy_src, policy_dst)

    os.environ["AGENT_POLICY_PATH"] = str(policy_dst)
    os.environ["AGENT_AUDIT_LOG"] = str(sandbox / "audit.log")
    os.environ["AGENT_REGISTRY_PATH"] = str(sandbox / "agents.json")
    os.environ["AGENT_SESSION_SECRET"] = "test-secret-not-used-in-production"
    os.environ["AGENT_UI_DIR"] = os.path.join(REPO_ROOT, "ui", "ai-agent")
    os.environ["AWX_URL"] = "http://127.0.0.1:65535"  # nothing listens here
    os.environ["OLLAMA_URL"] = "http://127.0.0.1:65534"
    yield sandbox


@pytest.fixture(scope="session")
def tools_loaded(isolated_environment):
    import tools

    tools.load_all()
    from tools import registry

    return registry


@pytest.fixture
def config():
    from installer.common import load_yaml

    return load_yaml(os.path.join(REPO_ROOT, "config", "compatibility.yaml"))
