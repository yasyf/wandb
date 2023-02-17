"""Batching file prepare requests to our API."""

import queue
import threading
import time
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

if TYPE_CHECKING:
    from wandb.sdk.internal.internal_api import (
        Api,
        CreateArtifactFileSpecInput,
        CreateArtifactFilesResponseFile,
    )


# Request for a file to be prepared.
class RequestPrepare(NamedTuple):
    file_spec: "CreateArtifactFileSpecInput"
    response_queue: "queue.Queue[ResponsePrepare]"


class RequestFinish(NamedTuple):
    pass


class ResponsePrepare(NamedTuple):
    upload_url: Optional[str]
    upload_headers: Sequence[str]
    birth_artifact_id: str


Event = Union[RequestPrepare, RequestFinish, ResponsePrepare]


def _clamp(x: float, low: float, high: float) -> float:
    return max(low, min(x, high))


def gather_batch(
    request_queue: "queue.Queue[Event]",
    batch_time: float,
    inter_event_time: float,
    max_batch_size: int,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[bool, Sequence[RequestPrepare]]:

    batch_start_time = clock()
    remaining_time = batch_time

    first_request = request_queue.get()
    if isinstance(first_request, RequestFinish):
        return True, []

    batch: List[RequestPrepare] = [first_request]

    while remaining_time > 0 and len(batch) < max_batch_size:
        try:
            request = request_queue.get(
                timeout=_clamp(
                    x=inter_event_time,
                    low=1e-12,  # 0 = "block forever", so just use something tiny
                    high=remaining_time,
                ),
            )
            if isinstance(request, RequestFinish):
                return True, batch

            batch.append(request)
            remaining_time = batch_time - (clock() - batch_start_time)

        except queue.Empty:
            break

    return False, batch


class StepPrepare:
    """A thread that batches requests to our file prepare API.

    Any number of threads may call prepare_async() in parallel. The PrepareBatcher thread
    will batch requests up and send them all to the backend at once.
    """

    def __init__(
        self,
        api: "Api",
        batch_time: float,
        inter_event_time: float,
        max_batch_size: int,
        request_queue: Optional["queue.Queue[Event]"] = None,
    ) -> None:
        self._api = api
        self._inter_event_time = inter_event_time
        self._batch_time = batch_time
        self._max_batch_size = max_batch_size
        self._request_queue: "queue.Queue[RequestPrepare | RequestFinish]" = (
            request_queue or queue.Queue()
        )
        self._thread = threading.Thread(target=self._thread_body)
        self._thread.daemon = True

    def _thread_body(self) -> None:
        while True:
            finish, batch = gather_batch(
                request_queue=self._request_queue,
                batch_time=self._batch_time,
                inter_event_time=self._inter_event_time,
                max_batch_size=self._max_batch_size,
            )
            if batch:
                prepare_response = self._prepare_batch(batch)
                # send responses
                for prepare_request in batch:
                    name = prepare_request.file_spec["name"]
                    response_file = prepare_response[name]
                    upload_url = response_file["uploadUrl"]
                    upload_headers = response_file["uploadHeaders"]
                    birth_artifact_id = response_file["artifact"]["id"]
                    prepare_request.response_queue.put(
                        ResponsePrepare(upload_url, upload_headers, birth_artifact_id)
                    )
            if finish:
                break

    def _prepare_batch(
        self, batch: Sequence[RequestPrepare]
    ) -> Mapping[str, "CreateArtifactFilesResponseFile"]:
        """Execute the prepareFiles API call.

        Arguments:
            batch: List of RequestPrepare objects
        Returns:
            dict of (save_name: ResponseFile) pairs where ResponseFile is a dict with
                an uploadUrl key. The value of the uploadUrl key is None if the file
                already exists, or a url string if the file should be uploaded.
        """
        return self._api.create_artifact_files([req.file_spec for req in batch])

    def prepare_async(
        self, file_spec: "CreateArtifactFileSpecInput"
    ) -> "queue.Queue[ResponsePrepare]":
        """Request the backend to prepare a file for upload.

        Returns:
            response_queue: a queue containing the prepare result. The prepare result is
                either a file upload url, or None if the file doesn't need to be uploaded.
        """
        response_queue: "queue.Queue[ResponsePrepare]" = queue.Queue()
        self._request_queue.put(RequestPrepare(file_spec, response_queue))
        return response_queue

    def prepare(self, file_spec: "CreateArtifactFileSpecInput") -> ResponsePrepare:
        return self.prepare_async(file_spec).get()

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> None:
        self._request_queue.put(RequestFinish())

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def shutdown(self) -> None:
        self.finish()
        self._thread.join()
