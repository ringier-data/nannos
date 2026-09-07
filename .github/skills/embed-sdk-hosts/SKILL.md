---
name: embed-sdk-hosts
description: "Develop and release @nannos/embed-sdk together with the apps that install it. Use when: linking the cockpit (or another external app) to the local SDK checkout, releasing/publishing the SDK to npm, moving a host onto a new SDK version, or reading a `just hosts` verdict."
---

# Embed-SDK Hosts: develop and release the SDK with its consumers

## When to Use

- Working on `packages/embed-sdk` and an app that installs it at the same time
- Releasing the SDK (`just release`) and making sure every consumer follows
- A host's build fails on `@nannos/embed-sdk` (stale lockfile, `file:` path, wrong version)
- Deciding whether a consumer needs linking at all

## The Model

`@nannos/embed-sdk` has two kinds of consumer. They are handled differently.

| Consumer | Where | How it gets the SDK | Needs linking? |
|----------|-------|---------------------|----------------|
| `console-frontend` | this monorepo | npm workspace (`"@nannos/embed-sdk": "*"`) → `node_modules` symlink to `packages/embed-sdk`; its Docker image builds the SDK from source | **No.** Always the checkout, released on the same commit |
| cockpit frontend (and any app in another repo) | other repo, own CI | the published package from the npm registry, pinned in its lockfile | **Yes** — a "host" |

A **host** is any app outside this repo that installs the published package. Its CI
cannot see your nannos checkout, so its lockfile must always pin a registry version.
Local development still needs the checkout. The recipes below move a host between
those two states without ever writing to its `package.json` or lockfile.

## Registering a Host

Hosts are not discovered. Register each one once per machine with a gitignored
symlink at the repo root — the same pattern as the `gitops` symlink used by
`just deploy-prod`:

```bash
mkdir -p hosts && ln -s /path/to/rcplus-alloy-cockpit-frontend/app hosts/cockpit
```

- The link name is the host name used in every command.
- The target is the directory that holds the host's `package.json`.
- `hosts/` is in `.gitignore`. Paths differ per developer; nothing is committed.
- `scripts/host-helpers.sh` (`host_names`, `host_dir`) lists and resolves them.

## Commands

```bash
just hosts                       # where every host stands, with a verdict and the fix
just host-link cockpit           # develop against this checkout (node_modules swap only)
just host-unlink cockpit         # back to the registry copy the lockfile pins
just host-bump cockpit           # move the host onto the SDK version of this checkout
just host-bump cockpit 0.4.1     # …or onto an explicit published version
just release                     # bump/tag/publish the SDK, then bump every host
just release-pkg embed-sdk       # same, SDK only
just publish-npm embed-sdk       # retry a publish at the current version (--dry-run supported)
```

### What `host-link` Does

1. Resolves the host, checks it depends on `@nannos/embed-sdk` and has `node_modules`
2. Builds the SDK `dist` if the checkout has none (hosts import built entry points)
3. Replaces `node_modules/@nannos/embed-sdk` with a symlink to `packages/embed-sdk`
4. Leaves `package.json` and `package-lock.json` untouched — commits in the host stay safe

Then run `npm run build:watch` in `packages/embed-sdk`; the host sees every save.
The cockpit also has `npm run start:sdk-src`, which compiles the SDK **source** through
Vite so HMR keeps component state (requires the link).

### What `host-unlink` Does

1. Removes the symlink
2. Runs `npm install` in the host, which restores the registry copy from the lockfile
3. Refuses (with the fix) if the lockfile does not pin a registry version

### What `host-bump` Does

1. Waits (up to 90s) until the version is visible on the registry
2. Removes the symlink if the host is linked
3. Runs `npm install @nannos/embed-sdk@<version>` in the host — this pins **exactly**
   that version in the lockfile and writes the range in the host's own save style.
   (Editing `^version` into package.json and running a bare `npm install` would let
   npm pick the newest version the range allows — that is why the recipe does not.)
4. Verifies lockfile and `node_modules` both hold that version
5. Prints the two changed files and the commit command — it does **not** commit in the host
6. No-op when the host is already there

### What `just release` Adds for the SDK

The SDK is a normal release package (`embed-sdk/v<version>` tags) plus:

1. **Preflight**: npm credentials are checked before anything is bumped or committed.
   Publishing is the one step that cannot be rolled back
2. If hosts are registered, `just hosts` is shown before the bump
3. `npm publish` runs at the end of the push phase, after the image pushes.
   `prepublishOnly` rebuilds `dist`, so the tarball matches the tag
4. **Phase 5**, after the git push: `host-bump` for every registered host. A host failure
   is reported with the retry command; it never fails the release (it is already out)

## Reading `just hosts`

```
@nannos/embed-sdk checkout: v0.3.0 — 2 unreleased commit(s) since embed-sdk/v0.2.0 (working tree dirty)

● cockpit  /Users/…/rcplus-alloy-cockpit-frontend/app
   package.json   ^0.3.0
   node_modules   linked → /Users/…/nannos/packages/embed-sdk
   lockfile       v0.3.0
   ⚠ linked for development — commits are safe (lockfile pins v0.3.0). Back to the registry: just host-unlink cockpit
   ⚠ the checkout has unreleased SDK changes — release them (just release) before merging host code that needs them
```

| Verdict | Meaning | Fix |
|---------|---------|-----|
| `✓ in sync with the registry` | lockfile, node_modules and range agree | none |
| `⚠ linked for development` | node_modules points at the checkout; tracked files are clean | `just host-unlink <name>` when done |
| `⚠ the checkout has unreleased SDK changes` | host code may rely on SDK code nobody can install yet | `just release` before merging the host |
| `✗ the lockfile pins a local path` | a `file:` dependency was installed — CI cannot resolve it | `just host-bump <name>` |
| `✗ lockfile vX does not satisfy <range>` | package.json and lockfile disagree | `just host-bump <name>` |
| `⚠ node_modules vX ≠ lockfile vY` | stale install | `npm install` in the host |
| `✗ not installed` / `missing` | no `node_modules` | `npm install` in the host |
| `✗ no dependency on @nannos/embed-sdk` | wrong directory registered | fix the `hosts/<name>` symlink |

## Typical Workflows

**Develop SDK + cockpit together**

```bash
just host-link cockpit
cd packages/embed-sdk && npm run build:watch     # terminal 1
cd <cockpit>/app && npm run start:sdk-src        # terminal 2 (or npm start)
```
Commit in both repos as usual. The cockpit commit still pins the last released SDK.

**Ship it**

```bash
just release            # SDK (and console-frontend, which bundles it) get tagged + published; cockpit gets bumped
cd <cockpit>/app && git commit -am "chore: bump @nannos/embed-sdk to 0.4.0" && git push
just host-link cockpit  # if you keep working on the SDK
```

**Something went wrong**

```bash
just hosts                          # says what is off and what to run
just publish-npm embed-sdk          # publish failed after the tag: retry at the current version
just host-bump cockpit              # host bump failed or was skipped
```

## Prerequisites

- npm credentials for `https://registry.npmjs.org/`: `npm login`, or
  `//registry.npmjs.org/:_authToken=<token>` in `~/.npmrc`. GitHub Packages auth is not enough
- Publish rights on the `nannos` npm scope
- Node ≥ 20 with npm ≥ 9 (workspaces; `npm install` restoring a swapped symlink is verified on npm 11)

## Files

| File | Role |
|------|------|
| `scripts/host-helpers.sh` | host registry, state inspection, `wait_for_npm_version`, `bump_all_hosts` |
| `scripts/release-helpers.sh` | `NPM_PACKAGES`, `require_npm_auth`, `publish_npm_package`, `refresh_node_lockfile` |
| `justfile` → "SDK Hosts" section | `hosts`, `host-link`, `host-unlink`, `host-bump`; Phase 5 in `release` / `release-pkg` |
| `packages/embed-sdk/package.json` | `publishConfig` (public, npmjs), `prepublishOnly` build, `files: ["dist"]` |
| `hosts/<name>` | per-machine symlink to a host app dir (gitignored) |

## Gotchas

- Never commit a `file:` dependency on the SDK in a host. It is a path on one machine; every CI build fails on it. `just hosts` flags it in red
- The root `package-lock.json` records each workspace member's version. Node bumps refresh it (`refresh_node_lockfile`); otherwise console-frontend's image `npm ci` fails on the mismatch
- Adding a second npm-published package: add it to `NPM_PACKAGES` in `release-helpers.sh` and give its `package.json` a `publishConfig`; the release wiring is generic. Host recipes are SDK-specific (`SDK_NAME` in `host-helpers.sh`)
- `just --dry-run <recipe>` prints the recipe script to **stderr**
