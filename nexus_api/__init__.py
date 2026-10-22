"""Nexus API and OAuth for the Nexus MCP service (Claude integration)."""

from nexus_api.oauth import PUBLIC_OAUTH_ENDPOINTS, register_oauth_routes
from nexus_api.routes import register_api_routes

PUBLIC_ENDPOINT_PREFIXES = ("nexus_api.",)

__all__ = ["PUBLIC_OAUTH_ENDPOINTS", "PUBLIC_ENDPOINT_PREFIXES", "register_api_routes", "register_oauth_routes"]
