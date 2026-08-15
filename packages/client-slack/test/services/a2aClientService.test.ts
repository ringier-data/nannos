import { describe, test, expect, jest, beforeEach } from '@jest/globals';

/**
 * Guards the push-notification wiring.
 *
 * The regression: `A2ASlackBasedRequest` carried `webhookUrl`/`webhookToken`,
 * the caller populated them, and the token was persisted on the in-flight record
 * for later validation — but `sendMessageStream` built `MessageSendParams` with
 * only `message` and `metadata`. The server was never told to push, so the
 * out-of-band delivery path that should rescue a dropped stream was dead code.
 */

const sendMessageStreamMock = jest.fn();

jest.unstable_mockModule('@a2a-js/sdk/client', () => ({
  A2AClient: {
    fromCardUrl: jest.fn(async () => ({
      sendMessageStream: sendMessageStreamMock,
    })),
  },
}));

const { A2AClientService } = await import('../../src/services/a2aClientService.js');

/** Drive the generator far enough that sendMessageStream is actually called. */
async function capturedParams(request: any): Promise<any> {
  sendMessageStreamMock.mockReturnValue(
    (async function* () {
      // no events — we only care about the params handed to the SDK
    })() as any
  );
  const service = new A2AClientService('https://orchestrator.example/');
  for await (const _ of service.sendMessageStream(request, 'access-token')) {
    // drain
  }
  return (sendMessageStreamMock.mock.calls[0] as any[])[0];
}

const baseRequest = {
  botName: 'nannos',
  userId: 'U1',
  teamId: 'T1',
  channelId: 'C1',
  messageTs: '1',
  text: 'hello',
};

describe('A2AClientService.sendMessageStream — push notification config', () => {
  beforeEach(() => {
    sendMessageStreamMock.mockReset();
  });

  test('forwards webhookUrl and webhookToken as pushNotificationConfig', async () => {
    const params = await capturedParams({
      ...baseRequest,
      webhookUrl: 'https://slack-client.example/api/v1/a2a/callback',
      webhookToken: 'secret-token',
    });

    expect(params.configuration?.pushNotificationConfig).toEqual({
      url: 'https://slack-client.example/api/v1/a2a/callback',
      token: 'secret-token',
    });
  });

  test('omits configuration entirely when no webhook is configured (local mode)', async () => {
    const params = await capturedParams({ ...baseRequest });

    // Local mode deliberately sends no webhook; we must not send an empty or
    // half-built configuration block in that case.
    expect(params.configuration).toBeUndefined();
  });

  test('sends the url without a token when only the url is set', async () => {
    const params = await capturedParams({
      ...baseRequest,
      webhookUrl: 'https://slack-client.example/api/v1/a2a/callback',
    });

    expect(params.configuration.pushNotificationConfig).toEqual({
      url: 'https://slack-client.example/api/v1/a2a/callback',
    });
  });
});
