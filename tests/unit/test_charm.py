# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
import unittest.mock as mock
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from lightkube import ApiError, Client
from ops.model import BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import ActionFailed, Harness

from charm import CinderCSICharm
from uninstall import DRIVER, INSTALLATION, INSTALLED_UID


@pytest.fixture
def harness():
    harness = Harness(CinderCSICharm)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture(autouse=True)
def mock_kubeconfig(tmpdir):
    kubeconfig = Path(tmpdir) / "kubeconfig"
    with mock.patch.object(
        CinderCSICharm, "_kubeconfig_path", new_callable=mock.PropertyMock(return_value=kubeconfig)
    ):
        yield kubeconfig


@pytest.fixture(autouse=True)
def mock_ca_cert(tmpdir):
    ca_cert = Path(tmpdir) / "ca.crt"
    with mock.patch.object(
        CinderCSICharm, "_ca_cert_path", new_callable=mock.PropertyMock(return_value=ca_cert)
    ):
        yield ca_cert


@pytest.fixture()
def integrator():
    with mock.patch("charm.OpenstackIntegrationRequirer") as mocked:
        integrator = mocked.return_value
        integrator.evaluate_relation.return_value = None
        integrator.cloud_conf_b64 = b"abc"
        integrator.endpoint_tls_ca = b"def"
        integrator.proxy_config = {}
        yield integrator


@pytest.fixture()
def certificates():
    with mock.patch("charm.CertificatesRequires") as mocked:
        certificates = mocked.return_value
        certificates.ca = "abcd"
        certificates.evaluate_relation.return_value = None
        yield certificates


@pytest.fixture()
def kube_control():
    with mock.patch("charm.KubeControlRequirer") as mocked:
        kube_control = mocked.return_value
        kube_control.evaluate_relation.return_value = None
        kube_control.get_registry_location.return_value = "ghcr.io/canonical/cdk"
        kube_control.get_controller_taints.return_value = []
        kube_control.get_controller_labels.return_value = []
        kube_control.relation.app.name = "kubernetes-control-plane"
        kube_control.relation.units = [f"kubernetes-control-plane/{_}" for _ in range(2)]
        yield kube_control


def test_waits_for_integrator(harness):
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required openstack"

    # Test adding the integrator relation
    rel_cls = type(charm.integrator)
    rel_cls.relation = property(rel_cls.relation.func)
    rel_cls._data = property(rel_cls._data.func)
    rel_cls._raw_data = property(rel_cls._raw_data.func)
    rel_id = harness.add_relation("openstack", "openstack-integrator")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for openstack"
    harness.add_relation_unit(rel_id, "openstack-integrator/0")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for openstack"
    harness.update_relation_data(
        rel_id,
        "openstack-integrator/0",
        yaml.safe_load(Path("tests/data/openstack_data.yaml").read_text()),
    )
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required certificates"


@pytest.mark.usefixtures("integrator")
def test_waits_for_certificates(harness):
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required certificates"

    # Test adding the certificates relation
    rel_cls = type(charm.certificates)
    rel_cls.relation = property(rel_cls.relation.func)
    rel_cls._data = property(rel_cls._data.func)
    rel_cls._raw_data = property(rel_cls._raw_data.func)
    rel_id = harness.add_relation("certificates", "easyrsa")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for certificates"
    harness.add_relation_unit(rel_id, "easyrsa/0")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for certificates"
    harness.update_relation_data(
        rel_id,
        "easyrsa/0",
        yaml.safe_load(Path("tests/data/certificates_data.yaml").read_text()),
    )
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required kube-control relation"


@mock.patch("ops.interface_kube_control.KubeControlRequirer.create_kubeconfig")
@pytest.mark.usefixtures("integrator", "certificates")
def test_waits_for_kube_control(mock_create_kubeconfig, harness, caplog, lk_client):
    lk_client.get.side_effect = ApiError(
        response=httpx.Response(404, json={"kind": "Status", "message": "Not found"})
    )
    lk_client.create.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="created", annotations=None, resourceVersion="1")
    )
    harness.begin_with_initial_hooks()
    charm = harness.charm
    assert isinstance(charm.unit.status, BlockedStatus)
    assert charm.unit.status.message == "Missing required kube-control relation"

    # Add the kube-control relation
    rel_cls = type(charm.kube_control)
    rel_cls.relation = property(rel_cls.relation.func)
    rel_cls._data = property(rel_cls._data.func)
    rel_id = harness.add_relation("kube-control", "kubernetes-control-plane")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for kube-control relation"

    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")
    assert isinstance(charm.unit.status, WaitingStatus)
    assert charm.unit.status.message == "Waiting for kube-control relation"
    mock_create_kubeconfig.assert_not_called()

    caplog.clear()
    harness.update_relation_data(
        rel_id,
        "kubernetes-control-plane/0",
        yaml.safe_load(Path("tests/data/kube_control_data.yaml").read_text()),
    )
    mock_create_kubeconfig.assert_called_once_with(
        charm._ca_cert_path, charm._kubeconfig_path, "root", charm.unit.name
    )
    assert charm.unit.status == MaintenanceStatus("Deploying Cinder Storage")
    storage_messages = {r.message for r in caplog.records if "storage" in r.filename}

    assert storage_messages == {
        'Applying Control Node Selector as node-role.kubernetes.io/control-plane: ""',
        "Encode secret data for storage.",
        "Setting Control Node tolerations",
        "Creating storage class csi-cinder-default",
        "Setting secret for DaemonSet/csi-cinder-nodeplugin",
        "Setting secret for Deployment/csi-cinder-controllerplugin",
        "Configuring cinder topology awareness=true",
    }

    caplog.clear()


@pytest.fixture
def ready_cinder(harness, mock_kubeconfig, mock_ca_cert):
    harness.set_leader(True)
    harness.begin()
    harness.add_relation("cinder-peers", "cinder-csi")
    harness.add_relation("kube-control", "kubernetes-control-plane")
    mock_kubeconfig.write_text("""current-context: test
clusters:
- name: test
  cluster: {server: https://k8s.example}
contexts:
- name: test
  context: {cluster: test, user: test}
users:
- name: test
  user: {token: test}
""")
    mock_ca_cert.write_text("test CA")
    return harness.charm


def test_stop_after_detaching_never_contacts_kubernetes(
    harness, ready_cinder, mock_kubeconfig, mock_ca_cert, lk_client, monkeypatch
):
    relation = ready_cinder.model.get_relation("kube-control")
    harness.remove_relation(relation.id)
    unrelated = mock_kubeconfig.parent / "unrelated"
    unrelated.touch()
    monkeypatch.setattr(
        "charm.Client", mock.Mock(side_effect=AssertionError("API call from stop"))
    )

    ready_cinder.on.stop.emit()
    ready_cinder.on.stop.emit()

    assert not mock_kubeconfig.exists()
    assert not mock_ca_cert.exists()
    assert unrelated.exists()
    assert not lk_client.mock_calls


SECRET = "/api/v1/namespaces/kube-system/secrets"
ATTACHMENTS = "/apis/storage.k8s.io/v1/volumeattachments"


@pytest.fixture
def cluster(ready_cinder, monkeypatch):
    app_labels = {"juju.io/application": "cinder-csi", "juju.io/manifest": "cinder-csi-driver"}
    installation = ready_cinder.storage_manifests.installation
    objects = {
        SECRET: [
            {
                "metadata": {
                    "name": "csi-cinder-cloud-config",
                    "namespace": "kube-system",
                    "uid": "own-uid",
                    "labels": {**app_labels, INSTALLATION: installation},
                    "annotations": {INSTALLED_UID: "own-uid"},
                },
            },
            {
                "metadata": {
                    "name": "foreign",
                    "namespace": "kube-system",
                    "uid": "foreign-uid",
                    "labels": {**app_labels, INSTALLATION: "another-installation"},
                },
            },
        ],
    }

    def api(request):
        path = request.url.path
        if request.method == "DELETE":
            collection, name = path.rsplit("/", 1)
            uid = json.loads(request.content)["preconditions"]["uid"]
            entries = objects[collection]
            for entry in entries:
                if entry["metadata"]["name"] != name:
                    continue
                if replacement_uid := entry.pop("replace-on-delete", None):
                    entry["metadata"]["uid"] = replacement_uid
                if entry["metadata"]["uid"] != uid:
                    return httpx.Response(409, json={"kind": "Status", "message": "UID mismatch"})
                entries.remove(entry)
                return httpx.Response(200, json={"kind": "Status"})
            return httpx.Response(404, json={"kind": "Status", "message": "Not found"})
        selector = dict(
            pair.split("=")
            for pair in request.url.params.get("labelSelector", "").split(",")
            if pair
        )
        return httpx.Response(
            200,
            json={
                "metadata": {},
                "items": [
                    obj
                    for obj in objects.get(path, [])
                    if selector.items() <= obj["metadata"].get("labels", {}).items()
                ],
            },
        )

    monkeypatch.setattr("charm.Client", partial(Client, transport=httpx.MockTransport(api)))
    return objects


def test_uninstall_waits_for_peers_and_preserves_foreign_objects(harness, ready_cinder, cluster):
    peer_id = ready_cinder.model.get_relation("cinder-peers").id
    harness.add_relation_unit(peer_id, "cinder-csi/1")
    harness.run_action("prepare-uninstall")

    assert harness.run_action("uninstall").results["state"] == "waiting"
    assert len(cluster[SECRET]) == 2
    with pytest.raises(ActionFailed, match="reconciliation is paused"):
        harness.run_action("sync-resources")

    harness.update_relation_data(peer_id, "cinder-csi/1", {"uninstall-paused": "true"})
    assert harness.run_action("uninstall").results["state"] == "removed"
    assert [obj["metadata"]["uid"] for obj in cluster[SECRET]] == ["foreign-uid"]
    assert harness.run_action("uninstall").results["state"] == "removed"


@pytest.mark.parametrize("prepared", [False, True])
def test_uninstall_rejects_live_cinder_attachment(harness, cluster, prepared):
    if prepared:
        harness.run_action("prepare-uninstall")
    cluster[ATTACHMENTS] = [
        {
            "metadata": {"name": "in-use"},
            "spec": {"attacher": DRIVER, "nodeName": "node", "source": {}},
        }
    ]

    with pytest.raises(ActionFailed, match="Cinder VolumeAttachment still exists"):
        harness.run_action("uninstall" if prepared else "prepare-uninstall")
    assert len(cluster[SECRET]) == 2


def test_uninstall_requires_exact_legacy_uid_confirmation(harness, cluster):
    # A same-name replacement copied the installation marker, but not its Kubernetes UID.
    cluster[SECRET][0]["metadata"]["uid"] = "replacement"
    harness.run_action("prepare-uninstall")

    with pytest.raises(ActionFailed, match="Verify the listed legacy objects") as failed:
        harness.run_action("uninstall", {"legacy-uids": "own-uid"})
    assert failed.value.output.results["legacy-objects"].endswith(" replacement")
    assert len(cluster[SECRET]) == 2

    assert harness.run_action("uninstall", {"legacy-uids": "replacement"}).results == {
        "state": "removed"
    }
    assert [obj["metadata"]["uid"] for obj in cluster[SECRET]] == ["foreign-uid"]


def test_uninstall_preserves_objects_replaced_between_list_and_delete(harness, cluster):
    harness.run_action("prepare-uninstall")
    cluster[SECRET][0]["replace-on-delete"] = "replacement"

    with pytest.raises(ActionFailed, match="UID mismatch"):
        harness.run_action("uninstall")

    assert [obj["metadata"]["uid"] for obj in cluster[SECRET]] == ["replacement", "foreign-uid"]


def test_uninstall_preserves_volume_objects_and_refuses_a_live_consumer(harness, cluster):
    pvs = "/api/v1/persistentvolumes"
    pvcs = "/api/v1/persistentvolumeclaims"
    pods = "/api/v1/pods"
    cluster[pvs] = [
        {
            "metadata": {"name": "pv"},
            "spec": {
                "claimRef": {"namespace": "test", "name": "claim"},
                "csi": {"driver": DRIVER, "volumeHandle": "volume"},
            },
        }
    ]
    cluster[pvcs] = [
        {
            "metadata": {"namespace": "test", "name": "claim"},
            "spec": {"volumeName": "pv"},
            "status": {"phase": "Bound"},
        }
    ]
    cluster[pods] = [
        {
            "metadata": {"namespace": "test", "name": "consumer"},
            "spec": {
                "containers": [],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "claim"}}],
            },
        }
    ]

    with pytest.raises(ActionFailed, match="Pod still uses Cinder: test/consumer"):
        harness.run_action("prepare-uninstall")
    assert len(cluster[SECRET]) == 2

    del cluster[pods]
    harness.run_action("prepare-uninstall")
    assert harness.run_action("uninstall").results["state"] == "removed"
    assert [pv["metadata"]["name"] for pv in cluster[pvs]] == ["pv"]
    assert [pvc["metadata"]["name"] for pvc in cluster[pvcs]] == ["claim"]
