#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Deploy and manage the Cinder CSI plugin for K8s on OpenStack."""

import json
import logging
import os
from errno import EEXIST, ENOTEMPTY
from pathlib import Path

import ops
from httpx import HTTPError
from lightkube import Client, ConfigError, KubeConfig
from ops.interface_kube_control import KubeControlRequirer
from ops.interface_openstack_integration import OpenstackIntegrationRequirer
from ops.interface_tls_certificates import CertificatesRequires
from ops.manifests import Collector, ManifestClientError

from config import CharmConfig
from storage_manifests import StorageManifests
from uninstall import (
    UninstallError,
    check_driver_unused,
    delete_instance,
    installed_objects,
)

log = logging.getLogger(__name__)
UNINSTALL_MEMBERS = "uninstall-members"
UNINSTALL_ACK = "uninstall-paused"


class CinderCSICharm(ops.CharmBase):
    """Deploy and manage the Cinder CSI plugin for K8s on OpenStack."""

    stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        # Ensure kubeconfig environment
        self._kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)

        # Relation Validator and datastore
        self.kube_control = KubeControlRequirer(self, schemas="0,1")
        self.certificates = CertificatesRequires(self)
        self.integrator = OpenstackIntegrationRequirer(self)
        # Config Validator and datastore
        self.charm_config = CharmConfig(self)

        self.stored.set_default(
            config_hash=None,  # hashed value of the config once valid
            deployed=False,  # True if the config has been applied after new hash
        )
        self.storage_manifests = StorageManifests(
            self, self.charm_config, self.kube_control, self.integrator
        )
        self.collector = Collector(self.storage_manifests)

        self.framework.observe(self.on.kube_control_relation_created, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_joined, self._kube_control)
        self.framework.observe(self.on.kube_control_relation_changed, self._merge_config)
        self.framework.observe(self.on.kube_control_relation_broken, self._merge_config)

        self.framework.observe(self.on.certificates_relation_created, self._merge_config)
        self.framework.observe(self.on.certificates_relation_changed, self._merge_config)
        self.framework.observe(self.on.certificates_relation_broken, self._merge_config)

        self.framework.observe(self.on.openstack_relation_created, self._merge_config)
        self.framework.observe(self.on.openstack_relation_joined, self._merge_config)
        self.framework.observe(self.on.openstack_relation_changed, self._merge_config)
        self.framework.observe(self.on.openstack_relation_broken, self._merge_config)

        self.framework.observe(self.on.list_versions_action, self._list_versions)
        self.framework.observe(self.on.list_resources_action, self._list_resources)
        self.framework.observe(self.on.scrub_resources_action, self._scrub_resources)
        self.framework.observe(self.on.sync_resources_action, self._sync_resources)
        self.framework.observe(self.on.prepare_uninstall_action, self._prepare_uninstall)
        self.framework.observe(self.on.uninstall_action, self._uninstall)
        self.framework.observe(self.on.cinder_peers_relation_created, self._merge_config)
        self.framework.observe(self.on.cinder_peers_relation_changed, self._ack_uninstall)
        self.framework.observe(self.on.update_status, self._update_status)

        self.framework.observe(self.on.install, self._install_or_upgrade)
        self.framework.observe(self.on.upgrade_charm, self._install_or_upgrade)
        self.framework.observe(self.on.config_changed, self._merge_config)
        self.framework.observe(self.on.stop, self._cleanup)

    @property
    def _ca_cert_path(self) -> Path:
        return Path(f"/srv/{self.unit.name}/ca.crt")

    @property
    def _kubeconfig_path(self) -> Path:
        path = f"/srv/{self.unit.name}/kubeconfig"
        os.environ["KUBECONFIG"] = path
        return Path(path)

    def _list_versions(self, event):
        self.collector.list_versions(event)

    def _list_resources(self, event):
        manifests = event.params.get("controller", "")
        resources = event.params.get("resources", "")
        return self.collector.list_resources(event, manifests, resources)

    def _scrub_resources(self, event):
        if self._ack_uninstall(event):
            event.fail("Cinder CSI reconciliation is paused")
            return
        manifests = event.params.get("controller", "")
        resources = event.params.get("resources", "")
        return self.collector.scrub_resources(event, manifests, resources)

    def _sync_resources(self, event):
        if self._ack_uninstall(event):
            event.fail("Cinder CSI reconciliation is paused")
            return
        manifests = event.params.get("controller", "")
        resources = event.params.get("resources", "")
        try:
            self.collector.apply_missing_resources(event, manifests, resources)
        except ManifestClientError:
            msg = "Failed to apply missing resources. API Server unavailable."
            event.set_results({"result": msg})

    def _update_status(self, event):
        if self._ack_uninstall(event):
            return
        if not self.stored.deployed:
            return

        unready = self.collector.unready
        if unready:
            self.unit.status = ops.WaitingStatus(", ".join(unready))
        else:
            self.unit.status = ops.ActiveStatus("Ready")
            self.unit.set_workload_version(self.collector.short_version)
            self.app.status = ops.ActiveStatus(self.collector.long_version)

    def _kube_control(self, event):
        self.kube_control.set_auth_request(self.unit.name, "system:masters")
        return self._merge_config(event)

    def _check_integrator(self, event):
        self.unit.status = ops.MaintenanceStatus("Evaluating Openstack relation.")
        evaluation = self.integrator.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = ops.WaitingStatus(evaluation)
            else:
                self.unit.status = ops.BlockedStatus(evaluation)
            return False
        return True

    def _check_kube_control(self, event):
        self.unit.status = ops.MaintenanceStatus("Evaluating kubernetes authentication.")
        evaluation = self.kube_control.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = ops.WaitingStatus(evaluation)
            else:
                self.unit.status = ops.BlockedStatus(evaluation)
            return False
        if not self.kube_control.get_auth_credentials(self.unit.name):
            self.unit.status = ops.WaitingStatus("Waiting for kube-control: unit credentials")
            return False
        self.kube_control.create_kubeconfig(
            self._ca_cert_path, self._kubeconfig_path, "root", self.unit.name
        )
        return True

    def _check_certificates(self, event):
        if self.kube_control.get_ca_certificate():
            log.info("CA Certificate is available from kube-control.")
            return True

        self.unit.status = ops.MaintenanceStatus("Evaluating certificates.")
        evaluation = self.certificates.evaluate_relation(event)
        if evaluation:
            if "Waiting" in evaluation:
                self.unit.status = ops.WaitingStatus(evaluation)
            else:
                self.unit.status = ops.BlockedStatus(evaluation)
            return False
        self._ca_cert_path.write_text(self.certificates.ca)
        return True

    def _check_config(self):
        self.unit.status = ops.MaintenanceStatus("Evaluating charm config.")
        evaluation = self.charm_config.evaluate()
        if evaluation:
            self.unit.status = ops.BlockedStatus(evaluation)
            return False
        return True

    def _merge_config(self, event):
        if self._ack_uninstall(event):
            return
        if not self._check_integrator(event):
            return

        if not self._check_certificates(event):
            return

        if not self._check_kube_control(event):
            return

        if not self._check_config():
            return

        self.unit.status = ops.MaintenanceStatus("Evaluating Manifests")
        new_hash = 0
        for controller in self.collector.manifests.values():
            evaluation = controller.evaluate()
            if evaluation:
                self.unit.status = ops.BlockedStatus(evaluation)
                return
            new_hash += controller.hash()

        self.stored.deployed = False
        if self._install_or_upgrade(event, config_hash=new_hash):
            self.stored.config_hash = new_hash
            self.stored.deployed = True

    def _install_or_upgrade(self, event, config_hash=None):
        if self._ack_uninstall(event):
            return False
        if self.stored.config_hash == config_hash:
            log.info("Skipping until the config is evaluated.")
            return True

        self.unit.status = ops.MaintenanceStatus("Deploying Cinder Storage")
        self.unit.set_workload_version("")
        for controller in self.collector.manifests.values():
            try:
                controller.apply_manifests()
            except ManifestClientError as e:
                self.unit.status = ops.WaitingStatus("Waiting for kube-apiserver")
                log.warning("Encountered retryable installation error: %s", e)
                event.defer()
                return False
        return True

    def _ack_uninstall(self, _) -> bool:
        peer = self.model.get_relation("cinder-peers")
        if peer is None:
            self.unit.status = ops.WaitingStatus("Waiting for Cinder CSI peer relation")
            return True
        if UNINSTALL_MEMBERS not in peer.data[self.app]:
            return False
        # Juju runs one hook at a time per unit. Prior reconciliation has finished here,
        # and all subsequent mutating handlers on this unit see the persistent pause.
        peer.data[self.unit][UNINSTALL_ACK] = "true"
        self.unit.status = ops.MaintenanceStatus("Paused for Cinder CSI uninstall")
        return True

    def _uninstall_peers(self):
        if not self.unit.is_leader():
            raise UninstallError("Run uninstall on the Cinder CSI leader")
        peer = self.model.get_relation("cinder-peers")
        if peer is None:
            raise UninstallError("Cinder CSI peer relation is not ready")
        units = {self.unit, *peer.units}
        if len(units) != self.app.planned_units():
            raise UninstallError("Peer membership is changing; wait for the model to settle")
        return peer, units

    def _uninstall_client(self) -> Client:
        if self.model.get_relation("kube-control") is None:
            raise UninstallError("Keep kube-control connected until uninstall succeeds")
        return Client(config=KubeConfig.from_file(self._kubeconfig_path))

    def _prepare_uninstall(self, event):
        try:
            peer, units = self._uninstall_peers()
            check_driver_unused(self._uninstall_client())
            peer.data[self.app][UNINSTALL_MEMBERS] = json.dumps(
                sorted(unit.name for unit in units)
            )
            self._ack_uninstall(event)
        except (UninstallError, ConfigError, HTTPError, OSError) as exc:
            event.fail(str(exc))
        else:
            event.set_results({"state": "prepared"})

    def _uninstall(self, event):
        try:
            peer, units = self._uninstall_peers()
            app_data = peer.data[self.app]
            if UNINSTALL_MEMBERS not in app_data:
                raise UninstallError("Run prepare-uninstall first")
            if {unit.name for unit in units} != set(json.loads(app_data[UNINSTALL_MEMBERS])):
                raise UninstallError("Peer membership changed; run prepare-uninstall again")
            self._ack_uninstall(event)
            waiting = sorted(unit.name for unit in units if not peer.data[unit].get(UNINSTALL_ACK))
            if waiting:
                event.set_results({"state": "waiting", "units": ", ".join(waiting)})
                return
            self._delete_installation(event)
        except (UninstallError, ConfigError, HTTPError, OSError) as exc:
            event.fail(str(exc))

    def _delete_installation(self, event):
        client = self._uninstall_client()
        check_driver_unused(client)
        app, manifest = self.app.name, self.storage_manifests
        approved = {uid.strip() for uid in event.params["legacy-uids"].split(",") if uid.strip()}
        objects = list(installed_objects(client, app, manifest.name, manifest.installation))
        unapproved = [
            f"{type(obj).__name__}/{obj.metadata.namespace or '-'}/{obj.metadata.name}"
            f" {obj.metadata.uid}"
            for obj, legacy in objects
            if legacy and obj.metadata.uid not in approved
        ]
        if unapproved:
            event.set_results({"legacy-objects": "\n".join(sorted(unapproved))})
            raise UninstallError(
                "Verify the listed legacy objects and retry with their legacy-uids"
            )
        for obj, _ in objects:
            delete_instance(client, obj)
        remaining = list(installed_objects(client, app, manifest.name, manifest.installation))
        event.set_results({"state": "waiting" if remaining else "removed"})

    def _cleanup(self, _):
        kubeconfig = self._kubeconfig_path
        self.unit.status = ops.MaintenanceStatus("Shutting down")
        kubeconfig.unlink(missing_ok=True)
        kubeconfig.with_suffix(".new").unlink(missing_ok=True)
        self._ca_cert_path.unlink(missing_ok=True)
        try:
            kubeconfig.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno not in (ENOTEMPTY, EEXIST):
                raise


if __name__ == "__main__":
    ops.main(CinderCSICharm)
