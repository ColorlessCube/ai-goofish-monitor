from src.mcp.goofish_readonly import GoofishReadOnlyClient


def test_readonly_client_sends_bearer_token_only_to_api_requests():
    calls = []

    def fake_fetch(path, headers):
        calls.append((path, headers))
        return {"path": path}

    client = GoofishReadOnlyClient(
        "https://goofish.example",
        "read-token",
        fetch_json=fake_fetch,
    )

    assert client.list_tasks() == {"path": "/api/tasks"}
    assert client.health() == {"path": "/health"}
    assert calls == [
        ("/api/tasks", {"Authorization": "Bearer read-token"}),
        ("/health", {}),
    ]


def test_readonly_client_rejects_path_traversal_in_result_filename():
    client = GoofishReadOnlyClient("https://goofish.example", "read-token", fetch_json=lambda *_: {})

    try:
        client.result_records("../state.json")
    except ValueError as error:
        assert "filename" in str(error)
    else:
        raise AssertionError("unsafe filename was accepted")
