# Azure Blob Storage Implementation for WRAPP

This document describes the Azure Blob Storage implementation for WRAPP, which allows you to use Azure Blob Storage as a backend for WRAPP operations.

## Overview

The Azure Blob Storage implementation provides a way to interact with Azure Blob Storage using the WRAPP storage interface. It supports all the basic operations such as:

- Uploading files to Azure Blob Storage
- Downloading files from Azure Blob Storage
- Listing files in a container
- Deleting files from Azure Blob Storage
- Copying files within Azure Blob Storage

## URL Format

Azure Blob Storage URLs in WRAPP can be in one of the following formats:

1. `azure://account/container/blob` - WRAPP-specific format
2. `https://account.blob.core.windows.net/container/blob` - Standard Azure Blob Storage URL

Where:
- `account` is the Azure Storage account name
- `container` is the Azure Blob Storage container name
- `blob` is the path to the blob within the container

## Authentication

The Azure Blob Storage implementation supports the following authentication methods:

### 1. Environment Variables

You can set the following environment variables to authenticate with Azure Blob Storage:

- `WRAPP_AZURE_CONNECTION_STRING`: Azure storage connection string
- `WRAPP_AZURE_ACCOUNT_NAME` and `WRAPP_AZURE_ACCOUNT_KEY`: Azure storage account name and key
- `WRAPP_AZURE_ACCOUNT_NAME` and `WRAPP_AZURE_SAS_TOKEN`: Azure storage account name and SAS token

### 2. Explicit Authentication

You can also explicitly set authentication information in your code:

```python
import os

from wrapp.utils.storage import Router, StorageAuthInfo
from wrapp.utils.storage_impls.azure_blob_storage import AzureBlobAuthInfo

# Using connection string
connection_string = os.environ["WRAPP_AZURE_CONNECTION_STRING"]
auth_info = StorageAuthInfo.azure_blob("azure://your_account", connection_string)
Router.me().azure().add_auth_info(auth_info)

# Or using account name and key
account_name = "your_account"
account_key = "your_key"
auth_info = StorageAuthInfo(
    f"azure://{account_name}",
    AzureBlobAuthInfo(account_name=account_name, account_key=account_key)
)
Router.me().azure().add_auth_info(auth_info)
```

## Usage Examples

### Initializing WRAPP with Azure Blob Storage

```python
from wrapp.utils.storage import wrapp_initialize, wrapp_shutdown

# Initialize WRAPP
wrapp_initialize("1.0.0", debug_level=True)

# Use Azure Blob Storage...

# Shutdown WRAPP
wrapp_shutdown()
```

### Uploading a File to Azure Blob Storage

```python
import asyncio
from wrapp.utils.storage import wrapp_copy_address

async def upload_file():
    local_file_url = "file:///path/to/local/file.txt"
    azure_url = "azure://account/container/file.txt"
    result = await wrapp_copy_address(local_file_url, azure_url, overwrite=True)
    print(f"Upload result: {result}")

asyncio.run(upload_file())
```

### Downloading a File from Azure Blob Storage

```python
import asyncio
from wrapp.utils.storage import wrapp_copy_address

async def download_file():
    azure_url = "azure://account/container/file.txt"
    local_file_url = "file:///path/to/local/file.txt"
    result = await wrapp_copy_address(azure_url, local_file_url, overwrite=True)
    print(f"Download result: {result}")

asyncio.run(download_file())
```

### Listing Files in a Container

```python
import asyncio
from wrapp.utils.storage import wrapp_list_directory

async def list_files():
    container_url = "azure://account/container"
    async for entry in wrapp_list_directory(container_url):
        if entry.is_folder:
            print(f"Folder: {entry.relative_path}/")
        else:
            print(f"File: {entry.relative_path} ({entry.size} bytes)")

asyncio.run(list_files())
```

### Deleting a File from Azure Blob Storage

```python
import asyncio
from wrapp.utils.storage import wrapp_delete_item
from wrapp.datastructures.command_context import CommandParameters

async def delete_file():
    azure_url = "azure://account/container/file.txt"
    context = CommandParameters()
    result = await wrapp_delete_item(context, azure_url)
    print(f"Delete result: {result}")

asyncio.run(delete_file())
```

### Reading a File from Azure Blob Storage

```python
import asyncio
from wrapp.utils.storage import wrapp_read_file_content

async def read_file():
    azure_url = "azure://account/container/file.txt"
    result, content = await wrapp_read_file_content(azure_url)
    if result.name == "OK":
        print(f"File content: {content.decode('utf-8')}")
    else:
        print(f"Error reading file: {result}")

asyncio.run(read_file())
```

## Example Script

See the `azure_blob_storage_example.py` file for a complete example of how to use the Azure Blob Storage implementation.

## Testing

The Azure Blob Storage implementation includes tests in `tests/test_azure_blob_storage.py`. These tests verify that the implementation works correctly. They require valid Azure Blob Storage credentials to be set in environment variables. The tests will be skipped if the credentials are not set.

To run the tests:

```bash
pytest tests/test_azure_blob_storage.py -v
```

## Limitations

- The Azure Blob Storage implementation does not support checkpoints.
- The `create_with_hash` operation is not supported.
- Local caching is not supported.

## Metadata Handling

Azure Blob Storage has specific restrictions on metadata keys and values:

- Metadata keys must adhere to C# identifier naming rules (alphanumeric and underscore characters only)
- Keys and values can only contain ASCII characters
- Non-alphanumeric characters in metadata are not allowed except for specific ones

The Azure Blob Storage implementation includes a metadata sanitization function that automatically handles these restrictions:

- Invalid characters in keys are replaced with underscores
- Non-ASCII characters in values are replaced with underscores
- `None` values are converted to empty strings
- Non-string values are converted to strings

This sanitization is applied automatically when uploading blobs with metadata, ensuring compatibility with Azure Blob Storage's requirements.

## Comparison with S3 Implementation

The Azure Blob Storage implementation is similar to the S3 implementation in WRAPP. Both use a flat namespace with virtual directories (DirectorySemantics.PREFIX). The main differences are:

- Authentication methods are different
- URL formats are different
- Azure Blob Storage supports tags, while S3 does not
- Azure Blob Storage has different API methods for operations like copying and listing

## Environment Variables

The Azure Blob Storage implementation uses the following environment variables:

- `WRAPP_AZURE_CONNECTION_STRING`: Azure storage connection string
- `WRAPP_AZURE_ACCOUNT_NAME`: Azure storage account name
- `WRAPP_AZURE_ACCOUNT_KEY`: Azure storage account key
- `WRAPP_AZURE_SAS_TOKEN`: Azure storage SAS token
- `WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY`: Max parallel connections per upload operation (default: `1`)
- `WRAPP_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE`: Threshold for a single PUT upload in bytes (default: `67108864`, 64 MiB)
- `WRAPP_AZURE_UPLOAD_MAX_BLOCK_SIZE`: Block size for multipart/block uploads in bytes (default: `4194304`, 4 MiB)
- `WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY`: Max parallel connections per download operation (default: `1`)
- `WRAPP_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE`: Threshold for a single GET download in bytes (default: `33554432`, 32 MiB)
- `WRAPP_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE`: Chunk size for segmented downloads in bytes (default: `4194304`, 4 MiB)
