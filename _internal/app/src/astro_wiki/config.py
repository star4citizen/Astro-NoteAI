from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(os.getenv("ASTRO_WIKI_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()
LOCAL_SETTINGS_ENV_KEYS = {
    "provider": "ASTRO_WIKI_LLM_PROVIDER",
    "openai_base_url": "ASTRO_WIKI_OPENAI_BASE_URL",
    "openai_api_key": "ASTRO_WIKI_OPENAI_API_KEY",
    "nasa_ads_api_key": "NASA_ADS_API_KEY",
    "ollama_base_url": "ASTRO_WIKI_OLLAMA_BASE_URL",
    "chat_model": "ASTRO_WIKI_CHAT_MODEL",
    "retrieval_model": "ASTRO_WIKI_RETRIEVAL_MODEL",
    "context_window": "ASTRO_WIKI_CONTEXT_WINDOW",
}
CUSTOM_API_PROVIDER = "openai_compatible"


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def load_yaml(path: str | Path) -> dict[str, Any]:
    full_path = Path(path)
    if not full_path.is_absolute():
        full_path = project_path(str(full_path))
    if not full_path.exists():
        return {}
    with full_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {full_path}")
    return data


def local_settings_path() -> Path:
    return Path(os.getenv("ASTRO_WIKI_LOCAL_SETTINGS_PATH", project_path("data", "config", "local_settings.json"))).resolve()


def load_local_settings() -> dict[str, Any]:
    path = local_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_local_settings(settings: dict[str, Any]) -> None:
    path = local_settings_path()
    ensure_parent(path)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def apply_local_settings_to_env(settings: dict[str, Any] | None = None) -> None:
    settings = settings or load_local_settings()
    for key, env_key in LOCAL_SETTINGS_ENV_KEYS.items():
        value = settings.get(key)
        if value is not None:
            os.environ[env_key] = str(value)


def configured_value(key: str, default: Any = None) -> Any:
    env_key = LOCAL_SETTINGS_ENV_KEYS.get(key)
    if env_key and env_key in os.environ:
        return os.environ[env_key]
    settings = load_local_settings()
    if key in settings:
        return settings[key]
    return default


def db_path() -> Path:
    return project_path(os.getenv("ASTRO_WIKI_DB_PATH", "data/metadata/papers.sqlite"))


def llm_config() -> dict[str, Any]:
    return load_yaml("config/models.yml").get("llm", {})


def api_providers_config() -> dict[str, Any]:
    data = load_yaml("config/models.yml").get("api_providers", {})
    return data if isinstance(data, dict) else {}


def api_provider_names() -> set[str]:
    return {CUSTOM_API_PROVIDER, *api_providers_config().keys()}


def is_api_provider(provider: str | None = None) -> bool:
    return str(provider or llm_provider()).strip() in api_provider_names()


def api_provider_config(provider: str | None = None) -> dict[str, Any]:
    provider_name = str(provider or llm_provider()).strip()
    if provider_name == CUSTOM_API_PROVIDER:
        return {
            "label": "Custom OpenAI-compatible",
            "base_url": llm_config().get("base_url", "http://localhost:8000/v1"),
            "chat_model": "",
            "retrieval_model": "",
            "models": [],
        }
    cfg = api_providers_config().get(provider_name, {})
    return cfg if isinstance(cfg, dict) else {}


def api_provider_label(provider: str | None = None) -> str:
    provider_name = str(provider or llm_provider()).strip()
    cfg = api_provider_config(provider_name)
    return str(cfg.get("label") or provider_name)


def api_provider_default_base_url(provider: str | None = None) -> str:
    cfg = api_provider_config(provider)
    return str(cfg.get("base_url") or llm_config().get("base_url") or "http://localhost:8000/v1")


def api_provider_default_chat_model(provider: str | None = None) -> str:
    cfg = api_provider_config(provider)
    if "chat_model" in cfg:
        return str(cfg.get("chat_model") or "")
    return str(llm_config().get("chat_model") or "")


def api_provider_default_retrieval_model(provider: str | None = None) -> str:
    cfg = api_provider_config(provider)
    if "retrieval_model" in cfg or "chat_model" in cfg:
        return str(cfg.get("retrieval_model") or cfg.get("chat_model") or "")
    return str(llm_config().get("retrieval_model") or "")


def api_provider_model_catalog(provider: str | None = None) -> list[str]:
    models = api_provider_config(provider).get("models", [])
    if not isinstance(models, list):
        return []
    return list(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))


def configured_nonempty_value(key: str, default: Any = None) -> Any:
    value = configured_value(key, None)
    if value not in (None, ""):
        return value
    return default


def llm_provider() -> str:
    cfg = llm_config()
    return str(configured_value("provider", cfg.get("provider", "ollama")))


def openai_base_url() -> str:
    cfg = llm_config()
    default = api_provider_default_base_url() if is_api_provider() else cfg.get("base_url", "http://localhost:8000/v1")
    return str(configured_nonempty_value("openai_base_url", default))


def openai_api_key() -> str:
    cfg = llm_config()
    configured = configured_nonempty_value("openai_api_key", "")
    if configured:
        return str(configured)
    env_names = api_provider_config().get("api_key_env", [])
    if isinstance(env_names, str):
        env_names = [env_names]
    for env_name in env_names:
        value = os.getenv(str(env_name))
        if value:
            return value
    return str(cfg.get("api_key", ""))


def nasa_ads_api_key() -> str:
    return str(configured_nonempty_value("nasa_ads_api_key", os.getenv("NASA_ADS_API_KEY", "")) or "")


def context_window(default: int = 8192) -> int:
    cfg = llm_config()
    value = configured_value("context_window", cfg.get("context_window", default))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ollama_base_url() -> str:
    models = load_yaml("config/models.yml").get("ollama", {})
    return str(configured_value("ollama_base_url", models.get("base_url", "http://localhost:11434")))


def chat_model(default: str = "qwen3:8b") -> str:
    provider = llm_provider()
    if is_api_provider(provider):
        return str(configured_nonempty_value("chat_model", api_provider_default_chat_model(provider) or default))
    models = load_yaml("config/models.yml").get("ollama", {})
    return str(configured_value("chat_model", models.get("chat_model", default)))


def retrieval_model(default: str = "gemma4:e2b") -> str:
    provider = llm_provider()
    if is_api_provider(provider):
        fallback = api_provider_default_retrieval_model(provider) or api_provider_default_chat_model(provider) or default
        return str(configured_nonempty_value("retrieval_model", fallback))
    models = load_yaml("config/models.yml").get("ollama", {})
    return str(configured_value("retrieval_model", models.get("retrieval_model", default)))


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
