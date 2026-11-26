# cinder-csi

## Description

This subordinate charm manages the cinder-csi-driver components a Kubernetes cloud deployed on Openstack.

## Usage

The charm requires openstack credentials and connection information, which
can be provided via the `openstack-integration` relation to the [Openstack Integrator charm](https://charmhub.io/openstack-integrator).

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

## Removal

Stop workloads that use Cinder and wait for detach and provisioning to finish.
Keep `kube-control` connected. Do not start new Cinder workloads, scale either
charm, or refresh the charm during uninstall.

Run the ordered uninstall (requires `juju` and `jq`):

```bash
bash scripts/uninstall-cinder-csi.sh <model> cinder-csi
```

The workflow pauses reconciliation on all Cinder units, checks storage usage,
and removes only confirmed CSI resources, including the cloud-config Secret.
It does not delete PVCs, PVs, or Cinder volumes. The Juju application is removed
only after the charm confirms Kubernetes cleanup; otherwise the application
and its relations remain in place so the same command can be retried.

Objects created before ownership tracking, or whose recorded UID no longer
matches, require explicit approval. Review the `legacy-objects` result and
rerun with a comma-separated list of the verified UIDs as the third argument.
Do not approve objects that belong to another installation.

Plain `juju remove-application` only cleans up local unit credentials. It can
leave the CSI driver and its Kubernetes Secret behind.

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/canonical/cinder-csi/blob/main/CONTRIBUTING.md) for developer guidance.
