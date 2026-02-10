from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

from migi.automation.engine import auto_screen_operation
from migi.config import (
    MigiConfig,
    default_config_path,
    load_file_config,
    resolve_config_path,
    resolve_runtime_config,
    save_file_config,
    user_fallback_config_path,
)
from migi.installers import KNOWN_TARGETS, install_many, resolve_targets
from migi.json_result import ResultBuilder, emit_json


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="migi", description="Task-oriented GUI automation CLI.")
    parser.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    subparsers = parser.add_subparsers(dest="command")

    setup_p = subparsers.add_parser("setup", aliases=["init"], help="Initialize model config.")
    setup_p.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    setup_p.add_argument("--api-key", dest="api_key")
    setup_p.add_argument("--model")
    setup_p.add_argument("--base-url", dest="base_url")
    setup_p.add_argument("--provider", default="openai-compatible")
    setup_p.add_argument("--action-parser", choices=["doubao", "custom"])
    setup_p.add_argument("--action-parser-callable", dest="action_parser_callable")
    setup_p.add_argument("--config-path", dest="config_path")
    setup_p.add_argument("--non-interactive", action="store_true")

    status_p = subparsers.add_parser("status", help="Show effective config status.")
    status_p.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    status_p.add_argument("--config-path", dest="config_path")

    config_p = subparsers.add_parser("config", help="Config helper commands.")
    config_p.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    config_sub = config_p.add_subparsers(dest="config_command")
    config_show = config_sub.add_parser("show", help="Alias for status.")
    config_show.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    config_show.add_argument("--config-path", dest="config_path")

    for cmd_name, help_text in [("see", "Analyze current screen only."), ("act", "Analyze and execute actions.")]:
        run_p = subparsers.add_parser(cmd_name, help=help_text)
        run_p.add_argument("--json", dest="json_mode", choices=["compact", "full"])
        run_p.add_argument("instruction", help="Natural language GUI instruction.")
        run_p.add_argument("--api-key", dest="api_key")
        run_p.add_argument("--model")
        run_p.add_argument("--base-url", dest="base_url")
        run_p.add_argument("--provider")
        run_p.add_argument("--action-parser", choices=["doubao", "custom"])
        run_p.add_argument("--action-parser-callable", dest="action_parser_callable")
        run_p.add_argument("--config-path", dest="config_path")
        run_p.add_argument("--no-exec", action="store_true", help="Disable action execution.")

    install_p = subparsers.add_parser("install", aliases=["install-skill"], help="Install skill package.")
    install_p.add_argument("--json", dest="json_mode", choices=["compact", "full"])
    install_p.add_argument("--target", choices=["all", *KNOWN_TARGETS], default="all")
    install_p.add_argument("--path", dest="custom_path")
    install_p.add_argument("--dry-run", action="store_true")

    return parser


def _canonical_command(args: argparse.Namespace) -> str:
    if args.command in {"init", "setup"}:
        return "setup"
    if args.command in {"install", "install-skill"}:
        return "install"
    if args.command == "config" and getattr(args, "config_command", None) == "show":
        return "status"
    return args.command or "help"


def _prompt_value(prompt: str) -> str | None:
    value = input(prompt).strip()
    return value or None


def _handle_setup(args: argparse.Namespace) -> dict[str, Any]:
    builder = ResultBuilder.start("setup")
    explicit_path = Path(args.config_path).expanduser() if args.config_path else None
    path = explicit_path or default_config_path()

    api_key = args.api_key
    model = args.model
    base_url = args.base_url
    provider = args.provider
    action_parser = args.action_parser
    action_parser_callable = args.action_parser_callable
    updated_fields: list[str] = []

    if not args.non_interactive and sys.stdin.isatty():
        if api_key is None:
            api_key = _prompt_value("GUI_VISION_API_KEY (leave blank to keep current): ")
        if model is None:
            model = _prompt_value("GUI_VISION_MODEL (leave blank to keep current): ")
        if base_url is None:
            base_url = _prompt_value("GUI_VISION_BASE_URL (leave blank to keep current): ")
    if action_parser is not None:
        action_parser = action_parser.strip().lower()
        if action_parser not in {"doubao", "custom"}:
            return builder.fail(
                code="CONFIG_INVALID",
                message="Invalid action parser backend.",
                error_type="ConfigError",
                detail=f"Unsupported action_parser: {action_parser}",
                hint="Use one of: doubao, custom.",
            )

    current = load_file_config(path=resolve_config_path(path=path))
    next_cfg = MigiConfig(
        provider=provider or current.provider,
        api_key=api_key if api_key is not None else current.api_key,
        model=model if model is not None else current.model,
        base_url=base_url if base_url is not None else current.base_url,
        action_parser=action_parser if action_parser is not None else current.action_parser,
        action_parser_callable=(
            action_parser_callable
            if action_parser_callable is not None
            else current.action_parser_callable
        ),
    )
    if current.provider != next_cfg.provider:
        updated_fields.append("provider")
    if current.api_key != next_cfg.api_key:
        updated_fields.append("api_key")
    if current.model != next_cfg.model:
        updated_fields.append("model")
    if current.base_url != next_cfg.base_url:
        updated_fields.append("base_url")
    if current.action_parser != next_cfg.action_parser:
        updated_fields.append("action_parser")
    if current.action_parser_callable != next_cfg.action_parser_callable:
        updated_fields.append("action_parser_callable")

    if next_cfg.action_parser == "custom" and not next_cfg.action_parser_callable:
        return builder.fail(
            code="CONFIG_INVALID",
            message="Custom action parser requires callable path.",
            error_type="ConfigError",
            detail="action_parser is 'custom' but action_parser_callable is empty.",
            hint="Set --action-parser-callable as module:function.",
        )

    fallback_used = False
    fallback_path = user_fallback_config_path()
    try:
        saved_path = save_file_config(next_cfg, path)
    except PermissionError as exc:
        if explicit_path is not None:
            return builder.fail(
                code="CONFIG_WRITE_FAILED",
                message="Failed to write config to explicit path.",
                error_type="PermissionError",
                detail=str(exc),
                hint="Use a writable --config-path.",
                data={"path": str(path)},
            )
        saved_path = save_file_config(next_cfg, fallback_path)
        fallback_used = True
    except OSError as exc:
        return builder.fail(
            code="CONFIG_WRITE_FAILED",
            message="Failed to write config.",
            error_type=exc.__class__.__name__,
            detail=str(exc),
            hint="Use `--config-path` to set a writable location.",
            data={"path": str(path)},
        )
    validation = {
        "api_key_present": bool(next_cfg.api_key),
        "model_present": bool(next_cfg.model),
        "base_url_present": bool(next_cfg.base_url),
    }
    return builder.ok(
        code="CONFIG_UPDATED",
        message="Configuration has been saved.",
        data={
            "config_path": str(saved_path),
            "updated_fields": updated_fields,
            "validation": validation,
            "fallback_used": fallback_used,
            "preferred_path": str(path),
        },
    )


def _dependency_report() -> dict[str, bool]:
    deps = [
        "mss",
        "pyautogui",
        "pyperclip",
        "PIL",
        "ui_tars",
        "httpx",
    ]
    return {name: importlib.util.find_spec(name) is not None for name in deps}


def _handle_status(args: argparse.Namespace) -> dict[str, Any]:
    builder = ResultBuilder.start("status")
    path = Path(args.config_path).expanduser() if args.config_path else resolve_config_path()
    effective, sources = resolve_runtime_config(path=path)
    config_exists = path.exists()
    missing = []
    if not effective.api_key:
        missing.append("api_key")
    if not effective.model:
        missing.append("model")
    if not effective.base_url:
        missing.append("base_url")

    return builder.ok(
        code="STATUS_READY" if not missing else "STATUS_INCOMPLETE",
        message="Status collected.",
        data={
            "config_path": str(path),
            "config_exists": config_exists,
            "effective_config": effective.redacted(),
            "sources": sources,
            "missing_fields": missing,
            "dependencies": _dependency_report(),
        },
    )


def _handle_see_or_act(args: argparse.Namespace, command: str) -> dict[str, Any]:
    builder = ResultBuilder.start(command)
    cfg_path = Path(args.config_path).expanduser() if args.config_path else resolve_config_path()
    effective, sources = resolve_runtime_config(
        cli_api_key=args.api_key,
        cli_model=args.model,
        cli_base_url=args.base_url,
        cli_provider=args.provider,
        cli_action_parser=args.action_parser,
        cli_action_parser_callable=args.action_parser_callable,
        path=cfg_path,
    )
    if not effective.api_key:
        return builder.fail(
            code="CONFIG_MISSING",
            message="Missing api key.",
            error_type="ConfigError",
            detail="GUI_VISION_API_KEY is not configured.",
            hint="Run `migi setup` or provide --api-key.",
            data={"sources": sources},
        )
    if effective.action_parser == "custom" and not effective.action_parser_callable:
        return builder.fail(
            code="CONFIG_INVALID",
            message="Custom action parser requires callable path.",
            error_type="ConfigError",
            detail="action_parser is 'custom' but action_parser_callable is not configured.",
            hint="Run `migi setup --action-parser custom --action-parser-callable module:function`.",
            data={"sources": sources},
        )

    execute_action = False if command == "see" else (not args.no_exec)
    result = auto_screen_operation(
        instruction=args.instruction,
        api_key=effective.api_key,
        model_name=effective.model,
        base_url=effective.base_url,
        action_parser=effective.action_parser,
        action_parser_callable=effective.action_parser_callable,
        execute_action=execute_action,
    ).to_dict()

    if not result["success"]:
        return builder.fail(
            code="ACTION_FAILED",
            message="Automation failed.",
            error_type="AutomationError",
            detail=result.get("error") or "Unknown automation error.",
            hint="Check dependencies and model configuration.",
            data={"result": result},
        )

    return builder.ok(
        code="ACTION_DONE" if execute_action else "ANALYSIS_DONE",
        message="Automation completed.",
        data={
            "instruction": args.instruction,
            "model": {
                "provider": effective.provider,
                "model": effective.model,
                "base_url": effective.base_url,
                "action_parser": effective.action_parser,
                "action_parser_callable": effective.action_parser_callable,
                "sources": sources,
            },
            "analysis": {
                "response": result["response"],
                "action_type": result["action_type"],
            },
            "execution": {
                "executed": execute_action,
                "action_triggered": result["action_triggered"],
                "steps": result["execution_result"] or [],
            },
            "timing": result["timing"],
            "image_size": result["image_size"],
        },
    )


def _handle_install(args: argparse.Namespace) -> dict[str, Any]:
    builder = ResultBuilder.start("install")
    if args.custom_path and args.target != "all":
        targets = resolve_targets(args.target, args.custom_path)
    elif args.custom_path:
        targets = resolve_targets(None, args.custom_path)
    else:
        targets = resolve_targets(args.target, None)

    results = install_many(targets=targets, dry_run=args.dry_run)
    statuses = [item.to_dict() for item in results]
    failed = [item for item in results if item.status == "failed"]
    code = "INSTALL_PARTIAL" if failed else "INSTALL_DONE"
    return builder.ok(
        code=code,
        message="Skill installation completed." if not failed else "Skill installation partially failed.",
        data={
            "mode": "dry-run" if args.dry_run else "apply",
            "targets": statuses,
        },
    )


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    command = _canonical_command(args)
    if command == "setup":
        return _handle_setup(args)
    if command == "status":
        return _handle_status(args)
    if command in {"see", "act"}:
        return _handle_see_or_act(args, command)
    if command == "install":
        return _handle_install(args)
    builder = ResultBuilder.start("help")
    return builder.fail(
        code="USAGE_ERROR",
        message="No command provided.",
        error_type="ArgumentError",
        detail="Run `migi --help` for available commands.",
        hint="Use one of: see, act, setup, install, status.",
    )


def _extract_json_mode(argv: list[str]) -> str:
    mode = "compact"
    for index, token in enumerate(argv):
        if token.startswith("--json="):
            _, value = token.split("=", 1)
            if value in {"compact", "full"}:
                mode = value
        elif token == "--json" and index + 1 < len(argv):
            value = argv[index + 1]
            if value in {"compact", "full"}:
                mode = value
    return mode


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    raw_argv = argv if argv is not None else sys.argv[1:]
    json_mode = _extract_json_mode(raw_argv)
    try:
        args = parser.parse_args(raw_argv)
        payload = _dispatch(args)
        emit_json(payload, mode=json_mode)
        return 0 if payload.get("ok") else 2
    except Exception as exc:  # noqa: BLE001
        payload = ResultBuilder.start("unknown").fail(
            code="UNHANDLED_ERROR",
            message="Command failed before completion.",
            error_type=exc.__class__.__name__,
            detail=str(exc),
            hint="Run `migi --help` or check command arguments.",
        )
        emit_json(payload, mode="compact")
        return 2

