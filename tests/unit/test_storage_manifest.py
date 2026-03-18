# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import os
import unittest.mock as mock

import pytest
from lightkube.models.core_v1 import Container, EnvVar, Taint, Toleration, Volume
from lightkube.resources.apps_v1 import DaemonSet, Deployment

import storage_manifests
from charm import CinderCSICharm, KubeControlRequirer, OpenstackIntegrationRequirer
from config import CharmConfig

CLUSTER_NAME = "k8s-cluster-name"
PROXY_URL = "http://proxy:80"
PROXY_URL_1 = f"{PROXY_URL}81"
PROXY_URL_2 = f"{PROXY_URL}82"
NO_PROXY = "127.0.0.1,localhost,::1,example.com"


@pytest.fixture(params=["", NO_PROXY])
def no_proxy(request):
    """Return the no_proxy value."""
    return request.param


@pytest.fixture(params=[True, False])
def respect_proxy(request):
    """Return the juju model proxy enable value."""
    return request.param


@pytest.fixture
def charm_config(respect_proxy, no_proxy):
    """Return the charm config."""
    config = mock.MagicMock(spec=CharmConfig)
    config.available_data = {
        "cloud-conf": "abc",
        "endpoint-ca-cert": "def",
        "web-proxy-enable": respect_proxy,
    }
    env = {
        "JUJU_CHARM_HTTPS_PROXY": PROXY_URL_1,
        "JUJU_CHARM_HTTP_PROXY": PROXY_URL_2,
        "JUJU_CHARM_NO_PROXY": no_proxy,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        yield config


@pytest.fixture
def kube_control():
    """Return the kube control mock."""
    kube_control = mock.MagicMock(spec=KubeControlRequirer)
    kube_control.evaluate_relation.return_value = None
    kube_control.get_registry_location.return_value = "rocks.canonical.com/cdk"
    kube_control.kubeconfig = b"abc"
    kube_control.get_cluster_tag.return_value = CLUSTER_NAME
    return kube_control


@pytest.fixture
def integrator():
    """Return the openstack integration mock."""
    integrator = mock.MagicMock(spec=OpenstackIntegrationRequirer)
    integrator.evaluate_relation.return_value = None
    integrator.cloud_conf_b64 = b"abc"
    integrator.endpoint_tls_ca = b"def"
    yield integrator


@pytest.fixture
def storage(kube_control, charm_config, integrator):
    """Return the manifests object."""
    yield storage_manifests.StorageManifests(
        mock.MagicMock(spec=CinderCSICharm),
        charm_config,
        kube_control,
        integrator,
    )


@pytest.mark.parametrize(
    "resource,name",
    [
        (DaemonSet, "csi-cinder-nodeplugin"),
        (Deployment, "csi-cinder-controllerplugin"),
    ],
    ids=["csi-cinder-nodeplugin", "csi-cinder-controllerplugin"],
)
def test_patch_csi_driver(resource, name, storage, respect_proxy, no_proxy):
    """Test the patching of the csi_driver executables."""
    update_rsc = storage.manipulations[-1]
    assert isinstance(update_rsc, storage_manifests.UpdateCSIDriver)

    secret_volume = mock.MagicMock(spec=Volume)
    cluster_env = EnvVar(name="CLUSTER_NAME", value="set-me")

    container = mock.MagicMock(spec=Container)
    container.name = "cinder-csi-plugin"
    container.env = [cluster_env]

    rsc = mock.MagicMock(spec=resource)
    rsc.kind = resource._api_info.resource.kind
    rsc.metadata.name = name
    rsc.spec.template.spec.volumes = [secret_volume]
    rsc.spec.template.spec.containers = [container]

    update_rsc(rsc)
    assert secret_volume.secret.secretName == storage_manifests.SECRET_NAME
    if respect_proxy:
        split_no_proxy = no_proxy.split(",") if no_proxy else []
        expected_no_proxy = ",".join(
            dict.fromkeys(storage_manifests.K8S_DEFAULT_NO_PROXY + split_no_proxy)
        )
        assert EnvVar(name="CLUSTER_NAME", value=CLUSTER_NAME) in container.env
        assert EnvVar(name="HTTPS_PROXY", value=PROXY_URL_1) in container.env
        assert EnvVar(name="HTTP_PROXY", value=PROXY_URL_2) in container.env
        assert EnvVar(name="NO_PROXY", value=expected_no_proxy) in container.env
        assert EnvVar(name="https_proxy", value=PROXY_URL_1) in container.env
        assert EnvVar(name="http_proxy", value=PROXY_URL_2) in container.env
        assert EnvVar(name="no_proxy", value=expected_no_proxy) in container.env
    else:
        keys = {e.name for e in container.env}
        assert "HTTP_PROXY" not in keys
        assert "HTTPS_PROXY" not in keys
        assert "NO_PROXY" not in keys
        assert "http_proxy" not in keys
        assert "https_proxy" not in keys
        assert "no_proxy" not in keys


@pytest.mark.parametrize(
    "taints,expected",
    [
        pytest.param(
            [],
            [Taint(key="node-role.kubernetes.io/control-plane", effect="NoSchedule")],
            id="default-fallback-when-empty",
        ),
        pytest.param(
            [Taint(key="custom-taint", value="custom-value", effect="NoSchedule")],
            [Taint(key="custom-taint", value="custom-value", effect="NoSchedule")],
            id="uses-kube-control-taints",
        ),
    ],
)
def test_config_control_node_taints(taints, expected, kube_control, storage):
    """Test that control-node-taints uses kube_control or falls back to a default."""
    kube_control.get_controller_taints.return_value = taints
    assert storage.config["control-node-taints"] == expected


@pytest.mark.parametrize(
    "taints,expected_tolerations",
    [
        pytest.param(
            [Taint(key="node-role.kubernetes.io/control-plane", effect="NoSchedule")],
            [
                Toleration(
                    key="node-role.kubernetes.io/control-plane",
                    effect="NoSchedule",
                    operator="Exists",
                )
            ],
            id="taint-without-value-uses-exists-operator",
        ),
        pytest.param(
            [Taint(key="my-key", value="my-value", effect="NoSchedule")],
            [Toleration(key="my-key", value="my-value", effect="NoSchedule", operator="Equal")],
            id="taint-with-value-uses-equal-operator",
        ),
        pytest.param(
            [
                Taint(key="key1", effect="NoSchedule"),
                Taint(key="key2", value="val2", effect="NoExecute"),
            ],
            [
                Toleration(key="key1", effect="NoSchedule", operator="Exists"),
                Toleration(key="key2", value="val2", effect="NoExecute", operator="Equal"),
            ],
            id="multiple-taints-to-tolerations",
        ),
    ],
)
def test_tolerations_set_on_controller_deployment(
    taints, expected_tolerations, kube_control, storage
):
    """Test that tolerations are applied to the csi-cinder-controllerplugin Deployment."""
    kube_control.get_controller_taints.return_value = taints

    update_rsc = storage.manipulations[-1]
    assert isinstance(update_rsc, storage_manifests.UpdateCSIDriver)

    rsc = mock.MagicMock(spec=Deployment)
    rsc.kind = "Deployment"
    rsc.metadata.name = "csi-cinder-controllerplugin"
    rsc.spec.template.spec.volumes = []
    container = mock.MagicMock(spec=Container)
    container.name = "cinder-csi-plugin"
    container.env = []
    rsc.spec.template.spec.containers = [container]

    update_rsc(rsc)
    assert rsc.spec.template.spec.tolerations == expected_tolerations


def test_daemonset_tolerations_not_modified(kube_control, storage):
    """Test that tolerations are not set on the csi-cinder-nodeplugin DaemonSet."""
    kube_control.get_controller_taints.return_value = [
        Taint(key="node-role.kubernetes.io/control-plane", effect="NoSchedule")
    ]

    update_rsc = storage.manipulations[-1]
    assert isinstance(update_rsc, storage_manifests.UpdateCSIDriver)

    rsc = mock.MagicMock(spec=DaemonSet)
    rsc.kind = "DaemonSet"
    rsc.metadata.name = "csi-cinder-nodeplugin"
    rsc.spec.template.spec.volumes = []
    container = mock.MagicMock(spec=Container)
    container.name = "cinder-csi-plugin"
    container.env = []
    rsc.spec.template.spec.containers = [container]

    original_tolerations = rsc.spec.template.spec.tolerations
    update_rsc(rsc)
    assert rsc.spec.template.spec.tolerations is original_tolerations
