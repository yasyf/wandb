import logging
import os
import time
from typing import Any, Dict, Optional

import wandb
import wandb.docker as docker
from wandb.sdk.launch.builder.abstract import AbstractBuilder

from .._project_spec import (
    EntryPoint,
    LaunchProject,
    create_metadata_file,
    get_entry_point_command,
)
from ..utils import LOG_PREFIX, LaunchError, sanitize_wandb_api_key
from .build import (
    _create_docker_build_ctx,
    docker_image_exists,
    generate_dockerfile,
    image_tag_from_dockerfile_and_source,
    pull_docker_image,
    validate_docker_installation,
)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.wandb-autogenerated"
_logger = logging.getLogger(__name__)


class DockerBuilder(AbstractBuilder):
    type = "docker"

    def __init__(self, builder_config: Dict[str, Any]):
        super().__init__(builder_config)
        validate_docker_installation()

    def build_image(
        self,
        launch_project: LaunchProject,
        repository: Optional[str],
        entrypoint: EntryPoint,
    ) -> str:
        now = time.time()
        dockerfile_str = generate_dockerfile(
            launch_project, entrypoint, launch_project.resource, self.type
        )
        print(f"Generated dockerfile in {time.time() - now} seconds")

        image_tag = image_tag_from_dockerfile_and_source(launch_project, dockerfile_str)

        if repository:
            image_uri = f"{repository}:{image_tag}"
        else:
            image_uri = f"{launch_project.image_name}:{image_tag}"

        if not launch_project.build_required():
            if not repository and docker_image_exists(image_uri):
                _logger.info(
                    f"Image {image_uri} already exists locally, skipping build."
                )
                return image_uri
            elif repository:
                try:
                    pull_docker_image(image_uri)
                    return image_uri
                except DockerError:
                    _logger.info(
                        f"image {image_uri} does not already exist in repository, building."
                    )
        entry_cmd = get_entry_point_command(entrypoint, launch_project.override_args)

        create_metadata_file(
            launch_project,
            image_uri,
            sanitize_wandb_api_key(" ".join(entry_cmd)),
            dockerfile_str,
        )
        build_ctx_path = _create_docker_build_ctx(launch_project, dockerfile_str)
        dockerfile = os.path.join(build_ctx_path, _GENERATED_DOCKERFILE_NAME)
        try:
            docker.build(tags=[image_uri], file=dockerfile, context_path=build_ctx_path)
        except docker.DockerError as e:
            raise LaunchError(f"Error communicating with docker client: {e}")

        try:
            os.remove(build_ctx_path)
        except Exception:
            _msg = f"{LOG_PREFIX}Temporary docker context file {build_ctx_path} was not deleted."
            _logger.info(_msg)

        if repository:
            reg, tag = image_uri.split(":")
            wandb.termlog(f"{LOG_PREFIX}Pushing image {image_uri}")
            push_resp = docker.push(reg, tag)
            if push_resp is None:
                raise LaunchError("Failed to push image to repository")
            elif (
                launch_project.resource == "sagemaker"
                and f"The push refers to repository [{repository}]" not in push_resp
            ):
                raise LaunchError(f"Unable to push image to ECR, response: {push_resp}")

        return image_uri
