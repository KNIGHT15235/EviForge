
from __future__ import annotations

import tempfile
from pathlib import Path

from eviforge.tools.work_dir import resolve_tool_path


class PathSandbox:


    def __init__(
        self,
        project_root: str,
        extra_allowed: list[str] | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        self._allowed_roots: list[Path] = [root, Path(tempfile.gettempdir()).resolve()]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())


    @property
    def project_root(self) -> Path:
        return self._allowed_roots[0]

    def set_project_root(self, project_root: str) -> None:
        """Rebase relative-path checks while preserving explicit extra roots."""
        self._allowed_roots[0] = Path(project_root).resolve()


    def check(self, path: str) -> tuple[bool, str]:
        abs_path = resolve_tool_path(path, default=self.project_root)

        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            ancestor = abs_path
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            try:
                resolved_ancestor = ancestor.resolve(strict=True)
            except OSError:
                return False, f"无法解析路径: {path}"
            real_path = resolved_ancestor / abs_path.relative_to(ancestor)

        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        return False, f"路径 {path} 超出沙箱范围"
