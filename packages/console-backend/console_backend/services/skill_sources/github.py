"""GitHub source — skill discovery and file fetching via GitHub API.

Provides:
- search (browse a repo for SKILL.md directories)
- fetch_skill (resolve repo+skill to files)

This is both a SkillSource (for discovery) and the primary file-fetching
mechanism used during import.
"""

import logging
import time
from base64 import b64decode

import httpx
import jwt

from console_backend.config import config
from console_backend.models.skills_registry import SkillFile, SkillSearchResult
from console_backend.services.skill_sources.base import SkillSource, SkillSourceDetail

logger = logging.getLogger(__name__)

# Well-known directories where skills are conventionally stored in repos
_KNOWN_SKILL_DIRS = ["skills", ".claude/skills", ".agents/skills"]


class GitHubSource(SkillSource):
    """Skill source backed by GitHub API for repo browsing and file fetching."""

    def __init__(self) -> None:
        self._base = config.skills_registry.github_api_base_url.rstrip("/")
        self._installation_token: str | None = None
        self._token_expires_at: float = 0

    @property
    def name(self) -> str:
        return "github"

    async def _get_token(self) -> str | None:
        """Get a valid token, preferring GitHub App installation token over static PAT."""
        # If a static PAT is configured, use it (local dev fallback)
        static_token = config.skills_registry.github_token
        if static_token:
            return static_token.get_secret_value()

        # GitHub App: generate installation token
        app_id = config.skills_registry.github_app_id
        private_key = config.skills_registry.github_app_private_key
        if not app_id or not private_key:
            return None

        # Reuse cached token if still valid (with 60s buffer)
        if self._installation_token and time.time() < self._token_expires_at - 60:
            return self._installation_token

        # Generate JWT
        now = int(time.time())
        payload = {
            "iat": now - 60,  # slight backdate for clock skew
            "exp": now + 600,  # 10 min max
            "iss": app_id,
        }
        # Resolve PEM key: supports inline PEM, file path, or literal \n
        pem_key = private_key.get_secret_value()
        if pem_key.startswith("/") or pem_key.startswith("~"):
            # File path to .pem
            import os

            key_path = os.path.expanduser(pem_key)
            try:
                with open(key_path) as f:
                    pem_key = f.read()
            except OSError:
                logger.error("Cannot read GitHub App private key file: %s", key_path)
                return None
        else:
            pem_key = pem_key.replace("\\n", "\n")

        if not pem_key.startswith("-----BEGIN"):
            logger.error(
                "GITHUB_APP_PRIVATE_KEY is not a PEM private key. "
                "Generate one from: GitHub App settings → Private keys → Generate a private key"
            )
            return None

        try:
            app_jwt = jwt.encode(payload, pem_key, algorithm="RS256")
        except Exception as e:
            logger.error("Failed to sign GitHub App JWT: %s", e)
            return None

        # Get installation ID
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._base}/app/installations",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if resp.status_code != 200:
                logger.error("Failed to list GitHub App installations: %s", resp.status_code)
                return None

            installations = resp.json()
            if not installations:
                logger.error("GitHub App has no installations")
                return None

            installation_id = installations[0]["id"]

            # Create installation access token
            resp = await client.post(
                f"{self._base}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if resp.status_code != 201:
                logger.error("Failed to create installation token: %s", resp.status_code)
                return None

            data = resp.json()
            self._installation_token = data["token"]
            # Tokens expire in 1 hour; parse or default to 55 min from now
            self._token_expires_at = time.time() + 3300
            logger.info("Generated GitHub App installation token (expires in ~55 min)")
            return self._installation_token

    async def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = await self._get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def search(self, query: str, limit: int = 50) -> list[SkillSearchResult]:
        """Browse a repo for skills. Query format: 'owner/repo' with optional '@ref'.

        Example: 'anthropics/skills' or 'anthropics/skills@main'
        """
        results, _ = await self.browse(query, limit=limit)
        return results

    async def browse(self, query: str, limit: int = 50, offset: int = 0) -> tuple[list[SkillSearchResult], int]:
        """Browse a repo for skills with pagination. Returns (results, total).

        Query format: 'owner/repo' with optional '@ref'.
        Example: 'anthropics/skills' or 'anthropics/skills@main'
        """
        ref = "main"
        repo = query
        if "@" in query:
            repo, ref = query.rsplit("@", 1)

        parts = repo.strip("/").split("/")
        if len(parts) != 2:
            return [], 0

        owner, repo_name = parts
        tree = await self._fetch_tree(owner, repo_name, ref)
        if tree is None:
            return [], 0

        skill_md_entries = [
            entry for entry in tree if entry.get("type") == "blob" and entry.get("path", "").endswith("/SKILL.md")
        ]
        total = len(skill_md_entries)

        results = []
        for entry in skill_md_entries[offset : offset + limit]:
            path = entry["path"]
            path_parts = path.rsplit("/", 2)
            if len(path_parts) >= 2:
                skill_name = path_parts[-2]
            else:
                continue

            content = await self._fetch_blob(owner, repo_name, entry["sha"])
            description = ""
            if content:
                text_content, _ = content
                description = _extract_description(text_content)

            results.append(
                SkillSearchResult(
                    id=f"{repo}/{skill_name}",
                    slug=skill_name,
                    name=skill_name,
                    source=repo,
                    installs=0,
                    url=f"https://github.com/{repo}/tree/{ref}/{'/'.join(path_parts[:-1])}",
                    source_type="github",
                )
            )

        return results, total

    async def fetch_skill(self, source_id: str) -> SkillSourceDetail | None:
        """Fetch a skill from GitHub.

        source_id format: 'owner/repo/skill-name' or 'owner/repo' (repo IS the skill).
        Optional '@ref' suffix: 'owner/repo/skill@v2'
        """
        ref = "main"
        if "@" in source_id:
            source_id, ref = source_id.rsplit("@", 1)

        parts = source_id.strip("/").split("/")
        if len(parts) < 2:
            return None

        owner = parts[0]
        repo_name = parts[1]
        repo = f"{owner}/{repo_name}"
        skill_name = parts[2] if len(parts) >= 3 else repo_name

        files, tree_sha, source_path = await self._fetch_skill_files(owner, repo_name, skill_name, ref)
        if not files:
            return None

        # Extract description from SKILL.md
        description = ""
        for f in files:
            if f.path == "SKILL.md":
                description = _extract_description(f.content)
                break

        return SkillSourceDetail(
            name=skill_name,
            slug=skill_name,
            description=description,
            files=files,
            source_repo=repo,
            source_ref=ref,
            source_path=source_path,
            tree_sha=tree_sha,
        )

    async def get_tree_sha(self, source_id: str) -> str | None:
        """Get the tree SHA for a skill without fetching file contents.

        source_id format: same as fetch_skill ('owner/repo/skill-name@ref').
        Returns the git tree SHA or None if the skill directory isn't found.
        """
        ref = "main"
        if "@" in source_id:
            source_id, ref = source_id.rsplit("@", 1)

        parts = source_id.strip("/").split("/")
        if len(parts) < 2:
            return None

        owner = parts[0]
        repo_name = parts[1]
        skill_name = parts[2] if len(parts) >= 3 else repo_name

        # Try known directories first (fast path)
        for known_dir in _KNOWN_SKILL_DIRS:
            skill_dir = f"{known_dir}/{skill_name}"
            sha = await self._get_tree_sha(owner, repo_name, skill_dir, ref)
            if sha is not None:
                return sha

        # Fallback: full tree scan for the skill directory
        tree = await self._fetch_tree(owner, repo_name, ref)
        if tree is None:
            return None

        for entry in tree:
            if entry.get("type") == "tree":
                path = entry.get("path", "")
                if path.endswith(f"/{skill_name}") or path == skill_name:
                    return entry.get("sha")
            # Root-level skill (repo IS the skill) — return the root tree SHA
            if entry.get("type") == "tree" and entry.get("path") == "" and skill_name == repo_name:
                return entry.get("sha")

        return None

    # ─── File fetching (used by both search and import) ──────────────────────

    async def _fetch_skill_files(
        self, owner: str, repo_name: str, skill_name: str, ref: str
    ) -> tuple[list[SkillFile], str | None, str | None]:
        """Fetch all files for a skill. Returns (files, tree_sha, source_path).

        Strategy:
        1. Try known directories first (fast path)
        2. Fall back to full tree scan
        """
        # Fast path: try known directories
        for known_dir in _KNOWN_SKILL_DIRS:
            path = f"{known_dir}/{skill_name}/SKILL.md"
            result = await self._fetch_file_contents(owner, repo_name, path, ref)
            if result is not None:
                skill_dir = f"{known_dir}/{skill_name}"
                files = await self._fetch_directory_files(owner, repo_name, skill_dir, ref)
                if files:
                    tree_sha = await self._get_tree_sha(owner, repo_name, skill_dir, ref)
                    return files, tree_sha, skill_dir

        # Fallback: full tree scan
        tree = await self._fetch_tree(owner, repo_name, ref)
        if tree is None:
            return [], None, None

        # Find the skill's SKILL.md in the tree
        skill_md_path = None
        target_suffix = f"/{skill_name}/SKILL.md"
        target_exact = f"{skill_name}/SKILL.md"
        for entry in tree:
            if entry.get("type") == "blob":
                path = entry.get("path", "")
                if path.endswith(target_suffix) or path == target_exact:
                    skill_md_path = path
                    break

        if not skill_md_path:
            # Try root-level skill (repo IS the skill)
            for entry in tree:
                if entry.get("type") == "blob" and entry.get("path") == "SKILL.md":
                    skill_md_path = "SKILL.md"
                    break

        if not skill_md_path:
            return [], None, None

        # Determine skill directory
        if skill_md_path == "SKILL.md":
            skill_dir_prefix = ""
        else:
            skill_dir_prefix = skill_md_path.rsplit("/SKILL.md", 1)[0]

        # Collect all files
        files: list[SkillFile] = []
        tree_sha = None
        for entry in tree:
            entry_path = entry.get("path", "")
            if entry.get("type") == "blob":
                if skill_dir_prefix:
                    if entry_path.startswith(skill_dir_prefix + "/"):
                        relative_path = entry_path[len(skill_dir_prefix) + 1 :]
                        result = await self._fetch_blob(owner, repo_name, entry["sha"])
                        if result is not None:
                            blob_content, encoding = result
                            files.append(SkillFile(path=relative_path, content=blob_content, encoding=encoding))
                else:
                    result = await self._fetch_blob(owner, repo_name, entry["sha"])
                    if result is not None:
                        blob_content, encoding = result
                        files.append(SkillFile(path=entry_path, content=blob_content, encoding=encoding))
            elif entry.get("type") == "tree" and entry.get("path") == skill_dir_prefix:
                tree_sha = entry.get("sha")

        return files, tree_sha, skill_dir_prefix or None

    # ─── GitHub API helpers ──────────────────────────────────────────────────

    async def _fetch_tree(self, owner: str, repo_name: str, ref: str) -> list[dict] | None:
        url = f"{self._base}/repos/{owner}/{repo_name}/git/trees/{ref}?recursive=1"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("GitHub tree fetch failed for %s/%s: %s", owner, repo_name, resp.status_code)
                return None
            return resp.json().get("tree", [])

    async def _fetch_blob(self, owner: str, repo_name: str, sha: str) -> tuple[str, str | None] | None:
        """Fetch a blob from GitHub. Returns (content, encoding) or None.

        Text files return (decoded_text, None).
        Binary files return (base64_content, "base64").
        """
        url = f"{self._base}/repos/{owner}/{repo_name}/git/blobs/{sha}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = data.get("content", "")
            if data.get("encoding") == "base64":
                raw = b64decode(content)
                if b"\x00" in raw:
                    # Binary file — keep as base64
                    return content, "base64"
                return raw.decode("utf-8", errors="replace"), None
            return content, None

    async def _fetch_file_contents(
        self, owner: str, repo_name: str, path: str, ref: str
    ) -> tuple[str, str | None] | None:
        """Fetch file via Contents API. Returns (content, encoding) or None."""
        url = f"{self._base}/repos/{owner}/{repo_name}/contents/{path}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"ref": ref}, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("type") != "file":
                return None
            content = data.get("content", "")
            if data.get("encoding") == "base64":
                raw = b64decode(content)
                if b"\x00" in raw:
                    return content, "base64"
                return raw.decode("utf-8", errors="replace"), None
            return content, None

    async def _fetch_directory_files(self, owner: str, repo_name: str, dir_path: str, ref: str) -> list[SkillFile]:
        url = f"{self._base}/repos/{owner}/{repo_name}/contents/{dir_path}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={"ref": ref}, headers=headers)
            if resp.status_code != 200:
                return []
            entries = resp.json()
            if not isinstance(entries, list):
                return []

        files: list[SkillFile] = []
        for entry in entries:
            if entry.get("type") == "file":
                result = await self._fetch_file_contents(owner, repo_name, entry["path"], ref)
                if result is not None:
                    file_content, encoding = result
                    relative = (
                        entry["path"][len(dir_path) + 1 :]
                        if entry["path"].startswith(dir_path + "/")
                        else entry["name"]
                    )
                    files.append(SkillFile(path=relative, content=file_content, encoding=encoding))
            elif entry.get("type") == "dir":
                sub_files = await self._fetch_directory_files(owner, repo_name, entry["path"], ref)
                for sf in sub_files:
                    files.append(SkillFile(path=f"{entry['name']}/{sf.path}", content=sf.content, encoding=sf.encoding))
        return files

    async def _get_tree_sha(self, owner: str, repo_name: str, dir_path: str, ref: str) -> str | None:
        url = f"{self._base}/repos/{owner}/{repo_name}/git/trees/{ref}?recursive=1"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            for entry in resp.json().get("tree", []):
                if entry.get("type") == "tree" and entry.get("path") == dir_path:
                    return entry.get("sha")
        return None


def _extract_description(content: str) -> str:
    """Extract description from SKILL.md frontmatter or first paragraph."""
    if not content.startswith("---"):
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:200]
        return ""
    end_idx = content.find("---", 3)
    if end_idx == -1:
        return ""
    frontmatter = content[3:end_idx]
    for line in frontmatter.split("\n"):
        line = line.strip()
        if line.startswith("description:"):
            desc = line[len("description:") :].strip()
            if (desc.startswith('"') and desc.endswith('"')) or (desc.startswith("'") and desc.endswith("'")):
                desc = desc[1:-1]
            return desc
    return ""
