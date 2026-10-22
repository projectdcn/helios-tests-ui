"""helios/afm.py client helpers."""


def test_unreachable_reason_is_one_line():
    import requests
    from helios.afm import unreachable_reason
    reset = requests.ConnectionError(("Connection aborted.", ConnectionResetError(54, "Connection reset by peer")))
    assert unreachable_reason(reset).startswith("connection reset during the TLS handshake")
    assert unreachable_reason(requests.ConnectTimeout("timed out")).startswith("connection timed out")
    assert unreachable_reason(requests.ConnectionError("[Errno 61] Connection refused")).startswith("connection refused")
    assert "\n" not in unreachable_reason(reset) and len(unreachable_reason(ValueError("x" * 999))) <= 240
