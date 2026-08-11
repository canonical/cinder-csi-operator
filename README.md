# cinder-csi

## Description

This subordinate charm manages the cinder-csi-driver components a Kubernetes cloud deployed on Openstack.

## Usage

The charm requires openstack credentials and connection information, which
can be provided via the `openstack-integration` relation to the [Openstack Integrator charm](https://charmhub.io/openstack-integrator).

### Release Selection

When setting `storage-release`, manifests for that version must already be
present in the charm source tree under `upstream/cloud_storage/manifests`.

You can verify supported versions via:

```bash
juju run cinder-csi/leader list-versions --wait
```

## Deployment

### The full process

```bash
juju deploy charmed-kubernetes
juju config kubernetes-control-plane allow-privileged=true
juju deploy openstack-integrator --trust
juju deploy cinder-csi

juju relate cinder-csi:certificates     easyrsa:client
juju relate cinder-csi:kube-control     kubernetes-control-plane:kube-control
juju relate cinder-csi:openstack  openstack-integrator:clients

##  wait for the kubernetes-control-plane to be active/idle
```

### Details

* Requires a `charmed-kubernetes` deployment on a openstack cloud launched by juju
* Deploy the `openstack-integrator` charm into the model using `--trust` so juju provides openstack credentials
* Deploy the `cinder-csi` charm in the model relating to the integrator and to charmed-kubernetes components
* Once the model is active/idle, the storage charm will have successfully deployed the cinder-csi in the `kube-system` namespace

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/canonical/cinder-csi/blob/main/CONTRIBUTING.md) for developer guidance.
