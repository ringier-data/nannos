import { SSMClient, GetParameterCommand } from '@aws-sdk/client-ssm';
import type { StorageConfig } from '../storage/index.js';

export type EnvName = 'local' | 'dev' | 'stg' | 'prod';
export type StorageProviderType = 'postgres';

export interface Config {
  isLocal(): boolean;
  isDev(): boolean;
  isStg(): boolean;
  isProd(): boolean;
  readonly version: string;
  readonly environment: EnvName;
  readonly baseUrl: string;
  readonly slackAppPort: number;
  readonly v2AppPort: number;
  readonly httpKeepAliveTimeout: number;
  readonly logLevel: 'trace' | 'debug' | 'info' | 'warn' | 'error' | 'fatal';
  readonly slackAppConfig: {
    signingSecret: string;
    appToken?: string; // Required for socket mode
    botToken?: string; // Optional: only used for zero-downtime Nannos seed migration
    socketMode: boolean;
    // Seed migration fields — used to upsert the initial Nannos bot into bot_installations
    seedAppId?: string; // SLACK_APP_ID env var
    seedTeamId?: string; // SLACK_TEAM_ID env var
    seedBotName?: string; // SLACK_BOT_NAME env var (default: 'Nannos')
    seedSlashCommand?: string; // SLACK_SLASH_COMMAND env var (default: '/nannos')
  };
  readonly storage: StorageConfig;
  readonly aws: {
    region: string;
    s3: {
      fileUploadBucket: string;
    };
  };
  readonly oidc: {
    issuerUrl: string;
    clientId: string;
    clientSecret: string;
    orchestratorAudience: string;
  };
  readonly a2aServer: {
    url: string;
    timeout: number;
  };
  readonly consoleBackend?: {
    url: string;
    audience: string;
  };
  readonly adminGroup: string;
  readonly v2CookieSecret: string;
  readonly sessionTtlSeconds: number;
}

/**
 * Helper function to get a parameter from AWS SSM Parameter Store
 */
async function getSSMParameter(ssmKey: string): Promise<string> {
  const ssmClient = new SSMClient({});
  const command = new GetParameterCommand({
    Name: ssmKey,
    WithDecryption: true,
  });

  try {
    const response = await ssmClient.send(command);
    if (!response.Parameter?.Value) {
      throw new Error(`SSM parameter ${ssmKey} not found or has no value`);
    }
    return response.Parameter.Value;
  } catch (error) {
    throw new Error(`Failed to retrieve SSM parameter ${ssmKey}: ${error}`);
  }
}

/**
 * Get a value either from environment variable or SSM Parameter Store
 * If both envVar and ssmKey are provided, envVar takes precedence
 */
async function getSecretSSMValue(envVar: string, ssmKeyEnvVar?: string): Promise<string> {
  const envValue = process.env[envVar];
  if (envValue) {
    return envValue;
  }

  const ssmKey = ssmKeyEnvVar ? process.env[ssmKeyEnvVar] : undefined;
  if (ssmKey) {
    return getSSMParameter(ssmKey);
  }

  throw new Error(`Please provide ${envVar}${ssmKeyEnvVar ? ` or ${ssmKeyEnvVar}` : ''}`);
}

export async function getConfigFromEnv(): Promise<Config> {
  // Required environment variables
  if (!process.env.ENVIRONMENT) {
    throw new Error('Please provide ENVIRONMENT');
  }
  if (!process.env.V2_COOKIE_SECRET) {
    throw new Error('Please provide V2_COOKIE_SECRET');
  }
  const environment = (process.env.ENVIRONMENT as Config['environment']) || 'local';

  // Socket mode is only enabled for local development
  const socketMode = environment === 'local';

  // SLACK_APP_TOKEN is only required for socket mode (local development)
  if (socketMode && !process.env.SLACK_APP_TOKEN) {
    throw new Error('Please provide SLACK_APP_TOKEN (required for socket mode in local development)');
  }

  // SLACK_BOT_TOKEN is now optional: token routing is done via bot_installations table.
  // Still read here for the zero-downtime seed migration of the initial Nannos bot.
  const slackBotToken = process.env.SLACK_BOT_TOKEN;

  // Validate OIDC configuration
  if (!process.env.OIDC_ISSUER_URL) {
    throw new Error('Please provide OIDC_ISSUER_URL');
  }

  // Validate that we have either direct OIDC client secret or SSM key
  const hasOidcClientSecret = process.env.OIDC_CLIENT_SECRET || process.env.OIDC_CLIENT_SECRET_SSM_KEY;
  if (!hasOidcClientSecret) {
    throw new Error('Please provide OIDC_CLIENT_SECRET or OIDC_CLIENT_SECRET_SSM_KEY');
  }

  if (!process.env.OIDC_CLIENT_ID) {
    throw new Error('Please provide OIDC_CLIENT_ID');
  }

  // Validate admin group configuration
  if (!process.env.ADMIN_GROUP) {
    throw new Error('Please provide ADMIN_GROUP');
  }

  // Validate A2A server configuration
  if (!process.env.A2A_SERVER_URL) {
    throw new Error('Please provide A2A_SERVER_URL');
  }

  // Get OIDC client secret (potentially from SSM)
  const oidcClientSecret = await getSecretSSMValue('OIDC_CLIENT_SECRET', 'OIDC_CLIENT_SECRET_SSM_KEY');

  // Storage provider configuration
  const storageProvider: StorageProviderType = 'postgres';

  // PostgreSQL configuration (used when storage provider is 'postgres')
  const postgresConfig = process.env.POSTGRES_HOST
    ? {
        host: process.env.POSTGRES_HOST,
        port: Number(process.env.POSTGRES_PORT) || 5432,
        username: process.env.POSTGRES_USER || 'postgres',
        password: process.env.POSTGRES_PASSWORD || '',
        database: process.env.POSTGRES_DB || 'postgres',
        useSsl: process.env.POSTGRES_USE_SSL === 'true',
        sslCa: process.env.POSTGRES_SSL_CA,
      }
    : undefined;

  return {
    isLocal() {
      return environment === 'local';
    },
    isDev() {
      return environment === 'dev';
    },
    isStg() {
      return environment === 'stg';
    },
    isProd() {
      return environment === 'prod';
    },
    version: process.env.VERSION || 'next',
    environment,
    baseUrl: process.env.BASE_URL || `http://localhost:${process.env.SLACK_APP_PORT || 3000}`,
    slackAppPort: Number(process.env.SLACK_APP_PORT) || 3000,
    v2AppPort: Number(process.env.V2_APP_PORT) || 3001,
    httpKeepAliveTimeout: Number(process.env.HTTP_IDLE_CONNECTION_KEEP_ALIVE_TIMEOUT || 60000),
    logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'debug',
    slackAppConfig: {
      signingSecret: process.env.SLACK_SIGNING_SECRET!,
      appToken: process.env.SLACK_APP_TOKEN,
      botToken: slackBotToken,
      socketMode,
      seedAppId: process.env.SLACK_APP_ID,
      seedTeamId: process.env.SLACK_TEAM_ID,
      seedBotName: process.env.SLACK_BOT_NAME,
      seedSlashCommand: process.env.SLACK_SLASH_COMMAND,
    },
    storage: {
      provider: storageProvider,
      postgres: postgresConfig,
    },
    aws: {
      region: process.env.AWS_REGION || 'eu-central-1',
      s3: {
        fileUploadBucket: process.env.FILES_S3_BUCKET || `dev-nannos-infrastructure-agents-files`,
      },
    },
    oidc: {
      issuerUrl: process.env.OIDC_ISSUER_URL!,
      clientId: process.env.OIDC_CLIENT_ID!,
      clientSecret: oidcClientSecret,
      orchestratorAudience: process.env.OIDC_ORCHESTRATOR_AUDIENCE || 'orchestrator',
    },
    a2aServer: {
      url: process.env.A2A_SERVER_URL!,
      timeout: Number(process.env.A2A_SERVER_TIMEOUT) || 30000,
    },
    consoleBackend: process.env.CONSOLE_BACKEND_URL
      ? {
          url: process.env.CONSOLE_BACKEND_URL,
          audience: process.env.OIDC_CONSOLE_BACKEND_AUDIENCE || 'agent-console',
        }
      : undefined,
    adminGroup: process.env.ADMIN_GROUP!,
    v2CookieSecret: process.env.V2_COOKIE_SECRET!,
    sessionTtlSeconds: Number(process.env.SESSION_TTL_SECONDS) || 86400,
  };
}
