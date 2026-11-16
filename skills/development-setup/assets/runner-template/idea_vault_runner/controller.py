"""One ephemeral Actions job coordinates a bounded, serialized development run."""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

from .bootstrap import SOURCE_DOCUMENT_NAMES
from .errors import BlockedError, QuotaThresholdReached, RunnerError
from .runtime import SymphonyProcess, write_workflow
from .schedule import Schedule, cron_entries, execution_window, load_schedule
from .selector import parse_idea, read_ideas, select_candidates

UTC = dt.timezone.utc
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class DeadlineExpired(BaseException):
    """Interrupt all controller work once the hard wall-clock deadline arrives."""


def utcnow():
    return dt.datetime.now(UTC)


def timestamp():
    return utcnow().isoformat()


def read_source_documents(source, idea_id):
    """Read optional documents from the same checkout used to select the idea."""
    if not isinstance(idea_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", idea_id):
        raise BlockedError("Invalid idea identifier.", "invalid_idea_id")
    folder_fd = None
    try:
        # Relative descriptor reads keep fixed filenames inside the selected
        # folder. Do not follow links or block while opening a named pipe.
        folder_fd = os.open(Path(source) / idea_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        documents = {}
        for name in SOURCE_DOCUMENT_NAMES:
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=folder_fd)
            except FileNotFoundError:
                continue
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise BlockedError("Optional project document is not an ordinary file.",
                                       "invalid_source_document")
                with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
                    descriptor = None
                    documents[name] = handle.read()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return documents
    except (OSError, UnicodeError):
        raise BlockedError("Optional project documents could not be read safely.",
                           "source_document_unreadable") from None
    finally:
        if folder_fd is not None:
            os.close(folder_fd)


def load_config(path):
    path = Path(path).resolve()
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        raise BlockedError("Runner configuration is unavailable", "invalid_config") from None
    if not isinstance(value, dict):
        raise BlockedError("Runner configuration must be a mapping", "invalid_config")
    for key in ("ideas_repository", "runner_repository"):
        if not isinstance(value.get(key), str) or not REPOSITORY.fullmatch(value[key]):
            raise BlockedError("A repository setting is invalid", "invalid_config")
        if value[key].split("/")[0].casefold() != str(value.get("owner", "")).casefold():
            raise BlockedError("All repositories must belong to the configured owner", "owner_mismatch")
    if value["ideas_repository"].casefold() == value["runner_repository"].casefold():
        raise BlockedError("Ideas and private execution repositories must be distinct", "invalid_config")
    for key, default in (("checkpoint_interval_seconds", 60), ("poll_interval_seconds", 10),
                         ("max_projects_per_run", 5)):
        item = value.get(key, default)
        if type(item) is not int or not 1 <= item <= 3600:
            raise BlockedError("A runtime limit is invalid", "invalid_config")
        value[key] = item
    retries = value.get("max_retries", 2)
    if type(retries) is not int or not 0 <= retries <= 5:
        raise BlockedError("The retry limit is invalid", "invalid_config")
    value["max_retries"] = retries
    command = value.get("symphony_command", ["symphony"])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise BlockedError("Symphony command must be an argument list", "invalid_config")
    value["symphony_command"] = command
    value["_base"] = str(path.parent)
    value["runtime_directory"] = str((path.parent / value.get("runtime_directory", "../.runtime")).resolve())
    value["workflow_template"] = str((path.parent / value.get("workflow_template", "WORKFLOW.md")).resolve())
    billing = dict(value.get("billing") or {})
    if billing.get("snapshot_path"):
        billing["snapshot_path"] = str((path.parent / billing["snapshot_path"]).resolve())
    value["billing"] = billing
    return value


@contextmanager
def local_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "controller.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockedError("Another local controller is running", "already_running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def git_clone(repository, branch, destination, token, timeout=30):
    if not REPOSITORY.fullmatch(repository):
        raise BlockedError("Invalid clone repository", "invalid_repository")
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c",
               "credential.helper=!gh auth git-credential", "clone", "--quiet"]
    if branch:
        command += ["--branch", branch, "--single-branch"]
    command += ["https://github.com/" + repository + ".git", str(destination)]
    env = dict(os.environ, GH_TOKEN=token or "", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=max(0.1, timeout))
    except (OSError, subprocess.TimeoutExpired):
        raise BlockedError("Git checkout timed out or could not start", "checkout_failed") from None
    if result.returncode:
        raise BlockedError("Git checkout failed; check repository permissions", "checkout_failed")


def remaining(deadline):
    return max(0.0, deadline - time.monotonic())


def active_issue_state(github, project):
    issue = github.api("/repos/%s/issues/%s" % (project["repository"], project["issue_number"]))
    labels = {item["name"].lower() if isinstance(item, dict) else str(item).lower()
              for item in issue.get("labels", [])}
    if "agent:blocked" in labels:
        return "blocked"
    if issue.get("state") == "closed" or "agent:review" in labels or "agent:ready" not in labels:
        return "review"
    return "running"


def issue_note(github, project, state, note):
    body = "### Idea Vault checkpoint\n\nStatus: `%s`\n\n%s\n" % (state, note)
    github.issue_comment(project["repository"], project["issue_number"], body)


class Controller:
    def __init__(self, config, github, development_github, auth, billing, store,
                 process_factory=SymphonyProcess, clone=git_clone, checkpoint=None, quota_stop=None):
        self.config = config
        self.github = github
        self.development_github = development_github
        self.auth = auth
        self.billing = billing
        self.store = store
        self.process_factory = process_factory
        self.clone = clone
        self.checkpoint = checkpoint
        self.stop_requested = False
        self.process = None
        self.state = None
        self.deadline = None
        self.quota_stop = quota_stop
        self.quota_error = None

    def check_billing(self, required_minutes):
        try:
            return self.billing.check(utcnow(), required_minutes)
        except QuotaThresholdReached as error:
            self.quota_error = error
            self.stop_requested = True
            if self.process is not None:
                self.process.stop(deadline=self.deadline)
            raise

    def request_stop(self, *_):
        self.stop_requested = True
        # Alarm enforcement does not wait for HTTP, Git, tests or cleanup.
        if self.process is not None:
            self.process.stop(deadline=self.deadline)
        if _ and _[0] == signal.SIGALRM:
            raise DeadlineExpired()

    def bounded_clients(self):
        if self.deadline is not None:
            budget = max(0.1, min(20.0, remaining(self.deadline)))
            self.github.timeout = budget
            self.development_github.timeout = budget
            self.github.deadline = self.deadline
            self.development_github.deadline = self.deadline

    def save_state(self, project_id=None, project=None, status=None, reason=None):
        if project_id is not None:
            self.state["projects"][project_id] = dict(project)
        self.state["last_run"] = {
            "updated_at": timestamp(), "status": status or "running",
            "reason": reason, "run_url": os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            + "/" + self.config["runner_repository"] + "/actions/runs/"
            + os.environ.get("GITHUB_RUN_ID", "local"),
        }
        self.bounded_clients()
        self.store.save(self.state)

    def source_snapshot(self):
        folder = Path(self.config["runtime_directory"]) / "ideas"
        if folder.exists():
            shutil.rmtree(folder)
        self.clone(self.config["ideas_repository"], None, folder,
                   self.github.token, timeout=min(30.0, remaining(self.deadline)) if self.deadline else 30)
        return folder

    def latest_settings_and_permission(self, idea):
        content, _ = self.github.read_file(self.config["ideas_repository"],
                                           self.config["schedule_path"])
        # Reuse the strict duplicate-key loader for live edits too.
        path = Path(self.config["runtime_directory"]) / "latest-schedule.yaml"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content, encoding="utf-8")
        latest = load_schedule(path)
        window = execution_window(latest, utcnow(), self.started_at, require_work_time=False)
        if window is None:
            self.deadline = min(self.deadline, time.monotonic() + (300 if not latest.enabled else 0))
            signal.setitimer(signal.ITIMER_REAL, max(0.001, remaining(self.deadline)))
            return None
        # A settings edit may shorten a run, never extend the current deadline.
        self.deadline = min(self.deadline, time.monotonic()
                            + max(0, (window.deadline - utcnow()).total_seconds()))
        signal.setitimer(signal.ITIMER_REAL, max(0.001, remaining(self.deadline)))
        if remaining(self.deadline) <= 300:
            return None
        content, _ = self.github.read_file(self.config["ideas_repository"], idea.id + "/idea.md")
        latest_idea = parse_idea(idea.id, content)
        return window if latest_idea.development == "allowed" and latest_idea.status in ("構想", "開発中") else None

    def checkpoint_project(self, root, project, status, note):
        self.bounded_clients()
        # Symphony creates one workspace per issue. A single-agent workflow must
        # never checkpoint two unrelated repositories from the workspace root.
        candidates = [p for p in Path(root).iterdir()
                      if not p.is_symlink() and p.is_dir() and (p / ".git").exists()]
        if len(candidates) > 1:
            raise BlockedError("Multiple workspaces need manual review", "multiple_workspaces")
        if candidates and self.checkpoint:
            result = self.checkpoint(candidates[0], project, self.development_github,
                                     note=note, deadline=self.deadline)
            project["last_commit"] = result.get("commit")
        project["status"] = status
        project["updated_at"] = timestamp()

    def run_project(self, idea, project, window, raw_auth):
        from .auth import clean_child_env

        runtime = Path(self.config["runtime_directory"]) / idea.id
        if runtime.exists():
            shutil.rmtree(runtime)
        seed = runtime / "seed"
        workspaces = runtime / "workspaces"
        workspaces.mkdir(parents=True, mode=0o700)
        self.clone(project["repository"], project["branch"], seed,
                   self.development_github.token, timeout=min(30.0, remaining(self.deadline)))
        workflow = write_workflow(self.config["workflow_template"], runtime / "WORKFLOW.md",
                                   project, workspaces, self.config.get("codex_command", "codex app-server"))
        env = clean_child_env(os.environ, development_token=self.development_github.token)
        env["CODEX_HOME"] = str(self.auth.codex_home)
        env["IDEA_VAULT_SEED"] = str(seed)
        project["status"] = "running"
        self.save_state(idea.id, project)
        self.process = self.process_factory(self.config["symphony_command"], workflow, env,
                                             runtime / "symphony.log")
        self.process.deadline = self.deadline
        next_checkpoint = time.monotonic() + self.config["checkpoint_interval_seconds"]
        self.process.start()
        status, reason = "paused", "time_window_ended"
        failure = None
        try:
            while not self.stop_requested and remaining(self.deadline) > 300:
                self.bounded_clients()
                if self.latest_settings_and_permission(idea) is None:
                    reason = "schedule_disabled_or_permission_revoked"
                    break
                state = active_issue_state(self.development_github, project)
                if state != "running":
                    status, reason = state, "issue_" + state
                    break
                snapshot = self.process.snapshot()
                if snapshot.get("blocked") or snapshot.get("counts", {}).get("blocked", 0):
                    status, reason = "blocked", "operator_input_required"
                    break
                if self.process.poll() is not None:
                    raise BlockedError("Symphony exited before review", "symphony_exited")
                if time.monotonic() >= next_checkpoint:
                    self.check_billing(max(0, remaining(self.deadline) / 60))
                    self.checkpoint_project(workspaces, project, "running", "Periodic checkpoint")
                    self.auth.persist()
                    self.save_state(idea.id, project)
                    next_checkpoint = time.monotonic() + self.config["checkpoint_interval_seconds"]
                time.sleep(min(self.config["poll_interval_seconds"], max(0.01, remaining(self.deadline) - 300)))
        except RunnerError as error:
            failure = error
            status, reason = "blocked", error.code
        finally:
            # Stop edits before committing a final workspace snapshot.
            self.process.stop(deadline=self.deadline)
            self.process = None
        if self.stop_requested and not isinstance(failure, QuotaThresholdReached):
            reason = "stop_requested"
        for operation in (
            lambda: self.checkpoint_project(workspaces, project, status, reason),
            self.auth.persist,
        ):
            try:
                if remaining(self.deadline) > 0:
                    self.bounded_clients()
                    operation()
            except RunnerError as error:
                failure = failure or error
                status, reason = "blocked", failure.code
        note = ("Reason: `%s`. Work is saved on branch `%s`. Quota-stop recovery is not implemented."
                if isinstance(failure, QuotaThresholdReached) else
                "Reason: `%s`. Resume from branch `%s` and this issue.")
        issue_note(self.development_github, project, status, note % (reason, project["branch"]))
        if status == "blocked":
            issue = self.development_github.api("/repos/%s/issues/%s" % (project["repository"], project["issue_number"]))
            labels = [x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])]
            labels = [x for x in labels if x.lower() != "agent:ready"]
            self.development_github.api("/repos/%s/issues/%s" % (project["repository"], project["issue_number"]),
                                       method="PATCH", data={"labels": list(set(labels + ["agent:blocked"]))})
        self.save_state(idea.id, project, status=status, reason=reason)
        if failure:
            raise failure
        return status

    def run(self, raw_auth):
        try:
            if self.quota_stop is not None:
                self.quota_stop.assert_not_stopped()
            return self._run(raw_auth)
        finally:
            # Save worker checkpoints before committing the separate quota-stop
            # marker, so ProjectStore's optimistic branch lease stays valid.
            # A checkpoint/cleanup failure must never swallow a quota stop.
            if self.quota_error is not None:
                self.quota_error.stop_result = record_quota_stop(self.quota_stop, self.quota_error)
                raise self.quota_error

    def _run(self, raw_auth):
        from .bootstrap import prepare_project

        try:
            self.started_at = dt.datetime.fromisoformat(os.environ["RUNNER_JOB_STARTED_AT"].replace("Z", "+00:00"))
            if self.started_at.tzinfo is None or self.started_at > utcnow():
                raise ValueError()
        except KeyError:
            self.started_at = utcnow()
        except ValueError:
            raise BlockedError("Job start timestamp is invalid", "invalid_start_time") from None
        meta = self.github.repository(self.config["runner_repository"])
        if meta.get("private") is not True or meta.get("owner", {}).get("login", "").casefold() != self.config["owner"].casefold():
            raise BlockedError("Execution repository must be private and owned by you", "private_repository_required")
        source = self.source_snapshot()
        schedule = load_schedule(source / self.config["schedule_path"])
        self.state = self.store.load()
        window = execution_window(schedule, utcnow(), self.started_at)
        if window is None:
            self.save_state(status="skipped", reason="disabled_or_outside_time_window")
            return {"status": "skipped", "reason": "disabled_or_outside_time_window"}
        # Hosted Actions jobs have a six-hour ceiling. Leave room for platform
        # teardown without interpreting the configured maximum as a minimum.
        backend_end = self.started_at + dt.timedelta(minutes=350)
        self.deadline = time.monotonic() + max(0, (min(window.deadline, backend_end) - utcnow()).total_seconds())
        old_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)}
        for sig in old_handlers:
            signal.signal(sig, self.request_stop)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, remaining(self.deadline)))
        restored = False
        try:
            self.bounded_clients()
            self.check_billing(remaining(self.deadline) / 60)
            self.auth.restore(raw_auth)
            restored = True
            ideas = select_candidates(read_ideas(source), self.state["projects"])
            processed = []
            for idea in ideas[:self.config["max_projects_per_run"]]:
                if self.stop_requested or remaining(self.deadline) <= 300:
                    break
                existing = self.state["projects"].get(idea.id, {})
                if existing.get("attempts", 0) > self.config["max_retries"]:
                    continue
                if self.latest_settings_and_permission(idea) is None:
                    break
                self.check_billing(remaining(self.deadline) / 60)
                project = dict(existing)
                try:
                    project = prepare_project(self.github, self.config["owner"], idea,
                                              existing=existing, prefix=self.config.get("repository_prefix", "idea-"),
                                              source_repository=self.config["ideas_repository"],
                                              source_documents=read_source_documents(source, idea.id))
                    status = self.run_project(idea, project, window, raw_auth)
                    processed.append({"idea": idea.id, "status": status})
                except QuotaThresholdReached:
                    raise
                except RunnerError as error:
                    attempts = project.get("attempts", 0) + 1
                    retryable = error.code in {
                        "github_timeout", "github_error", "github_http_429", "github_http_500",
                        "github_http_502", "github_http_503", "github_http_504", "checkout_failed",
                    }
                    state = "failed" if retryable and attempts <= self.config["max_retries"] else "blocked"
                    project.update(status=state, reason=error.code, updated_at=timestamp(), attempts=attempts)
                    if remaining(self.deadline) > 0:
                        self.save_state(idea.id, project, status="blocked", reason=error.code)
                    processed.append({"idea": idea.id, "status": state, "reason": error.code})
                    # Fail closed on infrastructure/auth/storage problems.
                    break
            blocked = any(item["status"] in ("failed", "blocked") for item in processed)
            result = {"status": "blocked" if blocked else "finished", "projects": processed}
            if remaining(self.deadline) > 0:
                self.save_state(status=result["status"], reason="project_blocked" if blocked else "bounded_run_complete")
            return result
        except DeadlineExpired:
            return {"status": "paused", "reason": "hard_deadline_reached"}
        finally:
            if self.process:
                self.process.stop(deadline=self.deadline)
            try:
                if restored and remaining(self.deadline) > 0:
                    self.bounded_clients()
                    self.auth.persist()
            finally:
                self.auth.cleanup()
                signal.setitimer(signal.ITIMER_REAL, 0)
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)


def build_quota_services(config):
    from .billing import BillingGuard, BudgetPolicy
    from .github import GitHub
    from .quota_stop import QuotaStop

    admin_token = os.environ.get("RUNNER_GITHUB_TOKEN")
    if not admin_token:
        raise BlockedError("A management token is required", "management_token_missing")
    admin = GitHub(admin_token)
    notification_token = os.environ.get("QUOTA_NOTIFICATION_TOKEN")
    notifications = GitHub(notification_token, timeout=10) if notification_token else None
    # Quota suspension must still work after a worker checkpoint exhausts the
    # development client's deadline. Use an independent management client.
    quota_admin = GitHub(admin_token, timeout=10)
    quota_stop = QuotaStop(quota_admin, config["owner"], config["runner_repository"],
                          notification_github=notifications)
    billing = BillingGuard(admin, config["owner"], BudgetPolicy.from_mapping(config["billing"]))
    return admin, billing, quota_stop


def record_quota_stop(quota_stop, error):
    from .github import GitHub

    if quota_stop is None:
        return {"status": "stop_pending", "failures": {"quota_stop": "quota_stop_unavailable"}}
    try:
        stop_deadline = time.monotonic() + 60
        for attribute in ("github", "notification_github"):
            client = getattr(quota_stop, attribute, None)
            if isinstance(client, GitHub):
                client.deadline = stop_deadline
                client.timeout = 10
        return quota_stop.stop(error.evidence)
    except Exception:
        # Provider errors can include credentials; expose only a fixed code.
        return {"status": "stop_pending", "failures": {"quota_stop": "quota_stop_failed"}}


def report_quota_stop(error):
    remaining_minutes = error.evidence["remaining_minutes"]
    threshold = error.evidence["threshold_minutes"]
    message = ("Idea Vaultは自動開発を停止しました。無料の計算枠の残りは標準Linux換算で%.2f分です。"
               "停止閾値は%s分です。利用再開方法は未定です。" % (remaining_minutes, threshold))
    outcome = getattr(error, "stop_result", {})
    persistent = ("停止状態は保存済みです。" if outcome.get("status") == "stopped" else
                  "停止状態の保存を確認できません。実行設定の確認が必要です。")
    notification = ("警告Issue: " + outcome["issue_url"]
                    if outcome.get("notification_sent") and outcome.get("issue_url") else
                    "警告Issueの作成は未完了です。")
    print("::error::" + message, file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as out:
                out.write("## Idea Vaultの利用停止\n\n" + message + "\n\n"
                          + persistent + "\n\n" + notification + "\n")
        except OSError:
            pass


def build_services(config):
    from .auth import AuthStore
    from .checkpoint import checkpoint_workspace
    from .github import GitHub
    from .state import ProjectStore

    admin, billing, quota_stop = build_quota_services(config)
    development_token = os.environ.get("DEVELOPMENT_GITHUB_TOKEN")
    if not development_token:
        raise BlockedError("A separate development token is required", "development_token_missing")
    if development_token == admin.token:
        raise BlockedError("Management and development tokens must be distinct", "token_separation_required")
    development = GitHub(development_token)
    auth = AuthStore(admin, config["runner_repository"], Path(config["runtime_directory"]) / ("codex-" + uuid.uuid4().hex))
    store = ProjectStore(admin, config["runner_repository"], config["state_path"])
    return Controller(config, admin, development, auth, billing, store,
                      checkpoint=checkpoint_workspace, quota_stop=quota_stop)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "status", "sync-schedule", "resume", "doctor", "auth-check", "quota-check"))
    parser.add_argument("--config", default="config/runner.yaml")
    parser.add_argument("--ideas-dir", type=Path)
    parser.add_argument("--schedule", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--idea-id")
    args = parser.parse_args(argv)
    quota_stop = None
    try:
        config = load_config(args.config)
        if args.command == "plan":
            if args.ideas_dir is None or args.schedule is None:
                raise BlockedError("Offline planning requires --ideas-dir and --schedule", "plan_input_missing")
            schedule = load_schedule(args.schedule)
            state = json.loads(args.state.read_text()) if args.state else {"projects": {}}
            ideas = select_candidates(read_ideas(args.ideas_dir), state.get("projects", {}))
            result = {"mode": "offline_plan", "schedule_enabled": schedule.enabled,
                      "cron": cron_entries(schedule), "ideas": [x.id for x in ideas]}
        elif args.command == "sync-schedule":
            from .operations import sync_schedule
            _, _, quota_stop = build_quota_services(config)
            quota_stop.assert_not_stopped()
            result = sync_schedule(config)
        elif args.command == "resume":
            from .operations import resume_project
            if not args.idea_id:
                raise BlockedError("Resume requires --idea-id", "idea_id_missing")
            _, _, quota_stop = build_quota_services(config)
            quota_stop.assert_not_stopped()
            result = resume_project(config, args.idea_id)
        elif args.command == "status":
            from .github import GitHub
            from .quota_stop import QuotaStop
            from .state import ProjectStore
            github = GitHub(os.environ.get("RUNNER_GITHUB_TOKEN"))
            result = ProjectStore(github, config["runner_repository"], config["state_path"]).load()
            result["quota_stop"] = QuotaStop(github, config["owner"], config["runner_repository"]).status()
        else:
            if args.command in ("run", "auth-check", "quota-check") and (
                os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_REPOSITORY", "").casefold() != config["runner_repository"].casefold()
            ):
                raise BlockedError("Development runs require the private execution repository's Actions job", "actions_required")
            if args.command == "quota-check":
                _, billing, quota_stop = build_quota_services(config)
                quota_stop.assert_not_stopped()
                result = {"mode": "quota_check", "status": "ready",
                          "billing": billing.check(utcnow(), required_minutes=1)}
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            controller = build_services(config)
            quota_stop = controller.quota_stop
            if quota_stop is not None and args.command != "run":
                quota_stop.assert_not_stopped()
            with local_lock(config["runtime_directory"]):
                if args.command == "run":
                    result = controller.run(os.environ.get("CODEX_AUTH_JSON", ""))
                else:
                    from .operations import doctor
                    result = doctor(controller, auth_check=args.command == "auth-check")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "blocked" else 0
    except QuotaThresholdReached as error:
        if not hasattr(error, "stop_result"):
            error.stop_result = record_quota_stop(quota_stop, error)
        report_quota_stop(error)
        print(json.dumps({"status": "blocked", "reason": error.code,
                          "billing": error.evidence, "quota_stop": error.stop_result}, ensure_ascii=False))
        return 2
    except RunnerError as error:
        print(json.dumps({"status": "blocked", "reason": error.code, "message": str(error)}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        print(json.dumps({"status": "blocked", "reason": "invalid_or_unavailable_input"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
