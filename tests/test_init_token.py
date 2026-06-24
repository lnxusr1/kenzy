"""F-2: kenzy-init generates a join token by default and wires it consistently
across server.yaml, the co-located node.yaml, and .env (KENZY_SERVICE_TOKEN)."""

from __future__ import annotations

import yaml

from kenzy.init import scaffold


def _token(yaml_path):
    data = yaml.safe_load(yaml_path.read_text())
    return (data.get("discovery") or {}).get("token")


def _env_token(env_path):
    for line in env_path.read_text().splitlines():
        if line.startswith("KENZY_SERVICE_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def test_all_profile_generates_and_syncs_token(tmp_path):
    scaffold(tmp_path, profile="all")
    server_tok = _token(tmp_path / "configs" / "server.yaml")
    node_tok = _token(tmp_path / "configs" / "node.yaml")
    env_tok = _env_token(tmp_path / ".env")
    assert server_tok and len(server_tok) >= 16  # generated, non-trivial
    assert node_tok == server_tok  # co-located node matches
    assert env_tok == server_tok  # services authenticate with the same bearer


def test_explicit_token_is_used(tmp_path):
    scaffold(tmp_path, profile="all", token="shared-secret-123")
    assert _token(tmp_path / "configs" / "server.yaml") == "shared-secret-123"
    assert _token(tmp_path / "configs" / "node.yaml") == "shared-secret-123"
    assert _env_token(tmp_path / ".env") == "shared-secret-123"


def test_token_is_stable_across_reruns(tmp_path):
    scaffold(tmp_path, profile="all")
    first = _token(tmp_path / "configs" / "server.yaml")
    scaffold(tmp_path, profile="all")  # re-run without --force
    assert _token(tmp_path / "configs" / "server.yaml") == first  # not rotated


def test_force_rerun_rotates_token(tmp_path):
    scaffold(tmp_path, profile="all")
    first = _token(tmp_path / "configs" / "server.yaml")
    scaffold(tmp_path, profile="all", force=True)
    second = _token(tmp_path / "configs" / "server.yaml")
    assert second and second != first
    # still consistent across files after a forced regen
    assert _token(tmp_path / "configs" / "node.yaml") == second
    assert _env_token(tmp_path / ".env") == second


def test_node_profile_sets_token_only_when_given(tmp_path):
    scaffold(tmp_path, profile="node", token="paste-me")
    assert _token(tmp_path / "configs" / "node.yaml") == "paste-me"

    other = tmp_path / "n2"
    scaffold(other, profile="node")  # no token → stays unset (commented template)
    assert _token(other / "configs" / "node.yaml") is None
