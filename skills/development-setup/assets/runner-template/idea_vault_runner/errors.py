"""Public errors contain operational context, never credential values."""


class RunnerError(RuntimeError):
    def __init__(self, message, code="runner_error"):
        super().__init__(message)
        self.code = code


class BlockedError(RunnerError):
    pass


class QuotaThresholdReached(BlockedError):
    """Verified free allowance reached the protected, persistent-stop threshold."""

    def __init__(self, evidence):
        super().__init__("Free compute allowance reached the protected minute threshold.",
                         "billing_quota_threshold")
        self.evidence = dict(evidence)


class ConflictError(RunnerError):
    pass
