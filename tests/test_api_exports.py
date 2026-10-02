from untether import api


def test_api_exports() -> None:
    assert api.TAKOPI_PLUGIN_API_VERSION == 1
    assert "TransportRuntime" in api.__all__
    assert api.TransportRuntime is not None


def test_418_command_attachment_exported() -> None:
    assert "CommandAttachment" in api.__all__
    att = api.CommandAttachment(filename="a.md", content=b"x")
    assert att.fallback_text is None
    assert api.CommandResult(text="t").attachment is None
