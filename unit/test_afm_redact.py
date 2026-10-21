"""helios.afm.redact: secrets stay out of the logs, selector field names stay readable (TC-CTL-098 18 Oct: an alert
policy requirement {"key": "severity"} was logged as "<REDACTED>", hiding which field the policy matched on)."""
from helios.afm import redact


def test_selector_keys_stay_readable():
    s = '{"requirements": [{"key": "severity", "operator": "equals", "values": ["warn"]}, {"key": "meta.name"}]}'
    assert redact(s) == s


def test_secrets_and_key_material_are_masked():
    for s in ('{"password": "pw"}', '{"token": "abc"}', '{"private-key": "x"}', '{"recoveryKey": "x"}',
              '{"key": "-----BEGIN PRIVATE KEY-----MIIE"}', '{"key": "MIIEvQIBADANBgkqhkiG9w0BAQEF"}',
              '{"key": "abc123def456ghi789jkl012mno345pqr678"}'):
        assert "<REDACTED>" in redact(s), s
    assert redact("Cookie: sid=abcdef; x") == "Cookie: sid=<REDACTED>; x"
