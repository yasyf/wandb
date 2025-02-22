import logging
from typing import Any, Dict, List

from wandb.sdk.launch.utils import LaunchError

from .abstract import AbstractBuilder

__logger__ = logging.getLogger(__name__)


_WANDB_BUILDERS: List[str] = ["kaniko", "docker", "noop"]


def load_builder(builder_config: Dict[str, Any]) -> AbstractBuilder:
    builder_name = builder_config.get("type", "docker")
    if builder_name == "kaniko":
        from .kaniko import KanikoBuilder

        return KanikoBuilder(builder_config)
    elif builder_name == "docker":
        from .docker import DockerBuilder

        return DockerBuilder(builder_config)
    elif builder_name == "noop":
        from .noop import NoOpBuilder

        return NoOpBuilder(builder_config)
    raise LaunchError(
        "Builder name not among available builders. Available builders: {} ".format(
            ",".join(_WANDB_BUILDERS)
        )
    )
