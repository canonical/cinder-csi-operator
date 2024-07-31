# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of cinder-csi specific details of the kubernetes manifests."""

import logging
import pickle
from hashlib import md5
from typing import Dict, Optional

from lightkube.codecs import AnyResource, from_dict
from ops.manifests import Addition, ConfigRegistry, ManifestLabel, Manifests, Patch

log = logging.getLogger(__file__)
NAMESPACE = "kube-system"
SECRET_NAME = "csi-cinder-cloud-config"
STORAGE_CLASS_NAME = "csi-cinder-{type}"


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
                secret_config[new_k] = value.decode()

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


class UpdateSecrets(Patch):
    """Update the secret name in Deployments and DaemonSets."""

    def __call__(self, obj):
        """Update the secret volume spec in daemonsets and deployments."""
        if not any(
            [
                (obj.kind == "DaemonSet" and obj.metadata.name == "csi-cinder-nodeplugin"),
                (obj.kind == "Deployment" and obj.metadata.name == "csi-cinder-controllerplugin"),
            ]
        ):
            return

        for volume in obj.spec.template.spec.volumes:
            if volume.secret:
                volume.secret.secretName = SECRET_NAME
                log.info(f"Setting secret for {obj.kind}/{obj.metadata.name}")


class StorageManifests(Manifests):
    """Deployment Specific details for the cinder-csi-driver."""

    def __init__(self, charm, charm_config, kube_control, integrator):
        super().__init__(
            "cinder-csi-driver",
            charm.model,
            "upstream/cloud_storage",
            [
                CreateSecret(self),
                ManifestLabel(self),
                ConfigRegistry(self),
                CreateStorageClass(self, "default"),  # creates csi-cinder-default
                UpdateSecrets(self),  # update secrets
                UpdateControllerPlugin(self),
            ],
        )
        self.integrator = integrator
        self.charm_config = charm_config
        self.kube_control = kube_control

    @property
    def config(self) -> Dict:
        """Returns current config available from charm config and joined relations."""
        config: Dict = {}

        if self.kube_control.is_ready:
            config["image-registry"] = self.kube_control.get_registry_location()

        if self.integrator.is_ready:
            config["cloud-conf"] = self.integrator.cloud_conf_b64
            config["endpoint-ca-cert"] = self.integrator.endpoint_tls_ca

        config.update(**self.charm_config.available_data)

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


class UpdateControllerPlugin(Patch):
    """Update the controller args in Deployments."""

    def __call__(self, obj):
        """Update the controller args in Deployments."""
        if not (obj.kind == "Deployment" and obj.metadata.name == "csi-cinder-controllerplugin"):
            return

        for container in obj.spec.template.spec.containers:
            if container.name == "csi-provisioner":
                for i, val in enumerate(container.args):
                    if "feature-gates" in val.lower():
                        topology = str(self.manifests.config.get("topology")).lower()
                        container.args[i] = f"feature-gates=Topology={topology}"
                        log.info("Configuring cinder topology awareness=%s", topology)
