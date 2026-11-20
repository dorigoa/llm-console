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
    assert strata.variants["iq4"].label == "iq4 · UD-IQ4_XS"
    assert strata.default_variant is None
    assert strata.stop == "~/Strata/stop.sh"
    assert strata.stop_before_run
    assert strata.variants["q8"].model == "Qwen3.8-Flash-Next Q8_0"
    assert strata.description == "Model: Qwen3.8-Flash-Next"
    assert strata.ready.url == "http://192.168.1.200:8000/v1/models" and strata.ready.process is None
    llama = linux.section("llama-server")
    assert list(llama.variants) == ["qwen-q8", "qwen-q4", "gemma-q8", "gemma-q4"]
    assert llama.default_variant == "qwen-q8"
    assert llama.variants["qwen-q8"].command.endswith("./run-qwen3.8-27b.sh -port 8000 -host 192.168.1.200")
    assert llama.variants["qwen-q4"].command.endswith(" -4bit")
    assert llama.variants["gemma-q8"].command.endswith("./run-gemma-4-31b.sh -port 8000 -host 192.168.1.200")
    assert llama.variants["gemma-q4"].command.endswith(" -qat4bit")
    assert llama.variants["gemma-q4"].model == "Gemma-4-31B-it QAT UD-Q4_K_XL"
    assert llama.stop.startswith("killall ") and llama.stop_before_run
    assert llama.ready.port == 8000 and llama.ready.process == "llama-server"
    assert llama.running == "pgrep -x llama-server >/dev/null"
    assert llama.exclusive_with == ["strata"] and strata.exclusive_with == ["llama-server"]
    assert strata.running == ("out=$(~/.local/bin/strata launch --status 2>/dev/null) || exit 2; "
                              "printf '%s\\n' \"$out\" | grep -qE ': (starting|ready) at '")
    assert config.ssh.identity == os.path.expanduser("~/.ssh/id_ecdsa")
    assert config.ssh.known_hosts == "/run/host-ssh/known_hosts"


def test_repo_config_mac_tab():
    config = load_config(REPO_CONFIG)
    mac = config.tab("mac")
    assert config.hosts[mac.host].address == "192.168.1.101"
    turbo, mlx = mac.sections
    assert (turbo.id, mlx.id) == ("turbofieldfare", "mlx-lm")
    assert list(turbo.variants) == ["gemma-q8", "gemma-q4", "qwen36-q4"]
    assert turbo.default_variant == "gemma-q8"
    context = turbo.options["context"]
    assert list(context.choices) == ["256k", "128k", "64k"] and context.default == "256k"
    assert [c.value for c in context.choices.values()] == ["262144", "131072", "65536"]
    # The default Run: Gemma 8 bit with 256k of context.
    assert turbo.command("gemma-q8", turbo.choose({})) == (
        "~/TurboFF/run-turboff.sh -model gemma4-8bit -ctx 262144 -host 192.168.1.101 -port 8080")
    assert turbo.command("qwen36-q4", {"context": "64k"}).startswith(
        "~/TurboFF/run-turboff.sh -model qwen36 -ctx 65536 ")
    assert turbo.stop == "~/TurboFF/stop.sh" and turbo.stop_before_run
    assert list(mlx.variants) == ["qwen-q4"] and mlx.default_variant == "qwen-q4" and not mlx.options
    assert mlx.command("qwen-q4", {}) == (
        "~/MLX/run-mlx.sh -model mlx-community/Qwen3.8-27B-4bit -host 192.168.1.101 -port 8080")
    assert mlx.stop == "~/MLX/stop.sh" and mlx.stop_before_run
    assert turbo.exclusive_with == ["mlx-lm"] and mlx.exclusive_with == ["turbofieldfare"]
    for section in (turbo, mlx):
        # No ready.process on macOS (no ss): the scripts check the port and the process.
        assert section.ready.url == "http://192.168.1.101:8080/v1/models" and section.ready.process is None
        assert section.running.startswith("pgrep -f '[/]")


def with_options(command="run -ctx {context}", **option):
    context = {"legend": "Context", "choices": {"big": "262144", "small": {"value": 65536, "label": "64k"}},
               "default": "big"} | option
    return minimal(sections=[{"id": "s", "label": "S", "variants": {"a": command}, "options": {"context": context}}])


def test_options():
    section = Config.model_validate(with_options()).tabs[0].sections[0]
    choices = section.options["context"].choices
    assert (choices["big"].value, choices["big"].label) == ("262144", None)
    assert (choices["small"].value, choices["small"].label) == ("65536", "64k")
    assert section.choose({}) == {"context": "big"}
    assert section.choose({"context": "small"}) == {"context": "small"}
    assert section.command("a", {"context": "small"}) == "run -ctx 65536"


@pytest.mark.parametrize(("requested", "match"), [
    ({"context": "huge"}, "Unknown Context choice: huge"),
    ({"ctx": "big"}, "Unknown option: ctx"),
    ({"context": "big; rm -rf ~"}, "Unknown Context choice"),
])
def test_choose_refuses(requested, match):
    section = Config.model_validate(with_options()).tabs[0].sections[0]
    with pytest.raises(ValueError, match=match):
        section.choose(requested)


def test_option_without_default_must_be_chosen():
    section = Config.model_validate(with_options(default=None)).tabs[0].sections[0]
    with pytest.raises(ValueError, match="Choose a value for Context"):
        section.choose({})
    assert section.choose({"context": "small"}) == {"context": "small"}


def test_shell_braces_are_not_placeholders():
    command = 'run -ctx {context} -home ${HOME} {a,b} { x; }; for i in {1..3}; do :; done'
    section = Config.model_validate(with_options(command)).tabs[0].sections[0]
    assert section.command("a", {"context": "big"}) == command.replace("{context}", "262144")


@pytest.mark.parametrize(("command", "option", "match"), [
    ("run -ctx {ctx}", {}, "no option ctx"),
    ("run", {}, r"does not use \{context\}"),
    ("run -ctx {context}", {"default": "medium"}, "default 'medium' is not a choice"),
    ("run -ctx {context}", {"choices": {}}, "has no choices"),
    ("run -ctx {context}", {"choices": {"Big": "1"}}, "choices"),
])
def test_bad_options(command, option, match):
    with pytest.raises(ValidationError, match=match):
        Config.model_validate(with_options(command, **option))


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


def exclusive(a_extra=None, b_extra=None):
    a = {"id": "a", "label": "A", "running": "check-a"} | (a_extra or {})
    b = {"id": "b", "label": "B", "running": "check-b"} | (b_extra or {})
    return minimal(sections=[a, b])


def test_exclusive_with():
    config = Config.model_validate(exclusive({"exclusive_with": ["b"]}, {"exclusive_with": ["a"]}))
    assert config.tabs[0].section("a").exclusive_with == ["b"]


@pytest.mark.parametrize(("a_extra", "b_extra", "match"), [
    ({"exclusive_with": ["c"]}, None, "not another section"),
    ({"exclusive_with": ["a"]}, None, "not another section"),
    ({"exclusive_with": ["b"]}, {"running": None}, "needs a running command"),
])
def test_bad_exclusive_with(a_extra, b_extra, match):
    with pytest.raises(ValidationError, match=match):
        Config.model_validate(exclusive(a_extra, b_extra))
