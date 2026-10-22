# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest.mock as mock
from pathlib import Path

import httpx
import pytest
import yaml
from ops.manifests import ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness

from charm import CinderCSICharm


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
def test_waits_for_kube_control(mock_create_kubeconfig, harness, caplog):
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


def bad_gateway() -> httpx.HTTPStatusError:
    """Non-JSON 502 from the API load balancer, which lightkube raises unwrapped."""
    request = httpx.Request("GET", "https://10.0.0.1:6443/api/v1/nodes")
    response = httpx.Response(502, request=request, headers={"Content-Type": "text/html"})
    return httpx.HTTPStatusError(
        "Server error '502 Bad Gateway'", request=request, response=response
    )


@pytest.fixture()
def deployed_charm(harness):
    harness.set_leader(True)
    harness.begin()
    charm = harness.charm
    charm.stored.deployed = True
    charm.collector = mock.MagicMock()
    charm.collector.unready = []
    charm.collector.short_version = "1.0"
    charm.collector.long_version = "cinder-csi 1.0"
    return charm


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ManifestClientError(
                "Failed to load in cluster CRDs", httpx.ConnectError("No route to host")
            ),
            id="manifest client error",
        ),
        pytest.param(httpx.ConnectError("No route to host"), id="connect error"),
        pytest.param(httpx.TimeoutException("timed out"), id="timeout"),
        pytest.param(httpx.ReadError("connection reset"), id="read error"),
        pytest.param(bad_gateway(), id="502 from load balancer"),
    ],
)
def test_update_status_waits_when_api_unreachable(deployed_charm, error, caplog):
    """A transient API error must not leave the unit in a sticky hook error."""
    type(deployed_charm.collector).unready = mock.PropertyMock(side_effect=error)

    deployed_charm._update_status(None)

    assert deployed_charm.unit.status == WaitingStatus("Waiting for kube-apiserver")
    assert any("Kubernetes API unreachable" in r.message for r in caplog.records)


def test_update_status_active_when_ready(deployed_charm):
    deployed_charm._update_status(None)

    assert deployed_charm.unit.status == ActiveStatus("Ready")
