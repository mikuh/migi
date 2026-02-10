from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


ENV_API_KEY = "GUI_VISION_API_KEY"
ENV_MODEL = "GUI_VISION_MODEL"
ENV_BASE_URL = "GUI_VISION_BASE_URL"
ENV_ACTION_PARSER = "GUI_VISION_ACTION_PARSER"
ENV_ACTION_PARSER_CALLABLE = "GUI_VISION_ACTION_PARSER_CALLABLE"
ENV_CONFIG_PATH = "MIGI_CONFIG_PATH"


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "migi" / "config.json"
    return Path.home() / ".config" / "migi" / "config.json"


def user_fallback_config_path() -> Path:
    return Path.home() / ".migi" / "config.json"


def legacy_local_config_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / ".migi" / "config.json"


def candidate_config_paths(cwd: Path | None = None) -> list[Path]:
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return [Path(env_path).expanduser(), default_config_path(), user_fallback_config_path(), legacy_local_config_path(cwd)]
    return [default_config_path(), user_fallback_config_path(), legacy_local_config_path(cwd)]


def resolve_config_path(path: Path | None = None, cwd: Path | None = None) -> Path:
    if path is not None:
        return path
    candidates = candidate_config_paths(cwd)
    for item in candidates:
        if item.exists():
            return item
    return candidates[0]


@dataclass
class MigiConfig:
    provider: str = "openai-compatible"
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    action_parser: str = "doubao"
    action_parser_callable: str | None = None

    def redacted(self) -> dict[str, Any]:
        api_key = self.api_key or ""
        return {
            "provider": self.provider,
            "api_key": _mask_secret(api_key),
            "model": self.model,
            "base_url": self.base_url,
            "action_parser": self.action_parser,
            "action_parser_callable": self.action_parser_callable,
        }


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def load_file_config(path: Path | None = None, cwd: Path | None = None) -> MigiConfig:
    path = resolve_config_path(path=path, cwd=cwd)
    if not path.exists():
        return MigiConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return MigiConfig(
        provider=raw.get("provider", "openai-compatible"),
        api_key=raw.get("api_key"),
        model=raw.get("model"),
        base_url=raw.get("base_url"),
        action_parser=raw.get("action_parser", "doubao"),
        action_parser_callable=raw.get("action_parser_callable"),
    )


def save_file_config(config: MigiConfig, path: Path | None = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def resolve_runtime_config(
    cli_api_key: str | None = None,
    cli_model: str | None = None,
    cli_base_url: str | None = None,
    cli_provider: str | None = None,
    cli_action_parser: str | None = None,
    cli_action_parser_callable: str | None = None,
    path: Path | None = None,
    cwd: Path | None = None,
) -> tuple[MigiConfig, dict[str, str]]:
    file_cfg = load_file_config(path=path, cwd=cwd)
    sources: dict[str, str] = {}

    provider = cli_provider or file_cfg.provider or "openai-compatible"
    sources["provider"] = "cli" if cli_provider else ("config" if file_cfg.provider else "default")

    if cli_api_key:
        api_key = cli_api_key
        sources["api_key"] = "cli"
    elif os.environ.get(ENV_API_KEY):
        api_key = os.environ[ENV_API_KEY]
        sources["api_key"] = "env"
    else:
        api_key = file_cfg.api_key
        sources["api_key"] = "config" if file_cfg.api_key else "unset"

    if cli_model:
        model = cli_model
        sources["model"] = "cli"
    elif os.environ.get(ENV_MODEL):
        model = os.environ[ENV_MODEL]
        sources["model"] = "env"
    else:
        model = file_cfg.model
        sources["model"] = "config" if file_cfg.model else "unset"

    if cli_base_url:
        base_url = cli_base_url
        sources["base_url"] = "cli"
    elif os.environ.get(ENV_BASE_URL):
        base_url = os.environ[ENV_BASE_URL]
        sources["base_url"] = "env"
    else:
        base_url = file_cfg.base_url
        sources["base_url"] = "config" if file_cfg.base_url else "unset"

    if cli_action_parser:
        action_parser = cli_action_parser
        sources["action_parser"] = "cli"
    elif os.environ.get(ENV_ACTION_PARSER):
        action_parser = os.environ[ENV_ACTION_PARSER]
        sources["action_parser"] = "env"
    else:
        action_parser = file_cfg.action_parser or "doubao"
        sources["action_parser"] = "config" if file_cfg.action_parser else "default"

    if cli_action_parser_callable:
        action_parser_callable = cli_action_parser_callable
        sources["action_parser_callable"] = "cli"
    elif os.environ.get(ENV_ACTION_PARSER_CALLABLE):
        action_parser_callable = os.environ[ENV_ACTION_PARSER_CALLABLE]
        sources["action_parser_callable"] = "env"
    else:
        action_parser_callable = file_cfg.action_parser_callable
        sources["action_parser_callable"] = "config" if file_cfg.action_parser_callable else "unset"

    return MigiConfig(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        action_parser=action_parser,
        action_parser_callable=action_parser_callable,
    ), sources

