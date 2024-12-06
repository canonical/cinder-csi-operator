# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Config Management for the cinder-csi storage charm."""

import logging
from typing import Optional

log = logging.getLogger(__name__)


class CharmConfig:
    """Representation of the charm configuration."""

    def __init__(self, charm):
        """Creates a CharmConfig object from the configuration data."""
        self.config = charm.config

    @property
    def available_data(self):
        """Parse valid charm config into a dict, drop keys if unset."""
        data = dict(**self.config)
        for key, value in dict(**self.config).items():
            if value == "" or value is None:
                del data[key]

        return data

    def evaluate(self) -> Optional[str]:
        """Determine if configuration is valid."""
        if self.config["reclaim-policy"].title() not in ["Delete", "Retain"]:
            return "reclaim-policy should be either 'Delete' or 'Retain'"
        return None
