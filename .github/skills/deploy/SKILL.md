---
name: deploy
description: "Deploy a package to your FluxCD environment. Use when: deploying a service to dev/staging/prod, building a dev prerelease image, triggering a Flux reconciliation, or troubleshooting deployment issues."
---

# Deploy a Release

## When to Use

- Deploying a service change to the dev Kubernetes cluster
- Deploying a released version to the prod Kubernetes cluster
- Building and pushing a `-next` prerelease image
- Triggering FluxCD to pick up a new image tag

## Methods

### Deploy to prod using `gitops` symlink

For prod deployments: The root of this repo must contain a `gitops` symlink pointing to the gitops repository. Expected structure:

```
gitops -> FLUX CD GITOPS REPO
```

The gitops repo structure:

```
gitops/
├── manifests/
│   ├── apps/nannos/      # FluxCD Kustomization names "nannos-app"
│   │   ├── base/         # K8s manifests (Deployments, Services, etc.)
│   │   └── dev/          # dev overlay
│   │   └── stg/          # stg overlay
│   │   └── prod/         # prod overlay
│   │   └── any-env/      # any env overlay
```

The reason is that `just deploy-prod` - which is the command for deploying to a prod environment with the latest stable release - needs to push changes to the `manifests/apps/nannos/prod/` directory in the gitops repo. The `gitops` symlink allows the justfile to access that path regardless of where the gitops repo is located on the developer's machine.

#### CLI Tools Required

- `docker` with buildx
- `git`

### FluxCD Image Automation

For this method, it's enough that the cluster has FluxCD installed and configured with ImageRepository and ImagePolicy resources that point to the correct image registry and tag patterns. The `just deploy-dev` command will trigger FluxCD to pick up new `v1.2.3-next${timestamp}` prerelease tags and deploy them. Same for `just release` which creates a release tag `v1.2.3` and relies on FluxCD to pick it up for deployments.

The cluster is expected to use FluxCD image automation with:

- **ImageRepository** per package — targets `ghcr.io/ringier-data/nannos-<pkg>`
- **ImagePolicy** per package — uses semver policy with `filterTags: "-next"` to match dev prerelease tags
- **ImagePolicy** per package — uses semver policy to match stable tags
- **ImageUpdateAutomation** — commits tag bumps to `manifests/apps/nannos/${env}/` via the `Setters` strategy

#### CLI Tools Required

- `docker` with buildx
- `flux` CLI (FluxCD)
- `kubectl` with access to the dev cluster

If `flux reconcile` commands fail, either:
- FluxCD is not installed on the cluster
- The `flux` CLI is not installed locally
- kubectl context is not pointing to the correct cluster

#### Deploy Commands

```bash
# Deploy to dev (builds -next prerelease image, pushes, triggers Flux)
just deploy-dev <package-name>

# Deploy to prod (updates gitops repo with latest stable tag, pushes)
just deploy-prod <package-name>
```

Examples:
```bash
just deploy-dev client-slack
just deploy-prod orchestrator-agent
```

### What `deploy-dev` Does

1. **Builds a dev prerelease image** tagged `v<version>-next.<YYYYMMDDHHmmss>` (e.g., `v1.10.0-next.20260417143022`)
2. **Pushes** the image to `ghcr.io/ringier-data/nannos-<package>`
3. **Waits** for the tag to be visible in the registry (up to 30s)
4. **Triggers FluxCD reconciliation**:
   - `flux reconcile image repository nannos-<package>` — forces Flux to re-scan tags
   - `flux reconcile image policy nannos-<package>` — forces Flux to evaluate the new tag
   - `flux reconcile kustomization nannos-app --with-source` — forces Flux to apply changes
5. **Waits for rollout** of the Kubernetes deployment (`kubectl rollout status`, 300s timeout)

### What `deploy-prod` Does

1. **Validates** the `gitops` symlink exists and contains the prod overlay
2. **Resolves** the package's current version and image reference
3. **Verifies** the image tag exists in the container registry
4. **Updates** the image tag in `gitops/manifests/apps/nannos/prod/image-patch.yaml`
5. **Commits and pushes** the change to the gitops repo
6. FluxCD detects the commit and **rolls out** the new image to the prod cluster

### Package → Deployment Mapping

Some packages share a K8s deployment:

| Package | K8s Deployment |
|---------|---------------|
| `console-backend` | `console` |
| `console-frontend` | `console` |
| `client-slack` | `client-slack` |
| `client-slack-frontend` | `client-slack` |
| All others | Same as package name |

## Related Commands

```bash
# Build dev image only (no deploy)
just build-dev <package>

# Build and push a release image (tagged v<version>)
just push=true build-pkg <package>

# Full release: bump version, tag, build, push
just release-pkg <package>

# Deploy latest stable release to prod (requires gitops symlink)
just deploy-prod <package>
```
