"""Shared constants for the httpx-based store modules.

Kept in its own module (not stores/__init__.py) so the package import has zero
side effects — useful for the pytest suite which monkeypatches user_store
before any store code runs.

Note: auth_browser.py has a separate _MOBILE_UA (Chrome 131) that must match
Playwright's bundled Chromium version. Changing this one only affects the
httpx requests that the store modules make after authentication.
"""

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)
