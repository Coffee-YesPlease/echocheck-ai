import pytest
from backend.run import trusted_proxy_ips


@pytest.mark.parametrize('value', ['', '  ', '127.0.0.1', '10.3.0.0/24', '127.0.0.1, ::1'])
def test_explicit_proxy_networks(value):
    assert isinstance(trusted_proxy_ips(value), str)


@pytest.mark.parametrize('value', ['*', '0.0.0.0/0', '::/0', '127.0.0.1,', 'proxy.example.com'])
def test_wildcard_or_ambiguous_proxy_trust_rejected(value):
    with pytest.raises(ValueError):
        trusted_proxy_ips(value)
