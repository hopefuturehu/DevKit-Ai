import os
from pathlib import Path

import pytest
import tomli_w
from pydantic import ValidationError

from bot.config import ConfigError, load_config
from bot.config.models import AppConfig
from bot.execution.factory import create_execution_target
from bot.execution.local import LocalExecutionTarget
from bot.sandbox.config import SandboxConfig, SandboxNetworkConfig, canonical_domain
from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.policy import validate_security_context


@pytest.fixture
def sources(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("BOT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    user = home / ".bot/config.toml"
    project = workspace / ".bot/config.toml"
    user.parent.mkdir(parents=True)
    project.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    user.write_text('[sandbox]\nmode = "required"\n')
    return workspace, user, project


@pytest.mark.parametrize(
    "project_data",
    [
        {"sandbox": {"mode": "off"}},
        {"sandbox": {"read_roots": ["/"]}},
        {"sandbox": {"write_roots": ["/"]}},
        {"sandbox": {"network": {"mode": "allowlist", "allowed_domains": ["example.com"]}}},
        {"permissions": {"mode": "full-access"}},
        {"permissions": {"auto_approve": True}},
        {"permissions": {"workspace_only": False}},
        {"permissions": {"network": "allow"}},
    ],
)
def test_project_cannot_widen_user_security_ceiling(sources, project_data):
    workspace, _, project = sources
    project.write_text(tomli_w.dumps(project_data))
    with pytest.raises(ConfigError, match="不能"):
        load_config(workspace)


def test_project_can_narrow_paths_domains_ports_and_permissions(sources, tmp_path):
    workspace, user, project = sources
    shared = tmp_path / "shared"
    user.write_text(
        tomli_w.dumps(
            {
                "sandbox": {
                    "mode": "required",
                    "read_roots": [str(shared)],
                    "write_roots": [str(shared)],
                    "network": {
                        "mode": "allowlist",
                        "allowed_domains": ["example.com", "pypi.org"],
                    },
                },
            }
        )
    )
    project.write_text(
        tomli_w.dumps(
            {
                "sandbox": {
                    "read_roots": [str(shared / "sub")],
                    "write_roots": [],
                    "network": {"allowed_domains": ["EXAMPLE.COM.:443"]},
                },
                "permissions": {"mode": "read-only", "network": "deny"},
            }
        )
    )
    config = load_config(workspace)
    assert config.sandbox.read_roots == ((shared / "sub").resolve(),)
    assert config.sandbox.network.allowed_domains == ("example.com:443",)
    assert config._sandbox_trust.sandbox.network.allowed_domains == ("example.com", "pypi.org")
    assert config._sandbox_trust.trusted_config == user.resolve()
    assert config._sandbox_trust.project_config == project.resolve()


@pytest.mark.parametrize("domain", ["example.com", "example.com:80", "other.example.com:443"])
def test_project_cannot_remove_port_restriction_or_expand_subdomains(sources, domain):
    workspace, user, project = sources
    user.write_text(
        tomli_w.dumps(
            {
                "sandbox": {
                    "mode": "required",
                    "network": {"mode": "allowlist", "allowed_domains": ["example.com:443"]},
                }
            }
        )
    )
    project.write_text(tomli_w.dumps({"sandbox": {"network": {"allowed_domains": [domain]}}}))
    with pytest.raises(ConfigError, match="不能扩大"):
        load_config(workspace)


def test_project_may_enable_required_but_not_add_authority(sources):
    workspace, user, project = sources
    user.write_text("")
    project.write_text('[sandbox]\nmode = "required"\n')
    config = load_config(workspace)
    assert config.sandbox.mode == "required"
    child = config.model_copy(update={"sandbox": SandboxConfig(mode="off")})
    with pytest.raises(SandboxError, match="不能关闭"):
        create_execution_target(child)
    project.write_text('[sandbox]\nmode = "required"\nread_roots = ["/"]\n')
    with pytest.raises(ConfigError, match="不能扩大"):
        load_config(workspace)


def test_explicit_config_and_host_overrides_are_trusted(sources, monkeypatch):
    workspace, _, project = sources
    project.write_text('[sandbox]\nmode = "off"\n[permissions]\nmode = "full-access"\n')
    explicit = load_config(workspace, config_path=project)
    assert explicit.sandbox.mode == "off"
    assert explicit._sandbox_trust.project_config is None
    monkeypatch.setenv("BOT_AUTO_APPROVE", "true")
    config = load_config(workspace, overrides={"sandbox": {"mode": "off"}})
    assert config.permissions.auto_approve
    assert "environment:permissions.auto_approve" in config._sandbox_trust.trusted_overrides
    assert "override:sandbox.mode" in config._sandbox_trust.trusted_overrides


def test_required_with_auto_approve_still_cannot_bypass_backend(sources, monkeypatch):
    workspace, _, _ = sources
    monkeypatch.setenv("BOT_AUTO_APPROVE", "true")
    config = load_config(workspace)
    with pytest.raises(SandboxError) as error:
        create_execution_target(config)
    assert error.value.code == SandboxErrorCode.UNAVAILABLE
    assert error.value.executed is False


@pytest.mark.parametrize("field,value", [("mode", "off"), ("write_roots", (Path("/"),))])
def test_derived_config_cannot_discard_the_ceiling(sources, field, value):
    workspace, _, _ = sources
    config = load_config(workspace)
    child = config.model_copy(
        update={
            "sandbox": config.sandbox.model_copy(update={field: value}),
        }
    )
    with pytest.raises(SandboxError, match="不能"):
        create_execution_target(child)


@pytest.mark.parametrize("update", [{"mode": "full-access"}, {"auto_approve": "false"}])
def test_unvalidated_permission_copies_are_rechecked(sources, update):
    workspace, _, _ = sources
    config = load_config(workspace)
    child = config.model_copy(update={"permissions": config.permissions.model_copy(update=update)})
    with pytest.raises(SandboxError) as error:
        validate_security_context(child)
    assert error.value.code == SandboxErrorCode.CONFIG_INVALID


def test_off_keeps_legacy_project_permissions(sources):
    workspace, user, project = sources
    user.write_text("")
    project.write_text('[permissions]\nmode = "full-access"\nauto_approve = true\n')
    target = create_execution_target(load_config(workspace))
    assert isinstance(target, LocalExecutionTarget)
    assert target.strict_process_scope is False


def test_required_never_constructs_local_fallback(sources, monkeypatch):
    import bot.execution.factory as factory

    workspace, _, _ = sources

    def unexpected(**kwargs):
        pytest.fail("required must not construct a Local fallback")

    monkeypatch.setattr(factory, "LocalExecutionTarget", unexpected)
    with pytest.raises(SandboxError, match="SANDBOX_UNAVAILABLE"):
        factory.create_execution_target(load_config(workspace))


def test_trust_context_cannot_be_loaded_from_project_data(sources):
    workspace, _, project = sources
    project.write_text('[_sandbox_trust]\ntrusted_config = "/arbitrary"\n')
    with pytest.raises(ConfigError, match="extra_forbidden"):
        load_config(workspace)
    assert "_sandbox_trust" not in AppConfig().model_dump()


def test_missing_explicit_or_dangling_configuration_fails_closed(sources):
    workspace, user, _ = sources
    with pytest.raises(ConfigError, match="无法读取配置"):
        load_config(workspace, config_path=workspace / "missing.toml")
    user.unlink()
    user.symlink_to(user.parent / "missing.toml")
    with pytest.raises(ConfigError, match="无法读取配置"):
        load_config(workspace)


def test_trust_retains_config_alias_and_target(sources):
    workspace, user, project = sources
    project.write_text('[permissions]\nmode = "read-only"\n')
    user.unlink()
    user.symlink_to(project)
    config = load_config(workspace)
    assert user in config._sandbox_trust.source_paths
    assert project in config._sandbox_trust.source_paths


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com",
        "*.example.com",
        "localhost",
        "x.localhost",
        "127.0.0.1",
        "127.1",
        "0x7f.1",
        "[::1]",
        "example.com:0",
        "example.com:65536",
        "example.com..",
        "example.com/path",
        "example.com:443:80",
        " example.com",
    ],
)
def test_domain_rules_reject_ambiguous_or_broad_destinations(value):
    with pytest.raises(ValueError):
        canonical_domain(value)


def test_network_mode_and_immutable_paths():
    with pytest.raises(ValidationError):
        SandboxNetworkConfig(mode="allowlist")
    with pytest.raises(ValidationError):
        SandboxNetworkConfig(allowed_domains=("example.com",))
    config = SandboxConfig(read_roots=["/tmp"])
    assert isinstance(config.read_roots, tuple)
    with pytest.raises(ValidationError):
        config.mode = "required"
    with pytest.raises(ValidationError):
        config.network.mode = "allowlist"
    for root in ("relative", "/tmp/*", "/tmp/\ninvalid"):
        with pytest.raises(ValidationError):
            SandboxConfig(read_roots=[root])
