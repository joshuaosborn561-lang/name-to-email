from fastapi.testclient import TestClient

from finder.api import app


def test_oauth_discovery_and_dcr():
    with TestClient(app) as client:
        as_meta = client.get("/.well-known/oauth-authorization-server")
        assert as_meta.status_code == 200
        body = as_meta.json()
        assert body["authorization_endpoint"].endswith("/authorize")
        assert body["token_endpoint"].endswith("/token")
        assert body["registration_endpoint"].endswith("/register")

        pr = client.get("/.well-known/oauth-protected-resource")
        assert pr.status_code == 200
        assert "resource" in pr.json()
        assert pr.json()["authorization_servers"]

        registered = client.post(
            "/register",
            json={"redirect_uris": ["https://example.com/cb"], "client_name": "cursor"},
        )
        assert registered.status_code == 201
        assert registered.json()["client_id"]
        assert registered.json()["token_endpoint_auth_method"] == "none"

        auth = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registered.json()["client_id"],
                "redirect_uri": "https://example.com/cb",
                "state": "xyz",
                "code_challenge": "abc",
                "code_challenge_method": "plain",
            },
            follow_redirects=False,
        )
        assert auth.status_code == 302
        assert "code=" in auth.headers["location"]
        assert "state=xyz" in auth.headers["location"]
        code = dict(
            part.split("=", 1)
            for part in auth.headers["location"].split("?", 1)[1].split("&")
        )["code"]

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://example.com/cb",
                "code_verifier": "abc",
            },
        )
        assert token.status_code == 200
        assert token.json()["access_token"]
        assert token.json()["token_type"] == "Bearer"


def test_mcp_initialize_and_tools_list():
    with TestClient(app) as client:
        init = client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert init.status_code == 200
        assert init.json()["result"]["serverInfo"]["name"] == "name-to-email"
        assert "mcp-session-id" in init.headers

        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert "verify_person" in names
        assert "start_run" in names
