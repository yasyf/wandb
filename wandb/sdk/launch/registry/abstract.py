"""Abstract base class for registries."""
from abc import ABC, abstractmethod
from typing import Tuple


class AbstractRegistry(ABC):
    """Abstract base class for registries."""

    uri: str

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
        """Get the username and password for the registry."""
        raise NotImplementedError
