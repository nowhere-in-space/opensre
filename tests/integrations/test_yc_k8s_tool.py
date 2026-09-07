"""Managed Kubernetes: what the control plane knows that the cluster does not.

The payload shapes here are taken from the Yandex protobufs
(``yandex/cloud/k8s/v1/node.proto`` and its siblings) rather than invented, so a
test passing means the mapping matches what the API actually sends - the field
names are the whole risk in a read-only integration.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx
import pytest

from integrations.yandex_cloud.tools import get_yc_k8s_cluster, list_yc_k8s_clusters

_CREDENTIALS: dict[str, Any] = {"folder_id": "b1gfolder", "iam_token": "t1.token"}

_CLUSTER = {
    "id": "cat1",
    "name": "prod",
    "status": "RUNNING",
    "health": "HEALTHY",
    "releaseChannel": "STABLE",
    "createdAt": "2026-06-09T10:24:09Z",
    "master": {
        "version": "1.30",
        "versionInfo": {"currentVersion": "1.30", "versionDeprecated": True},
        "endpoints": {
            "externalV4Endpoint": "https://84.0.0.1",
            "internalV4Endpoint": "https://10.0.0.1",
        },
        "masterAuth": {"clusterCaCertificate": "-----BEGIN CERTIFICATE-----"},
    },
}


@pytest.fixture(autouse=True)
def _no_endpoint_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("integrations.yandex_cloud.endpoints._fetch_endpoints", dict)
    from integrations.yandex_cloud.endpoints import reset_endpoint_cache

    reset_endpoint_cache()


def _responder(routes: dict[str, dict[str, Any]]) -> Any:
    """Return a request stand-in matching on a path fragment, longest first."""

    def _request(_method: str, url: str, **_kwargs: Any) -> httpx.Response:
        for fragment in sorted(routes, key=len, reverse=True):
            if fragment in url:
                return httpx.Response(HTTPStatus.OK, json=routes[fragment])
        return httpx.Response(HTTPStatus.NOT_FOUND, json={"message": f"no stub for {url}"})

    return _request


def _cluster_routes(**extra: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {
        "/clusters/cat1/nodeGroups": {"nodeGroups": []},
        "/clusters/cat1/nodes": {"nodes": []},
        "/clusters/cat1/operations": {"operations": []},
        "/clusters/cat1": _CLUSTER,
    }
    routes.update(extra)
    return routes


class TestListing:
    def test_a_cluster_comes_back_with_its_version_and_health(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder({"/clusters": {"clusters": [_CLUSTER]}}),
        )

        result = list_yc_k8s_clusters(**_CREDENTIALS)

        assert result["count"] == 1
        assert result["clusters"][0]["version"] == "1.30"
        assert result["clusters"][0]["healthy"] is True

    def test_a_retiring_version_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Yandex carries the deprecation flag on the master, so no second call is needed."""
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder({"/clusters": {"clusters": [_CLUSTER]}}),
        )

        result = list_yc_k8s_clusters(**_CREDENTIALS)

        assert result["clusters"][0]["version_deprecated"] is True

    def test_a_stopped_cluster_is_not_counted_as_broken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopped is a decision somebody made; unhealthy is a fault to chase."""
        clusters = [
            {**_CLUSTER, "id": "c1", "name": "live"},
            {**_CLUSTER, "id": "c2", "name": "switched-off", "status": "STOPPED", "health": ""},
            {**_CLUSTER, "id": "c3", "name": "sick", "health": "UNHEALTHY"},
        ]
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder({"/clusters": {"clusters": clusters}}),
        )

        result = list_yc_k8s_clusters(**_CREDENTIALS)

        assert [c["name"] for c in result["unhealthy"]] == ["sick"]
        assert [c["name"] for c in result["stopped"]] == ["switched-off"]

    def test_every_page_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = {
            "": {"clusters": [{**_CLUSTER, "id": "c1", "name": "first"}], "nextPageToken": "p2"},
            "p2": {"clusters": [{**_CLUSTER, "id": "c2", "name": "second"}]},
        }

        def _request(_method: str, _url: str, **kwargs: Any) -> httpx.Response:
            token = str((kwargs.get("params") or {}).get("pageToken", ""))
            return httpx.Response(HTTPStatus.OK, json=pages[token])

        monkeypatch.setattr("integrations.yandex_cloud.rest_client.send_request", _request)

        result = list_yc_k8s_clusters(**_CREDENTIALS)

        assert [c["name"] for c in result["clusters"]] == ["first", "second"]
        assert result["complete"] is True


class TestWhyANodeIsNotReady:
    """The reason a node never joined is in the control plane, not in the cluster."""

    _NODES = {
        "nodes": [
            {
                "status": "READY",
                "cloudStatus": {"id": "fhm1", "status": "RUNNING"},
                "kubernetesStatus": {
                    "id": "cl1-abc",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "status": "NOT_READY",
                "cloudStatus": {"id": "fhm2", "status": "RUNNING"},
                "kubernetesStatus": {
                    "id": "cl1-def",
                    "conditions": [
                        {"type": "Ready", "status": "False", "message": "kubelet not posting"},
                        {"type": "MemoryPressure", "status": "True", "message": "low memory"},
                        {"type": "DiskPressure", "status": "False"},
                    ],
                },
            },
            {
                # Never registered: no Kubernetes identity, only a failing instance.
                "status": "NOT_CONNECTED",
                "cloudStatus": {
                    "id": "fhm3",
                    "status": "ERROR",
                    "statusMessage": "instance could not start",
                },
            },
        ]
    }

    def _read(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder(_cluster_routes(**{"/clusters/cat1/nodes": self._NODES})),
        )
        return get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)

    def test_only_the_unready_nodes_are_singled_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._read(monkeypatch)

        assert len(result["nodes"]) == 3
        assert [n["name"] for n in result["not_ready_nodes"]] == ["cl1-def", "fhm3"]

    def test_the_conditions_that_are_wrong_are_the_ones_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ready is wrong when False; pressure conditions are wrong when True.

        Reporting every condition would bury the one that matters, and treating
        them all alike would invert the diagnosis for half of them.
        """
        result = self._read(monkeypatch)

        unready = next(n for n in result["nodes"] if n["name"] == "cl1-def")
        assert [c["type"] for c in unready["conditions"]] == ["Ready", "MemoryPressure"]
        assert unready["conditions"][0]["message"] == "kubelet not posting"

    def test_a_healthy_node_reports_no_conditions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._read(monkeypatch)

        healthy = next(n for n in result["nodes"] if n["name"] == "cl1-abc")
        assert healthy["conditions"] == []

    def test_a_node_that_never_registered_is_named_by_its_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the case the cluster's own API cannot answer: it never saw the node."""
        result = self._read(monkeypatch)

        never_joined = next(n for n in result["nodes"] if n["name"] == "fhm3")
        assert never_joined["instance_status"] == "ERROR"
        assert never_joined["instance_message"] == "instance could not start"


class TestNodeGroups:
    def test_an_autoscaling_group_reports_its_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fixed group cannot have failed to scale, so which policy it is matters."""
        groups = {
            "nodeGroups": [
                {
                    "id": "cat1g1",
                    "name": "workers",
                    "status": "RECONCILING",
                    "versionInfo": {"currentVersion": "1.30", "newRevisionAvailable": True},
                    "scalePolicy": {"autoScale": {"minSize": "1", "maxSize": "9"}},
                    "allocationPolicy": {"locations": [{"zoneId": "ru-central1-a"}]},
                }
            ]
        }
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder(_cluster_routes(**{"/clusters/cat1/nodeGroups": groups})),
        )

        result = get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)

        group = result["node_groups"][0]
        assert group["auto_scale"] == {"min": "1", "max": "9"}
        assert group["new_revision_available"] is True
        assert [g["name"] for g in result["not_running_node_groups"]] == ["workers"]

    def test_groups_are_read_scoped_to_the_cluster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The folder-wide listing would need filtering and can page away a group."""
        seen: list[str] = []

        def _request(_method: str, url: str, **_kwargs: Any) -> httpx.Response:
            seen.append(url)
            if "/nodeGroups" in url:
                return httpx.Response(HTTPStatus.OK, json={"nodeGroups": []})
            if "/nodes" in url:
                return httpx.Response(HTTPStatus.OK, json={"nodes": []})
            if "/operations" in url:
                return httpx.Response(HTTPStatus.OK, json={"operations": []})
            return httpx.Response(HTTPStatus.OK, json=_CLUSTER)

        monkeypatch.setattr("integrations.yandex_cloud.rest_client.send_request", _request)

        get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)

        assert any(url.endswith("/clusters/cat1/nodeGroups") for url in seen)


class TestWhatToDoNext:
    def test_the_connection_hint_names_the_reachable_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request", _responder(_cluster_routes())
        )

        connect = get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)["connect"]

        assert connect["endpoint"] == "https://84.0.0.1"
        assert connect["reachable_from"] == "the internet"
        assert "--external" in connect["get_credentials"]

    def test_a_private_cluster_says_so_instead_of_offering_an_unreachable_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a public endpoint the tools cannot reach it from outside at all."""
        private = {
            **_CLUSTER,
            "master": {
                **_CLUSTER["master"],
                "endpoints": {"internalV4Endpoint": "https://10.0.0.1"},
            },
        }
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder(_cluster_routes(**{"/clusters/cat1": private})),
        )

        connect = get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)["connect"]

        assert connect["endpoint"] == "https://10.0.0.1"
        assert connect["reachable_from"] == "the cloud network only"
        assert "--external" not in connect["get_credentials"]

    def test_every_result_points_at_the_tools_that_read_workloads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API has no pod endpoint, and an agent looking for one loses turns."""
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder({"/clusters": {"clusters": [_CLUSTER]}}),
        )

        listing = list_yc_k8s_clusters(**_CREDENTIALS)

        assert "kubernetes_get_events" in listing["workload_hint"]


class TestAFailedReadIsNotAnEmptyCluster:
    def test_a_failed_node_read_is_said_out_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty node list and a failed node read lead to opposite conclusions."""

        def _request(_method: str, url: str, **_kwargs: Any) -> httpx.Response:
            if "/nodes" in url:
                return httpx.Response(HTTPStatus.SERVICE_UNAVAILABLE, json={"message": "busy"})
            if "/nodeGroups" in url:
                return httpx.Response(HTTPStatus.OK, json={"nodeGroups": []})
            if "/operations" in url:
                return httpx.Response(HTTPStatus.OK, json={"operations": []})
            return httpx.Response(HTTPStatus.OK, json=_CLUSTER)

        monkeypatch.setattr("integrations.yandex_cloud.rest_client.send_request", _request)

        result = get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)

        assert result["nodes"] == []
        assert "read_errors" in result

    def test_an_unreadable_cluster_is_unavailable_rather_than_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "integrations.yandex_cloud.rest_client.send_request",
            _responder({}),
        )

        result = get_yc_k8s_cluster(cluster_id="cat1", **_CREDENTIALS)

        assert result["available"] is False

    def test_a_missing_cluster_id_is_refused_before_any_request(self) -> None:
        result = get_yc_k8s_cluster(cluster_id="  ", **_CREDENTIALS)

        assert result["available"] is False
        assert "list_yc_k8s_clusters" in result["error"]
