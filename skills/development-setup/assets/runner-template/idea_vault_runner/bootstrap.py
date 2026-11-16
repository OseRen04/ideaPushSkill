"""Idempotent preparation of private, explicitly bound development repositories."""

import json
import re
from collections.abc import Mapping
from urllib.parse import quote

from .errors import BlockedError, ConflictError, RunnerError
from .github import validate_repository


SOURCE_DOCUMENT_NAMES = ("development-flow.md", "system-design.md")


def _source_documents(value):
    if value is None:
        return {}
    if (not isinstance(value, Mapping)
            or any(name not in SOURCE_DOCUMENT_NAMES or not isinstance(text, str)
                   for name, text in value.items())):
        raise BlockedError("Invalid optional project documents.", "invalid_source_documents")
    try:
        for text in value.values():
            text.encode("utf-8")
    except UnicodeError:
        raise BlockedError("Optional project documents must be valid UTF-8.",
                           "invalid_source_documents") from None
    return dict(value)


def _verify_marker(marker, binding):
    try:
        saved = json.loads(marker)
    except (ValueError, TypeError):
        raise BlockedError("Invalid project ownership marker.", "repository_binding") from None
    if saved != binding:
        raise BlockedError("Existing repository is bound to another idea.", "repository_binding")


def _optional_file(github, repository, path, ref=None):
    try:
        return github.read_file(repository, path, ref=ref)[0]
    except RunnerError as exc:
        if exc.code == "not_found":
            return None
        raise


def _repository_access(github, repository, owner):
    repo = github.repository(repository)
    if (repo.get("owner", {}).get("login", "").lower() != owner.lower()
            or repo.get("private") is not True or repo.get("archived") is True
            or repo.get("disabled") is True):
        raise BlockedError("Development repository owner or privacy is invalid.", "repository_binding")
    return repo


def prepare_project(github, owner, idea, existing=None, prefix="idea-", source_repository=None,
                    source_documents=None):
    if getattr(idea, "development", None) != "allowed":
        raise BlockedError("Project creation requires explicit development permission.", "development_denied")
    if not isinstance(idea.id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", idea.id):
        raise BlockedError("Invalid idea identifier.", "invalid_idea_id")
    documents = _source_documents(source_documents)
    source = validate_repository(source_repository or owner + "/idea-vault")
    if source.split("/", 1)[0].lower() != owner.lower():
        raise BlockedError("Idea source must belong to the configured owner.", "repository_binding")
    identity = github.api("/user")
    if identity.get("login", "").lower() != owner.lower():
        raise BlockedError("GitHub identity does not match the configured owner.", "github_identity")
    prior = dict(existing or {})
    repository = prior.get("repository") or "%s/%s%s" % (owner, prefix, idea.id)
    validate_repository(repository)
    if repository.split("/", 1)[0].lower() != owner.lower():
        raise BlockedError("Development repository belongs to another owner.", "repository_binding")
    if repository.lower() == source.lower():
        raise BlockedError("Idea source cannot be used as a development repository.", "repository_binding")
    description = "Idea Vault project: %s/%s" % (source, idea.id)
    binding = {"version": 1, "idea_id": idea.id, "source_repository": source}
    try:
        repo = _repository_access(github, repository, owner)
    except RunnerError as exc:
        if exc.code != "not_found":
            raise
        if prior.get("repository_id"):
            raise BlockedError("Previously bound repository is inaccessible.", "repository_binding") from None
        github.api("/user/repos", "POST", {
            "name": repository.split("/", 1)[1], "private": True,
            "auto_init": True, "description": description})
        repo = _repository_access(github, repository, owner)
    if prior.get("repository_id") is not None and prior["repository_id"] != repo.get("id"):
        raise BlockedError("Development repository identity changed.", "repository_binding")
    branch = repo["default_branch"]
    head = github.head(repository, branch)
    marker = _optional_file(github, repository, ".idea-vault/project.json", ref=head)
    if marker is None:
        # The description is written atomically with creation. It also binds a repo
        # when a previous run stopped before the first marker commit.
        if repo.get("description") != description:
            raise BlockedError("Existing repository is not bound to this idea.", "repository_binding")
    else:
        _verify_marker(marker, binding)
    development_branch = "agent/" + idea.id
    if prior.get("branch") and prior["branch"] != development_branch:
        raise BlockedError("Stored development branch does not match the idea.", "repository_binding")
    try:
        development_head = github.head(repository, development_branch)
    except RunnerError as exc:
        if exc.code != "not_found":
            raise
        development_head = None
    if development_head is not None:
        _verify_marker(_optional_file(github, repository, ".idea-vault/project.json",
                                      ref=development_head), binding)
    issue_marker = "<!-- idea-vault:%s:%s -->" % (source, idea.id)
    matches = []
    for page in range(1, 101):
        issues = github.api("/repos/%s/issues?state=all&per_page=100&page=%d" % (repository, page))
        if not isinstance(issues, list):
            raise RunnerError("Invalid GitHub issue response.", "github_response")
        matches.extend(issue for issue in issues if "pull_request" not in issue
                       and issue_marker in (issue.get("body") or ""))
        if len(issues) < 100:
            break
    else:
        raise BlockedError("Issue search exceeded the safety limit.", "issue_search_limit")
    if len(matches) > 1:
        raise ConflictError("Multiple issues are bound to the same idea.", "issue_binding")
    if matches:
        issue = matches[0]
        if prior.get("issue_number") and prior["issue_number"] != issue.get("number"):
            raise BlockedError("Stored issue identity changed.", "issue_binding")
        labels = {item["name"] if isinstance(item, dict) else item for item in issue.get("labels", [])}
        if issue.get("state") != "open" or {"agent:review", "agent:blocked"} & labels:
            raise BlockedError("Development issue is closed or awaiting a user.", "issue_not_ready")
        if "agent:ready" not in labels:
            raise BlockedError("Development issue is not marked agent:ready.", "issue_not_ready")
    else:
        if prior.get("issue_number"):
            raise BlockedError("Previously bound issue was not found.", "issue_binding")
        issue = None

    # Verify existing branches and issue readiness before changing snapshots.
    files = {}
    if marker is None:
        files[".idea-vault/project.json"] = json.dumps(binding, ensure_ascii=False, indent=2) + "\n"
    if _optional_file(github, repository, "docs/idea.md", ref=head) is None:
        files["docs/idea.md"] = idea.content
    if _optional_file(github, repository, "README.md", ref=head) is None:
        files["README.md"] = "# %s\n\nDevelopment project for `%s/%s`.\n" % (idea.title, source, idea.id)
    snapshots = {}
    for name, content in documents.items():
        path = "docs/" + name
        saved = _optional_file(github, repository, path, ref=head)
        snapshots[path] = saved if saved is not None else content
        if saved is None:
            files[path] = content
    if files:
        head = github.write_files(repository, files, "Initialize Idea Vault project", branch, head)

    if development_head is None:
        try:
            github.api("/repos/%s/git/refs" % repository, "POST",
                       {"ref": "refs/heads/" + development_branch, "sha": head})
        except RunnerError as race:
            if race.code not in {"conflict", "github_http_422"}:
                raise
            raise ConflictError("Development branch was created concurrently.", "conflict") from None
    if development_head is not None:
        missing = {path: content for path, content in snapshots.items()
                   if _optional_file(github, repository, path, ref=development_head) is None}
        if missing:
            github.write_files(repository, missing, "Add optional Idea Vault documents",
                               development_branch, development_head)

    for label, color in [("agent:ready", "0e8a16"), ("agent:review", "fbca04"),
                         ("agent:blocked", "d73a4a")]:
        try:
            github.api("/repos/%s/labels/%s" % (repository, quote(label, safe="")))
        except RunnerError as exc:
            if exc.code != "not_found":
                raise
            try:
                github.api("/repos/%s/labels" % repository, "POST", {"name": label, "color": color})
            except RunnerError as race:
                if race.code not in {"conflict", "github_http_422"}:
                    raise
    if issue is None:
        body = (issue_marker + "\n\n## Purpose\nImplement the authorized project in `docs/idea.md`.\n"
                "Read `docs/development-flow.md` and `docs/system-design.md` when present.\n"
                "These documents are saved snapshots and may predate the source repository.\n"
                "Missing optional documents are not an error. Only those exact filenames are imported.\n"
                "\n## Acceptance\n- Commit code and tests to `%s`.\n"
                "- Record test commands, results, remaining work and next action here.\n"
                "- Request review; do not merge, deploy, or change development permission.\n") % development_branch
        issue = github.api("/repos/%s/issues" % repository, "POST", {
            "title": ("Implement MVP: " + idea.title.replace("\n", " "))[:240],
            "body": body, "labels": ["agent:ready"]})
    return dict(prior, idea_id=idea.id, source_repository=source, repository=repository,
                repository_id=repo["id"], default_branch=branch, branch=development_branch,
                issue_number=issue["number"], issue_url=issue.get("html_url"),
                status=prior.get("status", "pending"))
