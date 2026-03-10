from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    return Path.home() / "migi" / "config.json"


def resolve_config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    return default_config_path()


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


def load_file_config(path: Path | None = None) -> MigiConfig:
    path = resolve_config_path(path=path)
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
) -> tuple[MigiConfig, dict[str, str]]:
    file_cfg = load_file_config(path=path)
    sources: dict[str, str] = {}

    provider = cli_provider or file_cfg.provider or "openai-compatible"
    sources["provider"] = "cli" if cli_provider else ("config" if file_cfg.provider else "default")

    api_key = cli_api_key or file_cfg.api_key
    sources["api_key"] = "cli" if cli_api_key else ("config" if file_cfg.api_key else "unset")

    model = cli_model or file_cfg.model
    sources["model"] = "cli" if cli_model else ("config" if file_cfg.model else "unset")

    base_url = cli_base_url or file_cfg.base_url
    sources["base_url"] = "cli" if cli_base_url else ("config" if file_cfg.base_url else "unset")

    action_parser = cli_action_parser or file_cfg.action_parser or "doubao"
    sources["action_parser"] = "cli" if cli_action_parser else ("config" if file_cfg.action_parser else "default")

    action_parser_callable = cli_action_parser_callable or file_cfg.action_parser_callable
    sources["action_parser_callable"] = "cli" if cli_action_parser_callable else ("config" if file_cfg.action_parser_callable else "unset")

    return MigiConfig(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        action_parser=action_parser,
        action_parser_callable=action_parser_callable,
    ), sources

