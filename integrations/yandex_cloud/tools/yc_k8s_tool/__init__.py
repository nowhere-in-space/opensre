"""Managed Kubernetes: control-plane health, node groups, and the nodes themselves.

Workloads are deliberately absent. Pods, events and container logs have no
endpoint in the Yandex Cloud API at all - they are read from the cluster's own
API server with the ``kubernetes_*`` tools - so every result here names those
tools rather than leaving the agent to look for a pod listing that does not
exist.

What the control plane does know, and the cluster's API server does not, is why
a node never joined: the instance behind it can be failing to start, and that
shows up here as a cloud status long before Kubernetes has an opinion.
"""

from __future__ import annotations

from typing import Any, Final

from core.domain.types.evidence import record_evidence_entry
from core.domain.types.tools import ToolSurface
from core.tool_framework import tool
from core.tool_framework.utils import tool_unavailable
from integrations.yandex_cloud.availability import (
    YC_INJECTED_PARAMS,
    client_from_params,
    yc_available_or_backend,
    yc_credentials,
)
from integrations.yandex_cloud.rest_client import YandexCloudClient

SOURCE = "yandex_cloud"
SERVICE = "managed-kubernetes"

_CLUSTERS_PATH: Final = "/managed-kubernetes/v1/clusters"

#: How many pages one collection is followed for. A cluster with more nodes than
#: this is past what a single answer should carry; the point of the cap is that
#: the read stops and says so rather than stopping quietly.
_MAX_PAGES: Final = 5

#: How many operations are worth carrying back. Enough to cover an upgrade and
#: the node-group changes around it without filling the prompt with history.
_RECENT_OPERATIONS: Final = 10

_RUNNING: Final = "RUNNING"
_HEALTHY: Final = "HEALTHY"
#: A cluster somebody switched off. Not a failure, and worth keeping apart from
#: one: a session that treats a stopped cluster as broken goes looking
#: for a cause when the answer is that it was stopped on purpose.
_STOPPED: Final = "STOPPED"
_NODE_READY: Final = "READY"

#: Pods, events and container logs live behind the cluster's own API server.
#: Named in the result because the alternative - the agent hunting for them in
#: the Yandex Cloud API - costs several turns and ends in
#: "there is no such endpoint", which reads like the data does not exist.
WORKLOAD_TOOLS: Final[tuple[str, ...]] = (
    "kubernetes_list_pods",
    "kubernetes_get_events",
    "kubernetes_get_pod_logs",
    "kubernetes_describe_pod",
    "kubernetes_list_nodes",
)
_WORKLOAD_HINT: Final = (
    "Pods, events, container logs and node objects are read with these tools, "
    "not through the Yandex Cloud API - it has no endpoint for them. They need "
    "the kubernetes integration pointed at this cluster; connect says how. A "
    "pod that is not starting needs kubernetes_get_events, because the "
    "scheduler's reason is there and a pod listing only shows that it is stuck."
)


def _extract_params(sources: dict[str, dict]) -> dict[str, Any]:
    return yc_credentials(sources)


def _read_pages(
    client: YandexCloudClient,
    path: str,
    key: str,
    params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Return every item under *key*, an error if a read failed, and completeness.

    Yandex answers a hundred at a time. Reading one page would drop the rest
    from both the list and the count, and a count that is quietly short is worse
    than no count at all - nothing about it looks wrong.
    """
    items: list[dict[str, Any]] = []
    page_token = ""
    for _page in range(_MAX_PAGES):
        response = client.get(SERVICE, path, params, page_token=page_token)
        if not response.get("success"):
            return items, str(response.get("error", "")), False
        items.extend((response.get("data") or {}).get(key) or [])
        page_token = str((response.get("metadata") or {}).get("next_page_token", "") or "")
        if not page_token:
            return items, "", True
    return items, "", False


def _version_facts(owner: dict[str, Any]) -> dict[str, Any]:
    """Return what Yandex says about the version this component runs.

    ``versionDeprecated`` is carried by the master and by every node group, so
    "this cluster is on a version that is being retired" is answerable without a
    second call to the versions catalogue.
    """
    info = owner.get("versionInfo") or {}
    return {
        "version": info.get("currentVersion", "") or owner.get("version", ""),
        "version_deprecated": bool(info.get("versionDeprecated", False)),
        "new_revision_available": bool(info.get("newRevisionAvailable", False)),
    }


def _summarize_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    master = cluster.get("master") or {}
    status = cluster.get("status", "")
    return {
        "id": cluster.get("id", ""),
        "name": cluster.get("name", ""),
        "status": status,
        "health": cluster.get("health", ""),
        **_version_facts(master),
        "release_channel": cluster.get("releaseChannel", ""),
        "created_at": cluster.get("createdAt", ""),
        "healthy": status == _RUNNING and cluster.get("health") == _HEALTHY,
        "stopped": status == _STOPPED,
    }


def _summarize_node_group(group: dict[str, Any]) -> dict[str, Any]:
    policy = group.get("scalePolicy") or {}
    fixed = policy.get("fixedScale") or {}
    auto = policy.get("autoScale") or {}
    locations = (group.get("allocationPolicy") or {}).get("locations") or []
    return {
        "id": group.get("id", ""),
        "name": group.get("name", ""),
        "status": group.get("status", ""),
        **_version_facts(group),
        "fixed_size": fixed.get("size", ""),
        # Only one of the two is set, and which one it is answers a different
        # question: a fixed group cannot have failed to scale.
        "auto_scale": {"min": auto.get("minSize", ""), "max": auto.get("maxSize", "")}
        if auto
        else {},
        "zones": [location.get("zoneId", "") for location in locations],
        "running": group.get("status") == _RUNNING,
    }


def _notable_conditions(kubernetes_status: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the conditions that say something is wrong.

    Kubernetes inverts the sense between condition types: ``Ready`` is healthy
    when it is True, while ``MemoryPressure`` and its siblings are healthy when
    they are False. Reporting every condition would bury the one that matters,
    and reporting them all as "true is good" would invert the diagnosis.
    """
    notable: list[dict[str, Any]] = []
    for condition in kubernetes_status.get("conditions") or []:
        kind = str(condition.get("type", ""))
        state = str(condition.get("status", ""))
        wrong = state != "True" if kind == "Ready" else state == "True"
        if kind and wrong:
            notable.append(
                {
                    "type": kind,
                    "status": state,
                    "message": condition.get("message", ""),
                    "since": condition.get("lastTransitionTime", ""),
                }
            )
    return notable


def _summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    cloud = node.get("cloudStatus") or {}
    kubernetes = node.get("kubernetesStatus") or {}
    status = node.get("status", "")
    return {
        # A node that never registered has no Kubernetes identity yet, so the
        # compute instance is the only name it has.
        "name": kubernetes.get("id", "") or cloud.get("id", ""),
        "status": status,
        "instance_status": cloud.get("status", ""),
        "instance_message": cloud.get("statusMessage", ""),
        "conditions": _notable_conditions(kubernetes),
        "ready": status == _NODE_READY,
    }


def _summarize_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": operation.get("id", ""),
        "description": operation.get("description", ""),
        "created_at": operation.get("createdAt", ""),
        "done": operation.get("done", False),
        "error": (operation.get("error") or {}).get("message", ""),
    }


def _connect(cluster: dict[str, Any]) -> dict[str, Any]:
    """Return how to point the kubernetes integration at this cluster.

    The endpoint is not a preference: a cluster with no external endpoint is
    unreachable from outside its network no matter what the agent does, and
    saying which one exists turns "the tools are broken" into a configuration
    fact.
    """
    master = cluster.get("master") or {}
    endpoints = master.get("endpoints") or {}
    external = endpoints.get("externalV4Endpoint", "")
    internal = endpoints.get("internalV4Endpoint", "")
    name = cluster.get("name", "") or cluster.get("id", "")
    external_flag = " --external" if external else ""
    return {
        "endpoint": external or internal,
        "reachable_from": "the internet" if external else "the cloud network only",
        # The CLI writes a kubeconfig that refreshes its own token; a token
        # copied out by hand expires within hours and takes the tools with it.
        "get_credentials": (
            f"yc managed-kubernetes cluster get-credentials {name}{external_flag} --force"
        ),
        "then": "opensre integrations setup kubernetes",
        "workload_tools": list(WORKLOAD_TOOLS),
        # Reading nodes needs more than the namespace-scoped view role, and an
        # agent handed cluster-admin holds a credential that can delete the
        # cluster it is investigating.
        "role_note": (
            "k8s.cluster-api.viewer covers namespaced resources; node objects "
            "need a ClusterRole bound to the yc:viewer group. Do not use "
            "cluster-admin for an agent."
        ),
    }


def _map_k8s_clusters(
    evidence: dict[str, Any], output: dict[str, Any], _tool_input: dict[str, Any]
) -> None:
    """Cite how many clusters there are and how many are not healthy."""
    if not output.get("available"):
        return
    clusters = output.get("clusters") or []
    if not clusters:
        return
    unhealthy = output.get("unhealthy") or []
    summary = f"{len(clusters)} Kubernetes cluster(s), {len(unhealthy)} unhealthy"
    stopped = output.get("stopped") or []
    if stopped:
        summary += f", {len(stopped)} stopped"
    record_evidence_entry(
        evidence,
        source="yc_k8s_clusters",
        label="Yandex Managed Kubernetes",
        summary=summary,
    )


def _map_k8s_cluster(
    evidence: dict[str, Any], output: dict[str, Any], _tool_input: dict[str, Any]
) -> None:
    """Cite the control plane's state, the node counts, and what changed last."""
    if not output.get("available"):
        return
    cluster = output.get("cluster") or {}
    if not cluster:
        return
    nodes = output.get("nodes") or []
    parts = [
        f"'{cluster.get('name', cluster.get('id', 'unknown'))}'",
        f"{cluster.get('status', 'unknown')}/{cluster.get('health', 'unknown')}",
        f"version {cluster.get('version', 'unknown')}"
        + (" (deprecated)" if cluster.get("version_deprecated") else ""),
        f"{len(output.get('node_groups') or [])} node group(s)",
        f"{len(nodes)} node(s), {len([n for n in nodes if not n['ready']])} not ready",
    ]
    operations = output.get("recent_operations") or []
    if operations:
        latest = operations[0].get("description") or ""
        if latest:
            parts.append(f"latest operation: {latest}")
    record_evidence_entry(
        evidence,
        source="yc_k8s_cluster",
        label="Yandex Managed Kubernetes Cluster",
        summary=", ".join(parts),
    )


@tool(
    name="list_yc_k8s_clusters",
    evidence_mapper=_map_k8s_clusters,
    surfaces=(ToolSurface.ACTION,),
    display_name="Managed Kubernetes",
    source=SOURCE,
    description=(
        "List Managed Kubernetes clusters in the folder with their status, "
        "health, version and release channel. Use to find a cluster id, or to "
        "rule the control plane in or out before investigating what runs on it. "
        "Reads the control plane only: pods, events and container logs come "
        "from the kubernetes_* tools, which the result names."
    ),
    use_cases=[
        "Finding a cluster id from its name",
        "Checking whether the control plane itself is degraded",
        "Spotting a cluster on a Kubernetes version that is being retired",
    ],
    requires=[],
    outputs={
        "clusters": "clusters with status, health, version and release channel",
        "unhealthy": "the subset that is running but not healthy",
        "stopped": "the subset somebody switched off, which is not a failure",
        "complete": "false when more pages were available than were read",
        "workload_hint": "where pods, events and container logs are read instead",
    },
    input_schema={"type": "object", "properties": {}, "required": []},
    is_available=yc_available_or_backend,
    extract_params=_extract_params,
    injected_params=YC_INJECTED_PARAMS,
)
def list_yc_k8s_clusters(
    yc_backend: Any = None,
    **credentials: Any,
) -> dict[str, Any]:
    """List Managed Kubernetes clusters in the folder."""
    if yc_backend is not None:
        return dict(yc_backend.list_yc_k8s_clusters())

    client = client_from_params(credentials)
    if client is None:
        return tool_unavailable(SOURCE, "Yandex Cloud credentials are not configured.")

    raw, failure, complete = _read_pages(
        client, _CLUSTERS_PATH, "clusters", {"folderId": client.folder_id}
    )
    if failure:
        return {"source": SOURCE, "available": False, "error": failure}

    clusters = [_summarize_cluster(cluster) for cluster in raw]
    return {
        "source": SOURCE,
        "available": True,
        "clusters": clusters,
        "unhealthy": [c for c in clusters if not c["healthy"] and not c["stopped"]],
        "stopped": [c for c in clusters if c["stopped"]],
        "count": len(clusters),
        "complete": complete,
        "workload_hint": _WORKLOAD_HINT,
    }


@tool(
    name="get_yc_k8s_cluster",
    evidence_mapper=_map_k8s_cluster,
    surfaces=(ToolSurface.ACTION,),
    display_name="Managed Kubernetes",
    source=SOURCE,
    description=(
        "Read one Managed Kubernetes cluster: control-plane health and version, "
        "every node group with its size and status, every node with why it is "
        "not ready, and the recent operations that say what changed. Use to "
        "tell a cluster problem from a workload one - a node whose instance "
        "never started, or a node group part-way through an update, explains "
        "pod symptoms that look like an application fault. Also returns how to "
        "connect the kubernetes tools to this cluster."
    ),
    use_cases=[
        "Finding which node is not ready and what condition says so",
        "Checking whether a node group is mid-update or failing to scale",
        "Confirming a cluster upgrade happened around the time an incident began",
        "Getting the endpoint and command needed to read workloads",
    ],
    requires=["cluster_id"],
    outputs={
        "cluster": "control-plane status, health, version and release channel",
        "node_groups": "each group with status, size and version",
        "nodes": "each node with its status and the conditions that are wrong",
        "not_ready_nodes": "the subset that is not READY",
        "recent_operations": "the most recent operations, newest first",
        "connect": "endpoint, the command that writes a kubeconfig, and the role it needs",
    },
    input_schema={
        "type": "object",
        "properties": {
            "cluster_id": {
                "type": "string",
                "description": "Cluster id, as returned by list_yc_k8s_clusters.",
            }
        },
        "required": ["cluster_id"],
    },
    is_available=yc_available_or_backend,
    extract_params=_extract_params,
    injected_params=YC_INJECTED_PARAMS,
)
def get_yc_k8s_cluster(
    cluster_id: str,
    yc_backend: Any = None,
    **credentials: Any,
) -> dict[str, Any]:
    """Read one cluster with its node groups, nodes and recent operations."""
    if not cluster_id.strip():
        return tool_unavailable(
            SOURCE, "cluster_id is required. Call list_yc_k8s_clusters to find one."
        )

    if yc_backend is not None:
        return dict(yc_backend.get_yc_k8s_cluster(cluster_id))

    client = client_from_params(credentials)
    if client is None:
        return tool_unavailable(SOURCE, "Yandex Cloud credentials are not configured.")

    detail = client.get(SERVICE, f"{_CLUSTERS_PATH}/{cluster_id}", page_size=None)
    if not detail.get("success"):
        return {
            "source": SOURCE,
            "available": False,
            "error": detail.get("error", "Could not read the cluster."),
            "cluster_id": cluster_id,
        }
    cluster = detail.get("data") or {}

    groups_raw, groups_error, groups_complete = _read_pages(
        client, f"{_CLUSTERS_PATH}/{cluster_id}/nodeGroups", "nodeGroups"
    )
    nodes_raw, nodes_error, nodes_complete = _read_pages(
        client, f"{_CLUSTERS_PATH}/{cluster_id}/nodes", "nodes"
    )
    operations_raw, _operations_error, _complete = _read_pages(
        client, f"{_CLUSTERS_PATH}/{cluster_id}/operations", "operations"
    )

    node_groups = [_summarize_node_group(group) for group in groups_raw]
    nodes = [_summarize_node(node) for node in nodes_raw]
    result: dict[str, Any] = {
        "source": SOURCE,
        "available": True,
        "cluster_id": cluster_id,
        "cluster": _summarize_cluster(cluster),
        "node_groups": node_groups,
        "not_running_node_groups": [group for group in node_groups if not group["running"]],
        "nodes": nodes,
        "not_ready_nodes": [node for node in nodes if not node["ready"]],
        "recent_operations": [
            _summarize_operation(operation) for operation in operations_raw[:_RECENT_OPERATIONS]
        ],
        "connect": _connect(cluster),
        "workload_hint": _WORKLOAD_HINT,
        "complete": groups_complete and nodes_complete,
    }
    # Said out loud rather than left as an empty list: "this cluster has no
    # nodes" and "the node read failed" lead to opposite
    # conclusions, and from the outside they look the same.
    errors = [error for error in (groups_error, nodes_error) if error]
    if errors:
        result["read_errors"] = "; ".join(errors)
    return result


__all__ = ["WORKLOAD_TOOLS", "get_yc_k8s_cluster", "list_yc_k8s_clusters"]
