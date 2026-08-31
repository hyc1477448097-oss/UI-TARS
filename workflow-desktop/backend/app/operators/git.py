import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitOperator:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def _run(self, *args: str) -> str:
        if not self.repo_path.exists():
            raise GitError(f"仓库路径不存在: {self.repo_path}")
        result = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise GitError(f"git {' '.join(args)} 失败: {detail}")
        return (result.stdout or "").strip()

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def prepare_test(self, test_branch: str, merge_from: str) -> list[str]:
        logs: list[str] = []
        source = self.current_branch() if merge_from == "current" else merge_from
        logs.append(f"当前分支: {self.current_branch()}，将合并: {source}")
        logs.append(self._run("fetch", "--all", "--prune") or "git fetch 完成")
        logs.append(self._run("checkout", test_branch) or f"已切换到 {test_branch}")
        pull = self._run("pull", "--ff-only")
        logs.append(pull or "git pull 完成")
        merge_out = self._run("merge", "--no-edit", source)
        logs.append(merge_out or f"已合并 {source} -> {test_branch}")
        return logs
