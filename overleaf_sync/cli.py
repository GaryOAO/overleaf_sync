"""Overleaf Sync CLI."""

from __future__ import annotations

import json
import mimetypes
import pickle
import re
import ssl
import time
from pathlib import Path

import click
import requests as reqs
import websocket
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from socketIO_client import SocketIO

from overleaf_sync.git_bridge import (
    BRIDGE_CONFIG_NAME,
    BRIDGE_CONFIG_VERSION,
    DEFAULT_GIT_REMOTE,
    BridgeConfig,
    GitStatusSummary,
    bridge_config_path,
    collect_git_status,
    detect_default_branch,
    display_store_config_path,
    find_bound_root,
    find_repo_root,
    git_remote_url,
    has_meaningful_git_changes,
    load_bridge_config,
    normalize_bridge_path,
    normalize_store_config_path,
    parse_git_status_porcelain,
    require_clean_worktree,
    require_default_branch,
    require_repo_binding,
    resolve_repo_path,
    run_git_command,
    write_bridge_config,
)
from overleaf_sync.local_state import (
    BASE_SNAPSHOT_DIR,
    CONFLICT_SNAPSHOT_DIR,
    CONFLICT_STATE_FILE,
    STAGE_FILE_NAME,
    file_sha256,
    load_conflict_entries,
    load_stage_entries,
    print_conflict_entries,
    print_staged_entries,
    read_base_snapshot_map,
    replace_base_snapshot,
    require_no_unresolved_conflicts,
    save_conflict_entries,
    save_stage_entries,
    set_conflict_entry,
    update_base_snapshot_from_local_paths,
    write_base_snapshot,
)
from overleaf_sync.sync_engine import (
    RemoteZipDownloadError,
    build_metadata_only_local_push_plan,
    build_destructive_sync_warnings,
    build_sync_plan,
    build_text_components,
    collect_sync_state,
    collect_tree_sync_state,
    ensure_local_dir,
    format_sync_plan_summary,
    ignore_patterns,
    merge_text_three_way,
    normalize_stage_path,
    print_sync_plan,
    pull_bound_project,
    push_staged_entries,
    replace_base_snapshot_from_local,
    snapshot_lines_to_text,
    sync_project,
    zip_map,
    apply_resolve_choice,
)


BASE_URL = "https://www.overleaf.com"
PROJECTS_URL = f"{BASE_URL}/project"
DOWNLOAD_ZIP_URL = f"{BASE_URL}/project/{{project_id}}/download/zip"
CREATE_FOLDER_URL = f"{BASE_URL}/project/{{project_id}}/folder"
DELETE_DOC_URL = f"{BASE_URL}/project/{{project_id}}/doc/{{entity_id}}"
DELETE_FILE_URL = f"{BASE_URL}/project/{{project_id}}/file/{{entity_id}}"
DELETE_FOLDER_URL = f"{BASE_URL}/project/{{project_id}}/folder/{{entity_id}}"
UPLOAD_URL = f"{BASE_URL}/project/{{project_id}}/upload"
COMPILE_URL = f"{BASE_URL}/project/{{project_id}}/compile?enable_pdf_caching=true"
DEFAULT_STORE_PATH = ".overleaf-sync-auth"
DEFAULT_OLIGNORE = ".ovsignore"
LEGACY_STORE_PATHS = (
    DEFAULT_STORE_PATH,
    ".olauth",
)
GLOBAL_STORE_FILENAME = "auth-store.pkl"
DEFAULT_REQUEST_TIMEOUT = (10, 30)
DOWNLOAD_ZIP_TIMEOUT = (10, 60)
LOCAL_BRIDGE_METADATA_FILES = {
    BRIDGE_CONFIG_NAME,
    DEFAULT_STORE_PATH,
    ".olauth",
    STAGE_FILE_NAME,
    DEFAULT_OLIGNORE,
    BASE_SNAPSHOT_DIR,
    CONFLICT_STATE_FILE,
    CONFLICT_SNAPSHOT_DIR,
}
AUTH_STORE_OPTION_DEFAULT = "local .overleaf-sync-auth/.olauth, else saved global auth"
LOGIN_STORE_OPTION_DEFAULT = "saved global auth"
AUTH_STORE_OPTION_HELP = (
    "Path to the persisted Overleaf auth store. If omitted, uses local "
    ".overleaf-sync-auth/.olauth when present, otherwise the saved global auth store."
)
LOGIN_STORE_OPTION_HELP = (
    "Path to store the persisted Overleaf auth store. If omitted, saves to the global default auth store."
)
REPO_STORE_OPTION_HELP = (
    "Path to the persisted Overleaf auth store. Relative paths are resolved from the repository root. "
    "If omitted, repo init uses local .overleaf-sync-auth/.olauth when present, otherwise the saved global auth store."
)
TREE_JS = r"""
() => {
  const treeRoot = document.querySelector('[role="tree"]');
  if (!treeRoot) throw new Error('No Overleaf file tree found.');

  let rootList = null;
  let rootNode = null;
  let rootFolderId = null;
  const parentFolderIds = new Map();
  const seenFibers = new Set();

  const scoreProps = (props) => {
    const docsLen = Array.isArray(props.docs) ? props.docs.length : 0;
    const foldersLen = Array.isArray(props.folders) ? props.folders.length : 0;
    const filesLen = Array.isArray(props.files) ? props.files.length : 0;
    if (!Array.isArray(props.docs) || !Array.isArray(props.folders) || !Array.isArray(props.files)) {
      return -1;
    }
    return foldersLen * 1000000 + (docsLen + filesLen);
  };

  const visitFiber = (fiber) => {
    for (let node = fiber, i = 0; node && i < 60; i += 1, node = node.return) {
      if (seenFibers.has(node)) continue;
      seenFibers.add(node);

      const props = node.memoizedProps;
      if (props && typeof props === 'object') {
        if (!rootList || scoreProps(props) > scoreProps(rootList)) {
          if (Array.isArray(props.docs) && Array.isArray(props.folders) && Array.isArray(props.files)) {
            rootList = props;
            rootNode = node;
          }
        }
      }

      const type = node.elementType || node.type;
      if (type && typeof type === 'object' && String(type.$$typeof).includes('react.provider')) {
        const value = node.memoizedProps && node.memoizedProps.value;
        if (value && typeof value === 'object' && value.parentFolderId) {
          parentFolderIds.set(value.parentFolderId, (parentFolderIds.get(value.parentFolderId) || 0) + 1);
        }
      }
    }
  };

  for (const el of treeRoot.querySelectorAll('*')) {
    const reactKey = Object.getOwnPropertyNames(el).find(key => key.startsWith('__reactFiber'));
    if (reactKey) visitFiber(el[reactKey]);
  }

  if (rootNode) {
    for (let node = rootNode, i = 0; node && i < 60; i += 1, node = node.return) {
      const type = node.elementType || node.type;
      if (type && typeof type === 'object' && String(type.$$typeof).includes('react.provider')) {
        const value = node.memoizedProps && node.memoizedProps.value;
        if (value && typeof value === 'object' && value.parentFolderId) {
          rootFolderId = value.parentFolderId;
          break;
        }
      }
    }
  }

  if (!rootFolderId && parentFolderIds.size) {
    rootFolderId = [...parentFolderIds.entries()].sort((a, b) => b[1] - a[1])[0][0];
  }

  if (!rootList || !rootFolderId) {
    throw new Error('Could not locate Overleaf file tree data.');
  }

  const buildDoc = (doc, parentPath, parentFolderId) => ({
    kind: 'doc',
    id: doc._id,
    name: doc.name,
    path: parentPath ? `${parentPath}/${doc.name}` : doc.name,
    parentFolderId,
  });

  const buildFile = (file, parentPath, parentFolderId) => ({
    kind: 'file',
    id: file._id,
    name: file.name,
    path: parentPath ? `${parentPath}/${file.name}` : file.name,
    parentFolderId,
  });

  const buildFolder = (folder, parentPath, parentFolderId) => {
    const path = parentPath ? `${parentPath}/${folder.name}` : folder.name;
    return {
      kind: 'folder',
      id: folder._id,
      name: folder.name,
      path,
      parentFolderId,
      docs: (folder.docs || []).map(doc => buildDoc(doc, path, folder._id)),
      files: ((folder.fileRefs || folder.files || [])).map(file => buildFile(file, path, folder._id)),
      folders: (folder.folders || []).map(child => buildFolder(child, path, folder._id)),
    };
  };

  return {
    rootFolderId,
    docs: (rootList.docs || []).map(doc => buildDoc(doc, '', rootFolderId)),
    files: (rootList.files || []).map(file => buildFile(file, '', rootFolderId)),
    folders: (rootList.folders || []).map(folder => buildFolder(folder, '', rootFolderId)),
  };
}
"""

def load_store(cookie_path: str) -> dict:
    with Path(cookie_path).expanduser().open("rb") as handle:
        return pickle.load(handle)


def save_store(cookie_path: str, cookie: dict, csrf: str) -> None:
    path = Path(cookie_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump({"cookie": cookie, "csrf": csrf}, handle)


def global_store_path() -> Path:
    return Path(click.get_app_dir("overleaf-sync")) / GLOBAL_STORE_FILENAME


def resolve_cli_path(value: str, *, base_dir: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base_dir or Path.cwd()).resolve() / path).resolve()


def auth_store_candidates(search_roots: list[Path] | None = None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    roots: list[Path] = []
    for root in search_roots or []:
        resolved_root = root.resolve()
        if resolved_root not in roots:
            roots.append(resolved_root)

    cwd = Path.cwd().resolve()
    if cwd not in roots:
        roots.append(cwd)

    for root in roots:
        for name in LEGACY_STORE_PATHS:
            candidate = root / name
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)

    global_path = global_store_path().resolve()
    if global_path not in seen:
        candidates.append(global_path)

    return candidates


def resolve_auth_store_path(
    cookie_path: str | None,
    *,
    search_roots: list[Path] | None = None,
    require_exists: bool = True,
    base_dir: Path | None = None,
) -> Path:
    if cookie_path:
        resolved = resolve_cli_path(cookie_path, base_dir=base_dir)
        if require_exists and not resolved.is_file():
            raise click.ClickException(
                f"Persisted Overleaf auth store not found at {resolved}. Run `ovs login` first."
            )
        return resolved

    candidates = auth_store_candidates(search_roots)
    if not require_exists:
        return global_store_path().resolve()

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise click.ClickException(
        "Persisted Overleaf auth store not found. Run `ovs login` first. "
        f"Tried local .overleaf-sync-auth/.olauth and global {global_store_path().resolve()}."
    )


def normalize_project_name(name: str) -> str:
    return re.sub(r"[\W_]+", "", name, flags=re.UNICODE).lower()


def bridge_ignored_untracked_paths(repo_root: Path, config: BridgeConfig) -> set[str]:
    ignored = {
        BRIDGE_CONFIG_NAME,
        STAGE_FILE_NAME,
        BASE_SNAPSHOT_DIR,
        CONFLICT_STATE_FILE,
        CONFLICT_SNAPSHOT_DIR,
    }
    olignore_path = Path(config.olignore)
    if olignore_path.is_absolute():
        try:
            ignored.add(olignore_path.resolve().relative_to(repo_root).as_posix())
        except ValueError:
            pass
    else:
        ignored.add(olignore_path.as_posix())
    store_path = Path(config.store_path).expanduser()
    if store_path.is_absolute():
        try:
            ignored.add(store_path.resolve().relative_to(repo_root).as_posix())
        except ValueError:
            pass
    else:
        ignored.add(store_path.as_posix())
    return ignored


def flatten_tree(tree_data: dict) -> tuple[dict[str, dict], dict[str, dict], str]:
    folders: dict[str, dict] = {}
    files: dict[str, dict] = {}

    def add_folder(folder: dict) -> None:
        folders[folder["path"]] = {
            "kind": "folder",
            "id": folder["id"],
            "path": folder["path"],
            "parent_folder_id": folder["parentFolderId"],
            "name": folder["name"],
        }
        for doc in folder.get("docs", []):
            files[doc["path"]] = {
                "kind": "doc",
                "id": doc["id"],
                "path": doc["path"],
                "parent_folder_id": doc["parentFolderId"],
                "name": doc["name"],
            }
        for file_ref in folder.get("files", []):
            files[file_ref["path"]] = {
                "kind": "file",
                "id": file_ref["id"],
                "path": file_ref["path"],
                "parent_folder_id": file_ref["parentFolderId"],
                "name": file_ref["name"],
            }
        for child in folder.get("folders", []):
            add_folder(child)

    for doc in tree_data.get("docs", []):
        files[doc["path"]] = {
            "kind": "doc",
            "id": doc["id"],
            "path": doc["path"],
            "parent_folder_id": doc["parentFolderId"],
            "name": doc["name"],
        }
    for file_ref in tree_data.get("files", []):
        files[file_ref["path"]] = {
            "kind": "file",
            "id": file_ref["id"],
            "path": file_ref["path"],
            "parent_folder_id": file_ref["parentFolderId"],
            "name": file_ref["name"],
        }
    for folder in tree_data.get("folders", []):
        add_folder(folder)

    return folders, files, tree_data["rootFolderId"]


class RealtimeProjectClient:
    def __init__(self, session: "OverleafSession", project_id: str):
        self.session = session
        self.project_id = project_id
        self.socket = None
        self.project_joined = False
        self.project_error = None
        self.pending_update = None
        self.active_doc_ids: set[str] = set()

    def _cookie_header(self) -> str:
        cookie_parts = []
        gclb_values = self.session._cookie_values("GCLB")
        if gclb_values:
            cookie_parts.append(f"GCLB={gclb_values[0]}")
        session_values = self.session._cookie_values("overleaf_session2")
        if session_values:
            cookie_parts.append(f"overleaf_session2={session_values[0]}")
        return "; ".join(cookie_parts)

    def _wait_for(self, predicate, timeout: float, message: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            self.socket.wait(seconds=0.1)
            if predicate():
                return
        raise click.ClickException(message)

    def _on_join_project(self, *args) -> None:
        self.project_joined = True
        self.project_error = None

    def _on_connection_rejected(self, *args) -> None:
        self.project_error = args[0] if args else {"message": "connection rejected"}

    def _on_update_applied(self, *args) -> None:
        if self.pending_update is not None and self.pending_update.get("applied") is None:
            self.pending_update["applied"] = args

    def _on_update_error(self, *args) -> None:
        if self.pending_update is not None and self.pending_update.get("error") is None:
            self.pending_update["error"] = args

    def connect(self) -> None:
        if self.socket is not None:
            return

        if not hasattr(websocket, "SSLError"):
            websocket.SSLError = ssl.SSLError

        handshake = self.session._request(
            "get",
            f"{BASE_URL}/socket.io/1/",
            params={"projectId": self.project_id, "esh": 1, "ssp": 1, "t": int(time.time())},
        )
        if "GCLB" in handshake.cookies:
            self.session.session.cookies.set("GCLB", handshake.cookies["GCLB"])

        self.project_joined = False
        self.project_error = None
        self.socket = SocketIO(
            BASE_URL,
            params={"projectId": self.project_id, "esh": 1, "ssp": 1, "t": int(time.time())},
            headers={"Cookie": self._cookie_header()},
        )
        self.socket.on("joinProjectResponse", self._on_join_project)
        self.socket.on("connectionRejected", self._on_connection_rejected)
        self.socket.on("otUpdateApplied", self._on_update_applied)
        self.socket.on("otUpdateError", self._on_update_error)

        self._wait_for(
            lambda: self.project_joined or self.project_error is not None,
            timeout=15,
            message="Timed out connecting to Overleaf realtime service.",
        )
        if self.project_error is not None:
            raise click.ClickException(f"Overleaf realtime connection rejected: {self.project_error}")

    def close(self) -> None:
        if self.socket is None:
            return
        socket = self.socket
        for doc_id in sorted(self.active_doc_ids):
            try:
                self.leave_doc(doc_id)
            except click.ClickException:
                pass
            except Exception:
                pass
        self.active_doc_ids.clear()

        try:
            socket.disconnect()
        except Exception:
            try:
                transport = socket._transport
            except Exception:
                transport = None
            if transport is not None:
                try:
                    transport.disconnect()
                except TypeError:
                    transport.disconnect("")
                except Exception:
                    pass
                try:
                    transport.close()
                except Exception:
                    pass

        deadline = time.time() + 2
        while getattr(socket, "connected", False) and time.time() < deadline:
            try:
                socket.wait(seconds=0.1)
            except Exception:
                break

        self.socket = None
        self.project_joined = False
        self.project_error = None
        self.pending_update = None

    def join_doc(self, doc_id: str) -> tuple[str, int]:
        self.connect()
        result = {}
        self.socket.emit(
            "joinDoc",
            doc_id,
            {"encodeRanges": True, "supportsHistoryOT": True},
            lambda *args: result.setdefault("args", args),
        )
        self._wait_for(lambda: "args" in result, timeout=15, message=f"Timed out joining Overleaf document {doc_id}.")

        args = result["args"]
        if args[0] is not None:
            raise click.ClickException(f"Failed to join Overleaf document {doc_id}: {args[0]}")

        ot_type = args[5] if len(args) > 5 else "sharejs-text-ot"
        if ot_type != "sharejs-text-ot":
            raise click.ClickException(f"Unsupported Overleaf document OT type: {ot_type}")

        self.active_doc_ids.add(doc_id)
        return snapshot_lines_to_text(args[1]), args[2]

    def leave_doc(self, doc_id: str) -> None:
        if self.socket is None:
            self.active_doc_ids.discard(doc_id)
            return

        result = {}
        self.socket.emit("leaveDoc", doc_id, lambda *args: result.setdefault("args", args))
        self._wait_for(lambda: "args" in result, timeout=10, message=f"Timed out leaving Overleaf document {doc_id}.")
        args = result["args"]
        if args and args[0] is not None:
            raise click.ClickException(f"Failed to leave Overleaf document {doc_id}: {args[0]}")
        self.active_doc_ids.discard(doc_id)

    def update_doc(self, doc_id: str, target_text: str) -> bool:
        current_text, version = self.join_doc(doc_id)
        try:
            components = build_text_components(current_text, target_text)
            if not components:
                return False

            self.pending_update = {"applied": None, "error": None}
            self.socket.emit("applyOtUpdate", doc_id, {"v": version, "op": components}, lambda *args: None)
            self._wait_for(
                lambda: self.pending_update["applied"] is not None or self.pending_update["error"] is not None,
                timeout=20,
                message=f"Timed out applying Overleaf OT update for {doc_id}.",
            )
            if self.pending_update["error"] is not None:
                raise click.ClickException(f"Overleaf OT update failed for {doc_id}: {self.pending_update['error']}")
            return True
        finally:
            self.pending_update = None
            self.leave_doc(doc_id)


class OverleafSession:
    def __init__(self, store: dict):
        self.session = reqs.Session()
        # Avoid inheriting system proxy settings. Some local loopback proxies can
        # interrupt multipart uploads and make sync behavior inconsistent.
        self.session.trust_env = False
        self.session.proxies.clear()
        self.session.cookies.update(store["cookie"])
        self.csrf = store["csrf"]

    def persist(self, cookie_path: str) -> None:
        save_store(cookie_path, self.session.cookies.get_dict(), self.csrf)

    def _request(self, method: str, url: str, *, timeout=DEFAULT_REQUEST_TIMEOUT, **kwargs):
        response = self.session.request(method, url, timeout=timeout, **kwargs)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            self._update_csrf(response.text)
        return response

    def _cookie_values(self, name: str) -> list[str]:
        values = []
        for cookie in self.session.cookies:
            if cookie.name == name and cookie.value not in values:
                values.append(cookie.value)
        return values

    def _update_csrf(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        token = soup.find("meta", {"name": "ol-csrfToken"})
        if token is not None:
            self.csrf = token.get("content", self.csrf)

    def _projects_page(self) -> str:
        response = self._request("get", PROJECTS_URL)
        return response.text

    def _parse_projects(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"name": "ol-prefetchedProjectsBlob"}) or soup.find("meta", {"name": "ol-projects"})
        if meta is None:
            raise click.ClickException("Could not parse Overleaf project list.")

        payload = meta.get("content", "")
        data = reqs.models.complexjson.loads(payload)
        if isinstance(data, dict) and "projects" in data:
            projects = data["projects"]
        else:
            projects = data
        return [project for project in projects if not project.get("archived") and not project.get("trashed")]

    def list_projects(self) -> list[dict]:
        return self._parse_projects(self._projects_page())

    def get_project(self, project_name: str) -> dict:
        projects = self.list_projects()
        exact = next((project for project in projects if project.get("name") == project_name), None)
        if exact:
            return exact

        normalized = normalize_project_name(project_name)
        fuzzy = [project for project in projects if normalize_project_name(project.get("name", "")) == normalized]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise click.ClickException(f"Multiple Overleaf projects match '{project_name}'. Use the exact project name.")
        raise click.ClickException(f"Overleaf project '{project_name}' not found.")

    def download_zip(self, project_id: str) -> bytes:
        try:
            response = self._request("get", DOWNLOAD_ZIP_URL.format(project_id=project_id), timeout=DOWNLOAD_ZIP_TIMEOUT)
        except reqs.RequestException as exc:
            raise RemoteZipDownloadError(
                "Failed to download the Overleaf project archive. "
                "Retry the command, or use a local-only push (`ovs -l` / `ovs repo push-overleaf`) "
                "which can fall back to the remote file tree."
            ) from exc
        return response.content

    def create_folder(self, project_id: str, parent_folder_id: str, folder_name: str) -> dict:
        response = self._request(
            "post",
            CREATE_FOLDER_URL.format(project_id=project_id),
            headers={"X-Csrf-Token": self.csrf},
            json={"parent_folder_id": parent_folder_id, "name": folder_name},
        )
        return response.json()

    def delete_entity(self, project_id: str, entity: dict) -> None:
        if entity["kind"] == "doc":
            url = DELETE_DOC_URL.format(project_id=project_id, entity_id=entity["id"])
        elif entity["kind"] == "file":
            url = DELETE_FILE_URL.format(project_id=project_id, entity_id=entity["id"])
        elif entity["kind"] == "folder":
            url = DELETE_FOLDER_URL.format(project_id=project_id, entity_id=entity["id"])
        else:
            raise click.ClickException(f"Unsupported Overleaf entity kind '{entity['kind']}'.")

        self._request("delete", url, headers={"X-Csrf-Token": self.csrf}, json={})

    def upload_file(self, project_id: str, folder_id: str, local_path: Path) -> dict:
        mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        with local_path.open("rb") as handle:
            response = self._request(
                "post",
                f"{UPLOAD_URL.format(project_id=project_id)}?folder_id={folder_id}",
                headers={"X-Csrf-Token": self.csrf},
                files={
                    "relativePath": (None, "null"),
                    "name": (None, local_path.name),
                    "type": (None, mime_type),
                    "qqfile": (local_path.name, handle, mime_type),
                },
            )
        payload = response.json()
        if not payload.get("success"):
            raise click.ClickException(f"Failed to upload '{local_path.name}' to Overleaf: {payload}")
        return payload

    def download_pdf(self, project_id: str) -> tuple[str, bytes]:
        payload = self.compile_project(project_id)
        pdf_file = next(output for output in payload["outputFiles"] if output["type"] == "pdf")
        return pdf_file["path"], self.download_output(pdf_file["url"])

    def compile_project(
        self,
        project_id: str,
        *,
        root_doc_id: str = "",
        draft: bool = False,
        stop_on_first_error: bool = False,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> dict:
        payload = {}
        for attempt in range(max_attempts):
            response = self._request(
                "post",
                COMPILE_URL.format(project_id=project_id),
                headers={"X-Csrf-Token": self.csrf},
                json={
                    "check": "silent",
                    "draft": draft,
                    "incrementalCompilesEnabled": True,
                    "rootDoc_id": root_doc_id,
                    "stopOnFirstError": stop_on_first_error,
                },
            )
            payload = response.json()

            status = payload.get("status")
            output_files = payload.get("outputFiles") or []
            should_retry = status in {"too-recently-compiled", "compile-in-progress"} and not output_files
            if not should_retry or attempt == max_attempts - 1:
                return payload
            time.sleep(retry_delay)

        return payload

    def download_output(self, output_url: str) -> bytes:
        if output_url.startswith("http://") or output_url.startswith("https://"):
            url = output_url
        elif output_url.startswith("/"):
            url = BASE_URL + output_url
        else:
            url = f"{BASE_URL}/{output_url.lstrip('/')}"

        response = self._request("get", url, headers={"X-Csrf-Token": self.csrf})
        return response.content

    def extract_tree(self, project_id: str) -> tuple[dict[str, dict], dict[str, dict], str]:
        socket_response = self._request(
            "get",
            f"{BASE_URL}/socket.io/1/",
            params={"projectId": project_id, "esh": 1, "ssp": 1, "t": 1},
        )
        if "GCLB" in socket_response.cookies:
            self.session.cookies.set("GCLB", socket_response.cookies["GCLB"])

        browser_cookies = []
        session_values = self._cookie_values("overleaf_session2")
        overleaf_session = session_values[0] if session_values else None
        if overleaf_session:
            browser_cookies.append(
                {
                    "name": "overleaf_session2",
                    "value": overleaf_session,
                    "domain": ".overleaf.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        gclb_values = self._cookie_values("GCLB")
        gclb = gclb_values[0] if gclb_values else None
        if gclb:
            browser_cookies.append(
                {
                    "name": "GCLB",
                    "value": gclb,
                    "domain": "www.overleaf.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            if browser_cookies:
                context.add_cookies(browser_cookies)
            page = context.new_page()
            page.goto(f"{BASE_URL}/project/{project_id}", wait_until="domcontentloaded")
            page.wait_for_selector('[role="tree"] [role="treeitem"]', timeout=30000)
            tree_data = page.evaluate(TREE_JS)
            browser.close()

        return flatten_tree(tree_data)


def collect_local_push_preview_state(
    session: OverleafSession,
    project: dict,
    sync_root: Path,
    olignore_path: Path,
) -> tuple[dict, bool]:
    try:
        return collect_sync_state(session, project, sync_root, olignore_path), False
    except RemoteZipDownloadError as exc:
        click.echo(f"[WARN] {exc}")
        click.echo(
            "[WARN] Falling back to a metadata-only local push preview; "
            "exact replace/no-op detection is unavailable until the remote archive is reachable."
        )
        return collect_tree_sync_state(session, project, sync_root, olignore_path), True


def build_local_push_preview_plan(
    session: OverleafSession,
    project: dict,
    sync_root: Path,
    olignore_path: Path,
) -> dict[str, list[str]]:
    state, used_fallback = collect_local_push_preview_state(session, project, sync_root, olignore_path)
    if used_fallback:
        return build_metadata_only_local_push_plan(
            state["local_files"],
            state["remote_entities"],
            state["remote_folders"],
        )
    return build_sync_plan(
        state["local_files"],
        state["remote_zip"],
        state["remote_entities"],
        state["remote_folders"],
        True,
        False,
    )


def print_bridge_status(
    git_status: GitStatusSummary,
    config: BridgeConfig,
    push_plan: dict[str, list[str]],
    pull_plan: dict[str, list[str]] | None,
    sync_root: Path,
) -> None:
    click.echo("Git:")
    click.echo(f"  repo_root: {git_status.repo_root}")
    click.echo(f"  remote: {git_status.git_remote} ({git_status.remote_url})")
    click.echo(f"  current_branch: {git_status.current_branch}")
    click.echo(f"  default_branch: {git_status.default_branch}")
    click.echo(f"  working_tree: {'clean' if git_status.is_clean else 'dirty'}")
    click.echo(f"  ahead: {git_status.ahead}")
    click.echo(f"  behind: {git_status.behind}")
    if git_status.current_branch != git_status.default_branch:
        click.echo(
            f"  warning: current branch '{git_status.current_branch}' is not the configured default branch "
            f"'{git_status.default_branch}'; bridge push/pull commands only operate on the default branch."
        )

    click.echo("")
    click.echo("Overleaf:")
    click.echo(f"  project: {config.project_name}")
    click.echo(f"  sync_path: {sync_root}")
    click.echo(f"  push-overleaf: {format_sync_plan_summary(push_plan)}")
    if pull_plan is None:
        click.echo("  pull-overleaf: unavailable (remote archive unavailable)")
    else:
        click.echo(f"  pull-overleaf: {format_sync_plan_summary(pull_plan)}")


def build_display_tree(remote_folders: dict[str, dict], remote_entities: dict[str, dict]) -> dict:
    def new_node() -> dict:
        return {"folders": {}, "files": []}

    root = new_node()

    def ensure_node(parts: list[str]) -> dict:
        node = root
        for part in parts:
            node = node["folders"].setdefault(part, new_node())
        return node

    for folder_path in sorted(remote_folders):
        ensure_node(folder_path.split("/"))

    for rel_path, entity in sorted(remote_entities.items()):
        parts = rel_path.split("/")
        node = ensure_node(parts[:-1])
        node["files"].append({"name": parts[-1], "kind": entity["kind"], "path": rel_path})

    return root


def render_tree_lines(node: dict, prefix: str = "") -> list[str]:
    entries = [(name, "folder", child) for name, child in sorted(node["folders"].items())]
    entries.extend((item["name"], item["kind"], item) for item in sorted(node["files"], key=lambda file_item: file_item["name"]))

    lines = []
    for index, (name, kind, payload) in enumerate(entries):
        is_last = index == len(entries) - 1
        connector = "└── " if is_last else "├── "
        if kind == "folder":
            lines.append(f"{prefix}{connector}{name}/")
            lines.extend(render_tree_lines(payload, prefix + ("    " if is_last else "│   ")))
            continue

        label = "[doc]" if kind == "doc" else "[file]"
        lines.append(f"{prefix}{connector}{name} {label}")

    return lines


def print_remote_tree(remote_folders: dict[str, dict], remote_entities: dict[str, dict]) -> None:
    tree = build_display_tree(remote_folders, remote_entities)
    lines = render_tree_lines(tree)
    if not lines:
        click.echo("(empty project)")
        return
    for line in lines:
        click.echo(line)


def sorted_output_files(payload: dict) -> list[dict]:
    return sorted(
        payload.get("outputFiles", []),
        key=lambda item: (item.get("path", ""), item.get("type", ""), item.get("url", "")),
    )


def print_compile_outputs(payload: dict) -> None:
    output_files = sorted_output_files(payload)
    click.echo(f"Compile status: {payload.get('status', 'unknown')}")
    click.echo(f"Artifacts: {len(output_files)}")

    timings = payload.get("timings") or {}
    if timings:
        timing_parts = []
        for key in ("compile", "compileE2E", "output", "sync"):
            value = timings.get(key)
            if value is not None:
                timing_parts.append(f"{key}={value}")
        if timing_parts:
            click.echo("Timings: " + ", ".join(timing_parts))

    for item in output_files:
        click.echo(f"[ARTIFACT {item.get('type', 'unknown')}] {item.get('path', '')}")


def select_output_files(payload: dict, artifact_paths: tuple[str, ...], download_all: bool) -> list[dict]:
    output_files = sorted_output_files(payload)
    if download_all:
        return output_files

    if not artifact_paths:
        return []

    by_path = {item.get("path", ""): item for item in output_files}
    selected = []
    missing = []
    seen = set()
    for artifact_path in artifact_paths:
        item = by_path.get(artifact_path)
        if item is None:
            missing.append(artifact_path)
            continue
        if artifact_path not in seen:
            selected.append(item)
            seen.add(artifact_path)

    if missing:
        available = ", ".join(sorted(by_path)) or "(none)"
        raise click.ClickException(
            "Unknown compile artifact(s): "
            + ", ".join(missing)
            + f". Available artifacts: {available}"
        )

    return selected


def bridge_session_and_project(repo_root: Path, config: BridgeConfig) -> tuple[OverleafSession, dict, Path, Path, Path]:
    store_path = resolve_repo_path(repo_root, config.store_path)
    if not store_path.is_file():
        raise click.ClickException("Persisted Overleaf auth store not found. Run `ovs login` first.")

    sync_root = resolve_repo_path(repo_root, config.sync_path)
    if not sync_root.exists():
        raise click.ClickException(f"Configured sync path does not exist: {sync_root}")

    olignore_path = resolve_repo_path(repo_root, config.olignore)
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(config.project_name)
    return session, project, store_path, sync_root, olignore_path


def resolve_bound_sync_context(
    project_name: str,
    cookie_path: str | None,
    sync_path: str,
    olignore_path: str,
) -> tuple[Path, str, Path, Path, Path | None, BridgeConfig | None]:
    binding_root = None
    config = None
    if not project_name and cookie_path is None and sync_path == "." and olignore_path == DEFAULT_OLIGNORE:
        binding_root = find_bound_root(required=False)
        if binding_root is not None:
            config = load_bridge_config(binding_root)
            store_path = resolve_repo_path(binding_root, config.store_path)
            sync_root = resolve_repo_path(binding_root, config.sync_path)
            return sync_root, config.project_name, store_path, resolve_repo_path(binding_root, config.olignore), binding_root, config

    sync_root = Path(sync_path).resolve()
    store_path = resolve_auth_store_path(cookie_path, search_roots=[sync_root])
    return sync_root, project_name or sync_root.name, store_path, sync_root / olignore_path, binding_root, config


def resolve_bound_project_context(
    project_name: str,
    cookie_path: str | None,
) -> tuple[str, Path, Path | None, BridgeConfig | None]:
    if not project_name and cookie_path is None:
        binding_root = find_bound_root(required=False)
        if binding_root is not None:
            config = load_bridge_config(binding_root)
            store_path = resolve_repo_path(binding_root, config.store_path)
            return config.project_name, store_path, binding_root, config

    store_path = resolve_auth_store_path(cookie_path)
    return project_name or Path.cwd().name, store_path, None, None


@click.group(invoke_without_command=True)
@click.option("-l", "--local-only", "local_only", is_flag=True, help="Sync local files to Overleaf.")
@click.option("-r", "--remote-only", "remote_only", is_flag=True, help="Sync remote files to local.")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show planned sync actions without applying them.")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
@click.option("-p", "--path", "sync_path", default=".", type=click.Path(exists=True), help="Local sync path.")
@click.option("-i", "--ovsignore", "olignore_path", default=DEFAULT_OLIGNORE, type=click.Path(exists=False), help="Path to .ovsignore relative to sync path.")
@click.pass_context
def main(ctx: click.Context, local_only: bool, remote_only: bool, dry_run: bool, project_name: str, cookie_path: str | None, sync_path: str, olignore_path: str) -> None:
    if ctx.invoked_subcommand is not None:
        return

    if local_only and remote_only:
        raise click.ClickException("Use at most one of --local-only and --remote-only.")
    sync_root, resolved_project_name, store_path, resolved_olignore_path, binding_root, _ = resolve_bound_sync_context(
        project_name,
        cookie_path,
        sync_path,
        olignore_path,
    )
    if binding_root is not None and not dry_run:
        require_no_unresolved_conflicts(binding_root)
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(resolved_project_name)
    if dry_run:
        if local_only and not remote_only:
            plan = build_local_push_preview_plan(session, project, sync_root, resolved_olignore_path)
        else:
            state = collect_sync_state(session, project, sync_root, resolved_olignore_path)
            plan = build_sync_plan(
                state["local_files"],
                state["remote_zip"],
                state["remote_entities"],
                state["remote_folders"],
                local_only,
                remote_only,
            )
        print_sync_plan(plan)
        session.persist(str(store_path))
        return
    sync_project(
        session,
        project,
        sync_root,
        resolved_olignore_path,
        local_only,
        remote_only,
        realtime_factory=RealtimeProjectClient,
    )
    if binding_root is not None:
        replace_base_snapshot_from_local(binding_root, sync_root, resolved_olignore_path)
    session.persist(str(store_path))


@main.command(name="bind")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name. Defaults to the current directory name.")
@click.option("--store-path", "store_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
@click.option("-p", "--path", "sync_path", default=".", show_default=True, type=click.Path(exists=False), help="Local sync path, relative to the binding root.")
@click.option("-i", "--ovsignore", "olignore_path", default=DEFAULT_OLIGNORE, show_default=True, type=click.Path(exists=False), help="Path to .ovsignore, relative to the binding root.")
@click.option("--force", is_flag=True, help="Overwrite an existing binding in the current directory.")
def bind(project_name: str, store_path: str | None, sync_path: str, olignore_path: str, force: bool) -> None:
    bind_root = Path.cwd().resolve()
    config_path = bridge_config_path(bind_root)
    existing = None
    if config_path.exists():
        if not force:
            raise click.ClickException(
                f"Binding already exists at {config_path}. Use `ovs push` / `ovs pull`, or re-run with --force."
            )
        existing = load_bridge_config(bind_root)

    normalized_sync_path = normalize_bridge_path(sync_path, "sync_path")
    normalized_olignore = normalize_bridge_path(olignore_path, "olignore")
    if store_path:
        normalized_store_path = normalize_store_config_path(store_path)
        store_abs_path = resolve_repo_path(bind_root, normalized_store_path)
        if not store_abs_path.is_file():
            raise click.ClickException(f"Persisted Overleaf auth store not found at {store_abs_path}. Run `ovs login` first.")
    else:
        store_abs_path = resolve_auth_store_path(None, search_roots=[bind_root])
        normalized_store_path = display_store_config_path(bind_root, store_abs_path)

    sync_root = resolve_repo_path(bind_root, normalized_sync_path)
    if not sync_root.exists():
        raise click.ClickException(f"Configured sync path does not exist: {sync_root}")

    resolved_project_name = project_name or bind_root.name
    session = OverleafSession(load_store(str(store_abs_path)))
    project = session.get_project(resolved_project_name)
    config = BridgeConfig(
        version=BRIDGE_CONFIG_VERSION,
        project_name=project["name"],
        store_path=normalized_store_path,
        sync_path=normalized_sync_path,
        olignore=normalized_olignore,
        git_remote=existing.git_remote if existing else "",
        default_branch=existing.default_branch if existing else "",
    )
    write_bridge_config(bind_root, config)
    save_stage_entries(bind_root, {})
    save_conflict_entries(bind_root, {})
    replace_base_snapshot(bind_root, {})
    try:
        replace_base_snapshot(bind_root, zip_map(session.download_zip(project["id"])))
    except RemoteZipDownloadError as exc:
        click.echo(f"[WARN] {exc}")
        click.echo("[WARN] Remote merge base was reset; first pull will be conservative until initialization succeeds.")
    session.persist(str(store_abs_path))
    click.echo(f"Bound {bind_root} to Overleaf project '{config.project_name}'.")
    click.echo(f"config: {config_path}")
    click.echo(f"sync_path: {resolve_repo_path(bind_root, config.sync_path)}")


@main.command(name="add")
@click.option("-A", "--all", "add_all", is_flag=True, help="Stage all local files under the bound sync root.")
@click.argument("paths", nargs=-1)
def add(paths: tuple[str, ...], add_all: bool) -> None:
    binding_root = find_bound_root()
    require_no_unresolved_conflicts(binding_root)
    config = load_bridge_config(binding_root)
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(binding_root, config)
    state = collect_sync_state(session, project, sync_root, olignore_path)
    stage_entries = load_stage_entries(binding_root)

    target_paths: set[str] = set()
    if add_all:
        target_paths.update(state["local_files"])
    for value in paths:
        target_paths.add(normalize_stage_path(sync_root, value))
    if not target_paths:
        raise click.ClickException("Nothing to stage. Provide path(s) or use `ovs add -A`.")

    for rel_path in sorted(target_paths):
        local_path = state["local_files"].get(rel_path)
        remote_bytes = state["remote_zip"].get(rel_path)
        if local_path is None and remote_bytes is None:
            raise click.ClickException(f"Path not found locally or remotely: {rel_path}")
        stage_entries[rel_path] = {
            "local_hash": file_sha256(local_path.read_bytes()) if local_path is not None else None,
            "remote_hash": file_sha256(remote_bytes) if remote_bytes is not None else None,
        }
        click.echo(f"staged {rel_path}")

    save_stage_entries(binding_root, stage_entries)
    session.persist(str(store_path))


@main.command(name="reset")
@click.option("--all", "reset_all", is_flag=True, help="Clear the current Overleaf stage.")
@click.argument("paths", nargs=-1)
def reset(paths: tuple[str, ...], reset_all: bool) -> None:
    binding_root = find_bound_root()
    stage_entries = load_stage_entries(binding_root)
    if not stage_entries:
        click.echo("No staged Overleaf paths.")
        return
    if reset_all:
        save_stage_entries(binding_root, {})
        click.echo("Cleared all staged Overleaf paths.")
        return
    if not paths:
        raise click.ClickException("Provide path(s) to unstage, or use `ovs reset --all`.")

    config = load_bridge_config(binding_root)
    sync_root = resolve_repo_path(binding_root, config.sync_path)
    removed = 0
    for value in paths:
        rel_path = normalize_stage_path(sync_root, value)
        if rel_path in stage_entries:
            stage_entries.pop(rel_path, None)
            removed += 1
            click.echo(f"unstaged {rel_path}")
    save_stage_entries(binding_root, stage_entries)
    if removed == 0:
        raise click.ClickException("No matching staged Overleaf paths.")


@main.command(name="resolve")
@click.option("--ours", "choice", flag_value="ours", help="Resolve conflict(s) by keeping the local pre-pull version.")
@click.option("--theirs", "choice", flag_value="theirs", help="Resolve conflict(s) by keeping the current Overleaf version.")
@click.option("--mark-resolved", "choice", flag_value="mark-resolved", help="Mark manually edited conflict(s) as resolved.")
@click.option("--all", "resolve_all", is_flag=True, help="Apply the selected resolution to all unresolved conflicts.")
@click.argument("paths", nargs=-1)
def resolve(choice: str | None, resolve_all: bool, paths: tuple[str, ...]) -> None:
    binding_root = find_bound_root()
    conflict_entries = load_conflict_entries(binding_root)
    if not conflict_entries:
        click.echo("No unresolved Overleaf conflicts.")
        return
    if choice is None:
        if resolve_all or paths:
            raise click.ClickException("Choose one of --ours, --theirs, or --mark-resolved.")
        print_conflict_entries(conflict_entries)
        return
    if resolve_all:
        target_paths = sorted(conflict_entries)
    else:
        if not paths:
            raise click.ClickException("Provide path(s) to resolve, or use `ovs resolve --all ...`.")
        config = load_bridge_config(binding_root)
        sync_root = resolve_repo_path(binding_root, config.sync_path)
        target_paths = [normalize_stage_path(sync_root, value) for value in paths]

    config = load_bridge_config(binding_root)
    sync_root = resolve_repo_path(binding_root, config.sync_path)
    resolved_count = 0
    for rel_path in target_paths:
        if rel_path not in conflict_entries:
            raise click.ClickException(f"No unresolved Overleaf conflict recorded for {rel_path}.")
        apply_resolve_choice(binding_root, sync_root, rel_path, choice)
        click.echo(f"resolved {rel_path} ({choice})")
        resolved_count += 1
    if resolved_count == 0:
        raise click.ClickException("No matching unresolved Overleaf conflicts.")


@main.command(name="push")
@click.option("--dry-run", is_flag=True, help="Show the local->remote plan without applying it.")
def push(dry_run: bool) -> None:
    binding_root = find_bound_root()
    require_no_unresolved_conflicts(binding_root)
    config = load_bridge_config(binding_root)
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(binding_root, config)
    stage_entries = load_stage_entries(binding_root)
    if stage_entries:
        print_staged_entries(stage_entries)
    if dry_run:
        if stage_entries:
            state = collect_sync_state(session, project, sync_root, olignore_path)
            plan = {
                "push_new": [path for path in sorted(stage_entries) if path in state["local_files"] and path not in state["remote_zip"]],
                "push_replace": [path for path in sorted(stage_entries) if path in state["local_files"] and path in state["remote_zip"]],
                "pull_new": [],
                "pull_replace": [],
                "local_delete": [],
                "remote_delete": [path for path in sorted(stage_entries) if path not in state["local_files"] and path in state["remote_zip"]],
                "remote_delete_folders": [],
                "conflicts": [],
            }
        else:
            plan = build_local_push_preview_plan(session, project, sync_root, olignore_path)
        print_sync_plan(plan)
    elif stage_entries:
        def mark_staged_path_applied(rel_path: str) -> None:
            update_base_snapshot_from_local_paths(binding_root, sync_root, {rel_path})
            stage_entries.pop(rel_path, None)
            save_stage_entries(binding_root, stage_entries)

        push_staged_entries(
            session,
            project,
            sync_root,
            olignore_path,
            stage_entries,
            realtime_factory=RealtimeProjectClient,
            on_applied=mark_staged_path_applied,
        )
    else:
        sync_project(
            session,
            project,
            sync_root,
            olignore_path,
            local_only=True,
            remote_only=False,
            realtime_factory=RealtimeProjectClient,
        )
        replace_base_snapshot_from_local(binding_root, sync_root, olignore_path)
    session.persist(str(store_path))


@main.command(name="pull")
@click.option("--dry-run", is_flag=True, help="Show the remote->local plan without applying it.")
def pull(dry_run: bool) -> None:
    binding_root = find_bound_root()
    require_no_unresolved_conflicts(binding_root)
    config = load_bridge_config(binding_root)
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(binding_root, config)
    stage_entries = load_stage_entries(binding_root)
    if stage_entries and not dry_run:
        raise click.ClickException("Staged Overleaf paths exist. Push or reset them before pulling remote changes.")
    if dry_run:
        state = collect_sync_state(session, project, sync_root, olignore_path)
        plan = build_sync_plan(
            state["local_files"],
            state["remote_zip"],
            state["remote_entities"],
            state["remote_folders"],
            False,
            True,
        )
        print_sync_plan(plan)
    else:
        pull_bound_project(session, project, binding_root, sync_root, olignore_path)
    session.persist(str(store_path))


@main.group(name="repo")
def repo() -> None:
    """Manage a Git repository that syncs with GitHub and Overleaf."""


@repo.command(name="init")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name. Defaults to the repository root name.")
@click.option("--store-path", "store_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=REPO_STORE_OPTION_HELP)
@click.option("-p", "--path", "sync_path", default=".", show_default=True, type=click.Path(exists=False), help="Local sync path, relative to the repository root.")
@click.option("-i", "--ovsignore", "olignore_path", default=DEFAULT_OLIGNORE, show_default=True, type=click.Path(exists=False), help="Path to .ovsignore, relative to the repository root.")
@click.option("--git-remote", "git_remote", default=DEFAULT_GIT_REMOTE, show_default=True, help="Git remote used for GitHub operations.")
def repo_init(project_name: str, store_path: str | None, sync_path: str, olignore_path: str, git_remote: str) -> None:
    repo_root = find_repo_root()
    normalized_sync_path = normalize_bridge_path(sync_path, "sync_path")
    normalized_olignore = normalize_bridge_path(olignore_path, "olignore")

    git_remote_url(repo_root, git_remote)
    if store_path:
        normalized_store_path = normalize_store_config_path(store_path)
        store_abs_path = resolve_repo_path(repo_root, normalized_store_path)
        if not store_abs_path.is_file():
            raise click.ClickException(f"Persisted Overleaf auth store not found at {store_abs_path}. Run `ovs login` first.")
    else:
        store_abs_path = resolve_auth_store_path(None, search_roots=[repo_root])
        normalized_store_path = display_store_config_path(repo_root, store_abs_path)

    sync_root = resolve_repo_path(repo_root, normalized_sync_path)
    if not sync_root.exists():
        raise click.ClickException(f"Configured sync path does not exist: {sync_root}")

    resolved_project_name = project_name or repo_root.name
    session = OverleafSession(load_store(str(store_abs_path)))
    project = session.get_project(resolved_project_name)
    default_branch = detect_default_branch(repo_root, git_remote)
    config = BridgeConfig(
        version=BRIDGE_CONFIG_VERSION,
        project_name=project["name"],
        store_path=normalized_store_path,
        sync_path=normalized_sync_path,
        olignore=normalized_olignore,
        git_remote=git_remote,
        default_branch=default_branch,
    )
    config_path = write_bridge_config(repo_root, config)
    save_stage_entries(repo_root, {})
    save_conflict_entries(repo_root, {})
    replace_base_snapshot(repo_root, {})
    try:
        replace_base_snapshot(repo_root, zip_map(session.download_zip(project["id"])))
    except RemoteZipDownloadError as exc:
        click.echo(f"[WARN] {exc}")
        click.echo("[WARN] Remote merge base was reset; first pull will be conservative until initialization succeeds.")
    session.persist(str(store_abs_path))

    click.echo(f"Wrote bridge config to {config_path}")
    click.echo(f"project_name: {config.project_name}")
    click.echo(f"git_remote: {config.git_remote}")
    click.echo(f"default_branch: {config.default_branch}")


@repo.command(name="status")
def repo_status() -> None:
    repo_root = find_repo_root()
    config = load_bridge_config(repo_root)
    require_repo_binding(config)
    git_status = collect_git_status(
        repo_root,
        config.git_remote,
        config.default_branch,
        ignored_untracked_paths=bridge_ignored_untracked_paths(repo_root, config),
    )
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(repo_root, config)
    try:
        state = collect_sync_state(session, project, sync_root, olignore_path)
        push_plan = build_sync_plan(
            state["local_files"],
            state["remote_zip"],
            state["remote_entities"],
            state["remote_folders"],
            True,
            False,
        )
        pull_plan = build_sync_plan(
            state["local_files"],
            state["remote_zip"],
            state["remote_entities"],
            state["remote_folders"],
            False,
            True,
        )
    except RemoteZipDownloadError as exc:
        click.echo(f"[WARN] {exc}")
        click.echo("[WARN] Showing a metadata-only push summary; pull summary is unavailable until the remote archive is reachable.")
        state = collect_tree_sync_state(session, project, sync_root, olignore_path)
        push_plan = build_metadata_only_local_push_plan(
            state["local_files"],
            state["remote_entities"],
            state["remote_folders"],
        )
        pull_plan = None
    print_bridge_status(git_status, config, push_plan, pull_plan, sync_root)
    print_conflict_entries(load_conflict_entries(repo_root))
    session.persist(str(store_path))


@repo.command(name="push-github")
def repo_push_github() -> None:
    repo_root = find_repo_root()
    config = load_bridge_config(repo_root)
    require_repo_binding(config)
    git_status = collect_git_status(
        repo_root,
        config.git_remote,
        config.default_branch,
        ignored_untracked_paths=bridge_ignored_untracked_paths(repo_root, config),
    )
    require_default_branch(git_status)
    require_clean_worktree(git_status)
    result = run_git_command(["push", config.git_remote, git_status.current_branch], cwd=repo_root)
    output = result.stdout.strip() or f"Pushed {git_status.current_branch} to {config.git_remote}."
    click.echo(output)


@repo.command(name="pull-github")
def repo_pull_github() -> None:
    repo_root = find_repo_root()
    config = load_bridge_config(repo_root)
    require_repo_binding(config)
    git_status = collect_git_status(
        repo_root,
        config.git_remote,
        config.default_branch,
        ignored_untracked_paths=bridge_ignored_untracked_paths(repo_root, config),
    )
    require_default_branch(git_status)
    require_clean_worktree(git_status)
    result = run_git_command(["pull", "--ff-only", config.git_remote, git_status.current_branch], cwd=repo_root)
    output = result.stdout.strip() or f"Pulled {git_status.current_branch} from {config.git_remote}."
    click.echo(output)


@repo.command(name="push-overleaf")
def repo_push_overleaf() -> None:
    repo_root = find_repo_root()
    require_no_unresolved_conflicts(repo_root)
    config = load_bridge_config(repo_root)
    require_repo_binding(config)
    git_status = collect_git_status(
        repo_root,
        config.git_remote,
        config.default_branch,
        ignored_untracked_paths=bridge_ignored_untracked_paths(repo_root, config),
    )
    require_default_branch(git_status)
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(repo_root, config)
    sync_project(
        session,
        project,
        sync_root,
        olignore_path,
        local_only=True,
        remote_only=False,
        realtime_factory=RealtimeProjectClient,
    )
    replace_base_snapshot_from_local(repo_root, sync_root, olignore_path)
    session.persist(str(store_path))


@repo.command(name="pull-overleaf")
def repo_pull_overleaf() -> None:
    repo_root = find_repo_root()
    require_no_unresolved_conflicts(repo_root)
    config = load_bridge_config(repo_root)
    require_repo_binding(config)
    git_status = collect_git_status(
        repo_root,
        config.git_remote,
        config.default_branch,
        ignored_untracked_paths=bridge_ignored_untracked_paths(repo_root, config),
    )
    require_default_branch(git_status)
    require_clean_worktree(git_status)
    session, project, store_path, sync_root, olignore_path = bridge_session_and_project(repo_root, config)
    pull_bound_project(session, project, repo_root, sync_root, olignore_path)
    session.persist(str(store_path))


main.add_command(repo, name="bridge")


@main.command()
@click.option("--store-path", "--path", "cookie_path", default=None, show_default=LOGIN_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=LOGIN_STORE_OPTION_HELP)
def login(cookie_path: str | None) -> None:
    from overleaf_sync.browser_login import login as browser_login

    store = browser_login()
    if store is None:
        raise click.ClickException("Login failed.")
    store_path = resolve_auth_store_path(cookie_path, require_exists=False)
    save_store(str(store_path), store["cookie"], store["csrf"])
    click.echo(f"Login successful. Cookie persisted as `{click.format_filename(str(store_path))}`.")


@main.command(name="list")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
def list_projects(cookie_path: str | None) -> None:
    store_path = resolve_auth_store_path(cookie_path)
    session = OverleafSession(load_store(str(store_path)))
    for project in sorted(session.list_projects(), key=lambda item: item.get("lastUpdated", ""), reverse=True):
        click.echo(f"{project.get('lastUpdated', '')} - {project.get('name', '')}")
    session.persist(str(store_path))


@main.command(name="download")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--download-path", "download_path", default=".", type=click.Path(exists=False), help="Where to write the compiled PDF.")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
def download_pdf(project_name: str, download_path: str, cookie_path: str | None) -> None:
    resolved_project_name, store_path, _, _ = resolve_bound_project_context(project_name, cookie_path)
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(resolved_project_name)
    file_name, content = session.download_pdf(project["id"])
    output_root = Path(download_path).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / file_name
    ensure_local_dir(output_path)
    output_path.write_bytes(content)
    session.persist(str(store_path))
    click.echo(f"Downloaded PDF to {output_path}")


@main.command(name="tree")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
@click.option("--json", "json_output", is_flag=True, help="Print the remote file tree as JSON.")
def tree(project_name: str, cookie_path: str | None, json_output: bool) -> None:
    resolved_project_name, store_path, _, _ = resolve_bound_project_context(project_name, cookie_path)
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(resolved_project_name)
    remote_folders, remote_entities, root_folder_id = session.extract_tree(project["id"])

    if json_output:
        click.echo(
            json.dumps(
                {
                    "project": {"id": project["id"], "name": project.get("name", "")},
                    "rootFolderId": root_folder_id,
                    "folders": [remote_folders[path] for path in sorted(remote_folders)],
                    "entities": [remote_entities[path] for path in sorted(remote_entities)],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_remote_tree(remote_folders, remote_entities)

    session.persist(str(store_path))


@main.command(name="artifacts")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
@click.option("--download-path", "download_path", default="output", type=click.Path(exists=False), help="Where to write downloaded compile artifacts.")
@click.option("--artifact", "artifact_paths", multiple=True, help="Compile artifact path to download. Repeat the option to download multiple artifacts.")
@click.option("--all", "download_all", is_flag=True, help="Download all compile artifacts.")
@click.option("--json", "json_output", is_flag=True, help="Print the raw compile response as JSON.")
def artifacts(project_name: str, cookie_path: str | None, download_path: str, artifact_paths: tuple[str, ...], download_all: bool, json_output: bool) -> None:
    resolved_project_name, store_path, _, _ = resolve_bound_project_context(project_name, cookie_path)
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(resolved_project_name)
    payload = session.compile_project(project["id"])

    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_compile_outputs(payload)

    selected = select_output_files(payload, artifact_paths, download_all)
    output_root = Path(download_path).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    for item in selected:
        output_path = output_root / item["path"]
        ensure_local_dir(output_path)
        output_path.write_bytes(session.download_output(item["url"]))
        click.echo(f"Downloaded {item['path']} to {output_path}")

    session.persist(str(store_path))


@main.command(name="status")
@click.option("-l", "--local-only", "local_only", is_flag=True, help="Show the plan for local-only sync.")
@click.option("-r", "--remote-only", "remote_only", is_flag=True, help="Show the plan for remote-only sync.")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=None, show_default=AUTH_STORE_OPTION_DEFAULT, type=click.Path(exists=False), help=AUTH_STORE_OPTION_HELP)
@click.option("-p", "--path", "sync_path", default=".", type=click.Path(exists=True), help="Local sync path.")
@click.option("-i", "--ovsignore", "olignore_path", default=DEFAULT_OLIGNORE, type=click.Path(exists=False), help="Path to .ovsignore relative to sync path.")
def status(local_only: bool, remote_only: bool, project_name: str, cookie_path: str | None, sync_path: str, olignore_path: str) -> None:
    if local_only and remote_only:
        raise click.ClickException("Use at most one of --local-only and --remote-only.")
    sync_root, resolved_project_name, store_path, resolved_olignore_path, binding_root, _ = resolve_bound_sync_context(
        project_name,
        cookie_path,
        sync_path,
        olignore_path,
    )
    session = OverleafSession(load_store(str(store_path)))
    project = session.get_project(resolved_project_name)
    if local_only and not remote_only:
        plan = build_local_push_preview_plan(session, project, sync_root, resolved_olignore_path)
    else:
        state = collect_sync_state(session, project, sync_root, resolved_olignore_path)
        plan = build_sync_plan(
            state["local_files"],
            state["remote_zip"],
            state["remote_entities"],
            state["remote_folders"],
            local_only,
            remote_only,
        )
    if binding_root is not None:
        print_conflict_entries(load_conflict_entries(binding_root))
        print_staged_entries(load_stage_entries(binding_root))
    print_sync_plan(plan)
    session.persist(str(store_path))


if __name__ == "__main__":
    main()
