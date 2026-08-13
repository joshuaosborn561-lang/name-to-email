from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_path() -> Path:
    env_path = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "config" / "finder.yaml",
        env_path.parents[2] / "config" / "finder.yaml",
        env_path.parents[1] / "config" / "finder.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_yaml_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or Path(__import__("os").environ.get("FINDER_CONFIG") or _default_config_path())
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Finder config not found at {config_path}. "
            "Set FINDER_CONFIG or keep config/finder.yaml next to the project."
        )
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("finder.yaml must be a mapping")
    if not data.get("patterns"):
        raise ValueError("finder.yaml must define a non-empty patterns list")
    return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./finder.db"
    millionverifier_api_key: str = ""
    no2bounce_api_key: str = ""
    max_concurrency: int = 5
    default_cost_ceiling: float = 25.0
    finder_config: Path | None = None

    yaml_data: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        settings = cls()
        settings.yaml_data = load_yaml_config(config_path or settings.finder_config)
        return settings

    @property
    def patterns(self) -> list[str]:
        return list(self.yaml_data["patterns"])

    @property
    def max_candidates(self) -> int:
        return int(self.yaml_data.get("max_candidates", 10))

    @property
    def pattern_trust_threshold(self) -> int:
        return int(self.yaml_data.get("pattern_trust_threshold", 2))

    @property
    def catchall_probe_local(self) -> str:
        return str(self.yaml_data.get("catchall", {}).get("probe_local", "zzq-nope-9f3a2b"))

    @property
    def catchall_ttl_days(self) -> int:
        return int(self.yaml_data.get("catchall", {}).get("ttl_days", 30))

    @property
    def personal_domains(self) -> set[str]:
        return {d.lower() for d in self.yaml_data.get("personal_domains", [])}

    @property
    def suffixes(self) -> set[str]:
        return {s.lower() for s in self.yaml_data.get("name", {}).get("suffixes", [])}

    @property
    def credentials(self) -> set[str]:
        return {s.lower() for s in self.yaml_data.get("name", {}).get("credentials", [])}

    @property
    def column_aliases(self) -> dict[str, list[str]]:
        return dict(self.yaml_data.get("columns", {}))

    @property
    def verifier_order(self) -> list[str]:
        return list(self.yaml_data.get("verifiers", {}).get("order", ["millionverifier", "no2bounce"]))

    def verifier_cfg(self, name: str) -> dict[str, Any]:
        return dict(self.yaml_data.get("verifiers", {}).get(name, {}))

    @property
    def vendor_cost_per_email(self) -> float:
        return float(self.yaml_data.get("eval", {}).get("vendor_cost_per_email", 0.01))

    @property
    def holdout_size(self) -> int:
        return int(self.yaml_data.get("eval", {}).get("holdout_size", 50))

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url
