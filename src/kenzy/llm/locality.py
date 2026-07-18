"""Is the configured language model local or cloud? (4.0.2 privacy slice.)

Private-tier memory must not ride into a model that leaves the house: the
tier walls gate which *voices* hear a fact back, but context injection and
semantic consolidation hand fact text to the configured model — fine when
that model runs on the operator's own hardware, a leak when it's a cloud
provider. This helper is the single place that judgment lives.

Deliberately conservative: anything we can't positively identify as local is
treated as cloud. A hosted LiteLLM proxy or OpenRouter base_url is "cloud"
even though it's the operator's own account — the text still leaves the
house, which is the property that matters here.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

#: Model-string prefixes that imply a local runtime even with no base_url
#: (LiteLLM defaults ollama to localhost:11434).
_LOCAL_PREFIXES = ("ollama/", "ollama_chat/", "lm_studio/")

#: Hostname suffixes that only resolve on a LAN.
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa")


def _host_is_local(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(_LAN_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    # A bare single-label hostname ("mouse", "ollama-box") can't be a public
    # domain — it only resolves on the LAN.
    return "." not in host


def model_is_local(model: str, base_url: str | None) -> bool:
    """True when the model's endpoint stays inside the house.

    ``base_url`` set ⇒ judged by its host (private/loopback/LAN-suffix/bare
    hostname = local; any public domain = cloud, including hosted proxies).
    No ``base_url`` ⇒ judged by the model string (``ollama/…`` = local;
    every provider-routed model = cloud).
    """
    if base_url:
        try:
            return _host_is_local(urlparse(base_url).hostname or "")
        except ValueError:
            return False
    return str(model or "").lower().startswith(_LOCAL_PREFIXES)
