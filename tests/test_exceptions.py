"""Unit tests for custom exceptions."""

import pytest

from src.exceptions.errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    ExportError,
    TraxJourneyException,
    GPXError,
    TokenError,
    ValidationError,
)


class TestExceptions:
    """Test custom exception classes."""

    def test_base_exception(self):
        """Test TraxJourneyException can be raised and caught."""
        with pytest.raises(TraxJourneyException):
            raise TraxJourneyException("Test error")

    def test_configuration_error(self):
        """Test ConfigurationError is a TraxJourneyException."""
        error = ConfigurationError("Config error")
        assert isinstance(error, TraxJourneyException)
        with pytest.raises(ConfigurationError):
            raise error

    def test_authentication_error(self):
        """Test AuthenticationError is a TraxJourneyException."""
        error = AuthenticationError("Auth failed")
        assert isinstance(error, TraxJourneyException)

    def test_api_error(self):
        """Test APIError is a TraxJourneyException."""
        error = APIError("API call failed")
        assert isinstance(error, TraxJourneyException)

    def test_token_error(self):
        """Test TokenError is a TraxJourneyException."""
        error = TokenError("Token expired")
        assert isinstance(error, TraxJourneyException)

    def test_validation_error(self):
        """Test ValidationError is a TraxJourneyException."""
        error = ValidationError("Invalid data")
        assert isinstance(error, TraxJourneyException)

    def test_export_error(self):
        """Test ExportError is a TraxJourneyException."""
        error = ExportError("Export failed")
        assert isinstance(error, TraxJourneyException)

    def test_gpx_error(self):
        """Test GPXError is a TraxJourneyException."""
        error = GPXError("GPX parse error")
        assert isinstance(error, TraxJourneyException)

    def test_exception_message(self):
        """Test exception message is preserved."""
        message = "Test error message"
        error = TraxJourneyException(message)
        assert str(error) == message
