import {
  listSkillFilesApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesGet,
  getSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathGet,
  writeSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathPut,
  deleteSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathDelete,
} from '@/api/generated/sdk.gen';
import type { SkillFileSummary } from '@/api/generated/types.gen';

export async function listSkillFiles(
  agentName: string,
  skillName: string,
  scope: string,
  groupId?: string,
): Promise<SkillFileSummary[]> {
  const resp = await listSkillFilesApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesGet({
    path: { agent_name: agentName, skill_name: skillName },
    query: { scope, group_id: groupId },
    throwOnError: true,
  });
  return resp.data.items ?? [];
}

export async function getSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  scope: string,
  groupId?: string,
): Promise<string> {
  const resp = await getSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathGet({
    path: { agent_name: agentName, skill_name: skillName, file_path: filePath },
    query: { scope, group_id: groupId },
    throwOnError: true,
  });
  return resp.data.content;
}

export async function writeSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  content: string,
  scope: string,
  groupId?: string,
): Promise<void> {
  await writeSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathPut({
    path: { agent_name: agentName, skill_name: skillName, file_path: filePath },
    query: { scope, group_id: groupId },
    body: { content },
    throwOnError: true,
  });
}

export async function deleteSkillFile(
  agentName: string,
  skillName: string,
  filePath: string,
  scope: string,
  groupId?: string,
): Promise<void> {
  await deleteSkillFileApiV1PlaybooksAgentsAgentNameSkillsSkillNameFilesFilePathDelete({
    path: { agent_name: agentName, skill_name: skillName, file_path: filePath },
    query: { scope, group_id: groupId },
    throwOnError: true,
  });
}
