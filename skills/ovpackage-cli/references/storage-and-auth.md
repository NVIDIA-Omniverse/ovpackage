# Storage Systems & Authentication

ovpackage has six storage backends. Five are commonly used; the sixth (Storage API) is optional and service-specific.

| Backend | URL scheme | Requires server | Notes |
|---------|-----------|-----------------|-------|
| **S3** | `s3://`, `https://...amazonaws.com`, `https://...cloudfront.net` | No (AWS/MinIO) | Object storage via boto3 |
| **Azure** | `https://<account>.blob.core.windows.net/...` | No (Azure) | Object storage via azure-storage-blob |
| **GCS** | `gs://bucket-name/...` | No (GCP) | Object storage via google-cloud-storage. Install with `pip install ovpackage[gcs]` |
| **Nucleus** | `omniverse://hostname/...` | Yes (Nucleus server) | Nucleus file server |
| **Local** | `file:///...`, bare paths | No | Local filesystem |
| **Storage API** | Service-specific schemes | Yes (gRPC service) | Optional service-specific backend; **not Nucleus** |

**Important**: Storage API and Nucleus are completely separate backends. A line like "StorageAPI: ENABLED but not configured" (or an error from a configured-but-unreachable Storage API endpoint) does NOT mean Nucleus is broken. It means no Storage API discovery URL or gRPC address is configured, which is normal for most users. Nucleus (`omniverse://`) URLs work independently.

## URL Formats

### S3

```
s3://bucket-name/path/to/data
https://bucket-name.s3.amazonaws.com/path/to/data
https://distribution-id.cloudfront.net/path/to/data
```

- `s3://` and `https://...amazonaws.com` use boto3 directly
- `https://...cloudfront.net` uses the client library (read-only CDN)
- HTTP (not HTTPS) is only acceptable for local test endpoints like MinIO (`http://localhost:9000`)

### Azure Blob Storage

```
https://<account>.blob.core.windows.net/<container>/path/to/data
```

### Google Cloud Storage

```
gs://bucket-name/path/to/data
```

### Nucleus Servers

```
omniverse://hostname/path/to/data
```

Example: `omniverse://localhost/assets/vegetation`

### Local File System

```
file:///absolute/path/to/data
file://localhost/absolute/path/to/data
local_folder                          # no scheme = local path
file:relative_folder                  # relative path
```

Any URL without a recognized scheme is treated as a local file path.

---

## Authentication

### `--auth` Flag (All Backends)

The `--auth` flag accepts comma-separated credential triplets. Can be specified multiple times for different servers.

**Nucleus:**
```
--auth omniverse://SERVER,USERNAME,PASSWORD
--auth omniverse://SERVER,\$omni-api-token,API_TOKEN
```

Example with default local credentials:
```bash
ovpackage list-repo omniverse://localhost --auth omniverse://localhost,omniverse,omniverse
```

Example with API token (escape `$` on Linux):
```bash
ovpackage list-repo omniverse://staging.example.com/repo \
    --auth omniverse://staging.example.com,\$omni-api-token,$STAGING_TOKEN
```

**S3:**
```
--auth s3://BUCKET,AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY
```

Only one S3 auth value can be specified (applies to all S3 buckets).

**Azure:**
```
--auth https://ACCOUNT.blob.core.windows.net,CONNECTION_STRING
```

### `OVPACKAGE_AUTH` Environment Variable

Same format as `--auth` but multiple entries are separated by `:` on Linux and `;` on Windows.

```bash
export OVPACKAGE_AUTH="omniverse://server1,user1,pass1:omniverse://server2,user2,pass2"
```

### Interactive Authentication (Nucleus Only)

```bash
ovpackage list-repo omniverse://myserver --interactive-auth
```

Opens a browser for SSO. Successful connections are cached for subsequent commands. **Never use `-I` in automated pipelines** -- it will hang waiting for browser input.

### S3 Default Credential Chain

When no `--auth` is provided for S3, boto3 uses its standard credential chain:
1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`)
2. Shared credentials file (`~/.aws/credentials`)
3. AWS config file (`~/.aws/config`)
4. IAM role (EC2/ECS/Lambda)

### Azure Default Authentication

When no `--auth` is provided for Azure, the `azure-storage-blob` library uses standard Azure SDK mechanisms (environment variables, managed identity, etc.).

### GCS Default Authentication

When no `--auth` is provided for GCS, the `google-cloud-storage` library uses Google Application Default Credentials:
1. `GOOGLE_APPLICATION_CREDENTIALS` environment variable
2. Credentials from `gcloud auth application-default login`
3. Service account on GCE/GKE

With `--auth`:
```
--auth gs://BUCKET,/path/to/credentials.json
```
Omit the credentials path to use Application Default Credentials for a specific bucket.

---

## Storage Semantics

### Object Storage (S3, Azure)

- Folders are **implicit** -- they exist only as prefixes in object keys
- Deleting all files in a "folder" makes the folder vanish
- **Empty folders cannot exist** and are not captured in packages

### File System Storage (Local, Nucleus)

- Folders are **first-class** -- they exist independently of contents
- Empty folders persist after files are removed
- `uninstall` and `delete-packages` remove files but **leave empty folders behind**

### Implications

- **Package creation**: Only files are captured. Add a `.keep` placeholder for empty folders you need preserved.
- **Uninstall cleanup**: Empty folder skeletons may remain on filesystem/Nucleus after uninstall. Clean up manually if needed.

---

## Performance Tuning

### Parallelism

| Env Variable | CLI Flag | Default | Controls |
|-------------|----------|---------|----------|
| `OVPACKAGE_JOBS` | `--jobs`, `-j` | `200` | Parallel storage API operations |
| | `--file-transfer-jobs` | `50` | Parallel file copy operations |
| | `--tagging-jobs` | `50` | Parallel tagging service operations |

### S3 Multipart Upload

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `OVPACKAGE_S3_MULTIPART_THRESHOLD` | `5242880` (5 MiB) | Min file size for multipart upload |
| `OVPACKAGE_S3_MULTIPART_CHUNKSIZE` | `5242880` (5 MiB) | Size of each upload part |
| `OVPACKAGE_S3_MAX_CONCURRENCY` | `50` | Max threads for multipart uploads |

Example for large files over high-bandwidth connections:
```bash
export OVPACKAGE_S3_MULTIPART_THRESHOLD=10485760   # 10 MiB
export OVPACKAGE_S3_MULTIPART_CHUNKSIZE=10485760   # 10 MiB
export OVPACKAGE_S3_MAX_CONCURRENCY=32
```

### Azure Performance

| Env Variable | Description |
|-------------|-------------|
| `OVPACKAGE_AZURE_UPLOAD_MAX_CONCURRENCY` | Max concurrent upload operations |
| `OVPACKAGE_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE` | Max size for single PUT upload |
| `OVPACKAGE_AZURE_UPLOAD_MAX_BLOCK_SIZE` | Block size for block uploads |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_CONCURRENCY` | Max concurrent download operations |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE` | Max size for single GET download |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE` | Chunk size for chunked downloads |

### Storage API Performance

| Env Variable | Description |
|-------------|-------------|
| `OVPACKAGE_STORAGEAPI_ENABLE_RANGE_DOWNLOAD` | Enable range-based downloads with Storage API |
| `OVPACKAGE_STORAGEAPI_DOWNLOAD_RANGE_PART_SIZE` | Part size for range downloads |
| `OVPACKAGE_STORAGEAPI_DOWNLOAD_RANGE_MAX_CONCURRENCY` | Max concurrent range download operations |

### S3 Transfer Acceleration

```bash
export OVPACKAGE_ENABLE_S3_ACCELERATION=true
```

Global switch -- all accessed buckets must have Transfer Acceleration enabled in AWS. Additional AWS costs apply.

### Progress Reports

Suppress interactive progress bars (recommended for automation and AI agents):
```bash
export OVPACKAGE_PROGRESS_REPORT=off
# or per-command:
ovpackage create ... --progress-report off
```

---

## Environment Variable Summary

| Variable | Description |
|----------|-------------|
| `OVPACKAGE_AUTH` | Auth credentials (`:` separated on Linux, `;` on Windows) |
| `OVPACKAGE_REPO` / `OVPACKAGE_REMOTE` | Default repository URL |
| `OVPACKAGE_SOURCE_REPO` | Default source repo for mirror |
| `OVPACKAGE_DESTINATION_REPO` | Default destination repo for mirror |
| `OVPACKAGE_JOBS` | Parallel storage operations (default 200) |
| `OVPACKAGE_IGNORE_FILE` | Default ignore file path |
| `OVPACKAGE_LOG_FILE` | Log file path (default `ovpackage.log`) |
| `OVPACKAGE_PROGRESS_REPORT` | Progress verbosity: `off\|normal\|verbose` |
| `OVPACKAGE_HASH_CACHE_FILE` | Local hash cache file for catalog |
| `OVPACKAGE_S3_MULTIPART_THRESHOLD` | Multipart upload threshold in bytes |
| `OVPACKAGE_S3_MULTIPART_CHUNKSIZE` | Multipart chunk size in bytes |
| `OVPACKAGE_S3_MAX_CONCURRENCY` | Multipart upload thread count |
| `OVPACKAGE_ENABLE_S3_ACCELERATION` | Enable S3 Transfer Acceleration |
| `OVPACKAGE_AZURE_UPLOAD_MAX_CONCURRENCY` | Azure upload concurrency |
| `OVPACKAGE_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE` | Azure single PUT max size |
| `OVPACKAGE_AZURE_UPLOAD_MAX_BLOCK_SIZE` | Azure block upload size |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_CONCURRENCY` | Azure download concurrency |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE` | Azure single GET max size |
| `OVPACKAGE_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE` | Azure chunk GET size |
| `OVPACKAGE_STORAGEAPI_ENABLE_RANGE_DOWNLOAD` | Enable Storage API range downloads |
| `OVPACKAGE_STORAGEAPI_DOWNLOAD_RANGE_PART_SIZE` | Storage API range download part size |
| `OVPACKAGE_STORAGEAPI_DOWNLOAD_RANGE_MAX_CONCURRENCY` | Storage API range download concurrency |
| `OVPACKAGE_DISABLE_NUCLEUS` | Disable Nucleus/client-library backend |
| `OVPACKAGE_AZURE_API_VERSION` | Override Azure API version |
