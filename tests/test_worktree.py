
"""Git Worktree 管理系统的测试（第 13 章）。"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from likecc.agent import Agent, PermissionRequest, PermissionResponse
from likecc.cache import FileCache
from likecc.commands.handlers.worktree import create_worktree_command
from likecc.config import WorktreeConfig, load_config
from likecc.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from likecc.tools import create_default_registry
from likecc.tools.base import ToolCallComplete
from likecc.tools.enter_worktree import EnterWorktreeParams, EnterWorktreeTool
from likecc.tools.exit_worktree import ExitWorktreeParams, ExitWorktreeTool
from likecc.tools.work_dir import tool_working_directory
from likecc.worktree.changes import count_worktree_changes, has_worktree_changes
from likecc.worktree.integration import build_worktree_notice, generate_worktree_name
from likecc.worktree.manager import WorktreeError, WorktreeManager
from likecc.worktree.models import WorktreeSession
from likecc.worktree.session import load_worktree_session, save_worktree_session
from likecc.worktree.slug import flatten_slug, validate_slug

# =========================================================================
# A. Slug 校验
# =========================================================================

class TestValidateSlug:
    def test_valid_simple(self):
        assert validate_slug("my-feature") is None

    def test_valid_with_dots(self):
        assert validate_slug("v1.0") is None

    def test_valid_nested(self):
        assert validate_slug("team/alice") is None

    def test_valid_single_char(self):
        assert validate_slug("a") is None

    def test_valid_underscores(self):
        assert validate_slug("my_feature_2") is None

    def test_empty(self):
        assert validate_slug("") is not None

    def test_too_long(self):
        assert validate_slug("a" * 65) is not None
        assert validate_slug("a" * 64) is None

    def test_path_traversal(self):
        assert validate_slug("../../etc/passwd") is not None

    def test_dot_segment(self):
        assert validate_slug("foo/./bar") is not None
        assert validate_slug("foo/../bar") is not None

    def test_dot_only(self):
        assert validate_slug(".") is not None
        assert validate_slug("..") is not None

    def test_spaces(self):
        assert validate_slug("my feature") is not None

    def test_special_chars(self):
        assert validate_slug("my@feature") is not None
        assert validate_slug("my feature") is not None
        assert validate_slug("my;feature") is not None

    def test_empty_segment(self):
        assert validate_slug("foo//bar") is not None

class TestFlattenSlug:
    def test_no_slash(self):
        assert flatten_slug("my-feature") == "my-feature"

    def test_with_slash(self):
        assert flatten_slug("team/alice") == "team+alice"

    def test_multiple_slashes(self):
        assert flatten_slug("a/b/c") == "a+b+c"

# =========================================================================
# B. FileCache
# =========================================================================

class TestFileCache:
    def test_put_and_get(self):
        cache = FileCache()
        cache.put("/tmp/test.py", "content")
        assert cache.get("/tmp/test.py") == "content"

    def test_miss(self):
        cache = FileCache()
        assert cache.get("/nonexistent") is None

    def test_invalidate(self):
        cache = FileCache()
        cache.put("/tmp/test.py", "content")
        cache.invalidate("/tmp/test.py")
        assert cache.get("/tmp/test.py") is None

    def test_clear(self):
        cache = FileCache()
        cache.put("/a", "1")
        cache.put("/b", "2")
        assert len(cache) == 2
        cache.clear()
        assert len(cache) == 0
        assert cache.get("/a") is None

    def test_invalidate_nonexistent(self):
        cache = FileCache()
        cache.invalidate("/nonexistent")  # 不应抛出异常

# =========================================================================
# C. 配置扩展
# =========================================================================

class TestWorktreeConfig:
    def test_defaults(self):
        cfg = WorktreeConfig()
        assert "node_modules" in cfg.symlink_directories
        assert cfg.stale_cleanup_interval == 3600
        assert cfg.stale_cutoff_hours == 24

    def test_load_config_without_worktree_section(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "providers:\n"
            "  - name: test\n"
            "    protocol: openai\n"
            "    base_url: http://localhost\n"
            "    model: gpt-4\n"
        )
        cfg = load_config(config_file)
        assert cfg.worktree.stale_cleanup_interval == 3600

    def test_load_config_with_worktree_section(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "providers:\n"
            "  - name: test\n"
            "    protocol: openai\n"
            "    base_url: http://localhost\n"
            "    model: gpt-4\n"
            "worktree:\n"
            "  symlink_directories:\n"
            "    - .venv\n"
            "  stale_cleanup_interval: 1800\n"
            "  stale_cutoff_hours: 12\n"
        )
        cfg = load_config(config_file)
        assert cfg.worktree.symlink_directories == [".venv"]
        assert cfg.worktree.stale_cleanup_interval == 1800
        assert cfg.worktree.stale_cutoff_hours == 12

# =========================================================================
# H. 会话持久化
# =========================================================================

class TestSessionPersistence:

    def test_save_and_load(self, tmp_path):
        session = WorktreeSession(
            original_cwd="/original",
            worktree_path="/wt/path",
            worktree_name="my-feature",
            original_branch="main",
            original_head_commit="abc123",
        )
        save_worktree_session(tmp_path, session)
        loaded = load_worktree_session(tmp_path)
        assert loaded is not None
        assert loaded.worktree_name == "my-feature"
        assert loaded.original_cwd == "/original"

    def test_save_none_clears(self, tmp_path):
        session = WorktreeSession(
            original_cwd="/original",
            worktree_path="/wt/path",
            worktree_name="my-feature",
            original_branch="main",
            original_head_commit="abc123",
        )
        save_worktree_session(tmp_path, session)
        save_worktree_session(tmp_path, None)
        loaded = load_worktree_session(tmp_path)
        assert loaded is None

    def test_load_missing_file(self, tmp_path):
        assert load_worktree_session(tmp_path) is None

    def test_load_corrupt_json(self, tmp_path):
        path = tmp_path / "worktree_session.json"
        path.write_text("not json")
        assert load_worktree_session(tmp_path) is None

# =========================================================================
# 集成辅助函数
# =========================================================================

class TestIntegrationHelpers:
    def test_generate_worktree_name(self):
        name = generate_worktree_name()
        assert name.startswith("agent-")
        assert len(name) == 14  # "agent-" + 8 个十六进制字符

    def test_build_worktree_notice(self):
        notice = build_worktree_notice("/parent/dir", "/wt/dir")
        assert "/parent/dir" in notice
        assert "/wt/dir" in notice
        assert "WORKTREE CONTEXT" in notice

# =========================================================================
# D. WorktreeManager（需要真实的 git 仓库）
# =========================================================================

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)

@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo

@pytest.fixture
def file_cache():
    return FileCache()

@pytest.fixture
def tool_registry(file_cache):
    return create_default_registry(file_cache=file_cache)

@pytest.fixture
def work_dir_changes():
    return []

@pytest.fixture
def sync_work_dir(tool_registry, work_dir_changes):
    def sync(path: str) -> None:
        tool_registry.clear_file_caches()
        work_dir_changes.append(path)

    return sync

@pytest.fixture
def manager(git_repo, sync_work_dir):
    worktree_manager = WorktreeManager(
        repo_root=str(git_repo),
        symlink_directories=[],
        on_work_dir_change=sync_work_dir,
    )
    yield worktree_manager

class TestWorktreeManager:
    @pytest.mark.asyncio
    async def test_create(self, manager, git_repo):
        wt = await manager.create("test-feature")
        assert wt.name == "test-feature"
        assert wt.branch == "worktree-test-feature"
        assert Path(wt.path).exists()
        assert (Path(wt.path) / "README.md").exists()

    @pytest.mark.asyncio
    async def test_create_invalid_slug(self, manager):
        with pytest.raises(WorktreeError, match="must not contain"):
            await manager.create("../escape")

    @pytest.mark.asyncio
    async def test_create_duplicate(self, manager):
        await manager.create("dup")
        with pytest.raises(WorktreeError, match="already exists"):
            await manager.create("dup")

    @pytest.mark.asyncio
    async def test_create_nested_slug(self, manager):
        wt = await manager.create("team/alice")
        assert wt.branch == "worktree-team+alice"
        assert Path(wt.path).exists()

    @pytest.mark.asyncio
    async def test_fast_recovery(self, manager):
        wt1 = await manager.create("recover")
        path = wt1.path
        manager.active.clear()
        wt2 = await manager.create("recover")
        assert wt2.path == path
        assert wt2.head_commit == wt1.head_commit

    @pytest.mark.asyncio
    async def test_enter_and_session(self, manager, work_dir_changes):
        process_cwd = Path.cwd()
        await manager.create("enter-test")
        session = await manager.enter("enter-test")
        assert session.worktree_name == "enter-test"
        assert manager.current_session is not None
        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == session.worktree_path

    @pytest.mark.asyncio
    async def test_enter_clears_cache(self, manager, file_cache):
        await manager.create("cache-test")
        file_cache.put("/some/file", "old content")
        await manager.enter("cache-test")
        assert file_cache.get("/some/file") is None

    @pytest.mark.asyncio
    async def test_exit_keep(self, manager, git_repo, work_dir_changes):
        process_cwd = Path.cwd()
        await manager.create("exit-keep")
        await manager.enter("exit-keep")
        await manager.exit("exit-keep", action="keep")
        assert manager.current_session is None
        assert "exit-keep" in manager.active
        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == str(git_repo)

    @pytest.mark.asyncio
    async def test_exit_remove_clean(self, manager):
        wt = await manager.create("exit-rm")
        await manager.enter("exit-rm")
        await manager.exit("exit-rm", action="remove", discard_changes=True)
        assert "exit-rm" not in manager.active

    @pytest.mark.asyncio
    async def test_exit_remove_with_changes_blocked(self, manager, git_repo):
        wt = await manager.create("exit-protect")
        (Path(wt.path) / "new_file.txt").write_text("changes")
        await manager.enter("exit-protect")
        with pytest.raises(WorktreeError, match="has changes"):
            await manager.exit(
                "exit-protect", action="remove", discard_changes=False
            )

    @pytest.mark.asyncio
    async def test_list_worktrees(self, manager):
        await manager.create("list-a")
        await manager.create("list-b")
        wts = manager.list_worktrees()
        names = {wt.name for wt in wts}
        assert names == {"list-a", "list-b"}

    @pytest.mark.asyncio
    async def test_enter_nonexistent(self, manager):
        with pytest.raises(WorktreeError, match="not found"):
            await manager.enter("nope")

    @pytest.mark.asyncio
    async def test_restore_session_switches_context_and_clears_cache(
        self, manager, git_repo, file_cache, sync_work_dir, work_dir_changes
    ):
        wt = await manager.create("restore-test")
        await manager.enter("restore-test")

        file_cache.put("/some/file", "old content")
        work_dir_changes.clear()
        restored_manager = WorktreeManager(
            repo_root=str(git_repo),
            symlink_directories=[],
            on_work_dir_change=sync_work_dir,
        )

        restored = restored_manager.restore_session()

        assert restored is not None
        assert restored.worktree_path == wt.path
        assert restored_manager.current_session is restored
        assert work_dir_changes == [wt.path]
        assert file_cache.get("/some/file") is None

    @pytest.mark.asyncio
    async def test_enter_and_exit_tools_use_manager_context(
        self, manager, git_repo, file_cache, work_dir_changes
    ):
        process_cwd = Path.cwd()
        file_cache.put("/some/file", "old content")
        enter_result = await EnterWorktreeTool(manager).execute(
            EnterWorktreeParams(name="tool-context")
        )

        assert not enter_result.is_error
        session = manager.get_current_session()
        assert session is not None
        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == session.worktree_path
        assert file_cache.get("/some/file") is None

        file_cache.put("/some/file", "worktree content")
        exit_result = await ExitWorktreeTool(manager).execute(
            ExitWorktreeParams(action="keep")
        )

        assert not exit_result.is_error
        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == str(git_repo)
        assert file_cache.get("/some/file") is None

    @pytest.mark.asyncio
    async def test_slash_command_uses_manager_context(
        self, manager, git_repo, file_cache, work_dir_changes
    ):
        process_cwd = Path.cwd()
        messages: list[str] = []
        ctx = SimpleNamespace(
            args="create slash-context",
            agent=None,
            ui=SimpleNamespace(add_system_message=messages.append),
        )
        command = create_worktree_command(manager)
        file_cache.put("/some/file", "old content")

        await command.handler(ctx)

        session = manager.get_current_session()
        assert session is not None
        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == session.worktree_path
        assert file_cache.get("/some/file") is None
        assert "已创建并进入 worktree" in messages[-1]

        file_cache.put("/some/file", "worktree content")
        ctx.args = "exit"
        await command.handler(ctx)

        assert Path.cwd() == process_cwd
        assert work_dir_changes[-1] == str(git_repo)
        assert file_cache.get("/some/file") is None

    @pytest.mark.asyncio
    async def test_relative_file_and_bash_tools_follow_worktree_cwd(
        self, manager, tool_registry
    ):
        wt = await manager.create("relative-tools")
        await manager.enter("relative-tools")

        agent = Agent(
            client=SimpleNamespace(),
            registry=tool_registry,
            protocol="anthropic",
            work_dir=wt.path,
        )
        read_result = await agent._execute_tool_noninteractive(
            ToolCallComplete("read", "ReadFile", {"file_path": "README.md"})
        )
        bash_result = await agent._execute_tool_noninteractive(
            ToolCallComplete("pwd", "Bash", {"command": "pwd"})
        )

        assert not read_result.is_error
        assert "# Test" in read_result.output
        assert not bash_result.is_error
        assert wt.path in bash_result.output

    @pytest.mark.asyncio
    async def test_concurrent_agents_with_shared_tools_keep_work_dirs_isolated(
        self, tmp_path, tool_registry
    ):
        root_a = tmp_path / "agent-a"
        root_b = tmp_path / "agent-b"
        root_a.mkdir()
        root_b.mkdir()

        class RecordingHistory:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def track_edit(self, path: str) -> None:
                self.paths.append(path)

        shared_tool_history = RecordingHistory()
        history_a = RecordingHistory()
        history_b = RecordingHistory()
        write_tool = tool_registry.get("WriteFile")
        assert write_tool is not None
        write_tool.file_history = shared_tool_history

        # Keep both executions suspended inside their Agent-local contexts at
        # the same time.  This catches process-global/thread-local approaches
        # that appear correct when the async tool bodies happen not to yield.
        original_execute = write_tool.execute
        both_started = asyncio.Event()
        started = 0

        async def overlapping_execute(params):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return await original_execute(params)

        write_tool.execute = overlapping_execute

        agent_a = Agent(SimpleNamespace(), tool_registry, "anthropic", work_dir=str(root_a))
        agent_b = Agent(SimpleNamespace(), tool_registry, "anthropic", work_dir=str(root_b))
        agent_a.file_history = history_a
        agent_b.file_history = history_b

        async def run(agent: Agent, content: str):
            pwd_result = await agent._execute_tool_noninteractive(
                ToolCallComplete(f"pwd-{content}", "Bash", {"command": "pwd"})
            )
            write_result = await agent._execute_tool_noninteractive(
                ToolCallComplete(
                    f"write-{content}",
                    "WriteFile",
                    {"file_path": "shared.txt", "content": content},
                )
            )
            return pwd_result, write_result

        (pwd_a, write_a), (pwd_b, write_b) = await asyncio.gather(
            run(agent_a, "alpha"),
            run(agent_b, "bravo"),
        )

        assert not pwd_a.is_error and str(root_a) in pwd_a.output
        assert not pwd_b.is_error and str(root_b) in pwd_b.output
        assert not write_a.is_error and not write_b.is_error
        assert (root_a / "shared.txt").read_text() == "alpha"
        assert (root_b / "shared.txt").read_text() == "bravo"
        assert history_a.paths == [str((root_a / "shared.txt").resolve())]
        assert history_b.paths == [str((root_b / "shared.txt").resolve())]
        assert shared_tool_history.paths == []

    @pytest.mark.asyncio
    async def test_agent_without_history_does_not_fall_back_to_shared_tool_history(
        self, tmp_path, tool_registry
    ):
        class RecordingHistory:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def track_edit(self, path: str) -> None:
                self.paths.append(path)

        root = tmp_path / "child"
        root.mkdir()
        parent_history = RecordingHistory()
        write_tool = tool_registry.get("WriteFile")
        assert write_tool is not None
        write_tool.file_history = parent_history

        child = Agent(SimpleNamespace(), tool_registry, "anthropic", work_dir=str(root))
        result = await child._execute_tool_noninteractive(
            ToolCallComplete(
                "child-write",
                "WriteFile",
                {"file_path": "child.txt", "content": "child"},
            )
        )

        assert not result.is_error
        assert (root / "child.txt").read_text() == "child"
        assert parent_history.paths == []

        # Calling the tool directly remains backwards compatible: without an
        # Agent-local binding it uses the history supplied to the tool itself.
        direct_path = root / "direct.txt"
        direct_result = await write_tool.execute(
            write_tool.params_model.model_validate(
                {"file_path": str(direct_path), "content": "direct"}
            )
        )
        assert not direct_result.is_error
        assert parent_history.paths == [str(direct_path.resolve())]

    @pytest.mark.asyncio
    async def test_failed_edit_validation_does_not_track_history(self, tmp_path):
        from likecc.tools.edit_file import EditFile

        class RecordingHistory:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def track_edit(self, path: str) -> None:
                self.paths.append(path)

        path = tmp_path / "note.txt"
        path.write_text("unchanged")
        history = RecordingHistory()
        tool = EditFile(file_history=history)

        result = await tool.execute(
            tool.params_model.model_validate(
                {
                    "file_path": str(path),
                    "old_string": "missing",
                    "new_string": "replacement",
                }
            )
        )

        assert result.is_error
        assert path.read_text() == "unchanged"
        assert history.paths == []

    @pytest.mark.asyncio
    async def test_glob_and_grep_patterns_cannot_escape_agent_work_dir(
        self, tmp_path, tool_registry
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        agent = Agent(SimpleNamespace(), tool_registry, "anthropic", work_dir=str(work_dir))

        glob_result = await agent._execute_tool_noninteractive(
            ToolCallComplete(
                "glob-traversal",
                "Glob",
                {"path": ".", "pattern": "../*"},
            )
        )
        grep_result = await agent._execute_tool_noninteractive(
            ToolCallComplete(
                "grep-traversal",
                "Grep",
                {"path": ".", "pattern": ".", "include": "../*"},
            )
        )

        assert glob_result.is_error
        assert grep_result.is_error
        assert "stay within" in glob_result.output
        assert "stay within" in grep_result.output

    @pytest.mark.asyncio
    async def test_permission_wait_keeps_captured_work_dir_for_execution_and_state(
        self, tmp_path, tool_registry
    ):
        root_a = tmp_path / "approved-root"
        root_b = tmp_path / "later-root"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "note.txt").write_text("approved content")
        (root_b / "note.txt").write_text("wrong content")

        class RecordingHistory:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def track_edit(self, path: str) -> None:
                self.paths.append(path)

        history = RecordingHistory()
        write_tool = tool_registry.get("WriteFile")
        assert write_tool is not None
        write_tool.file_history = history

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(str(tmp_path)),
            rule_engine=RuleEngine(),
            mode=PermissionMode.DEFAULT,
        )
        agent = Agent(
            SimpleNamespace(),
            tool_registry,
            "anthropic",
            work_dir=str(root_a),
            permission_checker=checker,
        )
        agent.file_history = history

        write_stream = agent._execute_tool(
            ToolCallComplete(
                "write-after-wait",
                "WriteFile",
                {"file_path": "created.txt", "content": "captured"},
            )
        )
        write_request = await anext(write_stream)
        assert isinstance(write_request, PermissionRequest)
        agent.work_dir = str(root_b)
        write_request.future.set_result(PermissionResponse.ALLOW)
        write_result, _, _ = await anext(write_stream)

        assert not write_result.is_error
        assert (root_a / "created.txt").read_text() == "captured"
        assert not (root_b / "created.txt").exists()
        assert history.paths == [str((root_a / "created.txt").resolve())]

        checker.mode = PermissionMode.CUSTOM
        agent.work_dir = str(root_a)
        with tool_working_directory(root_b):
            read_stream = agent._execute_tool(
                ToolCallComplete(
                    "read-after-wait",
                    "ReadFile",
                    {"file_path": "note.txt"},
                )
            )
            read_request = await anext(read_stream)
            assert isinstance(read_request, PermissionRequest)
            agent.work_dir = str(root_b)
            read_request.future.set_result(PermissionResponse.ALLOW)
            read_result, _, _ = await anext(read_stream)

        assert not read_result.is_error
        assert "approved content" in read_result.output
        snapshots = agent.recovery_state.snapshot_files(limit=10)
        assert len(snapshots) == 1
        assert snapshots[0].path == str((root_a / "note.txt").resolve())
        assert snapshots[0].content == "approved content"

# =========================================================================
# F. 变更检测与自动清理
# =========================================================================

class TestChangeDetection:
    @pytest.mark.asyncio
    async def test_clean_worktree(self, manager):
        wt = await manager.create("clean-wt")
        assert not has_worktree_changes(wt.path, wt.head_commit)

    @pytest.mark.asyncio
    async def test_uncommitted_changes(self, manager):
        wt = await manager.create("dirty-wt")
        (Path(wt.path) / "dirty.txt").write_text("new")
        assert has_worktree_changes(wt.path, wt.head_commit)

    @pytest.mark.asyncio
    async def test_new_commits(self, manager):
        wt = await manager.create("commit-wt")
        assert wt.head_commit, "head_commit should not be empty after create"
        (Path(wt.path) / "committed.txt").write_text("new")
        subprocess.run(["git", "add", "."], cwd=wt.path, capture_output=True, check=True)
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=t@t",
             "commit", "-m", "test"],
            cwd=wt.path, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"commit failed: {result.stderr}"
        changes = count_worktree_changes(wt.path, wt.head_commit)
        assert changes.new_commits > 0

    @pytest.mark.asyncio
    async def test_auto_cleanup_removes_clean(self, manager):
        wt = await manager.create("auto-clean")
        result = await manager.auto_cleanup("auto-clean", wt.head_commit)
        assert not result.kept
        assert "auto-clean" not in manager.active

    @pytest.mark.asyncio
    async def test_auto_cleanup_keeps_dirty(self, manager):
        wt = await manager.create("auto-dirty")
        (Path(wt.path) / "file.txt").write_text("content")
        result = await manager.auto_cleanup("auto-dirty", wt.head_commit)
        assert result.kept
        assert result.path == wt.path
        assert "auto-dirty" in manager.active

# =========================================================================
# D4. read_worktree_head_sha
# =========================================================================

class TestReadWorktreeHeadSha:
    @pytest.mark.asyncio
    async def test_valid_worktree(self, manager):
        wt = await manager.create("sha-test")
        sha = WorktreeManager.read_worktree_head_sha(wt.path)
        assert sha is not None
        assert len(sha) == 40

    def test_nonexistent_dir(self):
        sha = WorktreeManager.read_worktree_head_sha("/nonexistent/path")
        assert sha is None

    def test_not_a_worktree(self, tmp_path):
        sha = WorktreeManager.read_worktree_head_sha(str(tmp_path))
        assert sha is None


class TestAppWorktreeContext:
    def test_sync_updates_agent_sandbox_caches_and_conversation(self, tmp_path):
        from likecc.app import LikeCCApp

        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        old_dir.mkdir()
        new_dir.mkdir()
        checker = SimpleNamespace(sandbox=PathSandbox(str(old_dir)))
        app = LikeCCApp(providers=[])
        app.agent = SimpleNamespace(
            work_dir=str(old_dir),
            permission_checker=checker,
        )
        app.file_cache.put("/old/file", "stale")
        app.conversation.inject_environment(
            f"Current working directory: {old_dir}"
        )

        app._sync_worktree_context(str(new_dir))

        assert app.agent.work_dir == str(new_dir)
        assert checker.sandbox.project_root == new_dir.resolve()
        assert app.file_cache.get("/old/file") is None
        assert (
            f"Current working directory changed to: {new_dir}"
            in app.conversation.history[-1].content
        )
