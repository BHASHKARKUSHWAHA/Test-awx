"""The configuration files are part of the contract, so they are tested."""

from __future__ import annotations

import os

import pytest
import yaml

from installer import minyaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPAT = os.path.join(REPO_ROOT, "config", "compatibility.yaml")
POLICY = os.path.join(REPO_ROOT, "policies", "permissions.yaml")


@pytest.mark.parametrize("path", [COMPAT, POLICY])
def test_mini_yaml_matches_pyyaml(path):
    """The dependency-free fallback parser must agree with PyYAML exactly."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    rows = minyaml._tokenize(text)
    parsed, _ = minyaml._parse_block(rows, 0, rows[0][0])
    assert parsed == yaml.safe_load(text)


@pytest.mark.parametrize(
    "document",
    [
        "key: {inline: mapping}",   # flow mappings are not supported
        "&anchor\nkey: value",      # anchors are not supported
        "key: |\n  block scalar",   # block scalars are not supported
    ],
)
def test_mini_yaml_refuses_what_it_cannot_parse(document):
    """Loudly unsupported beats silently half-parsed."""
    rows = minyaml._tokenize(document) if "{" in document else None
    with pytest.raises(minyaml.MiniYamlError):
        if rows is not None:
            minyaml._parse_block(rows, 0, rows[0][0])
        else:
            minyaml._parse_block(minyaml._tokenize(document), 0, 0)


def test_every_profile_has_sizing_and_models(config):
    for profile in config["profiles"]:
        sizing = config["sizing"][profile]
        assert sizing["awx"]["web_mem_limit"]
        assert sizing["models"], "profile %s has no model candidates" % profile
        assert len(sizing["models"]) >= 2, "profile %s has no fallback model" % profile
        assert config["versions"]["k3s_exec"][profile]


def test_profiles_cover_the_ram_range(config):
    from installer.hardware import select_profile

    cases = {2048: "LOW", 4096: "LOW", 6000: "TEST", 8192: "TEST", 12000: "NORMAL", 16384: "NORMAL", 32000: "HIGH"}
    for ram, expected in cases.items():
        assert select_profile(ram, config["profiles"]) == expected, "%d MB should map to %s" % (ram, expected)


def test_eight_gb_machine_uses_a_small_model(config):
    """Spec section 4/13: an 8 GB box must not pull a large model."""
    from installer.hardware import select_profile

    profile = select_profile(8192, config["profiles"])
    first_model = config["sizing"][profile]["models"][0]
    size_token = first_model.split(":", 1)[1]
    billions = float(size_token.split("b")[0].replace("-instruct", "").strip())
    assert billions <= 3.0, "profile %s would pull %s on an 8 GB machine" % (profile, first_model)
    assert "q4" in first_model.lower(), "the 8 GB model should be quantised"


def test_policy_denies_automatic_writes_above_safe(config):
    from installer.common import load_yaml

    policy = load_yaml(POLICY)["policy"]
    assert policy["auto_medium_risk"] is False
    assert policy["auto_high_risk"] is False
    assert policy["max_tool_calls_per_request"] >= 1
