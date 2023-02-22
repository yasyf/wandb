"""Abstract base class for registries."""
from abc import ABC, abstractmethod
from typing import Tuple

from ..environment.abstract import AbstractEnvironment


class AbstractRegistry(ABC):
    """Abstract base class for registries."""

    @abstractmethod
    def verify(self) -> None:
        """Verify that the registry is configured correctly."""
        raise NotImplementedError

    @abstractmethod
    def get_repo_uri(self) -> str:
        """Get the uri of the given repository."""
        raise NotImplementedError

    @abstractmethod
    def get_username_password(self) -> Tuple[str, str]:
        """Get the username and password of the given repository."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_config(
        cls, config: dict, environment: "AbstractEnvironment"
    ) -> "AbstractRegistry":
        """Create a registry from a config."""
        raise NotImplementedError
