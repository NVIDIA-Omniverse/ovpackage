# Pipeline Recipes

Multi-step workflow recipes for common ovpackage operations. Each recipe shows the exact commands in order.

---

## 1. Publish a New Package Version

Snapshot source content and store it as a versioned package.

```bash
# Step 1: Create the package (catalogs + freezes in one step)
ovpackage create my_package 1.0.0 /path/to/source/content \
    --progress-report off

# Step 2: Verify it's in the repository
ovpackage list-repo /path/to
# Output: my_package: 1.0.0
```

With a specific repository location:
```bash
ovpackage create my_package 1.0.0 s3://bucket/source/content \
    --repo s3://bucket/my_repo \
    --progress-report off
```

With a `.wrappignore` file to exclude thumbnails, logs, etc.:
```bash
ovpackage create my_package 1.0.0 /path/to/source \
    --ignore-file .wrappignore \
    --progress-report off
```

### Publishing an Updated Version

```bash
# Same command, new version string
ovpackage create my_package 2.0.0 /path/to/source/content \
    --progress-report off

# Confirm both versions exist
ovpackage list-repo /path/to
# Output: my_package: 2.0.0, 1.0.0
```

---

## 2. Install and Update Packages in a Project

### First-Time Setup

```bash
# Step 1: Create a top-level package metadata file to track dependencies
ovpackage new my_project 1.0.0 /path/to/project

# Step 2: Install a package, recording the dependency
ovpackage install vegetation_pack 20230505 /path/to/project/vegetation \
    --package /path/to/project/.my_project.wrapp \
    --progress-report off

# Step 3: Install another package
ovpackage install rocks_pack v1.0.0 /path/to/project/rocks \
    --package /path/to/project/.my_project.wrapp \
    --progress-report off

# Step 4: Verify what's installed
ovpackage installed-package-list /path/to/project/.my_project.wrapp
```

### Updating to a Newer Version

```bash
# Step 1: Check for local modifications
ovpackage status /path/to/project/.my_project.wrapp \
    --progress-report off

# Step 2: If clean, install the new version (overwrites old)
ovpackage install vegetation_pack 20230512 /path/to/project/vegetation \
    --package /path/to/project/.my_project.wrapp \
    --progress-report off
```

---

## 3. Handle Install Conflicts with Patches

When an install fails because of local modifications:

```bash
# Step 1: Attempt install (will fail if destination is dirty)
ovpackage install vegetation_pack 20230512 /path/to/project/vegetation
# Error: local modifications detected

# Step 2: Re-run with --patch to capture conflicts
ovpackage install vegetation_pack 20230512 /path/to/project/vegetation \
    --patch /tmp/conflicts.patch \
    --progress-report off

# Step 3: Review the patch file (JSON format)
cat /tmp/conflicts.patch

# Step 4: Apply the patch to complete the update
ovpackage apply-patch /tmp/conflicts.patch \
    --progress-report off
```

For a manual three-way merge using catalogs:

```bash
# Step 1: Extract the original version's catalog
ovpackage extract-catalog vegetation_pack 20230505 /tmp/base.json \
    --repo /path/to/repo --progress-report off

# Step 2: Catalog the current (modified) state
ovpackage catalog /path/to/project/vegetation /tmp/current.json \
    --progress-report off

# Step 3: Extract the new version's catalog
ovpackage extract-catalog vegetation_pack 20230512 /tmp/new.json \
    --repo /path/to/repo --progress-report off

# Step 4: Create a merge patch
ovpackage create-patch /tmp/current.json /tmp/new.json /tmp/base.json \
    --patch /tmp/merge.patch --show

# Step 5: Apply the merge
ovpackage apply-patch /tmp/merge.patch --progress-report off
```

---

## 4. Promote Packages Between Environments (Mirror)

### Simple Promotion

```bash
# Dev to staging
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --progress-report off
```

### With Dependencies

```bash
# Mirror package and all its subpackages
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --recursive \
    --progress-report off
```

### Resume Interrupted Transfer

```bash
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --resume \
    --progress-report off
```

### Differential Mirror (Only Transfer Changes)

```bash
# If v1.0.0 was already mirrored, use it as template for v1.1.0
ovpackage mirror my_package v1.1.0 \
    --source-repo s3://source-bucket \
    --destination-repo s3://dest-bucket \
    --template-version v1.0.0 \
    --progress-report off
```

---

## 5. Export/Import for Air-Gapped Delivery

### Export

```bash
# Step 1: Export to a tar file
ovpackage export vegetation_pack 20230505 \
    --repo omniverse://dev.example.com \
    --progress-report off
# Creates: vegetation_pack.20230505.tar

# With deduplication for packages with many duplicate files
ovpackage export vegetation_pack 20230505 \
    --repo omniverse://dev.example.com \
    --dedup \
    --progress-report off

# Custom output filename
ovpackage export vegetation_pack 20230505 \
    --repo omniverse://dev.example.com \
    --output /mnt/usb/delivery.tar \
    --progress-report off
```

### Import

```bash
# Step 2: At the destination site, import into their repository
ovpackage import vegetation_pack.20230505.tar \
    --repo omniverse://client.example.com \
    --progress-report off

# Step 3: Verify it arrived
ovpackage list-repo omniverse://client.example.com

# Step 4: Install as normal
ovpackage install vegetation_pack 20230505 \
    omniverse://client.example.com/projects/demo/vegetation \
    --progress-report off
```

---

## 6. Catalog and Diff for Change Tracking

### Take Periodic Snapshots

```bash
# January snapshot
ovpackage catalog omniverse://localhost/assets/skies/ skies_jan.json \
    --progress-report off

# February snapshot
ovpackage catalog omniverse://localhost/assets/skies/ skies_feb.json \
    --progress-report off
```

For S3/Azure (no server-side checksums):
```bash
ovpackage catalog s3://my-bucket/assets/ assets.json \
    --local-hash --progress-report off
```

### Compare Snapshots

```bash
# Diff with detailed output
ovpackage diff skies_jan.json skies_feb.json --show

# Just check if different (exit code 0=same, 1=different)
ovpackage diff skies_jan.json skies_feb.json
echo $?

# Show only files unique to one side
ovpackage diff skies_jan.json skies_feb.json --show-unique
```

### Count Statistics

```bash
ovpackage count skies_jan.json
# Prints: file count, folder count, total size, dedup stats
```

---

## 7. Freeze for Reproducible Builds

### Archive a Directory

```bash
# Copy source tree to archive location
ovpackage freeze omniverse://server/lib/props/vegetation \
    omniverse://server/archive/vegetation_20230505 \
    --progress-report off
```

### Freeze from a Catalog

```bash
# Step 1: Catalog first (for repeatability)
ovpackage catalog omniverse://server/source vegetation.json \
    --progress-report off

# Step 2: Freeze from the catalog to local disk (CI/CD)
ovpackage freeze vegetation.json file:///build/output/vegetation \
    --catalog --progress-report off
```

### Freeze Excluding Subpackages

```bash
# Only freeze the top-level files, skip installed subpackage content
ovpackage freeze omniverse://server/project /archive/project_snapshot \
    --ignore-packages --progress-report off
```

---

## 8. Repository Cleanup (find-orphans, delete-packages)

### Find Unreferenced Packages

```bash
# Step 1: Identify orphans given seed packages that should be kept
ovpackage find-orphans \
    --package omniverse://server/project/.project.wrapp \
    --package omniverse://server/project2/.project2.wrapp \
    --repo omniverse://server \
    --progress-report off

# Write results to a file
ovpackage find-orphans \
    --package omniverse://server/project/.project.wrapp \
    --repo omniverse://server \
    --output orphans.txt -w \
    --progress-report off
```

### Delete Orphaned Packages

```bash
# Step 2: Dry run first!
ovpackage delete-packages --package-list orphans.txt --dry-run

# Step 3: Actually delete (DESTRUCTIVE -- breaks any packages depending on these)
ovpackage delete-packages --package-list orphans.txt \
    --progress-report off
```

### Delete Specific Packages

```bash
ovpackage delete-packages \
    omniverse://server/.packages/old_pack/v0.1.0/.old_pack.wrapp \
    --progress-report off
```

---

## 9. Provenance Tracking (Traceback)

### Trace Package History

```bash
ovpackage traceback omniverse://localhost/project/.project.wrapp

# Output shows the chain:
# install location -> repository -> source (or mirror source)
```

### Verify Package Integrity

```bash
# Check a single package
ovpackage check vegetation_pack 20230505 \
    --repo omniverse://localhost \
    --progress-report off

# Check recursively (package + all dependencies)
ovpackage check vegetation_pack 20230505 \
    --repo omniverse://localhost \
    --recursive \
    --progress-report off
```

---

## 10. Working with Metapackages and .wrappignore

### Create a Metapackage (Dependencies Only, No Files)

```bash
# From a JSON dependency set
ovpackage create-metapackage scene_bundle 1.0.0 \
    --repo omniverse://server \
    --json dependencies.json \
    --progress-report off

# From a CSV file (columns: name, version, path, repo)
ovpackage create-metapackage scene_bundle 1.0.0 \
    --repo omniverse://server \
    --csv dependencies.csv \
    --progress-report off
```

### Use .wrappignore

Create a `.wrappignore` file (same syntax as `.gitignore`):

```text
# Thumbnails and caches
.thumbs/
*.tmp

# Logs
*.log
*.txt

# OS files
.DS_Store
Thumbs.db
```

ovpackage automatically finds `.wrappignore` in the source directory during `catalog`, `create`, and `freeze`. For a custom location:

```bash
ovpackage create my_package 1.0.0 /source \
    --ignore-file /path/to/custom_ignore.txt \
    --progress-report off
```

Set globally via environment variable:
```bash
export OVPACKAGE_IGNORE_FILE=/path/to/.wrappignore
```

### Browse Remote Content

```bash
# List a remote directory
ovpackage ls omniverse://server/assets/

# Recursive listing with pattern filter
ovpackage ls omniverse://server/assets/ -R --pattern "*.usd"

# Download a single file
ovpackage get omniverse://server/assets/readme.txt -o /tmp/readme.txt

# Print a remote file to console
ovpackage cat omniverse://server/project/.project.wrapp
```
