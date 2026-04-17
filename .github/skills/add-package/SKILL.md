---
name: add-package
description: "Add a new package/service to the Nannos monorepo. Use when: creating a new microservice, adding a new library, scaffolding a new package under packages/, registering a package for versioning and release."
---

# Add a New Package to the Monorepo

## When to Use

- Adding a new Python or Node.js service/library under `packages/`
- Scaffolding a new deployable microservice with Docker support
- Adding a new shared library (no Dockerfile)

## Overview

Every package lives under `packages/<package-name>/`. Adding one requires updates to several repo-wide coordination files so the package participates in versioning, releases, builds, and (optionally) Kubernetes deployment.

## Procedure

### 1. Create the Package Directory

Create `packages/<package-name>/` with the appropriate project files:

- **Python package**: `pyproject.toml` (with `version = "0.1.0"`), source directory, `tests/`
- **Node.js package**: `package.json` (with `"version": "0.1.0"`), `src/`, `tsconfig.json`

If the package is deployable, add a `Dockerfile`.

### 2. Update `scripts/release-helpers.sh`

This file contains all package metadata. Three places must be updated:

#### a. `ALL_PACKAGES` variable (line ~19)

Add the package name to the space-separated list:

```bash
ALL_PACKAGES="agent-creator agent-runner orchestrator-agent console-backend console-frontend ringier-a2a-sdk client-slack client-slack-frontend client-email <new-package>"
```

#### b. `pkg_dir()` function

Add a case entry mapping package name → directory:

```bash
    <new-package>)           echo "packages/<new-package>" ;;
```

#### c. `pkg_type()` function

Add a case entry declaring the package type (`python` or `node`):

```bash
    # Add to the correct case group:
    # node packages: console-frontend|client-slack-frontend|client-slack|client-email|<new-package>
    # python packages: agent-creator|agent-runner|orchestrator-agent|console-backend|ringier-a2a-sdk|<new-package>
```

### 3. Update `justfile`

Only required if the package has a **Dockerfile** (is buildable/deployable):

#### a. Add an image variable (top of file, ~line 30)

```just
img_<new_package> := registry + "/nannos-<new-package>"
```

Variable name uses underscores; image name uses hyphens.

#### b. Add to `_buildable_packages` list (~line 46)

```just
_buildable_packages := "agent-creator agent-runner orchestrator-agent console-backend console-frontend client-slack client-slack-frontend client-email <new-package>"
```

#### c. Add case in `pkg-image` recipe (~line 62)

```just
      <new-package>)    echo "{{ img_<new_package> }}" ;;
```

#### d. (Optional) Add case in `pkg-deploy` recipe

Only if the k8s deployment name differs from the package name:

```just
      <new-package>) echo "<deploy-name>" ;;
```

### 4. (Optional) Kubernetes Manifests

If the package is deployed to Kubernetes:

1. Create `example-k8s-deployment/base/<new-package>.yaml` with Deployment + Service
2. Add it to `example-k8s-deployment/base/kustomization.yaml` resources list
3. Add an image patch entry in `example-k8s-deployment/overlay/image-patch.yaml`

### 5. Verify

Run these commands to confirm the package is correctly registered:

```bash
# Should list your new package with its version
just pkg-version <new-package>

# Should show the package in the changed list (since it has no release tag yet)
just changed

# If buildable, verify the image name resolves
just pkg-image <new-package>
```

## Checklist Summary

| Step | File | Required? |
|------|------|-----------|
| Create package dir | `packages/<name>/` | Always |
| Add to `ALL_PACKAGES` | `scripts/release-helpers.sh` | Always |
| Add to `pkg_dir()` | `scripts/release-helpers.sh` | Always |
| Add to `pkg_type()` | `scripts/release-helpers.sh` | Always |
| Add image variable | `justfile` | If has Dockerfile |
| Add to `_buildable_packages` | `justfile` | If has Dockerfile |
| Add to `pkg-image` | `justfile` | If has Dockerfile |
| Add to `pkg-deploy` | `justfile` | If deploy name differs |
| Add k8s manifests | `example-k8s-deployment/` | If deployed to k8s |
