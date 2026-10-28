"""Enumerate APIRoutes, including those inside routers mounted with include_router.

FastAPI 0.162 keeps included routers nested (`_IncludedRouter.original_router`) instead of flattening them.
"""

from typing import Iterable, List

from fastapi.routing import APIRoute


def api_routes(routes: Iterable) -> List[APIRoute]:
    found: List[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        nested = getattr(route, "original_router", None)
        if nested is not None:
            found.extend(api_routes(nested.routes))
    return found
