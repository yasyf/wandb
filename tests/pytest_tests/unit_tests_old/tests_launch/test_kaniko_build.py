import base64
import configparser
import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import kubernetes
import pytest
import wandb
from google.cloud import storage
from wandb.errors import LaunchError
from wandb.sdk.launch._project_spec import EntryPoint, LaunchProject
from wandb.sdk.launch.builder.kaniko import (
    KanikoBuilder,
    _create_dockerfile_configmap,
    _wait_for_completion,
)

from tests.pytest_tests.unit_tests_old.utils import fixture_open

from .test_launch import mocked_fetchable_git_repo  # noqa: F401


def return_kwargs(**kwargs):
    return kwargs


@pytest.fixture
def mock_kubernetes_client(monkeypatch):
    mock_config_map = MagicMock()
    mock_config_map.metadata = MagicMock()
    mock_config_map.metadata.name = "test-config-map"
    monkeypatch.setattr(kubernetes.client, "V1ConfigMap", mock_config_map)
    mock_api_client = MagicMock(name="api-client")
    mock_job = MagicMock(name="mock_job")
    mock_job_status = MagicMock()
    mock_job.status = mock_job_status
    # test success is true
    mock_job_status.succeeded = 1
    mock_api_client().read_namespaced_job_status.return_value = mock_job
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", mock_api_client)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", MagicMock())

    monkeypatch.setattr(kubernetes.client, "V1PodSpec", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1Volume", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1JobSpec", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1Job", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1PodTemplateSpec", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1Container", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1VolumeMount", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1SecretVolumeSource", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1ConfigMapVolumeSource", return_kwargs)
    monkeypatch.setattr(kubernetes.client, "V1ObjectMeta", return_kwargs)
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", return_kwargs)
    yield mock_api_client


@pytest.fixture
def mock_V1ObjectMeta(monkeypatch):
    monkeypatch.setattr(kubernetes.client, "V1ObjectMeta", return_kwargs)
    yield return_kwargs


@pytest.fixture
def mock_V1ConfigMap(monkeypatch):
    monkeypatch.setattr(kubernetes.client, "V1ConfigMap", return_kwargs)
    yield return_kwargs


@pytest.fixture
def mock_boto3(monkeypatch):
    monkeypatch.setattr(boto3, "client", MagicMock())


@pytest.fixture
def mock_storage_client(monkeypatch):
    monkeypatch.setattr(storage, "Client", MagicMock())


def test_wait_for_completion():
    mock_api_client = MagicMock()
    mock_job = MagicMock()
    mock_job_status = MagicMock()
    mock_job.status = mock_job_status
    # test success is true
    mock_job_status.succeeded = 1
    mock_api_client.read_namespaced_job_status.return_value = mock_job
    assert _wait_for_completion(mock_api_client, "test", 60)

    # test failed is false
    mock_job_status.succeeded = None
    mock_job_status.failed = 1
    assert _wait_for_completion(mock_api_client, "test", 60) is False

    # test timeout is false
    mock_job_status.failed = None
    assert _wait_for_completion(mock_api_client, "test", 5) is False


def test_create_dockerfile_configmap(
    monkeypatch, runner, mock_V1ConfigMap, mock_V1ObjectMeta
):
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/Dockerfile.wandb-autogenerated", "wb") as f:
            f.write(b"docker file test contents")
        result = _create_dockerfile_configmap("test_name", "./test/context/path/")
        assert result["metadata"]["name"] == "test_name"
        assert result["metadata"]["namespace"] == "wandb"
        assert result["metadata"]["labels"] == {"wandb": "launch"}
        assert result["binary_data"]["Dockerfile"] == base64.b64encode(
            b"docker file test contents"
        ).decode("UTF-8")

        assert result["immutable"] is True


def test_create_docker_ecr_config_map_non_instance(
    monkeypatch, runner, mock_V1ConfigMap, mock_V1ObjectMeta
):

    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws/",
        },
    }

    expected_args = (
        "wandb",
        {
            "api_version": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "docker-config",
                "namespace": "wandb",
            },
            "data": {"config.json": json.dumps({"credsStore": "ecr-login"})},
            "immutable": True,
        },
    )

    def check_args(*args):
        assert args == expected_args

    builder = KanikoBuilder(build_config)
    mock_client = MagicMock()
    mock_client.V1ConfigMap = mock_V1ConfigMap
    mock_client.V1ObjectMeta = mock_V1ObjectMeta
    mock_client.create_namespaced_config_map = check_args
    builder._create_docker_ecr_config_map(mock_client, "")


def test_create_docker_ecr_config_map_instance(
    monkeypatch, runner, mock_V1ConfigMap, mock_V1ObjectMeta
):
    reg = "12345678.dkr.ecr.us-east-1.amazonaws.com/test-repo"
    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
    }

    expected_args = (
        "wandb",
        {
            "api_version": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "docker-config",
                "namespace": "wandb",
            },
            "data": {"config.json": json.dumps({"credHelpers": {reg: "ecr-login"}})},
            "immutable": True,
        },
    )

    def check_args(*args):
        assert args == expected_args

    builder = KanikoBuilder(build_config)
    mock_client = MagicMock()
    mock_client.V1ConfigMap = mock_V1ConfigMap
    mock_client.V1ObjectMeta = mock_V1ObjectMeta
    mock_client.create_namespaced_config_map = check_args
    builder._create_docker_ecr_config_map(mock_client, reg)


def test_upload_build_context_aws(monkeypatch, runner, mock_boto3):
    context_store_url = "test-url"
    run_id = "12345678"
    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": context_store_url,
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws/",
        },
    }
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/blah.txt", "wb") as f:
            f.write(b"test contents")
        builder = KanikoBuilder(build_config)
        returned_path = builder._upload_build_context(run_id, "./test/context/path/")
        assert returned_path == f"s3://{context_store_url}/{run_id}.tgz"


def test_upload_build_context_gcp(monkeypatch, runner, mock_storage_client):
    context_store_url = "test-url"
    run_id = "12345678"
    build_config = {
        "cloud-provider": "gcp",
        "build-context-store": "test-url",
        "credentials": {
            "secret-name": "gcp-secret",
            "secret-mount-path": "/root/.gcp/",
        },
    }
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/blah.txt", "wb") as f:
            f.write(b"test contents")
        builder = KanikoBuilder(build_config)
        returned_path = builder._upload_build_context(run_id, "./test/context/path/")
        assert returned_path == f"gs://{context_store_url}/{run_id}.tgz"


def test_upload_build_context_err(monkeypatch, runner, mock_boto3):
    build_config = {
        "cloud-provider": "bad-provider",
        "build-context-store": "test-url",
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws/",
        },
    }
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/blah.txt", "wb") as f:
            f.write(b"test contents")
        builder = KanikoBuilder(build_config)
        with pytest.raises(LaunchError):
            builder._upload_build_context("12345678", "./test/context/path/")


def test_create_kaniko_job_static(mock_kubernetes_client, runner):
    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws",
        },
    }
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/Dockerfile.wandb-autogenerated", "wb") as f:
            f.write(b"docker file test contents")
        builder = KanikoBuilder(build_config)
        job_name = "test_job_name"
        config_map_name = "wandb-launch-build-context"
        repo_url = "repository-url"
        image_tag = "image_tag:12345678"
        context_path = "./test/context/path/"
        job = builder._create_kaniko_job(
            job_name,
            config_map_name,
            repo_url,
            image_tag,
            context_path,
        )

        assert job["metadata"]["name"] == "test_job_name"
        assert job["metadata"]["namespace"] == "wandb"
        assert job["metadata"]["labels"] == {"wandb": "launch"}
        assert (
            job["spec"]["template"]["spec"]["containers"][0]["image"]
            == "gcr.io/kaniko-project/executor:v1.8.0"
        )
        assert job["spec"]["template"]["spec"]["containers"][0]["args"] == [
            f"--context={context_path}",
            "--dockerfile=/etc/config/Dockerfile",
            f"--destination={image_tag}",
            "--cache=true",
            f"--cache-repo={repo_url}",
            "--snapshotMode=redo",
        ]

        assert job["spec"]["template"]["spec"]["containers"][0]["volume_mounts"] == [
            {"name": "build-context-config-map", "mount_path": "/etc/config"},
            {
                "name": "docker-config",
                "mount_path": "/kaniko/.docker/",
            },
            {"name": "aws-secret", "mount_path": "/root/.aws", "read_only": True},
        ]

        assert job["spec"]["template"]["spec"]["volumes"] == [
            {
                "name": "build-context-config-map",
                "config_map": {"name": config_map_name},
            },
            {
                "name": "docker-config",
                "config_map": {"name": "docker-config"},
            },
            {
                "name": "aws-secret",
                "secret": {
                    "secret_name": "aws-secret",
                },
            },
        ]


def test_create_kaniko_job_instance(mock_kubernetes_client, runner):
    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
    }
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/Dockerfile.wandb-autogenerated", "wb") as f:
            f.write(b"docker file test contents")
        builder = KanikoBuilder(build_config)
        job_name = "test_job_name"
        config_map_name = "wandb-launch-build-context"
        repo_url = "12345678.dkr.ecr.us-east-1.amazonaws.com/test-repo"
        image_tag = "image_tag:12345678"
        context_path = "./test/context/path/"
        job = builder._create_kaniko_job(
            job_name,
            config_map_name,
            repo_url,
            image_tag,
            context_path,
        )

        assert job["metadata"]["name"] == "test_job_name"
        assert job["metadata"]["namespace"] == "wandb"
        assert job["metadata"]["labels"] == {"wandb": "launch"}
        assert (
            job["spec"]["template"]["spec"]["containers"][0]["image"]
            == "gcr.io/kaniko-project/executor:v1.8.0"
        )
        assert job["spec"]["template"]["spec"]["containers"][0]["args"] == [
            f"--context={context_path}",
            "--dockerfile=/etc/config/Dockerfile",
            f"--destination={image_tag}",
            "--cache=true",
            f"--cache-repo={repo_url}",
            "--snapshotMode=redo",
        ]

        assert job["spec"]["template"]["spec"]["containers"][0]["volume_mounts"] == [
            {"name": "build-context-config-map", "mount_path": "/etc/config"},
            {
                "name": "docker-config",
                "mount_path": "/kaniko/.docker/",
            },
        ]
        assert job["spec"]["template"]["spec"]["containers"][0]["env"] == [
            kubernetes.client.V1EnvVar(name="AWS_REGION", value="us-east-1")
        ]

        assert job["spec"]["template"]["spec"]["volumes"] == [
            {
                "name": "build-context-config-map",
                "config_map": {"name": config_map_name},
            },
            {
                "name": "docker-config",
                "config_map": {"name": "docker-config"},
            },
        ]


def test_build_image_success(
    monkeypatch, mock_kubernetes_client, runner, mock_boto3, test_settings, capsys
):

    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws",
        },
    }
    api = wandb.sdk.internal.internal_api.Api(
        default_settings=test_settings, load_settings=False
    )
    with runner.isolated_filesystem():
        os.makedirs("./test/context/path/", exist_ok=True)
        with open("./test/context/path/Dockerfile.wandb-autogenerated", "wb") as f:
            f.write(b"docker file test contents")
        builder = KanikoBuilder(build_config)
        kwargs = {
            "uri": "https://wandb.ai/mock_server_entity/test/runs/1",
            "job": None,
            "api": api,
            "launch_spec": {},
            "target_entity": "mock_server_entity",
            "target_project": "test",
            "name": None,
            "docker_config": {},
            "git_info": {},
            "overrides": {"entry_point": ["python", "main.py"]},
            "resource": "kubernetes",
            "resource_args": {},
            "cuda": None,
            "run_id": None,
        }
        project = LaunchProject(**kwargs)
        entry_point = EntryPoint("main.py", ["python", "main.py"])
        image_uri = builder.build_image(project, "repository-url", entry_point)
        assert "defaulting to building" in capsys.readouterr().err
        # the string below is the result of the hash
        assert image_uri == "repository-url:7ab84ee7"


def test_kaniko_build_no_cloud_provider():
    with pytest.raises(LaunchError):
        KanikoBuilder({"cloud-provider": "AWS"})


def test_kaniko_build_instance_mode(capsys):
    KanikoBuilder({"cloud-provider": "AWS", "build-context-store": "s3://test-url"})
    assert "Kaniko builder running in instance mode" in capsys.readouterr().err


def test_no_context_store():
    with pytest.raises(LaunchError):
        KanikoBuilder({"cloud-provider": "AWS"})


def build_image_no_repo():
    build_config = {
        "cloud-provider": "AWS",
        "build-context-store": "s3",
        "credentials": {
            "secret-name": "aws-secret",
            "secret-mount-path": "/root/.aws",
        },
    }
    builder = KanikoBuilder(build_config)
    with pytest.raises(LaunchError):
        builder.build_image(None, None, None, {})
