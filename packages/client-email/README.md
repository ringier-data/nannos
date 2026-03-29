# RC+/Alloy A2A Slack Client

A Slack bot that acts as an A2A (App-to-App) client, allowing users to interact with A2A services directly from Slack.

## Features

- **App Mentions**: Users can mention `@a2aapp` to trigger A2A actions
- **Per-User OAuth**: Each user authorizes the bot to access A2A services on their behalf
- **Thread Context**: Captures conversation context and forwards to A2A server
- **DM Conversations**: Bot can request additional information via direct messages
- **Multi-Workspace Support**: Designed to be installed across multiple Slack workspaces

## Setup

### Prerequisites

- Node.js >= 20.0.0
- AWS account with DynamoDB access
- Slack App with appropriate permissions
- OIDC provider for user authentication

### Installation

```bash
npm install
```

### Configuration

1. Copy `.env.example` to `.env`
2. Fill in all required environment variables
3. Create DynamoDB tables (see below)

### DynamoDB Tables

Create the following tables:

1. **User Auth Table**: Stores per-user OIDC tokens
   - Partition Key: `userId` (String)
   - Attributes: `accessToken`, `refreshToken`, `expiresAt`, etc.

2. **Installations Table**: Stores Slack workspace installations
   - Partition Key: `teamId` (String)
   - Attributes: `botToken`, `botUserId`, etc.

### Development

```bash
npm run dev
```

### Build

```bash
npm run build
```

### Run

```bash
npm start
```

### Testing

```bash
npm test
npm run test:watch
npm run test:coverage
```

### Linting

```bash
npm run lint
npm run lint:fix
```

## Usage

1. **Authorize**: Users must first authorize the bot:
   - Send `/authorize` command in Slack
   - Complete OIDC authentication flow

2. **Use A2A Features**: Once authorized, mention the bot:

   ```
   @a2aapp create a ticket based on this thread
   ```

3. **Bot Response Flow**:
   - Immediate acknowledgment in thread
   - May DM for additional information
   - Posts artifact link back to thread when complete

## Architecture

- Built with Slack Bolt framework
- TypeScript for type safety
- DynamoDB for persistence
- Axios for A2A server communication
- Pino for structured logging

## License

SEE LICENSE IN LICENSE
