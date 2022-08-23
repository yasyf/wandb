import base64
import contextlib
import functools
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
    cast,
)
from urllib.parse import parse_qsl, quote, urlparse

import requests
import urllib3
from pkg_resources import parse_version

import wandb
import wandb.data_types as data_types
from wandb import env, util
from wandb.apis import InternalApi, PublicApi
from wandb.apis.public import Artifact as PublicArtifact
from wandb.errors import CommError
from wandb.errors.term import termlog, termwarn
from wandb.sdk.internal import progress

from . import lib as wandb_lib
from .data_types._dtypes import Type, TypeRegistry
from .interface.artifacts import Artifact as ArtifactInterface
from .interface.artifacts import (  # noqa: F401
    ArtifactEntry,
    ArtifactManifest,
    ArtifactsCache,
    StorageHandler,
    StorageLayout,
    StoragePolicy,
    b64_string_to_hex,
    get_artifacts_cache,
    md5_file_b64,
    md5_string,
)

if TYPE_CHECKING:
    import boto3  # type: ignore
    import google.cloud.storage as gcs_module  # type: ignore

    import wandb.filesync.step_prepare.StepPrepare as StepPrepare  # type: ignore

# This makes the first sleep 1s, and then doubles it up to total times,
# which makes for ~18 hours.
_REQUEST_RETRY_STRATEGY = urllib3.util.retry.Retry(
    backoff_factor=1,
    total=16,
    status_forcelist=(308, 408, 409, 429, 500, 502, 503, 504),
)

_REQUEST_POOL_CONNECTIONS = 64

_REQUEST_POOL_MAXSIZE = 64

ARTIFACT_TMP = tempfile.TemporaryDirectory("wandb-artifacts")


class _AddedObj:
    def __init__(self, entry: ArtifactEntry, obj: data_types.WBValue):
        self.entry = entry
        self.obj = obj


def _normalize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise TypeError(f"metadata must be dict, not {type(metadata)}")
    return cast(
        Dict[str, Any], json.loads(json.dumps(util.json_friendly_val(metadata)))
    )


class Artifact(ArtifactInterface):
    """
    Flexible and lightweight building block for dataset and model versioning.

    Constructs an empty artifact whose contents can be populated using its
    `add` family of functions. Once the artifact has all the desired files,
    you can call `wandb.log_artifact()` to log it.

    Arguments:
        name: (str) A human-readable name for this artifact, which is how you
            can identify this artifact in the UI or reference it in `use_artifact`
            calls. Names can contain letters, numbers, underscores, hyphens, and
            dots. The name must be unique across a project.
        type: (str) The type of the artifact, which is used to organize and differentiate
            artifacts. Common types include `dataset` or `model`, but you can use any string
            containing letters, numbers, underscores, hyphens, and dots.
        description: (str, optional) Free text that offers a description of the artifact. The
            description is markdown rendered in the UI, so this is a good place to place tables,
            links, etc.
        metadata: (dict, optional) Structured data associated with the artifact,
            for example class distribution of a dataset. This will eventually be queryable
            and plottable in the UI. There is a hard limit of 100 total keys.

    Examples:
        Basic usage
        ```
        wandb.init()

        artifact = wandb.Artifact('mnist', type='dataset')
        artifact.add_dir('mnist/')
        wandb.log_artifact(artifact)
        ```

    Raises:
        Exception: if problem.

    Returns:
        An `Artifact` object.
    """

    _added_objs: Dict[int, _AddedObj]
    _added_local_paths: Dict[str, ArtifactEntry]
    _distributed_id: Optional[str]
    _metadata: dict
    _logged_artifact: Optional[ArtifactInterface]
    _incremental: bool
    _client_id: str
    _manifest: ArtifactManifest

    def __init__(
        self,
        name: str,
        type: str,
        description: Optional[str] = None,
        metadata: Optional[dict] = None,
        incremental: Optional[bool] = None,
        use_as: Optional[str] = None,
        in_memory: bool = False,
    ) -> None:
        if not re.match(r"^[a-zA-Z0-9_\-.]+$", name):
            raise ValueError(
                "Artifact name may only contain alphanumeric characters, dashes, underscores, and dots. "
                'Invalid name: "%s"' % name
            )
        metadata = _normalize_metadata(metadata)
        # TODO: this shouldn't be a property of the artifact. It's a more like an
        # argument to log_artifact.
        storage_layout = StorageLayout.V2
        if env.get_use_v1_artifacts():
            storage_layout = StorageLayout.V1

        self._storage_policy = WandbStoragePolicy(
            config={
                "storageLayout": storage_layout,
                #  TODO: storage region
            }
        )
        self._client_id = util.generate_id(128)
        self._sequence_client_id = util.generate_id(128)
        self._api = InternalApi()
        self._final = False
        self._digest = ""
        if not in_memory:
            try:
                self._manifest = ArtifactManifestSQL(self, self._storage_policy)
            except ValueError:
                # Unsupported sqlite3 version, auto fallback to in_memory
                in_memory = True
        if in_memory:
            self._manifest = ArtifactManifestV1(self, self._storage_policy)
        self._cache = get_artifacts_cache()
        self._added_objs = {}
        self._added_local_paths = {}
        # You can write into this directory when creating artifact files
        self._artifact_dir = tempfile.TemporaryDirectory()
        self._type = type
        self._name = name
        self._description = description
        self._metadata = metadata
        self._distributed_id = None
        self._logged_artifact = None
        self._incremental = False
        self._cache.store_client_artifact(self)
        self._use_as = use_as

        if incremental:
            self._incremental = incremental
            wandb.termwarn("Using experimental arg `incremental`")

    @property
    def id(self) -> Optional[str]:
        if self._logged_artifact:
            return self._logged_artifact.id

        # The artifact hasn't been saved so an ID doesn't exist yet.
        return None

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def version(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.version

        raise ValueError(
            "Cannot call version on an artifact before it has been logged or in offline mode"
        )

    @property
    def entity(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.entity
        return self._api.settings("entity") or self._api.viewer().get("entity")  # type: ignore

    @property
    def project(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.project

        return self._api.settings("project")  # type: ignore

    @property
    def manifest(self) -> ArtifactManifest:
        if self._logged_artifact:
            return self._logged_artifact.manifest

        self.finalize()
        return self._manifest

    @property
    def digest(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.digest

        self.finalize()
        # Digest will be none if the artifact hasn't been saved yet.
        return self._digest

    @property
    def type(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.type

        return self._type

    @property
    def name(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.name

        return self._name

    @property
    def state(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.state

        return "PENDING"

    @property
    def size(self) -> int:
        if self._logged_artifact:
            return self._logged_artifact.size
        return self._manifest.size

    @property
    def commit_hash(self) -> str:
        if self._logged_artifact:
            return self._logged_artifact.commit_hash

        raise ValueError(
            "Cannot access commit_hash on an artifact before it has been logged or in offline mode"
        )

    @property
    def description(self) -> Optional[str]:
        if self._logged_artifact:
            return self._logged_artifact.description

        return self._description

    @description.setter
    def description(self, desc: Optional[str]) -> None:
        if self._logged_artifact:
            self._logged_artifact.description = desc
            return

        self._description = desc

    @property
    def metadata(self) -> dict:
        if self._logged_artifact:
            return self._logged_artifact.metadata

        return self._metadata

    @metadata.setter
    def metadata(self, metadata: dict) -> None:
        metadata = _normalize_metadata(metadata)
        if self._logged_artifact:
            self._logged_artifact.metadata = metadata
            return

        self._metadata = metadata

    @property
    def aliases(self) -> List[str]:
        if self._logged_artifact:
            return self._logged_artifact.aliases

        raise ValueError(
            "Cannot call aliases on an artifact before it has been logged or in offline mode"
        )

    @aliases.setter
    def aliases(self, aliases: List[str]) -> None:
        """
        Arguments:
            aliases: (list) The list of aliases associated with this artifact.
        """
        if self._logged_artifact:
            self._logged_artifact.aliases = aliases
            return

        raise ValueError(
            "Cannot set aliases on an artifact before it has been logged or in offline mode"
        )

    @property
    def use_as(self) -> Optional[str]:
        return self._use_as

    @property
    def distributed_id(self) -> Optional[str]:
        return self._distributed_id

    @distributed_id.setter
    def distributed_id(self, distributed_id: Optional[str]) -> None:
        self._distributed_id = distributed_id

    @property
    def incremental(self) -> bool:
        return self._incremental

    def used_by(self) -> List["wandb.apis.public.Run"]:
        if self._logged_artifact:
            return self._logged_artifact.used_by()

        raise ValueError(
            "Cannot call used_by on an artifact before it has been logged or in offline mode"
        )

    def logged_by(self) -> "wandb.apis.public.Run":
        if self._logged_artifact:
            return self._logged_artifact.logged_by()

        raise ValueError(
            "Cannot call logged_by on an artifact before it has been logged or in offline mode"
        )

    @contextlib.contextmanager
    def new_file(
        self, name: str, mode: str = "w", encoding: Optional[str] = None
    ) -> Generator[IO, None, None]:
        self._ensure_can_add()
        path = os.path.join(self._artifact_dir.name, name.lstrip("/"))
        if os.path.exists(path):
            raise ValueError(f'File with name "{name}" already exists at "{path}"')

        util.mkdir_exists_ok(os.path.dirname(path))
        try:
            with util.fsync_open(path, mode, encoding) as f:
                yield f
        except UnicodeEncodeError as e:
            wandb.termerror(
                f"Failed to open the provided file (UnicodeEncodeError: {e}). Please provide the proper encoding."
            )
            raise e
        self.add_file(path, name=name)

    def add_file(
        self,
        local_path: str,
        name: Optional[str] = None,
        is_tmp: Optional[bool] = False,
    ) -> ArtifactEntry:
        self._ensure_can_add()
        if not os.path.isfile(local_path):
            raise ValueError("Path is not a file: %s" % local_path)

        name = util.to_forward_slash_path(name or os.path.basename(local_path))
        digest = md5_file_b64(local_path)

        if is_tmp:
            file_path, file_name = os.path.split(name)
            file_name_parts = file_name.split(".")
            file_name_parts[0] = b64_string_to_hex(digest)[:20]
            name = os.path.join(file_path, ".".join(file_name_parts))

        return self._add_local_file(name, local_path, digest=digest)

    def add_dir(self, local_path: str, name: Optional[str] = None) -> None:
        self._ensure_can_add()
        if not os.path.isdir(local_path):
            raise ValueError("Path is not a directory: %s" % local_path)

        termlog(
            "Adding directory to artifact (%s)... "
            % os.path.join(".", os.path.normpath(local_path)),
            newline=False,
        )
        start_time = time.time()

        # TODO: maybe get this out memory / batch it into chunks
        paths = []
        for dirpath, _, filenames in os.walk(local_path, followlinks=True):
            for fname in filenames:
                physical_path = os.path.join(dirpath, fname)
                logical_path = os.path.relpath(physical_path, start=local_path)
                if name is not None:
                    logical_path = os.path.join(name, logical_path)
                paths.append((logical_path, physical_path))

        def add_manifest_file(log_phy_path: Tuple[str, str]) -> None:
            logical_path, physical_path = log_phy_path
            self._add_local_file(logical_path, physical_path)

        import multiprocessing.dummy  # this uses threads

        num_threads = 8
        pool = multiprocessing.dummy.Pool(num_threads)
        pool.map(add_manifest_file, paths)
        pool.close()
        pool.join()

        termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)

    def add_reference(
        self,
        uri: Union[ArtifactEntry, str],
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        self._ensure_can_add()
        if name is not None:
            name = util.to_forward_slash_path(name)

        # This is a bit of a hack, we want to check if the uri is a of the type
        # ArtifactEntry which is a private class returned by Artifact.get_path in
        # wandb/apis/public.py. If so, then recover the reference URL.
        uri_str: str
        if isinstance(uri, ArtifactEntry) and uri.parent_artifact() != self:
            ref_url_fn = uri.ref_url
            uri_str = ref_url_fn()
        elif isinstance(uri, str):
            uri_str = uri
        url = urlparse(str(uri_str))
        if not url.scheme:
            raise ValueError(
                "References must be URIs. To reference a local file, use file://"
            )

        manifest_entries = self._storage_policy.store_reference(
            self, uri_str, name=name, checksum=checksum, max_objects=max_objects
        )

        with self._manifest.transaction():
            for entry in manifest_entries:
                self._manifest.add_entry(entry)

        return self._manifest.entries.values()  # type: ignore[return-value]

    def add(self, obj: data_types.WBValue, name: str) -> ArtifactEntry:
        self._ensure_can_add()
        name = util.to_forward_slash_path(name)

        # This is a "hack" to automatically rename tables added to
        # the wandb /media/tables directory to their sha-based name.
        # TODO: figure out a more appropriate convention.
        is_tmp_name = name.startswith("media/tables")

        # Validate that the object is one of the correct wandb.Media types
        # TODO: move this to checking subclass of wandb.Media once all are
        # generally supported
        allowed_types = [
            data_types.Bokeh,
            data_types.JoinedTable,
            data_types.PartitionedTable,
            data_types.Table,
            data_types.Classes,
            data_types.ImageMask,
            data_types.BoundingBoxes2D,
            data_types.Audio,
            data_types.Image,
            data_types.Video,
            data_types.Html,
            data_types.Object3D,
            data_types.Molecule,
            data_types._SavedModel,
        ]

        if not any(isinstance(obj, t) for t in allowed_types):
            raise ValueError(
                "Found object of type {}, expected one of {}.".format(
                    obj.__class__, allowed_types
                )
            )

        obj_id = id(obj)
        if obj_id in self._added_objs:
            return self._added_objs[obj_id].entry

        # If the object is coming from another artifact, save it as a reference
        ref_path = obj._get_artifact_entry_ref_url()
        if ref_path is not None:
            return next(self.add_reference(ref_path, type(obj).with_suffix(name)))

        val = obj.to_json(self)
        name = obj.with_suffix(name)
        entry = self._manifest.get_entry_by_path(name)
        if entry is not None:
            return entry

        def do_write(f: IO) -> None:
            import json

            # TODO: Do we need to open with utf-8 codec?
            f.write(json.dumps(val, sort_keys=True))

        if is_tmp_name:
            file_path = os.path.join(ARTIFACT_TMP.name, str(id(self)), name)
            folder_path, _ = os.path.split(file_path)
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            with open(file_path, "w") as tmp_f:
                do_write(tmp_f)
        else:
            with self.new_file(name) as f:
                file_path = f.name
                do_write(f)

        # Note, we add the file from our temp directory.
        # It will be added again later on finalize, but succeed since
        # the checksum should match
        entry = self.add_file(file_path, name, is_tmp_name)
        self._added_objs[obj_id] = _AddedObj(entry, obj)
        if obj._artifact_target is None:
            obj._set_artifact_target(self, entry.path)

        if is_tmp_name:
            if os.path.exists(file_path):
                os.remove(file_path)

        return entry

    def get_path(self, name: str) -> ArtifactEntry:
        if self._logged_artifact:
            return self._logged_artifact.get_path(name)

        raise ValueError(
            "Cannot load paths from an artifact before it has been logged or in offline mode"
        )

    def get(self, name: str) -> data_types.WBValue:
        if self._logged_artifact:
            return self._logged_artifact.get(name)

        raise ValueError(
            "Cannot call get on an artifact before it has been logged or in offline mode"
        )

    def download(self, root: str = None, recursive: bool = False) -> str:
        if self._logged_artifact:
            return self._logged_artifact.download(root=root, recursive=recursive)

        raise ValueError(
            "Cannot call download on an artifact before it has been logged or in offline mode"
        )

    def checkout(self, root: Optional[str] = None) -> str:
        if self._logged_artifact:
            return self._logged_artifact.checkout(root=root)

        raise ValueError(
            "Cannot call checkout on an artifact before it has been logged or in offline mode"
        )

    def verify(self, root: Optional[str] = None) -> bool:
        if self._logged_artifact:
            return self._logged_artifact.verify(root=root)

        raise ValueError(
            "Cannot call verify on an artifact before it has been logged or in offline mode"
        )

    def save(
        self,
        project: Optional[str] = None,
        settings: Optional["wandb.wandb_sdk.wandb_settings.Settings"] = None,
    ) -> None:
        """
        Persists any changes made to the artifact. If currently in a run, that run will
        log this artifact. If not currently in a run, a run of type "auto" will be created
        to track this artifact.

        Arguments:
            project: (str, optional) A project to use for the artifact in the case that a run is not already in context
            settings: (wandb.Settings, optional) A settings object to use when initializing an
            automatic run. Most commonly used in testing harness.

        Returns:
            None
        """

        if self._incremental:
            with wandb_lib.telemetry.context() as tel:
                tel.feature.artifact_incremental = True

        if self._logged_artifact:
            return self._logged_artifact.save()
        else:
            if wandb.run is None:
                if settings is None:
                    settings = wandb.Settings(silent="true")
                with wandb.init(
                    project=project, job_type="auto", settings=settings
                ) as run:
                    # redoing this here because in this branch we know we didn't
                    # have the run at the beginning of the method
                    if self._incremental:
                        with wandb_lib.telemetry.context(run=run) as tel:
                            tel.feature.artifact_incremental = True
                    run.log_artifact(self)
            else:
                wandb.run.log_artifact(self)

    def delete(self) -> None:
        if self._logged_artifact:
            return self._logged_artifact.delete()

        raise ValueError(
            "Cannot call delete on an artifact before it has been logged or in offline mode"
        )

    def wait(self) -> ArtifactInterface:
        if self._logged_artifact:
            return self._logged_artifact.wait()

        raise ValueError(
            "Cannot call wait on an artifact before it has been logged or in offline mode"
        )

    def get_added_local_path_name(self, local_path: str) -> Optional[str]:
        """
        Get the artifact relative name of a file added by a local filesystem path.

        Arguments:
            local_path: (str) The local path to resolve into an artifact relative name.

        Returns:
            str: The artifact relative name.

        Examples:
            Basic usage
            ```
            artifact = wandb.Artifact('my_dataset', type='dataset')
            artifact.add_file('path/to/file.txt', name='artifact/path/file.txt')

            # Returns `artifact/path/file.txt`:
            name = artifact.get_added_local_path_name('path/to/file.txt')
            ```
        """
        entry = self._added_local_paths.get(local_path, None)
        if entry is None:
            return None
        return entry.path

    def finalize(self) -> Optional[str]:
        """
        Marks this artifact as final, which disallows further additions to the artifact.
        This happens automatically when calling `log_artifact`.


        Returns:
            A file path to the manifest file if one was created, otherwise None
        """
        if self._final:
            return self._manifest.path

        # mark final after all files are added
        self._final = True
        self._digest = self._manifest.digest()
        self._manifest.flush(close=True)
        return self._manifest.path

    def json_encode(self) -> Dict[str, Any]:
        if not self._logged_artifact:
            raise ValueError(
                "Cannot json encode artifact before it has been logged or in offline mode."
            )
        return util.artifact_to_json(self)

    def _ensure_can_add(self) -> None:
        if self._final:
            raise ValueError("Can't add to finalized artifact.")

    def _add_local_file(
        self, name: str, path: str, digest: Optional[str] = None
    ) -> ArtifactEntry:
        digest = digest or md5_file_b64(path)
        size = os.path.getsize(path)
        name = util.to_forward_slash_path(name)

        cache_path, hit, cache_open = self._cache.check_md5_obj_path(digest, size)
        if not hit:
            with cache_open() as f:
                shutil.copyfile(path, f.name)

        entry = ArtifactManifestEntry(
            name,
            None,
            digest=digest,
            size=size,
            local_path=cache_path,
        )

        self._manifest.add_entry(entry)
        self._added_local_paths[path] = entry
        return entry

    def __setitem__(self, name: str, item: data_types.WBValue) -> ArtifactEntry:
        return self.add(item, name)

    def __getitem__(self, name: str) -> Optional[data_types.WBValue]:
        return self.get(name)


class EntryToDictMapping(dict):
    def __init__(
        self,
        mapping: Mapping,
        map_fn: Callable[[ArtifactEntry], Dict[str, Union[str, int]]],
    ):
        self._mapping = mapping
        self._map_fn = map_fn

    def items(self) -> Iterator[Tuple[str, Dict]]:  # type: ignore[override]
        for path, entry in self._mapping.items():
            yield path, self._map_fn(entry)

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __bool__(self) -> bool:
        return len(self._mapping) > 0

    def __repr__(self) -> str:
        return "<ArtifactManifest iterator>"


class SQLArtifactEntryMapping(Mapping):
    def __init__(
        self,
        manifest: "ArtifactManifestSQL",
    ):
        self._manifest = manifest

    def map(self, fn: Callable[[ArtifactEntry], Dict]) -> Mapping[str, Dict]:
        return EntryToDictMapping(self, fn)

    def size(self) -> int:
        db = self._manifest._db()
        return int(db.execute("SELECT SUM(size) FROM manifest_entries").fetchone()[0])

    def items(self) -> Iterator[Tuple[str, ArtifactEntry]]:  # type: ignore[override]
        db = self._manifest._db()
        for row in db.execute("SELECT * FROM manifest_entries ORDER BY path"):
            yield row[0], self.row_to_entry(row)

    def values(self) -> Iterator[ArtifactEntry]:  # type: ignore[override]
        for _, e in self.items():
            yield e

    def row_to_entry(self, row: tuple) -> ArtifactEntry:
        row_list = list(row)
        # remove conflict
        row_list.pop()
        if row_list[5] is None:
            row_list[5] = {}
        else:
            row_list[5] = json.loads(row_list[5])
        entry = ArtifactManifestEntry(*row_list)
        return entry

    def digest(self) -> str:
        hasher = hashlib.md5()
        hasher.update(b"wandb-artifact-manifest-v1\n")
        for (name, entry) in self.items():
            hasher.update(f"{name}:{entry.digest}\n".encode())
        return hasher.hexdigest()

    def __getitem__(self, path: str) -> ArtifactEntry:
        db = self._manifest._db()
        res = db.execute(
            "SELECT * FROM manifest_entries WHERE path = ?", (path,)
        ).fetchone()
        if res is None:
            raise KeyError()
        return self.row_to_entry(res)

    def __len__(self) -> int:
        db = self._manifest._db()
        return int(db.execute("SELECT COUNT(*) FROM manifest_entries").fetchone()[0])

    def __iter__(self) -> Iterator[str]:
        db = self._manifest._db()
        for row in db.execute("SELECT path FROM manifest_entries ORDER BY path"):
            yield str(row[0])

    def __repr__(self) -> str:
        return f"<ArtifactManifest digest: {self.digest()}, entries: {len(self)}, bytes: {util.to_human_size(self.size())}>"


class ArtifactManifestSQL(ArtifactManifest):
    SCHEMA = """CREATE TABLE manifest_entries
                (path TEXT NOT NULL PRIMARY KEY, ref TEXT, digest TEXT NOT NULL,
                    birth_artifact_id TEXT, size INT, extra TEXT, local_path TEXT,
                    digest_conflict TEXT)"""

    @classmethod
    def version(cls) -> int:
        return 1

    def __init__(
        self,
        artifact: ArtifactInterface,
        storage_policy: "WandbStoragePolicy",
        entries: Optional[Mapping[str, ArtifactEntry]] = None,
        manifest_path: Optional[str] = None,
        max_buffer_size: int = 10000,
    ) -> None:
        super().__init__(artifact, storage_policy)
        if parse_version(sqlite3.sqlite_version) < parse_version("3.24.0"):
            raise ValueError(
                f"ArtifactManifestSQL requires sqlite >= 3.24.0, have sqlite = {sqlite3.sqlite_version}"
            )
        self._dir = tempfile.mkdtemp()
        self._connection_pool: Dict[int, sqlite3.Connection] = {}
        if artifact:
            self._uuid = artifact.client_id[:32]
        else:
            self._uuid = util.generate_id(32)
        if manifest_path:
            self._path = manifest_path
        else:
            self._path = os.path.join(self._dir, f"{self._uuid}.db")
        if not os.path.exists(self._path):
            setup = True
        self._entries_since_flush = 0
        self._max_buffer_size = max_buffer_size
        if setup:
            self._setup()
        self._mapping = SQLArtifactEntryMapping(self)
        if entries is not None:
            # TODO: we may be able to just get rid of .transaction() since we
            # have auto flushing
            with self.transaction():
                for entry in entries.values():
                    self.add_entry(entry)

    @functools.lru_cache()  # noqa: B019
    def _thread_safe_client(self, id: int) -> sqlite3.Connection:
        if self._connection_pool.get(id) is None:
            # There's risk of corruption if a connection is shared across threads
            # we create a separate connection per thread so we can disable this check
            client = sqlite3.connect(self._path, check_same_thread=False)
            self._connection_pool[id] = client
        return self._connection_pool[id]

    @contextlib.contextmanager
    def _db_commit(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            client = self._thread_safe_client(id(threading.current_thread()))
            yield client
        finally:
            client.commit()

    @property
    def path(self) -> str:
        return self._path

    @property
    def size(self) -> int:
        return self._mapping.size()

    def _db(
        self, cursor: bool = True
    ) -> Union[sqlite3.Connection, sqlite3.Cursor,]:
        """Get the current connection or cursor."""
        client = self._thread_safe_client(id(threading.current_thread()))
        return client.cursor() if cursor else client

    def _setup(self) -> None:
        with self._db_commit() as db:
            db.execute(self.SCHEMA)
            # With journal enabled we can rollback, but we must keep our transactions
            # reasonably small.  If the process crashes we risk corruption, but
            # resuming artifact creation isn't supported.  The following benchmarks
            # are for adding 2.5 million entries without any batching:
            # 166.3s (no sqlite), 246.5s (below pragmas), 1510.3 (default sqlite)
            db.execute("PRAGMA journal_mode = MEMORY")
            db.execute("PRAGMA synchronous = OFF")

    @property
    def entries(self) -> Mapping[str, "ArtifactEntry"]:
        return self._mapping

    def flush(self, close: bool = False) -> None:
        db = self._db(cursor=False)
        db.commit()  # type: ignore[union-attr]
        self._entries_since_flush = 0
        if close:
            for _, client in self._connection_pool.items():
                client.close()
            self._connection_pool = {}
            self._thread_safe_client.cache_clear()

    def cleanup(self) -> None:
        self.flush(True)
        super().cleanup()

    def add_entry(self, entry: "ArtifactEntry") -> None:
        try:
            db = self._db(cursor=False)
            # ON CONFLICT was added in 3.24.0 (2018-06-04), our constructor blows
            # up if a user attempts to use an earlier version.
            db.execute(
                "INSERT INTO manifest_entries VALUES(?, ?, ?, ?, ?, ?, ?, ?) \
                ON CONFLICT(path) DO UPDATE SET digest_conflict=excluded.digest",
                (
                    entry.path,
                    entry.ref,
                    entry.digest,
                    entry.birth_artifact_id,
                    entry.size,
                    json.dumps(entry.extra) if entry.extra != {} else None,
                    entry.local_path,
                    None,
                ),
            )
            # Unfortunately "RETURNING" wasn't introduced until 3.35.0 so we do
            # a select after the insert to check for conflicts
            conflict = db.execute(
                "SELECT path, digest, digest_conflict FROM manifest_entries WHERE path = ?",
                (entry.path,),
            ).fetchone()
            if conflict[2] is not None:
                if conflict[1] != conflict[2]:
                    raise ValueError("Cannot add the same path twice: %s" % conflict[0])
        finally:
            if (
                not self._in_transaction
                or self._entries_since_flush > self._max_buffer_size
            ):
                self._entries_since_flush = 0
                db.commit()  # type: ignore[union-attr]
            else:
                self._entries_since_flush += 1

    def get_entry_by_path(self, path: str) -> Optional["ArtifactEntry"]:
        return self._mapping.get(path)

    def get_entries_in_directory(self, directory: str) -> Iterator["ArtifactEntry"]:
        db = self._db()
        for row in db.execute(
            "SELECT * FROM manifest_entries WHERE path LIKE '?%'", (directory,)
        ):
            yield self._mapping.row_to_entry(row)

    def digest(self) -> str:
        return self._mapping.digest()

    def __repr__(self) -> str:
        return repr(self._mapping)


class ArtifactManifestV1(ArtifactManifest):
    @classmethod
    def version(cls) -> int:
        return 1

    def __init__(
        self,
        artifact: ArtifactInterface,
        storage_policy: "WandbStoragePolicy",
        entries: Optional[Mapping[str, ArtifactEntry]] = None,
        manifest_path: Optional[str] = None,
    ) -> None:
        super().__init__(artifact, storage_policy, entries=entries)

    def digest(self) -> str:
        hasher = hashlib.md5()
        hasher.update(b"wandb-artifact-manifest-v1\n")
        for (name, entry) in sorted(self.entries.items(), key=lambda kv: kv[0]):
            hasher.update(f"{name}:{entry.digest}\n".encode())
        return hasher.hexdigest()


class ArtifactManifestEntry(ArtifactEntry):
    def __init__(
        self,
        path: str,
        ref: Optional[str],
        digest: str,
        birth_artifact_id: Optional[str] = None,
        size: Optional[int] = None,
        extra: Dict = None,
        local_path: Optional[str] = None,
    ):
        if local_path is not None and size is None:
            raise AssertionError(
                "programming error, size required when local_path specified"
            )
        self.path = util.to_forward_slash_path(path)
        self.ref = ref  # This is None for files stored in the artifact.
        self.digest = digest
        self.birth_artifact_id = birth_artifact_id
        self.size = size
        self.extra = extra or {}
        # This is not stored in the manifest json, it's only used in the process
        # of saving
        self.local_path = local_path

    def ref_target(self) -> str:
        if self.ref is None:
            raise ValueError("Only reference entries support ref_target().")
        return self.ref

    def to_json(self) -> Dict[str, Union[int, str]]:
        json_dict = {}
        for key in ("digest", "ref", "birth_artifact_id", "size", "extra"):
            val = getattr(self, key)
            if key == "birth_artifact_id":
                key = "birthArtifactID"
            if val or val == 0:
                json_dict[key] = val
        return json_dict

    def __repr__(self) -> str:
        summary = "digest: %s, " % self.digest[:32]
        if self.ref is None:
            summary += f"name: {self.path}"
        else:
            summary += f"ref: {self.ref}"

        return f"<ManifestEntry {summary}, size: {util.to_human_size(self.size or 0)}>"


class WandbStoragePolicy(StoragePolicy):
    @classmethod
    def name(cls) -> str:
        return "wandb-storage-policy-v1"

    @classmethod
    def from_config(cls, config: Dict) -> "WandbStoragePolicy":
        return cls(config=config)

    def __init__(self, config: Dict = None) -> None:
        self._cache = get_artifacts_cache()
        self._config = config or {}
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=_REQUEST_RETRY_STRATEGY,
            pool_connections=_REQUEST_POOL_CONNECTIONS,
            pool_maxsize=_REQUEST_POOL_MAXSIZE,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

        s3 = S3Handler()
        gcs = GCSHandler()
        http = HTTPHandler(self._session)
        https = HTTPHandler(self._session, scheme="https")
        artifact = WBArtifactHandler()
        local_artifact = WBLocalArtifactHandler()
        file_handler = LocalFileHandler()

        self._api = InternalApi()
        self._handler = MultiHandler(
            handlers=[
                s3,
                gcs,
                http,
                https,
                artifact,
                local_artifact,
                file_handler,
            ],
            default_handler=TrackingHandler(),
        )

    def config(self) -> Dict:
        return self._config

    def load_file(
        self, artifact: ArtifactInterface, name: str, manifest_entry: ArtifactEntry
    ) -> str:
        path, hit, cache_open = self._cache.check_md5_obj_path(
            manifest_entry.digest,
            manifest_entry.size if manifest_entry.size is not None else 0,
        )
        if hit:
            return path

        response = self._session.get(
            self._file_url(self._api, artifact.entity, manifest_entry),
            auth=("api", self._api.api_key),
            stream=True,
        )
        response.raise_for_status()

        with cache_open(mode="wb") as file:
            for data in response.iter_content(chunk_size=16 * 1024):
                file.write(data)
        return path

    def store_reference(
        self,
        artifact: ArtifactInterface,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        return self._handler.store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )

    def load_reference(
        self,
        artifact: ArtifactInterface,
        name: str,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        return self._handler.load_path(artifact, manifest_entry, local)

    def _file_url(
        self, api: InternalApi, entity_name: str, manifest_entry: ArtifactEntry
    ) -> str:
        storage_layout = self._config.get("storageLayout", StorageLayout.V1)
        storage_region = self._config.get("storageRegion", "default")
        md5_hex = util.bytes_to_hex(base64.b64decode(manifest_entry.digest))

        if storage_layout == StorageLayout.V1:
            return "{}/artifacts/{}/{}".format(
                api.settings("base_url"), entity_name, md5_hex
            )
        elif storage_layout == StorageLayout.V2:
            return "{}/artifactsV2/{}/{}/{}/{}".format(
                api.settings("base_url"),
                storage_region,
                entity_name,
                quote(
                    manifest_entry.birth_artifact_id
                    if manifest_entry.birth_artifact_id is not None
                    else ""
                ),
                md5_hex,
            )
        else:
            raise Exception(f"unrecognized storage layout: {storage_layout}")

    def store_file(
        self,
        artifact_id: str,
        artifact_manifest_id: str,
        entry: ArtifactEntry,
        preparer: "StepPrepare",
        progress_callback: Optional["progress.ProgressFn"] = None,
    ) -> bool:
        # write-through cache
        cache_path, hit, cache_open = self._cache.check_md5_obj_path(
            entry.digest, entry.size if entry.size is not None else 0
        )
        if not hit and entry.local_path is not None:
            with cache_open() as f:
                shutil.copyfile(entry.local_path, f.name)
            entry.local_path = cache_path

        resp = preparer.prepare(
            lambda: {
                "artifactID": artifact_id,
                "artifactManifestID": artifact_manifest_id,
                "name": entry.path,
                "md5": entry.digest,
            }
        )

        entry.birth_artifact_id = resp.birth_artifact_id
        exists = resp.upload_url is None
        if not exists:
            if entry.local_path is not None:
                with open(entry.local_path, "rb") as file:
                    # This fails if we don't send the first byte before the signed URL
                    # expires.
                    self._api.upload_file_retry(
                        resp.upload_url,
                        file,
                        progress_callback,
                        extra_headers={
                            header.split(":", 1)[0]: header.split(":", 1)[1]
                            for header in (resp.upload_headers or {})
                        },
                    )
        return exists


# Don't use this yet!
class __S3BucketPolicy(StoragePolicy):
    @classmethod
    def name(cls) -> str:
        return "wandb-s3-bucket-policy-v1"

    @classmethod
    def from_config(cls, config: Dict[str, str]) -> "__S3BucketPolicy":
        if "bucket" not in config:
            raise ValueError("Bucket name not found in config")
        return cls(config["bucket"])

    def __init__(self, bucket: str) -> None:
        self._bucket = bucket
        s3 = S3Handler(bucket)
        local = LocalFileHandler()

        self._handler = MultiHandler(
            handlers=[
                s3,
                local,
            ],
            default_handler=TrackingHandler(),
        )

    def config(self) -> Dict[str, str]:
        return {"bucket": self._bucket}

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        return self._handler.load_path(artifact, manifest_entry, local=local)

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        return self._handler.store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )


class MultiHandler(StorageHandler):
    _handlers: Dict[str, StorageHandler]

    def __init__(
        self,
        handlers: Optional[List[StorageHandler]] = None,
        default_handler: Optional[StorageHandler] = None,
    ) -> None:
        self._handlers = {}
        self._default_handler = default_handler

        handlers = handlers or []
        for handler in handlers:
            self._handlers[handler.scheme] = handler

    @property
    def scheme(self) -> str:
        raise NotImplementedError()

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        url = urlparse(manifest_entry.ref)
        if url.scheme not in self._handlers:
            if self._default_handler is not None:
                return self._default_handler.load_path(
                    artifact, manifest_entry, local=local
                )
            raise ValueError(
                'No storage handler registered for scheme "%s"' % str(url.scheme)
            )
        return self._handlers[str(url.scheme)].load_path(
            artifact, manifest_entry, local=local
        )

    def store_path(
        self,
        artifact: ArtifactInterface,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        url = urlparse(path)
        if url.scheme not in self._handlers:
            if self._default_handler is not None:
                return self._default_handler.store_path(
                    artifact,
                    path,
                    name=name,
                    checksum=checksum,
                    max_objects=max_objects,
                )
            raise ValueError(
                'No storage handler registered for scheme "%s"' % url.scheme
            )
        handler: StorageHandler
        handler = self._handlers[url.scheme]
        return handler.store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )


class TrackingHandler(StorageHandler):
    def __init__(self, scheme: Optional[str] = None) -> None:
        """
        Tracks paths as is, with no modification or special processing. Useful
        when paths being tracked are on file systems mounted at a standardized
        location.

        For example, if the data to track is located on an NFS share mounted on
        `/data`, then it is sufficient to just track the paths.
        """
        self._scheme = scheme or ""

    @property
    def scheme(self) -> str:
        return self._scheme

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        if local:
            # Likely a user error. The tracking handler is
            # oblivious to the underlying paths, so it has
            # no way of actually loading it.
            url = urlparse(manifest_entry.ref)
            raise ValueError(
                "Cannot download file at path %s, scheme %s not recognized"
                % (str(manifest_entry.ref), str(url.scheme))
            )
        return manifest_entry.path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        url = urlparse(path)
        if name is None:
            raise ValueError(
                'You must pass name="<entry_name>" when tracking references with unknown schemes. ref: %s'
                % path
            )
        termwarn(
            "Artifact references with unsupported schemes cannot be checksummed: %s"
            % path
        )
        name = name or url.path[1:]  # strip leading slash
        return iter([ArtifactManifestEntry(name, path, digest=path)])


DEFAULT_MAX_OBJECTS = 10000


class LocalFileHandler(StorageHandler):
    """Handles file:// references"""

    def __init__(self, scheme: Optional[str] = None) -> None:
        """
        Tracks files or directories on a local filesystem. Directories
        are expanded to create an entry for each file contained within.
        """
        self._scheme = scheme or "file"
        self._cache = get_artifacts_cache()

    @property
    def scheme(self) -> str:
        return self._scheme

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        url = urlparse(manifest_entry.ref)
        local_path = f"{str(url.netloc)}{str(url.path)}"
        if not os.path.exists(local_path):
            raise ValueError(
                "Local file reference: Failed to find file at path %s" % local_path
            )

        path, hit, cache_open = self._cache.check_md5_obj_path(
            manifest_entry.digest,
            manifest_entry.size if manifest_entry.size is not None else 0,
        )
        if hit:
            return path

        md5 = md5_file_b64(local_path)
        if md5 != manifest_entry.digest:
            raise ValueError(
                "Local file reference: Digest mismatch for path %s: expected %s but found %s"
                % (local_path, manifest_entry.digest, md5)
            )

        util.mkdir_exists_ok(os.path.dirname(path))

        with cache_open() as f:
            shutil.copy(local_path, f.name)
        return path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        url = urlparse(path)
        local_path = f"{url.netloc}{url.path}"
        max_objects = max_objects or DEFAULT_MAX_OBJECTS
        # We have a single file or directory
        # Note, we follow symlinks for files contained within the directory

        def md5(path: str) -> str:
            return (
                md5_file_b64(path)
                if checksum
                else md5_string(str(os.stat(path).st_size))
            )

        if os.path.isdir(local_path):
            i = 0
            start_time = time.time()
            if checksum:
                termlog(
                    'Generating checksum for up to %i files in "%s"...\n'
                    % (max_objects, local_path),
                    newline=False,
                )
            for root, _, files in os.walk(local_path):
                for sub_path in files:
                    i += 1
                    if i >= max_objects:
                        raise ValueError(
                            "Exceeded %i objects tracked, pass max_objects to add_reference"
                            % max_objects
                        )
                    physical_path = os.path.join(root, sub_path)
                    logical_path = os.path.relpath(physical_path, start=local_path)
                    if name is not None:
                        logical_path = os.path.join(name, logical_path)

                    entry = ArtifactManifestEntry(
                        logical_path,
                        os.path.join(path, logical_path),
                        size=os.path.getsize(physical_path),
                        digest=md5(physical_path),
                    )
                    yield entry
            if checksum:
                termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        elif os.path.isfile(local_path):
            name = name or os.path.basename(local_path)
            entry = ArtifactManifestEntry(
                name,
                path,
                size=os.path.getsize(local_path),
                digest=md5(local_path),
            )
            yield entry
        else:
            # TODO: update error message if we don't allow directories.
            raise ValueError('Path "%s" must be a valid file or directory path' % path)


class S3Handler(StorageHandler):
    _s3: Optional["boto3.resources.base.ServiceResource"]
    _scheme: str
    _versioning_enabled: Optional[bool]

    def __init__(self, scheme: Optional[str] = None) -> None:
        self._scheme = scheme or "s3"
        self._s3 = None
        self._versioning_enabled = None
        self._cache = get_artifacts_cache()

    @property
    def scheme(self) -> str:
        return self._scheme

    def init_boto(self) -> "boto3.resources.base.ServiceResource":
        if self._s3 is not None:
            return self._s3
        boto3 = util.get_module(
            "boto3",
            required="s3:// references requires the boto3 library, run pip install wandb[aws]",
        )
        self._s3 = boto3.session.Session().resource(
            "s3",
            endpoint_url=os.getenv("AWS_S3_ENDPOINT_URL"),
            region_name=os.getenv("AWS_REGION"),
        )
        self._botocore = util.get_module("botocore")
        return self._s3

    def _parse_uri(self, uri: str) -> Tuple[str, str, Optional[str]]:
        url = urlparse(uri)
        query = dict(parse_qsl(url.query))

        bucket = url.netloc
        key = url.path[1:]  # strip leading slash
        version = query.get("versionId")

        return bucket, key, version

    def versioning_enabled(self, bucket: str) -> bool:
        self.init_boto()
        assert self._s3 is not None  # mypy: unwraps optionality
        if self._versioning_enabled is not None:
            return self._versioning_enabled
        res = self._s3.BucketVersioning(bucket)
        self._versioning_enabled = res.status == "Enabled"
        return self._versioning_enabled

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        if not local:
            assert manifest_entry.ref is not None
            return manifest_entry.ref

        path, hit, cache_open = self._cache.check_etag_obj_path(
            manifest_entry.digest,
            manifest_entry.size if manifest_entry.size is not None else 0,
        )
        if hit:
            return path

        self.init_boto()
        assert self._s3 is not None  # mypy: unwraps optionality
        assert manifest_entry.ref is not None
        bucket, key, _ = self._parse_uri(manifest_entry.ref)
        version = manifest_entry.extra.get("versionID")

        extra_args = {}
        if version is None:
            # We don't have version information so just get the latest version
            # and fallback to listing all versions if we don't have a match.
            obj = self._s3.Object(bucket, key)
            etag = self._etag_from_obj(obj)
            if etag != manifest_entry.digest:
                if self.versioning_enabled(bucket):
                    # Fallback to listing versions
                    obj = None
                    object_versions = self._s3.Bucket(bucket).object_versions.filter(
                        Prefix=key
                    )
                    for object_version in object_versions:
                        if (
                            manifest_entry.extra.get("etag")
                            == object_version.e_tag[1:-1]
                        ):
                            obj = object_version.Object()
                            extra_args["VersionId"] = object_version.version_id
                            break
                    if obj is None:
                        raise ValueError(
                            "Couldn't find object version for %s/%s matching etag %s"
                            % (bucket, key, manifest_entry.extra.get("etag"))
                        )
                else:
                    raise ValueError(
                        "Digest mismatch for object %s: expected %s but found %s"
                        % (manifest_entry.ref, manifest_entry.digest, etag)
                    )
        else:
            obj = self._s3.ObjectVersion(bucket, key, version).Object()
            extra_args["VersionId"] = version

        with cache_open(mode="wb") as f:
            obj.download_fileobj(f, ExtraArgs=extra_args)
        return path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        self.init_boto()
        assert self._s3 is not None  # mypy: unwraps optionality

        # The passed in path might have query string parameters.
        # We only need to care about a subset, like version, when
        # parsing. Once we have that, we can store the rest of the
        # metadata in the artifact entry itself.
        bucket, key, version = self._parse_uri(path)
        path = f"{self.scheme}://{bucket}/{key}"
        if not self.versioning_enabled(bucket) and version:
            raise ValueError(
                f"Specifying a versionId is not valid for s3://{bucket} as it does not have versioning enabled."
            )

        max_objects = max_objects or DEFAULT_MAX_OBJECTS
        if not checksum:
            yield ArtifactManifestEntry(name or key, path, digest=path)
            return

        # If an explicit version is specified, use that. Otherwise, use the head version.
        objs = (
            [self._s3.ObjectVersion(bucket, key, version).Object()]
            if version
            else [self._s3.Object(bucket, key)]
        )
        start_time = None
        multi = False
        try:
            objs[0].load()
            # S3 doesn't have real folders, however there are cases where the folder key has a valid file which will not
            # trigger a recursive upload.
            # we should check the object's metadata says it is a directory and do a multi file upload if it is
            if "x-directory" in objs[0].content_type:
                multi = True
        except self._botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                multi = True
            else:
                raise CommError(
                    "Unable to connect to S3 (%s): %s"
                    % (e.response["Error"]["Code"], e.response["Error"]["Message"])
                )
        if multi:
            start_time = time.time()
            termlog(
                'Generating checksum for up to %i objects with prefix "%s"... '
                % (max_objects, key),
                newline=False,
            )
            objs = self._s3.Bucket(bucket).objects.filter(Prefix=key).limit(max_objects)

        entries = 0
        for obj in objs:
            if self._size_from_obj(obj) > 0:
                entries += 1
                yield self._entry_from_obj(obj, path, name, prefix=key, multi=multi)

        if start_time is not None:
            termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        if entries >= max_objects:
            raise ValueError(
                "Exceeded %i objects tracked, pass max_objects to add_reference"
                % max_objects
            )

    def _size_from_obj(self, obj: "boto3.s3.Object") -> int:
        # ObjectSummary has size, Object has content_length
        size: int
        if hasattr(obj, "size"):
            size = obj.size
        else:
            size = obj.content_length
        return size

    def _entry_from_obj(
        self,
        obj: "boto3.s3.Object",
        path: str,
        name: str = None,
        prefix: str = "",
        multi: bool = False,
    ) -> ArtifactManifestEntry:
        """
        Arguments:
            obj: The S3 object
            path: The S3-style path (e.g.: "s3://bucket/file.txt")
            name: The user assigned name, or None if not specified
            prefix: The prefix to add (will be the same as `path` for directories)
            multi: Whether or not this is a multi-object add
        """
        bucket, key, _ = self._parse_uri(path)

        # Always use posix paths, since that's what S3 uses.
        posix_key = pathlib.PurePosixPath(obj.key)  # the bucket key
        posix_path = pathlib.PurePosixPath(bucket) / pathlib.PurePosixPath(
            key
        )  # the path, with the scheme stripped
        posix_prefix = pathlib.PurePosixPath(prefix)  # the prefix, if adding a prefix
        posix_name = pathlib.PurePosixPath(name or "")
        posix_ref = posix_path

        if name is None:
            # We're adding a directory (prefix), so calculate a relative path.
            if str(posix_prefix) in str(posix_key) and posix_prefix != posix_key:
                posix_name = posix_key.relative_to(posix_prefix)
                posix_ref = posix_path / posix_name
            else:
                posix_name = pathlib.PurePosixPath(posix_key.name)
                posix_ref = posix_path
        elif multi:
            # We're adding a directory with a name override.
            relpath = posix_key.relative_to(posix_prefix)
            posix_name = posix_name / relpath
            posix_ref = posix_path / relpath
        return ArtifactManifestEntry(
            str(posix_name),
            f"{self.scheme}://{str(posix_ref)}",
            self._etag_from_obj(obj),
            size=self._size_from_obj(obj),
            extra=self._extra_from_obj(obj),
        )

    @staticmethod
    def _etag_from_obj(obj: "boto3.s3.Object") -> str:
        etag: str
        etag = obj.e_tag[1:-1]  # escape leading and trailing quote
        return etag

    @staticmethod
    def _extra_from_obj(obj: "boto3.s3.Object") -> Dict[str, str]:
        extra = {
            "etag": obj.e_tag[1:-1],  # escape leading and trailing quote
        }
        # ObjectSummary will never have version_id
        if hasattr(obj, "version_id") and obj.version_id != "null":
            extra["versionID"] = obj.version_id
        return extra

    @staticmethod
    def _content_addressed_path(md5: str) -> str:
        # TODO: is this the structure we want? not at all human
        # readable, but that's probably OK. don't want people
        # poking around in the bucket
        return "wandb/%s" % base64.b64encode(md5.encode("ascii")).decode("ascii")


class GCSHandler(StorageHandler):
    _client: Optional["gcs_module.client.Client"]
    _versioning_enabled: Optional[bool]

    def __init__(self, scheme: Optional[str] = None) -> None:
        self._scheme = scheme or "gs"
        self._client = None
        self._versioning_enabled = None
        self._cache = get_artifacts_cache()

    def versioning_enabled(self, bucket_path: str) -> bool:
        if self._versioning_enabled is not None:
            return self._versioning_enabled
        self.init_gcs()
        assert self._client is not None  # mypy: unwraps optionality
        bucket = self._client.bucket(bucket_path)
        bucket.reload()
        self._versioning_enabled = bucket.versioning_enabled
        return self._versioning_enabled

    @property
    def scheme(self) -> str:
        return self._scheme

    def init_gcs(self) -> "gcs_module.client.Client":
        if self._client is not None:
            return self._client
        storage = util.get_module(
            "google.cloud.storage",
            required="gs:// references requires the google-cloud-storage library, run pip install wandb[gcp]",
        )
        self._client = storage.Client()
        return self._client

    def _parse_uri(self, uri: str) -> Tuple[str, str, Optional[str]]:
        url = urlparse(uri)
        bucket = url.netloc
        key = url.path[1:]
        version = url.fragment if url.fragment else None
        return bucket, key, version

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        if not local:
            assert manifest_entry.ref is not None
            return manifest_entry.ref

        path, hit, cache_open = self._cache.check_md5_obj_path(
            manifest_entry.digest,
            manifest_entry.size if manifest_entry.size is not None else 0,
        )
        if hit:
            return path

        self.init_gcs()
        assert self._client is not None  # mypy: unwraps optionality
        assert manifest_entry.ref is not None
        bucket, key, _ = self._parse_uri(manifest_entry.ref)
        version = manifest_entry.extra.get("versionID")

        obj = None
        # First attempt to get the generation specified, this will return None if versioning is not enabled
        if version is not None:
            obj = self._client.bucket(bucket).get_blob(key, generation=version)

        if obj is None:
            # Object versioning is disabled on the bucket, so just get
            # the latest version and make sure the MD5 matches.
            obj = self._client.bucket(bucket).get_blob(key)
            if obj is None:
                raise ValueError(
                    "Unable to download object %s with generation %s"
                    % (manifest_entry.ref, version)
                )
            md5 = obj.md5_hash
            if md5 != manifest_entry.digest:
                raise ValueError(
                    "Digest mismatch for object %s: expected %s but found %s"
                    % (manifest_entry.ref, manifest_entry.digest, md5)
                )

        with cache_open(mode="wb") as f:
            obj.download_to_file(f)
        return path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        self.init_gcs()
        assert self._client is not None  # mypy: unwraps optionality

        # After parsing any query params / fragments for additional context,
        # such as version identifiers, pare down the path to just the bucket
        # and key.
        bucket, key, version = self._parse_uri(path)
        path = f"{self.scheme}://{bucket}/{key}"
        max_objects = max_objects or DEFAULT_MAX_OBJECTS
        if not self.versioning_enabled(bucket) and version:
            raise ValueError(
                f"Specifying a versionId is not valid for s3://{bucket} as it does not have versioning enabled."
            )

        if not checksum:
            return [ArtifactManifestEntry(name or key, path, digest=path)]

        start_time = None
        obj = self._client.bucket(bucket).get_blob(key, generation=version)
        multi = obj is None
        if multi:
            start_time = time.time()
            termlog(
                'Generating checksum for up to %i objects with prefix "%s"... '
                % (max_objects, key),
                newline=False,
            )
            objects = self._client.bucket(bucket).list_blobs(
                prefix=key, max_results=max_objects
            )
        else:
            objects = [obj]

        entries = 0
        for obj in objects:
            entries += 1
            yield self._entry_from_obj(obj, path, name, prefix=key, multi=multi)

        if start_time is not None:
            termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        if entries >= max_objects:
            raise ValueError(
                "Exceeded %i objects tracked, pass max_objects to add_reference"
                % max_objects
            )

    def _entry_from_obj(
        self,
        obj: "gcs_module.blob.Blob",
        path: str,
        name: Optional[str] = None,
        prefix: str = "",
        multi: bool = False,
    ) -> ArtifactManifestEntry:
        """
        Arguments:
            obj: The GCS object
            path: The GCS-style path (e.g.: "gs://bucket/file.txt")
            name: The user assigned name, or None if not specified
            prefix: The prefix to add (will be the same as `path` for directories)
            multi: Whether or not this is a multi-object add
        """
        bucket, key, _ = self._parse_uri(path)

        # Always use posix paths, since that's what S3 uses.
        posix_key = pathlib.PurePosixPath(obj.name)  # the bucket key
        posix_path = pathlib.PurePosixPath(bucket) / pathlib.PurePosixPath(
            key
        )  # the path, with the scheme stripped
        posix_prefix = pathlib.PurePosixPath(prefix)  # the prefix, if adding a prefix
        posix_name = pathlib.PurePosixPath(name or "")
        posix_ref = posix_path

        if name is None:
            # We're adding a directory (prefix), so calculate a relative path.
            if str(posix_prefix) in str(posix_key) and posix_prefix != posix_key:
                posix_name = posix_key.relative_to(posix_prefix)
                posix_ref = posix_path / posix_name
            else:
                posix_name = pathlib.PurePosixPath(posix_key.name)
                posix_ref = posix_path
        elif multi:
            # We're adding a directory with a name override.
            relpath = posix_key.relative_to(posix_prefix)
            posix_name = posix_name / relpath
            posix_ref = posix_path / relpath
        return ArtifactManifestEntry(
            str(posix_name),
            f"{self.scheme}://{str(posix_ref)}",
            obj.md5_hash,
            size=obj.size,
            extra=self._extra_from_obj(obj),
        )

    @staticmethod
    def _extra_from_obj(obj: "gcs_module.blob.Blob") -> Dict[str, str]:
        return {
            "etag": obj.etag,
            "versionID": obj.generation,
        }

    @staticmethod
    def _content_addressed_path(md5: str) -> str:
        # TODO: is this the structure we want? not at all human
        # readable, but that's probably OK. don't want people
        # poking around in the bucket
        return "wandb/%s" % base64.b64encode(md5.encode("ascii")).decode("ascii")


class HTTPHandler(StorageHandler):
    def __init__(self, session: requests.Session, scheme: Optional[str] = None) -> None:
        self._scheme = scheme or "http"
        self._cache = get_artifacts_cache()
        self._session = session

    @property
    def scheme(self) -> str:
        return self._scheme

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        if not local:
            assert manifest_entry.ref is not None
            return manifest_entry.ref

        path, hit, cache_open = self._cache.check_etag_obj_path(
            manifest_entry.digest,
            manifest_entry.size if manifest_entry.size is not None else 0,
        )
        if hit:
            return path

        assert manifest_entry.ref is not None
        response = self._session.get(manifest_entry.ref, stream=True)
        response.raise_for_status()

        digest, size, extra = self._entry_from_headers(response.headers)
        digest = digest or path
        if manifest_entry.digest != digest:
            raise ValueError(
                "Digest mismatch for url %s: expected %s but found %s"
                % (manifest_entry.ref, manifest_entry.digest, digest)
            )

        with cache_open(mode="wb") as file:
            for data in response.iter_content(chunk_size=16 * 1024):
                file.write(data)
        return path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        name = name or os.path.basename(path)
        if not checksum:
            return iter([ArtifactManifestEntry(name, path, digest=path)])

        with self._session.get(path, stream=True) as response:
            response.raise_for_status()
            digest, size, extra = self._entry_from_headers(response.headers)
            digest = digest or path
        return iter(
            [ArtifactManifestEntry(name, path, digest=digest, size=size, extra=extra)]
        )

    def _entry_from_headers(
        self, headers: requests.structures.CaseInsensitiveDict
    ) -> Tuple[Optional[str], Optional[int], Dict[str, str]]:
        response_headers = {k.lower(): v for k, v in headers.items()}
        size = None
        if response_headers.get("content-length", None):
            size = int(response_headers["content-length"])

        digest = response_headers.get("etag", None)
        extra = {}
        if digest:
            extra["etag"] = digest
        if digest and digest[:1] == '"' and digest[-1:] == '"':
            digest = digest[1:-1]  # trim leading and trailing quotes around etag
        return digest, size, extra


class WBArtifactHandler(StorageHandler):
    """Handles loading and storing Artifact reference-type files"""

    _client: Optional[PublicApi]

    def __init__(self) -> None:
        self._scheme = "wandb-artifact"
        self._cache = get_artifacts_cache()
        self._client = None

    @property
    def scheme(self) -> str:
        """overrides parent scheme

        Returns:
            (str): The scheme to which this handler applies.
        """
        return self._scheme

    @property
    def client(self) -> PublicApi:
        if self._client is None:
            self._client = PublicApi()
        return self._client

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        """
        Loads the file within the specified artifact given its
        corresponding entry. In this case, the referenced artifact is downloaded
        and a new symlink is created and returned to the caller.

        Arguments:
            manifest_entry (ArtifactManifestEntry): The index entry to load

        Returns:
            (os.PathLike): A path to the file represented by `index_entry`
        """
        # We don't check for cache hits here. Since we have 0 for size (since this
        # is a cross-artifact reference which and we've made the choice to store 0
        # in the size field), we can't confirm if the file is complete. So we just
        # rely on the dep_artifact entry's download() method to do its own cache
        # check.

        # Parse the reference path and download the artifact if needed
        artifact_id = util.host_from_path(manifest_entry.ref)
        artifact_file_path = util.uri_from_path(manifest_entry.ref)

        dep_artifact = PublicArtifact.from_id(
            util.hex_to_b64_id(artifact_id), self.client
        )
        link_target_path: str
        if local:
            link_target_path = dep_artifact.get_path(artifact_file_path).download()
        else:
            link_target_path = dep_artifact.get_path(artifact_file_path).ref_target()

        return link_target_path

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        """
        Stores the file or directory at the given path within the specified artifact. In this
        case we recursively resolve the reference until the result is a concrete asset so that
        we don't have multiple hops. TODO-This resolution could be done in the server for
        performance improvements.

        Arguments:
            artifact: The artifact doing the storing
            path (str): The path to store
            name (str): If specified, the logical name that should map to `path`

        Returns:
            (list[ArtifactManifestEntry]): A list of manifest entries to store within the artifact
        """

        # Recursively resolve the reference until a concrete asset is found
        while path is not None and urlparse(path).scheme == self._scheme:
            artifact_id = util.host_from_path(path)
            artifact_file_path = util.uri_from_path(path)
            target_artifact = PublicArtifact.from_id(
                util.hex_to_b64_id(artifact_id), self.client
            )

            # this should only have an effect if the user added the reference by url
            # string directly (in other words they did not already load the artifact into ram.)
            target_artifact._load_manifest()

            entry = target_artifact._manifest.get_entry_by_path(artifact_file_path)
            path = entry.ref

        # Create the path reference
        path = "{}://{}/{}".format(
            self._scheme, util.b64_to_hex_id(target_artifact.id), artifact_file_path
        )

        # Return the new entry
        return iter(
            [
                ArtifactManifestEntry(
                    name or os.path.basename(path),
                    path,
                    size=0,
                    digest=entry.digest,
                )
            ]
        )


class WBLocalArtifactHandler(StorageHandler):
    """Handles loading and storing Artifact reference-type files"""

    _client: Optional[PublicApi]

    def __init__(self) -> None:
        self._scheme = "wandb-client-artifact"
        self._cache = get_artifacts_cache()

    @property
    def scheme(self) -> str:
        """overrides parent scheme

        Returns:
            (str): The scheme to which this handler applies.
        """
        return self._scheme

    def load_path(
        self,
        artifact: ArtifactInterface,
        manifest_entry: ArtifactEntry,
        local: bool = False,
    ) -> str:
        raise NotImplementedError(
            "Should not be loading a path for an artifact entry with unresolved client id."
        )

    def store_path(
        self,
        artifact: Artifact,
        path: str,
        name: Optional[str] = None,
        checksum: bool = True,
        max_objects: Optional[int] = None,
    ) -> Iterator[ArtifactEntry]:
        """
        Stores the file or directory at the given path within the specified artifact.

        Arguments:
            artifact: The artifact doing the storing
            path (str): The path to store
            name (str): If specified, the logical name that should map to `path`

        Returns:
            (list[ArtifactManifestEntry]): A list of manifest entries to store within the artifact
        """
        client_id = util.host_from_path(path)
        target_path = util.uri_from_path(path)
        target_artifact = self._cache.get_client_artifact(client_id)
        if target_artifact is None:
            raise RuntimeError("Local Artifact not found - invalid reference")
        target_entry = target_artifact._manifest.entries[target_path]
        if target_entry is None:
            raise RuntimeError("Local entry not found - invalid reference")

        # Return the new entry
        return iter(
            [
                ArtifactManifestEntry(
                    name or os.path.basename(path),
                    path,
                    size=0,
                    digest=target_entry.digest,
                )
            ]
        )


class _ArtifactVersionType(Type):
    name = "artifactVersion"
    types = [Artifact, PublicArtifact]


TypeRegistry.add(_ArtifactVersionType)
