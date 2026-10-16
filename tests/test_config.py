"""Unit tests for configuration management."""

import json
import tempfile
from pathlib import Path

import pytest

from src.config.settings import Config
from src.exceptions.errors import ConfigurationError


class TestConfig:
    """Test configuration management."""

    def test_config_default_values(self):
        """Test that default config values are set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            assert config.config_file == config_file
            assert config.get("strava.redirect_uri") == "http://localhost:8000/callback"
            assert config.get("app.debug") == False
            assert config.get("app.log_level") == "INFO"

    def test_config_load_from_file(self):
        """Test loading config from existing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"

            # Create a config file
            test_config = {
                "strava": {"client_id": "test_id"},
                "app": {"debug": True},
            }
            with open(config_file, "w") as f:
                json.dump(test_config, f)

            config = Config(str(config_file))
            assert config.get("strava.client_id") == "test_id"
            assert config.get("app.debug") == True

    def test_config_save_to_file(self):
        """Test saving config to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("strava.client_id", "my_client_id")
            config.save()

            # Verify file was created and contains the data
            assert config_file.exists()
            with open(config_file, "r") as f:
                data = json.load(f)
            assert data["strava"]["client_id"] == "my_client_id"

    def test_missing_config_file_is_not_created(self):
        """Reading the config must not write one (#638): importing the API
        used to create config/config.json in the working directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            assert config.get("app.log_level") == "INFO"
            assert not config_file.exists()

    def test_set_does_not_leak_into_another_instance(self):
        """api/strava.py sets credentials from env on its own Config; a fresh
        default Config must not inherit them (#638)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Config(str(Path(tmpdir) / "a.json"))
            first.set("strava.client_id", "real-id")

            second = Config(str(Path(tmpdir) / "b.json"))
            assert second.get("strava.client_id") == ""
            assert Config.DEFAULT_CONFIG["strava"]["client_id"] == ""

    def test_config_set_and_get(self):
        """Test setting and getting config values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("strava.client_id", "new_id")
            assert config.get("strava.client_id") == "new_id"

            config.set("app.debug", True)
            assert config.get("app.debug") == True

    def test_config_dot_notation(self):
        """Test getting values with dot notation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("level1.level2.level3", "deep_value")
            assert config.get("level1.level2.level3") == "deep_value"

    def test_config_default_return_value(self):
        """Test that default value is returned for missing keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            result = config.get("nonexistent.key", "default_value")
            assert result == "default_value"

    def test_config_get_none_for_missing(self):
        """Test that None is returned for missing keys without default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            result = config.get("nonexistent.key")
            assert result is None

    def test_config_invalid_json(self):
        """Test that ConfigurationError is raised for invalid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"

            # Create an invalid JSON file
            with open(config_file, "w") as f:
                f.write("{invalid json}")

            with pytest.raises(ConfigurationError):
                Config(str(config_file))

    def test_config_validate_strava_config_valid(self):
        """Test validation of valid Strava config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("strava.client_id", "id")
            config.set("strava.client_secret", "secret")

            assert config.validate_strava_config() == True

    def test_config_validate_strava_config_missing_id(self):
        """Test validation fails when client_id is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("strava.client_id", "")
            config.set("strava.client_secret", "secret")

            assert config.validate_strava_config() == False

    def test_config_validate_strava_config_missing_secret(self):
        """Test validation fails when client_secret is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config = Config(str(config_file))

            config.set("strava.client_id", "id")
            config.set("strava.client_secret", "")

            assert config.validate_strava_config() == False

    def test_config_create_parent_directory(self):
        """Test that parent directories are created when saving."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "subdir" / "nested" / "config.json"
            config = Config(str(config_file))

            config.save()
            assert config_file.parent.exists()
            assert config_file.exists()