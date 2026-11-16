"""Persistent, owner-scoped quota suspension with a GitHub Issue warning.

The issue is the durable warning. GitHub decides whether to also email the
assignee according to that person's notification settings. This module never
resumes a runner, including when a new billing cycle starts.
"""

import copy
from datetime import datetime
import json
import math
import re

from .errors import BlockedError, ConflictError, RunnerError
from .github import validate_repository


MARKER_PATH = "state/quota-stop.json"
ISSUE_MARKER = "<!-- idea-vault-quota-stop-v1 -->"
REASON = "billing_quota_threshold"
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_EVIDENCE_FIELDS = {
    "owner", "remaining_minutes", "threshold_minutes", "compute_remaining_usd",
    "stop_compute_usd", "checked_at", "billing_period", "evidence_source",
}


def _number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= minimum
    except OverflowError:
        return False


def _checked_at(value):
    try:
        if not isinstance(value, str):
            return False
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


class QuotaStop:
    def __init__(self, github, owner, repository, notification_github=None):
        self.repository = validate_repository(repository)
        if (not isinstance(owner, str) or not _LOGIN.fullmatch(owner)
                or repository.split("/", 1)[0].casefold() != owner.casefold()):
            raise BlockedError("Quota suspension requires the configured owner's repository.",
                               "owner_mismatch")
        self.github = github
        self.owner = owner
        self.notification_github = notification_github or github

    def _validate_target(self):
        meta = self.github.repository(self.repository)
        owner = meta.get("owner") if isinstance(meta, dict) else None
        if (not isinstance(meta, dict) or meta.get("private") is not True
                or not isinstance(owner, dict)
                or str(owner.get("login", "")).casefold() != self.owner.casefold()
                or (meta.get("full_name") and str(meta["full_name"]).casefold() != self.repository.casefold())):
            raise BlockedError("Quota suspension requires a private repository owned by the configured user.",
                               "private_repository_required")

    def _evidence(self, value):
        if not isinstance(value, dict):
            raise BlockedError("Quota evidence is invalid.", "quota_evidence_invalid")
        evidence = {key: copy.deepcopy(value[key]) for key in _EVIDENCE_FIELDS if key in value}
        if (str(evidence.get("owner", "")).casefold() != self.owner.casefold()
                or not _number(evidence.get("remaining_minutes"))
                or not _number(evidence.get("threshold_minutes"), 500)
                or evidence["remaining_minutes"] > evidence["threshold_minutes"]
                or not _checked_at(evidence.get("checked_at"))):
            raise BlockedError("Quota evidence does not establish the owner's stop threshold.",
                               "quota_evidence_invalid")
        for field in ("compute_remaining_usd", "stop_compute_usd"):
            if field in evidence and not _number(evidence[field]):
                raise BlockedError("Quota evidence contains an invalid amount.", "quota_evidence_invalid")
        period = evidence.get("billing_period")
        if period is not None and (not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}", period)):
            raise BlockedError("Quota evidence contains an invalid billing period.", "quota_evidence_invalid")
        source = evidence.get("evidence_source")
        if source is not None and source != "owner-confirmed-snapshot-and-github-account-usage":
            raise BlockedError("Quota evidence contains an unrecognized source.", "quota_evidence_invalid")
        return evidence

    def _validate_marker(self, value):
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1
                or value.get("stopped") is not True or value.get("reason") != REASON
                or str(value.get("owner", "")).casefold() != self.owner.casefold()
                or str(value.get("repository", "")).casefold() != self.repository.casefold()):
            raise BlockedError("Quota suspension marker is invalid; execution remains blocked.",
                               "quota_stop_invalid")
        self._evidence(value.get("evidence"))
        notice = value.get("notification")
        if not isinstance(notice, dict) or notice.get("status") not in ("pending", "sent"):
            raise BlockedError("Quota notification state is invalid; execution remains blocked.",
                               "quota_stop_invalid")
        if notice["status"] == "sent":
            self._issue_reference(notice)
        return value

    def _read_marker(self, ref=None):
        try:
            content, _ = self.github.read_file(self.repository, MARKER_PATH, ref=ref)
        except RunnerError as error:
            if error.code == "not_found":
                return None
            raise
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            raise BlockedError("Quota suspension marker is unreadable; execution remains blocked.",
                               "quota_stop_invalid") from None
        return self._validate_marker(value)

    def status(self):
        """Read the validated, durable suspension without changing remote state."""
        self._validate_target()
        branch = self.github.default_branch(self.repository)
        # A readable branch reference proves Contents-read access before a
        # marker 404 is interpreted as absence rather than unknown permissions.
        head = self.github.head(self.repository, branch)
        return self._read_marker(ref=head)

    def assert_not_stopped(self):
        """Fail closed on a latched suspension, even after allowance recovery."""
        marker = self.status()
        if marker is not None:
            raise BlockedError("Automatic development is suspended by the quota stop. Resumption is not implemented.",
                               "billing_quota_stopped")

    def _disable(self):
        endpoint = "/repos/%s/actions/variables/RUNNER_ENABLED" % self.repository
        value = {"name": "RUNNER_ENABLED", "value": "false"}
        try:
            self.github.api(endpoint, method="PATCH", data=value)
        except RunnerError as error:
            if error.code != "not_found":
                raise
            self.github.api("/repos/%s/actions/variables" % self.repository, method="POST", data=value)

    @staticmethod
    def _merge_marker(current, candidate):
        if current is None:
            return copy.deepcopy(candidate)
        # The first stop remains authoritative across month boundaries. A
        # notification already sent must not become pending after a retry.
        result = copy.deepcopy(current)
        if candidate["notification"]["status"] == "sent":
            result["notification"] = copy.deepcopy(candidate["notification"])
        result["runner_enabled_disabled"] = bool(current.get("runner_enabled_disabled")) or bool(
            candidate.get("runner_enabled_disabled"))
        return result

    def _save_marker(self, candidate):
        branch = self.github.default_branch(self.repository)
        for attempt in range(3):
            head = self.github.head(self.repository, branch)
            current = self._read_marker(ref=head)
            merged = self._merge_marker(current, candidate)
            if merged == current:
                return merged
            try:
                self.github.write_files(
                    self.repository,
                    {MARKER_PATH: json.dumps(merged, ensure_ascii=False, indent=2) + "\n"},
                    "Suspend Idea Vault at the free quota threshold",
                    branch=branch, expected_head=head,
                )
                return merged
            except ConflictError:
                if attempt == 2:
                    raise
        raise ConflictError("Quota suspension could not be saved.", "conflict")

    def _issue_reference(self, value):
        number = value.get("issue_number", value.get("number"))
        url = value.get("issue_url", value.get("html_url"))
        if (type(number) is not int or number <= 0
                or url != "https://github.com/%s/issues/%s" % (self.repository, number)):
            raise BlockedError("GitHub did not confirm the quota warning location.", "quota_notice_invalid")
        return {"status": "sent", "issue_number": number, "issue_url": url}

    def _notice(self, marker, stopped):
        old = marker["notification"]
        if old["status"] == "sent":
            return self._issue_reference(old)
        # Include closed issues: closing a warning must not create a fresh alert
        # or clear the persistent suspension. Refuse to guess after bounded paging.
        for page in range(1, 11):
            issues = self.notification_github.api(
                "/repos/%s/issues?state=all&per_page=100&page=%s" % (self.repository, page))
            if not isinstance(issues, list):
                raise BlockedError("Existing quota warnings cannot be verified.", "quota_notice_unverified")
            for issue in issues:
                if (isinstance(issue, dict) and "pull_request" not in issue
                        and ISSUE_MARKER in str(issue.get("body", ""))):
                    return self._issue_reference(issue)
            if len(issues) < 100:
                break
        else:
            raise BlockedError("Existing quota warning search exceeded its limit.", "quota_notice_unverified")
        evidence = marker["evidence"]
        outcome = ("制御プログラムは、自動開発の停止状態を保存しました。" if stopped else
                   "停止状態の保存に失敗しました。実行設定の確認が必要です。")
        body = (
            "%s\n\n@%s\n\n"
            "あなたの無料計算枠の残りは、標準Linux換算で **%s分** です。\n"
            "制御プログラムは、停止閾値 **%s分以下** への到達を検出しました。\n\n"
            "%s\n\n"
            "請求期間の更新や残枠の回復だけでは、自動開発は再開しません。\n"
            "利用再開方法は未定です。\n\n"
            "確認日時: `%s`\n"
        ) % (ISSUE_MARKER, self.owner, format(evidence["remaining_minutes"], ".2f"),
             format(evidence["threshold_minutes"], ".0f"), outcome, evidence["checked_at"])
        issue = self.notification_github.api(
            "/repos/%s/issues" % self.repository, method="POST",
            data={"title": "Idea Vault：無料枠の残りが停止閾値に達しました", "body": body,
                  "assignees": [self.owner]},
        )
        if not isinstance(issue, dict):
            raise BlockedError("GitHub did not confirm the quota warning.", "quota_notice_unverified")
        return self._issue_reference(issue)

    @staticmethod
    def _failure(error):
        # Never include exception messages or API payloads in persisted output.
        code = getattr(error, "code", "quota_operation_failed")
        return code if isinstance(code, str) and re.fullmatch(r"[a-z_0-9]{1,80}", code) else "quota_operation_failed"

    def stop(self, evidence):
        """Attempt every stop channel before warning, without any resume path."""
        self._validate_target()
        evidence = self._evidence(evidence)
        result = {"status": "stop_pending", "variable_disabled": False, "marker_saved": False,
                  "notification_sent": False, "issue_url": None, "failures": {}}
        marker = {"schema_version": 1, "stopped": True, "reason": REASON,
                  "owner": self.owner, "repository": self.repository,
                  "first_stopped_at": evidence["checked_at"], "evidence": evidence,
                  "runner_enabled_disabled": False, "notification": {"status": "pending"}}
        try:
            self._disable()
            result["variable_disabled"] = True
            marker["runner_enabled_disabled"] = True
        except Exception as error:
            result["failures"]["disable_variable"] = self._failure(error)
        try:
            marker = self._save_marker(marker)
            result["marker_saved"] = True
        except Exception as error:
            result["failures"]["save_marker"] = self._failure(error)
        stopped = result["variable_disabled"] or result["marker_saved"]
        result["status"] = "stopped" if stopped else "stop_pending"
        try:
            marker["notification"] = self._notice(marker, stopped)
            result["notification_sent"] = True
            result["issue_url"] = marker["notification"]["issue_url"]
        except Exception as error:
            result["failures"]["notification"] = self._failure(error)
        if result["notification_sent"]:
            try:
                self._save_marker(marker)
                result["marker_saved"] = True
                result["status"] = "stopped"
                result["failures"].pop("save_marker", None)
            except Exception as error:
                result["failures"]["save_notification"] = self._failure(error)
        return result
