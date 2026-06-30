/**
 * AWS SSM secret helper, isolated from config.ts so the AWS SDK is only loaded
 * when a deployment actually opts into SSM-backed config secrets (i.e. sets an
 * *_SSM_KEY env var). This keeps @aws-sdk/client-ssm an optional dependency.
 */

import { SSMClient, GetParameterCommand } from '@aws-sdk/client-ssm';

/**
 * Get a parameter from AWS SSM Parameter Store.
 */
export async function getSSMParameter(ssmKey: string): Promise<string> {
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
