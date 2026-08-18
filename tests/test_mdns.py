import socket
import struct
import unittest

from server.mdns import _find_ipv4


class MDNSTests(unittest.TestCase):
    def test_extracts_ipv4_answer(self):
        name = b"\x03app\x05local\x00"
        header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        answer = name + struct.pack(">HHIH", 1, 1, 120, 4) + socket.inet_aton("192.168.1.44")
        self.assertEqual(_find_ipv4(header + answer, "app.local"), "192.168.1.44")


if __name__ == "__main__":
    unittest.main()
