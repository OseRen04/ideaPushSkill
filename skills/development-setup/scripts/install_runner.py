#!/usr/bin/env python3
"""Deploy the bundled runner to the authenticated user's private GitHub repository.

The default is a read-only preview. This installer does not start login flows,
register CI credentials, enable a schedule, or declare operational readiness.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote, urlencode


SKILL_DIR = Path(__file__).resolve().parents[1]
BUNDLE_DIR = SKILL_DIR / "assets" / "runner-template"
MANIFEST = "bundle-manifest.json"
INSTALLATION = ".idea-vault/installation.json"
WORKFLOW = ".github/workflows/autonomous-development.yml"
CONFIG = "config/runner.yaml"
SCHEDULE = "settings/development-schedule.yaml"
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class InstallError(RuntimeError):
    def __init__(self, message, code="installation_failed", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("invalid constant")
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, TypeError, UnicodeError):
        raise InstallError("A deployment metadata file is invalid.", "invalid_metadata") from None


def _dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InstallError("A bundled path is invalid.", "invalid_bundle_path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise InstallError("A bundled path is invalid.", "invalid_bundle_path")
    lowered = [p.lower() for p in parts]
    if (any(p in {".git", ".venv", ".runtime", "__pycache__", ".pytest_cache"} for p in lowered)
            or any(p.startswith(".env") for p in lowered)
            or lowered[-1] == "auth.json" or lowered[-1].endswith((".pem", ".key", ".pyc"))
            or value in {"state/billing-snapshot.json", INSTALLATION, CONFIG}):
        raise InstallError("Private or generated files cannot be bundled.", "private_bundle_file")
    return path


def load_bundle(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise InstallError("The bundled runner is unavailable.", "bundle_unavailable")
    manifest_path = directory / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InstallError("The runner manifest is unavailable.", "bundle_unavailable")
    manifest = _json(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest.get("bundle_version"), str)
            or not VERSION.fullmatch(manifest["bundle_version"])
            or not isinstance(manifest.get("files"), dict)
            or not 1 <= len(manifest["files"]) <= 1000):
        raise InstallError("The runner manifest is invalid.", "invalid_bundle_manifest")
    files = {}
    total = 0
    names = set()
    for name, digest in manifest["files"].items():
        path = _safe_path(name)
        if name.casefold() in names:
            raise InstallError("Bundled paths collide on case-insensitive filesystems.", "invalid_bundle_path")
        names.add(name.casefold())
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise InstallError("A runner checksum is invalid.", "invalid_bundle_manifest")
        target = directory.joinpath(*path.parts)
        if any(p.is_symlink() for p in [target, *target.parents] if p != directory.parent):
            raise InstallError("Symlinks cannot be bundled.", "bundle_symlink")
        if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
            raise InstallError("A bundled file is unavailable or oversized.", "invalid_bundle_file")
        content = target.read_bytes()
        total += len(content)
        if total > 10 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
            raise InstallError("Runner checksums do not match.", "bundle_checksum_mismatch")
        files[name] = content
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise InstallError("Symlinks cannot be bundled.", "bundle_symlink")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(files) | {MANIFEST}:
        raise InstallError("The bundle contains unlisted files.", "bundle_unlisted_file")
    required = {"config/runner.template.json", "config/development-schedule.example.yaml",
                "pyproject.toml", WORKFLOW, "idea_vault_runner/controller.py",
                "scripts/install_symphony.py", "config/WORKFLOW.md"}
    if not required <= set(files):
        raise InstallError("The runner bundle is incomplete.", "bundle_incomplete")
    if "state/projects.json" in files:
        state = _json(files["state/projects.json"])
        if state != {"version": 1, "projects": {}, "last_run": None}:
            raise InstallError("The bundle contains execution history.", "private_bundle_state")
    return manifest, files


def _environment():
    env = os.environ.copy()
    env.pop("GH_DEBUG", None)
    for name in list(env):
        if name.startswith("GIT_") and name not in {"GIT_SSH_COMMAND"}:
            env.pop(name, None)
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


def _binary(name, override=None):
    value = override or shutil.which(name)
    if not value:
        raise InstallError("A required command is unavailable: " + name, "command_missing")
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise InstallError("A required command is unavailable: " + name, "command_missing")
    return str(path.resolve())


class GitHubClient:
    def __init__(self, binary):
        self.binary = binary

    def api(self, path, method="GET", data=None):
        args = [self.binary, "api", "--hostname", "github.com", "--method", method,
                "-H", "Accept: application/vnd.github+json", path]
        value = None
        if data is not None:
            args.extend(["--input", "-"])
            value = _dump(data)
        try:
            result = subprocess.run(args, input=value, capture_output=True, text=True,
                                    env=_environment(), timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError("GitHub could not be reached.", "github_unavailable") from None
        if result.returncode:
            match = re.search(r"HTTP(?:[ /]|\s+)(\d{3})", result.stderr)
            status = int(match.group(1)) if match else None
            raise InstallError("GitHub rejected a setup operation; check authentication and permissions.",
                               "github_http_" + str(status) if status else "github_failed", status)
        return _json(result.stdout) if result.stdout.strip() else None

    def profile(self):
        return self.api("/user")

    def repository(self, name):
        try:
            return self.api("/repos/" + name)
        except InstallError as error:
            if error.status == 404:
                return None
            raise

    def create_repository(self, name, initialize=False):
        return self.api("/user/repos", "POST", {"name": name.split("/")[1], "private": True,
                                               "auto_init": initialize, "has_issues": True})

    def read_file(self, repository, path, branch):
        try:
            item = self.api("/repos/%s/contents/%s?%s" % (repository, path, urlencode({"ref": branch})))
        except InstallError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(item, dict) or item.get("type") != "file" or item.get("encoding") != "base64":
            raise InstallError("A deployed file could not be verified.", "remote_file_unverified")
        try:
            return base64.b64decode("".join(item["content"].split()), validate=True).decode("utf-8")
        except (ValueError, UnicodeError, KeyError, TypeError):
            raise InstallError("A deployed file could not be verified.", "remote_file_unverified") from None

    def file_paths(self, repository, branch):
        tree = self.api("/repos/%s/git/trees/%s?recursive=1" % (repository, quote(branch, safe="")))
        if (not isinstance(tree, dict) or tree.get("truncated") is not False
                or not isinstance(tree.get("tree"), list)):
            raise InstallError("The deployed file inventory could not be verified.", "remote_tree_unverified")
        paths = set()
        for item in tree["tree"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise InstallError("The deployed file inventory is invalid.", "remote_tree_unverified")
            if item.get("type") == "blob":
                paths.add(item["path"])
        return paths

    def disable_runner(self, repository):
        data = {"name": "RUNNER_ENABLED", "value": "false"}
        try:
            self.api("/repos/%s/actions/variables/RUNNER_ENABLED" % repository, "PATCH", data)
        except InstallError as error:
            if error.status != 404:
                raise
            self.api("/repos/%s/actions/variables" % repository, "POST", data)

    def create_schedule_if_absent(self, repository, branch, content):
        if self.read_file(repository, SCHEDULE, branch) is not None:
            return False
        try:
            self.api("/repos/%s/contents/%s" % (repository, SCHEDULE), "PUT", {
                "message": "Add disabled Idea Vault development schedule", "branch": branch,
                "content": base64.b64encode(content).decode("ascii"),
            })
        except InstallError as error:
            if error.status not in {409, 422} or self.read_file(repository, SCHEDULE, branch) is None:
                raise
            return False
        return True


class GitClient:
    def __init__(self, binary, gh_binary):
        self.binary = binary
        self.gh_binary = gh_binary

    def run(self, args, cwd=None, allow_failure=False):
        helper = "!" + shlex.quote(self.gh_binary) + " auth git-credential"
        command = [self.binary, "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                   "-c", "credential.helper=" + helper, *args]
        env = _environment()
        # Isolate this dedicated deployment copy from global URL rewrites and
        # filters without modifying the user's Git configuration on disk.
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                    env=env, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError("A Git setup operation could not finish.", "git_unavailable") from None
        if result.returncode and not allow_failure:
            raise InstallError("A Git setup operation failed; verify repository access.", "git_failed")
        return result

    def remote_refs(self, repository):
        return self.run(["ls-remote", "https://github.com/" + repository + ".git"]).stdout.strip()


def _repository(value, owner):
    if (not isinstance(value, str) or not REPOSITORY.fullmatch(value)
            or any(p in {".", ".."} for p in value.split("/"))
            or value.split("/")[0].casefold() != owner.casefold()):
        raise InstallError("Repository targets must belong to the authenticated user.", "owner_mismatch")
    return value


def _verify_repository(meta, owner, target, private):
    if (not isinstance(meta, dict) or not isinstance(meta.get("owner"), dict)
            or str(meta["owner"].get("login", "")).casefold() != owner.casefold()
            or str(meta.get("full_name", "")).casefold() != target.casefold()
            or private and meta.get("private") is not True):
        raise InstallError("The repository owner or visibility does not match.", "repository_mismatch")


def _verify_marker(marker, owner, ideas, runner, version):
    if (not isinstance(marker, dict) or type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 1 or marker.get("installer") != "idea-vault-runner"
            or str(marker.get("owner", "")).casefold() != owner.casefold()
            or str(marker.get("ideas_repository", "")).casefold() != ideas.casefold()
            or str(marker.get("runner_repository", "")).casefold() != runner.casefold()):
        raise InstallError("The existing installation belongs to a different target.", "installation_mismatch")
    if marker.get("bundle_version") != version:
        raise InstallError("The existing runner uses a different version; an explicit upgrade is required.",
                           "upgrade_required")


def _check_workspace(path, bundle, owner, ideas, runner, version, git):
    if path.is_symlink():
        raise InstallError("The workspace must not be a symlink.", "unsafe_workspace")
    path = path.resolve()
    bundle = Path(bundle).resolve()
    plugin_root = Path(__file__).resolve().parents[3]
    if (path == bundle or path in bundle.parents or bundle in path.parents
            or path == plugin_root or plugin_root in path.parents):
        raise InstallError("The workspace must be separate from the plugin assets.", "unsafe_workspace")
    if not path.exists():
        return path, False, False
    if not path.is_dir():
        raise InstallError("The workspace must be a directory.", "unsafe_workspace")
    if not any(path.iterdir()):
        return path, False, False
    for p in [path / ".idea-vault", path / INSTALLATION, path / ".git"]:
        if p.is_symlink():
            raise InstallError("The workspace contains a symlink.", "unsafe_workspace")
    if (path / ".git").exists() and not (path / ".git").is_dir():
        raise InstallError("Linked worktrees are not deployment workspaces.", "unsafe_workspace")
    marker_path = path / INSTALLATION
    if not marker_path.is_file():
        raise InstallError("Use an empty workspace or a matching managed installation.", "workspace_not_empty")
    marker = _json(marker_path.read_text(encoding="utf-8"))
    _verify_marker(marker, owner, ideas, runner, version)
    if not (path / ".git").is_dir():
        return path, True, False
    expected = {("https://github.com/" + runner + ".git").casefold(),
                ("git@github.com:" + runner + ".git").casefold()}
    remotes = git.run(["remote"], cwd=path).stdout.splitlines()
    if "origin" in remotes:
        for args in (["remote", "get-url", "origin"], ["remote", "get-url", "--push", "origin"]):
            if git.run(args, cwd=path).stdout.strip().casefold() not in expected:
                raise InstallError("The workspace origin points to another repository.", "origin_mismatch")
    has_head = git.run(["rev-parse", "--verify", "HEAD"], cwd=path, allow_failure=True).returncode == 0
    if has_head and git.run(["status", "--porcelain"], cwd=path).stdout.strip():
        raise InstallError("The workspace has uncommitted changes; no files were overwritten.", "workspace_dirty")
    return path, True, has_head


def _remote_state(gh, git, meta, owner, ideas, runner, version, required_paths):
    if meta is None:
        return "absent"
    _verify_repository(meta, owner, runner, private=True)
    if not git.remote_refs(runner):
        return "empty"
    branch = meta.get("default_branch")
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
        raise InstallError("The default branch could not be verified.", "branch_unverified")
    raw_marker = gh.read_file(runner, INSTALLATION, branch)
    if raw_marker is None:
        raise InstallError("The existing repository is not an Idea Vault installation.", "repository_not_empty")
    _verify_marker(_json(raw_marker), owner, ideas, runner, version)
    if not required_paths <= gh.file_paths(runner, branch):
        raise InstallError("The deployed runner is missing required program files.", "runner_files_missing")
    raw_config = gh.read_file(runner, CONFIG, branch)
    config = _json(raw_config)
    expected = {"owner": owner, "ideas_repository": ideas, "runner_repository": runner}
    if (not isinstance(config, dict)
            or any(not isinstance(config.get(key), str) or config[key].casefold() != value.casefold()
                   for key, value in expected.items())
            or not isinstance(config.get("billing"), dict)
            or not isinstance(config["billing"].get("cache_repository"), str)
            or config["billing"]["cache_repository"].casefold() != runner.casefold()):
        raise InstallError("The deployed configuration does not match the user.", "remote_config_mismatch")
    if gh.read_file(runner, WORKFLOW, branch) is None:
        raise InstallError("The deployed Actions workflow is missing.", "workflow_missing")
    return "managed"


def _payload(files, manifest, owner, ideas, runner):
    payload = dict(files)
    config = _json(files["config/runner.template.json"])
    if not isinstance(config, dict) or not isinstance(config.get("billing"), dict):
        raise InstallError("The configuration template is invalid.", "invalid_bundle_config")
    config.update(owner=owner, ideas_repository=ideas, runner_repository=runner)
    config["billing"]["cache_repository"] = runner
    threshold = config["billing"].get("stop_remaining_minutes", 500)
    if type(threshold) is not int or threshold < 500:
        raise InstallError("The template does not protect the free allowance.", "invalid_bundle_config")
    config["billing"]["stop_remaining_minutes"] = threshold
    payload[CONFIG] = _dump(config).encode("utf-8")
    example_name = "config/billing-snapshot.example.json"
    if example_name in payload:
        example = _json(payload[example_name])
        if not isinstance(example, dict):
            raise InstallError("The billing example is invalid.", "invalid_bundle_config")
        example.update(owner=owner, cache_repository=runner)
        payload[example_name] = _dump(example).encode("utf-8")
    marker = {"schema_version": 1, "installer": "idea-vault-runner", "owner": owner,
              "ideas_repository": ideas, "runner_repository": runner,
              "bundle_version": manifest["bundle_version"], "source_commit": manifest.get("source_commit"),
              "managed_paths": sorted(payload)}
    payload[INSTALLATION] = _dump(marker).encode("utf-8")
    return payload


def _write_new_workspace(workspace, payload):
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        target = workspace.joinpath(*PurePosixPath(name).parts)
        if any(p.is_symlink() for p in [target, *target.parents] if p != workspace.parent):
            raise InstallError("The workspace contains a symlink.", "unsafe_workspace")
        if target.exists() and (not target.is_file() or target.read_bytes() != content):
            raise InstallError("An existing workspace file differs; no files were overwritten.", "workspace_changed")
    # Write the ownership marker first so an interrupted copy can be resumed.
    for name in [INSTALLATION, *[n for n in payload if n != INSTALLATION]]:
        content = payload[name]
        target = workspace.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)


def _commit_and_push(git, workspace, payload, owner, user_id, runner, has_head):
    if not (workspace / ".git").is_dir():
        git.run(["init", "--initial-branch=main"], cwd=workspace)
    if not has_head:
        git.run(["config", "--local", "user.name", owner], cwd=workspace)
        email = (str(user_id) + "+" + owner if type(user_id) is int and user_id > 0 else owner)
        git.run(["config", "--local", "user.email", email + "@users.noreply.github.com"], cwd=workspace)
        git.run(["add", "--", *sorted(payload)], cwd=workspace)
        git.run(["commit", "-m", "Install private Idea Vault runner"], cwd=workspace)
    if "origin" not in git.run(["remote"], cwd=workspace).stdout.splitlines():
        git.run(["remote", "add", "origin", "https://github.com/" + runner + ".git"], cwd=workspace)
    allowed = {("https://github.com/" + runner + ".git").casefold(),
               ("git@github.com:" + runner + ".git").casefold()}
    for args in (["remote", "get-url", "origin"], ["remote", "get-url", "--push", "origin"]):
        if git.run(args, cwd=workspace).stdout.strip().casefold() not in allowed:
            raise InstallError("The push destination does not match the user's repository.", "origin_mismatch")
    branch = git.run(["symbolic-ref", "--short", "HEAD"], cwd=workspace).stdout.strip()
    if branch != "main":
        raise InstallError("The deployment workspace must use its initial main branch.", "branch_mismatch")
    git.run(["push", "--set-upstream", "origin", "main"], cwd=workspace)


def install_runner(workspace, owner=None, ideas_repository=None, runner_repository=None,
                   apply=False, create_ideas=False, gh=None, git=None, bundle_dir=None):
    bundle_dir = Path(bundle_dir or BUNDLE_DIR)
    manifest, files = load_bundle(bundle_dir)
    required_paths = {path for path in files if path.startswith("idea_vault_runner/") and path.endswith(".py")}
    required_paths.update({"pyproject.toml", "scripts/install_symphony.py", "config/WORKFLOW.md"})
    gh = gh or GitHubClient(_binary("gh"))
    git = git or GitClient(_binary("git"), gh.binary)
    profile = gh.profile()
    login = profile.get("login") if isinstance(profile, dict) else None
    if not isinstance(login, str) or not LOGIN.fullmatch(login):
        raise InstallError("The signed-in GitHub user could not be verified.", "github_identity_unverified")
    if owner is not None and (not isinstance(owner, str) or not LOGIN.fullmatch(owner)
                              or owner.casefold() != login.casefold()):
        raise InstallError("GitHub is signed in to a different user.", "owner_mismatch")
    owner = login
    ideas = _repository(ideas_repository or owner + "/idea-vault", owner)
    runner = _repository(runner_repository or owner + "/idea-vault-runner", owner)
    if ideas.casefold() == runner.casefold():
        raise InstallError("Ideas and execution repositories must be distinct.", "repository_mismatch")
    workspace, local_managed, has_head = _check_workspace(
        Path(workspace).expanduser(), bundle_dir, owner, ideas, runner, manifest["bundle_version"], git)
    ideas_meta = gh.repository(ideas)
    if ideas_meta is not None:
        _verify_repository(ideas_meta, owner, ideas, private=False)
    runner_meta = gh.repository(runner)
    remote_state = _remote_state(gh, git, runner_meta, owner, ideas, runner, manifest["bundle_version"], required_paths)
    result = {"status": "preview", "owner": owner, "ideas_repository": ideas,
              "runner_repository": runner, "workspace": str(workspace), "private": True,
              "bundle_version": manifest["bundle_version"], "runner_state": remote_state,
              "create_ideas_required": ideas_meta is None, "deployment_complete": False,
              "full_setup_complete": False, "operational_readiness_verified": False,
              "unverified_steps": ["ci_credentials", "billing", "ci_authentication", "schedule", "idea_permission"]}
    payload = _payload(files, manifest, owner, ideas, runner)
    if not apply:
        return result
    if ideas_meta is None and not create_ideas:
        raise InstallError("The ideas repository is missing; specify --create-ideas to create it.", "ideas_repository_missing")
    if remote_state == "managed":
        if local_managed and not has_head:
            raise InstallError("The local copy is incomplete; use a new empty workspace to obtain the deployed runner.",
                               "workspace_incomplete")
        if ideas_meta is None:
            ideas_meta = gh.create_repository(ideas, initialize=True)
            _verify_repository(ideas_meta, owner, ideas, private=True)
        if not local_managed:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            git.run(["clone", "--single-branch", "--branch", runner_meta["default_branch"],
                     "https://github.com/" + runner + ".git", str(workspace)], cwd=workspace.parent)
        result.update(status="verified_existing", deployment_complete=True, create_ideas_required=False)
        return result
    if not local_managed or not has_head:
        _write_new_workspace(workspace, payload)
    if ideas_meta is None:
        ideas_meta = gh.create_repository(ideas, initialize=True)
        _verify_repository(ideas_meta, owner, ideas, private=True)
    if runner_meta is None:
        runner_meta = gh.create_repository(runner, initialize=False)
        _verify_repository(runner_meta, owner, runner, private=True)
    gh.disable_runner(runner)
    _commit_and_push(git, workspace, payload, owner, profile.get("id"), runner, has_head)
    branch = ideas_meta.get("default_branch") or "main"
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch):
        raise InstallError("The ideas default branch could not be verified.", "branch_unverified")
    gh.create_schedule_if_absent(ideas, branch, files["config/development-schedule.example.yaml"])
    deployed = gh.repository(runner)
    if _remote_state(gh, git, deployed, owner, ideas, runner, manifest["bundle_version"], required_paths) != "managed":
        raise InstallError("The deployed runner could not be verified.", "deployment_unverified")
    result.update(status="deployed", deployment_complete=True, runner_state="managed", create_ideas_required=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--owner")
    parser.add_argument("--ideas-repository")
    parser.add_argument("--runner-repository")
    parser.add_argument("--create-ideas", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--gh")
    parser.add_argument("--git")
    args = parser.parse_args(argv)
    try:
        gh = GitHubClient(_binary("gh", args.gh))
        git = GitClient(_binary("git", args.git), gh.binary)
        result = install_runner(args.workspace, owner=args.owner, ideas_repository=args.ideas_repository,
                                runner_repository=args.runner_repository, apply=args.apply,
                                create_ideas=args.create_ideas, gh=gh, git=git)
    except InstallError as error:
        print(_dump({"status": "blocked", "reason": error.code, "message": str(error),
                     "full_setup_complete": False, "operational_readiness_verified": False}), end="")
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(_dump({"status": "blocked", "reason": "invalid_or_unavailable_input",
                     "full_setup_complete": False, "operational_readiness_verified": False}), end="")
        return 2
    print(_dump(result), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
