"""The DNS-SD browser against a fake Apple TV answering on the loopback interface."""
import socket
import struct
import threading
import unittest

from resigner import mdns


def name(n):
    return mdns.encode_name(n)


def rr(owner, rtype, rdata, ttl=120):
    return name(owner) + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata


def txt(**kv):
    return b"".join(bytes([len(f"{k}={v}")]) + f"{k}={v}".encode() for k, v in kv.items())


def response(records):
    return struct.pack(">HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)


SERVICE = "_remotepairing._tcp.local"
INSTANCE = "Living Room._remotepairing._tcp.local"
HOST = "Living-Room.local"


def full_answer():
    return response([
        rr(SERVICE, mdns.TYPE_PTR, name(INSTANCE)),
        rr(INSTANCE, mdns.TYPE_SRV, struct.pack(">HHH", 0, 0, 49152) + name(HOST)),
        rr(INSTANCE, mdns.TYPE_TXT, txt(identifier="ID-1", authTag="TAG-1")),
        rr(HOST, mdns.TYPE_A, socket.inet_aton("192.168.1.50")),
        rr(HOST, mdns.TYPE_AAAA, socket.inet_pton(socket.AF_INET6, "fe80::1")),
    ])


class Parsing(unittest.TestCase):
    def test_a_full_answer_becomes_a_service(self):
        a = mdns.Answers()
        a.add(mdns.parse(full_answer()))
        [svc] = a.services(SERVICE)
        self.assertEqual((svc["name"], svc["ip"], svc["port"]), ("Living Room", "192.168.1.50", 49152))
        self.assertEqual(svc["txt"], {"identifier": "ID-1", "authTag": "TAG-1"})
        self.assertEqual(svc["type"], "_remotepairing._tcp")

    def test_link_local_ipv6_is_not_offered(self):
        a = mdns.Answers()
        a.add(mdns.parse(full_answer()))
        self.assertEqual(a.services(SERVICE)[0]["addrs"], ["192.168.1.50"])

    def test_a_service_without_an_address_is_left_out_and_asked_for(self):
        a = mdns.Answers()
        a.add(mdns.parse(response([
            rr(SERVICE, mdns.TYPE_PTR, name(INSTANCE)),
            rr(INSTANCE, mdns.TYPE_SRV, struct.pack(">HHH", 0, 0, 49152) + name(HOST)),
        ])))
        self.assertEqual(a.services(SERVICE), [])
        self.assertEqual(a.missing([SERVICE]), [(HOST.lower(), mdns.TYPE_A), (HOST.lower(), mdns.TYPE_AAAA)])

    def test_missing_srv_is_asked_for(self):
        a = mdns.Answers()
        a.add(mdns.parse(response([rr(SERVICE, mdns.TYPE_PTR, name(INSTANCE))])))
        self.assertEqual(a.missing([SERVICE]), [(INSTANCE, mdns.TYPE_SRV), (INSTANCE, mdns.TYPE_TXT)])

    def test_compressed_names_are_followed(self):
        head = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        owner = name("x.local")                      # starts at offset 12
        rdata = b"\x04inst" + struct.pack(">H", 0xC000 | 12)  # "inst" + a pointer back to "x.local"
        msg = head + owner + struct.pack(">HHIH", mdns.TYPE_PTR, 1, 1, len(rdata)) + rdata
        self.assertEqual(mdns.parse(msg), [("x.local", mdns.TYPE_PTR, "inst.x.local")])

    def test_a_pointer_loop_does_not_hang(self):
        data = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 1, 4) + b"\0" * 4
        self.assertEqual(mdns.parse(data), [])

    def test_garbage_gives_nothing(self):
        self.assertEqual(mdns.parse(b"\x00\x01"), [])
        self.assertEqual(mdns.parse(b""), [])

    def test_query_asks_for_a_direct_answer(self):
        q = mdns.query([("_remotepairing._tcp.local", mdns.TYPE_PTR)])
        self.assertEqual(struct.unpack(">HHHHHH", q[:12])[2], 1)
        self.assertEqual(q[-2:], struct.pack(">H", 1 | mdns.QU))


class Responder(threading.Thread):
    """Answers every question it gets with `reply`, like an Apple TV would."""

    def __init__(self, reply):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.addr, self.reply, self.stop, self.asked = self.sock.getsockname(), reply, False, []

    def run(self):
        while not self.stop:
            try:
                data, src = self.sock.recvfrom(9000)
            except socket.timeout:
                continue
            self.asked.append(data)
            self.sock.sendto(self.reply, src)

    def close(self):
        self.stop = True
        self.join()
        self.sock.close()


class Browse(unittest.TestCase):
    def test_finds_the_tv_over_the_network(self):
        r = Responder(full_answer())
        r.start()
        try:
            found = mdns.browse(["_remotepairing._tcp"], timeout=1.5, dest=r.addr, local=["127.0.0.1"])
        finally:
            r.close()
        self.assertEqual([(s["name"], s["ip"], s["port"]) for s in found], [("Living Room", "192.168.1.50", 49152)])
        self.assertTrue(r.asked)

    def test_nobody_answering_gives_an_empty_list(self):
        r = Responder(b"")
        r.start()
        try:
            self.assertEqual(mdns.browse(["_remotepairing._tcp"], timeout=0.6, dest=r.addr, local=["127.0.0.1"]), [])
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
