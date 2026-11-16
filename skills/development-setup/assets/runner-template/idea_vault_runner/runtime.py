"""Symphony configuration and process supervision for one project at a time."""

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
import psutil

from .errors import BlockedError


def write_workflow(template, destination, project, workspace_root, codex_command):
    """Keep credentials in the host environment, never in repo-owned prompts."""
    try:
        text = Path(template).read_text(encoding="utf-8")
        match = re.fullmatch(r"---\r?\n(.*?)^---[ \t]*\r?\n?(.*)", text, re.M | re.S)
        if match is None:
            raise ValueError()
        frontmatter, prompt = match.groups()
        config = yaml.safe_load(frontmatter) or {}
        if not isinstance(config, dict) or not isinstance(config.get("codex", {}), dict):
            raise ValueError()
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        raise BlockedError("Symphony workflow is missing or invalid", "invalid_workflow") from None
    config["tracker"] = {
        "kind": "github",
        "provider": {"repo": project["repository"], "token": "$SYMPHONY_GITHUB_TOKEN"},
        "active_states": ["open"],
        "terminal_states": ["closed"],
        "required_labels": ["agent:ready"],
    }
    config["workspace"] = {"root": str(Path(workspace_root).resolve())}
    config["agent"] = {"max_concurrent_agents": 1, "max_turns": 8}
    config.setdefault("codex", {}).update({
        "command": codex_command,
        # Codex 0.169 uses granular allow-flags, not the old reject object.
        # Keep a map: Symphony 0.0.3 auto-approves requests for the string "never".
        "approval_policy": {"granular": {"sandbox_approval": False, "rules": False,
                                          "skill_approval": False, "request_permissions": False,
                                          "mcp_elicitations": False}},
        "thread_sandbox": "workspace-write",
        "turn_sandbox_policy": {"type": "workspaceWrite", "networkAccess": True},
    })
    # Clone/checkout occurs before Symphony starts. after_create only copies that
    # local checkout, so repository creation credentials never enter a hook.
    config["hooks"] = {"after_create": 'cp -a "$IDEA_VAULT_SEED/." .'}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("---\n" + yaml.safe_dump(config, sort_keys=False)
                           + "---\n" + prompt, encoding="utf-8")
    return destination


class SymphonyProcess:
    def __init__(self, command, workflow, env, log_path):
        self.command = list(command)
        self.workflow = Path(workflow)
        self.env = dict(env)
        self.log_path = Path(log_path)
        self.process = None
        self.log = None
        self.port = None
        self.deadline = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Logs are private scratch files. They are never uploaded as artifacts.
        fd = os.open(str(self.log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.log = os.fdopen(fd, "w")
        try:
            self.process = subprocess.Popen(
                self.command + [str(self.workflow), "--port", str(self.port)],
                env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError):
            self.log.close()
            raise BlockedError("Symphony could not start", "symphony_start_failed") from None

    def poll(self):
        return self.process.poll()

    def snapshot(self):
        timeout = min(2.0, max(0.01, self.deadline - time.monotonic())) if self.deadline else 2.0
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:%s/api/v1/state" % self.port, timeout=timeout
            ) as response:
                data = json.load(response)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, urllib.error.URLError):
            return {}

    def stop(self, deadline=None):
        """Stop the process group and observed descendants, including new sessions."""
        if self.process is None:
            return
        group = self.process.pid
        try:
            descendants = psutil.Process(group).children(recursive=True)
        except psutil.Error:
            descendants = []
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.Error:
                pass
        grace = min(3.0, max(0.0, deadline - time.monotonic())) if deadline else 3.0
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # A daemonized child may outlive the entry process. Kill its group too.
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        if self.log:
            self.log.close()


def shell_command(arguments):
    return " ".join(shlex.quote(str(value)) for value in arguments)
