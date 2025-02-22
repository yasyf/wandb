from unittest import mock

import pytest
import wandb
from wandb.sdk.internal.internal_api import Api as InternalApi
from wandb.sdk.launch.launch import run
from wandb.sdk.launch.utils import LaunchError


def test_launch_repository(
    relay_server, runner, user, monkeypatch, wandb_init, test_settings
):
    queue = "default"
    proj = "test1"
    repo = "testing123"
    uri = "https://github.com/wandb/examples.git"
    entry_point = ["python", "/examples/examples/launch/launch-quickstart/train.py"]
    settings = test_settings({"project": proj})
    api = InternalApi()

    monkeypatch.setattr(
        wandb.sdk.launch.builder.build,
        "validate_docker_installation",
        lambda: None,
    )

    monkeypatch.setattr(
        wandb.docker,
        "build",
        lambda tags, file, context_path: None,
    )

    def patched_docker_push(reg, tag):
        assert reg == repo

    monkeypatch.setattr(
        wandb.docker,
        "push",
        lambda reg, tag: patched_docker_push(reg, tag),
    )

    with relay_server():
        wandb_init(settings=settings).finish()
        api.create_run_queue(
            entity=user, project=proj, queue_name=queue, access="PROJECT"
        )

        with pytest.raises(LaunchError) as e_info:
            run(
                api,
                uri=uri,
                entity=user,
                project=proj,
                entry_point=entry_point,
                repository=repo,
            )

        assert "Failed to push image to repository" in str(e_info)


def test_launch_incorrect_backend(
    relay_server, runner, user, monkeypatch, wandb_init, test_settings
):
    proj = "test1"
    uri = "https://github.com/wandb/examples.git"
    entry_point = ["python", "/examples/examples/launch/launch-quickstart/train.py"]
    settings = test_settings({"project": proj})
    api = InternalApi()

    monkeypatch.setattr(
        "wandb.sdk.launch.launch.fetch_and_validate_project",
        lambda _1, _2: "something",
    )

    monkeypatch.setattr(
        wandb.sdk.launch.builder.build,
        "validate_docker_installation",
        lambda: None,
    )

    monkeypatch.setattr(
        "wandb.docker",
        lambda: None,
    )

    with relay_server():
        r = wandb_init(settings=settings)

        with pytest.raises(LaunchError) as e_info:
            run(
                api,
                uri=uri,
                entity=user,
                project=proj,
                entry_point=entry_point,
                resource="testing123",
            )

        assert "Resource name not among available resources" in str(e_info)
        r.finish()


def test_launch_multi_run(relay_server, runner, user, wandb_init, test_settings):
    with runner.isolated_filesystem(), mock.patch.dict(
        "os.environ", {"WANDB_RUN_ID": "test", "WANDB_LAUNCH": "true"}
    ):
        run1 = wandb_init()
        run1.finish()

        run2 = wandb_init()
        run2.finish()

        assert run1.id == "test"
        assert run2.id != "test"


def test_launch_multi_run_context(
    relay_server, runner, user, wandb_init, test_settings
):
    with runner.isolated_filesystem(), mock.patch.dict(
        "os.environ", {"WANDB_RUN_ID": "test", "WANDB_LAUNCH": "true"}
    ):
        with wandb_init() as run1:
            run1.log({"test": 1})

        with wandb_init() as run2:
            run2.log({"test": 2})

        assert run1.id == "test"
        assert run2.id != "test"
