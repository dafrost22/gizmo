from gizmo.voice_clone import CloneVoiceClient


def test_the_console_sets_the_voice_servers():
    client = CloneVoiceClient()
    client.set_servers(["http://pc:8770", "http://pc-vpn:8770"])
    client.announce("http://laptop:8770")
    assert client.candidates() == ["http://pc:8770", "http://laptop:8770", "http://pc-vpn:8770"]
    assert client.where("http://pc:8770") == "box" and client.where("http://laptop:8770") == "mac"
    client.set_servers([])
    assert client.candidates() == ["http://laptop:8770"] and client.url is None


def test_info_reports_an_unreachable_server():
    info = CloneVoiceClient().info("http://127.0.0.1:9")
    assert info["ok"] is False and info["error"]
