---
description: Learn how to run flows on Kubernetes using containers
tags:
    - kubernetes
    - containers
    - orchestration
    - infrastructure
    - deployments
search:
  boost: 2
---

# Running flows with Kubernetes

This guide will walk you through running your flows on Kubernetes.
Though much of the guide is general to any Kubernetes cluster, there are differences between the managed Kubernetes offerings between cloud providers, especially when it comes to container registries and access management.
We'll focus on Amazon Elastic Kubernetes Service (EKS).

## Prerequisites

Before we begin, there are a few pre-requisites:

1. A Prefect Cloud account
2. A cloud provider (AWS, GCP, or Azure) account
3. [Install](/getting-started/installation/) Python and Prefect
4. Install [Helm](https://helm.sh/docs/intro/install/)
5. Install the [Kubernetes CLI (kubectl)](https://kubernetes.io/docs/tasks/tools/install-kubectl/)

!!! Note "Administrator Access"
    Though not strictly necessary, you may want to ensure you have admin access, both in Prefect Cloud and in your cloud provider.
    Admin access is only necessary during the initial setup and can be downgraded after.

## Create a cluster

Let's start by creating a new cluster. If you already have one, skip ahead to the next section.

=== "AWS"

    One easy way to get set up with a cluster in EKS is with [`eksctl`](https://eksctl.io/). 
    Node pools can be backed by either EC2 instances or FARGATE. 
    Let's choose FARGATE so there's less to manage. 
    The following command takes around 15 minutes and must not be interrupted:

    ```bash
    # Replace the cluster name with your own value
    eksctl create cluster --fargate --name <CLUSTER-NAME>

    # Authenticate to the cluster.
    aws eks update-kubeconfig --name <CLUSTER-NAME>
    ```

=== "GCP"

    You can get a GKE cluster up and running with a few commands using the [`gcloud` CLI](https://cloud.google.com/sdk/docs/install). 
    We'll build a bare-bones cluster that is accessible over the open internet - this should **not** be used in a production environment. 
    To deploy the cluster, your project must have a VPC network configured.

    First, authenticate to GCP by setting the following configuration options.

    ```bash
    # Authenticate to gcloud
    gcloud auth login

    # Specify the project & zone to deploy the cluster to
    # Replace the project name with your GCP project name
    gcloud config set project <GCP-PROJECT-NAME>
    gcloud config set compute/zone <AVAILABILITY-ZONE>
    ```

    Next, deploy the cluster - this command will take ~15 minutes to complete. 
    Once the cluster has been created, authenticate to the cluster.

    ```bash
    # Create cluster
    # Replace the cluster name with your own value
    gcloud container clusters create <CLUSTER-NAME> --num-nodes=1 \
    --machine-type=n1-standard-2

    # Authenticate to the cluster
    gcloud container clusters <CLUSTER-NAME> --region <AVAILABILITY-ZONE>
    ```

    !!! Warning "GCP Gotchas"
      
      - You'll need to enable the default service account in the IAM console, or specify a different service account with the appropriate permissions to be used.

      ```
      ERROR: (gcloud.container.clusters.create) ResponseError: code=400, message=Service account "000000000000-compute@developer.gserviceaccount.com" is disabled.
      ```
      
      - Organization policy blocks creation of external (public) IPs. You can override this policy (if you have the appropriate permissions) under the `Organizational Policy` page within IAM.

      ```
      creation failed: Constraint constraints/compute.vmExternalIpAccess violated for project 000000000000. Add instance projects/<GCP-PROJECT-NAME>/zones/us-east1-b/instances/gke-gke-guide-1-default-pool-c369c84d-wcfl to the constraint to use external IP with it."
      ```

=== "Azure"

    You can quickly create an AKS cluster using the [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/get-started-with-azure-cli), or use the Cloud Shell directly from the Azure portal [shell.azure.com](https://shell.azure.com).

    First, authenticate to Azure if not already done.

    ```bash
      az login
    ```

    Next, deploy the cluster - this command will take ~4 minutes to complete. Once the cluster has been created, authenticate to the cluster.

    ```bash

      # Create a Resource Group at the desired location, e.g. westus
      az group create --name <RESOURCE-GROUP-NAME> --location <LOCATION>

      # Create a kubernetes cluster with default kubernetes version, default SKU load balancer (Standard) and default vm set type (VirtualMachineScaleSets)
      az aks create --resource-group <RESOURCE-GROUP-NAME> --name <CLUSTER-NAME>

      # Configure kubectl to connect to your Kubernetes cluster
      az aks get-credentials --resource-group <RESOURCE-GROUP-NAME> --name <CLUSTER-NAME>

      # Verify the connection by listing the cluster nodes
      kubectl get nodes
    ```

## Create a container registry

Besides a cluster, the other critical resource we'll need is a container registry.
A registry is not strictly required, but in most cases you'll want to use custom images and/or have more control over where images are stored.
If you already have a registry, skip ahead to the next section.

=== "AWS"

    Let's create a registry using the AWS CLI and authenticate the docker daemon to said registry:

    ```bash
    # Replace the image name with your own value
    aws ecr create-repository --repository-name <IMAGE-NAME>

    # Login to ECR
    # Replace the region and account ID with your own values
    aws ecr get-login-password --region <REGION> | docker login \
      --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com
    ```

=== "GCP"
    Let's create a registry using the gcloud CLI and authenticate the docker daemon to said registry:

    ```bash
    # Create artifact registry repository to host your custom image
    # Replace the repository name with your own value; it can be the 
    # same name as your image
    gcloud artifacts repositories create <REPOSITORY-NAME> \
    --repository-format=docker --location=us

    # Authenticate to artifact registry
    gcloud auth configure-docker us-docker.pkg.dev
    ```

=== "Azure"
    Let's create a registry using the Azure CLI and authenticate the docker daemon to said registry:

    ```bash
    # Name must be a lower-case alphanumeric
    # Tier SKU can easily be updated later, e.g. az acr update --name <REPOSITORY-NAME> --sku Standard
    az acr create --resource-group <RESOURCE-GROUP-NAME> \
      --name <REPOSITORY-NAME> \
      --sku Basic

    # Attach ACR to AKS cluster
    # You need Owner, Account Administrator, or Co-Administrator role on your Azure subscription as per Azure docs
    az aks update --resource-group <RESOURCE-GROUP-NAME> --name <CLUSTER-NAME> --attach-acr <REPOSITORY-NAME>

    # You can verify AKS can now reach ACR
    az aks check-acr --resource-group RESOURCE-GROUP-NAME> --name <CLUSTER-NAME> --acr <REPOSITORY-NAME>.azurecr.io

    ```

## Create a Kubernetes work pool

[Work pools](/concepts/work-pools/) allow you to manage deployment infrastructure.
We'll configure the default values for our Kubernetes base job template.
Note that these values can be overridden by individual deployments.

Let's switch to the Prefect Cloud UI, where we'll create a new Kubernetes work pool (alternatively, you could use the Prefect CLI to create a work pool).

1. Click on the **Work Pools** tab on the left sidebar
1. Click the **+** button at the top of the page
1. Select **Kubernetes** as the work pool type
1. Click **Next** to configure the work pool settings

Let's look at a few popular configuration options.

**Environment Variables**

Add environment variables to set when starting a flow run.
So long as you are using a Prefect-maintained image and haven't overwritten the image's entrypoint, you can specify Python packages to install at runtime with `{"EXTRA_PIP_PACKAGES":"my_package"}`.
For example `{"EXTRA_PIP_PACKAGES":"pandas==1.2.3"}` will install pandas version 1.2.3.
Alternatively, you can specify package installation in a custom Dockerfile, which can allow you to take advantage of image caching.
As we'll see below, Prefect can help us create a Dockerfile with our flow code and the packages specified in a `requirements.txt` file baked in.

**Namespace**

Set the Kubernetes namespace to create jobs within, such as `prefect`. By default, set to **default**.

**Image**

Specify the Docker container image for created jobs.
If not set, the latest Prefect 2 image will be used (i.e. `prefecthq/prefect:2-latest`).
Note that you can override this on each deployment through `job_variables`.

**Image Pull Policy**

Select from the dropdown options to specify when to pull the image.
When using the `IfNotPresent` policy, make sure to use unique image tags, as otherwise old images could get cached on your nodes.

**Finished Job TTL**

Number of seconds before finished jobs are automatically cleaned up by Kubernetes' controller.
You may want to set to 60 so that completed flow runs are cleaned up after a minute.

**Pod Watch Timeout Seconds**

Number of seconds for pod creation to complete before timing out.
Consider setting to 300, especially if using a **serverless** type node pool, as these tend to have longer startup times.

**Kubernetes Cluster Config**

You can configure the Kubernetes cluster to use for job creation by specifying a `KubernetesClusterConfig` block.
Generally you should leave the cluster config blank as the worker should be provisioned with appropriate access and permissions.
Typically this setting is used when a worker is deployed to a cluster that is different from the cluster where flow runs are executed.

!!! Note "Advanced Settings"
    Want to modify the default base job template to add other fields or delete existing fields?

    Select the **Advanced** tab and edit the JSON representation of the base job template.

    For example, to set a CPU request, add the following section under variables:

    ```json
    "cpu_request": {
      "title": "CPU Request",
      "description": "The CPU allocation to request for this pod.",
      "default": "default",
      "type": "string"
    },
    ```

    Next add the following to the first `containers` item under `job_configuration`:

    ```json
    ...
    "containers": [
      {
        ...,
        "resources": {
          "requests": {
            "cpu": "{{ cpu_request }}"
          }
        }
      }
    ],
    ...
    ```

    Running deployments with this work pool will now request the specified CPU.

After configuring the work pool settings, move to the next screen.

Give the work pool a name and save.

Our new Kubernetes work pool should now appear in the list of work pools.

## Create a Prefect Cloud API key

While in the Prefect Cloud UI, create a Prefect Cloud API key if you don't already have one.
Click on your profile avatar picture, then click your name to go to your profile settings, click [API Keys](https://app.prefect.cloud/my/api-keys) and hit the plus button to create a new API key here.
Make sure to store it safely along with your other passwords, ideally via a password manager.

## Deploy a worker using Helm

With our cluster and work pool created, it's time to deploy a worker, which will set up Kubernetes infrastructure to run our flows.
The best way to deploy a worker is using the [Prefect Helm Chart](https://github.com/PrefectHQ/prefect-helm/tree/main/charts/prefect-worker).

### Add the Prefect Helm repository

Add the Prefect Helm repository to your Helm client:

```bash
helm repo add prefect https://prefecthq.github.io/prefect-helm
helm repo update
```

### Create a namespace

Create a new namespace in your Kubernetes cluster to deploy the Prefect worker:

```bash
kubectl create namespace prefect
```

### Create a Kubernetes secret for the Prefect API key

```bash
kubectl create secret generic prefect-api-key \
--namespace=prefect --from-literal=key=your-prefect-cloud-api-key
```

### Configure Helm chart values

Create a `values.yaml` file to customize the Prefect worker configuration.
Add the following contents to the file:

```yaml
worker:
  cloudApiConfig:
    accountId: <target account ID>
    workspaceId: <target workspace ID>
  config:
    workPool: <target work pool name>
```

These settings will ensure that the worker connects to the proper account, workspace, and work pool.

View your Account ID and Workspace ID in your browser URL when logged into Prefect Cloud.
For example: <https://app.prefect.cloud/account/abc-my-account-id-is-here/workspaces/123-my-workspace-id-is-here>.

### Create a Helm release

Let's install the Prefect worker using the Helm chart with your custom `values.yaml` file:

```bash
helm install prefect-worker prefect/prefect-worker \
  --namespace=prefect \
  -f values.yaml
```

### Verify deployment

Check the status of your Prefect worker deployment:

```bash
kubectl get pods -n prefect
```

## Define a flow

Let's start simple with a flow that just logs a message.
In a directory named `flows`, create a file named `hello.py` with the following contents:

```python
from prefect import flow, get_run_logger, tags

@flow
def hello(name: str = "Marvin"):
    logger = get_run_logger()
    logger.info(f"Hello, {name}!")

if __name__ == "__main__":
    with tags("local"):
        hello()
```

Run the flow locally with `python hello.py` to verify that it works.
Note that we use the `tags` context manager to tag the flow run as `local`.
This step is not required, but does add some helpful metadata.

## Define a Prefect deployment

Prefect has two recommended options for creating a deployment with dynamic infrastructure.
You can define a deployment in a Python script using the `flow.deploy` mechanics or in a `prefect.yaml` definition file.
The `prefect.yaml` file currently allows for more customization in terms of push and pull steps.
Kubernetes objects are defined in YAML, so we expect many teams using Kubernetes work pools to create their deployments with YAML as well.
To learn about the Python deployment creation method with `flow.deploy` refer to the [Workers & Work Pools tutorial page](/tutorial/workers/).

The [`prefect.yaml`](/concepts/deployments/#managing-deployments) file is used by the `prefect deploy` command to deploy our flows.
As a part of that process it will also build and push our image.
Create a new file named `prefect.yaml` with the following contents:

```yaml
# Generic metadata about this project
name: flows
prefect-version: 2.13.8

# build section allows you to manage and build docker images
build:
- prefect_docker.deployments.steps.build_docker_image:
    id: build-image
    requires: prefect-docker>=0.4.0
    image_name: "{{ $PREFECT_IMAGE_NAME }}"
    tag: latest
    dockerfile: auto
    platform: "linux/amd64"

# push section allows you to manage if and how this project is uploaded to remote locations
push:
- prefect_docker.deployments.steps.push_docker_image:
    requires: prefect-docker>=0.4.0
    image_name: "{{ build-image.image_name }}"
    tag: "{{ build-image.tag }}"

# pull section allows you to provide instructions for cloning this project in remote locations
pull:
- prefect.deployments.steps.set_working_directory:
    directory: /opt/prefect/flows

# the definitions section allows you to define reusable components for your deployments
definitions:
  tags: &common_tags
    - "eks"
  work_pool: &common_work_pool
    name: "kubernetes"
    job_variables:
      image: "{{ build-image.image }}"

# the deployments section allows you to provide configuration for deploying flows
deployments:
- name: "default"
  tags: *common_tags
  schedule: null
  entrypoint: "flows/hello.py:hello"
  work_pool: *common_work_pool

- name: "arthur"
  tags: *common_tags
  schedule: null
  entrypoint: "flows/hello.py:hello"
  parameters:
    name: "Arthur"
  work_pool: *common_work_pool
```

We define two deployments of the `hello` flow: `default` and `arthur`.
Note that by specifying `dockerfile: auto`, Prefect will automatically create a dockerfile that installs any `requirements.txt` and copies over the current directory.
You can pass a custom Dockerfile instead with `dockerfile: Dockerfile` or `dockerfile: path/to/Dockerfile`.
Also note that we are specifically building for the `linux/amd64` platform.
This specification is often necessary when images are built on Macs with M series chips but run on cloud provider instances.

!!! note "Deployment specific build, push, and pull"
    The build, push, and pull steps can be overridden for each deployment.
    This allows for more custom behavior, such as specifying a different image for each deployment.

Let's make sure we define our requirements in a `requirements.txt` file:

```
prefect>=2.13.8
prefect-docker>=0.4.0
prefect-kubernetes>=0.3.1
```

The directory should now look something like this:

```
.
├── prefect.yaml
└── flows
    ├── requirements.txt
    └── hello.py
```

### Tag images with a Git SHA

If your code is stored in a GitHub repository, it's good practice to tag your images with the Git SHA of the code used to build it.
This can be done in the `prefect.yaml` file with a few minor modifications, and isn't yet an option with the Python deployment creation method.
Let's use the `run_shell_script` command to grab the SHA and pass it to the `tag` parameter of `build_docker_image`:

```yaml hl_lines="2-5 10"
build:
- prefect.deployments.steps.run_shell_script:
    id: get-commit-hash
    script: git rev-parse --short HEAD
    stream_output: false
- prefect_docker.deployments.steps.build_docker_image:
    id: build-image
    requires: prefect-docker>=0.4.0
    image_name: "{{ $PREFECT_IMAGE_NAME }}"
    tag: "{{ get-commit-hash.stdout }}"
    dockerfile: auto
    platform: "linux/amd64"
```

Let's also set the SHA as a tag for easy identification in the UI:

```yaml hl_lines="4"
definitions:
  tags: &common_tags
    - "eks"
    - "{{ get-commit-hash.stdout }}"
  work_pool: &common_work_pool
    name: "kubernetes"
    job_variables:
      image: "{{ build-image.image }}"
```

## Authenticate to Prefect

Before we deploy the flows to Prefect, we will need to authenticate via the Prefect CLI.
We will also need to ensure that all of our flow's dependencies are present at `deploy` time.

This example uses a virtual environment to ensure consistency across environments.

```bash
# Create a virtualenv & activate it
virtualenv prefect-demo
source prefect-demo/bin/activate

# Install dependencies of your flow
prefect-demo/bin/pip install -r requirements.txt

# Authenticate to Prefect & select the appropriate 
# workspace to deploy your flows to
prefect-demo/bin/prefect cloud login
```

## Deploy the flows

Now we're ready to deploy our flows which will build our images.
The image name determines which registry it will end up in.
We have configured our `prefect.yaml` file to get the image name from the `PREFECT_IMAGE_NAME` environment variable, so let's set that first:

=== "AWS"

    ```bash
    export PREFECT_IMAGE_NAME=<AWS_ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/<IMAGE-NAME>
    ```

=== "GCP"

    ```bash
    export PREFECT_IMAGE_NAME=us-docker.pkg.dev/<GCP-PROJECT-NAME>/<REPOSITORY-NAME>/<IMAGE-NAME>
    ```

=== "Azure"

    ```bash
    export PREFECT_IMAGE_NAME=<REPOSITORY-NAME>.azurecr.io/<IMAGE-NAME>
    ```

To deploy your flows, ensure your Docker daemon is running first. Deploy all the flows with `prefect deploy --all` or deploy them individually by name: `prefect deploy -n hello/default` or `prefect deploy -n hello/arthur`.

## Run the flows

Once the deployments are successfully created, we can run them from the UI or the CLI:

```bash
prefect deployment run hello/default
prefect deployment run hello/arthur
```

Congratulations!
You just ran two deployments in Kubernetes.
Head over to the UI to check their status!

<!-- ### Example: cpu and memory example -->
