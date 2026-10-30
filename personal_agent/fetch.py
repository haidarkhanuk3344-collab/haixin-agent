"""Bounded public-web GET: validated DNS answers are pinned to the actual socket."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .app import AppError

MAX_BYTES = 1048576
TOTAL_SECONDS = 20
REDIRECTS = 3


def public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return public_ip(str(mapped))
        return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return False


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\x7f]", value):
        raise AppError("URL_INVALID", "URL含空白/控制字符或超过4096字符。")
    try:
        p = urlsplit(value)
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError()
        host = p.hostname.rstrip('.').encode("idna").decode("ascii").lower()
        if not host or '%' in host or len(host) > 253:
            raise ValueError()
        port = p.port or (443 if p.scheme.lower() == "https" else 80)
        if port != (443 if p.scheme.lower() == "https" else 80):
            raise AppError("URL_PORT", "仅支持HTTP 80和HTTPS 443端口。")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise AppError("URL_PRIVATE", "不读取本机、局域网或内部服务地址。")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError()
        else:
            if not public_ip(host):
                raise AppError("URL_PRIVATE", "不读取本机、局域网、保留网段或元数据地址。")
        netloc = '[' + host + ']' if ':' in host else host
        path = quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(p.query, safe="/%?:@!$&'()*+,;=-._~")
        return urlunsplit((p.scheme.lower(), netloc, path, query, ''))
    except (ValueError, UnicodeError):
        raise AppError("URL_INVALID", "仅支持无账号密码的有效HTTP(S)网址。")


def resolve_public(host, timeout=4):
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        # A subprocess bounds DNS resolution even if the system resolver hangs.
        code = "import socket,json,sys;print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo(sys.argv[1],None,type=socket.SOCK_STREAM)})))"
        try:
            p = subprocess.run([sys.executable, "-c", code, host], capture_output=True, text=True, timeout=timeout)
            if p.returncode:
                raise AppError("URL_DNS", "网址域名解析失败。")
            addresses = json.loads(p.stdout)
        except subprocess.TimeoutExpired:
            raise AppError("URL_TIMEOUT", "域名解析超时。")
        except (OSError, ValueError):
            raise AppError("URL_DNS", "网址域名解析失败。")
    if not isinstance(addresses, list) or not addresses or len(addresses) > 32 or not all(isinstance(a, str) and public_ip(a) for a in addresses):
        raise AppError("URL_PRIVATE", "域名解析包含非公开地址，已拒绝连接。")
    return addresses


def _abort(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def request_once(url, address, deadline):
    p = urlsplit(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AppError("URL_TIMEOUT", "网页读取超过总时限。")
    port = 443 if p.scheme == "https" else 80
    connection = http.client.HTTPConnection(p.hostname, port, timeout=min(4, remaining))
    sock, timer = None, None
    try:
        # Only this validated numeric address is dialled. No proxy or second hostname lookup.
        sock = socket.create_connection((address, port), timeout=min(4, remaining))
        if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(address):
            raise AppError("URL_PEER", "实际连接地址与已验证地址不一致。")
        if p.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname, do_handshake_on_connect=False)
        connection.sock = sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppError("URL_TIMEOUT", "网页读取超过总时限。")
        # Enforces wall time even for slow trickles of header or body bytes.
        timer = threading.Timer(remaining, _abort, args=(sock,)); timer.daemon = True; timer.start()
        sock.settimeout(min(4, remaining))
        if p.scheme == "https":
            sock.do_handshake()
        path = urlunsplit(('', '', p.path or '/', p.query, ''))
        connection.request("GET", path, headers={"User-Agent": "PersonalAgent/0.1 (explicit user URL)",
                                               "Accept": "text/html,text/plain,text/markdown,application/xhtml+xml",
                                               "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if response.status in (301, 302, 303, 307, 308):
            return response.status, headers, b''
        if response.status != 200:
            raise AppError("HTTP_" + str(response.status), "网页返回HTTP {}，未保存为成功材料。".format(response.status))
        if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            raise AppError("URL_ENCODING", "暂不读取压缩响应，未自动解压。")
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                raise AppError("URL_LENGTH", "网页响应长度无效。")
            if length < 0 or length > MAX_BYTES:
                raise AppError("SOURCE_SIZE", "网页原文超过1MiB或响应长度无效。")
        chunks, count = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppError("URL_TIMEOUT", "网页读取超过总时限。")
            sock.settimeout(min(4, remaining))
            chunk = response.read1(min(16384, MAX_BYTES + 1 - count))
            if not chunk:
                break
            chunks.append(chunk); count += len(chunk)
            if count > MAX_BYTES:
                raise AppError("SOURCE_SIZE", "网页原文超过1MiB。")
        body = b''.join(chunks)
        if time.monotonic() >= deadline:
            raise AppError("URL_TIMEOUT", "网页读取超过总时限，未保存不完整响应。")
        if "content-length" in headers and len(body) != int(headers["content-length"]):
            raise AppError("URL_TRUNCATED", "网页响应不完整，未保存为成功材料。")
        return response.status, headers, body
    except ssl.SSLCertVerificationError:
        raise AppError("URL_TLS", "网页证书校验失败，未绕过验证。")
    except (socket.timeout, TimeoutError):
        raise AppError("URL_TIMEOUT", "网页连接或读取超时。")
    except (OSError, http.client.HTTPException, ValueError):
        code = "URL_TIMEOUT" if time.monotonic() >= deadline else "URL_NETWORK"
        raise AppError(code, "网页网络读取失败或响应不完整。")
    finally:
        if timer: timer.cancel()
        connection.close()
        if sock: sock.close()


def fetch_url(value, cancel_check=lambda: None):
    current = canonical_url(value)
    deadline = time.monotonic() + TOTAL_SECONDS
    hops = []
    for step in range(REDIRECTS + 1):
        if cancel_check():
            raise AppError("SOURCE_CANCELLED", "材料读取收到停止或取消请求。")
        p = urlsplit(current)
        addresses = resolve_public(p.hostname, timeout=max(.01, min(4, deadline - time.monotonic())))
        # Recheck even injected resolver results; reject mixed public/private DNS responses.
        if not addresses or not all(public_ip(a) for a in addresses):
            raise AppError("URL_PRIVATE", "域名解析包含非公开地址，已拒绝连接。")
        status, headers, body = request_once(current, addresses[0], deadline)
        hops.append({"url": current, "status": status, "address": addresses[0]})
        if status == 200:
            return {"url": current, "headers": headers, "raw": body, "hops": hops}
        if step == REDIRECTS or not headers.get("location"):
            raise AppError("URL_REDIRECT", "网页重定向超过3次或缺少目标地址。")
        target = canonical_url(urljoin(current, headers["location"]))
        if p.scheme == "https" and urlsplit(target).scheme != "https":
            raise AppError("URL_DOWNGRADE", "拒绝将HTTPS页面重定向到HTTP。")
        current = target
    raise AppError("URL_REDIRECT", "网页重定向失败。")
