# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "cinder_csi" {
  name       = var.app_name
  model_uuid = var.model_uuid

  charm {
    name     = "cinder-csi"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  config      = var.config
}
