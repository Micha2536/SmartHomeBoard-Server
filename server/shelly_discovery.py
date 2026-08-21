import socket
import struct
import time


MDNS_ADDRESS = ("224.0.0.251", 5353)
SHELLY_SERVICE = "_shelly._tcp.local"


def discover_shelly_ipv4(timeout=3.0):
    """Discover Gen2+ Shellys through their official DNS-SD service."""
    return discover_service_ipv4(SHELLY_SERVICE, timeout)


def discover_service_ipv4(service, timeout=3.0):
    """Discover IPv4 addresses advertised for a local DNS-SD service."""
    service = str(service).rstrip(".").lower()
    query = _query(service, 12)
    deadline = time.monotonic() + max(0.2, float(timeout))
    records = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(min(0.5, timeout))
        sock.sendto(query, MDNS_ADDRESS)
        next_query = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                payload, _ = sock.recvfrom(65535)
                records.extend(_records(payload))
            except socket.timeout:
                if time.monotonic() >= next_query:
                    sock.sendto(query, MDNS_ADDRESS)
                    next_query = time.monotonic() + 1.0

    instances = {
        value.rstrip(".").lower()
        for name, kind, value in records
        if kind == 12 and name.rstrip(".").lower() == service
    }
    targets = {
        value[1].rstrip(".").lower()
        for name, kind, value in records
        if kind == 33 and (not instances or name.rstrip(".").lower() in instances)
    }
    addresses = {
        value
        for name, kind, value in records
        if kind == 1 and (not targets or name.rstrip(".").lower() in targets)
    }
    # Most Shellys include PTR, SRV and A records in the additional section.
    # A-only responses are accepted as a fallback for older firmware.
    if not addresses:
        addresses = {value for _, kind, value in records if kind == 1}
    return sorted(addresses, key=lambda value: tuple(int(part) for part in value.split(".")))


def _query(name, record_type):
    labels = b"".join(bytes([len(part)]) + part.encode("utf-8") for part in name.split(".")) + b"\0"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + labels + struct.pack(">HH", record_type, 0x8001)


def _read_name(payload, offset, visited=None):
    labels = []
    next_offset = None
    visited = set() if visited is None else visited
    while offset < len(payload):
        length = payload[offset]
        if length == 0:
            return ".".join(labels), next_offset or offset + 1
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
            raise ValueError("Unvollständiger DNS-Name")
        labels.append(payload[offset:offset + length].decode("utf-8", errors="ignore"))
        offset += length
    raise ValueError("Unvollständiger DNS-Name")


def _records(payload):
    if len(payload) < 12:
        return []
    _, _, questions, answers, authorities, additional = struct.unpack(">HHHHHH", payload[:12])
    offset = 12
    result = []
    try:
        for _ in range(questions):
            _, offset = _read_name(payload, offset)
            offset += 4
        for _ in range(answers + authorities + additional):
            name, offset = _read_name(payload, offset)
            record_type, _, _, length = struct.unpack(">HHIH", payload[offset:offset + 10])
            data_offset = offset + 10
            offset = data_offset + length
            if record_type == 1 and length == 4:
                result.append((name, 1, socket.inet_ntoa(payload[data_offset:offset])))
            elif record_type == 12:
                target, _ = _read_name(payload, data_offset)
                result.append((name, 12, target))
            elif record_type == 33 and length >= 6:
                port = struct.unpack(">H", payload[data_offset + 4:data_offset + 6])[0]
                target, _ = _read_name(payload, data_offset + 6)
                result.append((name, 33, (port, target)))
    except (ValueError, struct.error, IndexError):
        return result
    return result
