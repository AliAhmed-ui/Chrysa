"""Test package. The Browse page asks IMDb for title suggestions once internet access is
allowed; tests never reach the network, so that lookup is made to fail as if offline."""

from chrysa import suggest


def _offline(url: str) -> object:
    raise OSError(f"tests are offline: {url}")


suggest._get = _offline
