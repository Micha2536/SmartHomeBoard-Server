import asyncio
import json
import logging
import os


DISCOVERY_PORT = int(os.getenv("SHB_DISPLAY_DISCOVERY_PORT", "8788"))
DISCOVERY_REQUEST = b"SHB_DISCOVER_V1"


def discovery_response(api_port: int, server_id: str, version: str) -> bytes:
    return json.dumps({
        "service": "SmartHomeBoard",
        "protocol": 1,
        "server_id": server_id,
        "version": version,
        "api_port": api_port,
        "registration_path": "/api/v1/displays/register",
    }, separators=(",", ":")).encode("utf-8")


class DisplayDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, response: bytes):
        self.response = response
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if data.strip() == DISCOVERY_REQUEST and self.transport:
            self.transport.sendto(self.response, address)

    def error_received(self, error):
        logging.getLogger("smarthomeboard.displays").warning(
            "Fehler bei der M5Paper-Serversuche: %s", error
        )


async def start_display_discovery(api_port: int, server_id: str, version: str):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DisplayDiscoveryProtocol(discovery_response(api_port, server_id, version)),
        local_addr=("0.0.0.0", DISCOVERY_PORT),
        allow_broadcast=True,
        reuse_port=True,
    )
    logging.getLogger("smarthomeboard.displays").info(
        "M5Paper-Serversuche lauscht auf UDP-Port %d", DISCOVERY_PORT
    )
    return transport
