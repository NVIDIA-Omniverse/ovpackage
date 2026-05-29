# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

PYTHON ?= python
PYTEST ?= pytest

.PHONY: test test-unit test-local test-emulators start-test-services stop-test-services

test: test-unit

test-unit:
	WRAPP_DISABLE_NUCLEUS=True WRAPP_DISABLE_AWS=True WRAPP_DISABLE_AZURE=True WRAPP_DISABLE_GCS=True WRAPP_ENABLE_STORAGE_API=False \
	$(PYTHON) -m $(PYTEST) tests --timeout=300 -k "not s3 and not azure and not gcs and not STORAGEAPI"

test-local:
	WRAPP_DISABLE_AWS=True WRAPP_DISABLE_AZURE=True WRAPP_DISABLE_GCS=True WRAPP_DISABLE_NUCLEUS=True WRAPP_ENABLE_STORAGE_API=False \
	$(PYTHON) -m $(PYTEST) tests/test_local_file_storage.py tests/test_package.py tests/test_ignore.py

test-emulators:
	WRAPP_DISABLE_NUCLEUS=True WRAPP_ENABLE_STORAGE_API=False WRAPP_DISABLE_AWS=False WRAPP_DISABLE_AZURE=False WRAPP_DISABLE_GCS=False \
	WRAPP_S3_ENDPOINT=http://localhost:9000 WRAPP_FIXED_REGION=eu-central-1 WRAPP_GCS_ENDPOINT=http://localhost:4443 \
	WRAPP_AZURE_ACCOUNT_NAME=devstoreaccount1 WRAPP_AZURE_CONNECTION_STRING=UseDevelopmentStorage=true \
	$(PYTHON) -m $(PYTEST) tests/test_s3.py tests/test_azure_blob_storage.py tests/test_gcs.py

start-test-services:
	docker compose up -d minio azurite fake-gcs

stop-test-services:
	docker compose down
