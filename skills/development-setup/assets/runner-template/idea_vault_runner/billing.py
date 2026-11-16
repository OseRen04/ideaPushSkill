"""Fail-closed account billing gate with explicit owner-confirmed evidence.

The public personal-account APIs do not certify a hard spending stop or current
artifact/Packages shared capacity. A fresh owner-confirmed snapshot is therefore
required as well as the official account usage response. The result deliberately
does not call that snapshot an automatically verified GitHub guarantee.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re

from .errors import BlockedError, QuotaThresholdReached

UTC = timezone.utc


def _blocked(message, code="billing_unverified"):
    raise BlockedError(message, code)


def _number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _blocked("Billing evidence requires a numeric " + name + ".")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        _blocked("Billing evidence contains an invalid " + name + ".")
    return value


def _stop_threshold(value):
    if type(value) is not int or value < 500:
        _blocked("The protected free allowance must be an integer of at least 500 minutes.",
                 "billing_config_invalid")
    return value


def _timestamp(value, name):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        _blocked("Billing evidence requires an aware " + name + ".")


@dataclass(frozen=True)
class BudgetPolicy:
    confirmed_stop_usage: bool = False
    stop_usage_confirmed_at: str = ""
    confirmation_expires_at: str = ""
    monthly_compute_allowance_usd: object = None
    standard_linux_rate_usd_per_minute: object = None
    reserve_compute_usd: float = 0.05
    stop_remaining_minutes: int = 500
    minimum_artifact_remaining_gb: float = 0.01
    minimum_cache_remaining_gb: float = 0.01
    max_snapshot_age_minutes: int = 60
    snapshot_path: str = ""
    cache_repository: str = ""

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            _blocked("Billing policy must be a mapping.", "billing_config_invalid")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            _blocked("Billing policy contains unsupported keys.", "billing_config_invalid")
        policy = cls(**value)
        if type(policy.confirmed_stop_usage) is not bool:
            _blocked("Billing stop confirmation must be boolean.", "billing_config_invalid")
        _stop_threshold(policy.stop_remaining_minutes)
        age = policy.max_snapshot_age_minutes
        if type(age) is not int or not 1 <= age <= 1440:
            _blocked("Billing snapshot age must be between 1 and 1440 minutes.", "billing_config_invalid")
        if not isinstance(policy.snapshot_path, str) or not isinstance(policy.cache_repository, str):
            _blocked("Billing snapshot path and cache repository must be strings.", "billing_config_invalid")
        return policy


class BillingGuard:
    # Unknown SKUs stop the run; silently ignoring a newly shared product would
    # overstate the available allowance. Values are normalized official SKU names.
    COMPUTE = {
        "actions_linux", "actions_linux_slim", "actions_linux_arm",
        "actions_windows", "actions_windows_arm", "actions_macos", "actions_macos_arm",
        "cloud_sandbox_compute", "cloud_sandbox_memory",
    }
    STORAGE = {"actions_storage", "actions_artifact_storage", "actions_cache_storage"}
    SEPARATE = {"copilot", "github_copilot", "packages", "git_large_file_storage", "lfs", "codespaces"}

    def __init__(self, github, owner, policy):
        self.github = github
        self.owner = owner
        self.policy = policy

    @staticmethod
    def _key(value):
        return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")

    def _snapshot(self, now):
        policy = self.policy
        if not policy.snapshot_path:
            _blocked("A fresh owner-confirmed billing snapshot is required.")
        try:
            source = Path(policy.snapshot_path)
            if source.is_symlink() or source.stat().st_size > 1024 * 1024:
                raise ValueError()
            snapshot = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            _blocked("Cannot read a valid billing snapshot.")
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
            _blocked("Billing snapshot schema is not supported.")
        if snapshot.get("source") != "owner-confirmed-github-billing":
            _blocked("Billing evidence must identify its owner-confirmed source.")
        if str(snapshot.get("owner", "")).lower() != self.owner.lower():
            _blocked("Billing snapshot belongs to a different account.")
        expected_cache = policy.cache_repository.lower()
        if not expected_cache.startswith(self.owner.lower() + "/"):
            _blocked("Billing policy must identify the owner's cache repository.")
        if str(snapshot.get("cache_repository", "")).lower() != expected_cache:
            _blocked("Billing snapshot does not cover the execution repository cache.")
        collected = _timestamp(snapshot.get("collected_at"), "collected_at")
        covers = _timestamp(snapshot.get("covers_through"), "covers_through")
        age = timedelta(minutes=policy.max_snapshot_age_minutes)
        if not now - age <= covers <= collected <= now:
            _blocked("Billing snapshot is stale or dated in the future.", "billing_stale")
        start = _timestamp(snapshot.get("period_start"), "period_start")
        end = _timestamp(snapshot.get("period_end"), "period_end")
        if not start <= covers <= now < end or not timedelta(days=27) <= end - start <= timedelta(days=33):
            _blocked("Billing snapshot does not cover the current monthly cycle.")
        if snapshot.get("billing_period") != start.strftime("%Y-%m"):
            _blocked("Billing snapshot period does not match its cycle dates.")
        for key in ("compute_remaining_usd", "artifact_remaining_gb", "cache_remaining_gb"):
            _number(snapshot.get(key), key)
        return snapshot, start, end, collected, covers

    def _compute_usage(self, start, now):
        total = Decimal("0")
        month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while month <= now:
            path = "/users/{}/settings/billing/usage?year={}&month={}".format(self.owner, month.year, month.month)
            try:
                response = self.github.api(path)
            except Exception:
                _blocked("Account billing usage cannot be verified.", "billing_api_unavailable")
            if not isinstance(response, dict) or not isinstance(response.get("usageItems"), list):
                _blocked("Account billing usage response is incomplete.")
            for item in response["usageItems"]:
                if not isinstance(item, dict):
                    _blocked("Account billing usage contains an invalid item.")
                try:
                    date = datetime.strptime(item["date"][:10], "%Y-%m-%d").date()
                except (KeyError, ValueError, TypeError):
                    _blocked("Account billing usage has an invalid date.")
                if (date.year, date.month) != (month.year, month.month) or date > now.date():
                    _blocked("Account billing usage returned a different period.")
                # Include the entire first day conservatively when the billing
                # cycle starts mid-day; the date-only API cannot split that day.
                if date < start.date():
                    continue
                product, sku = self._key(item.get("product", "")), self._key(item.get("sku", ""))
                gross = _number(item.get("grossAmount"), "grossAmount")
                _number(item.get("quantity"), "quantity")
                if item.get("currency", "USD") != "USD":
                    _blocked("Account billing usage is not denominated in USD.")
                if sku in self.COMPUTE and product in {"actions", "github_actions", "cloud_sandbox", "cloud_sandboxes"}:
                    total += Decimal(str(gross))
                elif sku in self.STORAGE and product in {"actions", "github_actions"}:
                    pass  # Current shared capacity is covered by the snapshot.
                elif product in self.SEPARATE:
                    pass
                else:
                    _blocked("Account billing usage contains an unrecognized product or SKU.", "billing_unknown_sku")
            month = month.replace(year=month.year + (month.month == 12), month=1 if month.month == 12 else month.month + 1)
        return total

    def check(self, now, required_minutes):
        if not isinstance(now, datetime) or now.tzinfo is None:
            _blocked("Billing checks require an aware current time.")
        now = now.astimezone(UTC)
        policy = self.policy
        if policy.confirmed_stop_usage is not True:
            _blocked("The account owner must confirm GitHub's stop-usage setting.", "billing_stop_not_confirmed")
        confirmed = _timestamp(policy.stop_usage_confirmed_at, "stop_usage_confirmed_at")
        expires = _timestamp(policy.confirmation_expires_at, "confirmation_expires_at")
        if not confirmed <= now < expires or expires - confirmed > timedelta(days=31):
            _blocked("The account stop-usage confirmation has expired.", "billing_stop_not_confirmed")
        allowance = _number(policy.monthly_compute_allowance_usd, "monthly_compute_allowance_usd", positive=True)
        rate = _number(policy.standard_linux_rate_usd_per_minute, "standard_linux_rate_usd_per_minute", positive=True)
        reserve = _number(policy.reserve_compute_usd, "reserve_compute_usd")
        minutes = _number(required_minutes, "required_minutes", positive=True)
        artifact = _number(policy.minimum_artifact_remaining_gb, "minimum_artifact_remaining_gb", positive=True)
        cache = _number(policy.minimum_cache_remaining_gb, "minimum_cache_remaining_gb", positive=True)
        threshold_minutes = _stop_threshold(policy.stop_remaining_minutes)
        snapshot, start, end, collected, covers = self._snapshot(now)
        if now + timedelta(minutes=minutes) >= end:
            _blocked("A development run may not cross an unverified billing cycle.")
        used = self._compute_usage(start, now)
        # Decimal arithmetic preserves the inclusive 500-minute boundary even
        # when account usage has been accumulated across several repositories.
        rate_decimal = Decimal(str(rate))
        remaining = min(max(Decimal("0"), Decimal(str(allowance)) - used),
                        Decimal(str(snapshot["compute_remaining_usd"])))
        required = rate_decimal * Decimal(str(minutes)) + Decimal(str(reserve))
        protected = rate_decimal * threshold_minutes
        quota_evidence = {
            "owner": self.owner,
            "remaining_minutes": float(remaining / rate_decimal),
            "threshold_minutes": threshold_minutes,
            "compute_remaining_usd": float(remaining),
            "stop_compute_usd": float(protected),
            "checked_at": now.isoformat(),
            "billing_period": snapshot["billing_period"],
            "evidence_source": "owner-confirmed-snapshot-and-github-account-usage",
        }
        if remaining <= protected:
            raise QuotaThresholdReached(quota_evidence)
        if (remaining < required + protected
                or snapshot["artifact_remaining_gb"] < artifact
                or snapshot["cache_remaining_gb"] < cache):
            _blocked("Verified compute or storage allowance is insufficient.", "billing_insufficient")
        return {
            "allowed": True,
            "automatically_verified": False,
            "evidence_source": "owner-confirmed-snapshot-and-github-account-usage",
            "owner": self.owner,
            "compute_remaining_usd": float(remaining),
            "remaining_minutes": quota_evidence["remaining_minutes"],
            "threshold_minutes": threshold_minutes,
            "stop_compute_usd": float(protected),
            "required_compute_usd": float(required),
            "artifact_remaining_gb": snapshot["artifact_remaining_gb"],
            "cache_remaining_gb": snapshot["cache_remaining_gb"],
            "checked_at": now.isoformat(),
            "snapshot_collected_at": collected.isoformat(),
            "covers_through": covers.isoformat(),
            "billing_period": snapshot["billing_period"],
        }
