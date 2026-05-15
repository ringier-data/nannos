"""Skills Registry Service.

Handles discovery and fetching of skills from external sources:
- Primary: skills.sh API (search, detail, audit endpoints)
- Fallback: GitHub Git Trees API (for unindexed repos / private repos)
"""

import logging
from base64 import b64decode

import httpx

from console_backend.config import config
from console_backend.models.skills_registry import (
    GitHubSkillDetail,
    SkillAuditEntry,
    SkillAuditResponse,
    SkillDetailResponse,
    SkillFile,
    SkillSearchResult,
)

logger = logging.getLogger(__name__)

# Well-known directories where skills are conventionally stored in repos
_KNOWN_SKILL_DIRS = ["skills", ".claude/skills", ".agents/skills"]


class SkillsRegistryService:
    """Service for discovering and fetching skills from external registries."""

    def __init__(self) -> None:
        self._skills_sh_base = config.skills_registry.skills_sh_base_url.rstrip("/")
        self._github_base = config.skills_registry.github_api_base_url.rstrip("/")

    def _skills_sh_headers(self) -> dict[str, str]:
        """Build headers for skills.sh API requests."""
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = config.skills_registry.skills_sh_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        return headers

    def _github_headers(self) -> dict[str, str]:
        """Build headers for GitHub API requests."""
        headers: dict[str, str] = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = config.skills_registry.github_token
        if token:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        return headers

    async def search_skills(self, query: str, limit: int = 50) -> tuple[list[SkillSearchResult], str | None]:
        """Search for skills via skills.sh API.

        Uses semantic search for multi-word queries, fuzzy for single-word.
        Returns (results, search_type) where search_type is 'fuzzy' or 'semantic'.
        """
        if not query or len(query) < 2:
            return [], None

        url = f"{self._skills_sh_base}/api/v1/skills/search"
        params = {"q": query, "limit": min(limit, 200)}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=self._skills_sh_headers())
            if resp.status_code != 200:
                logger.warning("skills.sh search failed: %s %s", resp.status_code, resp.text[:200])
                return [], None

            data = resp.json()
            results = []
            for item in data.get("data", []):
                # Skip duplicates/forks
                if item.get("isDuplicate"):
                    continue
                results.append(
                    SkillSearchResult(
                        id=item["id"],
                        slug=item["slug"],
                        name=item.get("name", item["slug"]),
                        source=item["source"],
                        installs=item.get("installs", 0),
                        source_type=item.get("sourceType", "github"),
                        install_url=item.get("installUrl"),
                        url=item.get("url"),
                    )
                )
            search_type = data.get("searchType")
            return results, search_type

    async def browse_repo(self, repo: str, ref: str = "main") -> list[SkillSearchResult]:
        """Browse available skills in a GitHub repository.

        Strategy:
        1. Fetch full recursive tree
        2. Find all SKILL.md files
        3. Parse frontmatter from each to get name/description
        """
        owner, repo_name = self._parse_repo(repo)
        tree = await self._fetch_github_tree(owner, repo_name, ref)
        if tree is None:
            return []

        # Find all SKILL.md entries
        skill_md_entries = [
            entry for entry in tree if entry.get("type") == "blob" and entry.get("path", "").endswith("/SKILL.md")
        ]

        results = []
        for entry in skill_md_entries:
            path = entry["path"]
            # Extract skill name from path: "skills/my-skill/SKILL.md" -> "my-skill"
            parts = path.rsplit("/", 2)
            if len(parts) >= 2:
                skill_name = parts[-2]
            else:
                continue

            # Fetch SKILL.md content to get description from frontmatter
            content = await self._fetch_blob(owner, repo_name, entry["sha"])
            description = ""
            if content:
                description = self._extract_description_from_frontmatter(content)

            results.append(
                SkillSearchResult(
                    id=f"{repo}/{skill_name}",
                    slug=skill_name,
                    name=skill_name,
                    source=repo,
                    installs=0,
                    url=f"https://github.com/{repo}/tree/{ref}/{'/'.join(parts[:-1])}",
                    source_type="github",
                )
            )

        return results

    async def get_skill_detail(self, skill_id: str) -> SkillDetailResponse | None:
        """Get skill metadata and files from skills.sh.

        Returns full detail (id, source, slug, installs, hash, files) or None if not found.
        """
        url = f"{self._skills_sh_base}/api/v1/skills/{skill_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._skills_sh_headers())
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning("skills.sh detail failed: %s %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            files = None
            if data.get("files") is not None:
                files = [SkillFile(path=f["path"], contents=f["contents"]) for f in data["files"]]
            return SkillDetailResponse(
                id=data["id"],
                source=data["source"],
                slug=data["slug"],
                installs=data.get("installs", 0),
                hash=data.get("hash"),
                files=files,
            )

    async def fetch_skill_files_from_github(
        self, repo: str, skill_name: str, ref: str = "main"
    ) -> GitHubSkillDetail | None:
        """Fetch a skill's files from a GitHub repository.

        Strategy:
        1. Try known directories first (fast path)
        2. Fall back to full tree scan

        Returns GitHubSkillDetail (files + tree_sha) or None if not found.
        """
        owner, repo_name = self._parse_repo(repo)

        # Fast path: try known directories
        for known_dir in _KNOWN_SKILL_DIRS:
            path = f"{known_dir}/{skill_name}/SKILL.md"
            content = await self._fetch_file_contents(owner, repo_name, path, ref)
            if content is not None:
                # Found it — now fetch all files in this skill directory
                skill_dir = f"{known_dir}/{skill_name}"
                files = await self._fetch_directory_files(owner, repo_name, skill_dir, ref)
                if files:
                    # Get tree SHA for the directory
                    tree_sha = await self._get_directory_tree_sha(owner, repo_name, skill_dir, ref)
                    return GitHubSkillDetail(files=files, tree_sha=tree_sha)

        # Fallback: full tree scan
        tree = await self._fetch_github_tree(owner, repo_name, ref)
        if tree is None:
            return None

        # Find the skill's SKILL.md in the tree
        skill_md_path = None
        for entry in tree:
            if entry.get("type") == "blob" and entry.get("path", "").endswith(f"/{skill_name}/SKILL.md"):
                skill_md_path = entry["path"]
                break

        if not skill_md_path:
            # Try root-level skill (repo IS the skill)
            for entry in tree:
                if entry.get("type") == "blob" and entry.get("path") == "SKILL.md":
                    skill_md_path = "SKILL.md"
                    break

        if not skill_md_path:
            return None

        # Determine skill directory
        if skill_md_path == "SKILL.md":
            skill_dir_prefix = ""
        else:
            skill_dir_prefix = skill_md_path.rsplit("/SKILL.md", 1)[0]

        # Collect all files in that directory
        files: list[SkillFile] = []
        tree_sha = None
        for entry in tree:
            entry_path = entry.get("path", "")
            if entry.get("type") == "blob":
                if skill_dir_prefix:
                    if entry_path.startswith(skill_dir_prefix + "/"):
                        relative_path = entry_path[len(skill_dir_prefix) + 1 :]
                        blob_content = await self._fetch_blob(owner, repo_name, entry["sha"])
                        if blob_content is not None:
                            files.append(SkillFile(path=relative_path, contents=blob_content))
                else:
                    # Root-level skill — include all files
                    blob_content = await self._fetch_blob(owner, repo_name, entry["sha"])
                    if blob_content is not None:
                        files.append(SkillFile(path=entry_path, contents=blob_content))
            elif entry.get("type") == "tree" and entry.get("path") == skill_dir_prefix:
                tree_sha = entry.get("sha")

        if not files:
            return None

        return GitHubSkillDetail(files=files, tree_sha=tree_sha)

    async def get_skill_audit(self, skill_id: str) -> SkillAuditResponse | None:
        """Get security audit for a skill from skills.sh.

        Returns SkillAuditResponse or None if no audits exist.
        """
        url = f"{self._skills_sh_base}/api/v1/skills/audit/{skill_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._skills_sh_headers())
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning("skills.sh audit failed: %s %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            audits = [
                SkillAuditEntry(
                    provider=a["provider"],
                    slug=a["slug"],
                    status=a["status"],
                    summary=a["summary"],
                    audited_at=a["auditedAt"],
                    risk_level=a.get("riskLevel"),
                    categories=a.get("categories"),
                )
                for a in data.get("audits", [])
            ]
            return SkillAuditResponse(
                id=data["id"],
                source=data["source"],
                slug=data["slug"],
                audits=audits,
            )

    # ─── Private helpers ─────────────────────────────────────────────────────

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        """Parse 'owner/repo' into (owner, repo_name)."""
        parts = repo.strip("/").split("/")
        if len(parts) < 2:
            raise ValueError(f"Invalid repo format, expected 'owner/repo': {repo}")
        return parts[0], parts[1]

    async def _fetch_github_tree(self, owner: str, repo_name: str, ref: str) -> list[dict] | None:
        """Fetch recursive tree from GitHub."""
        url = f"{self._github_base}/repos/{owner}/{repo_name}/git/trees/{ref}?recursive=1"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._github_headers())
            if resp.status_code != 200:
                logger.warning("GitHub tree fetch failed for %s/%s: %s", owner, repo_name, resp.status_code)
                return None
            data = resp.json()
            return data.get("tree", [])

    async def _fetch_blob(self, owner: str, repo_name: str, sha: str) -> str | None:
        """Fetch a blob's content from GitHub by SHA."""
        url = f"{self._github_base}/repos/{owner}/{repo_name}/git/blobs/{sha}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self._github_headers())
            if resp.status_code != 200:
                return None
            data = resp.json()
            encoding = data.get("encoding", "base64")
            content = data.get("content", "")
            if encoding == "base64":
                return b64decode(content).decode("utf-8", errors="replace")
            return content

    async def _fetch_file_contents(self, owner: str, repo_name: str, path: str, ref: str) -> str | None:
        """Fetch a single file via GitHub Contents API."""
        url = f"{self._github_base}/repos/{owner}/{repo_name}/contents/{path}"
        params = {"ref": ref}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=self._github_headers())
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("type") != "file":
                return None
            content = data.get("content", "")
            encoding = data.get("encoding", "base64")
            if encoding == "base64":
                return b64decode(content).decode("utf-8", errors="replace")
            return content

    async def _fetch_directory_files(
        self, owner: str, repo_name: str, dir_path: str, ref: str
    ) -> list[SkillFile] | None:
        """Fetch all files in a directory (non-recursive) via GitHub Contents API."""
        url = f"{self._github_base}/repos/{owner}/{repo_name}/contents/{dir_path}"
        params = {"ref": ref}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=self._github_headers())
            if resp.status_code != 200:
                return None
            entries = resp.json()
            if not isinstance(entries, list):
                return None

        files: list[SkillFile] = []
        for entry in entries:
            if entry.get("type") == "file":
                content = await self._fetch_file_contents(owner, repo_name, entry["path"], ref)
                if content is not None:
                    # Store relative path (strip the dir prefix)
                    relative = (
                        entry["path"][len(dir_path) + 1 :]
                        if entry["path"].startswith(dir_path + "/")
                        else entry["name"]
                    )
                    files.append(SkillFile(path=relative, contents=content))
            elif entry.get("type") == "dir":
                # Recurse one level for scripts/, references/, assets/
                sub_files = await self._fetch_directory_files(owner, repo_name, entry["path"], ref)
                if sub_files:
                    for sf in sub_files:
                        sub_relative = entry["name"] + "/" + sf.path
                        files.append(SkillFile(path=sub_relative, contents=sf.contents))
        return files

    async def _get_directory_tree_sha(self, owner: str, repo_name: str, dir_path: str, ref: str) -> str | None:
        """Get the tree SHA for a specific directory."""
        # Fetch the tree at ref level and find the subtree entry
        url = f"{self._github_base}/repos/{owner}/{repo_name}/git/trees/{ref}?recursive=1"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._github_headers())
            if resp.status_code != 200:
                return None
            data = resp.json()
            for entry in data.get("tree", []):
                if entry.get("type") == "tree" and entry.get("path") == dir_path:
                    return entry.get("sha")
        return None

    def _extract_description_from_frontmatter(self, content: str) -> str:
        """Extract description from SKILL.md YAML frontmatter."""
        if not content.startswith("---"):
            return ""
        end_idx = content.find("---", 3)
        if end_idx == -1:
            return ""
        frontmatter = content[3:end_idx]
        for line in frontmatter.split("\n"):
            line = line.strip()
            if line.startswith("description:"):
                desc = line[len("description:") :].strip()
                # Remove surrounding quotes if present
                if (desc.startswith('"') and desc.endswith('"')) or (desc.startswith("'") and desc.endswith("'")):
                    desc = desc[1:-1]
                return desc
        return ""


# Singleton instance
skills_registry_service = SkillsRegistryService()
