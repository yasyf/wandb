#!/usr/bin/env python
"""wandb setup."""

from setuptools import setup

with open("package_readme.md") as readme_file:
    readme = readme_file.read()

# with open("requirements.txt") as requirements_file:
#     requirements = requirements_file.read().splitlines()

with open("requirements_sweeps.txt") as sweeps_requirements_file:
    sweeps_requirements = sweeps_requirements_file.read().splitlines()


requirements = [
    "Click>=7.0,!=8.0.0",
    "GitPython>=1.0.0",
    "requests>=2.0.0,<3",
    "psutil>=5.0.0",
    "sentry-sdk>=1.0.0",
    "docker-pycreds>=0.4.0",
    "protobuf>=3.12.0,!=4.21.0,<5; python_version < '3.9' and sys_platform == 'linux'",
    "protobuf>=3.15.0,!=4.21.0,<5; python_version == '3.9' and sys_platform == 'linux'",
    "protobuf>=3.19.0,!=4.21.0,<5; python_version > '3.9' and sys_platform == 'linux'",
    "protobuf>=3.19.0,!=4.21.0,<5; sys_platform != 'linux'",
    "PyYAML",
    # supports vendored version of watchdog 0.9.0
    "pathtools",
    "setproctitle",
    "setuptools",
    "appdirs>=1.4.3",
    "dataclasses; python_version < '3.7'",
    "typing_extensions; python_version < '3.10'",
]

test_requirements = ["mock>=2.0.0", "tox-pyenv>=1.0.3"]

gcp_requirements = ["google-cloud-storage"]
aws_requirements = ["boto3"]
azure_requirements = ["azure-storage-blob"]
grpc_requirements = ["grpcio>=1.27.2"]
service_requirements = []
kubeflow_requirements = ["kubernetes", "minio", "google-cloud-storage", "sh"]
media_requirements = [
    "numpy",
    "moviepy",
    "pillow",
    "bokeh",
    "soundfile",
    "plotly",
    "rdkit-pypi",
]
launch_requirements = [
    "nbconvert",
    "nbformat",
    "chardet",
    "iso8601",
    "typing_extensions",
    "boto3",
    "botocore",
    "google-cloud-storage",
    "kubernetes",
]

models_requirements = ["cloudpickle"]


setup(
    name="wandb",
    version="0.13.11.dev1",
    description="A CLI and library for interacting with the Weights and Biases API.",
    long_description=readme,
    long_description_content_type="text/markdown",
    author="Weights & Biases",
    author_email="support@wandb.com",
    url="https://github.com/wandb/wandb",
    packages=["wandb"],
    package_dir={"wandb": "wandb"},
    package_data={"wandb": ["py.typed"]},
    entry_points={
        "console_scripts": [
            "wandb=wandb.cli.cli:cli",
            "wb=wandb.cli.cli:cli",
        ]
    },
    include_package_data=True,
    install_requires=requirements,
    license="MIT license",
    zip_safe=False,
    # keywords='wandb',
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Logging",
        "Topic :: System :: Monitoring",
    ],
    test_suite="tests",
    tests_require=test_requirements,
    extras_require={
        "kubeflow": kubeflow_requirements,
        "gcp": gcp_requirements,
        "aws": aws_requirements,
        "azure": azure_requirements,
        "service": service_requirements,
        "grpc": grpc_requirements,
        "media": media_requirements,
        "sweeps": sweeps_requirements,
        "launch": launch_requirements,
        "models": models_requirements,
    },
)

# if os.name == "nt" and sys.version_info >= (3, 6):
#     legacy_env_var = "PYTHONLEGACYWINDOWSSTDIO"
#     if legacy_env_var not in os.environ:
#         if os.system("setx " + legacy_env_var + " 1") != 0:
#             raise Exception("Error setting environment variable " + legacy_env_var)
