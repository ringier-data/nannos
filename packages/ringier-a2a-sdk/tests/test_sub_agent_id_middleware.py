"""Unit tests for SubAgentIdMiddleware."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ringier_a2a_sdk.middleware import SubAgentIdMiddleware


class TestSubAgentIdMiddleware:
    """Tests for SubAgentIdMiddleware."""

    def test_extract_sub_agent_id_from_metadata(self):
        """Test that sub_agent_id is extracted from HTTP header."""

        # Create test endpoint that checks request.state
        def endpoint(request: Request):
            sub_agent_id = getattr(request.state, "sub_agent_id", None)
            return JSONResponse({"sub_agent_id": sub_agent_id})

        # Create app with middleware
        app = Starlette(
            routes=[Route("/execute", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        # Make request with sub_agent_id in X-Sub-Agent-Id header
        response = client.post(
            "/execute",
            json={
                "jsonrpc": "2.0",
                "method": "execute",
                "params": {"message": {"content": "test"}, "metadata": {"user_context": {"user_id": "test-user"}}},
                "id": 1,
            },
            headers={"X-Sub-Agent-Id": "42"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sub_agent_id"] == 42

    def test_no_sub_agent_id_in_metadata(self):
        """Test that missing sub_agent_id doesn't cause errors."""

        def endpoint(request: Request):
            sub_agent_id = getattr(request.state, "sub_agent_id", None)
            return JSONResponse({"sub_agent_id": sub_agent_id})

        app = Starlette(
            routes=[Route("/execute", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        # Make request without sub_agent_id
        response = client.post(
            "/execute",
            json={
                "jsonrpc": "2.0",
                "method": "execute",
                "params": {"message": {"content": "test"}, "metadata": {"user_context": {"user_id": "test-user"}}},
                "id": 1,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sub_agent_id"] is None

    def test_invalid_json_body(self):
        """Test that invalid JSON is handled gracefully."""

        def endpoint(request: Request):
            return JSONResponse({"ok": True})

        app = Starlette(
            routes=[Route("/execute", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        # Make request with invalid JSON
        response = client.post("/execute", data="invalid json{", headers={"Content-Type": "application/json"})

        # Should not crash, should pass through to endpoint
        assert response.status_code == 200

    def test_non_a2a_endpoints_ignored(self):
        """Test that non-A2A endpoints are not processed."""

        def endpoint(request: Request):
            # Check that middleware didn't set sub_agent_id
            has_sub_agent_id = hasattr(request.state, "sub_agent_id")
            return JSONResponse({"has_sub_agent_id": has_sub_agent_id})

        app = Starlette(
            routes=[Route("/health", endpoint, methods=["GET"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        # Middleware should skip non-A2A endpoints
        assert not data["has_sub_agent_id"]

    def test_stream_endpoint(self):
        """Test that /stream endpoint is also processed."""

        def endpoint(request: Request):
            sub_agent_id = getattr(request.state, "sub_agent_id", None)
            return JSONResponse({"sub_agent_id": sub_agent_id})

        app = Starlette(
            routes=[Route("/stream", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        response = client.post(
            "/stream",
            json={
                "jsonrpc": "2.0",
                "method": "stream",
                "params": {"message": {"content": "test"}, "metadata": {}},
                "id": 1,
            },
            headers={"X-Sub-Agent-Id": "99"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sub_agent_id"] == 99

    def test_empty_metadata(self):
        """Test that empty metadata object doesn't cause errors."""

        def endpoint(request: Request):
            sub_agent_id = getattr(request.state, "sub_agent_id", None)
            return JSONResponse({"sub_agent_id": sub_agent_id})

        app = Starlette(
            routes=[Route("/execute", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        response = client.post(
            "/execute",
            json={
                "jsonrpc": "2.0",
                "method": "execute",
                "params": {"message": {"content": "test"}, "metadata": {}},
                "id": 1,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["sub_agent_id"] is None

    def test_sub_agent_id_type_coercion(self):
        """Test that sub_agent_id value is preserved as integer."""

        def endpoint(request: Request):
            sub_agent_id = getattr(request.state, "sub_agent_id", None)
            return JSONResponse(
                {
                    "sub_agent_id": sub_agent_id,
                    "type": type(sub_agent_id).__name__ if sub_agent_id is not None else "NoneType",
                }
            )

        app = Starlette(
            routes=[Route("/execute", endpoint, methods=["POST"])], middleware=[Middleware(SubAgentIdMiddleware)]
        )

        client = TestClient(app)

        # Test with integer in header
        response = client.post("/execute", json={"params": {"metadata": {}}}, headers={"X-Sub-Agent-Id": "123"})

        data = response.json()
        assert data["sub_agent_id"] == 123
        assert data["type"] == "int"
        assert data["type"] == "int"
