"""URL policy shared by saved model settings and provider probes."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

_PRIVATE_HTTP_NETWORKS = tuple(map(ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
)))


def valid_model_base_url(base_url):
    try:
        url = urlsplit(base_url)
        host = url.hostname
        if (not host or url.username or url.password or url.query or url.fragment
                or url.port is not None and not 1 <= url.port <= 65535):
            return False
    except ValueError:
        return False

    if url.scheme == "https":
        return True
    if url.scheme != "http":
        return False
    if host.casefold() == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in _PRIVATE_HTTP_NETWORKS)
