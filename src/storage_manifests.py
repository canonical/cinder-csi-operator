# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of cinder-csi specific details of the kubernetes manifests."""

import datetime
import logging
import pickle
from hashlib import md5
from typing import Dict, List, Optional, cast

import charms.proxylib
from lightkube import Client
from lightkube.codecs import AnyResource, from_dict
from lightkube.models.core_v1 import Event, Pod
from ops.interface_kube_control import KubeControlRequirer
from ops.manifests import (
    Addition,
    ConfigRegistry,
    HashableResource,
    ManifestLabel,
    Manifests,
    Patch,
)
from ops.manifests.manipulations import AnyCondition

log = logging.getLogger(__file__)
NAMESPACE = "kube-system"
SECRET_NAME = "csi-cinder-cloud-config"
STORAGE_CLASS_NAME = "csi-cinder-{type}"
OPENSTACK_METADATA_SERVER = "169.254.169.254"
K8S_DEFAULT_NO_PROXY = [
    "127.0.0.1",
    OPENSTACK_METADATA_SERVER,  # this should always skip the proxy
    "localhost",
    "::1",
    "svc",
    "svc.cluster",
    "svc.cluster.local",
]


class CreateSecret(Addition):
    """Create secret for the deployment.

    a secret named cloud-config in the kube-system namespace
    cloud.conf -- base64 encoded contents of cloud.conf
    endpoint-ca.cert -- base64 encoded ca cert for the auth-url
    """

    CONFIG_TO_SECRET = {"cloud-conf": "cloud.conf", "endpoint-ca-cert": "endpoint-ca.cert"}

    def __call__(self) -> Optional[AnyResource]:
        """Craft the secrets object for the deployment."""
        secret_config = {}
        for k, new_k in self.CONFIG_TO_SECRET.items():
            if value := self.manifests.config.get(k):
                secret_config[new_k] = value

        log.info("Encode secret data for storage.")
        return from_dict(
            dict(
                apiVersion="v1",
                kind="Secret",
                type="Opaque",
                metadata=dict(name=SECRET_NAME, namespace=NAMESPACE),
                data=secret_config,
            )
        )


class CreateStorageClass(Addition):
    """Create cinder storage class."""

    def __init__(self, manifests: "Manifests", sc_type: str):
        super().__init__(manifests)
        self.type = sc_type

    def __call__(self) -> Optional[AnyResource]:
        """Craft the storage class object."""
        storage_name = STORAGE_CLASS_NAME.format(type=self.type)
        log.info(f"Creating storage class {storage_name}")
        reclaim_policy: str = self.manifests.config.get("reclaim-policy") or "Delete"
        is_default: str = "true" if self.manifests.config.get("storage-class-default") else "false"

        sc = from_dict(
            dict(
                apiVersion="storage.k8s.io/v1",
                kind="StorageClass",
                metadata=dict(
                    name=storage_name,
                    annotations={"storageclass.kubernetes.io/is-default-class": is_default},
                ),
                provisioner="cinder.csi.openstack.org",
                reclaimPolicy=reclaim_policy.title(),
                volumeBindingMode="WaitForFirstConsumer",
            )
        )
        if az := self.manifests.config.get("availability-zone"):
            sc.parameters = dict(availability=az)
        return sc


class UpdateCSIDriver(Patch):
    """Update the Deployments and DaemonSets."""

    def __call__(self, obj):
        """Update the daemonsets and deployments."""
        if not any(
            [
                (obj.kind == "DaemonSet" and obj.metadata.name == "csi-cinder-nodeplugin"),
                (obj.kind == "Deployment" and obj.metadata.name == "csi-cinder-controllerplugin"),
            ]
        ):
            return

        log.info(f"Setting secret for {obj.kind}/{obj.metadata.name}")
        self._update_node_selector(obj)
        self._update_secrets(obj.spec.template.spec.volumes)
        self._update_pod_spec(obj.spec.template.spec.containers)

    def _update_node_selector(self, obj):
        """Update the node selector for the controllerplugin deployment."""
        if obj.kind != "Deployment" or obj.metadata.name != "csi-cinder-controllerplugin":
            return

        node_selector = cast(dict, self.manifests.config["control-node-selector"])
        node_selector_text = " ".join('{0}: "{1}"'.format(*t) for t in node_selector.items())
        log.info(f"Applying Control Node Selector as {node_selector_text}")
        obj.spec.template.spec.nodeSelector = node_selector

    def _update_secrets(self, volumes):
        """Update the volumes in the deployment or daemonset."""
        for volume in volumes:
            if volume.secret:
                volume.secret.secretName = SECRET_NAME

    def _update_pod_spec(self, containers):
        for container in containers:
            if container.name == "csi-provisioner":
                for i, val in enumerate(container.args):
                    if "feature-gates" in val.lower():
                        topology = str(self.manifests.config.get("topology")).lower()
                        container.args[i] = f"feature-gates=Topology={topology}"
                        log.info("Configuring cinder topology awareness=%s", topology)
            if container.name == "cinder-csi-plugin":
                for env in container.env:
                    if env.name == "CLUSTER_NAME":
                        env.value = self.manifests.config.get("cluster-name")

                enabled = self.manifests.config.get("web-proxy-enable")
                env = charms.proxylib.environ(enabled=enabled, add_no_proxies=K8S_DEFAULT_NO_PROXY)
                container.env.extend(charms.proxylib.container_vars(env))


class StorageManifests(Manifests):
    """Deployment Specific details for the cinder-csi-driver."""

    def __init__(self, charm, charm_config, kube_control: KubeControlRequirer, integrator):
        super().__init__(
            "cinder-csi-driver",
            charm.model,
            "upstream/cloud_storage",
            [
                CreateSecret(self),
                ManifestLabel(self),
                ConfigRegistry(self),
                CreateStorageClass(self, "default"),  # creates csi-cinder-default
                UpdateCSIDriver(self),  # update secrets, specs, env-vars
            ],
        )
        self.integrator = integrator
        self.charm_config = charm_config
        self.kube_control = kube_control

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        if labels := self.kube_control.get_controller_labels():
            controller_labels = {label.key: label.value for label in labels}
        else:
            # the controller labels are sourced from juju config on either
            # the k8s or kubernetes-control-plane charm.
            # These could represent empty labels (as in the default with k8s)
            # and can also just be empty if the user judges to remove them
            # in order to make sure the cinder controllers land on controller
            # nodes we can just fallback to this well-known label
            log.warning("No controller labels found, using fallback")
            controller_labels = {"juju-application": self.kube_control.relation.app.name}
        config = {
            "image-registry": self.kube_control.get_registry_location(),
            "cluster-name": self.kube_control.get_cluster_tag(),
            "cloud-conf": (val := self.integrator.cloud_conf_b64) and val.decode(),
            "control-node-selector": controller_labels,
            "endpoint-ca-cert": (val := self.integrator.endpoint_tls_ca) and val.decode(),
            **self.charm_config.available_data,
        }

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("storage-release", None)
        return config

    def hash(self) -> int:
        """Calculate a hash of the current configuration."""
        return int(md5(pickle.dumps(self.config)).hexdigest(), 16)

    def evaluate(self) -> Optional[str]:
        """Determine if manifest_config can be applied to manifests."""
        for prop in ["cloud-conf"]:
            if not self.config.get(prop):
                return f"Storage manifests waiting for definition of {prop}"
        return None

    def is_ready(self, obj: HashableResource, cond: AnyCondition) -> Optional[bool]:
        """Determine if the resource is ready."""
        is_ready = super().is_ready(obj, cond)
        if not is_ready:
            try:
                log_events(self.client, obj)
            except Exception as e:
                log.error("failed to log events: %s", e)

        return is_ready


def log_events(client: Client, obj: HashableResource) -> None:
    """Log events for the object."""
    object_events = collect_events(client, obj.resource)

    if obj.kind in ["Deployment", "DaemonSet"]:
        involved_pods = client.list(Pod, namespace=obj.namespace, labels={"app": obj.name})
        object_events += [event for pod in involved_pods for event in collect_events(client, pod)]

    for event in sorted(object_events, key=by_localtime):
        log.info(
            "Event %s/%s %s msg=%s",
            event.involvedObject.kind,
            event.involvedObject.name,
            event.lastTimestamp and event.lastTimestamp.astimezone() or "Date not recorded",
            event.message,
        )


def by_localtime(event: Event) -> datetime.datetime:
    """Return the last timestamp of the event in local time."""
    dt = event.lastTimestamp or datetime.datetime.now(datetime.timezone.utc)
    return dt.astimezone()


def collect_events(client: Client, resource: AnyResource) -> List[Event]:
    """Collect events from the resource."""
    kind: str = resource.kind or type(resource).__name__
    return list(
        client.list(
            Event,
            namespace=resource.metadata.namespace,
            fields={
                "involvedObject.kind": kind,
                "involvedObject.name": resource.metadata.name,
            },
        )
    )
