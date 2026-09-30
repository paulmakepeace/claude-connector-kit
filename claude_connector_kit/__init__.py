"""Shared pieces for MCP connectors used from Claude clients: an OAuth 2.1 server
with a login page, and tool registration shaped for what a client shows the model."""

from claude_connector_kit.login import add_login
from claude_connector_kit.oauth import LoginRefused, Provider, Store, auth_settings
from claude_connector_kit.signin import sign_in
from claude_connector_kit.tools import READS, about, model_view, result, tool, writes

__all__ = ["READS", "LoginRefused", "Provider", "Store", "about", "add_login", "auth_settings",
           "model_view", "result", "sign_in", "tool", "writes"]
