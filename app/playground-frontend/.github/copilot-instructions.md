# Playground Frontend Copilot Instructions

## Tech Stack

- React with TypeScript
- Vite as the build tool
- React Router for routing
- Tailwind CSS for styling
- shadcn/ui for UI components

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to .env generation in start-dev.sh)
- Configuration changes that affect local setup
- New service dependencies or startup requirements
- Build configuration changes that affect how the app runs locally

The `start-dev.sh` script is the single source of truth for local environment setup. Always update it when making changes that affect how the application runs locally.

## Code Style

- Use functional components with hooks
- Use the `@/` alias for imports from `src/`
- Prefer named exports over default exports

## Components
- Do not always use cards for everything. Use them judiciously based on context.
- Follow the existing design patterns and component structures in the codebase.

# Backend Integration
- The backend API is defined using OpenAPI specifications.
- The SDK for API calls is auto-generated using the OpenAPI Generator.
- To regenerate the SDK after backend API changes, run the following command in the terminal:
```
npm run gen-sdk
```
- Regenerate the SDK every time there are changes to the backend API.


## API Calls

- Use the generated SDK under `src/api/` for all backend API calls
- Do NOT use `fetch` or `axios` directly for backend calls
- If the SDK is outdated or missing endpoints, ask the user to regenerate it by running:
  ```bash
  npm run gen-sdk
  ```
