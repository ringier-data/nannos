import type { StorageConfig } from '../storage/index.js';

export type EnvName = 'local' | 'dev' | 'stg' | 'prod';
export type StorageProviderType = 'postgres';
/**
 * Backend for per-installation notification secrets.
 *   'db'      — cloud-agnostic default, persisted via the storage provider.
 *   'aws-ssm' — AWS SSM Parameter Store (opt-in; requires AWS credentials).
 */
export type InstallationSecretProvider = 'db' | 'aws-ssm';

export interface Config {
  isLocal(): boolean;
  isDev(): boolean;
  isStg(): boolean;
  isProd(): boolean;
  readonly environment: EnvName;
  readonly baseUrl: string;
  readonly appPort: number;
  readonly httpKeepAliveTimeout: number;
  readonly logLevel: 'trace' | 'debug' | 'info' | 'warn' | 'error' | 'fatal';
  readonly googleChatTokenExpectedAudience: string;
  readonly googleChatConfigs: {
    projectName: string;
    projectNumber: string; // GCP project number for verifying Google-signed tokens
    botName: string; // Bot display name; used as the installation_id for delivery-channel registration
    googleApplicationCredentials: any;
  }[];
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
  readonly installationSecret: {
    provider: InstallationSecretProvider;
    ssmPrefix: string; // Only used when provider is 'aws-ssm'.
  };
}

export async function getConfigFromEnv(): Promise<Config> {
  // Required environment variables
  if (!process.env.ENVIRONMENT) {
    throw new Error('Please provide ENVIRONMENT');
  }

  const environment = (process.env.ENVIRONMENT as Config['environment']) || 'local';

  // Google Chat configuration
  if (!process.env.GCP_CHAT_PROJECTS) {
    throw new Error('Please provide GCP_CHAT_PROJECTS');
  }

  if (!process.env.GOOGLE_CHAT_TOKEN_EXPECTED_AUDIENCE) {
    throw new Error('Please provide GOOGLE_CHAT_TOKEN_EXPECTED_AUDIENCE');
  }

  // Validate OIDC configuration
  if (!process.env.OIDC_ISSUER_URL) {
    throw new Error('Please provide OIDC_ISSUER_URL');
  }

  // Validate that we have either direct OIDC client secret
  if (!process.env.OIDC_CLIENT_SECRET) {
    throw new Error('Please provide OIDC_CLIENT_SECRET');
  }

  if (!process.env.OIDC_CLIENT_ID) {
    throw new Error('Please provide OIDC_CLIENT_ID');
  }

  // Validate A2A server configuration
  if (!process.env.A2A_SERVER_URL) {
    throw new Error('Please provide A2A_SERVER_URL');
  }

  const googleChatConfigs: Config['googleChatConfigs'] = [];
  for (const project of JSON.parse(process.env.GCP_CHAT_PROJECTS) as { name: string; google_chat_app_id: string; bot_name: string }[]) {
    const envVarName = `GCP_SA_JSON_KEY_${project.name.toUpperCase().replace(/-/g, '_')}`;
    if (!process.env[envVarName]) {
      throw new Error(`Please provide ${envVarName}`);
    }
    // bot_name is load-bearing: it is the installation_id used for delivery-channel
    // registration and the SSM key for the inbound notification secret. A missing
    // value would silently break both, so fail fast at startup instead.
    if (!project.bot_name) {
      throw new Error(`Missing bot_name for Google Chat project '${project.name}' in GCP_CHAT_PROJECTS`);
    }
    googleChatConfigs.push({
      projectName: project.name,
      projectNumber: project.google_chat_app_id,
      botName: project.bot_name,
      googleApplicationCredentials: JSON.parse(process.env[envVarName]!),
    });
  }


  // Storage provider configuration
  const storageProvider = (process.env.STORAGE_PROVIDER as StorageProviderType);


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
    environment,
    baseUrl: process.env.BASE_URL || `http://localhost:${process.env.APP_PORT || 3000}`,
    appPort: Number(process.env.APP_PORT) || 3000,
    httpKeepAliveTimeout: Number(process.env.HTTP_IDLE_CONNECTION_KEEP_ALIVE_TIMEOUT || 60000),
    logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'debug',
    googleChatTokenExpectedAudience: process.env.GOOGLE_CHAT_TOKEN_EXPECTED_AUDIENCE,
    googleChatConfigs,
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
      clientSecret: process.env.OIDC_CLIENT_SECRET,
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
    installationSecret: {
      provider: (process.env.INSTALLATION_SECRET_PROVIDER as InstallationSecretProvider) || 'db',
      ssmPrefix:
        process.env.INSTALLATION_SECRET_SSM_PREFIX ||
        `/nannos/${environment}/client-google-chat/installation-secrets`,
    },
  };
}
