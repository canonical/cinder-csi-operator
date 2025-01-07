# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "cinder_csi" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "cinder-csi"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  config      = var.config
  constraints = var.constraints
}
