"""Custom exceptions for TraxJourney application."""


class TraxJourneyException(Exception):
    """Base exception for TraxJourney."""

    pass


class ConfigurationError(TraxJourneyException):
    """Raised when configuration is invalid or missing."""

    pass


class AuthenticationError(TraxJourneyException):
    """Raised when authentication fails."""

    pass


class APIError(TraxJourneyException):
    """Raised when Strava API returns an error."""

    pass


class RateLimitError(APIError):
    """Raised when the app's own Strava quota window is full.

    A subclass of APIError so the existing app-level handler still maps it to a
    502 "integration temporarily unavailable" — the client-facing outcome is the
    same, but no request is sent to Strava at all.
    """

    pass


class QuotaExceeded(TraxJourneyException):
    """Raised when an action would push a user past their plan's limits.

    Carries the numbers so the API handler can turn it into a 402 the client can
    render ("2 of 1 trips used") without a second round trip.
    """

    def __init__(self, message: str, *, plan: str, limit: int | None,
                 used: int, resource: str, needed: int | None = None):
        super().__init__(message)
        self.plan = plan
        self.limit = limit
        self.used = used
        self.resource = resource  # "projects" | "storage" | "trip_days"
        # What the refused action would have needed. Lets the client recommend
        # the cheapest plan that actually covers it, rather than the priciest.
        self.needed = needed if needed is not None else used


class TokenError(TraxJourneyException):
    """Raised when token management fails."""

    pass


class ValidationError(TraxJourneyException):
    """Raised when data validation fails."""

    pass


class ExportError(TraxJourneyException):
    """Raised when export operation fails."""

    pass


class GPXError(TraxJourneyException):
    """Raised when GPX processing fails."""

    pass
