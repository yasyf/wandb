import random
import threading
import time
from typing import Union
from unittest import mock

import wandb
from wandb.sdk.internal.settings_static import SettingsStatic
from wandb.sdk.internal.system.assets import OpenMetrics
from wandb.sdk.internal.system.assets.interfaces import Asset
from wandb.sdk.internal.system.system_monitor import AssetInterface


def random_in_range(vmin: Union[int, float] = 0, vmax: Union[int, float] = 100):
    return random.random() * (vmax - vmin) + vmin


FAKE_METRICS = """# HELP DCGM_FI_DEV_MEM_COPY_UTIL Memory utilization (in %).
# TYPE DCGM_FI_DEV_MEM_COPY_UTIL gauge
DCGM_FI_DEV_MEM_COPY_UTIL{{gpu="0",UUID="GPU-c601d117-58ff-cd30-ae20-529ab192ba51",device="nvidia0",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="",namespace="",pod=""}} {gpu_0_memory_utilization}
DCGM_FI_DEV_MEM_COPY_UTIL{{gpu="1",UUID="GPU-a7c8aa83-d112-b585-8456-5fc2f3e6d18e",device="nvidia1",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="dcgm-loadtest",namespace="default",pod="dcgm-loadtest"}} {gpu_1_memory_utilization}
# HELP DCGM_FI_DEV_GPU_TEMP GPU temperature (in C)
# TYPE DCGM_FI_DEV_GPU_TEMP gauge
DCGM_FI_DEV_GPU_TEMP{{gpu="0",UUID="GPU-c601d117-58ff-cd30-ae20-529ab192ba51",device="nvidia0",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="",namespace="",pod=""}} {gpu_0_temperature_c}
DCGM_FI_DEV_GPU_TEMP{{gpu="1",UUID="GPU-a7c8aa83-d112-b585-8456-5fc2f3e6d18e",device="nvidia1",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="dcgm-loadtest",namespace="default",pod="dcgm-loadtest"}} {gpu_1_temperature_c}
# HELP DCGM_FI_DEV_POWER_USAGE Power draw (in W).
# TYPE DCGM_FI_DEV_POWER_USAGE gauge
DCGM_FI_DEV_POWER_USAGE{{gpu="0",UUID="GPU-c601d117-58ff-cd30-ae20-529ab192ba51",device="nvidia0",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="",namespace="",pod=""}} {gpu_0_power_draw_w}
DCGM_FI_DEV_POWER_USAGE{{gpu="1",UUID="GPU-a7c8aa83-d112-b585-8456-5fc2f3e6d18e",device="nvidia1",modelName="Tesla T4",Hostname="gke-gke-dcgm-default-pool-eb7746d2-6vkd",container="dcgm-loadtest",namespace="default",pod="dcgm-loadtest"}} {gpu_1_power_draw_w}
"""


def random_metrics():
    return FAKE_METRICS.format(
        gpu_0_memory_utilization=random_in_range(),
        gpu_1_memory_utilization=random_in_range(),
        gpu_0_temperature_c=random_in_range(0, 100),
        gpu_1_temperature_c=random_in_range(0, 100),
        gpu_0_power_draw_w=random_in_range(0, 250),
        gpu_1_power_draw_w=random_in_range(0, 250),
    )


def mocked_requests_get(*args, **kwargs):
    return mock.Mock(
        status_code=200,
        text=random_metrics(),
    )


def test_dcgm(test_settings):
    with mock.patch.object(
        wandb.sdk.internal.system.assets.open_metrics.requests,
        "get",
        mocked_requests_get,
    ), mock.patch.object(
        wandb.sdk.internal.system.assets.open_metrics.requests.Session,
        "get",
        mocked_requests_get,
    ):

        interface = AssetInterface()
        settings = SettingsStatic(
            test_settings(
                dict(
                    _stats_sample_rate_seconds=1,
                    _stats_samples_to_average=1,
                )
            ).make_static()
        )
        shutdown_event = threading.Event()

        url = "http://localhost:9400/metrics"

        dcgm = OpenMetrics(
            interface=interface,
            settings=settings,
            shutdown_event=shutdown_event,
            name="dcgm",
            url=url,
        )

        assert dcgm.is_available(url)
        assert isinstance(dcgm, Asset)

        dcgm.start()

        # wait for the mock data to be processed indefinitely,
        # until the test times out in the worst case
        while interface.metrics_queue.empty():
            time.sleep(0.1)

        shutdown_event.set()
        dcgm.finish()

        assert not interface.metrics_queue.empty()
        assert not interface.telemetry_queue.empty()

        while not interface.metrics_queue.empty():
            print(interface.metrics_queue.get())
