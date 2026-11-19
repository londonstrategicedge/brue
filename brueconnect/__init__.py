"""Brue Connect: the universal broker connector.

Platform side:  Connector([...cmd]).connect() -> request()/events
Adapter side:   subclass Adapter, implement handle_* methods, .run()
Wire format:    protocol module (SPEC.md is the law)
"""
__version__ = "0.0.2"

from .protocol import PROTOCOL_VERSION, ERROR_CODES, TIMEFRAMES_MS, ProtocolError
from .adapter import Adapter, AdapterError
from .client import Connector, ConnectorError, Disconnected, NotArmed

__all__ = ["PROTOCOL_VERSION", "ERROR_CODES", "TIMEFRAMES_MS", "ProtocolError",
           "Adapter", "AdapterError", "Connector", "ConnectorError", "NotArmed",
           "Disconnected"]
