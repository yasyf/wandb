import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

from kubernetes import client  # type: ignore
from kubernetes.client.api.batch_v1_api import BatchV1Api  # type: ignore
from kubernetes.client.api.core_v1_api import CoreV1Api  # type: ignore
from kubernetes.client.models.v1_job import V1Job  # type: ignore
from kubernetes.client.models.v1_secret import V1Secret  # type: ignore

import wandb
from wandb.sdk.launch.builder.abstract import AbstractBuilder
from wandb.util import get_module, load_json_yaml_dict

from .._project_spec import LaunchProject, get_entry_point_command
from ..builder.build import get_env_vars_dict
from ..utils import (
    LOG_PREFIX,
    PROJECT_SYNCHRONOUS,
    LaunchError,
    get_kube_context_and_api_client,
    make_name_dns_safe,
)
from .abstract import AbstractRun, AbstractRunner, Status

TIMEOUT = 5
MAX_KUBERNETES_RETRIES = (
    60  # default 10 second loop time on the agent, this is 10 minutes
)
FAIL_MESSAGE_INTERVAL = 60

_logger = logging.getLogger(__name__)


class KubernetesSubmittedRun(AbstractRun):
    def __init__(
        self,
        batch_api: "BatchV1Api",
        core_api: "CoreV1Api",
        name: str,
        pod_names: List[str],
        namespace: Optional[str] = "default",
        secret: Optional["V1Secret"] = None,
    ) -> None:
        self.batch_api = batch_api
        self.core_api = core_api
        self.name = name
        self.namespace = namespace
        self.job = self.batch_api.read_namespaced_job(
            name=self.name, namespace=self.namespace
        )
        self._fail_count = 0
        self.pod_names = pod_names
        self.secret = secret

    @property
    def id(self) -> str:
        return self.name

    def get_job(self) -> "V1Job":
        return self.batch_api.read_namespaced_job(
            name=self.name, namespace=self.namespace
        )

    def wait(self) -> bool:
        while True:
            status = self.get_status()
            wandb.termlog(f"{LOG_PREFIX}Job {self.name} status: {status}")
            if status.state != "running":
                break
            time.sleep(5)
        return (
            status.state == "finished"
        )  # todo: not sure if this (copied from aws runner) is the right approach? should we return false on failure

    def get_status(self) -> Status:
        job_response = self.batch_api.read_namespaced_job_status(
            name=self.name, namespace=self.namespace
        )
        status = job_response.status
        try:
            self.core_api.read_namespaced_pod_log(
                name=self.pod_names[0], namespace=self.namespace
            )
        except Exception as e:
            now = time.time()
            if self._fail_count == 0:
                self._fail_first_msg_time = now
                self._fail_last_msg_time = 0.0
            self._fail_count += 1
            if now - self._fail_last_msg_time > FAIL_MESSAGE_INTERVAL:
                wandb.termlog(
                    f"{LOG_PREFIX}Failed to get pod status for job: {self.name}. Will wait up to {round(10 - (now - self._fail_first_msg_time)/60)} minutes for job to start."
                )
                self._fail_last_msg_time = now
            if self._fail_count > MAX_KUBERNETES_RETRIES:
                raise LaunchError(
                    f"Failed to start job {self.name}, because of error {str(e)}"
                )
        # todo: we only handle the 1 pod case. see https://kubernetes.io/docs/concepts/workloads/controllers/job/#parallel-jobs for multipod handling
        return_status = None
        if status.succeeded == 1:
            return_status = Status("finished")
        elif status.failed is not None and status.failed >= 1:
            return_status = Status("failed")
        elif status.active == 1:
            return Status("running")
        elif status.conditions is not None and status.conditions[0].type == "Suspended":
            return_status = Status("stopped")
        else:
            return_status = Status("unknown")
        if (
            return_status.state in ["stopped", "failed", "finished"]
            and self.secret is not None
        ):
            try:
                self.core_api.delete_namespaced_secret(
                    self.secret.metadata.name, self.namespace
                )
            except Exception as e:
                wandb.termerror(
                    f"Error deleting secret {self.secret.metadata.name}: {str(e)}"
                )
        return return_status

    def suspend(self) -> None:
        self.job.spec.suspend = True
        self.batch_api.patch_namespaced_job(
            name=self.name, namespace=self.namespace, body=self.job
        )
        timeout = TIMEOUT
        job_response = self.batch_api.read_namespaced_job_status(
            name=self.name, namespace=self.namespace
        )
        while job_response.status.conditions is None and timeout > 0:
            time.sleep(1)
            timeout -= 1
            job_response = self.batch_api.read_namespaced_job_status(
                name=self.name, namespace=self.namespace
            )

        if timeout == 0 or job_response.status.conditions[0].type != "Suspended":
            raise LaunchError(
                "Failed to suspend job {}. Check Kubernetes dashboard for more info.".format(
                    self.name
                )
            )

    def cancel(self) -> None:
        self.suspend()
        self.batch_api.delete_namespaced_job(name=self.name, namespace=self.namespace)


class KubernetesRunner(AbstractRunner):
    def populate_job_spec(
        self, job_spec: Dict[str, Any], resource_args: Dict[str, Any]
    ) -> None:
        if resource_args.get("backoff_limit"):
            job_spec["backoffLimit"] = resource_args.get("backoff_limit")
        if resource_args.get("completions"):
            job_spec["completions"] = resource_args.get("completions")
        if resource_args.get("parallelism"):
            job_spec["parallelism"] = resource_args.get("parallelism")
        if resource_args.get("suspend"):
            job_spec["suspend"] = resource_args.get("suspend")

    def populate_pod_spec(
        self, pod_spec: Dict[str, Any], resource_args: Dict[str, Any]
    ) -> None:
        pod_spec["restartPolicy"] = resource_args.get("restart_policy", "Never")
        if resource_args.get("preemption_policy"):
            pod_spec["preemptionPolicy"] = resource_args.get("preemption_policy")
        if resource_args.get("node_name"):
            pod_spec["nodeName"] = resource_args.get("node_name")
        if resource_args.get("node_selector"):
            pod_spec["nodeSelector"] = resource_args.get("node_selector")
        if resource_args.get("tolerations"):
            pod_spec["tolerations"] = resource_args.get("tolerations")
        if resource_args.get("security_context"):
            pod_spec["securityContext"] = resource_args.get("security_context")
        if resource_args.get("volumes") is not None:
            vols = resource_args.get("volumes")
            if not isinstance(vols, list):
                raise LaunchError("volumes must be a list of volume specifications")
            pod_spec["volumes"] = vols

    def populate_container_resources(
        self, containers: List[Dict[str, Any]], resource_args: Dict[str, Any]
    ) -> None:

        if resource_args.get("container_name"):
            if len(containers) > 1:
                raise LaunchError(
                    "Container name override not supported for multiple containers. Specify in yaml file supplied via job_spec."
                )
            containers[0]["name"] = resource_args["container_name"]
        else:
            for i, cont in enumerate(containers):
                cont["name"] = cont.get("name", "launch" + str(i))

        multi_container_override = len(containers) > 1
        for cont in containers:
            container_resources = cont.get("resources", {})
            if resource_args.get("resource_requests"):
                container_resources["requests"] = resource_args.get("resource_requests")
            if resource_args.get("resource_limits"):
                container_resources["limits"] = resource_args.get("resource_limits")
            if container_resources:
                multi_container_override &= (
                    cont.get("resources") != container_resources
                )  # if multiple containers and we changed something
                cont["resources"] = container_resources
            if resource_args.get("volume_mounts") is not None:
                vol_mounts = resource_args.get("volume_mounts")
                if not isinstance(vol_mounts, list):
                    raise LaunchError(
                        "volume mounts must be a list of volume mount specifications"
                    )
                cont["volumeMounts"] = vol_mounts
            cont["security_context"] = {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            }
        if multi_container_override:
            wandb.termwarn(
                "{LOG_PREFIX}Container overrides (e.g. resource limits) were provided with multiple containers specified: overrides will be applied to all containers."
            )

    def wait_job_launch(
        self, job_name: str, namespace: str, core_api: "CoreV1Api"
    ) -> List[str]:
        pods = core_api.list_namespaced_pod(
            label_selector=f"job-name={job_name}", namespace=namespace
        )
        timeout = TIMEOUT
        while len(pods.items) == 0 and timeout > 0:
            time.sleep(1)
            timeout -= 1
            pods = core_api.list_namespaced_pod(
                label_selector=f"job-name={job_name}", namespace=namespace
            )

        if timeout == 0:
            raise LaunchError(
                "No pods found for job {}. Check dashboard to see if job was launched successfully.".format(
                    job_name
                )
            )

        pod_names = [pi.metadata.name for pi in pods.items]
        wandb.termlog(
            f"{LOG_PREFIX}Job {job_name} created on pod(s) {', '.join(pod_names)}. See logs with e.g. `kubectl logs {pod_names[0]} -n {namespace}`."
        )
        return pod_names

    def get_namespace(
        self, resource_args: Dict[str, Any]
    ) -> Optional[str]:  # noqa: C901
        return self.backend_config.get("runner", {}).get(
            "namespace"
        ) or resource_args.get("namespace")

    def run(
        self,
        launch_project: LaunchProject,
        builder: AbstractBuilder,
        registry_config: Dict[str, Any],
    ) -> Optional[AbstractRun]:  # noqa: C901
        kubernetes = get_module(  # noqa: F811
            "kubernetes", "KubernetesRunner requires kubernetes to be installed"
        )

        resource_args = launch_project.resource_args.get("kubernetes", {})
        if not resource_args:
            wandb.termlog(
                f"{LOG_PREFIX}Note: no resource args specified. Add a Kubernetes yaml spec or other options in a json file with --resource-args <json>."
            )
        _logger.info(f"Running Kubernetes job with resource args: {resource_args}")
        context, api_client = get_kube_context_and_api_client(kubernetes, resource_args)

        batch_api = kubernetes.client.BatchV1Api(api_client)
        core_api = kubernetes.client.CoreV1Api(api_client)

        # allow users to specify template or entire spec
        if resource_args.get("job_spec"):
            job_dict = load_json_yaml_dict(resource_args["job_spec"])
        else:
            # begin constructing job sped
            job_dict = {"apiVersion": "batch/v1", "kind": "Job"}

        # extract job spec component parts for convenience
        job_metadata = job_dict.get("metadata", {})
        job_spec = job_dict.get("spec", {})
        pod_template = job_spec.get("template", {})
        pod_metadata = pod_template.get("metadata", {})
        pod_spec = pod_template.get("spec", {})
        containers = pod_spec.get("containers", [{}])
        job_status = job_dict.get("status", {})

        # begin pulling resource arg overrides. all of these are optional

        # allow top-level namespace override, otherwise take namespace specified at the job level, or default in current context
        default_namespace = (
            context["context"].get("namespace", "default") if context else "default"
        )
        namespace = self.get_namespace(resource_args) or default_namespace

        # name precedence: resource args override > name in spec file > generated name
        job_metadata["name"] = resource_args.get("job_name", job_metadata.get("name"))
        if not job_metadata.get("name"):
            job_metadata["generateName"] = make_name_dns_safe(
                f"launch-{launch_project.target_entity}-{launch_project.target_project}-"
            )

        if resource_args.get("job_labels"):
            job_metadata["labels"] = resource_args.get("job_labels")

        self.populate_job_spec(job_spec, resource_args)
        self.populate_pod_spec(pod_spec, resource_args)

        self.populate_container_resources(containers, resource_args)

        # cmd
        entry_point = launch_project.get_single_entry_point()

        # env vars
        env_vars = get_env_vars_dict(launch_project, self._api)
        secret = None
        # only need to do this if user is providing image, on build, our image sets an entrypoint
        entry_cmd = get_entry_point_command(entry_point, launch_project.override_args)
        if launch_project.docker_image and entry_cmd:
            # if user hardcodes cmd into their image, we don't need to run on top of that
            for cont in containers:
                cont["command"] = entry_cmd

        if launch_project.docker_image:
            if len(containers) > 1:
                raise LaunchError(
                    "Multiple container configurations should be specified in a yaml file supplied via job_spec."
                )
            # dont specify run id if user provided image, could have multiple runs
            containers[0]["image"] = launch_project.docker_image
            image_uri = launch_project.docker_image
            # TODO: handle secret pulling image from registry
        elif not any(["image" in cont for cont in containers]):
            if len(containers) > 1:
                raise LaunchError(
                    "Launch only builds one container at a time. Multiple container configurations should be pre-built and specified in a yaml file supplied via job_spec."
                )
            given_reg = resource_args.get("registry", "")
            repository: Optional[str] = (
                given_reg if given_reg != "" else registry_config.get("url")
            )
            if repository is None:
                # allow local registry usage for eg local clusters but throw a warning
                wandb.termwarn(
                    f"{LOG_PREFIX}Warning: No Docker repository specified. Image will be hosted on local registry, which may not be accessible to your training cluster."
                )
            assert entry_point is not None
            image_uri = builder.build_image(launch_project, repository, entry_point)
            # in the non instance case we need to make an imagePullSecret
            # so the new job can pull the image
            secret = maybe_create_imagepull_secret(
                core_api, registry_config, launch_project.run_id, namespace
            )

            containers[0]["image"] = image_uri

        # reassemble spec
        given_env_vars = resource_args.get("env", [])
        kubernetes_style_env_vars = [
            {"name": k, "value": v} for k, v in env_vars.items()
        ]
        _logger.info(
            f"Using environment variables: {given_env_vars + kubernetes_style_env_vars}"
        )
        for cont in containers:
            cont["env"] = given_env_vars + kubernetes_style_env_vars
        pod_spec["containers"] = containers

        pod_template["spec"] = pod_spec
        pod_template["metadata"] = pod_metadata
        if secret is not None:
            pod_spec["imagePullSecrets"] = [
                {"name": f"regcred-{launch_project.run_id}"}
            ]
        job_spec["template"] = pod_template
        job_dict["spec"] = job_spec
        job_dict["metadata"] = job_metadata
        job_dict["status"] = job_status

        if not self.ack_run_queue_item(launch_project):
            return None

        _logger.info(f"Creating Kubernetes job from: {job_dict}")
        job_response = kubernetes.utils.create_from_yaml(
            api_client, yaml_objects=[job_dict], namespace=namespace
        )[0][
            0
        ]  # create_from_yaml returns a nested list of k8s objects
        job_name = job_response.metadata.labels["job-name"]

        pod_names = self.wait_job_launch(job_name, namespace, core_api)

        submitted_job = KubernetesSubmittedRun(
            batch_api, core_api, job_name, pod_names, namespace, secret
        )

        if self.backend_config[PROJECT_SYNCHRONOUS]:
            submitted_job.wait()

        return submitted_job


def maybe_create_imagepull_secret(
    core_api: "CoreV1Api",
    registry_config: Dict[str, Any],
    run_id: str,
    namespace: str,
) -> Optional["V1Secret"]:
    secret = None
    ecr_provider = registry_config.get("ecr-provider", "").lower()
    if (
        ecr_provider
        and ecr_provider == "aws"
        and registry_config.get("url") is not None
        and registry_config.get("credentials") is not None
    ):
        boto3 = get_module(
            "boto3", "AWS ECR requires boto3,  install with pip install wandb[launch]"
        )
        ecr_client = boto3.client("ecr")
        try:
            encoded_token = ecr_client.get_authorization_token()["authorizationData"][
                0
            ]["authorizationToken"]
            decoded_token = base64.b64decode(encoded_token.encode()).decode()
            uname, token = decoded_token.split(":")
        except Exception as e:
            raise LaunchError(f"Could not get authorization token for ECR, error: {e}")
        creds_info = {
            "auths": {
                registry_config.get("url"): {
                    "username": uname,
                    "password": token,
                    # need an email but the use is deprecated
                    "email": "deprecated@wandblaunch.com",
                    "auth": encoded_token,
                }
            }
        }
        secret_data = {
            ".dockerconfigjson": base64.b64encode(
                json.dumps(creds_info).encode()
            ).decode()
        }
        secret = client.V1Secret(
            data=secret_data,
            metadata=client.V1ObjectMeta(name=f"regcred-{run_id}", namespace=namespace),
            kind="Secret",
            type="kubernetes.io/dockerconfigjson",
        )
        try:
            core_api.create_namespaced_secret(namespace, secret)
        except Exception as e:
            raise LaunchError(f"Exception when creating Kubernetes secret: {str(e)}\n")
    # TODO: support other ecr providers
    elif ecr_provider and ecr_provider != "aws":
        raise LaunchError(f"Registry provider not supported: {ecr_provider}")
    return secret
