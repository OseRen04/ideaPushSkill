"""Versioned project state, saved with an optimistic branch lease."""

import json

from .errors import BlockedError, RunnerError


def _check_state(value):
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("projects"), dict)):
        raise BlockedError("Invalid runner state schema.", "invalid_state")
    forbidden = {"access_token", "refresh_token", "id_token", "tokens", "auth_json", "private_key"}

    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in forbidden:
                    raise BlockedError("Credentials must not enter project state.", "secret_in_state")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)


class ProjectStore:
    def __init__(self, github, repository, path="state/projects.json"):
        self.github = github
        self.repository = repository
        self.path = path
        self._head = None
        self._branch = None

    def load(self):
        self._branch = self.github.default_branch(self.repository)
        self._head = self.github.head(self.repository, self._branch)
        try:
            text, _ = self.github.read_file(self.repository, self.path, ref=self._head)
        except RunnerError as exc:
            if exc.code == "not_found":
                return {"version": 1, "projects": {}, "last_run": None}
            raise
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            raise BlockedError("Runner state is not valid JSON.", "invalid_state") from None
        _check_state(value)
        return value

    def save(self, value):
        _check_state(value)
        if self._head is None:
            self.load()
        commit = self.github.write_files(
            self.repository, {self.path: json.dumps(value, ensure_ascii=False, indent=2) + "\n"},
            "Update Idea Vault execution state", branch=self._branch, expected_head=self._head)
        self._head = commit
        return commit
