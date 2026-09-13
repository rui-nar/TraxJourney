"""Retry wrapper for version-checked project saves (issues #172, #173).

Every structural mutation of a project is a load → mutate → compare-and-swap
save, and the CAS loses whenever another writer commits in between. Before this
module there were three inconsistent policies against that same conflict: the
resolve trigger had no retry at all (surfacing a 409 the client never retried),
the resolve job had a bare ``range(2)`` with no backoff, and the crash safety
net could itself lose the race. This is the one place that owns the policy.

The mutation is expressed as a callback so each attempt re-runs it against
*freshly loaded* state — re-applying to a stale object is what silently drops a
concurrent writer's work.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Optional

from models.db import get_session
from src.models.project import Project
from src.project.repo_core import StaleWriteError
from src.utils.logging import get_logger

_log = get_logger(__name__)

# A handful of writers colliding, not a stampede — this converges fast and
# stays well inside a request's budget even when every attempt is used.
DEFAULT_ATTEMPTS = 5
_BACKOFF_BASE_S = 0.02
_BACKOFF_MAX_S = 0.25


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter, capped."""
    ceiling = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_MAX_S)
    return random.uniform(0, ceiling)


class SaveRetryMixin:
    """``save_project_with_retry`` — the only sanctioned way to do a CAS save."""

    def save_project_with_retry(
        self,
        user_info_id: int,
        name: str,
        mutate: Callable[[Project], None],
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        activity_user_id: Optional[int] = None,
    ) -> Optional[Project]:
        """Load, apply *mutate*, and save under the optimistic lock; retry on conflict.

        Opens its own session per attempt: a retry must re-read the project, so
        holding one session across attempts would just re-apply the mutation to
        the stale snapshot the CAS already rejected.

        Returns the saved :class:`Project`, or ``None`` if it does not exist.
        Raises :class:`StaleWriteError` if every attempt loses the race — the
        caller decides whether that is a 409 or a terminal job failure.
        """
        for attempt in range(attempts):
            with get_session() as sess:
                project = self.get_project(sess, user_info_id, name)
                if project is None:
                    return None
                mutate(project)
                try:
                    self.save_project(
                        sess, user_info_id, project,
                        check_version=True, activity_user_id=activity_user_id,
                    )
                    return project
                except StaleWriteError:
                    if attempt == attempts - 1:
                        _log.warning(
                            "save_project_with_retry gave up on project=%r after %d attempts",
                            name, attempts)
                        raise
            time.sleep(_backoff(attempt))
        raise AssertionError("unreachable")  # pragma: no cover
