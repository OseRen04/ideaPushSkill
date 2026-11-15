#!/usr/bin/env python3
"""Inspect CLI login metadata without starting login or printing credentials.

Repository permissions are hints, not proof of Workflows/Secrets write access.
Codex login status describes a local session, not a successful remote model run.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]+)\Z")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TIMEOUT = 15


class InvalidArguments(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo arbitrary command-line input, which could contain a secret.
        raise InvalidArguments()


def _command(name, override):
    if override is not None:
        if not isinstance(override, str) or not Path(override).is_absolute():
            raise InvalidArguments()
        return override if Path(override).is_file() and os.access(override, os.X_OK) else None
    return shutil.which(name)


def _capture(arguments, environment):
    try:
        result = subprocess.run(arguments, env=environment, capture_output=True,
                                text=True, timeout=_TIMEOUT, check=False)
        return {"returncode": result.returncode, "out": result.stdout or "",
                "error": result.stderr or "", "reason": "cli_failed" if result.returncode else None}
    except subprocess.TimeoutExpired:
        return {"returncode": None, "out": "", "error": "", "reason": "timeout"}
    except OSError:
        return {"returncode": None, "out": "", "error": "", "reason": "cli_unavailable"}


def _json(result):
    if result["returncode"] != 0:
        return None
    try:
        return json.loads(result["out"])
    except (ValueError, TypeError):
        return None


def _not_logged_in(result):
    text = (result["out"] + "\n" + result["error"]).casefold()
    return any(phrase in text for phrase in ("not logged into", "not logged in", "not authenticated"))


def _github(binary, host, owner, repository, check_secrets, environment):
    report = {
        "tool_available": binary is not None,
        "status": "tool_missing" if binary is None else "unavailable",
        "host": host,
        "active_login": None,
        "owner_matches": None,
        "auth_status": "not_checked",
        "no_refresh_supported": None,
        "environment_credentials": {key: bool(environment.get(key)) for key in (
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")},
        "repository": {
            "requested": repository, "status": "not_checked", "private": None,
            "permissions": {"pull": None, "push": None, "admin": None},
            "workflow_write_verified": False, "secrets_write_verified": False,
            "secrets": {"status": "not_checked", "names": []},
        },
    }
    if binary is None:
        return report
    # Newer gh versions refresh near-expiry tokens in auth status by default.
    # Prefer its no-refresh switch when the installed version exposes it.
    help_result = _capture([binary, "auth", "status", "--help"], environment)
    help_text = help_result["out"] + help_result["error"]
    report["no_refresh_supported"] = "--no-refresh" in help_text
    arguments = [binary, "auth", "status", "--hostname", host]
    if "--active" in help_text:
        arguments.append("--active")
    if report["no_refresh_supported"]:
        arguments.append("--no-refresh")
    auth = _capture(arguments, environment)
    report["auth_status"] = ("authenticated" if auth["returncode"] == 0
                             else "not_logged_in" if _not_logged_in(auth) else "unavailable")
    identity = _capture([binary, "api", "--hostname", host, "--method", "GET", "/user"], environment)
    profile = _json(identity)
    login = profile.get("login") if isinstance(profile, dict) else None
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        report["status"] = "not_logged_in" if report["auth_status"] == "not_logged_in" else "unavailable"
        report["reason"] = identity["reason"] or "identity_unavailable"
        return report
    report["active_login"] = login
    report["owner_matches"] = login.casefold() == owner.casefold() if owner else None
    report["status"] = "owner_mismatch" if report["owner_matches"] is False else "available"
    if not repository or report["status"] == "owner_mismatch":
        return report
    endpoint = "/repos/" + repository
    requested = _capture([binary, "api", "--hostname", host, "--method", "GET", endpoint], environment)
    metadata = _json(requested)
    target = report["repository"]
    # A 404 may mean either absent or inaccessible. Do not claim absence.
    if (not isinstance(metadata, dict)
            or not isinstance(metadata.get("full_name"), str)
            or metadata["full_name"].casefold() != repository.casefold()):
        target["status"] = "unavailable"
        return report
    target["status"] = "available"
    target["private"] = metadata.get("private") if type(metadata.get("private")) is bool else None
    permissions = metadata.get("permissions", {})
    if isinstance(permissions, dict):
        for key in target["permissions"]:
            value = permissions.get(key)
            target["permissions"][key] = value if type(value) is bool else None
    if check_secrets:
        result = _capture([binary, "api", "--hostname", host, "--method", "GET",
                           endpoint + "/actions/secrets?per_page=100"], environment)
        values = _json(result)
        secrets = target["secrets"]
        if isinstance(values, dict) and isinstance(values.get("secrets"), list):
            names = [item.get("name") for item in values["secrets"] if isinstance(item, dict)]
            if all(isinstance(name, str) and _SECRET_NAME.fullmatch(name) for name in names):
                secrets["status"] = "available"
                secrets["names"] = sorted(set(names))
                secrets["listing_complete"] = values.get("total_count") == len(names)
            else:
                secrets["status"] = "unavailable"
        else:
            secrets["status"] = "unavailable"
    return report


def _codex(binary, home, environment):
    report = {
        "tool_available": binary is not None,
        "status": "tool_missing" if binary is None else "unavailable",
        "local_session_present": None, "method": "unknown", "remote_auth_verified": False,
        "home_override_requested": home is not None,
        "environment_credentials": {key: bool(environment.get(key)) for key in (
            "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")},
    }
    if binary is None:
        return report
    child_env = dict(environment)
    if home is not None:
        child_env["CODEX_HOME"] = str(home)
    result = _capture([binary, "login", "status"], child_env)
    if result["returncode"] == 0:
        report["status"] = "locally_logged_in"
        report["local_session_present"] = True
        text = (result["out"] + "\n" + result["error"]).casefold()
        if "chatgpt" in text:
            report["method"] = "chatgpt"
        elif "api key" in text or "api_key" in text:
            report["method"] = "api_key"
    elif _not_logged_in(result):
        report["status"] = "not_logged_in"
        report["local_session_present"] = False
    else:
        report["reason"] = result["reason"] or "login_status_unavailable"
    return report


def inspect_setup(gh=None, codex=None, host="github.com", expected_owner=None,
                  repository=None, codex_home=None, check_secrets=False, environment=None):
    if (not isinstance(host, str) or not _HOST.fullmatch(host)
            or expected_owner is not None and not _LOGIN.fullmatch(expected_owner)
            or repository is not None and not _REPOSITORY.fullmatch(repository)
            or repository is not None and repository.split("/")[-1] in {".", ".."}
            or check_secrets and repository is None):
        raise InvalidArguments()
    env = dict(os.environ if environment is None else environment)
    # Capture output even when a caller enabled verbose CLI request logging.
    env.pop("GH_DEBUG", None)
    gh_binary, codex_binary = _command("gh", gh), _command("codex", codex)
    return {
        "schema_version": 1,
        "full_setup_complete": False,
        "github": _github(gh_binary, host, expected_owner, repository, check_secrets, env),
        "codex": _codex(codex_binary, codex_home, env),
    }


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--gh")
    parser.add_argument("--codex")
    parser.add_argument("--host", default="github.com")
    parser.add_argument("--expected-owner")
    parser.add_argument("--repository")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--check-secrets", action="store_true")
    try:
        args = parser.parse_args(argv)
        report = inspect_setup(**vars(args))
    except InvalidArguments:
        print(json.dumps({"schema_version": 1, "full_setup_complete": False,
                          "status": "invalid_arguments"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
