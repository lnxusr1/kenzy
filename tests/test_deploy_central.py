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


# --- server URL auto-derivation + --listen-all ------------------------------


def _fleet_yaml(tmp_path: Path, extra: str = "") -> str:
    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(
        f"{extra}"
        "hosts:\n"
        "  pc:\n"
        "    address: officepc.lan\n"
        "    services: [server, llm]\n"
        "  bedroom:\n"
        "    address: 10.0.0.5\n"
        "    services: [stt]\n"
    )
    return str(cfg)


def test_server_url_derived_from_fleet(tmp_path: Path) -> None:
    hosts = {h.name: h for h in _load_hosts(_fleet_yaml(tmp_path))}
    # Co-located service uses loopback; a remote service uses the server host's address.
    assert hosts["pc"].server_url == "ws://127.0.0.1:8765"
    assert hosts["bedroom"].server_url == "ws://officepc.lan:8765"


def test_explicit_server_url_and_port_override(tmp_path: Path) -> None:
    hosts = {h.name: h for h in _load_hosts(_fleet_yaml(tmp_path, "server_port: 9000\n"))}
    assert hosts["bedroom"].server_url == "ws://officepc.lan:9000"
    hosts = {h.name: h for h in _load_hosts(_fleet_yaml(tmp_path, "server_url: ws://kenzy.lan:8765\n"))}
    assert hosts["pc"].server_url == "ws://kenzy.lan:8765"  # explicit wins everywhere


def test_no_server_in_fleet_means_no_url(tmp_path: Path) -> None:
    cfg = tmp_path / "deploy.yaml"
    cfg.write_text("hosts:\n  box:\n    address: 10.0.0.9\n    services: [stt]\n")
    hosts = {h.name: h for h in _load_hosts(str(cfg))}
    assert hosts["box"].server_url is None  # falls back to mDNS


def test_pull_unit_has_server_url_and_listen_all_env(tmp_path: Path) -> None:
    hosts = {h.name: h for h in _load_hosts(_fleet_yaml(tmp_path), listen_all=True)}
    unit = _unit_content("llm", hosts["pc"])
    assert "Environment=KENZY_SERVER_URL=ws://127.0.0.1:8765" in unit
    assert "Environment=KENZY_BIND=0.0.0.0" in unit
    # Without --listen-all there's no KENZY_BIND.
    hosts2 = {h.name: h for h in _load_hosts(_fleet_yaml(tmp_path))}
    assert "KENZY_BIND" not in _unit_content("llm", hosts2["pc"])


# --- extras (kokoro / mqtt) -------------------------------------------------


def test_non_service_entries_route_to_extras(tmp_path: Path) -> None:
    from kenzy.deploy.deploy import _pip_extras, _unit_name

    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(
        "hosts:\n"
        "  pc:\n"
        "    address: x\n"
        "    services: [server, tts, kokoro, mqtt]\n"  # kokoro/mqtt are extras, not services
    )
    host = next(iter(_load_hosts(str(cfg))))
    assert host.services == ["server", "tts"]  # only runnable services get units
    assert host.extras == ["kokoro", "mqtt"]
    assert _pip_extras(host, tmp_path) == "server,tts,kokoro,mqtt"
    # No bogus kenzy-kokoro / kenzy-mqtt units.
    assert [_unit_name(s) for s in host.services] == ["kenzy-server.service", "kenzy-tts.service"]


def test_explicit_extras_field(tmp_path: Path) -> None:
    from kenzy.deploy.deploy import _pip_extras

    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(
        "hosts:\n  pc:\n    address: x\n    services: [server, llm]\n    extras: [mqtt]\n"
    )
    host = next(iter(_load_hosts(str(cfg))))
    assert host.extras == ["mqtt"]
    assert _pip_extras(host, tmp_path) == "server,llm,mqtt"


def test_kokoro_autodetected_from_central_tts_config(tmp_path: Path) -> None:
    from kenzy.deploy.deploy import _pip_extras

    (tmp_path / "configs" / "services").mkdir(parents=True)
    (tmp_path / "configs" / "services" / "tts.yaml").write_text("provider: kokoro\n")
    cfg = tmp_path / "configs" / "deploy.yaml"
    cfg.write_text("hosts:\n  pc:\n    address: x\n    services: [server, tts]\n")
    host = next(iter(_load_hosts(str(cfg))))
    assert "kokoro" in _pip_extras(host, tmp_path).split(",")


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


# --- dashboard-owned state survives upgrades ---------------------------------


def test_dashboard_owned_paths_protected_from_sync() -> None:
    """Everything the dashboard writes on a deploy host must be excluded from the
    overwriting/--delete rsyncs in BOTH modes, or `kenzy-deploy upgrade` wipes live
    edits (the bug: Settings-tab server config vanished on every upgrade)."""
    from kenzy.deploy.deploy import _CONFIG_SYNC_EXCLUDES, RSYNC_EXCLUDES

    # source mode (full-tree rsync; root-anchored paths)
    for path in ("/configs/nodes/", "/configs/services/", "/configs/server.local.yaml", "/.env"):
        assert path in RSYNC_EXCLUDES
    # pypi mode (configs-only rsync; paths relative to configs/)
    for path in ("nodes/", "services/", "server.local.yaml"):
        assert path in _CONFIG_SYNC_EXCLUDES


# --- volume buttons: default-on for nodes, held out of the main pip spec -----
# The recommended room node is a USB speakerphone with AEC, and those carry
# volume keys — so `mediakeys` rides along by default rather than being a flag
# nobody knows to pass. It stays OUT of the main pip extras because evdev ships
# source-only on PyPI: a failed build must cost the buttons, not the install.


def _nodes_yaml(tmp_path: Path, body: str) -> str:
    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(f"hosts:\n{body}")
    return str(cfg)


def test_node_hosts_get_mediakeys_by_default(tmp_path: Path) -> None:
    hosts = {
        h.name: h
        for h in _load_hosts(
            _nodes_yaml(
                tmp_path,
                "  kitchen:\n    address: 1.1.1.1\n    services: [node]\n"
                "  brain:\n    address: 1.1.1.2\n    services: [server, llm]\n",
            )
        )
    }
    assert "mediakeys" in hosts["kitchen"].extras
    # A server-only host has no audio device and nothing to listen to.
    assert "mediakeys" not in hosts["brain"].extras


@pytest.mark.parametrize("scope", ["host", "defaults"])
def test_media_keys_false_opts_out(tmp_path: Path, scope: str) -> None:
    body = "  kitchen:\n    address: 1.1.1.1\n    services: [node]\n"
    if scope == "host":
        body += "    media_keys: false\n"
    else:
        body = "defaults:\n  media_keys: false\n" + "hosts:\n" + body
        cfg = tmp_path / "deploy.yaml"
        cfg.write_text(body)
        hosts = {h.name: h for h in _load_hosts(str(cfg))}
        assert "mediakeys" not in hosts["kitchen"].extras
        return
    hosts = {h.name: h for h in _load_hosts(_nodes_yaml(tmp_path, body))}
    assert "mediakeys" not in hosts["kitchen"].extras


def test_mediakeys_is_excluded_from_the_main_pip_spec(tmp_path: Path) -> None:
    """A source build that fails must not be able to abort the whole install."""
    from kenzy.deploy.deploy import _pip_extras

    host = _host(services=["node"], extras=["mediakeys", "mqtt"])
    extras = _pip_extras(host, tmp_path).split(",")
    assert "mediakeys" not in extras
    # Everything else still rides along — including `wakeword`, which a node
    # host gains automatically now that the engine is its own extra.
    assert extras == ["node", "mqtt", "wakeword"]
