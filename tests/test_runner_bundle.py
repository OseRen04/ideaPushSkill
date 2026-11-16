"""Validate the distributable bytes without private sources or network access."""

import ast
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runner_bundle_under_test", ROOT / "scripts/build_runner_bundle.py")
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)


def contents(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


@pytest.fixture
def copied_bundle(tmp_path):
    destination = tmp_path / "bundle"
    shutil.copytree(BUNDLE.OUTPUT, destination)
    return destination


@pytest.fixture
def source_checkout(tmp_path):
    """Create real Git provenance with deliberate private source contamination."""
    source = tmp_path / "source"
    source.mkdir()
    names = list(BUNDLE.STATIC) + [path.relative_to(BUNDLE.OUTPUT).as_posix()
                                  for path in (BUNDLE.OUTPUT / "idea_vault_runner").glob("*.py")]
    for name in names:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLE.OUTPUT / name, target)
    private = {
        "config/runner.yaml": "owner: private-fixture-owner\ncredential: private-fixture-credential\n",
        "config/billing-snapshot.example.json": '{"owner":"private-fixture-owner"}',
        "state/projects.json": '{"projects":{"private-fixture-project":{"history":"private-history"}}}',
        "state/billing-snapshot.json": '{"private":"private-billing-data"}',
        "state/quota-stop.json": '{"stopped":true,"private":"private-history"}',
        ".runtime/codex/auth.json": '{"access_token":"private-fixture-credential"}',
        ".env": "TOKEN=private-fixture-credential\n",
        "private.pem": "private-fixture-credential\n",
        ".venv/private.txt": "private-fixture-credential\n",
    }
    for name, text in private.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "--quiet", str(source)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin",
                    "git@github.com:private-fixture-owner/private-source.git"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(source), "add", "--force", "--all"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                    "-c", "user.name=Bundle Fixture", "-c", "user.email=bundle-fixture@example.invalid",
                    "commit", "--quiet", "-m", "Private fixture"], capture_output=True, check=True)
    return source


def test_distributed_bundle_contains_controller_cost_stop_workflow_and_dependencies():
    manifest = BUNDLE.verify(BUNDLE.OUTPUT)
    required = {
        "idea_vault_runner/controller.py", "idea_vault_runner/billing.py", "idea_vault_runner/quota_stop.py",
        ".github/workflows/autonomous-development.yml", "scripts/install_symphony.py", "pyproject.toml",
        "config/runner.template.json", "state/projects.json",
    }
    assert required <= set(manifest["files"])
    for name in manifest["files"]:
        path = BUNDLE.OUTPUT / name
        assert sha256(path.read_bytes()).hexdigest() == manifest["files"][name]
        if name.endswith(".py"):
            compile(path.read_text(), name, "exec")
    policy = ast.parse((BUNDLE.OUTPUT / "idea_vault_runner/billing.py").read_text())
    budget = next(node for node in policy.body if isinstance(node, ast.ClassDef) and node.name == "BudgetPolicy")
    threshold = next(node for node in budget.body if isinstance(node, ast.AnnAssign)
                     and isinstance(node.target, ast.Name) and node.target.id == "stop_remaining_minutes")
    assert ast.literal_eval(threshold.value) == 500
    suspension = ast.parse((BUNDLE.OUTPUT / "idea_vault_runner/quota_stop.py").read_text())
    stop_class = next(node for node in suspension.body if isinstance(node, ast.ClassDef) and node.name == "QuotaStop")
    assert {"stop", "status", "assert_not_stopped"} <= {node.name for node in stop_class.body
                                                        if isinstance(node, ast.FunctionDef)}
    workflow = yaml.load((BUNDLE.OUTPUT / ".github/workflows/autonomous-development.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["develop"]["if"] == "vars.RUNNER_ENABLED == 'true'"
    assert workflow["permissions"]["issues"] == "write"
    assert any("controller quota-check" in step.get("run", "")
               for step in workflow["jobs"]["develop"]["steps"])
    dependency_line = re.search(r"^dependencies\s*=\s*(\[.*\])$",
                                (BUNDLE.OUTPUT / "pyproject.toml").read_text(), re.M)
    dependencies = ast.literal_eval(dependency_line.group(1))
    assert any(value.startswith("PyYAML==") for value in dependencies)
    assert any(value.startswith("psutil==") for value in dependencies)


def test_distributed_configuration_is_generic_and_contains_no_real_user_state():
    manifest = BUNDLE.verify(BUNDLE.OUTPUT)
    config = json.loads((BUNDLE.OUTPUT / "config/runner.template.json").read_text())
    assert config["owner"] == "__OWNER__"
    assert config["ideas_repository"] == "__IDEAS_REPOSITORY__"
    assert config["runner_repository"] == config["billing"]["cache_repository"] == "__RUNNER_REPOSITORY__"
    assert config["billing"]["stop_remaining_minutes"] == 500
    assert config["billing"]["confirmed_stop_usage"] is False
    assert config["billing"]["monthly_compute_allowance_usd"] is None
    assert json.loads((BUNDLE.OUTPUT / "state/projects.json").read_text()) == {
        "version": 1, "projects": {}, "last_run": None,
    }
    assert {"config/runner.yaml", "state/billing-snapshot.json", "state/quota-stop.json"}.isdisjoint(manifest["files"])
    for name in manifest["files"]:
        assert not (set(Path(name).parts) & {".git", ".venv", ".runtime", "auth.json", ".env"})


def test_build_excludes_source_credentials_configuration_and_execution_history(source_checkout, tmp_path):
    output = tmp_path / "portable"
    manifest = BUNDLE.build(source_checkout, output)
    actual = contents(output)
    data = b"\n".join(actual.values())
    for private in [b"private-fixture-owner", b"private-fixture-credential", b"private-history", b"private-billing-data"]:
        assert private not in data
    assert not any(".git" in Path(name).parts for name in actual)
    assert "config/runner.yaml" not in actual
    assert "state/quota-stop.json" not in actual
    assert json.loads(actual["state/projects.json"])["projects"] == {}
    expected_head = subprocess.run(["git", "-C", str(source_checkout), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip()
    assert manifest["source_commit"] == expected_head


def test_same_source_builds_identical_bytes_and_can_replace_a_verified_output(source_checkout, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first_manifest = BUNDLE.build(source_checkout, first)
    second_manifest = BUNDLE.build(source_checkout, second)
    before = contents(first)
    assert first_manifest == second_manifest
    assert before == contents(second)
    assert BUNDLE.build(source_checkout, first) == first_manifest
    assert contents(first) == before


@pytest.mark.parametrize("change", ["tamper", "missing", "unregistered"])
def test_verify_rejects_modified_or_incomplete_inventory(copied_bundle, change):
    controller = copied_bundle / "idea_vault_runner/controller.py"
    if change == "tamper":
        controller.write_text(controller.read_text() + "\n# unauthorized edit\n")
    elif change == "missing":
        controller.unlink()
    else:
        (copied_bundle / "unexpected.txt").write_text("unregistered")
    with pytest.raises(ValueError):
        BUNDLE.verify(copied_bundle)


@pytest.mark.parametrize("kind", ["file", "directory", "manifest", "root"])
def test_verify_rejects_symlinks(copied_bundle, tmp_path, kind):
    if kind == "root":
        alias = tmp_path / "linked-bundle"
        alias.symlink_to(copied_bundle, target_is_directory=True)
        target = alias
    elif kind == "directory":
        original = copied_bundle / "idea_vault_runner"
        outside = tmp_path / "external-package"
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
        target = copied_bundle
    else:
        original = copied_bundle / (BUNDLE.MANIFEST if kind == "manifest" else "idea_vault_runner/controller.py")
        outside = tmp_path / "external-file"
        original.rename(outside)
        original.symlink_to(outside)
        target = copied_bundle
    with pytest.raises(ValueError):
        BUNDLE.verify(target)


@pytest.mark.parametrize("unsafe", ["../escape.py", "/absolute.py", ".git/config", "auth.json", "nested/.env", "private.pem"])
def test_verify_rejects_unsafe_manifest_paths(copied_bundle, unsafe):
    path = copied_bundle / BUNDLE.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["files"][unsafe] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        BUNDLE.verify(copied_bundle)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("bundle_version", None), ("bundle_version", "unversioned"),
    ("source_commit", "not-a-commit"), ("files", {}),
])
def test_verify_rejects_ambiguous_provenance(copied_bundle, field, value):
    path = copied_bundle / BUNDLE.MANIFEST
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        BUNDLE.verify(copied_bundle)


def test_build_rejects_a_source_file_symlink(source_checkout, tmp_path):
    controller = source_checkout / "idea_vault_runner/controller.py"
    external = tmp_path / "untrusted-controller.py"
    controller.rename(external)
    controller.symlink_to(external)
    with pytest.raises(ValueError):
        BUNDLE.build(source_checkout, tmp_path / "bundle")


def test_build_rejects_output_inside_source_through_a_symlink(source_checkout, tmp_path):
    alias = tmp_path / "source-alias"
    alias.symlink_to(source_checkout, target_is_directory=True)
    with pytest.raises(ValueError):
        BUNDLE.build(source_checkout, alias / "nested-bundle")
    assert not (source_checkout / "nested-bundle").exists()


def test_build_refuses_to_replace_an_unrelated_output(source_checkout, tmp_path):
    output = tmp_path / "existing-data"
    output.mkdir()
    valuable = output / "keep.txt"
    valuable.write_text("owner's unrelated data")
    with pytest.raises((ValueError, FileNotFoundError)):
        BUNDLE.build(source_checkout, output)
    assert valuable.read_text() == "owner's unrelated data"
