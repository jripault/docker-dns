# dockerdns - simple, automatic, self-contained dns server for docker


# monkey patch everything
from gevent import monkey
monkey.patch_all()

# core
import argparse
from collections import namedtuple
from datetime import datetime
import json
import os
import re
import signal
import sys
import time
from urlparse import urlparse

# libs
from dnslib import A, DNSHeader, DNSLabel, DNSRecord, QTYPE, RR
import docker
import gevent
from gevent import socket, threading
from gevent.server import DatagramServer
from gevent.resolver_ares import Resolver


PROCESS = 'dockerdns'
DOCKER_SOCK = 'unix:///docker.sock'
DNS_BINDADDR = '0.0.0.0:53'
DNS_RESOLVER = ['8.8.8.8']
DNS_RESOLVER_TIMEOUT = 3.0
RE_VALIDNAME = re.compile('[^\w\d.-]')
QUIET = 0
EPILOG = '''

'''


Container = namedtuple('Container', 'id, name, running, addr, domains')


def log(msg, *args):
    global QUIET
    if not QUIET:
        now = datetime.now().isoformat()
        line = '%s [%s] %s\n' % (now, PROCESS, msg % args)
        sys.stderr.write(line.encode('utf-8'))
        sys.stderr.flush()


def get(d, *keys):
    empty = {}
    return reduce(lambda d, k: d.get(k, empty), keys, d) or None


def contains(txt, *subs):
    return any(s in txt for s in subs)


class NameTable(object):

    'Table mapping names to addresses'

    def __init__(self, records):
        self._storage = {}
        self._lock = threading.Lock()
        for rec in records:
            self.add(rec[0], rec[1])

    def add(self, name, addr):
        key = self._key(name)
        if key:
            with self._lock:
                log('table.add %s -> %s', name, addr)
                self._storage[key] = addr

    def get(self, name):
        key = self._key(name)
        if key:
            with self._lock:
                res = self._storage.get(key)
                log('table.get %s == %s', name, res)
                return res

    def remove(self, name):
        key = self._key(name)
        if key:
            with self._lock:
                if self._storage.has_key(key):
                    log('table.remove %s', name)
                    del self._storage[key]

    def _key(self, name):
        try:
            return DNSLabel(name.lower()).label
        except Exception:
            return None


class DockerMonitor(object):

    'Reads events from Docker and updates the name table'

    def __init__(self, client, table, domain):
        self._docker = client
        self._table = table
        self._domain = domain.lstrip('.')

    def run(self):
        # start the event poller, but don't read from the stream yet
        events = self._docker.events()

        # bootstrap by inspecting all running containers
        for container in self._docker.containers():
            rec = self._inspect(container['Id'])
            if rec.running:
                self._table.add(rec.name, rec.addr)
                self._handleDomains(rec)

        # read the docker event stream and update the name table
        for raw in events:
            evt = json.loads(raw)
            cid = evt['id']
            status = evt['status']
            if status in ('start', 'die'):
                try:
                    rec = self._inspect(cid)
                    if rec:
                        if status == 'start':
                            self._table.add(rec.name, rec.addr)
                            self._handleDomains(rec)
                        else:
                            self._table.remove(rec.name)
                except Exception, e:
                    print str(e)

    def _handleDomains(self, rec):
        values = [ v for k,v in rec.domains.items() if 'domains' in k]
        print values
        for domain in values:
            self._table.add(domain, rec.addr)

    def _inspect(self, cid):
        # get full details on this container from docker
        metadatas = self._docker.inspect_container(cid)
        # ensure name is valid, and append our domain
        name = metadatas['Name']
        if not name:
            return None
        name = RE_VALIDNAME.sub('', name).rstrip('.')
        name += '.' + self._domain

        return Container(
            metadatas['Id'],
            name,
            metadatas['State']['Running'],
            metadatas['NetworkSettings']['IPAddress'],
            metadatas['Config']['Labels']
        )



class DnsServer(DatagramServer):

    '''
    Answers DNS queries against the name table, falling back to the recursive
    resolver (if present).
    '''

    def __init__(self, bindaddr, table, dns_servers=None):
        DatagramServer.__init__(self, bindaddr)
        self._table = table
        self._resolver = None
        if dns_servers:
            self._resolver = Resolver(servers=dns_servers,
                timeout=DNS_RESOLVER_TIMEOUT, tries=1)

    def handle(self, data, peer):
        rec = DNSRecord.parse(data)
        addr = None
        if rec.q.qtype in (QTYPE.A, QTYPE.AAAA):
            addr = self._table.get(rec.q.qname.idna())
            if not addr:
                addr = self._resolve('.'.join(rec.q.qname.label))
        self.socket.sendto(self._reply(rec, addr), peer)

    def _reply(self, rec, addr=None):
        reply = DNSRecord(DNSHeader(id=rec.header.id, qr=1, aa=1, ra=1), q=rec.q)
        if addr:
            qtype = QTYPE.A if QTYPE.A == rec.q.qtype else QTYPE.AAAA
            reply.add_answer(RR(rec.q.qname, qtype, rdata=A(addr)))

        rep = reply.pack()
        return rep

    def _resolve(self, name):
        if not self._resolver:
            return None
        try:
            return self._resolver.gethostbyname(name)
        except socket.gaierror, e:
            msg = str(e)
            if not contains(msg, 'ETIMEOUT', 'ENOTFOUND'):
                print msg


def stop(*servers):
    for svr in servers:
        if svr.started:
            svr.stop()
    sys.exit(signal.SIGINT)

def splitrecord(rec):
    #m = re.match('([a-zA-Z0-9_-]*):((?:[12]?[0-9]{1,2}\.){3}(?:[12]?[0-9]{1,2}){1}$)', rec)
    #if not m:
    #    log('--record has invalid format, expects: `--record <host>:<ip>`')
    #    sys.exit(1)
    #else:
    #    return (m.group(1), m.group(2))
    return rec.split(":")

def check(args):
    url = urlparse(args.docker)
    if url.scheme in ('unix','unix+http'):
        # check if the socket file exists
        if not os.path.exists(url.path):
            log('unix socket %r does not exist', url.path)
            sys.exit(1)

def parse_args():
    docker_url = os.environ.get('DOCKER_HOST')
    if not docker_url:
        docker_url = DOCKER_SOCK
    parser = argparse.ArgumentParser(PROCESS, epilog=EPILOG,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--docker', default=docker_url,
        help='Url to docker TCP/UNIX socket')
    parser.add_argument('--dns-bind', default=DNS_BINDADDR,
        help='Bind address for DNS server')
    parser.add_argument('--domain', default='docker',
        help='Base domain name for registered services')
    parser.add_argument('--resolver', default=DNS_RESOLVER, nargs='*',
        help='Servers for recursive DNS resolution')
    parser.add_argument('--no-recursion', action='store_const', const=1,
        help='Disables recursive DNS queries')
#    parser.add_argument('--verbose', action='store_const', const=1,
#        help='Be more verbose')
    parser.add_argument('-q', '--quiet', action='store_const', const=1,
        help='Quiet mode')
    parser.add_argument('-r', '--record', nargs="*", default=[],
        help="Add a static record `name:host`")
    return parser.parse_args()


def main():
    log("Custom Docker DNS")
    global QUIET
    args = parse_args()
    check(args)
    if args.record:
        args.record = map(splitrecord, args.record)

    QUIET = args.quiet
    resolver = () if args.no_recursion else args.resolver
    table = NameTable([(k.strip(), v) for (k, v) in args.record])
    monitor = DockerMonitor(docker.Client(args.docker), table, args.domain)
    dns = DnsServer(args.dns_bind, table, resolver)
    gevent.signal(signal.SIGINT, stop, dns)
    dns.start()
    gevent.wait([gevent.spawn(monitor.run)])


if __name__ == '__main__':
    main()
