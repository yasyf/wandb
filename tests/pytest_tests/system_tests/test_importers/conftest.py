import os
import signal
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import warnings
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
from typing import Any, Callable, Dict, Iterable, List, Optional

import hypothesis.strategies as st
import mlflow
import pytest
import requests
from hypothesis.errors import NonInteractiveExampleWarning
from mlflow.entities import Metric
from mlflow.tracking import MlflowClient

MLFLOW_BASE_URL = "http://localhost:4040"
MLFLOW_HEALTH_ENDPOINT = "health"
EXPERIMENTS = 2
RUNS_PER_EXPERIMENT = 3
N_ARTIFACTS_PER_RUN = 1000
STEPS = 100_000

SECONDS_FROM_2023_01_01 = 1672549200


Path = str


def _make_run(
    tags: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, float]] = None,
    metrics: Optional[Iterable[Dict[str, float]]] = None,
    make_artifact_fns: Optional[Iterable[Callable[[], Path]]] = None,
    batch_size: Optional[int] = None,
):
    client = MlflowClient()
    with mlflow.start_run() as run:
        if tags:
            mlflow.set_tags(tags)
        if params:
            mlflow.log_params(params)
        if metrics:
            if batch_size:
                for batch in batch_metrics(metrics, batch_size):
                    client.log_batch(run.info.run_id, metrics=batch)
            else:
                for m in metrics:
                    mlflow.log_metrics(m)
        if make_artifact_fns:
            for f in make_artifact_fns:
                path = f()
                mlflow.log_artifact(path)


# def make_nested_run():
#     with mlflow.start_run():
#         for _ in range(NUM_RUNS_PER_NESTED_EXPERIMENT):
#             make_run(batch_size=50)


def batched(n, iterable):
    i = iter(iterable)
    batch = list(islice(i, n))
    while batch:
        yield batch
        batch = list(islice(i, n))


def batch_metrics(metrics, bs: int) -> Iterable[List[Metric]]:
    step = 0
    for i, batch in enumerate(batched(bs, metrics)):
        batched_metrics = []
        for step, metric in enumerate(batch, start=i * bs):
            for k, v in metric.items():
                batched_metrics.append(
                    Metric(k, v, step=step, timestamp=SECONDS_FROM_2023_01_01)
                )
        yield batched_metrics


def make_tags():
    return st.dictionaries(
        st.text(
            min_size=1,
            max_size=20,
            alphabet="abcdefghijklmnopqrstuvwxyz1234567890_- ",
        ),
        st.text(max_size=20),
        max_size=10,
    ).example()


def make_params():
    return {
        "param_int": st.integers().example(),
        "param_float": st.floats().example(),
        "param_str": st.text(max_size=20).example(),
        "param_bool": st.booleans().example(),
        "param_list": st.lists(st.integers()).example(),
        "param_dict": st.dictionaries(
            st.text(max_size=20), st.integers(), max_size=10
        ).example(),
        "param_tuple": st.tuples(st.integers(), st.integers()).example(),
        "param_set": st.sets(st.integers()).example(),
        "param_none": None,
    }


def make_metrics(n_steps):
    for _ in range(n_steps):
        yield {
            "metric_int": st.integers(min_value=0, max_value=100).example(),
            "metric_float": st.floats(min_value=0, max_value=100).example(),
        }


def make_artifact_fns(temp_path: Path) -> Iterable[Callable[[], Path]]:
    def artifact_fn():
        fname = str(uuid.uuid4())
        file_path = f"{temp_path}/{fname}.txt"
        with open(file_path, "w") as f:
            f.write(f"text from {file_path}")
        return file_path

    for _ in range(N_ARTIFACTS_PER_RUN):
        yield artifact_fn


def make_run(n_steps, mlflow_batch_size):
    with tempfile.TemporaryDirectory() as temp_path:
        _make_run(
            tags=make_tags(),
            params=make_params(),
            metrics=make_metrics(n_steps),
            make_artifact_fns=make_artifact_fns(temp_path),
            batch_size=mlflow_batch_size,
        )


def make_exp(n_runs, n_steps, mlflow_batch_size):
    name = "Experiment " + str(uuid.uuid4())
    mlflow.set_experiment(name)
    with ProcessPoolExecutor() as exc:
        for _ in range(n_runs):
            exc.submit(make_run, n_steps, mlflow_batch_size)


def log_to_mlflow(
    mlflow_tracking_uri: str,
    n_exps: int = 2,
    n_runs_per_exp: int = 3,
    n_steps: int = 1000,
    mlflow_batch_size: int = 50,
):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=NonInteractiveExampleWarning)
        mlflow.set_tracking_uri(mlflow_tracking_uri)

        with ProcessPoolExecutor() as exc:
            for _ in range(n_exps):
                exc.submit(make_exp, n_runs_per_exp, n_steps, mlflow_batch_size)


def check_mlflow_server_health(
    base_url: str, endpoint: str, num_retries: int = 1, sleep_time: int = 1
):
    for _ in range(num_retries):
        try:
            response = requests.get(urllib.parse.urljoin(base_url, endpoint))
            if response.status_code == 200:
                return True
            time.sleep(sleep_time)
        except requests.exceptions.ConnectionError:
            time.sleep(sleep_time)
    return False


@pytest.fixture(scope="session")
def mlflow_server():
    start_cmd = [
        "mlflow",
        "server",
        "-p",
        "4040",
    ]
    process = subprocess.Popen(start_cmd)  # process
    healthy = check_mlflow_server_health(
        MLFLOW_BASE_URL, MLFLOW_HEALTH_ENDPOINT, num_retries=30
    )

    if healthy:
        yield MLFLOW_BASE_URL
    else:
        raise Exception("MLflow server is not healthy")
    if os.environ.get("CI") != "true":
        # only do this on local machine; causes problems in CI
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except KeyboardInterrupt:
            print("SIGINT for MLflow Server")

    cwd = os.getcwd()
    stop_cmd = ["rm", "-rf", f"{cwd}/mlruns", f"{cwd}/mlartifacts"]
    subprocess.run(stop_cmd)


@pytest.fixture(scope="session")
def prelogged_mlflow_server(mlflow_server):
    log_to_mlflow(mlflow_server, EXPERIMENTS, RUNS_PER_EXPERIMENT, STEPS)
    yield mlflow_server, EXPERIMENTS, RUNS_PER_EXPERIMENT, STEPS
