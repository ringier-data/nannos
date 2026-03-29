import { ParameterizedContext } from 'koa';
import type { IBotInstallationStore } from '../storage/types.js';

const ALLOWED_AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'];
const MAX_AVATAR_SIZE = 2 * 1024 * 1024; // 2 MB

export class BotInstallationsController {
  constructor(private readonly botInstallationStore: IBotInstallationStore) {}

  async getAll(ctx: ParameterizedContext) {
    const installations = await this.botInstallationStore.listAll();
    ctx.body = { installations };
  }

  async getByAppId(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const installation = await this.botInstallationStore.getByAppId(appId);
    if (!installation) {
      ctx.status = 404;
      ctx.body = { error: `Installation not found for appId: ${appId}` };
      return;
    }
    ctx.body = { installation };
  }

  async create(ctx: ParameterizedContext) {
    const body = ctx.request.body as {
      appId: string;
      teamId: string;
      botToken: string;
      signingSecret: string;
      botName: string;
      avatarUrl?: string;
      slashCommand: string;
    };
    await this.botInstallationStore.upsert({
      ...body,
      isActive: true,
    });
    const installation = await this.botInstallationStore.getByAppId(body.appId);
    ctx.status = 201;
    ctx.body = { installation };
  }

  async update(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const existing = await this.botInstallationStore.getByAppId(appId);
    if (!existing) {
      ctx.status = 404;
      ctx.body = { error: `Installation not found for appId: ${appId}` };
      return;
    }
    const body = ctx.request.body as {
      teamId?: string;
      botToken?: string;
      signingSecret?: string;
      botName?: string;
      avatarUrl?: string;
      slashCommand?: string;
    };
    await this.botInstallationStore.upsert({
      appId,
      teamId: body.teamId ?? existing.teamId,
      botToken: body.botToken ?? existing.botToken,
      signingSecret: body.signingSecret ?? existing.signingSecret,
      botName: body.botName ?? existing.botName,
      avatarUrl: body.avatarUrl ?? existing.avatarUrl,
      slashCommand: body.slashCommand ?? existing.slashCommand,
      isActive: existing.isActive,
    });
    const installation = await this.botInstallationStore.getByAppId(appId);
    ctx.body = { installation };
  }

  async delete(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const existing = await this.botInstallationStore.getByAppId(appId);
    if (!existing) {
      ctx.status = 404;
      ctx.body = { error: `Installation not found for appId: ${appId}` };
      return;
    }
    await this.botInstallationStore.deactivate(appId);
    ctx.status = 204;
  }

  async uploadAvatar(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const existing = await this.botInstallationStore.getByAppId(appId);
    if (!existing) {
      ctx.status = 404;
      ctx.body = { error: `Installation not found for appId: ${appId}` };
      return;
    }

    const file = ctx.request.files?.avatar;
    if (!file || Array.isArray(file)) {
      ctx.status = 400;
      ctx.body = { error: 'A single "avatar" file is required' };
      return;
    }

    if (!ALLOWED_AVATAR_TYPES.includes(file.mimetype ?? '')) {
      ctx.status = 400;
      ctx.body = { error: `Unsupported file type: ${file.mimetype}. Allowed: ${ALLOWED_AVATAR_TYPES.join(', ')}` };
      return;
    }

    if ((file.size ?? 0) > MAX_AVATAR_SIZE) {
      ctx.status = 400;
      ctx.body = { error: `File too large. Maximum size is ${MAX_AVATAR_SIZE / 1024 / 1024} MB` };
      return;
    }

    const { readFile } = await import('fs/promises');
    const data = await readFile(file.filepath);
    await this.botInstallationStore.updateAvatar(appId, data, file.mimetype ?? 'application/octet-stream');

    ctx.body = { ok: true };
  }

  async getAvatar(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const avatar = await this.botInstallationStore.getAvatar(appId);
    if (!avatar) {
      ctx.status = 404;
      ctx.body = { error: 'No avatar found' };
      return;
    }
    ctx.type = avatar.mimeType;
    ctx.set('Cache-Control', 'public, max-age=3600');
    ctx.body = avatar.data;
  }

  async deleteAvatar(ctx: ParameterizedContext) {
    const { appId } = ctx.params;
    const existing = await this.botInstallationStore.getByAppId(appId);
    if (!existing) {
      ctx.status = 404;
      ctx.body = { error: `Installation not found for appId: ${appId}` };
      return;
    }
    await this.botInstallationStore.deleteAvatar(appId);
    ctx.status = 204;
  }
}
