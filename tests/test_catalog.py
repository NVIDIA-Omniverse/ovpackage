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

import copy
import importlib
import io
import json
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path

from wrapp import catalog
from wrapp.commands.catalog import (
    AbsoluteUrl,
    CatalogBuilder,
    RelativePath,
)
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.two_way_diff import Diff
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.router import Router
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    Result,
    wrapp_copy_address,
    wrapp_stat_and_calculate_hash_if_missing,
    wrapp_supports_tags,
    wrapp_url_exists,
)
from wrapp.utils.storage_interface import ResourceIdentity
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_path_from_url,
)

from .common import *
from .storage_test_helpers import (
    StorageBackendForTests,
    random_bytes,
)
from .wrapp_sync_funcs import wrapp_url_exists_sync

catalog_module = importlib.import_module("wrapp.commands.catalog")


def _make_catalog(relative_paths):
    return Catalog(
        root="file:/dummy",
        items=[CatalogItem(source_path=f"file:/dummy/{p}", relative_path=p) for p in relative_paths],
    )


@pytest.mark.asyncio
async def test_catalog_keeps_logical_source_path_when_backend_returns_identity(monkeypatch):
    """A backend identity is catalog output; it must not replace the input address."""
    input_relative_path = "asset.usd"
    source_url = f"https://content.example/assets/{input_relative_path}"
    source_identity = "storage-id://service/original-version"

    class FakeResourceIdentity:
        def to_string(self):
            return source_identity

    class FakeStorage:
        async def make_resource_identity(self, file_url, item, relative_url):
            assert file_url == source_url
            return (
                "https://blob-store.example/deduplicated/object",
                "backend/rewritten/path.usd",
                FakeResourceIdentity(),
            )

    class FakeRouter:
        def storage_for_read(self, source):
            assert source == source_url
            return FakeStorage()

    class FakeItem:
        modified_time = datetime.now(timezone.utc)
        size = 8
        nucleus_hash = "sha-256-flat;1048576;original"
        has_checkpoint = True

    monkeypatch.setattr(Router, "me", classmethod(lambda cls: FakeRouter()))

    builder = CatalogBuilder(task_node=object())
    await builder._catalog_file_item(
        task_node=object(),
        item=FakeItem(),
        base=AbsoluteUrl(absolute_url="https://content.example/assets"),
        relative=RelativePath(relative_path=input_relative_path),
    )

    assert len(builder.result_list) == 1
    assert builder.result_list[0].source_path == source_url
    assert builder.result_list[0].relative_path == input_relative_path
    assert builder.result_list[0].resource_identity_v2 == source_identity


@pytest.mark.asyncio
async def test_catalog_hash_is_computed_from_recorded_resource_identity(monkeypatch, tmp_path):
    """A catalog identity and hash must describe the same immutable bytes."""
    original_bytes = b"bytes named by the resource identity"
    source_identity = "storage-id://service/original-version"

    class FakeResourceIdentity(ResourceIdentity):
        async def read_data(self, storage):
            pytest.fail("Catalog hashing must dispatch a serialized identity through the router")

        def to_string(self):
            return source_identity

    class FakeRouter:
        def storage_for_read(self, source):
            pytest.fail("A recorded resource identity must not be routed through its mutable source address")

    async def fake_download_resource_identity(identity, dst_url, *, overwrite, expected_hash=None):
        assert identity == source_identity
        assert overwrite is True
        assert expected_hash is None
        Path(get_path_from_url(dst_url)).write_bytes(original_bytes)
        return Result.OK

    class FakeTempFile:
        def __init__(self):
            self.path = str(tmp_path / "catalog-download.tmp")
            self.url = Path(self.path).as_uri()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

        def write_completed(self):
            pass

    class FakeTempFileManager:
        def create_temp_file(self):
            return FakeTempFile()

    monkeypatch.setattr(Router, "me", classmethod(lambda cls: FakeRouter()))
    monkeypatch.setattr(catalog_module, "wrapp_can_be_cached", lambda url: False)
    monkeypatch.setattr(catalog_module, "wrapp_download_resource_identity", fake_download_resource_identity)
    monkeypatch.setattr(
        catalog_module.TempFileManager,
        "me",
        classmethod(lambda cls: FakeTempFileManager()),
    )

    builder = CatalogBuilder(task_node=object())
    await builder._download_item(
        task_node=object(),
        base_url=AbsoluteUrl(absolute_url="https://content.example/"),
        url=AbsoluteUrl(absolute_url="https://content.example/asset.usd"),
        modified_time=datetime.now(timezone.utc),
        size=len(original_bytes),
        resource_identity=FakeResourceIdentity(),
    )

    expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(original_bytes))
    assert len(builder.result_list) == 1
    assert builder.result_list[0].resource_identity_v2 == source_identity
    assert builder.result_list[0].hash == expected_hash


@pytest.mark.parametrize(
    "relative_paths,expected",
    [
        ([], []),
        (["foo.psd", "bar.txt", "baz/qux.png"], []),
        (["foo.psd", "Foo.psd", "bar.txt"], ["foo.psd", "Foo.psd"]),
        (["foo.psd", "Foo.psd", "bar.txt", "Bar.txt", "unique.png"], ["foo.psd", "Foo.psd", "bar.txt", "Bar.txt"]),
        (
            ["SubUSDs/instances/.thumbs/lidar_link.usd.png", "SubUSDs/instances/.thumbs/Lidar_Link.usd.png"],
            ["SubUSDs/instances/.thumbs/lidar_link.usd.png", "SubUSDs/instances/.thumbs/Lidar_Link.usd.png"],
        ),
    ],
)
def test_check_case_insensitive_path_conflicts(relative_paths, expected):
    has_conflicts, paths = _make_catalog(relative_paths).check_case_insensitive_path_conflicts()
    assert has_conflicts is bool(expected)
    assert sorted(paths) == sorted(expected)


@pytest.fixture
def large_data_set_for_catalog(session_test_folder_url, storage_backend) -> str:
    parsed_url = urlparse(session_test_folder_url)
    scheme = parsed_url.scheme
    host = parsed_url.netloc
    source_url = f"{scheme}://{host}/NVIDIA/Materials/2023_1/Automotive/Carbon_Fiber/"
    try:
        exists = wrapp_url_exists_sync(source_url)
    except StorageOperationError:
        exists = False
    if not exists:
        pytest.skip(f"Large data set for catalog tests cannot be found on the server: {source_url}")
    return source_url


@pytest.mark.asyncio
@pytest.mark.skip("Skip this one for now as this now produces a parse error as a warning in the log only")
async def test_catalog_from_file_parse_error(local_tmp_folder, storage_backend):
    filelist = join_paths(local_tmp_folder, "filelist.txt")
    with open(filelist, "wt") as input:
        input.write(f"{local_tmp_folder}/ttest.txt")
    # This should fail as the line above can not be parsed.
    with pytest.raises(FailedCommand) as e:
        cat = await catalog(None, filelist)
    os.remove(filelist)


@pytest.mark.asyncio
async def test_catalog(cli_runner, test_folder_url, authenticator, local_tmp_folder, source_package, storage_backend):
    test_catalog_name = os.path.join(local_tmp_folder, "test-catalog.json")
    if await wrapp_url_exists(test_catalog_name):
        os.remove(test_catalog_name)
    params = ["catalog", test_folder_url, test_catalog_name]
    run_wrapp(cli_runner, params + ["--overwrite"])
    assert await wrapp_url_exists(test_catalog_name)
    with open(test_catalog_name) as jsonfile:
        data = json.load(jsonfile)
        data_items = data["items"]
        assert 4 == len(data_items), f"Expected 4 items, but got {[item['relative_path'] for item in data_items]}"
        assert any([item["relative_path"] == "source_package/testfile.txt" for item in data_items])

    # Fail running again as the file already exists
    assert await wrapp_url_exists(test_catalog_name)
    run_wrapp(cli_runner, params, 1)
    os.remove(test_catalog_name)

    # Rerun with stats
    time_params = copy.copy(params)
    time_params.append("--stats")
    Stats.get().clear()
    result = run_wrapp(cli_runner, time_params)
    assert "4 folders" in result.stdout
    assert ("2 files" in result.stdout) or ("4 files" in result.stdout)
    assert "52 Bytes" in result.stdout
    os.remove(test_catalog_name)

    # Rerun with time
    params.append("--time")
    result = run_wrapp(cli_runner, params)
    assert "elapsed" in result.stdout
    os.remove(test_catalog_name)


@pytest.mark.asyncio
async def test_catalog_from_file(cli_runner, test_folder_url, authenticator, local_tmp_folder, source_package):
    test_catalog_name = os.path.join(local_tmp_folder, "test-catalog.json")
    params = ["catalog", test_folder_url, test_catalog_name, "--overwrite"]
    run_wrapp(cli_runner, params)
    assert await wrapp_url_exists(test_catalog_name)
    test_input_name = os.path.join(local_tmp_folder, "input_file_list.csv")
    with open(test_catalog_name) as jsonfile:
        data = json.load(jsonfile)
        with open(test_input_name, "wt") as tab_separated_list:
            for item in data["items"]:
                tab_separated_list.write(f'{data["root"]}\t{item["relative_path"]}\n')

    # Catalog this
    test_catalog_from_file_name = os.path.join(local_tmp_folder, "test-catalog-from-file.json")
    run_wrapp(cli_runner, ["catalog", test_input_name, test_catalog_from_file_name, "--overwrite", "--file-list"])
    assert await wrapp_url_exists(test_catalog_from_file_name)

    # Diff the two catalogs
    result = run_wrapp(cli_runner, ["diff", test_catalog_name, test_catalog_from_file_name, "--show"])
    assert "No differences" in result.stdout

    os.remove(test_catalog_name)
    os.remove(test_catalog_from_file_name)


@pytest.mark.asyncio
async def test_catalog_tags(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    if not await wrapp_supports_tags(test_folder_url):
        pytest.skip("Tagging not supported on this storage backend")
    source_package = await storage_backend.create_source_package(test_folder_url, True)

    # Now catalog the source package including tags
    test_catalog_name = os.path.join(local_tmp_folder, "test-catalog.json")
    params = ["catalog", source_package, test_catalog_name, "--overwrite", "--tags"]
    run_wrapp(cli_runner, params)
    data = await Catalog.load(test_catalog_name)
    assert 3 == len(data.items)
    assert any([item.relative_path == "testfile.txt" for item in data.items])
    if await wrapp_supports_tags(test_folder_url):
        assert any([(item.tags[0].name if item.tags is not None else "") == "tagname" for item in data.items])
    os.remove(test_catalog_name)


@pytest.mark.asyncio
async def test_catalog_from_local(cli_runner, test_folder_url, local_tmp_folder, authenticator):
    test_data_path = os.path.join(os.path.dirname(__file__), "test_data/local_files")
    test_catalog_path = os.path.join(local_tmp_folder, "test_local_catalog.json")
    params = ["catalog", test_data_path, test_catalog_path, "--overwrite"]
    run_wrapp(cli_runner, params)
    with open(test_catalog_path) as jsonfile:
        data = json.load(jsonfile)
        data_items = data["items"]
        assert len(data_items) == 1
        assert any([item["relative_path"] == "example.json" for item in data_items])

    # Now write the example file and check the hash
    source = join_paths(test_data_path, "example.json")
    target = join_paths(test_folder_url, "example.json")
    result = await wrapp_copy_address(source, target)
    assert result == Result.OK
    result, uploaded_item = await wrapp_stat_and_calculate_hash_if_missing(target)
    assert result == Result.OK
    print(data_items)
    assert any(
        [item["relative_path"] == "example.json" and item["hash"] == uploaded_item.nucleus_hash for item in data_items]
    ), f"Failed to find item with name 'example.json' and hash {uploaded_item.nucleus_hash} in data_items={data_items}"


def test_catalog_mounts(cli_runner, test_folder_url, authenticator, local_tmp_folder, large_data_set_for_catalog):
    host = urlparse(test_folder_url).netloc
    source_url = large_data_set_for_catalog
    one_file = "Carbon_Fiber_ANI_01.mdl"
    test_data_path = os.path.join(local_tmp_folder, "test_mount_catalog.json")
    params = ["catalog", source_url, test_data_path, "--local-hash", "--overwrite"]
    run_wrapp(cli_runner, params)
    with open(test_data_path) as jsonfile:
        data = json.load(jsonfile)
        data_items = data["items"]
        assert any([item["relative_path"] == one_file for item in data_items])

    # Freeze that catalog to the server
    target_package = join_paths(test_folder_url, "freeze_target")
    run_wrapp(cli_runner, ["freeze", test_data_path, target_package, "--catalog"])
    assert wrapp_url_exists_sync(join_paths(target_package, one_file))

    # Create a package from that catalog
    package_name = "from_mount"
    assert wrapp_url_exists_sync(test_data_path)
    create_params = ["create", package_name, "1", test_data_path, "--catalog", "--repo", test_folder_url]
    run_wrapp(cli_runner, create_params)
    assert wrapp_url_exists_sync(join_paths(test_folder_url, ".packages", package_name))

    install_location = join_paths(test_folder_url, "installed_here")
    install_params = ["install", package_name, "1", install_location, "--repo", test_folder_url]
    run_wrapp(cli_runner, install_params)


@pytest.mark.asyncio
async def test_catalog_hash_cache(cli_runner, test_folder_url, authenticator, local_tmp_folder, large_data_set_for_catalog):
    test_data_path = os.path.join(local_tmp_folder, "test_mount_catalog.json")
    cachefile = join_paths(local_tmp_folder, f"hash-cache.{random_bytes()}.json")
    params = ["catalog", large_data_set_for_catalog, test_data_path, "--local-hash", "--overwrite", "--hash-cache-file", cachefile]
    Stats.get().clear()
    if await wrapp_url_exists(cachefile):
        os.remove(cachefile)
    run_wrapp(cli_runner, params)
    assert Stats.get().get_stat(StatType.HASH_CACHE_HIT) == 0

    catalog_1 = await Catalog.load(test_data_path)
    num_files_in_catalog = len(catalog_1.items)

    assert await wrapp_url_exists(cachefile)

    # Now load the cache file
    with open(cachefile) as f:
        cache = json.load(f)["entries"]
    # Make sure there is an entry for each file in the hashcache
    assert len(cache.keys()) == num_files_in_catalog

    # Now catalog again using the hash cache.
    Stats.get().clear()
    run_wrapp(cli_runner, params)
    assert Stats.get().get_stat(StatType.HASH_CACHE_HIT) == num_files_in_catalog

    # Verify the catalog is still the same
    catalog_2 = await Catalog.load(test_data_path)
    diff = Diff.diff_catalogs_internal(catalog_1, catalog_2, [])
    assert len(diff[0]) == 0 and len(diff[1]) == 0 and len(diff[2]) == 0, f"Catalog differs when created with populated hash cache: {diff}"

    os.remove(cachefile)


def test_catalog_without_hub(
    test_folder_url, authenticator, local_tmp_folder, client_cache_size_unchanged, large_data_set_for_catalog, storage_backend
):
    host = urlparse(test_folder_url).netloc
    source_url = large_data_set_for_catalog
    one_file = "Carbon_Fiber_ANI_01.mdl"
    test_catalog_name = os.path.join(local_tmp_folder, "test-catalog-no-hub.json")
    if wrapp_url_exists_sync(test_catalog_name):
        os.remove(test_catalog_name)
    params = ["catalog", source_url, test_catalog_name, "--local-hash"]
    with client_cache_size_unchanged(storage_backend.storage_type() == StorageBackendForTests.NUCLEUS):
        run_wrapp_without_hub(params)
    assert wrapp_url_exists_sync(test_catalog_name)
    with open(test_catalog_name) as jsonfile:
        data = json.load(jsonfile)
        data_items = data["items"]
        assert any([item["relative_path"] == one_file for item in data_items])


async def catalog_api_params_failures(fails: bool, root: str, file_list: None | str | List[str]):
    context = CommandParameters(verbose=True)
    if fails:
        with pytest.raises(FailedCommand) as failedCommand:
            result = await catalog(
                root_dir=root,
                file_list=file_list,
                context=context,
            )
    else:
        result = await catalog(
            root_dir=root,
            file_list=file_list,
            context=context,
        )


@pytest.mark.parametrize(
    "fails, has_root",
    [
        (True, False),
        (True, True),
    ],
)
@pytest.mark.asyncio
async def test_catalog_api_params_failures_no_list(fails: bool, has_root: bool, authenticator, request, test_folder_url):
    root = test_folder_url if has_root else None
    file_list = None
    await catalog_api_params_failures(fails, root, file_list)


@pytest.mark.parametrize(
    "fails, has_root",
    [
        (True, False),
        (False, True),
    ],
)
@pytest.mark.asyncio
async def test_catalog_api_params_failures_file_list_list(
    fails: bool, has_root: bool, authenticator, request, test_folder_url, file_list_list
):
    root = test_folder_url if has_root else None
    file_list = file_list_list
    await catalog_api_params_failures(fails, root, file_list)


@pytest.mark.parametrize(
    "fails, has_root",
    [
        (True, False),
        (False, True),
    ],
)
@pytest.mark.asyncio
async def test_catalog_api_params_failures_file_list_file(
    fails: bool, has_root: bool, authenticator, request, test_folder_url, file_list_file
):
    root = test_folder_url if has_root else None
    file_list = file_list_file
    await catalog_api_params_failures(fails, root, file_list)


def test_is_prefix_of_any_but_not_only_exact_match():
    # Simple unit test for the Catalog._is_prefix_of_any function
    sorted_paths = sorted({"f1/f1_1/X.txt", "f1/f1_1", "f2/f2_1/X.txt", "f2/f2_1", "f3/f3_1"})
    assert Catalog._is_prefix_of_any_but_not_only_exact_match("f1/", sorted_paths)
    assert Catalog._is_prefix_of_any_but_not_only_exact_match("f1/f1_1", sorted_paths)
    assert Catalog._is_prefix_of_any_but_not_only_exact_match("f2/f2_1", sorted_paths)
    assert Catalog._is_prefix_of_any_but_not_only_exact_match("f3", sorted_paths)
    assert Catalog._is_prefix_of_any_but_not_only_exact_match("f3/", sorted_paths)
    assert not Catalog._is_prefix_of_any_but_not_only_exact_match("f3/f1_1", sorted_paths)
    assert not Catalog._is_prefix_of_any_but_not_only_exact_match("f4/f1_1", sorted_paths)
    assert not Catalog._is_prefix_of_any_but_not_only_exact_match("f0", sorted_paths)
    assert not Catalog._is_prefix_of_any_but_not_only_exact_match("f1_1", sorted_paths)


def test_update_source_paths_rewrites_resource_identity_OMPE_92178():
    # Regression test for OMPE-92178: _update_source_paths must rewrite
    # resource_identity along with source_path. Otherwise mirror/import end up
    # with destination source_path entries but stale source-side
    # resource_identity strings, so later reads via read_resource_identity
    # resolve to the original location instead of the mirror/import target.
    source_catalog = Catalog(
        root="omniverse://source.example/pkg/1.0.0",
        items=[
            CatalogItem(
                source_path="omniverse://source.example/pkg/1.0.0/a.txt",
                relative_path="a.txt",
                hash="hash-a",
                size=1,
                resource_identity_v2="omniverse://source.example/pkg/1.0.0/a.txt?&1",
            ),
            CatalogItem(
                source_path="omniverse://source.example/pkg/1.0.0/sub/b.txt",
                relative_path="sub/b.txt",
                hash="hash-b",
                size=2,
                resource_identity_v2="omniverse://source.example/pkg/1.0.0/sub/b.txt?&2",
            ),
        ],
    )
    destination_catalog = Catalog(
        root="omniverse://dest.example/pkg/1.0.0",
        items=[
            CatalogItem(
                source_path="omniverse://dest.example/pkg/1.0.0/a.txt",
                relative_path="a.txt",
                hash="hash-a",
                size=1,
                resource_identity_v2="omniverse://dest.example/pkg/1.0.0/a.txt?&7",
            ),
            CatalogItem(
                source_path="omniverse://dest.example/pkg/1.0.0/sub/b.txt",
                relative_path="sub/b.txt",
                hash="hash-b",
                size=2,
                resource_identity_v2=None,
            ),
        ],
    )

    not_updated = source_catalog._update_source_paths(destination_catalog)

    assert not_updated == []
    items_by_path = {item.relative_path: item for item in source_catalog.items}
    assert items_by_path["a.txt"].source_path == "omniverse://dest.example/pkg/1.0.0/a.txt"
    assert items_by_path["a.txt"].resource_identity_v2 == "omniverse://dest.example/pkg/1.0.0/a.txt?&7"
    assert items_by_path["sub/b.txt"].source_path == "omniverse://dest.example/pkg/1.0.0/sub/b.txt"
    assert items_by_path["sub/b.txt"].resource_identity_v2 is None
