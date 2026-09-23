"""Configuration management for TraxJourney."""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from src.exceptions.errors import ConfigurationError


class Config:
    """Configuration management for TraxJourney."""

    DEFAULT_CONFIG = {
        "strava": {
            "client_id": "",
            "client_secret": "",
            "redirect_uri": "http://localhost:8000/callback",
        },
        "mapbox": {
            "token": "",
        },
        "app": {
            "debug": False,
            "log_level": "INFO",
            "cache_dir": "cache",
            "logs_dir": "logs",
        },
    }

    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize configuration.

        Args:
            config_file: Path to config file (uses default if not provided)

        Raises:
            ConfigurationError: If config file is invalid
        """
        if config_file:
            self.config_file = Path(config_file)
        else:
            # Try multiple common locations
            possible_paths = [
                Path("config/config.json"),  # config/ subdirectory
                Path("config.json"),           # current directory
                Path(".") / "config" / "config.json",  # relative to project
            ]
            
            self.config_file = None
            for path in possible_paths:
                if path.exists():
                    self.config_file = path
                    break
            
            # Default to config/config.json if none found
            if self.config_file is None:
                self.config_file = Path("config/config.json")
        
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """
        Load configuration from file or use defaults.

        Raises:
            ConfigurationError: If JSON is invalid
        """
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    self._config = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigurationError(f"Invalid JSON in config file: {e}")
        else:
            self._config = self.DEFAULT_CONFIG.copy()
            self.save()

    def save(self) -> None:
        """Save current configuration to file."""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w") as f:
            json.dump(self._config, f, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.

        Args:
            key: Configuration key (e.g., 'strava.client_id')
            default: Default value if key not found

        Returns:
            Configuration value
        """
        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        """
        Set configuration value using dot notation.

        Args:
            key: Configuration key (e.g., 'strava.client_id')
            value: Value to set
        """
        keys = key.split(".")
        config = self._config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def validate_strava_config(self) -> bool:
        """
        Validate Strava configuration.

        Returns:
            True if valid Strava config exists
        """
        client_id = self.get("strava.client_id")
        client_secret = self.get("strava.client_secret")
        return bool(client_id and client_secret)