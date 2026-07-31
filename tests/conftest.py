"""Suite-wide fixtures.

**Every test gets its own config home.** The config home is the writable
operational tree — `data/`, `configs/`, the memory ledger, the node roster — and
its built-in default is the developer's real `~/.config/kenzy`. So any test that
exercises a persistent path writes into the actual household's data unless it
remembers to isolate itself, and "remembers to" is not a property a suite has.

This is not hypothetical: the node roster shipped writing on every register and
deregister, and promptly deposited three fake nodes (`kit`, `n1`, a stray uuid)
into the real config home the first time the dashboard tests ran.

A test that wants a *particular* home still calls `monkeypatch.setenv` itself and
wins — this only supplies the default that should have been there all along.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_kenzy_home(tmp_path_factory, monkeypatch):
    """Point `KENZY_HOME` at a per-test temp dir before anything resolves it."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path_factory.mktemp("kenzy-home")))
