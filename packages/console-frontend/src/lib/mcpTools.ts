/**
 * Helpers for presenting MCP tools and their argument schemas.
 *
 * Kept out of the components that use them so both can be imported without
 * breaking fast refresh (a module exporting components must export nothing else).
 */
import type { McpTool } from '@/api/generated/types.gen';

/**
 * Server a tool belongs to. The gateway sets `server` inconsistently, so fall back
 * to the tool name's own prefix (`gdrive_copy_file` → `gdrive`), which is how the
 * aggregating gateway names them anyway.
 */
export function toolServer(tool: McpTool): string {
  if (tool.server) return tool.server;
  const underscore = tool.name.indexOf('_');
  return underscore > 0 ? tool.name.slice(0, underscore) : 'other';
}

/**
 * Tool name without its server prefix, for display only.
 *
 * In a list of several hundred tools the repeated prefix is a large share of the
 * horizontal space and carries no distinguishing signal — the server is shown once,
 * as a badge.
 */
export function toolShortName(tool: McpTool): string {
  const server = tool.server;
  if (server && tool.name.startsWith(`${server}_`)) return tool.name.slice(server.length + 1);
  const underscore = tool.name.indexOf('_');
  return underscore > 0 ? tool.name.slice(underscore + 1) : tool.name;
}

/** A parameter that can be rendered as a single form control. */
export interface FlatParam {
  key: string;
  /** JSON Schema `type`, narrowed to what we render. `multi` is an array of enum values. */
  type: 'string' | 'number' | 'integer' | 'boolean' | 'multi';
  required: boolean;
  description?: string;
  enumValues?: string[];
  /** Set when the schema declares a default; shown as the placeholder. */
  placeholder?: string;
}

export interface ParsedToolSchema {
  params: FlatParam[];
  /** Parameter names the flat form cannot represent (objects, arrays, unions). */
  complex: string[];
}

const RENDERABLE = new Set(['string', 'number', 'integer', 'boolean']);

/**
 * Flatten a tool's input schema into renderable scalar fields.
 *
 * Anything nested is reported in `complex` rather than dropped silently, so the
 * caller can offer the raw JSON editor and no argument becomes unreachable.
 */
export function parseToolSchema(tool: McpTool | undefined): ParsedToolSchema {
  const schema = tool?.input_schema as
    | { properties?: Record<string, unknown>; required?: unknown }
    | undefined;
  const properties = schema?.properties;
  if (!properties || typeof properties !== 'object') return { params: [], complex: [] };

  const required = new Set(
    Array.isArray(schema?.required)
      ? schema.required.filter((r): r is string => typeof r === 'string')
      : [],
  );

  const params: FlatParam[] = [];
  const complex: string[] = [];

  for (const [key, rawProp] of Object.entries(properties)) {
    const prop = (rawProp ?? {}) as Record<string, unknown>;

    // A union type (`["string", "null"]`) is renderable as long as exactly one
    // non-null member is; optional parameters are commonly spelled that way.
    const declared = Array.isArray(prop.type)
      ? prop.type.filter((t): t is string => typeof t === 'string' && t !== 'null')
      : typeof prop.type === 'string'
        ? [prop.type]
        : [];

    const enumValues = Array.isArray(prop.enum)
      ? prop.enum
          .filter((v): v is string | number => typeof v === 'string' || typeof v === 'number')
          .map(String)
      : undefined;

    if (enumValues?.length) {
      params.push({
        key,
        type: 'string',
        required: required.has(key),
        description: typeof prop.description === 'string' ? prop.description : undefined,
        enumValues,
      });
      continue;
    }

    // An array whose items are an enum is not "nested" in any way a JSON editor
    // helps with — it is a multi-select (metrics, dimensions), and calling it
    // complex sent people to raw JSON for a pick-from-a-list argument.
    if (declared.length === 1 && declared[0] === 'array') {
      const items = (prop.items ?? {}) as Record<string, unknown>;
      const itemEnum = Array.isArray(items.enum)
        ? items.enum
            .filter((v): v is string | number => typeof v === 'string' || typeof v === 'number')
            .map(String)
        : undefined;
      if (itemEnum?.length) {
        params.push({
          key,
          type: 'multi',
          required: required.has(key),
          description: typeof prop.description === 'string' ? prop.description : undefined,
          enumValues: itemEnum,
        });
        continue;
      }
    }

    if (declared.length !== 1 || !RENDERABLE.has(declared[0])) {
      complex.push(key);
      continue;
    }

    params.push({
      key,
      type: declared[0] as FlatParam['type'],
      required: required.has(key),
      description: typeof prop.description === 'string' ? prop.description : undefined,
      placeholder:
        prop.default !== undefined && prop.default !== null ? String(prop.default) : undefined,
    });
  }

  // Required parameters first — they are what stops a call from working.
  params.sort((a, b) => Number(b.required) - Number(a.required));
  return { params, complex };
}
