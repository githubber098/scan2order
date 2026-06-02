"""stores/zepto_sign.py - Zepto request signing.

The Zepto API gateway requires two SHA-256 hashes per request:
  - request-signature header
  - x-timezone header (despite the misleading name, it's a hash of the signature)

Algorithm cracked from JS modules 23621 and 53133, verified against three
captured requests.

Stdlib hashlib only - no third-party deps.
"""

import hashlib


def compute_signature(method: str, path_with_query: str, request_id: str,
                      device_id: str, xsrf_token: str,
                      body: str | None) -> tuple[str, str]:
    """Returns (request_signature, x_timezone) for one Zepto API call.

    Args:
        method: HTTP method, lowercase ("get", "post"). Anything else gets
            lowercased anyway.
        path_with_query: URL path + query string. For a request to
            https://bff-gateway.zepto.com/api/v3/search?foo=bar this is
            "/api/v3/search?foo=bar". No query params -> just the path.
        request_id: a fresh UUID for THIS request. Also goes into the
            request_id / requestid headers verbatim.
        device_id: the device_id from cookies or storage. Stable per device.
        xsrf_token: the x-xsrf-token cookie value. Acts as the signing
            secret in this scheme.
        body: the JSON-stringified request body, or None for GET. For GET
            the JS coerces undefined to the literal string "undefined" when
            building the join, so we mirror that exactly.

    Returns: (request_signature, x_timezone) - both 64-char lowercase hex.
    """
    body_part = body if body is not None else "undefined"

    parts = {
        "body": body_part,
        "deviceId": device_id,
        "method": method.lower(),
        "requestId": request_id,
        "secret": xsrf_token,
        "url": path_with_query,
    }
    joined = "|".join(parts[k] for k in sorted(parts.keys()))

    request_signature = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    x_timezone = hashlib.sha256(request_signature.encode("utf-8")).hexdigest()
    return request_signature, x_timezone
