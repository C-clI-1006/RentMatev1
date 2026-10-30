"""Atomic in-memory model settings; environment remains the startup default.

All adapters take a single immutable snapshot per call. A settings update can
never mix a previous endpoint with a new key. Nothing here writes credentials
to disk, changes domain permissions or makes a model request.
"""
from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class ModelConfiguration:
    base_url: str
    model: str
    api_key: str = field(repr=False)


_runtime_configuration: ModelConfiguration | None = None


def get_configuration() -> ModelConfiguration:
    override = _runtime_configuration
    return override if override is not None else ModelConfiguration(
        base_url=os.getenv("LLM_BASE_URL", "").strip(),
        model=os.getenv("LLM_MODEL", "").strip(),
        api_key=os.getenv("LLM_API_KEY", "").strip(),
    )


def set_configuration(configuration: ModelConfiguration | None) -> None:
    global _runtime_configuration
    _runtime_configuration = configuration


def configuration_source() -> str:
    return "runtime" if _runtime_configuration is not None else "environment"
