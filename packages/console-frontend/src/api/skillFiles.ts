/**
 * Skill file API helpers.
 *
 * These wrap the new file-level endpoints that aren't in the generated SDK yet.
 * After running `npm run gen-sdk`, these can be replaced with generated hooks.
 */

import { client } from './generated/client.gen';

export interface SkillFileSummary {
  path: string;
}

export interface SkillFileContent {
  path: string;
  content: string;
}

export interface SkillFileListResponse {
  items: SkillFileSummary[];
}

/** List files in a skill folder (excluding SKILL.md). */
export async function listSkillFiles(
  agentName: string,
  skillName: string,
  scope: string,
  groupId?: string,
): Promise<SkillFileSummary[]> {
  const params = new URLSearchParams({ scope });
  if (groupId) params.set('group_id', groupId);

  const { data, error } = await client.get({
    url: `/api/v1/playbooks/agents/${encodeURIComponent(agentName)}/skills/${encodeURIComponent(skillName)}/files?${params}`,
  });
  if (error) throw new Error('Failed to list skill files');
  return (data as SkillFileListResponse)?.items ?? [];
}

/** Read a single file from a skill folder. */
export async function getSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  scope: string,
  groupId?: string,
): Promise<string> {
  const params = new URLSearchParams({ scope });
  if (groupId) params.set('group_id', groupId);

  const { data, error } = await client.get({
    url: `/api/v1/playbooks/agents/${encodeURIComponent(agentName)}/skills/${encodeURIComponent(skillName)}/files/${filePath}?${params}`,
  });
  if (error) throw new Error(`Failed to read skill file: ${filePath}`);
  return (data as SkillFileContent)?.content ?? '';
}

/** Write a file to a skill folder. */
export async function writeSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  content: string,
  scope: string,
  groupId?: string,
): Promise<void> {
  const params = new URLSearchParams({ scope });
  if (groupId) params.set('group_id', groupId);

  const { error } = await client.put({
    url: `/api/v1/playbooks/agents/${encodeURIComponent(agentName)}/skills/${encodeURIComponent(skillName)}/files/${filePath}?${params}`,
    body: { content },
  });
  if (error) throw new Error(`Failed to write skill file: ${filePath}`);
}

/** Delete a file from a skill folder. */
export async function deleteSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  scope: string,
  groupId?: string,
): Promise<void> {
  const params = new URLSearchParams({ scope });
  if (groupId) params.set('group_id', groupId);

  const { error } = await client.delete({
    url: `/api/v1/playbooks/agents/${encodeURIComponent(agentName)}/skills/${encodeURIComponent(skillName)}/files/${filePath}?${params}`,
  });
  if (error) throw new Error(`Failed to delete skill file: ${filePath}`);
}
