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
    assert strata.variants["iq4"].command == "~/Strata/run-unsloth-ud-iq4_xs-v31.sh"
    assert strata.variants["iq4"].label is None
    assert strata.default_variant is None and strata.ready is None
    assert strata.stop == "~/Strata/stop.sh"
    assert strata.stop_before_run
    llama = linux.section("llama-server")
    assert list(llama.variants) == ["q8", "q4"]
    assert llama.default_variant == "q8"
    assert llama.variants["q8"].command.endswith("./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200")
    assert llama.variants["q4"].command.endswith(" -4bit")
    assert llama.stop.startswith("killall ") and llama.stop_before_run
    assert llama.ready.port == 8000 and llama.ready.process == "llama-server"
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


def test_variant_with_label():
    section = {"id": "s", "label": "S", "variants": {"a": "x", "b": {"command": "y", "label": "B"}}}
    variants = Config.model_validate(minimal(sections=[section])).tabs[0].sections[0].variants
    assert (variants["a"].command, variants["a"].label) == ("x", None)
    assert (variants["b"].command, variants["b"].label) == ("y", "B")


def test_default_variant_must_exist():
    section = {"id": "s", "label": "S", "variants": {"a": "x"}, "default_variant": "b"}
    with pytest.raises(ValidationError, match="default_variant"):
        Config.model_validate(minimal(sections=[section]))


@pytest.mark.parametrize(("url", "port"), [("http://h:8000/health", 8000), ("http://h/health", 80),
                                           ("https://h/", 443)])
def test_ready_port(url, port):
    section = {"id": "s", "label": "S", "ready": {"url": url, "process": "llama-server"}}
    assert Config.model_validate(minimal(sections=[section])).tabs[0].sections[0].ready.port == port


@pytest.mark.parametrize("ready", [{"url": "ftp://h/x", "process": "p"},
                                   {"url": "http:///x", "process": "p"},
                                   {"url": "http://h/", "process": "a b"},
                                   {"url": "http://h/", "process": "p;rm"},
                                   {"url": "http://h/", "process": "a" * 16}])
def test_bad_ready(ready):
    with pytest.raises(ValidationError):
        Config.model_validate(minimal(sections=[{"id": "s", "label": "S", "ready": ready}]))
