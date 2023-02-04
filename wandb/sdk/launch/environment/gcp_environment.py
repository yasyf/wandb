"""
Implementation of the GCP environment for wandb launch.
"""
import logging

from wandb.errors import LaunchError
from wandb.util import get_module

from .abstract import AbstractEnvironment

google = get_module(
    "google",
    required="Google Cloud Platform support requires the google package. Please"
    " install it with `pip install google`.",
)
google.cloud.compute_v1 = get_module(
    "google.cloud.compute_v1",
    required="Google Cloud Platform support requires the google-cloud-compute package. "
    "Please install it with `pip install google-cloud-compute`.",
)
# discovery = googleapiclient.discovery
google.auth.transport.requests = get_module(
    "google.auth.transport.requests",
    required="Google Cloud Platform support requires google-auth. "
    "Please install it with `pip install google-auth`.",
)
storage = get_module(
    "google.cloud.storage",
    required="Google Cloud Platform support requires google-cloud-storage. "
    "Please install it with `pip install google-cloud-storage.",
)


_logger = logging.getLogger(__name__)


class GcpEnvironment(AbstractEnvironment):
    """GCP Environment.

    Attributes:
        region: The GCP region.
    """

    region: str

    def __init__(self, region: str, verify: bool = True) -> None:
        """Initialize the GCP environment.

        Args:
            region: The GCP region.
            verify: Whether to verify the credentials, region, and project.

        Raises:
            LaunchError: If verify is True and the environment is not properly
                configured.
        """
        super().__init__()
        self.region = region
        self._project = None
        if verify:
            self.verify()

    @property
    def project(self):
        """Get the name of the gcp project.

        The project name is determined by the credentials, so this method
        verifies the credentials if they have not already been verified.

        Returns:
            str: The name of the gcp project.

        Raises:
            LaunchError: If the launch environment cannot be verified.
        """
        if self._project is None:
            self.verify()
        return self._project

    def get_credentials(self) -> google.auth.credentials.Credentials:
        """Get the GCP credentials.

        Uses google.auth.default() to get the credentials. If the credentials
        are invalid, this method will refresh them. If the credentials are
        still invalid after refreshing, this method will raise an error.

        Returns:
            google.auth.credentials.Credentials: The GCP credentials.

        Raises:
            LaunchError: If the GCP credentials are invalid.
        """
        try:
            creds, project = google.auth.default()
            if self._project is None:
                self._project = project
            elif self._project != project:
                # This should never happen, but we check just in case.
                raise LaunchError(
                    "The GCP project specified by the credentials has changed. "
                )
        except google.auth.exceptions.DefaultCredentialsError:
            raise LaunchError(
                "No Google Cloud Platform credentials found. Please run "
                "`gcloud auth application-default login` or set the environment "
                "variable GOOGLE_APPLICATION_CREDENTIALS to the path of a valid "
                "service account key file."
            )
        if not creds.valid:
            _logger.log(logging.INFO, "Refreshing GCP credentials")
            creds.refresh(google.auth.transport.requests.Request())
        if not creds.valid:
            raise LaunchError(
                "Invalid Google Cloud Platform credentials. Please run "
                "`gcloud auth application-default login` or set the environment "
                "variable GOOGLE_APPLICATION_CREDENTIALS to the path of a valid "
                "service account key file."
            )
        return creds

    def verify(self):
        """Verify the credentials, region, and project.

        Credentials and region are verified by calling get_credentials(). The
        region and is verified by calling the compute API.

        Raises:
            LaunchError: If the credentials, region, or project are invalid.

        Returns:
            None
        """
        creds = self.get_credentials()
        try:
            # Check if the region is available using the compute API.
            compute_client = google.cloud.compute_v1.RegionsClient(credentials=creds)
            compute_client.get(project=self.project, region=self.region)
        except google.api_core.exceptions.NotFound:
            raise LaunchError(
                f"Region {self.region} is not available in project {self.project}."
            )
