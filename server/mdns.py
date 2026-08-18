import asyncio
import socket
import struct
import time

MDNS_ADDRESS = ("224.0.0.251", 5353)


async def resolve_ipv4(hostname: str, timeout: float = 3.0) -> str:
    """Resolve a .local host directly over multicast DNS without a host daemon."""
    return await asyncio.to_thread(_resolve_ipv4, hostname.rstrip("."), timeout)


def _resolve_ipv4(hostname: str, timeout: float) -> str:
    encoded_name = b"".join(bytes([len(label)]) + label.encode("utf-8") for label in hostname.split(".")) + b"\0"
    # Das QU-Bit fordert eine direkte Antwort an den temporären Quellport an.
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + encoded_name + struct.pack(">HH", 1, 0x8001)
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        sock.sendto(query, MDNS_ADDRESS)
        while time.monotonic() < deadline:
            try:
                payload, _ = sock.recvfrom(9000)
            except socket.timeout:
                break
            address = _find_ipv4(payload, hostname)
            if address:
                return address
    raise OSError(f'Bonjour-Name "{hostname}" konnte nicht aufgelöst werden')


def _read_name(payload: bytes, offset: int, visited=None):
    labels, next_offset = [], None
    visited = set() if visited is None else visited
    while offset < len(payload):
        length = payload[offset]
        if length == 0:
            offset += 1
            return ".".join(labels), next_offset or offset
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(payload):
                raise ValueError("Ungültiger DNS-Zeiger")
            pointer = ((length & 0x3F) << 8) | payload[offset + 1]
            if pointer in visited:
                raise ValueError("DNS-Zeigerzyklus")
            visited.add(pointer)
            suffix, _ = _read_name(payload, pointer, visited)
            if suffix:
                labels.append(suffix)
            return ".".join(labels), next_offset or offset + 2
        offset += 1
        if offset + length > len(payload):
            raise ValueError("Ungültiger DNS-Name")
        labels.append(payload[offset:offset + length].decode("utf-8", errors="ignore"))
        offset += length
    raise ValueError("Unvollständiger DNS-Name")


def _find_ipv4(payload: bytes, hostname: str):
    if len(payload) < 12:
        return None
    _, _, questions, answers, authorities, additional = struct.unpack(">HHHHHH", payload[:12])
    offset = 12
    try:
        for _ in range(questions):
            _, offset = _read_name(payload, offset)
            offset += 4
        fallback = None
        for _ in range(answers + authorities + additional):
            name, offset = _read_name(payload, offset)
            if offset + 10 > len(payload):
                return None
            record_type, _, _, length = struct.unpack(">HHIH", payload[offset:offset + 10])
            offset += 10
            data = payload[offset:offset + length]
            offset += length
            if record_type == 1 and length == 4:
                address = socket.inet_ntoa(data)
                if name.rstrip(".").lower() == hostname.rstrip(".").lower():
                    return address
                fallback = fallback or address
        return fallback
    except (ValueError, struct.error):
        return None
