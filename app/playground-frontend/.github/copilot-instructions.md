# Playground Frontend Copilot Instructions

## Tech Stack

- React with TypeScript
- Vite as the build tool
- React Router for routing
- Tailwind CSS for styling
- shadcn/ui for UI components

## Code Style

- Use functional components with hooks
- Use the `@/` alias for imports from `src/`
- Prefer named exports over default exports

## API Calls

- Use the generated SDK under `src/api/` for all backend API calls
- Do NOT use `fetch` or `axios` directly for backend calls
- If the SDK is outdated or missing endpoints, ask the user to regenerate it by running:
  ```bash
  npm run gen-sdk
  ```
