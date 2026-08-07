"""kenzy-setup must SKIP what isn't installed, never crash on it.

It runs on every host — node, server, speaker — and downloads whatever that host
needs. A step that raises instead of skipping takes the later steps with it, so
the failure isn't "one model missing", it's "the model you actually ran this
for was never fetched".
"""

from __future__ import annotations

import logging

import kenzy.setup as setup


def test_openwakeword_step_skips_when_the_package_is_absent(monkeypatch, caplog):
    """The original bug: the try/except wrapped the IMPORT of kenzy.node.client,
    but that module imports sounddevice at module scope and openwakeword lazily
    inside the call. On a host with sounddevice and no openwakeword — which the
    `speaker` extra produces — the import succeeded and the call raised.
    """
    monkeypatch.setattr(setup, "probe_import", lambda name: False)
    called = []
    with caplog.at_level(logging.INFO):
        setup._setup_openwakeword()  # must not raise
    assert not called
    assert any("skipping" in r.message.lower() for r in caplog.records)


def test_a_failed_openwakeword_download_warns_instead_of_aborting(monkeypatch, caplog):
    """The steps after this one fetch different models for different services."""
    monkeypatch.setattr(setup, "probe_import", lambda name: True)

    def _boom() -> None:
        raise RuntimeError("network down")

    import kenzy.node.client as client

    monkeypatch.setattr(client, "_ensure_oww_resources", _boom)
    with caplog.at_level(logging.WARNING):
        setup._setup_openwakeword()  # must not raise
    assert any("failed" in r.message.lower() for r in caplog.records)


def test_a_failed_speechbrain_download_warns_instead_of_aborting(monkeypatch, caplog, tmp_path):
    """Same shape, same reasoning — kokoro already did this correctly."""
    pytest_speechbrain = __import__("importlib").util.find_spec("speechbrain")
    if pytest_speechbrain is None:
        import pytest

        pytest.skip("speechbrain not installed")

    from speechbrain.pretrained import EncoderClassifier

    def _boom(**kw):  # noqa: ANN001, ANN003
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(EncoderClassifier, "from_hparams", staticmethod(_boom))
    with caplog.at_level(logging.WARNING):
        setup._setup_speechbrain("some/model", str(tmp_path / "nope"))  # must not raise
    assert any("failed" in r.message.lower() for r in caplog.records)


def test_the_probe_targets_the_real_dependency():
    """Guard against reverting to "does kenzy.node.client import?", which is a
    proxy for sounddevice, not for openwakeword."""
    import inspect

    src = inspect.getsource(setup._setup_openwakeword)
    assert 'probe_import("openwakeword")' in src


# --- device resolution -------------------------------------------------------


def test_setup_resolves_auto_exactly_like_the_tts_service():
    """"auto" is the packaged default for kokoro.device AND a string Kokoro
    can't consume. The pre-warm script passed it through raw, so the one code
    path whose whole job is downloading and warming the model was the path
    getting it wrong — on a default install."""
    import inspect

    from kenzy.torchdevice import resolve_device

    src = inspect.getsource(setup.main)
    assert 'kcfg.get("device", "auto")' in src, "default must match the service, not 'cpu'"
    assert "resolve_device(" in src, "'auto' must be resolved before reaching Kokoro"

    # And the service uses the same implementation, so they cannot drift apart.
    from kenzy.tts import tts

    assert inspect.getsource(tts._resolve_device).count("resolve_device") >= 1
    assert resolve_device("cuda") == "cuda"  # explicit values pass through untouched
    assert resolve_device("cpu") == "cpu"


def test_resolve_device_picks_a_real_device_for_auto():
    from kenzy.torchdevice import resolve_device

    assert resolve_device("auto") in ("cuda", "mps", "cpu")


# --- service environment -----------------------------------------------------


def test_every_service_loads_dotenv():
    """kenzy-speaker didn't, so it never saw KENZY_SERVICE_TOKEN from .env and
    authenticated as though no token existed — invisible until one is set, at
    which point exactly one service starts failing to register."""
    import pathlib

    root = pathlib.Path(setup.__file__).parent
    missing = [
        name
        for name, rel in (
            ("server", "server/server.py"),
            ("stt", "stt/stt.py"),
            ("tts", "tts/tts.py"),
            ("llm", "llm/llm.py"),
            ("speaker", "speaker/speaker.py"),
        )
        if "load_dotenv()" not in (root / rel).read_text()
    ]
    assert not missing, f"services that never load .env: {missing}"


# --- wake-word model format --------------------------------------------------


def test_the_bundled_model_is_chosen_by_runtime_not_extension(monkeypatch, tmp_path):
    """tflite-runtime has no wheel past cp311 and none current for macOS, while
    onnxruntime is one of openwakeword's unconditional deps. Picking the model
    by what the host can RUN is what lets a Mac work without a hand-converted
    model — the workaround this replaces.
    """
    from kenzy.node import client

    models = tmp_path / "models"
    models.mkdir()
    (models / "hey_ken_zee.tflite").write_bytes(b"t")
    (models / "hey_ken_zee.onnx").write_bytes(b"o")
    monkeypatch.setattr(client, "files", lambda pkg: tmp_path)

    monkeypatch.setattr(client, "probe_import", lambda name: True)  # tflite runtime present
    assert client._bundled_model_paths()[0].endswith(".tflite")
    assert client._infer_framework(client._bundled_model_paths()) == "tflite"

    monkeypatch.setattr(client, "probe_import", lambda name: False)  # e.g. macOS
    assert client._bundled_model_paths()[0].endswith(".onnx")
    assert client._infer_framework(client._bundled_model_paths()) == "onnx"


def test_without_an_onnx_copy_it_still_returns_the_tflite_model(monkeypatch, tmp_path):
    """The ONNX file is optional — absent, behaviour is exactly as before."""
    from kenzy.node import client

    models = tmp_path / "models"
    models.mkdir()
    (models / "hey_ken_zee.tflite").write_bytes(b"t")
    monkeypatch.setattr(client, "files", lambda pkg: tmp_path)
    monkeypatch.setattr(client, "probe_import", lambda name: False)
    assert client._bundled_model_paths()[0].endswith(".tflite")


# --- the wakeword extra split ------------------------------------------------


def test_node_extra_no_longer_drags_in_the_wake_engine():
    """openwakeword hard-requires tflite-runtime on Linux, which has no wheel
    past cp311 and no sdist — so while it lived in the `node` extra,
    `pip install kenzy[node]` simply could not succeed on 3.12+. pip re-resolves
    an installed package's requirements, so no install ORDER avoided it; the
    dependency had to stop being reachable from `node` at all.
    """
    import pathlib
    import tomllib

    root = pathlib.Path(setup.__file__).resolve().parents[2]
    extras = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    assert not any("openwakeword" in d for d in extras["node"])
    assert any("openwakeword" in d for d in extras["wakeword"])


def test_the_installers_nodeps_list_matches_openwakeword_upstream():
    """When the extra can't be used we install openwakeword --no-deps and supply
    its requirements ourselves, which means owning a copy of upstream's list.
    This fails if that list changes — the whole point of writing it down."""
    import importlib.metadata as md
    import pathlib
    import re

    from packaging.requirements import Requirement

    upstream = set()
    for raw in md.requires("openwakeword") or []:
        req = Requirement(raw)
        if req.marker and "extra" in str(req.marker):
            continue  # optional extras, not runtime requirements
        if req.name == "tflite-runtime":
            continue  # the one we deliberately omit
        upstream.add(req.name)

    root = pathlib.Path(setup.__file__).resolve().parents[3]
    sh = (root / "kenzy-www/src/install.sh").read_text()
    m = re.search(r'WAKEWORD_DEPS="([^"]+)"', sh)
    assert m, "WAKEWORD_DEPS not found in install.sh"
    ours = set(m.group(1).split())

    assert ours == upstream, (
        f"install.sh's --no-deps list has drifted from openwakeword's own "
        f"requirements. missing={sorted(upstream - ours)} extra={sorted(ours - upstream)}"
    )


def test_deploy_gives_node_hosts_the_wake_engine():
    """The extra split must not silently leave the fleet without a wake word."""
    import pathlib

    from kenzy.deploy.deploy import HostConfig, _pip_extras

    host = HostConfig(
        name="n", address="1.1.1.1", ssh_user="pi", install_path="/o",
        venv_path="/o/.v", python_bin="python3", services=["node"],
    )
    assert "wakeword" in _pip_extras(host, pathlib.Path(".")).split(",")
