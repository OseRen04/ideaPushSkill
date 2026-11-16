"""Explicit operational commands; ordinary diagnostics never invoke a model."""

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote

import yaml

from .auth import clean_child_env
from .errors import BlockedError, RunnerError
from .github import GitHub, validate_repository
from .schedule import Schedule, cron_entries
from .selector import parse_idea
from .state import ProjectStore


class _WorkflowLoader(yaml.SafeLoader):
    pass


# GitHub's `on` event key is a string, not YAML 1.1's boolean True.
_WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in entries if tag != "tag:yaml.org,2002:bool"]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    value = {}
    for key_node, item_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in value
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "Unhashable key.", key_node.start_mark)
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, "Duplicate key.", key_node.start_mark)
        value[key] = loader.construct_object(item_node, deep=deep)
    return value


_WorkflowLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load_yaml(text, code):
    try:
        value = yaml.load(text, Loader=_WorkflowLoader)
    except (yaml.YAMLError, ValueError, TypeError):
        raise BlockedError("Operational configuration is not valid unambiguous YAML.", code) from None
    if not isinstance(value, dict):
        raise BlockedError("Operational configuration must be a mapping.", code)
    return value


def _owned_private(github, repo, owner, details=False):
    validate_repository(repo)
    if repo.split("/", 1)[0].casefold() != owner.casefold():
        raise BlockedError("Execution repository belongs to a different owner.", "owner_mismatch")
    metadata = github.repository(repo)
    if (not isinstance(metadata, dict)
            or str(metadata.get("owner", {}).get("login", "")).casefold() != owner.casefold()
            or str(metadata.get("full_name", "")).casefold() != repo.casefold()):
        raise BlockedError("Execution repository ownership cannot be verified.", "owner_mismatch")
    if metadata.get("private") is not True:
        raise BlockedError("Managed Codex execution requires a private repository.", "runner_not_private")
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise BlockedError("Execution repository has no default branch.", "runner_branch_missing")
    return metadata if details else branch


def _schedule_workflow(text, entries):
    workflow = _load_yaml(text, "invalid_actions_workflow")
    events = workflow.get("on")
    if not isinstance(events, dict) or "workflow_dispatch" not in events:
        raise BlockedError("The execution workflow must preserve workflow_dispatch.", "invalid_actions_workflow")
    desired = copy.deepcopy(workflow)
    if entries:
        desired["on"]["schedule"] = entries
    else:
        desired["on"].pop("schedule", None)
    if workflow == desired:
        return text, False
    try:
        node = yaml.compose(text, Loader=_WorkflowLoader)
        pairs = node.value
        index = next(i for i, (key, _) in enumerate(pairs) if key.value == "on")
        key_node = pairs[index][0]
        if key_node.start_mark.column != 0:
            raise ValueError()
        start = key_node.start_mark.index
        end = pairs[index + 1][0].start_mark.index if index + 1 < len(pairs) else len(text)
        region = text[start:end]
        lines = region.splitlines(keepends=True)
        tail = ""
        # Keep blank lines and top-level comments belonging to the next section.
        while lines and (not lines[-1].strip() or lines[-1].startswith("#")):
            tail = lines.pop() + tail
        replacement = yaml.safe_dump({"on": desired["on"]}, sort_keys=False, allow_unicode=True)
        changed = text[:start] + replacement + tail + text[end:]
        # Reject aliases/anchors or YAML forms that would change other settings.
        if _load_yaml(changed, "invalid_actions_workflow") != desired:
            raise ValueError()
    except (yaml.YAMLError, ValueError, StopIteration, AttributeError, TypeError):
        raise BlockedError("Workflow event configuration cannot be updated safely.", "invalid_actions_workflow") from None
    return changed, True


def sync_schedule(config):
    github = GitHub(os.environ.get("RUNNER_GITHUB_TOKEN"))
    owner, runner = config["owner"], config["runner_repository"]
    source = validate_repository(config["ideas_repository"])
    if source.split("/", 1)[0].casefold() != owner.casefold():
        raise BlockedError("Ideas repository belongs to a different owner.", "owner_mismatch")
    branch = _owned_private(github, runner, owner)
    source_branch = github.default_branch(source)
    source_head = github.head(source, source_branch)
    raw_schedule, _ = github.read_file(source, config["schedule_path"], ref=source_head)
    schedule = Schedule.from_dict(_load_yaml(raw_schedule, "invalid_schedule"))
    entries = cron_entries(schedule)
    target_head = github.head(runner, branch)
    path = ".github/workflows/autonomous-development.yml"
    raw_workflow, _ = github.read_file(runner, path, ref=target_head)
    updated, changed = _schedule_workflow(raw_workflow, entries)
    commit = target_head
    if changed:
        commit = github.write_files(
            runner, {path: updated}, "Synchronize the owner's development schedule",
            branch=branch, expected_head=target_head,
        )
    return {
        "mode": "sync_schedule", "repository": runner,
        "source_repository": source, "source_commit": source_head,
        "commit": commit, "changed": changed,
        "enabled": schedule.enabled, "cron": entries,
    }


def _resume_issue(github, repository, number, marker):
    issue = github.api("/repos/{}/issues/{}".format(repository, number))
    expected_url = "https://github.com/{}/issues/{}".format(repository, number)
    if (not isinstance(issue, dict) or issue.get("number") != number or "pull_request" in issue
            or not isinstance(issue.get("body"), str) or marker not in issue["body"]
            or str(issue.get("html_url", "")).casefold() != expected_url.casefold()):
        raise BlockedError("The development issue identity cannot be verified.", "issue_binding")
    if issue.get("state") != "open":
        raise BlockedError("A closed development issue requires an explicit reopening decision.", "issue_closed")
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise BlockedError("The development issue labels cannot be verified.", "issue_binding")
    names = []
    for item in labels:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            raise BlockedError("The development issue labels cannot be verified.", "issue_binding")
        names.append(name)
    return names


def resume_project(config, idea_id):
    """Resume metadata only after an explicit user request, without changing permission.

    The two repositories cannot share a transaction. A blocked resume intent is
    committed before label edits, so every interrupted stage can be retried and
    does not dispatch work before its execution state is confirmed.
    """
    if not isinstance(idea_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", idea_id):
        raise BlockedError("A valid kebab-case idea identifier is required.", "invalid_idea_id")
    github = GitHub(os.environ.get("RUNNER_GITHUB_TOKEN"))
    owner = config["owner"]
    source, runner = validate_repository(config["ideas_repository"]), validate_repository(config["runner_repository"])
    if source.split("/", 1)[0].casefold() != owner.casefold():
        raise BlockedError("Ideas repository belongs to a different owner.", "owner_mismatch")
    _owned_private(github, runner, owner)
    profile = github.api("/user")
    if not isinstance(profile, dict) or str(profile.get("login", "")).casefold() != owner.casefold():
        raise BlockedError("The management credential belongs to a different owner.", "owner_mismatch")
    source_branch = github.default_branch(source)
    source_head = github.head(source, source_branch)
    content, _ = github.read_file(source, idea_id + "/idea.md", ref=source_head)
    idea = parse_idea(idea_id, content)
    if idea.development != "allowed" or idea.status not in {"構想", "開発中"}:
        raise BlockedError("The idea must retain explicit active development permission.", "development_denied")
    store = ProjectStore(github, runner, config.get("state_path", "state/projects.json"))
    state = copy.deepcopy(store.load())
    project = state["projects"].get(idea_id)
    if not isinstance(project, dict) or project.get("status") not in {"blocked", "failed", "paused", "review"}:
        raise BlockedError("The execution state is not eligible for an explicit resume.", "resume_not_allowed")
    if project.get("idea_id") != idea_id or project.get("source_repository") != source:
        raise BlockedError("The execution state belongs to another idea source.", "repository_binding")
    repository = validate_repository(project.get("repository"))
    if repository.casefold() in {source.casefold(), runner.casefold()}:
        raise BlockedError("A management repository cannot be used as a development target.", "repository_binding")
    metadata = _owned_private(github, repository, owner, details=True)
    default_branch = metadata["default_branch"]
    if (type(project.get("repository_id")) is not int or project["repository_id"] <= 0
            or project["repository_id"] != metadata.get("id")
            or metadata.get("archived") is True or metadata.get("disabled") is True
            or project.get("branch") != "agent/" + idea_id or project["branch"] == default_branch):
        raise BlockedError("The development repository binding is invalid.", "repository_binding")
    marker_raw, _ = github.read_file(repository, ".idea-vault/project.json", ref=default_branch)
    try:
        binding = json.loads(marker_raw)
    except (ValueError, TypeError):
        raise BlockedError("The development repository marker is invalid.", "repository_binding") from None
    if binding != {"version": 1, "idea_id": idea_id, "source_repository": source}:
        raise BlockedError("The development repository is bound to another idea.", "repository_binding")
    number = project.get("issue_number")
    if type(number) is not int or number <= 0:
        raise BlockedError("The execution state has no valid development issue.", "issue_binding")
    marker = "<!-- idea-vault:{}:{} -->".format(source, idea_id)
    labels = _resume_issue(github, repository, number, marker)
    forbidden = {"agent:blocked", "agent:review"}
    normalized = {name.casefold() for name in labels}
    already_ready = (project["status"] == "paused" and project.get("attempts") == 0
                     and not project.get("reason") and not normalized.intersection(forbidden)
                     and "agent:ready" in normalized)
    if already_ready:
        return {"mode": "resume", "status": "paused", "idea_id": idea_id,
                "repository": repository, "issue_number": number, "changed": False}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    project.update(status="blocked", reason="resume_in_progress")
    project.setdefault("resume_requested_at", now)
    store.save(state)
    try:
        for label in labels:
            if label.casefold() in forbidden:
                try:
                    github.api("/repos/{}/issues/{}/labels/{}".format(repository, number, quote(label, safe="")), method="DELETE")
                except RunnerError as error:
                    if error.code != "not_found":
                        raise
        current_labels = _resume_issue(github, repository, number, marker)
        if "agent:ready" not in {label.casefold() for label in current_labels}:
            github.api("/repos/{}/issues/{}/labels".format(repository, number), method="POST", data={"labels": ["agent:ready"]})
        verified = {label.casefold() for label in _resume_issue(github, repository, number, marker)}
        if verified.intersection(forbidden) or "agent:ready" not in verified:
            raise BlockedError("The development issue was not confirmed ready.", "resume_incomplete")
        project.update(status="paused", attempts=0, reason=None, updated_at=now, resume_completed_at=now)
        commit = store.save(state)
    except Exception:
        # No rollback: the stored blocked intent and individually idempotent label
        # operations allow a retry to converge after a timeout or partial write.
        raise BlockedError("Resume is incomplete; verify the execution state and retry.", "resume_incomplete") from None
    return {"mode": "resume", "status": "paused", "idea_id": idea_id,
            "repository": repository, "issue_number": number, "changed": True, "commit": commit}


def _stop_process(process, deadline):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=min(2.0, max(0.01, deadline - time.monotonic())))
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=0.1)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _auth_identity(raw):
    try:
        value = json.loads(raw)
        selected = {"tokens": value.get("tokens"), "last_refresh": value.get("last_refresh")}
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, AttributeError):
        raise BlockedError("Cannot verify managed authentication after the check.", "codex_auth_invalid") from None


def doctor(controller, auth_check=False):
    config = controller.config
    deadline = time.monotonic() + 5 * 60
    # This is deliberately the first check. Diagnostic model calls cannot bypass
    # the same account-wide spending gate as development runs.
    billing = controller.billing.check(dt.datetime.now(dt.timezone.utc), required_minutes=5)
    if not isinstance(billing, dict) or billing.get("allowed") is not True:
        raise BlockedError("Billing approval could not be verified.", "billing_unverified")
    owner, runner = config["owner"], config["runner_repository"]
    executable_names = {
        "gh": "gh", "git": "git", "codex": shlex.split(config.get("codex_command", "codex app-server"))[0],
        "symphony": config.get("symphony_command", ["symphony"])[0],
    }
    executables = {}
    for name, command in executable_names.items():
        resolved = shutil.which(command)
        if resolved is None:
            raise BlockedError("Required executable is not installed: " + name, "executable_missing")
        executables[name] = resolved
    profile = controller.github.api("/user")
    if not isinstance(profile, dict) or str(profile.get("login", "")).casefold() != owner.casefold():
        raise BlockedError("The management credential belongs to a different owner.", "owner_mismatch")
    _owned_private(controller.github, runner, owner)
    result = {
        "mode": "auth_check" if auth_check else "doctor",
        "status": "configuration_checked", "owner": owner,
        "runner_repository": runner, "executables": executables, "billing": billing,
        "auth_verified": False, "refresh_writeback_verified": False,
    }
    if not auth_check:
        return result
    raw = os.environ.get("CODEX_AUTH_JSON", "")
    process, restored, writeback, refreshed = None, False, False, False
    try:
        controller.auth.restore(raw)
        restored = True
        original_identity = _auth_identity(raw)
        env = clean_child_env(os.environ)
        env["CODEX_HOME"] = str(controller.auth.codex_home)
        seconds = min(60.0, max(0.0, deadline - time.monotonic() - 20.0))
        if seconds <= 0:
            raise BlockedError("No verified time remains for an authentication check.", "auth_check_timeout")
        with tempfile.TemporaryDirectory(prefix="auth-check-", dir=str(Path(controller.auth.codex_home).parent)) as work:
            try:
                process = subprocess.Popen(
                    [executables["codex"], "exec", "--sandbox", "read-only",
                     "--skip-git-repo-check", "Reply with the single word OK."],
                    env=env, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                returncode = process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                raise BlockedError("The authentication check exceeded its time limit.", "auth_check_timeout") from None
            except OSError:
                raise BlockedError("The authentication check could not start.", "auth_check_failed") from None
            finally:
                _stop_process(process, deadline)
            if returncode != 0:
                raise BlockedError("Codex authentication could not be verified.", "auth_check_failed")
        try:
            current = (Path(controller.auth.codex_home) / "auth.json").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise BlockedError("Updated authentication is unavailable.", "codex_auth_read_failed") from None
        refreshed = _auth_identity(current) != original_identity
        result["auth_verified"] = True
    finally:
        try:
            if restored:
                controller.github.timeout = max(0.1, min(20.0, deadline - time.monotonic()))
                writeback = controller.auth.persist()
        finally:
            controller.auth.cleanup()
    result["refresh_writeback_verified"] = bool(refreshed and writeback)
    result["status"] = "auth_verified"
    return result
