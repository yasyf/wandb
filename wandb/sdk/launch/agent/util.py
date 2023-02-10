"""Utilities for the agent."""
from typing import Any, Dict, Optional

from wandb.errors import LaunchError
from wandb.util import get_module
from wandb.sdk.launch.environment.abstract import AbstractEnvironment
from wandb.sdk.launch.registry.abstract import AbstractRegistry
from wandb.sdk.launch.builder.abstract import AbstractBuilder


def environment_from_config(config: Optional[Dict[str, Any]]) -> AbstractEnvironment:
    """Create an environment from a config.

    This helper function is used to create an environment from a config. The
    config should have a "type" key that specifies the type of environment to
    create. The remaining keys are passed to the environment's from_config
    method.

    Args:
        config (Dict[str, Any]): The config.
    Returns:
        Environment: The environment.
    Raises:
        LaunchError: If the environment is not configured correctly.
    """
    env_type = config.get("type")
    if env_type == "aws":
        module = get_module("wandb.sdk.launch.environment.aws_environment")
        return module.AwsEnvironment.from_config(config)
    if env_type == "gcp":
        module = get_module("wandb.sdk.launch.environment.gcp_environment")
        return module.GcpEnvironment.from_config(config)
    raise LaunchError("Could not create environment from config.")


def registry_from_config(
    config: Optional[Dict[str, Any]], environment: AbstractEnvironment
) -> AbstractRegistry:
    """Create a registry from a config.

    This helper function is used to create a registry from a config. The
    config should have a "type" key that specifies the type of registry to
    create. The remaining keys are passed to the registry's from_config
    method.

    Args:
        config (Dict[str, Any]): The registry config.
        environment (Environment): The environment of the registry.

    Returns:
        The registry.

    Raises:
        LaunchError: If the registry is not configured correctly.
    """
    registry_type = config.get("type")
    if registry_type == "ecr":
        module = get_module("wandb.sdk.launch.registry.elastic_container_registry")
        return module.ElasticContainerRegistry.from_config(config, environment)
    if registry_type == "gcr":
        module = get_module("wandb.sdk.launch.registry.gcr_registry")
        return module.GcrRegistry.from_config(config, environment)
    raise LaunchError("Could not create registry from config.")


def builder_from_config(
    config: Optional[Dict[str, Any]], registry: AbstractRegistry
) -> AbstractBuilder:
    """Create a builder from a config.

    This helper function is used to create a builder from a config. The
    config should have a "type" key that specifies the type of builder to import
    and create. The remaining keys are passed to the builder's from_config
    method.

    Args:
        config (Dict[str, Any]): The builder config.
        registry (Registry): The registry of the builder.

    Returns:
        The builder.

    Raises:
        LaunchError: If the builder is not configured correctly.
    """
    builder_type = config.get("type")
    if builder_type == "docker":
        module = get_module("wandb.sdk.launch.builder.docker_builder")
        return module.DockerBuilder.from_config(config, registry)
    if builder_type == "kaniko":
        module = get_module("wandb.sdk.launch.builder.kaniko_builder")
        return module.KanikoBuilder.from_config(config, registry)
    raise LaunchError("Could not create builder from config.")
