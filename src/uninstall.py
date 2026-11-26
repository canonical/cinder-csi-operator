# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Remove a drained Cinder CSI installation without deleting storage data."""

from lightkube import ALL_NS, ApiError, Client
from lightkube.resources.apps_v1 import DaemonSet, Deployment
from lightkube.resources.core_v1 import (
    PersistentVolume,
    PersistentVolumeClaim,
    Pod,
    Secret,
    ServiceAccount,
)
from lightkube.resources.rbac_authorization_v1 import ClusterRole, ClusterRoleBinding
from lightkube.resources.storage_v1 import CSIDriver, StorageClass, VolumeAttachment
from ops.manifests.literals import APP_LABEL, MANIFEST_LABEL

INSTALLATION = "cinder-csi.charmhub.io/installation"
INSTALLED_UID = "cinder-csi.charmhub.io/installed-uid"
DRIVER = "cinder.csi.openstack.org"
NAMESPACE = "kube-system"
# All supported releases use these kinds, including the generated Secret and StorageClass.
RESOURCE_TYPES = (
    (Deployment, NAMESPACE),
    (DaemonSet, NAMESPACE),
    (CSIDriver, None),
    (StorageClass, None),
    (Secret, NAMESPACE),
    (ClusterRoleBinding, None),
    (ClusterRole, None),
    (ServiceAccount, NAMESPACE),
)


class UninstallError(Exception):
    """The installation cannot safely be removed."""


def check_driver_unused(client: Client) -> None:
    """Reject attachments, pending provisioning and pods that still use Cinder."""
    for attachment in client.list(VolumeAttachment):
        if attachment.spec.attacher == DRIVER:
            raise UninstallError(
                f"Cinder VolumeAttachment still exists: {attachment.metadata.name}"
            )

    classes = [sc for sc in client.list(StorageClass) if sc.provisioner == DRIVER]
    class_names = {sc.metadata.name for sc in classes}
    is_default = any(
        (sc.metadata.annotations or {}).get("storageclass.kubernetes.io/is-default-class")
        == "true"
        for sc in classes
    )
    for claim in client.list(PersistentVolumeClaim, namespace=ALL_NS):
        storage_class = claim.spec.storageClassName
        if storage_class in class_names or (storage_class is None and is_default):
            if claim.status is None or claim.status.phase in (None, "Pending"):
                raise UninstallError(
                    f"Cinder claim still provisioning: "
                    f"{claim.metadata.namespace}/{claim.metadata.name}"
                )
    _check_pod_users(client)


def _check_pod_users(client: Client) -> None:
    claims = {
        (pv.spec.claimRef.namespace, pv.spec.claimRef.name)
        for pv in client.list(PersistentVolume)
        if pv.spec.csi and pv.spec.csi.driver == DRIVER and pv.spec.claimRef
    }
    for pod in client.list(Pod, namespace=ALL_NS):
        if pod.status and pod.status.phase in ("Succeeded", "Failed"):
            continue
        for volume in pod.spec.volumes or ():
            pvc = volume.persistentVolumeClaim
            claim = pvc.claimName if pvc else None
            if volume.ephemeral:
                claim = f"{pod.metadata.name}-{volume.name}"
            if (volume.csi and volume.csi.driver == DRIVER) or (
                claim and (pod.metadata.namespace, claim) in claims
            ):
                raise UninstallError(
                    f"Pod still uses Cinder: {pod.metadata.namespace}/{pod.metadata.name}"
                )


def installed_objects(client: Client, app: str, manifest: str, installation: str):
    """Find current-installation objects and unmarked legacy candidates, not other installs."""
    labels = {APP_LABEL: app, MANIFEST_LABEL: manifest}
    for kind, namespace in RESOURCE_TYPES:
        for obj in client.list(kind, namespace=namespace, labels={INSTALLATION: installation}):
            recorded_uid = (obj.metadata.annotations or {}).get(INSTALLED_UID)
            yield obj, not recorded_uid or recorded_uid != obj.metadata.uid
        for obj in client.list(kind, namespace=namespace, labels=labels):
            if not (obj.metadata.labels or {}).get(INSTALLATION):
                yield obj, True


def delete_instance(client: Client, obj) -> None:
    """Delete only the listed instance, even if its name was reused after listing."""
    meta = obj.metadata
    if not meta.uid:
        raise UninstallError(f"No UID for {type(obj).__name__}/{meta.name}")
    # The pinned Lightkube Client.delete does not expose a DeleteOptions body.
    wire = client._client  # noqa: SLF001
    request = wire.prepare_request(
        "delete", res=type(obj), name=meta.name, namespace=meta.namespace
    )
    request.data = {
        "kind": "DeleteOptions",
        "apiVersion": "v1",
        "preconditions": {"uid": meta.uid},
    }
    try:
        wire.raise_for_status(wire.send(wire.build_adapter_request(request)))
    except ApiError as exc:
        if exc.response.status_code != 404:
            raise
