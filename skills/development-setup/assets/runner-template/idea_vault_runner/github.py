"""Small, credential-safe GitHub CLI adapter with non-forced Git commits."""

import base64
import json
import os
import re
import signal
import subprocess
import time
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from .errors import BlockedError, ConflictError, RunnerError


_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def validate_repository(value):
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise BlockedError("Invalid GitHub repository name.", "invalid_repository")
    if any(part in {".", ".."} for part in value.split("/")):
        raise BlockedError("Invalid GitHub repository name.", "invalid_repository")
    return value


def _path(value):
    if not isinstance(value, str):
        raise BlockedError("Invalid repository file path.", "invalid_path")
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value
            or path.is_absolute() or any(part in {".", "..", ".git"} for part in value.split("/"))):
        raise BlockedError("Invalid repository file path.", "invalid_path")
    return quote(value, safe="/")


class GitHub:
    def __init__(self, token=None, timeout=30):
        self.token = token
        self.timeout = timeout
        self.deadline = None

    def _run(self, args, value=None):
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise BlockedError("GitHub operation deadline reached.", "deadline")
            timeout = min(timeout, remaining)
        env = os.environ.copy()
        env.pop("GH_DEBUG", None)
        env["GH_HOST"] = "github.com"
        env["GH_PROMPT_DISABLED"] = "1"
        if self.token:
            env["GH_TOKEN"] = self.token
            env.pop("GITHUB_TOKEN", None)
        process = None
        try:
            process = subprocess.Popen(["gh"] + args, text=True, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            stdout, stderr = process.communicate(input=value, timeout=timeout)
            result = subprocess.CompletedProcess(["gh"] + args, process.returncode, stdout, stderr)
        except FileNotFoundError:
            raise BlockedError("GitHub CLI is not installed.", "github_cli_missing") from None
        except subprocess.TimeoutExpired:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise RunnerError("GitHub operation timed out; check remote state before retrying.",
                              "github_timeout") from None
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise
        if result.returncode:
            # CLI output can include request values. Never include it in a public error.
            match = re.search(r"(?:HTTP[ /]|status[: ]+)(\d{3})", result.stderr, re.I)
            status = int(match.group(1)) if match else None
            if status == 404:
                raise RunnerError("GitHub resource was not found or is inaccessible.", "not_found")
            if status in {409, 412}:
                raise ConflictError("GitHub rejected a conflicting update.", "conflict")
            raise RunnerError("GitHub operation failed" + (" (HTTP %s)." % status if status else "."),
                              "github_http_%s" % status if status else "github_error")
        return result.stdout

    def api(self, path, method="GET", data=None):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise BlockedError("GitHub API paths must be relative to github.com.", "invalid_api_path")
        if "\x00" in path or "\n" in path or "\r" in path or "://" in path:
            raise BlockedError("Invalid GitHub API path.", "invalid_api_path")
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise BlockedError("Unsupported GitHub API method.", "invalid_api_method")
        args = ["api", "--hostname", "github.com", "--method", method,
                "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2022-11-28", path]
        value = None
        if data is not None:
            args += ["--input", "-"]
            value = json.dumps(data, ensure_ascii=False)
        output = self._run(args, value)
        if not output.strip():
            return None
        try:
            return json.loads(output)
        except (ValueError, TypeError):
            raise RunnerError("GitHub returned an invalid JSON response.", "github_response") from None

    def repository(self, repository):
        return self.api("/repos/" + validate_repository(repository))

    def default_branch(self, repository):
        return self.repository(repository)["default_branch"]

    def head(self, repository, branch):
        result = self.api("/repos/%s/git/ref/heads/%s" % (
            validate_repository(repository), quote(branch, safe="")))
        return result["object"]["sha"]

    def read_file(self, repository, path, ref=None):
        endpoint = "/repos/%s/contents/%s" % (validate_repository(repository), _path(path))
        if ref:
            endpoint += "?" + urlencode({"ref": ref})
        item = self.api(endpoint)
        if not isinstance(item, dict) or item.get("type") != "file":
            raise BlockedError("Expected an ordinary repository file.", "invalid_file")
        blob = item
        if item.get("encoding") != "base64":
            blob = self.api("/repos/%s/git/blobs/%s" % (repository, item["sha"]))
        if blob.get("encoding") != "base64":
            raise RunnerError("Unsupported GitHub file encoding.", "github_response")
        try:
            content = base64.b64decode(blob.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise RunnerError("Repository file is not valid UTF-8.", "invalid_file") from None
        return content, item["sha"]

    def write_files(self, repository, files, message, branch=None, expected_head=None):
        validate_repository(repository)
        if not files:
            raise BlockedError("No repository files were supplied.", "invalid_files")
        branch = branch or self.default_branch(repository)
        head = self.head(repository, branch)
        if expected_head is not None and head != expected_head:
            raise ConflictError("Repository changed since it was read.", "conflict")
        changes = {}
        for path, text in files.items():
            _path(path)
            if not isinstance(text, str):
                raise BlockedError("Repository writes require UTF-8 text.", "invalid_files")
            try:
                current, _ = self.read_file(repository, path, ref=head)
            except RunnerError as exc:
                if exc.code != "not_found":
                    raise
                current = None
            if text != current:
                changes[path] = text
        if not changes:
            return head
        commit = self.api("/repos/%s/git/commits/%s" % (repository, head))
        entries = []
        for path, text in changes.items():
            blob = self.api("/repos/%s/git/blobs" % repository, "POST",
                            {"content": text, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self.api("/repos/%s/git/trees" % repository, "POST",
                        {"base_tree": commit["tree"]["sha"], "tree": entries})
        created = self.api("/repos/%s/git/commits" % repository, "POST",
                           {"message": message, "tree": tree["sha"], "parents": [head]})
        try:
            moved = self.api("/repos/%s/git/refs/heads/%s" % (repository, quote(branch, safe="")),
                             "PATCH", {"sha": created["sha"], "force": False})
        except RunnerError as exc:
            if exc.code in {"conflict", "github_http_422"}:
                raise ConflictError("GitHub rejected a concurrent branch update.", "conflict") from None
            raise
        if not isinstance(moved, dict) or moved.get("object", {}).get("sha") != created["sha"]:
            raise RunnerError("GitHub did not confirm the branch update.", "github_result_unknown")
        return created["sha"]

    def secret_set(self, repository, name, value):
        validate_repository(repository)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            raise BlockedError("Invalid Actions secret name.", "invalid_secret_name")
        self._run(["secret", "set", name, "--repo", repository, "--app", "actions"], value)

    def issue_comment(self, repository, number, body):
        return self.api("/repos/%s/issues/%d/comments" % (
            validate_repository(repository), int(number)), "POST", {"body": body})
