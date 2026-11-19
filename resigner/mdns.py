"""A small DNS-SD (Bonjour) browser, standard library only.

The Apple TV announces itself on the local network: `_remotepairing._tcp` while it can be reached for installs,
and `_remotepairing-manual-pairing._tcp` while it is waiting for a pairing PIN (Settings > Remote and Devices >
Remote App and Devices). This asks for those services and collects the answers. It sends from an ordinary port
and asks responders to reply to it directly, so it needs neither avahi nor the multicast port, and it can't get in
the way of netmuxd, which listens on that port for iPhones.
"""
import fcntl
import select
import socket
import struct
import time

MDNS_ADDR = ("224.0.0.251", 5353)
SIOCGIFADDR = 0x8915
TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_AAAA, TYPE_SRV = 1, 12, 16, 28, 33
QU = 0x8000  # "answer me directly": the unicast-response bit of the question class


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def query(questions):
    """A DNS message asking [(name, type), ...]."""
    msg = struct.pack(">HHHHHH", 0, 0, len(questions), 0, 0, 0)
    for name, qtype in questions:
        msg += encode_name(name) + struct.pack(">HH", qtype, 1 | QU)
    return msg


def _name(data, pos, depth=0):
    labels, jumped, end = [], False, pos
    while True:
        if pos >= len(data) or depth > 20:
            raise ValueError("bad DNS name")
        n = data[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                end = pos + 2
            sub, _ = _name(data, ptr, depth + 1)
            labels.append(sub)
            jumped = True
            break
        labels.append(data[pos + 1:pos + 1 + n].decode("utf-8", "replace"))
        pos += 1 + n
    if not jumped:
        end = pos
    return ".".join(x for x in labels if x), end


def parse(data):
    """Records of a DNS message as [(name, type, value)]. A malformed message gives whatever was readable."""
    out = []
    try:
        _id, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
        pos = 12
        for _ in range(qd):
            _, pos = _name(data, pos)
            pos += 4
        for _ in range(an + ns + ar):
            name, pos = _name(data, pos)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            rd = data[pos:pos + rdlen]
            if rtype == TYPE_PTR:
                out.append((name, rtype, _name(data, pos)[0]))
            elif rtype == TYPE_SRV:
                port = struct.unpack(">H", rd[4:6])[0]  # after priority and weight
                out.append((name, rtype, (_name(data, pos + 6)[0], port)))
            elif rtype == TYPE_TXT:
                out.append((name, rtype, _txt(rd)))
            elif rtype == TYPE_A and rdlen == 4:
                out.append((name, rtype, socket.inet_ntoa(rd)))
            elif rtype == TYPE_AAAA and rdlen == 16:
                out.append((name, rtype, socket.inet_ntop(socket.AF_INET6, rd)))
            pos += rdlen
    except (ValueError, struct.error, IndexError):
        pass
    return out


def _txt(rd):
    out, pos = {}, 0
    while pos < len(rd):
        n = rd[pos]
        item = rd[pos + 1:pos + 1 + n].decode("utf-8", "replace")
        pos += 1 + n
        key, eq, value = item.partition("=")
        if key:
            out[key] = value if eq else ""
    return out


class Answers:
    """Everything heard so far, put together into services."""

    def __init__(self):
        self.ptr, self.srv, self.txt, self.addr = {}, {}, {}, {}

    def add(self, records):
        for name, rtype, value in records:
            name = name.lower()
            if rtype == TYPE_PTR:
                self.ptr.setdefault(name, set()).add(value)
            elif rtype == TYPE_SRV:
                self.srv[name.lower()] = (value[0].lower(), value[1])
            elif rtype == TYPE_TXT:
                self.txt[name] = value
            elif rtype in (TYPE_A, TYPE_AAAA):
                self.addr.setdefault(name, [])
                if value not in self.addr[name]:
                    self.addr[name].append(value)

    def instances(self, stype):
        """Instance names announced for a service type given as "_x._tcp.local"."""
        return sorted(self.ptr.get(stype.lower(), set()))

    def missing(self, stypes):
        """Questions still needed to complete the services found: SRV/TXT of an instance, the address of a host."""
        ask = []
        for stype in stypes:
            for inst in self.instances(stype):
                key = inst.lower()
                if key not in self.srv:
                    ask += [(inst, TYPE_SRV), (inst, TYPE_TXT)]
                elif self.srv[key][0] not in self.addr:
                    ask += [(self.srv[key][0], TYPE_A), (self.srv[key][0], TYPE_AAAA)]
        return ask

    def services(self, stype):
        out = []
        for inst in self.instances(stype):
            key = inst.lower()
            if key not in self.srv:
                continue
            host, port = self.srv[key]
            addrs = self.addr.get(host, [])
            v4 = [a for a in addrs if ":" not in a]
            v6 = [a for a in addrs if ":" in a and not a.lower().startswith("fe80")]  # link-local needs a scope id
            if not (v4 or v6):
                continue
            label = inst[:-len(stype) - 1] if inst.lower().endswith("." + stype.lower()) else inst.split(".")[0]
            out.append({"type": stype[:-len(".local")], "instance": inst, "name": label.replace("\\", ""), "host": host, "port": port,
                        "ip": (v4 or v6)[0], "addrs": v4 + v6, "txt": self.txt.get(key, {})})
        return out


def interfaces():
    """IPv4 address of every network interface that is up (the loopback aside), so every network gets asked."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            if name == "lo":
                continue
            try:
                raw = fcntl.ioctl(probe.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
            except OSError:
                continue
            found.append(socket.inet_ntoa(raw[20:24]))
    finally:
        probe.close()
    return found


def _sockets(local):
    socks = []
    for addr in local or [""]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((addr, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if addr:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            s.setblocking(False)
            socks.append(s)
        except OSError:
            s.close()
    return socks


def browse(types, timeout=3.0, dest=None, local=None):
    """Looks for services of these types (like "_remotepairing._tcp") and returns what was found.

    `dest` and `local` are for tests: where the questions go and which local addresses they leave from."""
    dest = dest or MDNS_ADDR
    names = [t.rstrip(".") + ".local" for t in types]
    socks = _sockets(interfaces() if local is None else local)
    answers = Answers()
    deadline = time.time() + timeout
    next_send, followup = 0.0, []
    try:
        while time.time() < deadline and socks:
            now = time.time()
            if now >= next_send:
                for s in socks:
                    try:
                        s.sendto(query([(n, TYPE_PTR) for n in names]), dest)
                        if followup:
                            s.sendto(query(followup), dest)
                    except OSError:
                        pass
                next_send = now + 0.8
            ready, _, _ = select.select(socks, [], [], 0.2)
            for s in ready:
                try:
                    data, _src = s.recvfrom(9000)
                except OSError:
                    continue
                answers.add(parse(data))
            followup = answers.missing(names)
    finally:
        for s in socks:
            s.close()
    return [item for name in names for item in answers.services(name)]
