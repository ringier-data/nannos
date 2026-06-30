# Follow-up: cloud-agnostic file storage

**Status:** proposed (not implemented)
**Scope:** `client-slack` file uploads + the A2A orchestrator's file download

## Why this is deferred

Secrets (config + per-installation notification secrets) have been made
cloud-agnostic — see `installationSecretServiceFactory.ts`, `dbInstallationSecretService.ts`,
and the `getSecretValue` change in `config/config.ts`. File storage is **not** yet,
because it is the only AWS coupling that crosses a service boundary and therefore
can't be fixed inside `client-slack` alone.

## The coupling

`FileStorageService` ([src/services/fileStorageService.ts](../src/services/fileStorageService.ts))
uploads Slack attachments to S3 and returns an `s3://bucket/key` URI. That URI is
attached as the `url` of a file part ([src/utils/fileUtils.ts](../src/utils/fileUtils.ts), `processFileToS3`)
and handed to the **A2A orchestrator**, which then downloads the object directly
using its own AWS/IAM permissions.

So the contract between the two services is AWS-shaped: a non-AWS deployer would
have to stand up S3 *and* grant the orchestrator S3 access just to send a file.

## Proposed design

Stop emitting `s3://`. Define a `FileStore` interface whose `uploadFile()` always
returns an **HTTPS URL the orchestrator can plainly `GET`**, regardless of backend:

```ts
interface FileStore {
  uploadFile(buf: Buffer, name: string, mime: string, userId: string, contextId: string): Promise<UploadedFile>;
}
// UploadedFile.url is always http(s) and fetchable; no scheme-specific handling downstream.
```

Providers, selected by `FILE_STORE_PROVIDER` (mirroring `createStorageProvider` and
the new installation-secret factory):

- **`local` (OSS default)** — `client-slack` already runs an HTTP server; serve the
  uploaded file from it behind a short-lived signed/tokened URL. No object store, no
  cloud account. `console-backend` already has a precedent in
  `console_backend/services/local_file_storage_service.py`.
- **`s3`** — keep S3 storage but return a **presigned HTTPS URL** instead of `s3://`.
  The unused `@aws-sdk/s3-request-presigner` dependency is exactly the tool for this.
- (future) `gcs` / MinIO — same interface, signed URL out.

### Orchestrator side (the cross-service part)

The orchestrator must stop special-casing `s3://` and simply fetch the `url` over
authenticated HTTPS. This removes its hidden "needs S3 download permissions"
coupling and is what makes the whole path cloud-agnostic. This change lives in the
orchestrator package and is the reason this work is tracked separately.

## Dependency cleanup deferred with this

- `@aws-sdk/client-s3` stays a hard dependency until the `local` provider lands,
  then moves to `optionalDependencies` like `@aws-sdk/client-ssm`.
- `@aws-sdk/s3-request-presigner` is currently unused; it becomes used by the `s3`
  provider above.
