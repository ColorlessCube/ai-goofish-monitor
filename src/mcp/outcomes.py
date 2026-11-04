"""Internal handler-to-transport outcomes; never inferred from result JSON."""


class MutationOutcomeError(Exception):
    """An explicit failed or uncertain mutation, carrying a scrubbed-at-boundary payload."""

    def __init__(self, status: str, payload: dict):
        if status not in ("failed", "unknown"):
            raise ValueError("invalid_mutation_outcome_status")
        if not isinstance(payload, dict):
            raise TypeError("mutation_outcome_payload_must_be_dict")
        self.status = status
        self.payload = payload
        # Exception logs/reprs must not include an upstream response or secret.
        super().__init__("mutation_outcome_error")
