"""Forward one profile's DNS to a configured recursive resolver; publish observed IPv4 TTLs."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import struct
import time

import dns.asyncquery
import dns.flags
import dns.message
import dns.opcode
import dns.rcode
import dns.rdatatype

LISTEN = os.environ["DNS_LISTEN"]
SOURCE = ipaddress.IPv4Network(os.environ["DNS_SOURCE"])
UPSTREAM = str(ipaddress.IPv4Address(os.environ["DNS_UPSTREAM"]))
OUTPUT = Path(os.environ.get("DNS_ANSWERS_FILE", "/observations/answers.json"))
cache = {}
slots = asyncio.Semaphore(32)


def record(query, answer):
    q = query.question[0]
    if q.rdtype != dns.rdatatype.A or answer.rcode() != dns.rcode.NOERROR:
        return
    chain = answer.resolve_chaining()
    if chain.answer is None:
        return
    now = time.time()
    name = q.name.to_text(omit_final_dot=True).lower()
    for key in list(cache):
        if cache[key] <= now:
            del cache[key]
    for address in chain.answer:
        cache[(name, address.address)] = now + chain.minimum_ttl
    if len(cache) > 16384:
        cache.clear()
        raise ValueError("DNS observation limit exceeded")
    rows = [dict(domain=d, address=a, expires=t) for (d, a), t in cache.items()]
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    os.replace(tmp, OUTPUT)


async def resolve(packet, tcp):
    query = dns.message.from_wire(packet)
    if len(query.question) != 1 or query.opcode() != dns.opcode.QUERY or query.flags & dns.flags.QR:
        raise ValueError("invalid question")
    try:
        if tcp:
            answer = await dns.asyncquery.tcp(query, UPSTREAM, timeout=3)
        else:
            answer = await dns.asyncquery.udp(query, UPSTREAM, timeout=3)
        if not answer.flags & dns.flags.TC:
            record(query, answer)
    except Exception:
        answer = dns.message.make_response(query)
        answer.set_rcode(dns.rcode.SERVFAIL)
    return answer.to_wire()


async def udp(packet, addr, transport):
    async with slots:
        try:
            transport.sendto(await resolve(packet, False), addr)
        except (ValueError, dns.exception.DNSException):
            pass


class Datagram(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if ipaddress.ip_address(addr[0]) in SOURCE and not slots.locked():
            asyncio.create_task(udp(data, addr, self.transport))


async def tcp_exchange(reader, writer):
    size, = struct.unpack("!H", await reader.readexactly(2))
    reply = await resolve(await reader.readexactly(size), True)
    writer.write(struct.pack("!H", len(reply)) + reply)
    await writer.drain()


async def tcp(reader, writer):
    try:
        if ipaddress.ip_address(writer.get_extra_info("peername")[0]) not in SOURCE or slots.locked():
            return
        async with slots:
            await asyncio.wait_for(tcp_exchange(reader, writer), timeout=8)
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, dns.exception.DNSException):
        pass
    finally:
        writer.close()


async def main():
    if ipaddress.IPv4Address(LISTEN) not in SOURCE or UPSTREAM == LISTEN:
        raise ValueError("invalid profile DNS addresses")
    OUTPUT.write_text("[]")
    await asyncio.get_running_loop().create_datagram_endpoint(Datagram, local_addr=(LISTEN, 53))
    server = await asyncio.start_server(tcp, LISTEN, 53)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
