export interface Installation {
  appId: string
  teamId: string
  botToken: string
  signingSecret: string
  botName: string
  avatarUrl?: string
  slashCommand: string
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export interface InstallationFormData {
  appId: string
  teamId: string
  botToken: string
  signingSecret: string
  botName: string
  avatarUrl: string
  slashCommand: string
}
