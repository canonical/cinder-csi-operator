# Upstream Manifests

This directory contains local copies of the upstream manifests from multiple releases
supported by this charm to be used by the charm for deployment. These
files should not be modified locally.

## Updating

To update these, simply run the update script

```bash
tox -e update -- --registry ${upload-registry} ${namespacing-path} ${user} ~/.upload-password
```
This will overwrite the existing manifests for the supported components
This will also synchronize the images to a provided oci-registry

example) uploading to GHCR
    ```
    --registry ghcr.io canonical/cdk <user> ~/.token
    ```