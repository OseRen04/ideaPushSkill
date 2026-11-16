"""Exercise new-user deployments with real local Git and no network or login."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location(
    "idea_vault_installer", Path(__file__).resolve().parents[1] / "scripts/install_runner.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
OWNER = "another-user"
IDEAS = OWNER + "/my-ideas"
RUNNER = OWNER + "/my-runner"


def dump(value):
    return json.dumps(value, indent=2) + "\n"


def repository(name, private=True):
    return {"full_name": name, "owner": {"login": name.split("/")[0]},
            "private": private, "default_branch": "main"}


def bundle(path):
    files = {
        "config/runner.template.json": dump({
            "owner": "__OWNER__", "ideas_repository": "__IDEAS_REPOSITORY__",
            "runner_repository": "__RUNNER_REPOSITORY__", "state_path": "state/projects.json",
            "schedule_path": "settings/development-schedule.yaml",
            "billing": {"cache_repository": "__RUNNER_REPOSITORY__", "stop_remaining_minutes": 500,
                        "confirmed_stop_usage": False, "monthly_compute_allowance_usd": None},
        }),
        "config/billing-snapshot.example.json": dump({"owner": "__OWNER__", "cache_repository": "__RUNNER_REPOSITORY__",
                                                       "compute_remaining_usd": None}),
        "config/development-schedule.example.yaml": "enabled: false\ntimezone: Asia/Tokyo\n",
        "pyproject.toml": '[project]\nname="idea-vault-runner"\nversion="0.1.0"\n',
        "idea_vault_runner/controller.py": 'print("a bundled controller")\n',
        "scripts/install_symphony.py": "# pinned runtime installer\n",
        "config/WORKFLOW.md": "# Authorized development workflow\n",
        "state/projects.json": dump({"version": 1, "projects": {}, "last_run": None}),
        INSTALLER.WORKFLOW: "name: Runner\non:\n  workflow_dispatch:\njobs: {}\n",
        "README.md": "# Private runner\n",
        ".gitignore": ".runtime/\nauth.json\n",
    }
    manifest = {"schema_version": 1, "bundle_version": "0.6.0", "source_commit": "a" * 40,
                "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}}
    path.mkdir()
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (path / INSTALLER.MANIFEST).write_text(dump(manifest))
    return path


class FakeGitHub:
    def __init__(self, root):
        self.root = root
        self.profile_value = {"login": OWNER, "id": 12345}
        self.repos = {IDEAS: repository(IDEAS)}
        self.files = {(IDEAS, "alpha/idea.md"): "# Untouched project\n"}
        self.variables = {}
        self.events = []
        self.git = None

    def profile(self):
        return copy.deepcopy(self.profile_value)

    def repository(self, name):
        return copy.deepcopy(self.repos.get(name))

    def create_repository(self, name, initialize=False):
        self.events.append(("create", name, initialize))
        self.repos[name] = repository(name)
        if name == RUNNER:
            self.git.ensure_remote(name)
        return copy.deepcopy(self.repos[name])

    def read_file(self, name, path, branch):
        assert branch == "main"
        return self.files.get((name, path))

    def file_paths(self, name, branch):
        assert branch == "main"
        return {path for repo, path in self.files if repo == name}

    def disable_runner(self, name):
        self.events.append(("disable", name))
        self.variables[name] = "false"

    def create_schedule_if_absent(self, name, branch, content):
        self.events.append(("schedule", name))
        if (name, INSTALLER.SCHEDULE) not in self.files:
            self.files[(name, INSTALLER.SCHEDULE)] = content.decode()
            return True
        return False


class LocalGit:
    """Translate only canonical GitHub URLs to temporary bare Git repositories."""
    def __init__(self, root, gh):
        self.root = root
        self.gh = gh
        self.binary = shutil.which("git")
        self.fail_push = False
        self.commands = []

    def remote(self, name):
        return self.root / (name.replace("/", "-") + ".git")

    def ensure_remote(self, name):
        path = self.remote(name)
        if not path.exists():
            subprocess.run([self.binary, "init", "--bare", "--initial-branch=main", str(path)],
                           capture_output=True, check=True)

    def raw(self, args, cwd=None, allow_failure=False):
        env = INSTALLER._environment()
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        result = subprocess.run([self.binary, "-c", "core.hooksPath=/dev/null", *args],
                                cwd=cwd, env=env, text=True, capture_output=True)
        if result.returncode and not allow_failure:
            raise INSTALLER.InstallError("Local Git operation failed.", "git_failed")
        return result

    def run(self, args, cwd=None, allow_failure=False):
        self.commands.append(list(args))
        actual = list(args)
        if args[0] == "push":
            self.gh.events.append(("push", RUNNER))
            if self.fail_push:
                self.fail_push = False
                raise INSTALLER.InstallError("Simulated push interruption", "git_failed")
            actual[actual.index("origin")] = str(self.remote(RUNNER))
        if args[0] == "clone":
            actual = [str(self.remote(RUNNER)) if a == "https://github.com/" + RUNNER + ".git" else a for a in actual]
        result = self.raw(actual, cwd=cwd, allow_failure=allow_failure)
        if args[0] == "push" and not result.returncode:
            names = self.raw(["ls-tree", "-r", "--name-only", "HEAD"], cwd=cwd).stdout.splitlines()
            for name in names:
                self.gh.files[(RUNNER, name)] = self.raw(["show", "HEAD:" + name], cwd=cwd).stdout
        return result

    def remote_refs(self, name):
        return self.raw(["ls-remote", "--heads", "--tags", str(self.remote(name))]).stdout.strip()


@pytest.fixture
def case(tmp_path):
    source = bundle(tmp_path / "bundle")
    gh = FakeGitHub(tmp_path)
    git = LocalGit(tmp_path, gh)
    gh.git = git
    workspace = tmp_path / "workspace"
    def install(**changes):
        values = {"workspace": workspace, "owner": OWNER, "ideas_repository": IDEAS,
                  "runner_repository": RUNNER, "gh": gh, "git": git, "bundle_dir": source}
        values.update(changes)
        return INSTALLER.install_runner(**values)
    return SimpleNamespace(bundle=source, gh=gh, git=git, workspace=workspace, install=install)


def test_preview_does_not_create_files_repositories_or_variables(case):
    result = case.install()
    assert result["status"] == "preview"
    assert result["runner_state"] == "absent"
    assert not result["deployment_complete"] and not result["full_setup_complete"]
    assert not case.workspace.exists()
    assert case.gh.events == []
    assert not case.git.commands


def test_new_user_deployment_commits_actual_ci_config_and_workflow_over_https(case):
    before = {p.relative_to(case.bundle).as_posix(): p.read_bytes() for p in case.bundle.rglob("*") if p.is_file()}
    result = case.install(apply=True)
    assert result["status"] == "deployed" and result["deployment_complete"]
    assert not result["full_setup_complete"] and not result["operational_readiness_verified"]
    config = json.loads(case.gh.files[(RUNNER, INSTALLER.CONFIG)])
    assert config["owner"] == OWNER
    assert config["ideas_repository"] == IDEAS
    assert config["runner_repository"] == config["billing"]["cache_repository"] == RUNNER
    assert config["billing"]["stop_remaining_minutes"] == 500
    assert config["billing"]["monthly_compute_allowance_usd"] is None
    assert config["billing"]["confirmed_stop_usage"] is False
    example = json.loads(case.gh.files[(RUNNER, "config/billing-snapshot.example.json")])
    assert example["owner"] == OWNER and example["cache_repository"] == RUNNER
    assert example["compute_remaining_usd"] is None
    assert (RUNNER, "state/billing-snapshot.json") not in case.gh.files
    assert (RUNNER, INSTALLER.WORKFLOW) in case.gh.files
    assert (RUNNER, INSTALLER.INSTALLATION) in case.gh.files
    assert case.git.raw(["remote", "get-url", "origin"], cwd=case.workspace).stdout.strip() == "https://github.com/" + RUNNER + ".git"
    assert case.git.raw(["status", "--porcelain"], cwd=case.workspace).stdout == ""
    assert case.gh.variables[RUNNER] == "false"
    assert case.gh.events.index(("disable", RUNNER)) < case.gh.events.index(("push", RUNNER))
    assert "enabled: false" in case.gh.files[(IDEAS, INSTALLER.SCHEDULE)]
    assert before == {p.relative_to(case.bundle).as_posix(): p.read_bytes() for p in case.bundle.rglob("*") if p.is_file()}


def test_optional_ideas_creation_uses_the_signed_in_users_private_namespace(case):
    del case.gh.repos[IDEAS]
    assert case.install()["create_ideas_required"]
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "ideas_repository_missing"
    assert not case.workspace.exists()
    assert not case.gh.events
    result = case.install(apply=True, create_ideas=True)
    assert case.gh.repos[IDEAS]["private"]
    assert ("create", IDEAS, True) in case.gh.events
    assert result["create_ideas_required"] is False


@pytest.mark.parametrize("changes", [{"owner": "someone-else"}, {"runner_repository": "someone-else/runner"},
                                      {"ideas_repository": "someone-else/ideas"}, {"runner_repository": IDEAS}])
def test_unrelated_owners_and_ambiguous_targets_never_create_or_push(case, changes):
    with pytest.raises(INSTALLER.InstallError):
        case.install(apply=True, **changes)
    assert not case.gh.events
    assert not case.workspace.exists()


def test_existing_public_runner_is_not_changed_to_private(case):
    case.gh.repos[RUNNER] = repository(RUNNER, private=False)
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "repository_mismatch"
    assert not case.gh.events


def test_existing_nonempty_unmanaged_runner_is_not_overwritten(case):
    case.gh.repos[RUNNER] = repository(RUNNER)
    case.git.remote_refs = lambda name: "non-empty-reference"
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "repository_not_empty"
    assert not case.gh.events and not case.workspace.exists()


def test_repeat_install_preserves_remote_state_secrets_schedule_and_enabled_switch(case):
    case.install(apply=True)
    config = json.loads(case.gh.files[(RUNNER, INSTALLER.CONFIG)])
    config["billing"]["confirmed_stop_usage"] = True
    case.gh.files[(RUNNER, INSTALLER.CONFIG)] = dump(config)
    case.gh.files[(RUNNER, "state/quota-stop.json")] = '{"stopped": true}'
    case.gh.files[(IDEAS, INSTALLER.SCHEDULE)] = "enabled: true\ncustom: keep\n"
    case.gh.variables[RUNNER] = "true"
    before_files = copy.deepcopy(case.gh.files)
    before_variables = copy.deepcopy(case.gh.variables)
    case.gh.events.clear()
    case.git.commands.clear()
    result = case.install(apply=True)
    assert result["status"] == "verified_existing"
    assert case.gh.files == before_files and case.gh.variables == before_variables
    assert not case.gh.events
    assert all(args[0] not in {"commit", "add", "push", "config"} for args in case.git.commands)


def test_existing_managed_repo_can_be_obtained_in_a_new_empty_workspace(case, tmp_path):
    case.install(apply=True)
    case.gh.events.clear()
    other = tmp_path / "new-machine-copy"
    result = case.install(workspace=other, apply=True)
    assert result["status"] == "verified_existing"
    assert (other / INSTALLER.CONFIG).is_file()
    assert json.loads((other / INSTALLER.CONFIG).read_text())["owner"] == OWNER
    assert not case.gh.events


def test_interrupted_push_resumes_without_duplicate_creation_or_config_loss(case):
    case.git.fail_push = True
    with pytest.raises(INSTALLER.InstallError):
        case.install(apply=True)
    head = case.git.raw(["rev-parse", "HEAD"], cwd=case.workspace).stdout
    config = (case.workspace / INSTALLER.CONFIG).read_bytes()
    result = case.install(apply=True)
    assert result["status"] == "deployed"
    assert case.git.raw(["rev-parse", "HEAD"], cwd=case.workspace).stdout == head
    assert (case.workspace / INSTALLER.CONFIG).read_bytes() == config
    assert sum(e == ("create", RUNNER, False) for e in case.gh.events) == 1


def test_interrupted_file_copy_resumes_from_the_ownership_marker(case, monkeypatch):
    original = Path.write_bytes
    def interrupted(path, content):
        if path == case.workspace / "pyproject.toml":
            raise OSError("simulated local interruption")
        return original(path, content)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_bytes", interrupted)
        with pytest.raises(OSError):
            case.install(apply=True)
    assert (case.workspace / INSTALLER.INSTALLATION).is_file()
    assert not case.gh.events
    assert case.install(apply=True)["deployment_complete"]


@pytest.mark.parametrize("change", ["dirty", "origin", "pushurl"])
def test_changed_workspace_is_rejected_without_touching_remote(case, change):
    case.install(apply=True)
    if change == "dirty":
        (case.workspace / "README.md").write_text("user changes\n")
    elif change == "origin":
        case.git.raw(["remote", "set-url", "origin", "https://github.com/another-owner/runner.git"], cwd=case.workspace)
    else:
        case.git.raw(["config", "remote.origin.pushurl", "https://github.com/another-owner/runner.git"], cwd=case.workspace)
    case.gh.events.clear()
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code in {"workspace_dirty", "origin_mismatch"}
    assert not case.gh.events


def test_unmanaged_workspace_and_plugin_cache_are_never_modified(case):
    case.workspace.mkdir()
    secret = case.workspace / ".env"
    secret.write_text("secret-value")
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "workspace_not_empty"
    assert secret.read_text() == "secret-value"
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(workspace=case.bundle / "generated", apply=True)
    assert error.value.code == "unsafe_workspace"
    assert not (case.bundle / "generated").exists()
    plugin_workspace = INSTALLER.SKILL_DIR / "scripts" / "user-workspace"
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(workspace=plugin_workspace, apply=True)
    assert error.value.code == "unsafe_workspace"
    assert not plugin_workspace.exists()


@pytest.mark.parametrize("missing", ["idea_vault_runner/controller.py", "pyproject.toml",
                                      "scripts/install_symphony.py", "config/WORKFLOW.md"])
def test_existing_marker_without_required_program_files_is_not_a_verified_deployment(case, missing):
    case.install(apply=True)
    del case.gh.files[(RUNNER, missing)]
    case.gh.events.clear()
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "runner_files_missing"
    assert not case.gh.events


@pytest.mark.parametrize("kind", ["checksum", "extra", "symlink", "private-state", "path-traversal"])
def test_invalid_bundles_stop_before_any_github_action(case, kind, tmp_path):
    if kind == "checksum":
        (case.bundle / "README.md").write_text("changed")
    elif kind == "extra":
        (case.bundle / "auth.json").write_text("private-value")
    elif kind == "symlink":
        (case.bundle / "README.md").unlink()
        target = tmp_path / "outside"
        target.write_text("outside")
        (case.bundle / "README.md").symlink_to(target)
    else:
        manifest_path = case.bundle / INSTALLER.MANIFEST
        manifest = json.loads(manifest_path.read_text())
        if kind == "private-state":
            value = dump({"version": 1, "projects": {"private-project": {}}, "last_run": None})
            (case.bundle / "state/projects.json").write_text(value)
            manifest["files"]["state/projects.json"] = hashlib.sha256(value.encode()).hexdigest()
        else:
            manifest["files"]["../outside"] = "a" * 64
        manifest_path.write_text(dump(manifest))
    with pytest.raises(INSTALLER.InstallError):
        case.install(apply=True)
    assert not case.gh.events
    assert not case.workspace.exists()


def test_version_change_does_not_implicitly_upgrade_or_enable_existing_runner(case):
    case.install(apply=True)
    marker = json.loads(case.gh.files[(RUNNER, INSTALLER.INSTALLATION)])
    marker["bundle_version"] = "0.5.0"
    case.gh.files[(RUNNER, INSTALLER.INSTALLATION)] = dump(marker)
    before = copy.deepcopy(case.gh.files)
    case.gh.events.clear()
    with pytest.raises(INSTALLER.InstallError) as error:
        case.install(apply=True)
    assert error.value.code == "upgrade_required"
    assert case.gh.files == before and not case.gh.events


def test_git_adapter_uses_per_command_https_credentials_without_global_configuration(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(INSTALLER.subprocess, "run", run)
    client = INSTALLER.GitClient("/path/to/git", "/path with spaces/gh")
    client.run(["push", "--set-upstream", "origin", "main"], cwd="/deployment")
    command, settings = calls[0]
    assert "credential.helper=!'/path with spaces/gh' auth git-credential" in command
    assert "credential.helper=" in command
    assert "--global" not in command
    assert settings["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert settings["env"]["GH_PROMPT_DISABLED"] == "1"
    assert settings["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert settings["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_github_adapter_does_not_expose_provider_output_or_tokens(monkeypatch):
    secret = "ghp_private-value-do-not-print"
    monkeypatch.setattr(INSTALLER.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 1, secret, "HTTP 403 " + secret))
    with pytest.raises(INSTALLER.InstallError) as error:
        INSTALLER.GitHubClient("/gh").api("/user")
    assert error.value.status == 403
    assert secret not in str(error.value)


def test_github_file_and_tree_references_are_url_encoded():
    client = INSTALLER.GitHubClient("/gh")
    client.api = Mock(return_value={"type": "file", "encoding": "base64",
                                   "content": base64.b64encode(b"{}\n").decode()})
    assert client.read_file(RUNNER, INSTALLER.CONFIG, "feature/setup") == "{}\n"
    assert client.api.call_args.args[0].endswith("?ref=feature%2Fsetup")
    client.api.return_value = {"truncated": False, "tree": [{"path": "file.py", "type": "blob"}]}
    assert client.file_paths(RUNNER, "feature/setup") == {"file.py"}
    assert "/git/trees/feature%2Fsetup?recursive=1" in client.api.call_args.args[0]
    client.api.return_value["truncated"] = True
    with pytest.raises(INSTALLER.InstallError) as error:
        client.file_paths(RUNNER, "main")
    assert error.value.code == "remote_tree_unverified"


def test_bundled_release_is_complete_and_contains_no_execution_history():
    manifest, files = INSTALLER.load_bundle(INSTALLER.BUNDLE_DIR)
    plugin = json.loads((INSTALLER.BUNDLE_DIR.parents[3] / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["bundle_version"] == plugin["version"]
    assert "idea_vault_runner/controller.py" in files
    assert INSTALLER.WORKFLOW in files
    assert json.loads(files["state/projects.json"])["projects"] == {}
