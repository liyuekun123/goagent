#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Based on GAppProxy 2.0.0 by Du XiaoGang <dugang@188.com>
# Based on WallProxy 0.4.0 by hexieshe <www.ehust@gmail.com>

import sys, os, re, time
import errno, zlib, random, struct, traceback
import httplib, urllib2, urlparse, socket, select
import thread, BaseHTTPServer, SocketServer
import ConfigParser
import OpenSSL

__version__ = '1.0'
__author__ =  'phus.lu@gmail.com'

class RandomTCPConnection(object):
    '''random tcp connection class'''
    CONNECT_COUNT = 4
    CONNECT_TIMEOUT = 2
    def __init__(self, hosts, port):
        self.socket = None
        self.__socs = []
        self.connect(hosts, port)
    def connect(self, hosts, port):
        hosts = filter(None, hosts)
        if len(hosts) > self.CONNECT_COUNT:
            hosts = random.sample(hosts, self.CONNECT_COUNT)
        for host in hosts:
            soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            soc.setblocking(0)
            #soc.settimeout(2)
            err = soc.connect_ex((host, port))
            self.__socs.append(soc)
        (_, outs, _) = select.select([], self.__socs, [], self.CONNECT_TIMEOUT)
        if outs:
            self.socket = outs[0]
            self.socket.setblocking(1)
    def close(self):
        for soc in self.__socs:
            try:
                soc.close()
            except:
                pass


class Common(object):
    '''global config module, based on GappProxy 2.0.0'''
    FILENAME = os.path.splitext(__file__)[0] + '.ini'
    ConfigParser.RawConfigParser.OPTCRE = re.compile(r'(?P<option>[^=\s][^=]*)\s*(?P<vi>[=])\s*(?P<value>.*)$')

    def __init__(self):
        '''read config from proxy.ini'''
        self.config = ConfigParser.ConfigParser()
        self.config.read(self.FILENAME)
        self.LISTEN_IP   = self.config.get('listen', 'ip')
        self.LISTEN_PORT = self.config.getint('listen', 'port')
        self.FETCH_HOST  = self.config.get('fetch', 'host')
        self.FETCH_PATH  = self.config.get('fetch', 'path')
        if self.config.has_option('fetch', 'proxy'):
            proxies = self.config.get('fetch', 'proxy')
            self.FETCH_PROXY = dict(re.match(r'^(\w+)://(\S+)$', proxy.strip()).group(1, 2) for proxy in proxies.split('|'))
        else:
            self.FETCH_PROXY = {}
        self.HOSTS = dict((k, re.split(r'[,|]', v)) for k, v in self.config.items('hosts'))
        self.select_lock = thread.allocate_lock()
        self.select('http')

    def select(self, scheme='https'):
        '''select a available fetch server ip from proxy.ini ip list'''
        if scheme not in ('https', 'http'):
            raise TypeError(u'Common.select scheme must in (\'https\', \'http\')')
        hosts = self.config.get('fetch', scheme).split(':')[0].split('|')
        port  = int(self.config.get('fetch', scheme).split(':')[1])
        if not all(hosts):
            return
        random.shuffle(hosts)
        for hosts in  [hosts[i:i+RandomTCPConnection.CONNECT_COUNT] for i in xrange(0,len(hosts),RandomTCPConnection.CONNECT_COUNT)]:
            conn = RandomTCPConnection(hosts, port)
            if conn.socket is not None:
                fetch_ip     = conn.socket.getpeername()[0]
                fetch_server = '%s://%s:%s/%s' % (scheme, self.FETCH_HOST, port, self.FETCH_PATH.lstrip('/'))
                fetch_server_raw = '%s://%s:%s/%s' % (scheme, fetch_ip, port, self.FETCH_PATH.lstrip('/'))
                self.select_lock.acquire()
                self.FETCH_IP = fetch_ip
                self.FETCH_SERVER = fetch_server
                self.FETCH_SERVER_RAW = fetch_server_raw
                self.select_lock.release()
                conn.close()
                break
            else:
                conn.close()
        else:
            raise RuntimeError('Common RandomTCPConnection cannot select_http(s)_ip from %r!' % hosts)

    def show(self):
        '''show current config'''
        print '--------------------------------------------'
        print 'HTTPS Enabled: Yes'
        print 'Listen Addr  : %s:%d' % (self.LISTEN_IP, self.LISTEN_PORT)
        print 'Local Proxy  : %s' % (self.FETCH_PROXY if self.FETCH_PROXY else 'Disabled')
        print 'Fetch Server : %s' % self.FETCH_SERVER
        print 'Fetch IP     : %s' % self.FETCH_IP
        print '--------------------------------------------'

common = Common()

class RootCA(object):
    BASEDIR = os.path.dirname(__file__)

    def __init__(self):
        homedir = os.environ['USERPROFILE' if os.name == 'nt' else 'HOME']
        self.cert_dir = os.path.join(homedir, '.gacert')
        self.checkCA()

    def readFile(self, filename):
        try:
            f = open(filename, 'rb')
            c = f.read()
            f.close()
            return c
        except IOError:
            return None

    def writeFile(self, filename, content):
        f = open(filename, 'wb')
        f.write(str(content))
        f.close()

    def createKeyPair(self, type=None, bits=1024):
        if type is None:
            type = OpenSSL.crypto.TYPE_RSA
        pkey = OpenSSL.crypto.PKey()
        pkey.generate_key(type, bits)
        return pkey

    def createCertRequest(self, pkey, digest='sha1', **subj):
        req = OpenSSL.crypto.X509Req()
        subject = req.get_subject()
        for k,v in subj.iteritems():
            setattr(subject, k, v)
        req.set_pubkey(pkey)
        req.sign(pkey, digest)
        return req

    def createCertificate(self, req, (issuerKey, issuerCert), serial, (notBefore, notAfter), digest='sha1'):
        cert = OpenSSL.crypto.X509()
        cert.set_serial_number(serial)
        cert.gmtime_adj_notBefore(notBefore)
        cert.gmtime_adj_notAfter(notAfter)
        cert.set_issuer(issuerCert.get_subject())
        cert.set_subject(req.get_subject())
        cert.set_pubkey(req.get_pubkey())
        cert.sign(issuerKey, digest)
        return cert

    def loadPEM(self, pem, type):
        handlers = ('load_privatekey', 'load_certificate_request', 'load_certificate')
        return getattr(OpenSSL.crypto, handlers[type])(OpenSSL.crypto.FILETYPE_PEM, pem)

    def dumpPEM(self, obj, type):
        handlers = ('dump_privatekey', 'dump_certificate_request', 'dump_certificate')
        return getattr(OpenSSL.crypto, handlers[type])(OpenSSL.crypto.FILETYPE_PEM, obj)

    def makeCA(self):
        pkey = self.createKeyPair(bits=2048)
        subj = {'countryName': 'CN', 'stateOrProvinceName': 'Internet',
                'localityName': 'Cernet', 'organizationName': 'WallProxy',
                'organizationalUnitName': 'WallProxy Root', 'commonName': 'WallProxy CA'}
        req = self.createCertRequest(pkey, **subj)
        cert = self.createCertificate(req, (pkey, req), 0, (0, 60*60*24*7305))  #20 years
        return (self.dumpPEM(pkey, 0), self.dumpPEM(cert, 2))

    def makeCert(self, host, (cakey, cacrt), serial):
        pkey = self.createKeyPair()
        subj = {'countryName': 'CN', 'stateOrProvinceName': 'Internet',
                'localityName': 'Cernet', 'organizationName': host,
                'organizationalUnitName': 'WallProxy Branch', 'commonName': host}
        req = self.createCertRequest(pkey, **subj)
        cert = self.createCertificate(req, (cakey, cacrt), serial, (0, 60*60*24*7305))
        return (self.dumpPEM(pkey, 0), self.dumpPEM(cert, 2))

    def getCertificate(self, host):
        keyFile = os.path.join(self.cert_dir, '%s.key' % host)
        crtFile = os.path.join(self.cert_dir, '%s.crt' % host)
        if not os.path.isfile(keyFile) or not os.path.isfile(crtFile):
            if not OpenSSL.crypto:
                keyFile = os.path.join(self.BASEDIR, 'ssl/ca.key')
                crtFile = os.path.join(self.BASEDIR, 'ssl/ca.crt')
                return (keyFile, crtFile)
            self.SERIAL += 1
            key, crt = self.makeCert(host, self.CA, self.SERIAL)
            self.writeFile(keyFile, key)
            self.writeFile(crtFile, crt)
            self.writeFile(os.path.join(self.BASEDIR, 'ssl/serial'), self.SERIAL)
        return (keyFile, crtFile)

    def checkCA(self):
        #Check cert directory
        if not os.path.isdir(self.cert_dir):
            if os.path.isfile(self.cert_dir):
                os.remove(self.cert_dir)
            if os.system('ssl\\addroot.bat') != 0:
                raise OSError(u'Cannot add ssl\\ca.crt as Root Trust CA')
            os.mkdir(self.cert_dir)
        #Check CA file
        cakeyFile = os.path.join(self.BASEDIR, 'ssl/ca.key')
        cacrtFile = os.path.join(self.BASEDIR, 'ssl/ca.crt')
        serialFile = os.path.join(self.BASEDIR, 'ssl/serial')
        cakey = self.readFile(cakeyFile)
        cacrt = self.readFile(cacrtFile)
        self.SERIAL = self.readFile(serialFile)
        try:
            self.CA = (self.loadPEM(cakey, 0), self.loadPEM(cacrt, 2))
            self.SERIAL = int(self.SERIAL)
        except:
            cakey, cacrt = self.makeCA()
            self.SERIAL = 0
            #Remove old certifications, because ca and cert must be in pair
            for name in os.listdir(self.cert_dir):
                path = os.path.join(self.cert_dir, name)
                if os.path.isfile(path):
                    os.remove(path)
            self.writeFile(cakeyFile, cakey)
            self.writeFile(cacrtFile, cacrt)
            self.writeFile(serialFile, self.SERIAL)
            self.CA = (self.loadPEM(cakey, 0), self.loadPEM(cacrt, 2))

ROOTCA = RootCA()

class Encoder(object):
    def encode(self, dic):
        def _quote(s):
            return str(s).replace('\x10', '\x100').replace('=','\x101').replace('&','\x102')
        res = []
        for k,v in dic.iteritems():
            res.append('%s=%s' % (_quote(k), _quote(v)))
        return '&'.join(res)

    def decode(self, qs, keep_blank_values=False, strict_parsing=False):
        def _unquote(s):
            unquote_map = {'0':'\x10', '1':'=', '2':'&'}
            res = s.split('\x10')
            for i in xrange(1, len(res)):
                item = res[i]
                try:
                    res[i] = unquote_map[item[0]] + item[1:]
                except KeyError:
                    res[i] = '\x10' + item
            return ''.join(res)
        pairs = qs.split('&')
        dic = {}
        for name_value in pairs:
            if not name_value and not strict_parsing:
                continue
            nv = name_value.split('=', 1)
            if len(nv) != 2:
                if strict_parsing:
                    raise ValueError, 'bad query field: %r' % (name_value,)
                if keep_blank_values:
                    nv.append('')
                else:
                    continue
            if len(nv[1]) or keep_blank_values:
                dic[_unquote(nv[0])] = _unquote(nv[1])
        return dic

encoder = Encoder()

class LocalProxyHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    partSize = 1024000
    fetchTimeout = 5
    FR_Headers = ('', 'host', 'vary', 'via', 'x-forwarded-for', 'proxy-authorization', 'proxy-connection', 'upgrade', 'keep-alive')

    def address_string(self):
        return '%s:%s' % self.client_address[:2]

    def send_response(self, code, message=None):
        self.log_request(code)
        if message is None:
            if code in self.responses:
                message = self.responses[code][0]
            else:
                message = 'GoAgent Notify'
        if self.request_version != 'HTTP/0.9':
            self.wfile.write('%s %d %s\r\n' % (self.protocol_version, code, message))

    def end_error(self, code, message=None, data=None):
        if not data:
            self.send_error(code, message)
        else:
            self.send_response(code, message)
            self.wfile.write(data)
        self.connection.close()

    def do_CONNECT(self):
        if self.path in common.HOSTS:
            return self.do_CONNECT_1()
        else:
            return self.do_CONNECT_2()

    def do_CONNECT_1(self):
        MAX_IDLING = 30
        try:
            hosts = common.HOSTS[self.path]
            port  = int(self.path.split(':')[1])
            self.log_message('Random TCPConnection to %s within %d hosts' % (self.path, len(hosts)))
            conn = RandomTCPConnection(hosts, port)
            if conn.socket is None:
                self.send_error(502, 'Cannot Connect to %s:%s' % (hosts, port))
                return
            self.log_request(200)
            self.wfile.write('%s 200 Connection established\r\n' % self.protocol_version)
            self.wfile.write('Proxy-agent: %s\r\n\r\n' % self.version_string())

            socs = [self.connection, conn.socket]
            count = 0
            while 1:
                count += 1
                (recv, _, error) = select.select(socs, [], socs, 2)
                if error:
                    break
                if recv:
                    for in_ in recv:
                        data = in_.recv(8192)
                        if in_ is self.connection:
                            out = conn.socket
                        else:
                            out = self.connection
                        if data:
                            out.send(data)
                            count = 0
                if count == MAX_IDLING:
                    break
        except:
            exc_info = traceback.format_exc()
            sys.stderr.write(exc_info)
            self.send_error(502, exc_info)
        finally:
            for soc in (self.connection, conn):
                try:
                    soc.close()
                except:
                    pass

    def do_CONNECT_2(self):
        # for ssl proxy
        host, _, port = self.path.rpartition(':')
        keyFile, crtFile = ROOTCA.getCertificate(host)
        self.send_response(200)
        self.end_headers()
        try:
            ssl_sock = ssl.wrap_socket(self.connection, keyFile, crtFile, True)
        except ssl.SSLError, e:
            print 'SSLError: ' + str(e)
            return

        # rewrite request line, url to abs
        first_line = ''
        while True:
            data = ssl_sock.read()
            # EOF?
            if data == '':
                # bad request
                ssl_sock.close()
                self.connection.close()
                return
            # newline(\r\n)?
            first_line += data
            if '\n' in first_line:
                first_line, data = first_line.split('\n', 1)
                first_line = first_line.rstrip('\r')
                break
        # got path, rewrite
        method, path, ver = first_line.split()
        if path.startswith('/'):
            path = 'https://%s%s' % (host if port=='443' else self.path, path)
        # connect to local proxy server
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('127.0.0.1', common.LISTEN_PORT))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32*1024)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.send('%s %s %s\r\n%s' % (method, path, ver, data))

        # forward https request
        ssl_sock.settimeout(1)
        while True:
            try:
                data = ssl_sock.read(8192)
            except ssl.SSLError, e:
                if str(e).lower().find('timed out') == -1:
                    # error
                    sock.close()
                    ssl_sock.close()
                    self.connection.close()
                    return
                # timeout
                break
            if data != '':
                sock.send(data)
            else:
                # EOF
                break

        ssl_sock.setblocking(True)
        # simply forward response
        while True:
            data = sock.recv(8192)
            if data != '':
                ssl_sock.write(data)
            else:
                # EOF
                break
        # clean
        sock.close()
        ssl_sock.shutdown(socket.SHUT_WR)
        ssl_sock.close()
        self.connection.close()

    def _fetch(self, url, method, headers, payload):
        errors = []
        params = encoder.encode({'url':url, 'method':method, 'headers':headers, 'payload':payload})
        params = zlib.compress(params)
        for i in range(1, 3):
            if common.FETCH_PROXY:
                proxy_handler = urllib2.ProxyHandler(common.FETCH_PROXY)
                request = urllib2.Request(common.FETCH_SERVER, params)
            else:
                proxy_handler = urllib2.ProxyHandler({})
                request = urllib2.Request(common.FETCH_SERVER_RAW, params)
                request.add_header('Host', common.FETCH_HOST)
            request.add_header('Content-Type', 'application/octet-stream')
            try:
                continued, selected = 0, ''
                response = urllib2.build_opener(proxy_handler).open(request)
                data = response.read()
                response.close()
            except urllib2.HTTPError, e:
                # www.google.cn:80 is down, set selected to trigger common.select('https')
                if e.code == 502:
                    selected = str(e)
                errors.append('%d: %s' % (e.code, httplib.responses.get(e.code, 'Unknown HTTPError')))
                continued = 1
            except urllib2.URLError, e:
                # google ssl is down, set selected to trigger common.select('https')
                if e.reason[0] in (11004, 10051, 10054, 10060, 'timed out'):
                    selected = str(e)
                errors.append(str(e))
                continued = 1
            except Exception, e:
                errors.append(repr(e))
                continued = 1
            finally:
                # fetch server down, select another server
                if selected:
                    self.log_message('_fetch errors(%r), common.select(\'https\') again' % selected)
                    common.select('https')
                    common.show()
            # something wrong, continue to fetch again
            if continued:
                continue

            try:
                if data[0] == '0':
                    raw_data = data[1:]
                elif data[0] == '1':
                    raw_data = zlib.decompress(data[1:])
                else:
                    raise ValueError('Data format not match(%s)' % url)
                data = {}
                data['code'], hlen, clen = struct.unpack('>3I', raw_data[:12])
                if len(raw_data) != 12+hlen+clen:
                    raise ValueError('Data length not match')
                data['content'] = raw_data[12+hlen:]
                if data['code'] == 555:     #Urlfetch Failed
                    raise ValueError(data['content'])
                data['headers'] = encoder.decode(raw_data[12:12+hlen])
                return (0, data)
            except Exception, e:
                errors.append(str(e))
        return (-1, errors)

    def _RangeFetch(self, m, data):
        m = map(int, m.groups())
        start = m[0]
        end = m[2] - 1
        if 'range' in self.headers:
            req_range = re.search(r'(\d+)?-(\d+)?', self.headers['range'])
            if req_range:
                req_range = [u and int(u) for u in req_range.groups()]
                if req_range[0] is None:
                    if req_range[1] is not None:
                        if m[1]-m[0]+1==req_range[1] and m[1]+1==m[2]:
                            return False
                        if m[2] >= req_range[1]:
                            start = m[2] - req_range[1]
                else:
                    start = req_range[0]
                    if req_range[1] is not None:
                        if m[0]==req_range[0] and m[1]==req_range[1]:
                            return False
                        if end > req_range[1]:
                            end = req_range[1]
            data['headers']['content-range'] = 'bytes %d-%d/%d' % (start, end, m[2])
        elif start == 0:
            data['code'] = 200
            del data['headers']['content-range']
        data['headers']['content-length'] = end-start+1
        partSize = LocalProxyHandler.partSize
        self.send_response(data['code'])
        for k,v in data['headers'].iteritems():
            self.send_header(k.title(), v)
        self.end_headers()
        if start == m[0]:
            self.wfile.write(data['content'])
            start = m[1] + 1
            partSize = len(data['content'])
        failed = 0
        print '>>>>>>>>>>>>>>> Range Fetch started'
        while start <= end:
            self.headers['Range'] = 'bytes=%d-%d' % (start, start + partSize - 1)
            retval, data = self._fetch(self.path, self.command, self.headers, '')
            if retval != 0:
                time.sleep(4)
                continue
            m = re.search(r'bytes\s+(\d+)-(\d+)/(\d+)', data['headers'].get('content-range',''))
            if not m or int(m.group(1))!=start:
                if failed >= 1:
                    break
                failed += 1
                continue
            start = int(m.group(2)) + 1
            print '>>>>>>>>>>>>>>> %s %d' % (data['headers']['content-range'], end)
            failed = 0
            self.wfile.write(data['content'])
        print '>>>>>>>>>>>>>>> Range Fetch ended'
        self.connection.close()
        return True

    def do_METHOD(self):
        if self.path.startswith('/'):
            host = self.headers['host']
            if host.endswith(':80'):
                host = host[:-3]
            self.path = 'http://%s%s' % (host , self.path)

        payload_len = int(self.headers.get('content-length', 0))
        if payload_len > 0:
            payload = self.rfile.read(payload_len)
        else:
            payload = ''

        for k in LocalProxyHandler.FR_Headers:
            try:
                del self.headers[k]
            except KeyError:
                pass

        retval, data = self._fetch(self.path, self.command, self.headers, payload)
        try:
            if retval == -1:
                return self.end_error(502, str(data))
            if data['code']==206 and self.command=='GET':
                m = re.search(r'bytes\s+(\d+)-(\d+)/(\d+)', data['headers'].get('content-range',''))
                if m and self._RangeFetch(m, data):
                    return
            self.send_response(data['code'])
            for k,v in data['headers'].iteritems():
                self.send_header(k.title(), v)
            self.end_headers()
            self.wfile.write(data['content'])
        except socket.error, (err, _):
            # Connection closed before proxy return
            if err == errno.EPIPE or err == 10053:
                return
        self.connection.close()

    do_GET = do_METHOD
    do_HEAD = do_METHOD
    do_PUT = do_METHOD
    do_POST = do_METHOD
    do_DELETE = do_METHOD

class ThreadingHTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    pass

if __name__ == '__main__':
    common.show()
    httpd = ThreadingHTTPServer((common.LISTEN_IP, common.LISTEN_PORT), LocalProxyHandler)
    httpd.serve_forever()
