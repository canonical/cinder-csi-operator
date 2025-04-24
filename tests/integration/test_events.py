# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import datetime
import logging

from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import Event, Pod

log = logging.getLogger(__name__)


async def test_get_events(kubernetes):
    deployment = await kubernetes.get(
        Deployment, name="csi-cinder-controllerplugin", namespace="kube-system"
    )
    deployment_events = []
    async for event in kubernetes.list(
        Event,
        namespace=deployment.metadata.namespace,
        fields={
            "involvedObject.kind": "Deployment",
            "involvedObject.name": deployment.metadata.name,
        },
    ):
        deployment_events.append(event)

    pod_events = []
    async for pod in kubernetes.list(
        Pod,
        namespace=deployment.metadata.namespace,
        labels={"app": deployment.metadata.name},
    ):
        async for event in kubernetes.list(
            Event,
            namespace=pod.metadata.namespace,
            fields={
                "involvedObject.kind": "Pod",
                "involvedObject.name": pod.metadata.name,
            },
        ):
            pod_events.append(event)

    for event in sorted(deployment_events + pod_events, key=by_localtime):
        log.info(
            "Event %s/%s %s msg=%s",
            event.involvedObject.kind,
            event.involvedObject.name,
            event.lastTimestamp and event.lastTimestamp.astimezone() or "Date not recorded",
            event.message,
        )


def by_localtime(event: Event) -> datetime.datetime:
    """Return the last timestamp of the event."""
    dt = event.lastTimestamp or datetime.datetime.now()
    return dt.astimezone()
