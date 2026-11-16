"""Restore managed Codex auth in a dedicated, disposable CODEX_HOME."""

import hashlib
import json
import os
from pathlib import Path

from .errors import BlockedError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _managed_auth(raw):
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.get("auth_mode") != "chatgpt":
            raise ValueError()
        if value.get("OPENAI_API_KEY"):
            raise ValueError()
        tokens = value.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError()
        for key in ("access_token", "refresh_token"):
            if not isinstance(tokens.get(key), str) or not tokens[key].strip():
                raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise BlockedError("Managed ChatGPT authentication is missing or invalid.", "codex_auth_invalid") from None


class AuthStore:
    """Own only auth.json/config.toml created by this instance, never user auth."""

    def __init__(self, github, repo, codex_home, secret_name="CODEX_AUTH_JSON"):
        self.github = github
        self.repo = repo
        self.codex_home = Path(codex_home)
        self.secret_name = secret_name
        self._owned = set()
        self._created_home = False
        self._digest = None

    def restore(self, raw):
        value = _managed_auth(raw)
        home = self.codex_home
        # A per-run directory avoids clobbering local credentials or configuration.
        if home.is_symlink() or (home.exists() and (not home.is_dir() or any(home.iterdir()))):
            raise BlockedError("CODEX_HOME must be an empty dedicated directory.", "codex_home_not_empty")
        try:
            if not home.exists():
                home.mkdir(parents=True, mode=0o700)
                self._created_home = True
            home.chmod(0o700)
            normalized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            for name, content in (
                ("auth.json", normalized),
                ("config.toml", 'cli_auth_credentials_store = "file"\n'),
            ):
                target = home / name
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._owned.add(target)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.write(content)
            self._digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        except OSError:
            self.cleanup()
            raise BlockedError("Cannot restore managed authentication.", "codex_auth_restore_failed") from None

    def persist(self):
        target = self.codex_home / "auth.json"
        if self._digest is None or target not in self._owned or target.is_symlink():
            raise BlockedError("Managed authentication is not initialized.", "codex_auth_not_initialized")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(target), flags)
            with os.fdopen(fd, "r", encoding="utf-8") as current:
                raw = current.read(1024 * 1024 + 1)
            target.chmod(0o600)
        except (OSError, UnicodeError):
            raise BlockedError("Cannot read updated authentication.", "codex_auth_read_failed") from None
        value = _managed_auth(raw)
        normalized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest == self._digest:
            return False
        try:
            self.github.secret_set(self.repo, self.secret_name, normalized)
        except Exception:
            # CLI stderr or mock/provider exceptions can contain credential values.
            raise BlockedError("Cannot persist refreshed authentication.", "codex_auth_writeback_failed") from None
        self._digest = digest
        return True

    def cleanup(self):
        for target in tuple(self._owned):
            try:
                target.unlink(missing_ok=True)
                self._owned.discard(target)
            except OSError:
                pass
        if self._created_home:
            try:
                self.codex_home.rmdir()
            except OSError:
                pass
        self._digest = None


def clean_child_env(env, development_token=None):
    """Give Symphony a small allowlist, never the controller's admin credential."""
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TMPDIR", "TMP", "TEMP", "TZ", "TERM", "CI", "CODEX_HOME", "CODEX_BIN",
        "SYMPHONY_BIN", "SYMPHONY_WORKSPACE_ROOT", "SOURCE_REPO_URL", "TARGET_REPO",
        "TARGET_BRANCH", "TARGET_ISSUE", "IDEA_VAULT_DEADLINE", "IDEA_VAULT_WORK_DEADLINE",
        "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "RUNNER_TRACKING_ID", "GITHUB_ACTIONS",
    }
    result = {key: value for key, value in env.items() if key in allowed and isinstance(value, str)}
    if development_token:
        result["SYMPHONY_GITHUB_TOKEN"] = development_token
    return result
