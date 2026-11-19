import os

import pytest
from pydantic import ValidationError

from app.config import Config, load_config
from conftest import REPO_CONFIG


def minimal(**tab_overrides):
    tab = {"id": "linux", "label": "Linux", "host": "pc", "sections": []}
    tab.update(tab_overrides)
    return {"hosts": {"pc": {"address": "10.0.0.1", "user": "u"}}, "tabs": [tab]}


def test_repo_config_loads():
    config = load_config(REPO_CONFIG)
    linux = config.tab("linux")
    strata = linux.section("strata")
    assert list(strata.variants) == ["iq4", "q4", "q5", "q6", "q8"]
    assert strata.variants["iq4"] == "~/Strata/run-unsloth-ud-iq4_xs-v31.sh"
    assert strata.stop == "~/Strata/stop.sh"
    assert strata.stop_before_run
    llama = linux.section("llama-server")
    assert not llama.can_run and not llama.can_stop
    assert config.tab("mac").sections == []
    assert config.ssh.identity == os.path.expanduser("~/.ssh/id_ecdsa")
    assert config.ssh.known_hosts == "/run/host-ssh/known_hosts"


def test_env_var_selects_config(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text("hosts: {pc: {address: 1.2.3.4, user: x}}\ntabs: []\n")
    monkeypatch.setenv("LLM_CONSOLE_CONFIG", str(path))
    assert load_config().hosts["pc"].address == "1.2.3.4"


def test_unknown_host():
    with pytest.raises(ValidationError, match="unknown host"):
        Config.model_validate(minimal(host="mac"))


def test_duplicate_sections():
    section = {"id": "s", "label": "S"}
    with pytest.raises(ValidationError, match="duplicate section"):
        Config.model_validate(minimal(sections=[section, section]))


def test_stop_before_run_needs_stop():
    section = {"id": "s", "label": "S", "variants": {"a": "x"}, "stop_before_run": True}
    with pytest.raises(ValidationError, match="stop_before_run"):
        Config.model_validate(minimal(sections=[section]))


def test_null_variants_and_sections_are_empty():
    config = Config.model_validate(minimal(sections=[{"id": "s", "label": "S", "variants": None}]))
    assert config.tabs[0].sections[0].variants == {}
    assert Config.model_validate(minimal(sections=None)).tabs[0].sections == []


@pytest.mark.parametrize("bad_id", ["Q4", "q 4", "-q4", "q4;x", ""])
def test_variant_ids_are_restricted(bad_id):
    section = {"id": "s", "label": "S", "variants": {bad_id: "x"}}
    with pytest.raises(ValidationError):
        Config.model_validate(minimal(sections=[section]))


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(minimal(sections=[{"id": "s", "label": "S", "comand": "x"}]))
