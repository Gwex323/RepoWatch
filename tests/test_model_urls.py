import pytest

from repowatch.model_urls import valid_model_base_url


@pytest.mark.parametrize("url", [
    "https://provider.example/v1",
    "http://localhost:3000/v1",
    "http://127.0.0.1:3000/v1",
    "http://[::1]:3000/v1",
    "http://10.12.0.8:3000/v1",
    "http://172.16.116.14:3000/v1",
    "http://172.31.255.254:3000/v1",
    "http://192.168.1.10:3000/v1",
    "http://[fd12:3456::10]:3000/v1",
])
def test_valid_model_urls_allow_https_loopback_and_private_networks(url):
    assert valid_model_base_url(url)


@pytest.mark.parametrize("url", [
    "http://provider.example/v1",
    "http://8.8.8.8/v1",
    "http://172.15.255.255/v1",
    "http://172.32.0.1/v1",
    "http://192.167.1.10/v1",
    "http://169.254.1.10/v1",
    "http://100.64.0.1/v1",
    "ftp://172.16.0.1/v1",
    "http://user:password@172.16.0.1/v1",
    "http://172.16.0.1/v1?token=hidden",
    "http://172.16.0.1:70000/v1",
])
def test_valid_model_urls_reject_public_http_and_malformed_urls(url):
    assert not valid_model_base_url(url)
