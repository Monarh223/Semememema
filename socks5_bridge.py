"""
Локальный HTTP-прокси: принимает соединения и пробрасывает их через SOCKS5 с авторизацией.
Playwright не умеет SOCKS5+auth, поэтому поднимаем этот мост и отдаём Playwright http://127.0.0.1:port.
"""
import socket
import threading

import socks


def _run_bridge(listen_port: int, socks_server: str, socks_username: str, socks_password: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", listen_port))
    server.listen(32)
    server.settimeout(1.0)
    while getattr(_run_bridge, "running", True):
        try:
            client, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(
            target=_handle_connect,
            args=(client, socks_server, socks_username, socks_password),
            daemon=True,
        )
        t.start()
    try:
        server.close()
    except OSError:
        pass


def _handle_connect(
    client: socket.socket,
    socks_server: str,
    socks_username: str,
    socks_password: str,
) -> None:
    remote = None
    try:
        client.settimeout(30)
        first = b""
        while b"\r\n" not in first and len(first) < 8192:
            chunk = client.recv(4096)
            if not chunk:
                return
            first += chunk
        line = first.split(b"\r\n", 1)[0].decode("utf-8", errors="ignore")
        if not line.upper().startswith("CONNECT "):
            client.send(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        path = line.split(None, 2)[1]
        if ":" in path:
            host, port_str = path.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = path, 443
        if "://" in socks_server:
            socks_server = socks_server.split("://", 1)[1]
        if ":" in socks_server:
            shost, sport = socks_server.rsplit(":", 1)
            socks_port = int(sport)
        else:
            shost, socks_port = socks_server, 1080
        remote = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        remote.set_proxy(
            socks.SOCKS5,
            shost,
            socks_port,
            username=socks_username or None,
            password=socks_password or None,
        )
        remote.settimeout(30)
        remote.connect((host, port))
        client.send(b"HTTP/1.1 200 Connection established\r\n\r\n")
        client.setblocking(False)
        remote.setblocking(False)
        _pipe(client, remote)
    except Exception:
        try:
            client.send(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
    finally:
        try:
            client.close()
        except OSError:
            pass
        if remote:
            try:
                remote.close()
            except OSError:
                pass


def _pipe(a: socket.socket, b: socket.socket) -> None:
    import select
    while True:
        r, _, _ = select.select([a, b], [], [], 60)
        if not r:
            return
        for s in r:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            other = b if s is a else a
            try:
                other.sendall(data)
            except OSError:
                return


def start_socks5_bridge(proxy_dict: dict) -> tuple[int, threading.Thread]:
    """
    Запускает локальный HTTP-прокси на 127.0.0.1, пробрасывающий трафик в SOCKS5 (proxy_dict).
    Возвращает (port, thread). Остановка: stop_socks5_bridge(thread).
    """
    server = (proxy_dict.get("server") or "").strip()
    if "socks5" not in server.lower():
        raise ValueError("Ожидается SOCKS5 прокси")
    username = proxy_dict.get("username") or ""
    password = proxy_dict.get("password") or ""
    if "://" in server:
        server = server.split("://", 1)[1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    _run_bridge.running = True
    thread = threading.Thread(
        target=_run_bridge,
        args=(port, server, username, password),
        daemon=True,
    )
    thread.start()
    return port, thread


def stop_socks5_bridge(thread: threading.Thread) -> None:
    _run_bridge.running = False
    thread.join(timeout=2)
