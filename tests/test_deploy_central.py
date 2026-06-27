"""kenzy-deploy provisions into the central, dashboard-managed model:

- backend services run arg-less (pull their config from the server) so they're
  dashboard-managed; node/server keep an explicit local config;
- a per-host node_id slug is read from deploy.yaml and baked into node.yaml;
- the central store (configs/nodes, configs/services) is seeded don't-clobber so a
  re-deploy never overwrites dashboard edits (and --reseed forces overwrite).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kenzy.deploy.deploy import (
    _NODE_ID_PATCH,
    HostConfig,
    _load_hosts,
    _seed_central_config,
    _unit_content,
)


def _host(**kw: object) -> HostConfig:
    base: dict[str, object] = dict(
        name="h",
        address="1.2.3.4",
        ssh_user="pi",
        install_path="/opt/kenzy",
        venv_path="/opt/kenzy/.venv",
        python_bin="python3",
    )
    base.update(kw)
    return HostConfig(**base)  # type: ignore[arg-type]


# --- pull mode vs local config ----------------------------------------------


@pytest.mark.parametrize("svc", ["stt", "tts", "llm", "speaker"])
def test_service_units_are_argless_pull_mode(svc: str) -> None:
    unit = _unit_content(svc, _host(services=[svc]))
    # arg-less ExecStart (no config path) → serviceboot pull
    assert f"ExecStart=/opt/kenzy/.venv/bin/kenzy-{svc}\n" in unit
    assert "configs/" not in unit.split("ExecStart=")[1].splitlines()[0]
    # ordered after the server
    assert "After=kenzy-server.service" in unit


@pytest.mark.parametrize("svc,cfg", [("node", "node.yaml"), ("server", "server.yaml")])
def test_node_and_server_keep_local_config(svc: str, cfg: str) -> None:
    unit = _unit_content(svc, _host(services=[svc]))
    assert f"ExecStart=/opt/kenzy/.venv/bin/kenzy-{svc} /opt/kenzy/configs/{cfg}\n" in unit
    assert "After=kenzy-server.service" not in unit


# --- node_id from deploy.yaml -----------------------------------------------


def test_node_id_loaded_from_deploy_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(
        "hosts:\n"
        "  kitchen:\n"
        "    address: 10.0.0.5\n"
        "    services: [node]\n"
        "    node_id: kitchen-pi\n"
        "  garage:\n"
        "    address: 10.0.0.6\n"
        "    services: [node]\n"
    )
    hosts = {h.name: h for h in _load_hosts(str(cfg))}
    assert hosts["kitchen"].node_id == "kitchen-pi"
    assert hosts["garage"].node_id is None  # omitted → node self-generates


# --- node_id patch one-liner ------------------------------------------------


def _apply_patch(text: str, slug: str, tmp_path: Path) -> str:
    f = tmp_path / "node.yaml"
    f.write_text(text)
    subprocess.run([sys.executable, "-c", _NODE_ID_PATCH, str(f), slug], check=True)
    return f.read_text()


def test_patch_replaces_commented_node_id(tmp_path: Path) -> None:
    out = _apply_patch("log_level: info\n# node_id: null\nverbose: false\n", "kitchen-pi", tmp_path)
    assert 'node_id: "kitchen-pi"' in out
    assert out.count("node_id:") == 1  # replaced, not duplicated


def test_patch_replaces_existing_node_id(tmp_path: Path) -> None:
    out = _apply_patch('node_id: "old"\n', "new-id", tmp_path)
    assert out.strip() == 'node_id: "new-id"'


def test_patch_appends_when_absent(tmp_path: Path) -> None:
    out = _apply_patch("log_level: info\n", "garage", tmp_path)
    assert "log_level: info" in out and 'node_id: "garage"' in out


# --- seed-don't-clobber ------------------------------------------------------


def _seed_setup(tmp_path: Path) -> tuple[HostConfig, Path, Path]:
    root = tmp_path / "operator"
    install = tmp_path / "install"
    (root / "configs" / "nodes").mkdir(parents=True)
    (install / "configs").mkdir(parents=True)
    (root / "configs" / "nodes" / "kitchen-pi.yaml").write_text("volume: 80\n")
    host = _host(local=True, install_path=str(install), services=["server"])
    return host, root, install


def test_seed_copies_when_absent(tmp_path: Path) -> None:
    host, root, install = _seed_setup(tmp_path)
    _seed_central_config(host, root, reseed=False)
    assert (install / "configs" / "nodes" / "kitchen-pi.yaml").read_text() == "volume: 80\n"


def test_seed_does_not_clobber_existing(tmp_path: Path) -> None:
    host, root, install = _seed_setup(tmp_path)
    dest = install / "configs" / "nodes" / "kitchen-pi.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("volume: 30\n")  # simulate a dashboard edit on the server
    _seed_central_config(host, root, reseed=False)
    assert dest.read_text() == "volume: 30\n"  # preserved


def test_reseed_overwrites_existing(tmp_path: Path) -> None:
    host, root, install = _seed_setup(tmp_path)
    dest = install / "configs" / "nodes" / "kitchen-pi.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("volume: 30\n")
    _seed_central_config(host, root, reseed=True)
    assert dest.read_text() == "volume: 80\n"  # operator value forced back


def test_seed_skips_non_server_host(tmp_path: Path) -> None:
    host, root, install = _seed_setup(tmp_path)
    host.services = ["node"]  # not a server → central store doesn't live here
    _seed_central_config(host, root, reseed=False)
    assert not (install / "configs" / "nodes" / "kitchen-pi.yaml").exists()
