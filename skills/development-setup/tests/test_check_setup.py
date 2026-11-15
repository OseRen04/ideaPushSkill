import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts/check_setup.py"
SPEC = importlib.util.spec_from_file_location("development_setup_check", PATH)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)

SECRET = "ghp_PRIVATE_VALUE_MUST_NOT_LEAVE_THE_PROCESS"


def responses(monkeypatch, identity="owner", repo=None, codex_text="Logged in using ChatGPT", fail=None):
    calls = []
    monkeypatch.setattr(CHECK.shutil, "which", lambda name: "/fake/" + name)
    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if fail and fail(arguments):
            raise subprocess.TimeoutExpired(arguments, 15, output=SECRET, stderr=SECRET)
        if arguments[0] == "/fake/codex":
            return subprocess.CompletedProcess(arguments, 0, "", codex_text)
        if arguments[1:4] == ["auth", "status", "--help"]:
            return subprocess.CompletedProcess(arguments, 0, "--active --no-refresh", "")
        if arguments[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(arguments, 0, "", "Token: " + SECRET)
        if arguments[-1] == "/user":
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"login": identity, "token": SECRET}), "")
        if "actions/secrets" in arguments[-1]:
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"total_count": 1,
                "secrets": [{"name": "CODEX_AUTH_JSON", "value": SECRET}]}), "")
        return subprocess.CompletedProcess(arguments, 0, json.dumps(repo or {
            "full_name": "owner/runner", "private": True,
            "permissions": {"pull": True, "push": True, "admin": True}, "token": SECRET}), "")
    monkeypatch.setattr(CHECK.subprocess, "run", run)
    return calls


def test_success_reports_only_whitelisted_metadata(monkeypatch):
    calls = responses(monkeypatch)
    result = CHECK.inspect_setup(expected_owner="owner", repository="owner/runner", check_secrets=True,
                                 environment={"GH_TOKEN": SECRET, "OPENAI_API_KEY": SECRET})
    output = json.dumps(result)
    assert SECRET not in output
    assert result["full_setup_complete"] is False
    assert result["github"]["active_login"] == "owner"
    assert result["github"]["owner_matches"] is True
    target = result["github"]["repository"]
    assert target["status"] == "available" and target["private"] is True
    assert target["permissions"]["admin"] is True
    assert target["workflow_write_verified"] is False
    assert target["secrets_write_verified"] is False
    assert target["secrets"]["names"] == ["CODEX_AUTH_JSON"]
    assert result["codex"]["method"] == "chatgpt"
    assert result["codex"]["remote_auth_verified"] is False
    assert all(call[1]["capture_output"] and call[1]["timeout"] == 15 for call in calls)
    assert all("login" not in call[0] or call[0][1:] == ["login", "status"] for call in calls)


def test_scope_and_account_mismatch_does_not_inspect_target(monkeypatch):
    calls = responses(monkeypatch, identity="other")
    result = CHECK.inspect_setup(expected_owner="owner", repository="owner/runner")
    assert result["github"]["status"] == "owner_mismatch"
    assert result["github"]["repository"]["status"] == "not_checked"
    assert not any("/repos/" in call[0][-1] for call in calls)
    assert result["codex"]["local_session_present"] is True


def test_secrets_are_not_inspected_without_explicit_flag(monkeypatch):
    calls = responses(monkeypatch)
    result = CHECK.inspect_setup(repository="owner/runner")
    assert result["github"]["repository"]["secrets"]["status"] == "not_checked"
    assert not any("actions/secrets" in call[0][-1] for call in calls)


def test_404_is_unavailable_not_nonexistent_and_stderr_is_private(monkeypatch):
    calls = responses(monkeypatch)
    base = CHECK.subprocess.run
    def run(arguments, **kwargs):
        if arguments[-1] == "/repos/owner/runner":
            return subprocess.CompletedProcess(arguments, 1, "", "HTTP 404 " + SECRET)
        return base(arguments, **kwargs)
    monkeypatch.setattr(CHECK.subprocess, "run", run)
    result = CHECK.inspect_setup(repository="owner/runner")
    assert result["github"]["repository"]["status"] == "unavailable"
    assert SECRET not in json.dumps(result)


def test_missing_tools_are_independent_and_do_not_start_subprocesses(monkeypatch):
    monkeypatch.setattr(CHECK.shutil, "which", lambda name: None)
    monkeypatch.setattr(CHECK.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected process"))
    result = CHECK.inspect_setup()
    assert result["github"]["status"] == result["codex"]["status"] == "tool_missing"


def test_timeout_does_not_stop_other_diagnostics_or_expose_output(monkeypatch):
    responses(monkeypatch, fail=lambda args: args[-1] == "/user")
    result = CHECK.inspect_setup()
    assert result["github"]["status"] == "unavailable"
    assert result["github"]["reason"] == "timeout"
    assert result["codex"]["status"] == "locally_logged_in"
    assert SECRET not in json.dumps(result)


def test_existing_environment_is_not_changed_and_codex_home_is_scoped(monkeypatch, tmp_path):
    calls = responses(monkeypatch)
    original = {"GH_TOKEN": SECRET, "OPENAI_API_KEY": SECRET, "CODEX_HOME": "/existing", "GH_DEBUG": "api"}
    result = CHECK.inspect_setup(codex_home=tmp_path / "ci-auth", environment=original)
    codex_env = next(call[1]["env"] for call in calls if call[0][0] == "/fake/codex")
    gh_env = next(call[1]["env"] for call in calls if call[0][0] == "/fake/gh")
    assert codex_env["CODEX_HOME"] == str(tmp_path / "ci-auth")
    assert gh_env["CODEX_HOME"] == "/existing"
    assert "GH_DEBUG" not in gh_env
    assert original["CODEX_HOME"] == "/existing" and original["GH_DEBUG"] == "api"
    assert result["codex"]["environment_credentials"]["OPENAI_API_KEY"] is True
    assert SECRET not in json.dumps(result)


def test_api_key_method_is_not_reported_as_chatgpt(monkeypatch):
    responses(monkeypatch, codex_text="Logged in using an API key - " + SECRET)
    result = CHECK.inspect_setup()
    assert result["codex"]["method"] == "api_key"
    assert SECRET not in json.dumps(result)


def test_unknown_method_is_not_guessed(monkeypatch):
    responses(monkeypatch, codex_text="Session available")
    result = CHECK.inspect_setup()
    assert result["codex"]["method"] == "unknown"
    assert result["codex"]["remote_auth_verified"] is False


def test_not_logged_in_is_different_from_unavailable(monkeypatch):
    responses(monkeypatch)
    base = CHECK.subprocess.run
    def run(arguments, **kwargs):
        if arguments[1:3] in (["auth", "status"], ["login", "status"]) and arguments[-1] != "--help":
            return subprocess.CompletedProcess(arguments, 1, "", "You are not logged in.")
        if arguments[-1] == "/user":
            return subprocess.CompletedProcess(arguments, 1, "", "HTTP 401 " + SECRET)
        return base(arguments, **kwargs)
    monkeypatch.setattr(CHECK.subprocess, "run", run)
    result = CHECK.inspect_setup()
    assert result["github"]["status"] == "not_logged_in"
    assert result["codex"]["status"] == "not_logged_in"
    assert result["codex"]["local_session_present"] is False


def test_no_refresh_is_used_only_when_supported(monkeypatch):
    calls = responses(monkeypatch)
    CHECK.inspect_setup()
    auth = next(call[0] for call in calls if call[0][1:3] == ["auth", "status"] and call[0][-1] != "--help")
    assert "--no-refresh" in auth and "--active" in auth
    base = CHECK.subprocess.run
    def legacy(arguments, **kwargs):
        if arguments[1:4] == ["auth", "status", "--help"]:
            return subprocess.CompletedProcess(arguments, 0, "--hostname", "")
        return base(arguments, **kwargs)
    monkeypatch.setattr(CHECK.subprocess, "run", legacy)
    calls.clear()
    report = CHECK.inspect_setup()
    auth = next(call[0] for call in calls if call[0][1:3] == ["auth", "status"])
    assert "--no-refresh" not in auth and "--active" not in auth
    assert report["github"]["no_refresh_supported"] is False


@pytest.mark.parametrize("kwargs", [
    {"host": "https://github.com"}, {"host": "--danger"},
    {"repository": "owner/repo/extra"}, {"repository": "owner/../../x"},
    {"repository": "owner/."}, {"repository": "owner/.."},
    {"expected_owner": "--account"}, {"check_secrets": True},
    {"gh": "relative/gh"}, {"codex": "relative/codex"},
])
def test_invalid_input_never_starts_a_cli(monkeypatch, kwargs):
    monkeypatch.setattr(CHECK.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected process"))
    with pytest.raises(CHECK.InvalidArguments):
        CHECK.inspect_setup(**kwargs)


def test_cli_error_does_not_echo_arbitrary_values(capsys):
    assert CHECK.main(["--token", SECRET]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["status"] == "invalid_arguments"
