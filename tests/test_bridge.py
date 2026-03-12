import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import requests as reqs
from click.testing import CliRunner

from overleaf_sync import cli


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def init_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    git(["init"], cwd=repo_root)
    git(["config", "user.name", "Test User"], cwd=repo_root)
    git(["config", "user.email", "test@example.com"], cwd=repo_root)
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    git(["add", "README.md"], cwd=repo_root)
    git(["commit", "-m", "Initial commit"], cwd=repo_root)
    git(["branch", "-M", "main"], cwd=repo_root)


def write_bridge_config(
    repo_root: Path,
    *,
    project_name: str = "Demo Project",
    store_path: str = ".overleaf-sync-auth",
    sync_path: str = ".",
    olignore: str = ".olignore",
    git_remote: str = "origin",
    default_branch: str = "main",
) -> None:
    config = cli.BridgeConfig(
        version=cli.BRIDGE_CONFIG_VERSION,
        project_name=project_name,
        store_path=store_path,
        sync_path=sync_path,
        olignore=olignore,
        git_remote=git_remote,
        default_branch=default_branch,
    )
    cli.write_bridge_config(repo_root, config)


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class DummySession:
    last_instance = None

    def __init__(self, store: dict):
        self.store = store
        self.persisted_path = None
        DummySession.last_instance = self

    def get_project(self, project_name: str) -> dict:
        return {"id": "project-1", "name": project_name}

    def list_projects(self) -> list[dict]:
        return [{"lastUpdated": "2026-03-12", "name": "Demo Project"}]

    def persist(self, cookie_path: str) -> None:
        self.persisted_path = cookie_path


class BridgeHelpersTest(unittest.TestCase):
    def test_parse_git_status_porcelain(self) -> None:
        parsed = cli.parse_git_status_porcelain(
            "## main...origin/main [ahead 2, behind 1]\n M README.md\n?? draft.tex\n"
        )
        self.assertEqual(parsed["branch"], "main")
        self.assertEqual(parsed["upstream"], "origin/main")
        self.assertEqual(parsed["ahead"], 2)
        self.assertEqual(parsed["behind"], 1)
        self.assertFalse(parsed["is_clean"])

    def test_write_and_load_bridge_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            write_bridge_config(repo_root, project_name="Bridge Demo", store_path=".olauth")
            loaded = cli.load_bridge_config(repo_root)
            self.assertEqual(loaded.project_name, "Bridge Demo")
            self.assertEqual(loaded.store_path, ".olauth")
            self.assertEqual(loaded.default_branch, "main")

    def test_resolve_auth_store_path_prefers_local_before_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            local_store = root / ".overleaf-sync-auth"
            global_store = root / "global" / "auth-store.pkl"
            cli.save_store(str(local_store), {"sid": "local"}, "csrf-local")
            cli.save_store(str(global_store), {"sid": "global"}, "csrf-global")

            with working_directory(root), mock.patch.object(cli, "global_store_path", return_value=global_store):
                resolved = cli.resolve_auth_store_path(None)

            self.assertEqual(resolved, local_store.resolve())

    def test_resolve_auth_store_path_uses_global_when_local_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            global_store = root / "global" / "auth-store.pkl"
            cli.save_store(str(global_store), {"sid": "global"}, "csrf-global")

            with working_directory(root), mock.patch.object(cli, "global_store_path", return_value=global_store):
                resolved = cli.resolve_auth_store_path(None)

            self.assertEqual(resolved, global_store.resolve())

    def test_download_zip_wraps_request_timeout(self) -> None:
        session = cli.OverleafSession({"cookie": {}, "csrf": "token"})
        with mock.patch.object(session.session, "request", side_effect=reqs.Timeout("zip timeout")) as request:
            with self.assertRaises(cli.RemoteZipDownloadError):
                session.download_zip("project-1")

        self.assertEqual(request.call_args.kwargs["timeout"], cli.DOWNLOAD_ZIP_TIMEOUT)

    def test_build_destructive_sync_warnings(self) -> None:
        warnings = cli.build_destructive_sync_warnings(
            {
                "push_new": [],
                "push_replace": [],
                "pull_new": [],
                "pull_replace": [],
                "local_delete": [],
                "remote_delete": ["a.tex", "b.tex"],
                "remote_delete_folders": ["old"],
                "conflicts": [],
            },
            local_only=True,
            remote_only=False,
        )
        self.assertEqual(
            warnings,
            ["Local-only sync will delete 2 remote file(s) and 1 remote folder(s) not present locally."],
        )

    def test_realtime_close_disconnects_even_if_socket_not_marked_connected(self) -> None:
        client = cli.RealtimeProjectClient(mock.Mock(), "project-1")
        socket = mock.Mock()
        socket.connected = False
        client.socket = socket

        client.close()

        socket.disconnect.assert_called_once_with()
        self.assertIsNone(client.socket)

    def test_realtime_close_leaves_active_docs_before_disconnect(self) -> None:
        client = cli.RealtimeProjectClient(mock.Mock(), "project-1")
        socket = mock.Mock()
        socket.connected = False
        client.socket = socket
        client.active_doc_ids = {"doc-2", "doc-1"}

        left_docs: list[str] = []

        def fake_leave(doc_id: str) -> None:
            left_docs.append(doc_id)
            client.active_doc_ids.discard(doc_id)

        with mock.patch.object(client, "leave_doc", side_effect=fake_leave) as leave_doc:
            client.close()

        self.assertEqual(left_docs, ["doc-1", "doc-2"])
        self.assertEqual(leave_doc.call_count, 2)
        socket.disconnect.assert_called_once_with()
        self.assertEqual(client.active_doc_ids, set())


class BridgeCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_bridge_init_fails_outside_git_repo(self) -> None:
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli.main, ["repo", "init"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Not inside a Git repository.", result.output)

    def test_bridge_init_fails_when_remote_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            with working_directory(repo_root):
                result = self.runner.invoke(cli.main, ["repo", "init"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such remote 'origin'", result.output)

    def test_bridge_init_writes_config_for_existing_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            (repo_root / ".overleaf-sync-auth").write_bytes(b"auth")

            with mock.patch.object(cli, "load_store", return_value={"cookie": {}, "csrf": "token"}), mock.patch.object(
                cli, "OverleafSession", DummySession
            ):
                with working_directory(repo_root):
                    result = self.runner.invoke(cli.main, ["repo", "init", "--name", "Paper Project"])

            self.assertEqual(result.exit_code, 0, result.output)
            config_data = json.loads((repo_root / ".overleaf-sync.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["project_name"], "Paper Project")
            self.assertEqual(config_data["git_remote"], "origin")
            self.assertEqual(config_data["default_branch"], "main")

    def test_login_defaults_to_global_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            global_store = Path(tmpdir) / "global" / "auth-store.pkl"
            fake_browser_login = types.SimpleNamespace(login=lambda: {"cookie": {"sid": "cookie"}, "csrf": "token"})
            with mock.patch.object(cli, "global_store_path", return_value=global_store), mock.patch.dict(
                sys.modules, {"overleaf_sync.browser_login": fake_browser_login}
            ):
                result = self.runner.invoke(cli.main, ["login"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(global_store.is_file())
            store = cli.load_store(str(global_store))
            self.assertEqual(store["cookie"], {"sid": "cookie"})
            self.assertEqual(store["csrf"], "token")

    def test_list_uses_global_store_without_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            global_store = root / "global" / "auth-store.pkl"
            cli.save_store(str(global_store), {"sid": "cookie"}, "token")

            with working_directory(root), mock.patch.object(cli, "global_store_path", return_value=global_store), mock.patch.object(
                cli, "OverleafSession", DummySession
            ):
                result = self.runner.invoke(cli.main, ["list"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(DummySession.last_instance.persisted_path, str(global_store.resolve()))
            self.assertIn("Demo Project", result.output)

    def test_bridge_init_uses_global_store_when_no_local_auth_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            global_store = root / "global" / "auth-store.pkl"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            cli.save_store(str(global_store), {"sid": "cookie"}, "token")

            with working_directory(repo_root), mock.patch.object(cli, "global_store_path", return_value=global_store), mock.patch.object(
                cli, "load_store", return_value={"cookie": {}, "csrf": "token"}
            ), mock.patch.object(cli, "OverleafSession", DummySession):
                result = self.runner.invoke(cli.main, ["repo", "init", "--name", "Paper Project"])

            self.assertEqual(result.exit_code, 0, result.output)
            config_data = json.loads((repo_root / ".overleaf-sync.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["store_path"], str(global_store.resolve()))

    def test_bridge_status_reports_git_and_overleaf_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            write_bridge_config(repo_root)
            (repo_root / ".overleaf-sync-auth").write_bytes(b"auth")
            local_path = repo_root / "draft.tex"
            local_path.write_text("draft\n", encoding="utf-8")

            state = {
                "local_files": {"draft.tex": local_path},
                "remote_zip": {"server.tex": b"remote\n"},
                "remote_folders": {},
                "remote_entities": {"server.tex": {"kind": "doc", "id": "doc-1", "path": "server.tex"}},
                "root_folder_id": "root",
            }

            with mock.patch.object(cli, "load_store", return_value={"cookie": {}, "csrf": "token"}), mock.patch.object(
                cli, "OverleafSession", DummySession
            ), mock.patch.object(cli, "collect_sync_state", return_value=state):
                with working_directory(repo_root):
                    result = self.runner.invoke(cli.main, ["repo", "status"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("working_tree: dirty", result.output)
            self.assertIn("project: Demo Project", result.output)
            self.assertIn("push-overleaf: push_new=1, remote_delete=1", result.output)
            self.assertIn("pull-overleaf: pull_new=1, local_delete=1", result.output)

    def test_bridge_push_github_rejects_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            write_bridge_config(repo_root)
            (repo_root / "README.md").write_text("dirty\n", encoding="utf-8")

            with working_directory(repo_root):
                result = self.runner.invoke(cli.main, ["repo", "push-github"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Working tree must be clean", result.output)

    def test_bridge_pull_overleaf_rejects_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            write_bridge_config(repo_root)
            (repo_root / "README.md").write_text("dirty\n", encoding="utf-8")

            with working_directory(repo_root):
                result = self.runner.invoke(cli.main, ["repo", "pull-overleaf"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Working tree must be clean", result.output)

    def test_bridge_push_overleaf_allows_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            write_bridge_config(repo_root)
            (repo_root / ".overleaf-sync-auth").write_bytes(b"auth")
            (repo_root / "README.md").write_text("dirty\n", encoding="utf-8")

            with mock.patch.object(cli, "load_store", return_value={"cookie": {}, "csrf": "token"}), mock.patch.object(
                cli, "OverleafSession", DummySession
            ), mock.patch.object(cli, "sync_project") as sync_project:
                with working_directory(repo_root):
                    result = self.runner.invoke(cli.main, ["repo", "push-overleaf"])

            self.assertEqual(result.exit_code, 0, result.output)
            sync_project.assert_called_once()
            self.assertTrue(sync_project.call_args.kwargs["local_only"])
            self.assertFalse(sync_project.call_args.kwargs["remote_only"])

    def test_bridge_pull_overleaf_uses_remote_only_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", "https://example.com/demo.git"], cwd=repo_root)
            write_bridge_config(repo_root)
            (repo_root / ".overleaf-sync-auth").write_bytes(b"auth")

            with mock.patch.object(cli, "load_store", return_value={"cookie": {}, "csrf": "token"}), mock.patch.object(
                cli, "OverleafSession", DummySession
            ), mock.patch.object(cli, "sync_project") as sync_project:
                with working_directory(repo_root):
                    result = self.runner.invoke(cli.main, ["repo", "pull-overleaf"])

            self.assertEqual(result.exit_code, 0, result.output)
            sync_project.assert_called_once()
            self.assertFalse(sync_project.call_args.kwargs["local_only"])
            self.assertTrue(sync_project.call_args.kwargs["remote_only"])

    def test_bridge_push_github_pushes_to_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            remote_root = tmp_path / "remote.git"
            git(["init", "--bare", str(remote_root)], cwd=tmp_path)

            repo_root = tmp_path / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", str(remote_root)], cwd=repo_root)
            git(["push", "-u", "origin", "main"], cwd=repo_root)
            (repo_root / "README.md").write_text("second\n", encoding="utf-8")
            git(["add", "README.md"], cwd=repo_root)
            git(["commit", "-m", "Second commit"], cwd=repo_root)
            write_bridge_config(repo_root)

            with working_directory(repo_root):
                result = self.runner.invoke(cli.main, ["repo", "push-github"])

            self.assertEqual(result.exit_code, 0, result.output)
            remote_count = git(["rev-list", "--count", "main"], cwd=remote_root).stdout.strip()
            self.assertEqual(remote_count, "2")

    def test_bridge_pull_github_pulls_from_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            remote_root = tmp_path / "remote.git"
            git(["init", "--bare", str(remote_root)], cwd=tmp_path)

            repo_root = tmp_path / "repo"
            init_repo(repo_root)
            git(["remote", "add", "origin", str(remote_root)], cwd=repo_root)
            git(["push", "-u", "origin", "main"], cwd=repo_root)
            write_bridge_config(repo_root)

            other_root = tmp_path / "other"
            git(["clone", str(remote_root), str(other_root)], cwd=tmp_path)
            git(["config", "user.name", "Other User"], cwd=other_root)
            git(["config", "user.email", "other@example.com"], cwd=other_root)
            git(["checkout", "main"], cwd=other_root)
            (other_root / "README.md").write_text("remote update\n", encoding="utf-8")
            git(["add", "README.md"], cwd=other_root)
            git(["commit", "-m", "Remote update"], cwd=other_root)
            git(["push", "origin", "main"], cwd=other_root)

            with working_directory(repo_root):
                result = self.runner.invoke(cli.main, ["repo", "pull-github"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "remote update\n")

    def test_bridge_alias_still_works(self) -> None:
        result = self.runner.invoke(cli.main, ["bridge", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)


class SyncFallbackTest(unittest.TestCase):
    def test_sync_project_warns_and_shows_progress(self) -> None:
        session = mock.Mock()
        session.upload_file.return_value = {"success": True, "entity_type": "file", "entity_id": "file-1"}
        project = {"id": "project-1"}

        with tempfile.TemporaryDirectory() as tmpdir:
            sync_root = Path(tmpdir)
            local_path = sync_root / "big.bin"
            local_path.write_bytes(b"0123456789")
            olignore_path = sync_root / ".olignore"
            olignore_path.write_text("", encoding="utf-8")

            state = {
                "local_files": {"big.bin": local_path},
                "remote_zip": {"old.txt": b"remote\n"},
                "remote_folders": {
                    "old": {"kind": "folder", "id": "folder-1", "path": "old", "parent_folder_id": "root", "name": "old"}
                },
                "remote_entities": {
                    "old.txt": {"kind": "file", "id": "doc-1", "path": "old.txt", "parent_folder_id": "root", "name": "old.txt"}
                },
                "root_folder_id": "root",
                "remote_zip_available": True,
            }

            with mock.patch.object(cli, "collect_sync_state", return_value=state), mock.patch.object(
                cli, "ensure_remote_folder", return_value="root"
            ), mock.patch.object(cli, "LARGE_FILE_WARNING_BYTES", 4), mock.patch.object(cli.click, "echo") as echo:
                cli.sync_project(session, project, sync_root, olignore_path, local_only=True, remote_only=False)

        messages = [call.args[0] for call in echo.call_args_list]
        self.assertIn("[WARN] Local-only sync will delete 1 remote file(s) and 1 remote folder(s) not present locally.", messages)
        self.assertTrue(any("Large upload detected: big.bin" in message for message in messages))
        self.assertIn("[1/3 REMOTE DELETE] old.txt", messages)
        self.assertIn("[2/3 LOCAL -> REMOTE] big.bin", messages)
        self.assertIn("[3/3 REMOTE DELETE FOLDER] old", messages)

    def test_sync_project_falls_back_for_local_only_push(self) -> None:
        session = mock.Mock()
        project = {"id": "project-1"}
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_root = Path(tmpdir)
            olignore_path = sync_root / ".olignore"
            olignore_path.write_text("", encoding="utf-8")

            with mock.patch.object(
                cli,
                "collect_sync_state",
                side_effect=cli.RemoteZipDownloadError("zip export stalled"),
            ), mock.patch.object(cli, "sync_project_local_only_fallback") as fallback:
                cli.sync_project(session, project, sync_root, olignore_path, local_only=True, remote_only=False)

        fallback.assert_called_once()
        self.assertIs(fallback.call_args.args[0], session)
        self.assertEqual(fallback.call_args.args[1], project)
        self.assertEqual(fallback.call_args.args[2], sync_root)
        self.assertEqual(fallback.call_args.args[3], olignore_path)
        self.assertIsInstance(fallback.call_args.args[4], cli.RemoteZipDownloadError)

    def test_sync_project_propagates_zip_failure_for_non_local_sync(self) -> None:
        session = mock.Mock()
        project = {"id": "project-1"}
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_root = Path(tmpdir)
            olignore_path = sync_root / ".olignore"
            olignore_path.write_text("", encoding="utf-8")

            with mock.patch.object(
                cli,
                "collect_sync_state",
                side_effect=cli.RemoteZipDownloadError("zip export stalled"),
            ):
                with self.assertRaises(cli.RemoteZipDownloadError):
                    cli.sync_project(session, project, sync_root, olignore_path, local_only=False, remote_only=True)


if __name__ == "__main__":
    unittest.main()
