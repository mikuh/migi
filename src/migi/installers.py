from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Iterable


KNOWN_TARGETS = ("cursor", "claude-code", "open-code", "neostream", "lingxibox")
SKILL_NAME = "migi"
SKILL_ASSET_DIR = "migi-desktop"


def _home(path: str) -> Path:
    return Path(path).expanduser()


def candidate_roots(target: str) -> list[Path]:
    mapping: dict[str, list[Path]] = {
        "cursor": [
            _home("~/.cursor"),
            _home("~/Library/Application Support/Cursor/User"),
            _home("~/.config/Cursor"),
        ],
        "claude-code": [
            _home("~/.claude"),
            _home("~/.config/claude-code"),
        ],
        "open-code": [
            _home("~/.opencode"),
            _home("~/.config/opencode"),
        ],
        "neostream": [
            _home("~/.neostream"),
            _home("~/.config/neostream"),
        ],
        "lingxibox": [
            _home("~/.lingxibox"),
            _home("~/.config/lingxibox"),
            _home("~/codes/lingxibox-1"),
        ],
    }
    return mapping.get(target, [])


def resolve_target_dir(target: str) -> Path:
    for root in candidate_roots(target):
        if root.exists():
            if root.name == "skills":
                return root
            return root / "skills"
    roots = candidate_roots(target)
    if roots:
        base = roots[0]
        if base.name == "skills":
            return base
        return base / "skills"
    raise ValueError(f"Unknown target: {target}")


def skill_asset_dir() -> Path:
    # Keep source asset folder stable while exposing a cleaner installed skill name.
    return Path(__file__).resolve().parent / "assets" / SKILL_ASSET_DIR


@dataclass
class InstallItemResult:
    name: str
    path: str
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
        }


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _symlink_or_copy(src: Path, dest: Path) -> tuple[str, str | None]:
    try:
        if dest.exists() or dest.is_symlink():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.symlink_to(src, target_is_directory=True)
        return "installed", "symlink"
    except OSError:
        _copy_tree(src, dest)
        return "installed", "copied"


def install_skill_to_path(
    target_name: str,
    skills_dir: Path,
    dry_run: bool = False,
) -> InstallItemResult:
    src = skill_asset_dir()
    dest = skills_dir / SKILL_NAME
    if dry_run:
        return InstallItemResult(name=target_name, path=str(dest), status="planned", reason="dry-run")
    skills_dir.mkdir(parents=True, exist_ok=True)
    status, reason = _symlink_or_copy(src, dest)
    return InstallItemResult(name=target_name, path=str(dest), status=status, reason=reason)


def resolve_targets(requested_target: str | None, custom_path: str | None) -> list[tuple[str, Path]]:
    if custom_path:
        return [("custom", Path(custom_path).expanduser())]
    if not requested_target or requested_target == "all":
        return [(name, resolve_target_dir(name)) for name in KNOWN_TARGETS]
    return [(requested_target, resolve_target_dir(requested_target))]


def install_many(
    targets: Iterable[tuple[str, Path]],
    dry_run: bool = False,
) -> list[InstallItemResult]:
    results: list[InstallItemResult] = []
    for name, path in targets:
        try:
            results.append(install_skill_to_path(name, path, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            results.append(InstallItemResult(name=name, path=str(path / SKILL_NAME), status="failed", reason=str(exc)))
    return results

