import { SSMClient, GetParameterCommand } from '@aws-sdk/client-ssm';

export type EnvName = 'local' | 'dev' | 'stg' | 'prod';

export interface StorageConfig {
  provider: string;
  postgres: {
    host?: string;
    port: number;
    username: string;
    password: string;
    database: string;
    useSsl: boolean;
    sslCa?: string;
  };
}

export interface Config {
  isLocal(): boolean;
  isDev(): boolean;
  isStg(): boolean;
  isProd(): boolean;
  readonly environment: EnvName;
  readonly baseUrl: string;
  readonly appPort: number;
  readonly logLevel: 'trace' | 'debug' | 'info' | 'warn' | 'error' | 'fatal';
  readonly storage: StorageConfig;
  readonly aws: {
    region: string;
    s3: {
      fileUploadBucket: string;
    };
  };
  readonly ses: {
    fromEmail: string;
    region: string;
    inboundS3Bucket: string;
    inboundS3Prefix: string;
  };
  readonly sns: {
    topicArn: string;
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
  readonly localSqs: {
    queueUrl: string;
    pollIntervalMs: number;
  };
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

  const environment = (process.env.ENVIRONMENT as Config['environment']) || 'local';

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

  // Validate A2A server configuration
  if (!process.env.A2A_SERVER_URL) {
    throw new Error('Please provide A2A_SERVER_URL');
  }

  // Get OIDC client secret (potentially from SSM)
  const oidcClientSecret = await getSecretSSMValue('OIDC_CLIENT_SECRET', 'OIDC_CLIENT_SECRET_SSM_KEY');

  // PostgreSQL configuration
  const postgresConfig = {
    host: process.env.POSTGRES_HOST,
    port: Number(process.env.POSTGRES_PORT) || 5432,
    username: process.env.POSTGRES_USERNAME || 'postgres',
    password: process.env.POSTGRES_PASSWORD || '',
    database: process.env.POSTGRES_DATABASE || 'postgres',
    useSsl: process.env.POSTGRES_USE_SSL === 'true',
    sslCa: process.env.POSTGRES_SSL_CA,
  };

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
    appPort: Number(process.env.APP_PORT) || Number(process.env.APP_PORT) || 3000,
    logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'debug',
    storage: {
      provider: 'postgres',
      postgres: postgresConfig,
    },
    aws: {
      region: process.env.AWS_REGION || 'eu-central-1',
      s3: {
        fileUploadBucket: process.env.FILES_S3_BUCKET || `dev-nannos-infrastructure-agents-files`,
      },
    },
    ses: {
      fromEmail: process.env.SES_FROM_EMAIL || 'local@d.nannos.rcplus.io',
      region: process.env.SES_REGION || process.env.AWS_REGION || 'eu-central-1',
      inboundS3Bucket:
        process.env.SES_INBOUND_S3_BUCKET || process.env.FILES_S3_BUCKET || 'dev-nannos-infrastructure-agents-files',
      inboundS3Prefix: process.env.SES_INBOUND_S3_PREFIX || 'inbound-emails/',
    },
    sns: {
      topicArn: process.env.SNS_INBOUND_TOPIC_ARN || '',
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
    localSqs: {
      queueUrl: process.env.LOCAL_SQS_QUEUE_URL || '',
      pollIntervalMs: Number(process.env.LOCAL_SQS_POLL_INTERVAL_MS) || 5000,
    },
  };
}
