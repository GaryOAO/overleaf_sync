"""Core sync, merge, and conflict orchestration."""

from __future__ import annotations

import difflib
import fnmatch
import io
import posixpath
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import click

from overleaf_sync.local_state import (
    clear_conflict_entry,
    file_sha256,
    load_conflict_entries,
    read_base_snapshot_map,
    read_conflict_snapshot,
    remove_base_snapshot,
    replace_base_snapshot,
    set_conflict_entry,
    update_base_snapshot_from_local_paths,
    write_base_snapshot,
)


LARGE_FILE_WARNING_BYTES = 10 * 1024 * 1024


@dataclass
class SyncProgressTracker:
    total: int
    current: int = 0

    def step(self, label: str) -> str:
        self.current += 1
        return f"[{self.current}/{self.total} {label}]"


class RemoteZipDownloadError(click.ClickException):
    """Raised when Overleaf's project archive cannot be downloaded reliably."""


def format_byte_size(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def ignore_patterns(ovsignore_path: Path) -> list[str]:
    if ovsignore_path.is_file():
        return [line.strip() for line in ovsignore_path.read_text().splitlines() if line.strip()]
    return []


def should_ignore(rel_path: str, patterns: list[str]) -> bool:
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith(".git/") or rel_path == ".git":
        return True
    if any(part.startswith(".") for part in rel_path.split("/")):
        return True
    if rel_path == "output" or rel_path.startswith("output/"):
        return True
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def collect_local_files(sync_path: Path, patterns: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for file_path in sync_path.rglob("*"):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(sync_path).as_posix()
        if should_ignore(rel_path, patterns):
            continue
        result[rel_path] = file_path
    return result


def replace_base_snapshot_from_local(root: Path, sync_root: Path, ovsignore_path: Path) -> None:
    patterns = ignore_patterns(ovsignore_path)
    local_files = collect_local_files(sync_root, patterns)
    replace_base_snapshot(root, {rel_path: local_path.read_bytes() for rel_path, local_path in local_files.items()})


def normalize_stage_path(sync_root: Path, value: str) -> str:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        candidate = (Path.cwd() / raw_path).resolve()
    try:
        return candidate.relative_to(sync_root).as_posix()
    except ValueError as exc:
        raise click.ClickException(f"Path '{value}' is outside the bound sync root {sync_root}.") from exc


def zip_map(zip_bytes: bytes) -> dict[str, bytes]:
    file_map: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            file_map[info.filename] = archive.read(info.filename)
    return file_map


def normalize_text_content(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_text_bytes(content: bytes) -> str:
    return normalize_text_content(content.decode("utf-8-sig"))


def encode_text_content(text: str) -> bytes:
    return normalize_text_content(text).encode("utf-8")


def is_text_bytes(content: bytes | None) -> bool:
    if content is None:
        return True
    if b"\x00" in content:
        return False
    try:
        decode_text_bytes(content)
    except UnicodeDecodeError:
        return False
    return True


def read_local_text(local_path: Path) -> str:
    return normalize_text_content(local_path.read_text(encoding="utf-8-sig"))


def repair_socket_text(text: str) -> str:
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text

    try:
        if repaired.encode("utf-8").decode("latin-1") == text:
            return repaired
    except UnicodeError:
        return text
    return text


def snapshot_lines_to_text(lines: list[str]) -> str:
    return "\n".join(repair_socket_text(line) for line in lines)


def render_conflict_text(local_text: str, remote_text: str, *, local_label: str = "local", remote_label: str = "remote") -> str:
    local_block = local_text.rstrip("\n")
    remote_block = remote_text.rstrip("\n")
    return (
        f"<<<<<<< {local_label}\n"
        f"{local_block}\n"
        "=======\n"
        f"{remote_block}\n"
        f">>>>>>> {remote_label}\n"
    )


def merge_text_three_way(base_text: str, local_text: str, remote_text: str) -> tuple[str, bool]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        local_path = tmp_root / "local.txt"
        base_path = tmp_root / "base.txt"
        remote_path = tmp_root / "remote.txt"
        local_path.write_text(normalize_text_content(local_text), encoding="utf-8")
        base_path.write_text(normalize_text_content(base_text), encoding="utf-8")
        remote_path.write_text(normalize_text_content(remote_text), encoding="utf-8")
        try:
            result = subprocess.run(
                ["git", "merge-file", "-p", str(local_path), str(base_path), str(remote_path)],
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise click.ClickException("Git is required for three-way text merges, but `git` was not found.") from exc
        if result.returncode not in {0, 1}:
            message = result.stderr.strip() or result.stdout.strip() or "git merge-file failed."
            raise click.ClickException(message)
        return result.stdout, result.returncode == 0


def build_text_components(current_text: str, target_text: str) -> list[dict]:
    if current_text == target_text:
        return []

    matcher = difflib.SequenceMatcher(a=current_text, b=target_text, autojunk=False)
    components = []
    for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag == "equal":
            continue
        if tag in ("delete", "replace") and i1 != i2:
            components.append({"p": i1, "d": current_text[i1:i2]})
        if tag in ("insert", "replace") and j1 != j2:
            components.append({"p": i1, "i": target_text[j1:j2]})
    return components


def collect_folder_paths(file_map: dict[str, object]) -> set[str]:
    folders = set()
    for rel_path in file_map:
        folder_path = posixpath.dirname(rel_path)
        while folder_path:
            folders.add(folder_path)
            folder_path = posixpath.dirname(folder_path)
    return folders


def build_destructive_sync_warnings(plan: dict[str, list[str]], local_only: bool, remote_only: bool) -> list[str]:
    warnings = []
    if local_only:
        remote_files = len(plan["remote_delete"])
        remote_folders = len(plan["remote_delete_folders"])
        if remote_files or remote_folders:
            warnings.append(
                "Local-only sync will delete "
                f"{remote_files} remote file(s) and {remote_folders} remote folder(s) not present locally."
            )
    if remote_only:
        local_files = len(plan["local_delete"])
        if local_files:
            warnings.append(f"Remote-only sync will delete {local_files} local file(s) not present remotely.")
    return warnings


def warn_for_large_upload(local_path: Path, rel_path: str) -> None:
    size = local_path.stat().st_size
    if size < LARGE_FILE_WARNING_BYTES:
        return
    click.echo(
        f"[WARN] Large upload detected: {rel_path} ({format_byte_size(size)}). "
        "Upload may take longer; progress is shown below as sync steps complete."
    )


def make_progress_tracker(plan: dict[str, list[str]], push_updates: list[str], pull_updates: list[str]) -> SyncProgressTracker | None:
    total = (
        len(plan["local_delete"])
        + len(plan["remote_delete"])
        + len(pull_updates)
        + len(push_updates)
        + len(plan["remote_delete_folders"])
    )
    if total == 0:
        return None
    return SyncProgressTracker(total=total)


def progress_prefix(progress: SyncProgressTracker | None, label: str) -> str:
    if progress is None:
        return f"[{label}]"
    return progress.step(label)


def ensure_local_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_local_file(path: Path, content: bytes) -> None:
    ensure_local_dir(path)
    path.write_bytes(content)


def remove_local_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def ensure_remote_folder(session: Any, project_id: str, folders: dict[str, dict], root_folder_id: str, folder_path: str) -> str:
    if not folder_path:
        return root_folder_id

    current_path = ""
    parent_folder_id = root_folder_id
    for part in folder_path.split("/"):
        current_path = part if not current_path else f"{current_path}/{part}"
        existing = folders.get(current_path)
        if existing is None:
            created = session.create_folder(project_id, parent_folder_id, part)
            existing = {
                "kind": "folder",
                "id": created["_id"],
                "path": current_path,
                "parent_folder_id": parent_folder_id,
                "name": part,
            }
            folders[current_path] = existing
        parent_folder_id = existing["id"]
    return parent_folder_id


def prompt_conflict(path: str, local_only: bool, remote_only: bool) -> str:
    if local_only:
        return "local"
    if remote_only:
        return "remote"
    return click.prompt(
        f"Conflict on '{path}'. Keep [l]ocal or [r]emote?",
        type=click.Choice(["l", "r"]),
        default="l",
    )


def file_contents_match(local_path: Path, remote_bytes: bytes, remote_entity: dict | None) -> bool:
    if remote_entity is not None and remote_entity["kind"] == "doc":
        try:
            return read_local_text(local_path) == normalize_text_content(remote_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return local_path.read_bytes() == remote_bytes
    return local_path.read_bytes() == remote_bytes


def collect_sync_state(session: Any, project: dict, sync_path: Path, ovsignore_path: Path) -> dict:
    patterns = ignore_patterns(ovsignore_path)
    local_files = collect_local_files(sync_path, patterns)
    remote_zip = zip_map(session.download_zip(project["id"]))
    remote_folders, remote_entities, root_folder_id = session.extract_tree(project["id"])
    return {
        "local_files": local_files,
        "remote_zip": remote_zip,
        "remote_folders": remote_folders,
        "remote_entities": remote_entities,
        "root_folder_id": root_folder_id,
        "remote_zip_available": True,
    }


def collect_tree_sync_state(session: Any, project: dict, sync_path: Path, ovsignore_path: Path) -> dict:
    patterns = ignore_patterns(ovsignore_path)
    local_files = collect_local_files(sync_path, patterns)
    remote_folders, remote_entities, root_folder_id = session.extract_tree(project["id"])
    return {
        "local_files": local_files,
        "remote_zip": {},
        "remote_folders": remote_folders,
        "remote_entities": remote_entities,
        "root_folder_id": root_folder_id,
        "remote_zip_available": False,
    }


def build_sync_plan(
    local_files: dict[str, Path],
    remote_zip: dict[str, bytes],
    remote_entities: dict[str, dict],
    remote_folders: dict[str, dict],
    local_only: bool,
    remote_only: bool,
) -> dict[str, list[str]]:
    plan = {
        "push_new": [],
        "push_replace": [],
        "pull_new": [],
        "pull_replace": [],
        "local_delete": [],
        "remote_delete": [],
        "remote_delete_folders": [],
        "conflicts": [],
    }

    all_paths = sorted(set(local_files) | set(remote_zip))
    for path in all_paths:
        local_path = local_files.get(path)
        remote_bytes = remote_zip.get(path)
        remote_entity = remote_entities.get(path)

        if local_path and remote_bytes is not None:
            if file_contents_match(local_path, remote_bytes, remote_entity):
                continue
            if local_only:
                plan["push_replace"].append(path)
            elif remote_only:
                plan["pull_replace"].append(path)
            else:
                plan["conflicts"].append(path)
            continue

        if local_path and remote_bytes is None:
            if remote_only:
                plan["local_delete"].append(path)
            else:
                plan["push_new"].append(path)
            continue

        if local_path is None and remote_bytes is not None:
            if local_only:
                plan["remote_delete"].append(path)
            else:
                plan["pull_new"].append(path)

    if local_only:
        desired_folders = collect_folder_paths(local_files)
        for folder_path in sorted(remote_folders, key=lambda item: item.count("/"), reverse=True):
            if folder_path in desired_folders:
                continue
            plan["remote_delete_folders"].append(folder_path)

    return plan


def build_metadata_only_local_push_plan(
    local_files: dict[str, Path],
    remote_entities: dict[str, dict],
    remote_folders: dict[str, dict],
) -> dict[str, list[str]]:
    plan = {
        "push_new": [],
        "push_replace": [],
        "pull_new": [],
        "pull_replace": [],
        "local_delete": [],
        "remote_delete": [],
        "remote_delete_folders": [],
        "conflicts": [],
    }
    desired_folders = collect_folder_paths(local_files)
    for rel_path in sorted(local_files):
        if rel_path in remote_entities:
            plan["push_replace"].append(rel_path)
        else:
            plan["push_new"].append(rel_path)
    for rel_path in sorted(remote_entities):
        if rel_path not in local_files:
            plan["remote_delete"].append(rel_path)
    for folder_path in sorted(remote_folders, key=lambda item: item.count("/"), reverse=True):
        if folder_path not in desired_folders:
            plan["remote_delete_folders"].append(folder_path)
    return plan


def print_sync_plan(plan: dict[str, list[str]]) -> None:
    labels = [
        ("push_new", "[PLAN LOCAL -> REMOTE NEW]"),
        ("push_replace", "[PLAN LOCAL -> REMOTE REPLACE]"),
        ("pull_new", "[PLAN REMOTE -> LOCAL NEW]"),
        ("pull_replace", "[PLAN REMOTE -> LOCAL REPLACE]"),
        ("local_delete", "[PLAN LOCAL DELETE]"),
        ("remote_delete", "[PLAN REMOTE DELETE]"),
        ("remote_delete_folders", "[PLAN REMOTE DELETE FOLDER]"),
        ("conflicts", "[PLAN CONFLICT]"),
    ]
    total = sum(len(plan[key]) for key, _ in labels)
    if total == 0:
        click.echo("No sync actions needed.")
        return

    for key, label in labels:
        for path in plan[key]:
            click.echo(f"{label} {path}")

    click.echo(
        "Summary: "
        + ", ".join(f"{key}={len(plan[key])}" for key, _ in labels if plan[key])
    )


def summarize_sync_plan(plan: dict[str, list[str]]) -> dict[str, int]:
    return {key: len(values) for key, values in plan.items() if values}


def format_sync_plan_summary(plan: dict[str, list[str]]) -> str:
    counts = summarize_sync_plan(plan)
    if not counts:
        return "no actions"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def sync_project_local_only_fallback(
    session: Any,
    project: dict,
    sync_path: Path,
    ovsignore_path: Path,
    error: Exception,
    *,
    realtime_factory: Callable[[Any, str], Any],
) -> None:
    click.echo(f"[WARN] {error}")
    click.echo("[WARN] Falling back to metadata-only local push; matching remote files will be refreshed from local content.")

    state = collect_tree_sync_state(session, project, sync_path, ovsignore_path)
    local_files = state["local_files"]
    remote_folders = state["remote_folders"]
    remote_entities = state["remote_entities"]
    root_folder_id = state["root_folder_id"]
    desired_folders = collect_folder_paths(local_files)
    remote_delete_paths = [path for path in sorted(remote_entities) if path not in local_files]
    remote_delete_folder_paths = [
        folder_path
        for folder_path in sorted(remote_folders, key=lambda item: item.count("/"), reverse=True)
        if folder_path not in desired_folders
    ]
    progress_total = len(remote_delete_paths) + len(local_files) + len(remote_delete_folder_paths)
    progress = SyncProgressTracker(total=progress_total) if progress_total else None

    for warning in build_destructive_sync_warnings(
        {
            "push_new": [],
            "push_replace": [],
            "pull_new": [],
            "pull_replace": [],
            "local_delete": [],
            "remote_delete": remote_delete_paths,
            "remote_delete_folders": remote_delete_folder_paths,
            "conflicts": [],
        },
        True,
        False,
    ):
        click.echo(f"[WARN] {warning}")

    for path in remote_delete_paths:
        entity = remote_entities[path]
        session.delete_entity(project["id"], entity)
        click.echo(f"{progress_prefix(progress, 'REMOTE DELETE')} {path}")

    realtime = None
    try:
        for path in sorted(local_files):
            local_path = local_files[path]
            existing = remote_entities.get(path)
            warn_for_large_upload(local_path, path)

            if existing is not None and existing["kind"] == "doc":
                if realtime is None:
                    realtime = realtime_factory(session, project["id"])
                try:
                    updated = realtime.update_doc(existing["id"], read_local_text(local_path))
                    if updated:
                        click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE OT')} {path}")
                    continue
                except (UnicodeDecodeError, click.ClickException) as exc:
                    click.echo(f"[OT FALLBACK] {path}: {exc}")

            folder_path = posixpath.dirname(path)
            folder_id = ensure_remote_folder(session, project["id"], remote_folders, root_folder_id, folder_path)
            if existing is not None:
                session.delete_entity(project["id"], existing)
            payload = session.upload_file(project["id"], folder_id, local_path)
            remote_entities[path] = {
                "kind": "doc" if payload.get("entity_type") == "doc" else "file",
                "id": payload["entity_id"],
                "path": path,
                "parent_folder_id": folder_id,
                "name": local_path.name,
            }
            click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE')} {path}")
    finally:
        if realtime is not None:
            realtime.close()

    for folder_path in remote_delete_folder_paths:
        session.delete_entity(project["id"], remote_folders[folder_path])
        click.echo(f"{progress_prefix(progress, 'REMOTE DELETE FOLDER')} {folder_path}")


def sync_project(
    session: Any,
    project: dict,
    sync_path: Path,
    ovsignore_path: Path,
    local_only: bool,
    remote_only: bool,
    *,
    realtime_factory: Callable[[Any, str], Any],
) -> None:
    try:
        state = collect_sync_state(session, project, sync_path, ovsignore_path)
    except RemoteZipDownloadError as exc:
        if local_only and not remote_only:
            sync_project_local_only_fallback(
                session,
                project,
                sync_path,
                ovsignore_path,
                exc,
                realtime_factory=realtime_factory,
            )
            return
        raise
    local_files = state["local_files"]
    remote_zip = state["remote_zip"]
    remote_folders = state["remote_folders"]
    remote_entities = state["remote_entities"]
    root_folder_id = state["root_folder_id"]

    plan = build_sync_plan(local_files, remote_zip, remote_entities, remote_folders, local_only, remote_only)
    push_updates = list(plan["push_new"]) + list(plan["push_replace"])
    pull_updates = list(plan["pull_new"]) + list(plan["pull_replace"])

    for path in plan["conflicts"]:
        choice = prompt_conflict(path, local_only, remote_only)
        if choice in ("l", "local"):
            push_updates.append(path)
        else:
            pull_updates.append(path)

    for warning in build_destructive_sync_warnings(plan, local_only, remote_only):
        click.echo(f"[WARN] {warning}")

    progress = make_progress_tracker(plan, push_updates, pull_updates)

    for path in plan["local_delete"]:
        remove_local_file(sync_path / path)
        click.echo(f"{progress_prefix(progress, 'LOCAL DELETE')} {path}")

    for path in plan["remote_delete"]:
        entity = remote_entities.get(path)
        if entity:
            session.delete_entity(project["id"], entity)
            remote_entities.pop(path, None)
            click.echo(f"{progress_prefix(progress, 'REMOTE DELETE')} {path}")

    for path in pull_updates:
        write_local_file(sync_path / path, remote_zip[path])
        click.echo(f"{progress_prefix(progress, 'REMOTE -> LOCAL')} {path}")

    realtime = None
    try:
        for path in push_updates:
            local_path = local_files[path]
            existing = remote_entities.get(path)
            warn_for_large_upload(local_path, path)

            if existing is not None and existing["kind"] == "doc":
                if realtime is None:
                    realtime = realtime_factory(session, project["id"])
                try:
                    updated = realtime.update_doc(existing["id"], read_local_text(local_path))
                    if updated:
                        click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE OT')} {path}")
                    continue
                except (UnicodeDecodeError, click.ClickException) as exc:
                    click.echo(f"[OT FALLBACK] {path}: {exc}")

            folder_path = posixpath.dirname(path)
            folder_id = ensure_remote_folder(session, project["id"], remote_folders, root_folder_id, folder_path)
            if existing is not None:
                session.delete_entity(project["id"], existing)
            payload = session.upload_file(project["id"], folder_id, local_path)
            remote_entities[path] = {
                "kind": "doc" if payload.get("entity_type") == "doc" else "file",
                "id": payload["entity_id"],
                "path": path,
                "parent_folder_id": folder_id,
                "name": local_path.name,
            }
            click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE')} {path}")
    finally:
        if realtime is not None:
            realtime.close()

    for folder_path in plan["remote_delete_folders"]:
        session.delete_entity(project["id"], remote_folders[folder_path])
        click.echo(f"{progress_prefix(progress, 'REMOTE DELETE FOLDER')} {folder_path}")


def push_staged_entries(
    session: Any,
    project: dict,
    sync_root: Path,
    ovsignore_path: Path,
    stage_entries: dict[str, dict[str, str | None]],
    *,
    realtime_factory: Callable[[Any, str], Any],
    on_applied: Callable[[str], None] | None = None,
) -> list[str]:
    state = collect_sync_state(session, project, sync_root, ovsignore_path)
    local_files = state["local_files"]
    remote_zip = state["remote_zip"]
    remote_folders = state["remote_folders"]
    remote_entities = state["remote_entities"]
    root_folder_id = state["root_folder_id"]

    actions: list[tuple[str, str, Path | None, dict | None]] = []
    for rel_path in sorted(stage_entries):
        staged = stage_entries[rel_path]
        local_path = local_files.get(rel_path)
        remote_bytes = remote_zip.get(rel_path)
        remote_entity = remote_entities.get(rel_path)
        current_local_hash = file_sha256(local_path.read_bytes()) if local_path is not None else None
        current_remote_hash = file_sha256(remote_bytes) if remote_bytes is not None else None
        if current_local_hash != staged.get("local_hash"):
            raise click.ClickException(f"Staged local content changed after `ovs add`: {rel_path}. Run `ovs add {rel_path}` again.")
        if current_remote_hash != staged.get("remote_hash"):
            raise click.ClickException(f"Remote content changed after `ovs add`: {rel_path}. Run `ovs pull` or review, then `ovs add {rel_path}` again.")

        if local_path is None:
            if remote_entity is not None:
                actions.append(("delete", rel_path, None, remote_entity))
            continue

        if remote_bytes is not None and file_contents_match(local_path, remote_bytes, remote_entity):
            actions.append(("noop", rel_path, local_path, remote_entity))
            continue
        actions.append(("push", rel_path, local_path, remote_entity))

    progress = SyncProgressTracker(total=len([action for action in actions if action[0] != "noop"])) if any(
        action[0] != "noop" for action in actions
    ) else None
    realtime = None
    pushed: list[str] = []
    try:
        for action, rel_path, local_path, remote_entity in actions:
            if action == "noop":
                pushed.append(rel_path)
                if on_applied is not None:
                    on_applied(rel_path)
                continue
            if action == "delete":
                session.delete_entity(project["id"], remote_entity)
                click.echo(f"{progress_prefix(progress, 'REMOTE DELETE')} {rel_path}")
                pushed.append(rel_path)
                if on_applied is not None:
                    on_applied(rel_path)
                continue

            assert local_path is not None
            warn_for_large_upload(local_path, rel_path)
            if remote_entity is not None and remote_entity["kind"] == "doc":
                if realtime is None:
                    realtime = realtime_factory(session, project["id"])
                try:
                    updated = realtime.update_doc(remote_entity["id"], read_local_text(local_path))
                    if updated:
                        click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE OT')} {rel_path}")
                        pushed.append(rel_path)
                        if on_applied is not None:
                            on_applied(rel_path)
                        continue
                except (UnicodeDecodeError, click.ClickException) as exc:
                    click.echo(f"[OT FALLBACK] {rel_path}: {exc}")

            folder_path = posixpath.dirname(rel_path)
            folder_id = ensure_remote_folder(session, project["id"], remote_folders, root_folder_id, folder_path)
            if remote_entity is not None:
                session.delete_entity(project["id"], remote_entity)
            payload = session.upload_file(project["id"], folder_id, local_path)
            remote_entities[rel_path] = {
                "kind": "doc" if payload.get("entity_type") == "doc" else "file",
                "id": payload["entity_id"],
                "path": rel_path,
                "parent_folder_id": folder_id,
                "name": local_path.name,
            }
            click.echo(f"{progress_prefix(progress, 'LOCAL -> REMOTE')} {rel_path}")
            pushed.append(rel_path)
            if on_applied is not None:
                on_applied(rel_path)
    finally:
        if realtime is not None:
            realtime.close()
    return pushed


def apply_resolve_choice(binding_root: Path, sync_root: Path, rel_path: str, choice: str) -> None:
    if choice == "mark-resolved":
        clear_conflict_entry(binding_root, rel_path)
        return

    snapshot = read_conflict_snapshot(binding_root, "ours" if choice == "ours" else "theirs", rel_path)
    if snapshot is None:
        remove_local_file(sync_root / rel_path)
    else:
        write_local_file(sync_root / rel_path, snapshot)
    clear_conflict_entry(binding_root, rel_path)


def pull_bound_project(
    session: Any,
    project: dict,
    binding_root: Path,
    sync_root: Path,
    ovsignore_path: Path,
) -> None:
    state = collect_sync_state(session, project, sync_root, ovsignore_path)
    local_files = state["local_files"]
    remote_zip = state["remote_zip"]
    base_map = read_base_snapshot_map(binding_root)
    all_paths = sorted(set(local_files) | set(remote_zip) | set(base_map))
    conflicts: list[str] = []

    for rel_path in all_paths:
        clear_conflict_entry(binding_root, rel_path)
        local_path = local_files.get(rel_path)
        local_bytes = local_path.read_bytes() if local_path is not None else None
        remote_bytes = remote_zip.get(rel_path)
        base_bytes = base_map.get(rel_path)

        if local_bytes == remote_bytes:
            continue
        if remote_bytes == base_bytes:
            continue
        if local_bytes == base_bytes:
            if remote_bytes is None:
                remove_local_file(sync_root / rel_path)
                click.echo(f"[REMOTE DELETE] {rel_path}")
            else:
                write_local_file(sync_root / rel_path, remote_bytes)
                click.echo(f"[REMOTE -> LOCAL] {rel_path}")
            continue

        if local_bytes is None and base_bytes is None and remote_bytes is not None:
            write_local_file(sync_root / rel_path, remote_bytes)
            click.echo(f"[REMOTE -> LOCAL NEW] {rel_path}")
            continue
        if local_bytes is not None and remote_bytes is None and base_bytes is None:
            continue

        if not (is_text_bytes(base_bytes) and is_text_bytes(local_bytes) and is_text_bytes(remote_bytes)):
            conflicts.append(rel_path)
            set_conflict_entry(binding_root, rel_path, local_bytes, remote_bytes)
            click.echo(f"[CONFLICT binary] {rel_path}")
            continue

        local_text = decode_text_bytes(local_bytes) if local_bytes is not None else ""
        remote_text = decode_text_bytes(remote_bytes) if remote_bytes is not None else ""
        base_text = decode_text_bytes(base_bytes) if base_bytes is not None else ""
        if remote_bytes is None or local_bytes is None:
            merged_text = render_conflict_text(
                local_text if local_bytes is not None else "",
                remote_text if remote_bytes is not None else "",
                local_label="local",
                remote_label="remote",
            )
            clean = False
        else:
            merged_text, clean = merge_text_three_way(base_text, local_text, remote_text)
        write_local_file(sync_root / rel_path, encode_text_content(merged_text))
        if clean:
            click.echo(f"[MERGED] {rel_path}")
        else:
            conflicts.append(rel_path)
            set_conflict_entry(binding_root, rel_path, local_bytes, remote_bytes)
            click.echo(f"[CONFLICT] {rel_path}")

    replace_base_snapshot(binding_root, remote_zip)
    if conflicts:
        raise click.ClickException(
            f"Pull completed with conflicts in {len(conflicts)} path(s): {', '.join(conflicts)}"
        )
