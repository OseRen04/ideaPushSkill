"""Host-owned checkpoints on a verified development branch, without hooks."""

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .errors import BlockedError, RunnerError
from .github import validate_repository


_SECRET = re.compile(
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    rb"|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    rb'|"(?:access_token|refresh_token|id_token)"\s*:\s*"(?!example"|dummy"|test"|placeholder"|YOUR_)[^"\s]{12,}")'
)
_CREDENTIAL_NAMES = {"auth.json", "credentials.json", ".netrc", ".npmrc", "id_rsa", "id_ed25519"}


def _remaining(deadline):
    if deadline is None:
        return 30
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise BlockedError("Checkpoint deadline reached.", "deadline")
    return min(30, seconds)


def _git(workspace, arguments, github, deadline=None, input=None, allow_nonzero=False):
    env = os.environ.copy()
    for name in list(env):
        if (name in {"RUNNER_GITHUB_TOKEN", "DEVELOPMENT_GITHUB_TOKEN", "CODEX_AUTH_JSON",
                     "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN",
                     "GITHUB_ENTERPRISE_TOKEN", "GH_DEBUG", "GIT_CURL_VERBOSE"}
                or name.startswith("GIT_")):
            env.pop(name, None)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GH_PROMPT_DISABLED": "1", "GH_HOST": "github.com"})
    token = getattr(github, "token", None)
    # Repository-controlled filters run during staging. Only network commands
    # receive the narrowly scoped development token.
    if token and arguments and arguments[0] in {"push", "ls-remote"}:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
               "-c", "commit.gpgsign=false", "-c", "push.gpgsign=false", "-c", "user.name=Idea Vault",
               "-c", "user.email=idea-vault[bot]@users.noreply.github.com"] + arguments
    process = None
    try:
        timeout = _remaining(deadline)
        process = subprocess.Popen(command, cwd=str(workspace), env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise RunnerError("Checkpoint Git operation timed out.", "checkpoint_timeout") from None
    except FileNotFoundError:
        raise BlockedError("Git is not installed.", "git_missing") from None
    except BaseException:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    if result.returncode and not allow_nonzero:
        raise RunnerError("Checkpoint Git operation failed; no force push was attempted.", "checkpoint_git")
    return result


def _validate_remote(url, repository):
    validate_repository(repository)
    if url.startswith("git@github.com:"):
        actual = url[len("git@github.com:"):]
    else:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "github.com"
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443} or parts.query or parts.fragment):
            raise BlockedError("Development remote must be a credential-free GitHub URL.", "remote_binding")
        actual = parts.path.lstrip("/")
    if actual.endswith(".git"):
        actual = actual[:-4]
    if actual.lower() != repository.lower():
        raise BlockedError("Development remote does not match the bound repository.", "remote_binding")


def _check_path(workspace, relative):
    path = Path(relative)
    if (path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\x00" in relative
            or "\\" in relative):
        raise BlockedError("Invalid checkpoint path.", "unsafe_checkpoint")
    name = path.name.lower()
    if (name in _CREDENTIAL_NAMES or name == ".env" or name.startswith(".env.")
            and name not in {".env.example", ".env.sample", ".env.template"}
            or name.endswith((".pem", ".p12", ".pfx", ".key"))):
        raise BlockedError("Credential-shaped file cannot be checkpointed.", "secret_in_checkpoint")
    full = workspace / path
    for item in [full] + list(full.parents):
        if item == workspace:
            break
        if item.is_symlink():
            raise BlockedError("Symlinks cannot be checkpointed.", "unsafe_checkpoint")
    if full.exists() and not full.is_file():
        raise BlockedError("Checkpoint candidates must be ordinary files.", "unsafe_checkpoint")


def _scan_content(value):
    if _SECRET.search(value):
        raise BlockedError("Credential-shaped content cannot be checkpointed.", "secret_in_checkpoint")


def _check_repository_config(workspace, github, deadline):
    # The controller owns the clone. Extensions, includes, external filters,
    # transport rewrites and per-URL HTTP settings are unnecessary for it.
    # Credential helpers are reset by _git; hook/fsmonitor/signing settings are
    # overridden there. Inspect keys only, never print configuration values.
    unsafe = (r"^(filter\.|include\.|includeif\.|url\.|http\.|protocol\.|extensions\."
              r"|remote\..*\.(pushurl|receivepack|uploadpack|vcs|proxy)$"
              r"|core\.(sshcommand|gitproxy|alternaterefscommand)$)")
    found = _git(workspace, ["config", "--local", "--name-only", "--get-regexp", unsafe],
                 github, deadline, allow_nonzero=True)
    if found.returncode not in {0, 1}:
        raise BlockedError("Cannot verify development repository configuration.", "unsafe_git_config")
    if found.stdout.strip():
        raise BlockedError("Development repository contains executable or redirecting Git settings.",
                           "unsafe_git_config")


def _check_workflow_changes(workspace, commit, github, deadline):
    changes = _git(workspace, ["log", "--format=", "--name-only", "--diff-filter=ACMRT", "-z",
                               commit, "--not", "--remotes=origin", "--", ".github/workflows"],
                   github, deadline).stdout
    if changes.strip(b"\n\0"):
        raise BlockedError("Development checkpoints cannot add or change Actions workflows.",
                           "development_workflow")


def checkpoint_workspace(workspace, project, github, note="", deadline=None):
    workspace = Path(workspace)
    if workspace.is_symlink():
        raise BlockedError("Checkpoint workspace must not be a symlink.", "unsafe_checkpoint")
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise BlockedError("Checkpoint workspace does not exist.", "workspace_missing")
    repository = validate_repository(project["repository"])
    branch = project["branch"]
    if (branch == project.get("default_branch") or not branch.startswith("agent/")
            or ".." in branch or any(character in branch for character in "\n\r\x00")):
        raise BlockedError("Checkpoint requires the bound development branch.", "branch_binding")
    _check_repository_config(workspace, github, deadline)
    remote = _git(workspace, ["config", "--get", "remote.origin.url"], github, deadline).stdout.decode().strip()
    _validate_remote(remote, repository)
    active = _git(workspace, ["symbolic-ref", "--quiet", "--short", "HEAD"], github, deadline).stdout.decode().strip()
    if active != branch:
        raise BlockedError("Workspace is on another branch.", "branch_binding")
    _git(workspace, ["check-ref-format", "refs/heads/" + branch], github, deadline)
    marker = workspace / ".idea-vault/project.json"
    if (project.get("idea_id") or project.get("source_repository")) and not marker.exists():
        raise BlockedError("Project ownership marker is missing.", "repository_binding")
    if marker.exists():
        _check_path(workspace, ".idea-vault/project.json")
        try:
            binding = json.loads(marker.read_text())
        except (ValueError, OSError):
            raise BlockedError("Project binding is invalid in the workspace.", "repository_binding") from None
        if not isinstance(binding, dict):
            raise BlockedError("Project binding must be an object.", "repository_binding")
        if (project.get("idea_id") and binding.get("idea_id") != project["idea_id"]
                or project.get("source_repository")
                and binding.get("source_repository") != project["source_repository"]):
            raise BlockedError("Workspace binding does not match the project.", "repository_binding")
        committed = _git(workspace, ["show", "HEAD:.idea-vault/project.json"], github, deadline,
                         allow_nonzero=True)
        if committed.returncode == 0 and marker.read_bytes() != committed.stdout:
            raise BlockedError("Project ownership marker was modified.", "repository_binding")
    candidates = _git(workspace, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                      github, deadline).stdout.split(b"\0")
    paths = sorted({os.fsdecode(value) for value in candidates if value})
    if len(paths) > 10000:
        raise BlockedError("Checkpoint file count exceeds the safety limit.", "checkpoint_limit")
    for relative in paths:
        _check_path(workspace, relative)
    for offset in range(0, len(paths), 100):
        _git(workspace, ["add", "--all", "--"] + paths[offset:offset + 100], github, deadline)
    workflow_changes = _git(workspace, ["diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB",
                                        "-z", "--", ".github/workflows"], github, deadline).stdout
    if workflow_changes:
        raise BlockedError("Development checkpoints cannot add or change Actions workflows.",
                           "development_workflow")
    # Scan the actual index blobs. Reading the working tree alone leaves a race
    # between secret detection and git add when Codex is still writing files.
    entries = _git(workspace, ["ls-files", "--stage", "-z"], github, deadline).stdout.split(b"\0")
    for entry in entries:
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        mode, blob, stage = metadata.split()
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise BlockedError("Symlinks, submodules and unmerged files cannot be checkpointed.", "unsafe_checkpoint")
        _check_path(workspace, os.fsdecode(filename))
        size = _git(workspace, ["cat-file", "-s", blob.decode("ascii")], github, deadline).stdout
        if int(size) > 25 * 1024 * 1024:
            raise BlockedError("Checkpoint file exceeds 25 MiB.", "checkpoint_limit")
        _scan_content(_git(workspace, ["cat-file", "blob", blob.decode("ascii")], github, deadline).stdout)
    difference = _git(workspace, ["diff", "--cached", "--quiet", "--no-ext-diff"], github, deadline,
                      allow_nonzero=True)
    if difference.returncode not in {0, 1}:
        raise RunnerError("Cannot inspect checkpoint changes.", "checkpoint_git")
    changed = difference.returncode == 1
    if changed:
        _git(workspace, ["commit", "-m", "Save Idea Vault development checkpoint"], github, deadline)
    commit = _git(workspace, ["rev-parse", "HEAD"], github, deadline).stdout.decode().strip()
    _check_workflow_changes(workspace, commit, github, deadline)
    # A clean current index does not prove that Codex's earlier local commits
    # were clean. Inspect every outgoing blob before publishing its history.
    outgoing = _git(workspace, ["rev-list", "--objects", commit, "--not", "--remotes=origin"],
                    github, deadline).stdout.splitlines()
    if len(outgoing) > 30000:
        raise BlockedError("Outgoing history exceeds the checkpoint safety limit.", "checkpoint_limit")
    for entry in outgoing:
        blob, _, filename = entry.partition(b" ")
        kind = _git(workspace, ["cat-file", "-t", blob.decode("ascii")], github, deadline).stdout.strip()
        if kind != b"blob":
            continue
        if filename:
            _check_path(workspace, os.fsdecode(filename))
        size = _git(workspace, ["cat-file", "-s", blob.decode("ascii")], github, deadline).stdout
        if int(size) > 25 * 1024 * 1024:
            raise BlockedError("Outgoing history contains a file larger than 25 MiB.", "checkpoint_limit")
        _scan_content(_git(workspace, ["cat-file", "blob", blob.decode("ascii")], github, deadline).stdout)
    # Push even when the index was unchanged: Codex may have created local
    # commits that have not been pushed by the host controller yet.
    # Re-check token-receiving commands after staging; never publish a newer HEAD
    # that appeared after the immutable commit above was scanned.
    _check_repository_config(workspace, github, deadline)
    _git(workspace, ["push", "--porcelain", remote, commit + ":refs/heads/" + branch], github, deadline)
    remote_head = _git(workspace, ["ls-remote", remote, "refs/heads/" + branch], github, deadline).stdout
    if not remote_head.split() or remote_head.split()[0].decode() != commit:
        raise RunnerError("GitHub did not confirm the checkpoint commit.", "checkpoint_result_unknown")
    if note:
        _scan_content(note.encode("utf-8"))
        _remaining(deadline)
        previous_timeout = getattr(github, "timeout", 30)
        try:
            github.timeout = min(previous_timeout, _remaining(deadline))
            github.issue_comment(repository, project["issue_number"],
                                 "<!-- idea-vault-checkpoint -->\n\n" + note +
                                 "\n\nSaved commit: `%s` on `%s`." % (commit, branch))
        finally:
            github.timeout = previous_timeout
    return {"commit": commit, "branch": branch, "changed": changed}
