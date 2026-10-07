import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  AGENT_EXTENSION_KEY,
  INDEX_SCHEMA,
  WELL_KNOWN_BASE,
  agentSkillsWellKnown,
  buildWellKnown,
  parseFrontmatter,
  routeOf,
} from './agentSkills';

/** The real published tree -- these tests guard what the console ships to Nannos. */
const SOURCE_DIR = path.resolve(__dirname, '../well-known/agent-skills');

const sha256 = (bytes: Buffer) => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;

describe('the published /.well-known/agent-skills tree', () => {
  const bundle = buildWellKnown(SOURCE_DIR);
  const served = new Map(bundle.files.map((file) => [routeOf(file), file]));
  const agent = bundle.index[AGENT_EXTENSION_KEY];

  it('is an Agent Skills Discovery 0.2.0 index with one entry per skill directory', () => {
    expect(bundle.index.$schema).toBe(INDEX_SCHEMA);
    expect(bundle.index.skills.map((skill) => skill.name)).toEqual([
      'create-a-sub-agent',
      'manage-shared-jobs',
      'schedule-a-task',
      'set-up-a-watch',
      'write-a-skill',
    ]);
    for (const skill of bundle.index.skills) {
      expect(skill.type).toBe('skill-md');
      expect(skill.description.length).toBeGreaterThan(0);
      expect(skill.description.length).toBeLessThanOrEqual(1024);
      expect(skill.url).toBe(`${WELL_KNOWN_BASE}/${skill.name}/SKILL.md`);
    }
  });

  it('carries the agent in the x-nannos-agent extension', () => {
    expect(agent.name).toBe('Nannos Assistant');
    expect(agent.description.length).toBeLessThanOrEqual(1024);
    expect(agent.prompt.url).toBe(`${WELL_KNOWN_BASE}/AGENT.md`);
    // Model tier and thinking level stay the Nannos admin's call.
    expect(agent).not.toHaveProperty('model_tier');
    expect(agent).not.toHaveProperty('thinking_level');
  });

  it('scopes the agent to console MCP tools only, never the triage-only or web-search ones', () => {
    expect(agent.tools).toBeDefined();
    expect(agent.tools!.length).toBeGreaterThan(0);
    for (const tool of agent.tools!) expect(tool).toMatch(/^(console|scheduler)_[a-z0-9_]+$/);
    expect(agent.tools).toEqual(expect.arrayContaining(['scheduler_create_job', 'console_grep_mcp_tools']));
    expect(agent.tools).not.toContain('console_set_bug_report_external_link');
    expect(agent.tools).not.toContain('console_update_bug_report_status');
    expect(agent.tools).not.toContain('console_web_search');
  });

  it('pins every digest to the exact bytes it serves', () => {
    const pinned = [...bundle.index.skills.map(({ url, digest }) => ({ url, digest })), agent.prompt];
    for (const { url, digest } of pinned) {
      const file = served.get(url);
      expect(file).toBeDefined();
      expect(digest).toMatch(/^sha256:[0-9a-f]{64}$/);
      expect(digest).toBe(sha256(file!.body));
      expect(file!.contentType).toBe('text/markdown; charset=utf-8');
    }
  });

  it('serves index.json as JSON that round-trips to the index', () => {
    const indexFile = served.get(`${WELL_KNOWN_BASE}/index.json`)!;
    expect(indexFile.contentType).toBe('application/json; charset=utf-8');
    expect(JSON.parse(indexFile.body.toString('utf8'))).toEqual(bundle.index);
  });
});

describe('buildWellKnown validation', () => {
  let dir: string;
  const write = (relative: string, content: string) => {
    const absolute = path.join(dir, relative);
    fs.mkdirSync(path.dirname(absolute), { recursive: true });
    fs.writeFileSync(absolute, content);
  };
  const agentFile = (frontmatter = 'name: Test agent\ndescription: Does things.') =>
    `---\n${frontmatter}\n---\n\nYou are a test agent.\n`;
  const skillFile = (name: string, extra = '') =>
    `---\nname: ${name}\ndescription: Does one thing. Use when asked to do the thing.\n${extra}---\n\n# ${name}\n\nSteps.\n`;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-skills-'));
    write('AGENT.md', agentFile());
  });
  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('accepts a minimal valid tree', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    const { index } = buildWellKnown(dir);
    expect(index.skills).toHaveLength(1);
    expect(index[AGENT_EXTENSION_KEY].name).toBe('Test agent');
  });

  it('rejects a skill whose name differs from its directory', () => {
    write('do-thing/SKILL.md', skillFile('other-thing'));
    expect(() => buildWellKnown(dir)).toThrow(/must equal its directory name "do-thing"/);
  });

  it('rejects skill names outside the agentskills.io grammar', () => {
    write('Do-Thing/SKILL.md', skillFile('Do-Thing'));
    expect(() => buildWellKnown(dir)).toThrow(/lowercase letters, digits and single hyphens/);
    fs.rmSync(path.join(dir, 'Do-Thing'), { recursive: true });
    write('do--thing/SKILL.md', skillFile('do--thing'));
    expect(() => buildWellKnown(dir)).toThrow(/lowercase letters, digits and single hyphens/);
  });

  it('rejects an over-long description', () => {
    write('do-thing/SKILL.md', `---\nname: do-thing\ndescription: ${'x'.repeat(1025)}\n---\n\nBody.\n`);
    expect(() => buildWellKnown(dir)).toThrow(/1025 characters, the maximum is 1024/);
  });

  it('rejects unknown frontmatter keys', () => {
    write('do-thing/SKILL.md', skillFile('do-thing', 'model: opus\n'));
    expect(() => buildWellKnown(dir)).toThrow(/unknown frontmatter key\(s\) model/);
  });

  it('rejects a file without frontmatter or without a body', () => {
    write('do-thing/SKILL.md', '# Just markdown\n');
    expect(() => buildWellKnown(dir)).toThrow(/must start with a `---` YAML frontmatter block/);
    write('do-thing/SKILL.md', '---\nname: do-thing\ndescription: d\n---\n\n');
    expect(() => buildWellKnown(dir)).toThrow(/no markdown body/);
  });

  it('rejects a directory without SKILL.md', () => {
    write('do-thing/notes.md', 'x');
    expect(() => buildWellKnown(dir)).toThrow(/do-thing\/SKILL.md: missing/);
  });

  it('accepts skill metadata with a Nannos visibility, and other extension keys', () => {
    write(
      'do-thing/SKILL.md',
      skillFile('do-thing', 'metadata:\n  nannos-visibility: public\n  someone-elses-key: kept\n')
    );
    expect(buildWellKnown(dir).index.skills[0]?.name).toBe('do-thing');
  });

  it('rejects a Nannos visibility Nannos does not know, and a metadata that is not a map', () => {
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata:\n  nannos-visibility: internal\n'));
    expect(() => buildWellKnown(dir)).toThrow(/"metadata.nannos-visibility" must be one of private, public/);
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata: public\n'));
    expect(() => buildWellKnown(dir)).toThrow(/"metadata" must be a map/);
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata:\n  - public\n'));
    expect(() => buildWellKnown(dir)).toThrow(/"metadata" must be a map/);
  });

  it('publishes the optional tool list in order when AGENT.md sets one', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools:\n  - list_things\n  - scheduler_create_job'));
    expect(buildWellKnown(dir).index[AGENT_EXTENSION_KEY].tools).toEqual(['list_things', 'scheduler_create_job']);
  });

  it('rejects an empty tool list, a non-list, a bad tool name and a duplicate (as console-backend does)', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools:'));
    expect(() => buildWellKnown(dir)).toThrow(/"tools" has no value/);
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools: list_things'));
    expect(() => buildWellKnown(dir)).toThrow(/"tools", when present, must be a non-empty list/);
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools:\n  - List-Things'));
    expect(() => buildWellKnown(dir)).toThrow(/"List-Things" is not a snake_case MCP tool name/);
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools:\n  - 1st_tool'));
    expect(() => buildWellKnown(dir)).toThrow(/"1st_tool" is not a snake_case MCP tool name/);
    write('AGENT.md', agentFile('name: A\ndescription: d\ntools:\n  - list_things\n  - list_things'));
    expect(() => buildWellKnown(dir)).toThrow(/"list_things" is listed twice/);
  });

  it('accepts nannos-inline "true"/"false" skill metadata and rejects anything else', () => {
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata:\n  nannos-inline: "true"\n'));
    expect(buildWellKnown(dir).index.skills[0]?.name).toBe('do-thing');
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata:\n  nannos-inline: false\n'));
    expect(buildWellKnown(dir).index.skills[0]?.name).toBe('do-thing');
    write('do-thing/SKILL.md', skillFile('do-thing', 'metadata:\n  nannos-inline: yes\n'));
    expect(() => buildWellKnown(dir)).toThrow(/"metadata.nannos-inline" must be one of true, false/);
  });

  it('rejects a body containing "{{", which console-backend refuses', () => {
    write('do-thing/SKILL.md', '---\nname: do-thing\ndescription: d\n---\n\nUse {{TOKEN}} here.\n');
    expect(() => buildWellKnown(dir)).toThrow(/must not contain "\{\{"/);
    write('do-thing/SKILL.md', skillFile('do-thing'));
    write('AGENT.md', '---\nname: A\ndescription: d\n---\n\nHello {{USER}}.\n');
    expect(() => buildWellKnown(dir)).toThrow(/AGENT.md: markdown body must not contain/);
  });

  it('rejects files and skill counts over the console-backend fetch caps', () => {
    write('do-thing/SKILL.md', `${skillFile('do-thing')}${'x'.repeat(256 * 1024)}`);
    expect(() => buildWellKnown(dir)).toThrow(/bytes, the maximum is 262144/);
    fs.rmSync(path.join(dir, 'do-thing'), { recursive: true });
    for (let i = 0; i < 21; i++) write(`skill-${i}/SKILL.md`, skillFile(`skill-${i}`));
    expect(() => buildWellKnown(dir)).toThrow(/21 skill directories, the maximum is 20/);
  });

  it('publishes the optional organization, model tier and thinking level when AGENT.md sets them', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    write(
      'AGENT.md',
      agentFile('name: A\ndescription: d\norganization: Ringier Advertising\nmodel-tier: premium\nthinking-level: off')
    );
    expect(buildWellKnown(dir).index[AGENT_EXTENSION_KEY]).toMatchObject({
      organization: 'Ringier Advertising',
      model_tier: 'premium',
      thinking_level: 'off',
    });
  });

  it('leaves the optional keys out of the index when AGENT.md omits them (Nannos-side settings apply)', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    const agent = buildWellKnown(dir).index[AGENT_EXTENSION_KEY];
    expect(agent).not.toHaveProperty('organization');
    expect(agent).not.toHaveProperty('model_tier');
    expect(agent).not.toHaveProperty('thinking_level');
  });

  it('rejects a model tier or thinking level Nannos does not know', () => {
    write('do-thing/SKILL.md', skillFile('do-thing'));
    write('AGENT.md', agentFile('name: A\ndescription: d\nmodel-tier: gigantic'));
    expect(() => buildWellKnown(dir)).toThrow(/"model-tier" must be one of low, standard, premium/);
    write('AGENT.md', agentFile('name: A\ndescription: d\nthinking-level: maximum'));
    expect(() => buildWellKnown(dir)).toThrow(/"thinking-level" must be one of off, minimal, low, medium, high, xhigh/);
  });
});

describe('parseFrontmatter', () => {
  it('reads plain, quoted and folded scalars, lists and comments', () => {
    const yaml = [
      '# leading comment',
      'name: a-b',
      'description: >-',
      '  first line',
      '  second line',
      'license: "MIT"',
      'tools:',
      '  - x',
      '  # a comment between items',
      "  - 'y'",
    ].join('\n');
    expect(parseFrontmatter(yaml, 'f')).toEqual({
      name: 'a-b',
      description: 'first line second line',
      license: 'MIT',
      tools: ['x', 'y'],
    });
  });

  it('reads a one-level map of scalars', () => {
    expect(parseFrontmatter('metadata:\n  nannos-visibility: public\n  author: "x"\n', 'f')).toEqual({
      metadata: { 'nannos-visibility': 'public', author: 'x' },
    });
  });

  it('rejects what it does not support, naming the line', () => {
    expect(() => parseFrontmatter('tools:\n  - x\n  author: x\n', 'f')).toThrow(/expected a "- item" line/);
    expect(() => parseFrontmatter('metadata:\n  author: x\n  - y\n', 'f')).toThrow(/expected a "sub-key: value" line/);
    expect(() => parseFrontmatter('metadata:\n  nested:\n    deeper: x\n', 'f')).toThrow(
      /expected a "sub-key: value" line/
    );
    expect(() => parseFrontmatter('metadata:\n  a: x\n  a: y\n', 'f')).toThrow(/"metadata.a" is repeated/);
    expect(() => parseFrontmatter('metadata:\n', 'f')).toThrow(/"metadata" has no value/);
    expect(() => parseFrontmatter('  indented: x\n', 'f')).toThrow(/line 1 is not a `key: value`/);
    expect(() => parseFrontmatter('name: a\nname: b\n', 'f')).toThrow(/"name" is repeated/);
  });

  it('rejects unquoted values a real YAML parser would read differently, and accepts them quoted or folded', () => {
    expect(() => parseFrontmatter('description: does a thing: badly\n', 'f')).toThrow(/must not contain ": " or " #"/);
    expect(() => parseFrontmatter('description: does a thing #really\n', 'f')).toThrow(/must not contain/);
    expect(() => parseFrontmatter('tools:\n  - a: b\n', 'f')).toThrow(/"tools" item/);
    expect(parseFrontmatter('description: "does a thing: well"\n', 'f')).toEqual({ description: 'does a thing: well' });
    expect(parseFrontmatter('description: >-\n  does a thing: well\n', 'f')).toEqual({
      description: 'does a thing: well',
    });
    expect(parseFrontmatter('description: see https://example.com/x\n', 'f')).toEqual({
      description: 'see https://example.com/x',
    });
  });
});

describe('dev-server middleware', () => {
  type FakeResponse = {
    statusCode: number;
    headers: Record<string, string>;
    body: unknown;
    setHeader(name: string, value: string): void;
    end(body?: unknown): void;
  };

  const plugin = agentSkillsWellKnown(SOURCE_DIR);
  let handler: (req: { url: string }, res: FakeResponse, next: () => void) => void;
  const configureServer = plugin.configureServer as (server: unknown) => void;
  configureServer({ middlewares: { use: (fn: typeof handler) => (handler = fn) } });

  const request = (url: string) => {
    const res: FakeResponse = {
      statusCode: 200,
      headers: {},
      body: undefined,
      setHeader(name, value) {
        this.headers[name] = value;
      },
      end(body) {
        this.body = body;
      },
    };
    let passedThrough = false;
    handler({ url }, res, () => (passedThrough = true));
    return { res, passedThrough };
  };

  it('serves every file under the namespace with its content type, ignoring query strings', () => {
    const index = request('/.well-known/agent-skills/index.json?t=1');
    expect(index.passedThrough).toBe(false);
    expect(index.res.statusCode).toBe(200);
    expect(index.res.headers['Content-Type']).toBe('application/json; charset=utf-8');
    expect(index.res.headers['Cache-Control']).toBe('no-cache');
    expect((JSON.parse(String(index.res.body)) as { $schema: string }).$schema).toBe(INDEX_SCHEMA);

    const prompt = request('/.well-known/agent-skills/AGENT.md');
    expect(prompt.res.headers['Content-Type']).toBe('text/markdown; charset=utf-8');
    expect(String(prompt.res.body)).toMatch(/^---\nname: Nannos Assistant/);
  });

  it('404s inside the namespace and passes everything else through', () => {
    expect(request('/.well-known/agent-skills/missing/SKILL.md').res.statusCode).toBe(404);
    expect(request('/app/scheduler').passedThrough).toBe(true);
    expect(request('/.well-known/other').passedThrough).toBe(true);
  });
});
