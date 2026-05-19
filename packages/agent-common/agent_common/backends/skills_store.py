"""Read-only backend serving three-tier resolved skills at /skills/{name}/..."""

from __future__ import annotations

import logging
import re

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import create_file_data

from agent_common.core.skill_frontmatter import build_skill_content
from agent_common.models.skill import ResolvedSkill

logger = logging.getLogger(__name__)

_READ_ONLY_MSG = (
    "/skills/ is read-only. Use console_create_skill, console_update_skill, "
    "or console_write_skill_file to manage skills."
)


class SkillsStoreBackend(BackendProtocol):
    """Read-only backend serving merged skills at /skills/{name}/...

    Skills are pre-resolved by the upstream skills_resolver in order of precedence
    (personal > group > standard) and passed as an in-memory dictionary. This backend
    operates entirely against that dictionary.

    Instantiated once per agent invocation.
    """

    def __init__(self, merged_skills: dict[str, ResolvedSkill]):
        self._skills = merged_skills

    # ---- read operations ----

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        name, rel = self._parse_path(file_path)
        if not name:
            return ReadResult(error=f"Invalid path: {file_path}")

        skill = self._skills.get(name)
        if not skill:
            return ReadResult(error=f"Skill not found: {name}")

        if rel == "SKILL.md":
            content = build_skill_content(
                name=skill.name,
                description=skill.description,
                body=skill.body,
            )
            encoding = "utf-8"
        else:
            # Look for bundled file
            matched = next((f for f in skill.files if f.path == rel), None)
            if not matched:
                return ReadResult(error=f"File not found: {file_path}")
            content = matched.content
            encoding = matched.encoding or "utf-8"

        # Apply offset/limit (line-based)
        lines = content.splitlines(keepends=True)
        sliced = lines[offset : offset + limit]
        return ReadResult(file_data=create_file_data("".join(sliced), encoding=encoding))

    async def als(self, path: str) -> LsResult:
        normalized = path.rstrip("/") + "/"

        if normalized == "/skills/" or normalized == "/":
            entries = [FileInfo(path=f"/{name}/", is_dir=True) for name in sorted(self._skills)]
            return LsResult(entries=entries)

        # /skills/{name}/ — list files in a skill
        name, _ = self._parse_path(path)
        if name and name in self._skills:
            skill = self._skills[name]
            entries: list[FileInfo] = [FileInfo(path=f"/{name}/SKILL.md")]
            for f in skill.files:
                entries.append(FileInfo(path=f"/{name}/{f.path}"))
            return LsResult(entries=entries)

        return LsResult(error=f"Directory not found: {path}")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        pattern_lower = pattern.lower()
        matches: list[GrepMatch] = []

        # Compile glob filter if provided
        glob_re = self._glob_to_regex(glob) if glob else None

        for name, skill in self._skills.items():
            # SKILL.md
            skill_path = f"/{name}/SKILL.md"
            if (not glob_re or glob_re.search(skill_path)) and (not path or skill_path.startswith(path)):
                content = build_skill_content(name=skill.name, description=skill.description, body=skill.body)
                for i, line in enumerate(content.splitlines()):
                    if pattern_lower in line.lower():
                        matches.append(GrepMatch(path=skill_path, line=i + 1, text=line))

            # Bundled files
            for f in skill.files:
                fpath = f"/{name}/{f.path}"
                if (glob_re and not glob_re.search(fpath)) or (path and not fpath.startswith(path)):
                    continue
                for i, line in enumerate(f.content.splitlines()):
                    if pattern_lower in line.lower():
                        matches.append(GrepMatch(path=fpath, line=i + 1, text=line))

        return GrepResult(matches=matches)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        entries: list[FileInfo] = []
        glob_re = self._glob_to_regex(pattern)
        for name, skill in self._skills.items():
            skill_path = f"/{name}/SKILL.md"
            if skill_path.startswith(path) and glob_re.search(skill_path):
                entries.append(FileInfo(path=skill_path))
            for f in skill.files:
                fpath = f"/{name}/{f.path}"
                if fpath.startswith(path) and glob_re.search(fpath):
                    entries.append(FileInfo(path=fpath))
        return GlobResult(matches=entries)

    # ---- write operations (blocked) ----

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY_MSG)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=_READ_ONLY_MSG)

    # ---- sync stubs (required by protocol, delegate to async) ----

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        raise NotImplementedError("Use aread()")

    def ls(self, path: str) -> LsResult:
        raise NotImplementedError("Use als()")

    def write(self, file_path: str, content: str) -> WriteResult:
        raise NotImplementedError("Use awrite()")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        raise NotImplementedError("Use aedit()")

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        raise NotImplementedError("Use agrep()")

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        raise NotImplementedError("Use aglob()")

    # ---- helpers ----

    async def alist_recursive(self, prefix: str = "/skills/") -> list[str]:
        """List all file paths recursively under the given prefix.

        Returns flat list of absolute paths (e.g., /skills/my-skill/SKILL.md).
        Used by SkillSandboxSyncMiddleware to enumerate files for upload.
        """
        paths: list[str] = []
        for name, skill in self._skills.items():
            paths.append(f"/skills/{name}/SKILL.md")
            for f in skill.files:
                paths.append(f"/skills/{name}/{f.path}")
        return paths

    @staticmethod
    def _glob_to_regex(pattern: str) -> re.Pattern[str]:
        """Convert a glob pattern to a compiled regex.

        Supports: * (single segment), ** (cross-directory), ? (single char), [abc] (set).
        All other characters are regex-escaped.
        """
        i, n = 0, len(pattern)
        parts: list[str] = []
        while i < n:
            c = pattern[i]
            if c == "*":
                if i + 1 < n and pattern[i + 1] == "*":
                    parts.append(".*")
                    i += 2
                    # Skip trailing / after ** (e.g., **/ means "any dirs")
                    if i < n and pattern[i] == "/":
                        i += 1
                else:
                    parts.append("[^/]*")
                    i += 1
            elif c == "?":
                parts.append("[^/]")
                i += 1
            elif c == "[":
                # Pass through character class verbatim until ]
                j = i + 1
                while j < n and pattern[j] != "]":
                    j += 1
                parts.append(pattern[i : j + 1])
                i = j + 1
            else:
                parts.append(re.escape(c))
                i += 1
        return re.compile("".join(parts))

    @staticmethod
    def _parse_path(path: str) -> tuple[str | None, str]:
        """Parse /skills/{name}/{rest} into (name, rest).

        Returns (None, "") if path doesn't match expected pattern.
        """
        cleaned = path.strip("/")
        # Remove "skills/" prefix if present
        if cleaned.startswith("skills/"):
            cleaned = cleaned[len("skills/") :]
        elif cleaned == "skills":
            return (None, "")

        parts = cleaned.split("/", 1)
        name = parts[0]
        rest = parts[1] if len(parts) > 1 else "SKILL.md"
        return (name, rest)
