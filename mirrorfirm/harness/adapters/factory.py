"""Non-secret provider-adapter resolution for the WP-13 CLI and sweeps."""

from __future__ import annotations

import os
from collections.abc import Callable

from .base import ModelAdapter


class AdapterResolutionError(ValueError):
    """A requested live provider model cannot be safely constructed."""


AdapterFactory = Callable[[str], ModelAdapter]


_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
}


def known_provider(model: str) -> str:
    """Return the declared provider prefix without ever inspecting secret values."""

    provider, separator, name = model.partition("/")
    if not separator or not name or provider not in _CREDENTIALS:
        raise AdapterResolutionError(
            "model must use a known provider prefix: " + ", ".join(sorted(_CREDENTIALS))
        )
    return provider


def credentials_available(model: str) -> bool:
    """Check only whether the provider's required environment variable is set."""

    return any(os.environ.get(name) for name in _CREDENTIALS[known_provider(model)])


def resolve_live_adapter(model: str) -> ModelAdapter:
    """Construct one known live provider only after the credential preflight passes."""

    provider = known_provider(model)
    if not credentials_available(model):
        raise AdapterResolutionError(
            f"credentials unavailable for requested {provider} model"
        )
    _, _, provider_model = model.partition("/")
    factories: dict[str, AdapterFactory] = {
        "openai": _openai,
        "anthropic": _anthropic,
        "google": _google,
        "mistral": _mistral,
        "fireworks": _fireworks,
    }
    return factories[provider](provider_model)


def _openai(model: str) -> ModelAdapter:
    from .openai import OpenAIAdapter

    return OpenAIAdapter(model)


def _anthropic(model: str) -> ModelAdapter:
    from .anthropic import AnthropicAdapter

    return AnthropicAdapter(model)


def _google(model: str) -> ModelAdapter:
    from .google import GoogleAdapter

    return GoogleAdapter(model)


def _mistral(model: str) -> ModelAdapter:
    from .mistral import MistralAdapter

    return MistralAdapter(model)


def _fireworks(model: str) -> ModelAdapter:
    from .fireworks import FireworksAdapter

    return FireworksAdapter(model)
