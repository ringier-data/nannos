/**
 * Publishes the console's own Nannos agent definition under `/.well-known/agent-skills/`.
 *
 * WHY: the console hosts an embedded assistant ("Nannos Assistant") exactly the way the
 * Alloy cockpit hosts its "Alloy AI Assistant" (ADR-0006, docs/adr/0006-host-published-
 * agent-definition.md). The console is the authority on how its own domain is operated,
 * so it publishes the definition itself and console-backend DISCOVERS it, digest-pinned,
 * from the deployed site into the bound sub-agent. Ported from the cockpit's
 * `app/vite-plugins/agentSkills.ts`; the one difference is that the console publishes
 * a `tools` list (see below) where the cockpit leaves tools to the Nannos side.
 *
 * WHAT is served (source: `well-known/agent-skills/`, next to `public/`):
 *
 *   /.well-known/agent-skills/index.json          generated here, see below
 *   /.well-known/agent-skills/AGENT.md            system prompt + agent frontmatter
 *   /.well-known/agent-skills/<name>/SKILL.md     one Agent Skill per directory
 *
 * `index.json` follows the Agent Skills Discovery RFC 0.2.0
 * (https://github.com/cloudflare/agent-skills-discovery-rfc): `$schema` plus a
 * `skills[]` list of `{ name, type: "skill-md", description, url, digest }`. The RFC
 * covers skills only; the agent itself (name, description, prompt, and optionally the
 * organization, MCP tool scope, model tier and thinking level) rides along in an
 * `x-nannos-agent` extension block, which RFC clients must ignore. Both
 * `name` + `description` in the index are lifted from each file's YAML frontmatter,
 * so the reviewed markdown is the single source and the index can never disagree
 * with it. Every `digest` is `sha256:<hex>` over the exact bytes served, computed at
 * build time -- Nannos pins what it imported and detects changes without diffing.
 *
 * SKILL.md files follow the Agent Skills spec (https://agentskills.io/specification):
 * frontmatter `name` (must equal the directory name) and `description` (what the
 * skill does AND when to use it -- that text is the trigger), then the instructions.
 *
 * `tools` (optional, AGENT.md frontmatter `- item` list → `x-nannos-agent.tools`) is the
 * MCP tool SCOPE of the bound sub-agent: it narrows what the agent is mounted with, it
 * never grants anything (the gateway still enforces the user's own permissions and the
 * HITL floor stays). Validated exactly as console-backend's fetcher does
 * (`services/well_known_agent.py`): non-empty, snake_case names, no duplicates.
 *
 * HOW: in `vite build` the files are emitted as plain assets under
 * `dist/.well-known/agent-skills/`, which the image's nginx serves as static files
 * (see `nginx.conf`, no SPA fallback for that path). In `vite` dev they are
 * built per request from the source directory, so an edit shows up on reload.
 * Invalid content (bad name, missing description, unknown key, ...) fails the
 * build -- a broken published index is worse than a failed build.
 *
 * The YAML frontmatter parser below deliberately handles a small subset (scalars,
 * folded `>`/`>-` scalars, `- item` lists, one-level `key: value` maps, `#` comments)
 * and rejects everything else, rather than pulling a YAML dependency into the build
 * for four files.
 */
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';

export const WELL_KNOWN_BASE = '/.well-known/agent-skills';
export const INDEX_SCHEMA = 'https://schemas.agentskills.io/discovery/0.2.0/schema.json';
export const AGENT_EXTENSION_KEY = 'x-nannos-agent';

const INDEX_FILE = 'index.json';
const AGENT_FILE = 'AGENT.md';
const SKILL_FILE = 'SKILL.md';

/** agentskills.io: 1-64 chars, lowercase a-z/0-9 and single hyphens, none leading/trailing. */
const SKILL_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const MAX_NAME_LENGTH = 64;
const MAX_DESCRIPTION_LENGTH = 1024;
/** console-backend fetch caps (`well_known_agent.py`): a larger file or more skills fails the fetch. */
const MAX_FILE_BYTES = 256 * 1024;
const MAX_SKILLS = 20;

/** Nannos `ModelTier` / `ThinkingLevel` values (console-backend `models/sub_agent.py`); `off` disables thinking. */
const MODEL_TIERS = ['low', 'standard', 'premium'];
const THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh'];
const MAX_ORGANIZATION_LENGTH = 200;

/** console-backend `_TOOL_NAME`: a snake_case MCP tool name. */
const TOOL_NAME = /^[a-z][a-z0-9_]*$/;

/** `organization`, `tools`, `model-tier` and `thinking-level` are optional: left out, the
 *  Nannos admin's setting on the bound sub-agent applies (ADR-0006). */
const AGENT_KEYS = ['name', 'description', 'organization', 'tools', 'model-tier', 'thinking-level'];
const SKILL_KEYS = ['name', 'description', 'license', 'compatibility', 'allowed-tools', 'metadata'];

/** agentskills.io reserves `metadata` for a string-to-string map of extension keys. Nannos reads
 *  two of them (ADR-0006): `nannos-visibility` -- `private` (the default) keeps the synced skill
 *  inside the bound sub-agent, `public` publishes it in the Nannos skill registry -- and
 *  `nannos-inline` (ADR-0012) -- `"true"` puts the skill's body in the bound agent's system prompt
 *  on every turn. Any other value fails the Nannos fetch, so it fails the build here first. Other
 *  keys are somebody else's and pass through. */
const SKILL_VISIBILITY_METADATA_KEY = 'nannos-visibility';
const SKILL_VISIBILITIES = ['private', 'public'];
const SKILL_INLINE_METADATA_KEY = 'nannos-inline';
const SKILL_INLINE_VALUES = ['true', 'false'];

export interface PublishedSkill {
  name: string;
  type: 'skill-md';
  description: string;
  url: string;
  digest: string;
}

export interface PublishedAgent {
  name: string;
  description: string;
  /** Who operates the host; Nannos names it in its framing text. */
  organization?: string;
  /** The system prompt: `AGENT.md` as served, frontmatter included, digest over its bytes. */
  prompt: { url: string; digest: string };
  /** MCP tool scope of the bound sub-agent (narrows, never grants); unset = the Nannos admin's setting. */
  tools?: string[];
  /** Nannos model tier to run on (`low` | `standard` | `premium`); unset = the Nannos admin's setting. */
  model_tier?: string;
  /** Extended-thinking level (`off` | `minimal` | `low` | `medium` | `high` | `xhigh`); unset = the Nannos admin's setting. */
  thinking_level?: string;
}

export interface AgentSkillsIndex {
  $schema: string;
  skills: PublishedSkill[];
  [AGENT_EXTENSION_KEY]: PublishedAgent;
}

export interface WellKnownFile {
  /** Path below `WELL_KNOWN_BASE`, e.g. `index.json` or `book-line-items/SKILL.md`. */
  name: string;
  contentType: string;
  body: Buffer;
}

export interface WellKnownBundle {
  index: AgentSkillsIndex;
  files: WellKnownFile[];
}

type Frontmatter = Record<string, string | string[] | Record<string, string>>;

class WellKnownError extends Error {
  constructor(file: string, message: string) {
    super(`${file}: ${message}`);
    this.name = 'WellKnownError';
  }
}

export function digestOf(bytes: Buffer): string {
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
}

/** A one-line scalar: quotes are stripped; an unquoted value must also read the same to a real
 *  YAML parser. console-backend parses the frontmatter with `yaml.safe_load`, where a plain
 *  scalar containing `: ` is a mapping error and ` #` starts a comment -- so either would pass
 *  this subset and still break (or silently truncate) the published file on the Nannos side. */
function scalar(value: string, file: string, where: string): string {
  const trimmed = value.trim();
  const quote = trimmed[0];
  if ((quote === '"' || quote === "'") && trimmed.length >= 2 && trimmed.endsWith(quote)) {
    return trimmed.slice(1, -1);
  }
  if (/:\s|:$|\s#/.test(trimmed)) {
    throw new WellKnownError(
      file,
      `frontmatter ${where}: an unquoted value must not contain ": " or " #" (YAML reads it differently); ` +
        'reword it, or use a folded `>-` scalar'
    );
  }
  return trimmed;
}

/** Parses the supported YAML subset. Anything else is an error naming the offending line. */
export function parseFrontmatter(yaml: string, file: string): Frontmatter {
  const data: Frontmatter = {};
  const lines = yaml.split(/\r?\n/);
  let i = 0;

  const isIndented = (line: string) => /^\s+\S/.test(line);
  const isBlank = (line: string) => line.trim() === '' || line.trim().startsWith('#');

  while (i < lines.length) {
    const line = lines[i] ?? '';
    if (isBlank(line)) {
      i++;
      continue;
    }
    const entry = /^([A-Za-z][A-Za-z0-9_-]*):(.*)$/.exec(line);
    if (!entry || isIndented(line)) {
      throw new WellKnownError(
        file,
        `frontmatter line ${i + 1} is not a \`key: value\`, list or folded scalar: "${line}"`
      );
    }
    const key = entry[1] as string;
    const rest = (entry[2] ?? '').trim();
    if (key in data) throw new WellKnownError(file, `frontmatter key "${key}" is repeated`);
    i++;

    if (rest === '>' || rest === '>-') {
      // Folded block scalar: indented continuation lines joined by single spaces.
      const parts: string[] = [];
      while (i < lines.length && (isIndented(lines[i] ?? '') || (lines[i] ?? '').trim() === '')) {
        const part = (lines[i] ?? '').trim();
        if (part) parts.push(part);
        i++;
      }
      data[key] = parts.join(' ');
    } else if (rest === '') {
      // Block: the indented lines are either all `  - item` (a list) or all
      // `  sub-key: value` (a one-level map); the first one decides which.
      const block: string[] = [];
      while (i < lines.length && (isIndented(lines[i] ?? '') || isBlank(lines[i] ?? ''))) {
        const raw = lines[i] ?? '';
        i++;
        if (!isBlank(raw)) block.push(raw);
      }
      if (block.length === 0) throw new WellKnownError(file, `frontmatter key "${key}" has no value`);

      if (/^\s+-\s/.test(block[0] as string)) {
        data[key] = block.map((raw) => {
          const item = /^\s+-\s+(.+)$/.exec(raw);
          if (!item) throw new WellKnownError(file, `frontmatter key "${key}": expected a "- item" line, got "${raw}"`);
          return scalar(item[1] as string, file, `"${key}" item`);
        });
      } else {
        const map: Record<string, string> = {};
        for (const raw of block) {
          const pair = /^\s+([A-Za-z][A-Za-z0-9_-]*):\s+(.+)$/.exec(raw);
          if (!pair) {
            throw new WellKnownError(file, `frontmatter key "${key}": expected a "sub-key: value" line, got "${raw}"`);
          }
          const subKey = pair[1] as string;
          if (subKey in map) throw new WellKnownError(file, `frontmatter key "${key}.${subKey}" is repeated`);
          map[subKey] = scalar(pair[2] as string, file, `"${key}.${subKey}"`);
        }
        data[key] = map;
      }
    } else {
      data[key] = scalar(rest, file, `"${key}"`);
    }
  }
  return data;
}

function splitDocument(source: string, file: string): { frontmatter: Frontmatter; body: string } {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/.exec(source);
  if (!match)
    throw new WellKnownError(file, 'must start with a `---` YAML frontmatter block followed by the markdown body');
  const body = (match[2] ?? '').trim();
  if (!body) throw new WellKnownError(file, 'has no markdown body after the frontmatter');
  // The orchestrator substitutes whitelisted `{{TOKEN}}` markers on the composed prompt, so
  // console-backend refuses host prose containing `{{` -- fail here first.
  if (body.includes('{{')) throw new WellKnownError(file, 'markdown body must not contain "{{"');
  return { frontmatter: parseFrontmatter(match[1] ?? '', file), body };
}

function requireString(frontmatter: Frontmatter, key: string, file: string, maxLength: number): string {
  const value = frontmatter[key];
  if (typeof value !== 'string' || value.length === 0)
    throw new WellKnownError(file, `frontmatter "${key}" is required`);
  if (value.length > maxLength) {
    throw new WellKnownError(file, `frontmatter "${key}" is ${value.length} characters, the maximum is ${maxLength}`);
  }
  return value;
}

function optionalString(frontmatter: Frontmatter, key: string, file: string, maxLength: number): string | undefined {
  const value = frontmatter[key];
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || value.length === 0)
    throw new WellKnownError(file, `frontmatter "${key}" must be a non-empty string`);
  if (value.length > maxLength) {
    throw new WellKnownError(file, `frontmatter "${key}" is ${value.length} characters, the maximum is ${maxLength}`);
  }
  return value;
}

function optionalEnum(frontmatter: Frontmatter, key: string, allowed: string[], file: string): string | undefined {
  const value = frontmatter[key];
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !allowed.includes(value)) {
    throw new WellKnownError(file, `frontmatter "${key}" must be one of ${allowed.join(', ')}`);
  }
  return value;
}

/** `metadata`, when present, must be a map; its `nannos-visibility` must be a value Nannos accepts. */
function optionalSkillMetadata(frontmatter: Frontmatter, file: string): Record<string, string> | undefined {
  const value = frontmatter.metadata;
  if (value === undefined) return undefined;
  if (typeof value === 'string' || Array.isArray(value)) {
    throw new WellKnownError(file, 'frontmatter "metadata" must be a map of `sub-key: value` lines');
  }
  const visibility = value[SKILL_VISIBILITY_METADATA_KEY];
  if (visibility !== undefined && !SKILL_VISIBILITIES.includes(visibility)) {
    throw new WellKnownError(
      file,
      `frontmatter "metadata.${SKILL_VISIBILITY_METADATA_KEY}" must be one of ${SKILL_VISIBILITIES.join(', ')}`
    );
  }
  const inline = value[SKILL_INLINE_METADATA_KEY];
  if (inline !== undefined && !SKILL_INLINE_VALUES.includes(inline)) {
    throw new WellKnownError(
      file,
      `frontmatter "metadata.${SKILL_INLINE_METADATA_KEY}" must be one of ${SKILL_INLINE_VALUES.join(', ')}`
    );
  }
  return value;
}

/** `tools`, when present, must be a non-empty `- item` list of distinct snake_case MCP tool names
 *  (console-backend rejects anything else for the whole fetch, keeping the last good revision). */
function optionalToolList(frontmatter: Frontmatter, file: string): string[] | undefined {
  const value = frontmatter.tools;
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length === 0) {
    throw new WellKnownError(file, 'frontmatter "tools", when present, must be a non-empty list of `- tool_name` lines');
  }
  const seen = new Set<string>();
  for (const tool of value) {
    if (!TOOL_NAME.test(tool)) {
      throw new WellKnownError(file, `frontmatter "tools": "${tool}" is not a snake_case MCP tool name`);
    }
    if (seen.has(tool)) throw new WellKnownError(file, `frontmatter "tools": "${tool}" is listed twice`);
    seen.add(tool);
  }
  return value;
}

function rejectUnknownKeys(frontmatter: Frontmatter, allowed: string[], file: string) {
  const unknown = Object.keys(frontmatter).filter((key) => !allowed.includes(key));
  if (unknown.length) {
    throw new WellKnownError(file, `unknown frontmatter key(s) ${unknown.join(', ')}; allowed: ${allowed.join(', ')}`);
  }
}

function readCapped(absolute: string, file: string): Buffer {
  const bytes = fs.readFileSync(absolute);
  if (bytes.length > MAX_FILE_BYTES) {
    throw new WellKnownError(file, `is ${bytes.length} bytes, the maximum is ${MAX_FILE_BYTES}`);
  }
  return bytes;
}

function readSkill(sourceDir: string, dirName: string): { skill: PublishedSkill; file: WellKnownFile } {
  const relative = `${dirName}/${SKILL_FILE}`;
  const absolute = path.join(sourceDir, dirName, SKILL_FILE);
  if (!fs.existsSync(absolute))
    throw new WellKnownError(relative, `missing -- every directory under ${sourceDir} must be a skill`);
  const bytes = readCapped(absolute, relative);
  const { frontmatter } = splitDocument(bytes.toString('utf8'), relative);
  rejectUnknownKeys(frontmatter, SKILL_KEYS, relative);
  const name = requireString(frontmatter, 'name', relative, MAX_NAME_LENGTH);
  if (!SKILL_NAME.test(name)) {
    throw new WellKnownError(relative, `name "${name}" must be lowercase letters, digits and single hyphens`);
  }
  if (name !== dirName) throw new WellKnownError(relative, `name "${name}" must equal its directory name "${dirName}"`);
  const description = requireString(frontmatter, 'description', relative, MAX_DESCRIPTION_LENGTH);
  optionalSkillMetadata(frontmatter, relative);
  return {
    skill: { name, type: 'skill-md', description, url: `${WELL_KNOWN_BASE}/${relative}`, digest: digestOf(bytes) },
    file: { name: relative, contentType: 'text/markdown; charset=utf-8', body: bytes },
  };
}

function readAgent(sourceDir: string): { agent: PublishedAgent; file: WellKnownFile } {
  const absolute = path.join(sourceDir, AGENT_FILE);
  if (!fs.existsSync(absolute)) throw new WellKnownError(AGENT_FILE, `missing in ${sourceDir}`);
  const bytes = readCapped(absolute, AGENT_FILE);
  const { frontmatter } = splitDocument(bytes.toString('utf8'), AGENT_FILE);
  rejectUnknownKeys(frontmatter, AGENT_KEYS, AGENT_FILE);
  const name = requireString(frontmatter, 'name', AGENT_FILE, MAX_NAME_LENGTH);
  const description = requireString(frontmatter, 'description', AGENT_FILE, MAX_DESCRIPTION_LENGTH);
  const organization = optionalString(frontmatter, 'organization', AGENT_FILE, MAX_ORGANIZATION_LENGTH);
  const tools = optionalToolList(frontmatter, AGENT_FILE);
  const modelTier = optionalEnum(frontmatter, 'model-tier', MODEL_TIERS, AGENT_FILE);
  const thinkingLevel = optionalEnum(frontmatter, 'thinking-level', THINKING_LEVELS, AGENT_FILE);
  return {
    agent: {
      name,
      description,
      ...(organization !== undefined && { organization }),
      prompt: { url: `${WELL_KNOWN_BASE}/${AGENT_FILE}`, digest: digestOf(bytes) },
      ...(tools !== undefined && { tools }),
      ...(modelTier !== undefined && { model_tier: modelTier }),
      ...(thinkingLevel !== undefined && { thinking_level: thinkingLevel }),
    },
    file: { name: AGENT_FILE, contentType: 'text/markdown; charset=utf-8', body: bytes },
  };
}

/** Reads `sourceDir` and returns the index plus every file to serve. Throws on invalid content. */
export function buildWellKnown(sourceDir: string): WellKnownBundle {
  if (!fs.existsSync(sourceDir)) throw new WellKnownError(sourceDir, 'agent-skills source directory does not exist');
  const { agent, file: agentFile } = readAgent(sourceDir);
  const skillDirs = fs
    .readdirSync(sourceDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
  if (skillDirs.length === 0) throw new WellKnownError(sourceDir, 'has no skill directories');
  if (skillDirs.length > MAX_SKILLS) {
    throw new WellKnownError(sourceDir, `has ${skillDirs.length} skill directories, the maximum is ${MAX_SKILLS}`);
  }
  const skills = skillDirs.map((dirName) => readSkill(sourceDir, dirName));

  const index: AgentSkillsIndex = {
    $schema: INDEX_SCHEMA,
    skills: skills.map((entry) => entry.skill),
    [AGENT_EXTENSION_KEY]: agent,
  };
  const indexFile: WellKnownFile = {
    name: INDEX_FILE,
    contentType: 'application/json; charset=utf-8',
    body: Buffer.from(`${JSON.stringify(index, null, 2)}\n`, 'utf8'),
  };
  return { index, files: [indexFile, agentFile, ...skills.map((entry) => entry.file)] };
}

/** Route of a served file, e.g. `/.well-known/agent-skills/index.json`. */
export function routeOf(file: WellKnownFile): string {
  return `${WELL_KNOWN_BASE}/${file.name}`;
}

export function agentSkillsWellKnown(sourceDir: string): Plugin {
  let isBuild = false;

  return {
    name: 'nannos-console:agent-skills-well-known',

    configResolved(config) {
      isBuild = config.command === 'build';
    },

    // Validate before the (slow) bundle so a broken skill file fails in seconds.
    buildStart() {
      if (isBuild) buildWellKnown(sourceDir);
    },

    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url ?? '').split('?')[0] ?? '';
        if (!url.startsWith(`${WELL_KNOWN_BASE}/`)) return next();

        let bundle: WellKnownBundle;
        try {
          bundle = buildWellKnown(sourceDir);
        } catch (error) {
          res.statusCode = 500;
          res.setHeader('Content-Type', 'text/plain; charset=utf-8');
          res.end(error instanceof Error ? error.message : String(error));
          return;
        }
        const file = bundle.files.find((candidate) => routeOf(candidate) === url);
        if (!file) {
          res.statusCode = 404;
          res.end();
          return;
        }
        res.setHeader('Content-Type', file.contentType);
        res.setHeader('Cache-Control', 'no-cache');
        res.end(file.body);
      });
    },

    generateBundle() {
      for (const file of buildWellKnown(sourceDir).files) {
        this.emitFile({ type: 'asset', fileName: `${WELL_KNOWN_BASE.slice(1)}/${file.name}`, source: file.body });
      }
    },
  };
}
