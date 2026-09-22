import json
import os

# Secrets may be supplied by environment variable instead of config.json, and the
# environment always wins. This keeps tokens out of a file the customer might
# paste into a support ticket.
_ENV_OVERRIDES = {
    "QASE_API_TOKEN": "qase.api_token",
    "QASE_SCIM_TOKEN": "qase.scim_token",
    "ZEPHYR_ENTERPRISE_API_TOKEN": "zephyr.api_token",
    "ZEPHYR_ENTERPRISE_PASSWORD": "zephyr.password",
}


class ConfigError(Exception):
    """Raised when config.json is missing or unparseable.

    Both cases fail here rather than leaving an empty config behind, so a typo
    in the file surfaces immediately instead of as a 401 half an hour into a
    migration.
    """


class ConfigManager:

    def __init__(self, config_file="./config.json", env_vars_prefix="QASE_"):
        self.config_file = config_file
        self.env_vars_prefix = env_vars_prefix
        self.config = {}

    def load_config(self):
        """Load config.json, then apply environment overrides.

        Raises ConfigError if the file is missing or is not valid JSON.
        """
        if not os.path.exists(self.config_file):
            raise ConfigError(
                f"Config file not found: {self.config_file}. "
                f"Copy config.example.json to config.json and fill in your tokens."
            )
        try:
            with open(self.config_file, "r", encoding="utf-8") as file:
                loaded = json.load(file)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Config file {self.config_file} is not valid JSON: {e}") from e
        except OSError as e:
            raise ConfigError(f"Config file {self.config_file} could not be read: {e}") from e

        if not isinstance(loaded, dict):
            raise ConfigError(
                f"Config file {self.config_file} must contain a JSON object, "
                f"got {type(loaded).__name__}"
            )

        self.config = loaded
        self._apply_env_overrides()
        return self.config

    def _apply_env_overrides(self):
        for env_var, key in _ENV_OVERRIDES.items():
            value = os.environ.get(env_var)
            if value is not None and value.strip():
                self._set_config(key, value.strip())

    def get(self, key, default=None):
        """Dot-path lookup like ``qase.host``. Missing keys return ``default``."""
        keys = key.split(".")
        config = self.config
        for k in keys[:-1]:
            if not isinstance(config, dict) or k not in config:
                return default
            config = config[k]
        if not isinstance(config, dict):
            return default
        last = keys[-1]
        if last not in config:
            return default
        return config[last]

    def _set_config(self, key, value):
        keys = key.split(".")
        config = self.config
        for k in keys[:-1]:
            config = config.setdefault(k, {})
        config[keys[-1]] = value
