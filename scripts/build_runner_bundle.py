"""Build and verify the portable Runner template from an explicit source list."""

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "skills/development-setup/assets/runner-template"
VERSION = "0.7.0"
MANIFEST = "bundle-manifest.json"
STATIC = (
    ".github/workflows/autonomous-development.yml", ".gitignore", "pyproject.toml",
    "scripts/install_symphony.py", "config/WORKFLOW.md", "config/development-schedule.example.yaml",
)
FORBIDDEN = {".git", ".venv", ".runtime", "__pycache__", "auth.json", ".env"}


def safe_path(name):
    if not isinstance(name, str):
        raise ValueError("Unsafe bundle path")
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute()
            or any(part in {"", ".", ".."} or part in FORBIDDEN for part in name.split("/"))
            or path.suffix in {".pem", ".key", ".pyc"}):
        raise ValueError("Unsafe bundle path")
    return name


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A bundle must be an ordinary directory")
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("format") != "idea-vault-runner-template"
            or not isinstance(manifest.get("files"), dict) or not manifest["files"]
            or not isinstance(manifest.get("bundle_version"), str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", manifest["bundle_version"])
            or not isinstance(manifest.get("source_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", manifest["source_commit"])):
        raise ValueError("Unsupported bundle manifest")
    expected = {safe_path(name) for name in manifest["files"]} | {MANIFEST}
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Bundle symlinks are not supported")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ValueError("Bundle inventory does not match the manifest")
    for name, digest in manifest["files"].items():
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid bundle checksum")
        if sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Bundle content does not match its checksum: " + name)
    return manifest


def configuration():
    return {
        "owner": "__OWNER__", "ideas_repository": "__IDEAS_REPOSITORY__",
        "runner_repository": "__RUNNER_REPOSITORY__",
        "schedule_path": "settings/development-schedule.yaml", "state_path": "state/projects.json",
        "repository_prefix": "idea-", "checkpoint_interval_seconds": 60, "poll_interval_seconds": 10,
        "max_retries": 2, "max_projects_per_run": 5, "symphony_command": ["symphony"],
        "symphony_release": "v0.0.3", "codex_version": "0.169.0", "codex_command": "codex app-server",
        "workflow_template": "WORKFLOW.md", "runtime_directory": "../.runtime",
        "billing": {
            "confirmed_stop_usage": False, "stop_usage_confirmed_at": None, "confirmation_expires_at": None,
            "monthly_compute_allowance_usd": None, "standard_linux_rate_usd_per_minute": None,
            "reserve_compute_usd": 0.05, "stop_remaining_minutes": 500,
            "minimum_artifact_remaining_gb": 0.01, "minimum_cache_remaining_gb": 0.01,
            "max_snapshot_age_minutes": 60, "snapshot_path": "../state/billing-snapshot.json",
            "cache_repository": "__RUNNER_REPOSITORY__",
        },
    }


OPERATIONS = """# 利用者のIdea Vault Runner

## 配置と認証

利用者は、Idea Vaultへ「初期セットアップを進めて」と依頼します。
セットアップスキルは、同梱コードから利用者専用の作業コピーを作成します。
配置先は、利用者自身の非公開リポジトリです。
導入処理は、利用者の値をconfig/runner.yamlへ生成します。
初期状態では、RUNNER_ENABLEDと定期実行を無効に保ちます。
利用者は、CI設定を希望する場合、管理用と開発用のGitHub認証を用意します。
CI管理用認証には、対象リポジトリのContents・Secrets・Variables更新権限が必要です。
CI管理用認証には、開発先作成・使用量読取・時間設定同期の権限も必要です。
個人アカウントの使用量取得には、Planの読取権限を確認します。
開発用認証は、開発先のContents・Issues・Pull requestsの更新に限定します。
利用者は、公式CLIの安全な入力経路でRUNNER_GITHUB_TOKENとDEVELOPMENT_GITHUB_TOKENを登録します。
利用者は、CI専用のCODEX_HOMEでChatGPTログインを行います。
セットアップスキルは、CI専用認証をCODEX_AUTH_JSONへ登録します。
利用者は、パスワードやトークンを会話へ貼り付けません。
Actionsは、警告Issue用のbot認証をQUOTA_NOTIFICATION_TOKENとして渡します。

## 費用と自動開発の開始

利用者は、GitHub側の利用停止設定・契約枠・実際の請求期間を確認します。
利用者は、確認済みの値をconfig/runner.yamlのbillingへ設定します。
利用者は、config/billing-snapshot.example.jsonを参考にstate/billing-snapshot.jsonを登録します。
設定値は、推測で補いません。所有者とcache_repositoryには、利用者自身の値を使用します。
制御プログラムは、公式使用量と利用者が確認した残枠の小さい方を使います。
制御プログラムは、60分より古い確認データを拒否します。
残枠データの自動供給は未確立です。配置完了だけでは、無人継続運用の完了を意味しません。
利用者は、CIの検証も希望する場合、費用条件を満たしてからdoctorとauth-checkを実行します。
制御プログラムは、認証成功と更新認証の保存成功を別々に報告します。
利用者は、運用開始を希望する場合、曜日・時間帯・タイムゾーンを指定します。
利用者は、開発を許可する企画だけagent_developmentをallowedへ変更します。
セットアップスキルは、configの時間設定をcronへ同期します。
自動開発は、利用者による運用開始の指定後に有効にします。

## 開発フローとシステム設計

利用者は、企画の開発フローとシステム設計の追加をIdea Vaultへ依頼できます。
Idea Vaultは、企画直下のdevelopment-flow.mdとsystem-design.mdへ保存します。
通常の企画保存では、Idea Vaultは追加文書を自動作成しません。
制御プログラムは、企画書と同じ取得時点の追加文書を開発先のdocs/へコピーします。
制御プログラムは、標準名の2文書だけを自動開発の参照対象にします。
制御プログラムは、開発先に保存済みの文書を自動で上書きしません。
したがって、開発先の保存版と企画リポジトリの最新版は異なる場合があります。
利用者は、既存開発先の文書を更新する場合、対象を明示して更新を依頼します。
追加文書の保存は、開発許可や実行時間を変更しません。

## 残り500分の警告と停止

制御プログラムは、計算枠を設定済みの標準Linux単価で分数へ換算します。
制御プログラムは、残り500分以下を検出した場合に自動開発を停止します。
予定実行と予備費で500分の保護枠を割り込む場合、制御プログラムは実行を見送ります。
制御プログラムは、RUNNER_ENABLED=falseとstate/quota-stop.jsonへ停止状態を保存します。
通知用botは、利用者を担当者とする警告Issueを作成します。
GitHubは、利用者の通知設定に従ってメールを配信します。
通知に失敗しても、制御プログラムは停止状態を維持します。
請求月の更新は、停止状態を解除しません。利用再開方法は未検討です。
既存のresumeコマンドは、無料枠による停止を解除しません。
残枠の判定は、ジョブ開始時と実行中の定期確認で行います。
実行中の確認間隔は、既定60秒です。停止中の定期監視は行いません。
GitHub側の集計遅延があるため、制御プログラムは検出時点で停止します。

## 状況の確認

利用者は、Idea Vaultへ「自動開発の状況を確認して」と依頼します。
制御プログラムのstatusは、実行状態と永続停止の有無を返します。
導入処理を再実行しても、既存の認証情報や開発履歴を無断で上書きしません。
利用者は、最新のセットアップ手順をプラグイン内のdevelopment-setupスキルで確認します。
"""


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build(source, output=OUTPUT):
    if Path(output).is_symlink():
        raise ValueError("A build output must not be a symlink")
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents:
        raise ValueError("Build output must be outside the source repository")
    files = {}
    names = list(STATIC) + sorted(p.relative_to(source).as_posix()
                                for p in (source / "idea_vault_runner").glob("*.py"))
    if not any(name.endswith("/controller.py") for name in names):
        raise ValueError("The Runner source is incomplete")
    for name in names:
        safe_path(name)
        path = source / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source.parent):
            raise ValueError("Source symlinks are not supported")
        files[name] = path.read_bytes()
    files["config/runner.template.json"] = json_bytes(configuration())
    files["config/billing-snapshot.example.json"] = json_bytes({
        "schema_version": 1, "source": "owner-confirmed-github-billing", "owner": "__OWNER__",
        "cache_repository": "__RUNNER_REPOSITORY__", "billing_period": None,
        "period_start": None, "period_end": None, "collected_at": None, "covers_through": None,
        "compute_remaining_usd": None, "artifact_remaining_gb": None, "cache_remaining_gb": None,
    })
    files["state/projects.json"] = json_bytes({"version": 1, "projects": {}, "last_run": None})
    files["docs/OPERATIONS.md"] = OPERATIONS.encode("utf-8")
    files["README.md"] = ("# Idea Vault Runner\n\n"
                          "利用者自身の非公開GitHubリポジトリで動く、自動開発の制御プログラムです。\n\n"
                          "- セットアップスキルは、利用者専用の設定をconfig/runner.yamlへ生成します。\n"
                          "- 初期状態では、自動実行を無効に保ちます。\n"
                          "- 利用者は、CI認証と最新の費用確認データを登録します。\n"
                          "- 残り500分以下を検出すると、制御プログラムは停止と警告を行います。\n"
                          "- 詳しい操作は[運用手順](docs/OPERATIONS.md)を参照してください。\n").encode("utf-8")
    commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    manifest = {"schema_version": 1, "format": "idea-vault-runner-template", "bundle_version": VERSION,
                "source_commit": commit, "minimum_python": "3.9",
                "files": {name: sha256(content).hexdigest() for name, content in sorted(files.items())}}
    if output.exists() and any(output.iterdir()):
        verify(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runner-bundle-", dir=output.parent) as stage:
        stage = Path(stage)
        for name, content in files.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (stage / MANIFEST).write_bytes(json_bytes(manifest))
        verify(stage)
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(stage, output)
    return verify(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = build(args.source, args.output) if args.source else verify(args.output)
    print(json.dumps({"bundle_version": result["bundle_version"], "source_commit": result["source_commit"],
                      "file_count": len(result["files"]), "verified": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
