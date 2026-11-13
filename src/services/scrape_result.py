"""Internal CLI outcome metadata, preserving existing integer-count callers."""
from src.services.task_failure_notices import WEBSITE_FAULTS, safe_source_reason


class ScrapeResult(int):
    """An actual processed count with a safe, explicit terminal outcome."""

    status: str
    reason: str

    def __new__(cls, count: int, *, status: str = 'success', reason: str = ''):
        if status not in {'success', 'source_unavailable', 'paused', 'failed'}:
            raise ValueError('invalid_scrape_status')
        result = super().__new__(cls, count)
        result.status = status
        if status == 'source_unavailable':
            result.reason = safe_source_reason(reason)
        elif status in {'paused', 'failed'}:
            result.reason = reason if reason in WEBSITE_FAULTS else 'browser_operation_failed'
        else:
            result.reason = ''
        return result
