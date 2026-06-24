"""F-12 (constraints): the operator pip-pin file — config helpers that locate it and
turn it into pip args, and kenzy-init scaffolding it (template or seeded)."""

from __future__ import annotations

from kenzy.config import constraints_path, pip_constraint_args
from kenzy.init import scaffold


def test_constraints_path_uses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    assert constraints_path() == tmp_path / "constraints.txt"


def test_pip_args_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    assert pip_constraint_args() == []


def test_pip_args_present_when_file_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "constraints.txt").write_text("transformers==4.30.0\n")
    assert pip_constraint_args() == ["-c", str(tmp_path / "constraints.txt")]


def test_scaffold_writes_template(tmp_path):
    scaffold(tmp_path, profile="node")
    text = (tmp_path / "constraints.txt").read_text()
    assert "constraints" in text.lower()
    assert "transformers==" in text  # example line in the template


def test_scaffold_seeds_from_file(tmp_path):
    src = tmp_path / "mypins.txt"
    src.write_text("transformers==4.30.0\nnumpy<2.0\n")
    home = tmp_path / "home"
    scaffold(home, profile="node", constraints=str(src))
    assert (home / "constraints.txt").read_text() == "transformers==4.30.0\nnumpy<2.0\n"


def test_scaffold_does_not_clobber_existing(tmp_path):
    scaffold(tmp_path, profile="node")
    (tmp_path / "constraints.txt").write_text("pinned==1.0\n")
    scaffold(tmp_path, profile="node")  # re-run without --force
    assert (tmp_path / "constraints.txt").read_text() == "pinned==1.0\n"
