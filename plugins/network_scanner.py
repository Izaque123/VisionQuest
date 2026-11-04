"""
network_scanner.py — Mapeia os dispositivos conectados na rede local.

COMO FUNCIONA
    1. Descobre o IP local e a máscara de sub-rede.
    2. Faz um "ping sweep" paralelo (ICMP) em todos os IPs da rede (limitado
       a 254 hosts para não travar nem sobrecarregar a rede).
    3. Lê a tabela ARP do sistema operacional para obter o MAC address dos
       dispositivos que responderam.
    4. Tenta resolver o hostname (nome do dispositivo) de cada IP.
    5. Retorna tudo formatado como JSON.

POR QUE NÃO USAR NMAP OU SCAPY
    O Nmap é excelente, mas exige instalação de um binário externo (que o
    antivírus do Windows adora bloquear). O Scapy precisa de privilégios
    de root/administrador para enviar pacotes ARP crus. A abordagem de
    ping + tabela ARP do SO funciona nativamente em qualquer plataforma,
    sem privilégios especiais e sem dependências extras além do que o
    JARVIS já usa.

SEGURANÇA
    O script não envia pacotes maliciosos nem faz port scanning. Apenas
    usa o comando `ping` nativo do SO com timeout curtíssimo (0.3s).
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import platform
import re
import socket
import subprocess
from core.outcome import done, failed
from core.tool_cancel import cancelled

_OS = platform.system()

PLUGIN = {
    "name": "network_scanner",
    "description": (
        "Escaneia a rede local (Wi-Fi ou cabo) e retorna a lista de "
        "dispositivos conectados, incluindo IP, MAC address e hostname. "
        "Use quando o usuário perguntar 'quais dispositivos estão na minha "
        "rede?', 'quem está conectado no Wi-Fi?', 'qual o IP da impressora?', "
        "'lista os IPs da rede'. Chame uma vez por pedido. "
        "NÃO use para escanear a internet ou testar "
        "velocidade de conexão."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "subnet": {
                "type": "STRING",
                "description": (
                    "Opcional. Forçar o escaneamento de uma sub-rede específica "
                    "(ex: '192.168.1.0/24'). Se omitido, descobre automaticamente."
                ),
            }
        },
        "required": [],
    },
    "behavior": "BLOCKING",
    "tier": "extra",
    "brief": "Lista IPs e dispositivos da rede local.",
}


# Host-only and container adapters. They are private, and they are not the
# Wi-Fi the phone and the printer sit on.
_VIRTUAL_NETS = (
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.56.0/24"),
    ipaddress.IPv4Network("192.168.137.0/24"),
)


def _is_virtual(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _VIRTUAL_NETS)


def _is_lan(ip: str) -> bool:
    """True for a private address of this computer, never a public one."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return bool(addr.is_private and not addr.is_loopback and not addr.is_link_local)


def _candidate_ips() -> list[str]:
    """Every IPv4 this computer owns, private ones first."""
    found: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in found:
            found.append(ip)

    for probe in ("192.168.1.1", "192.168.0.1", "10.0.0.1", "8.8.8.8"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.3)
            sock.connect((probe, 80))
            add(sock.getsockname()[0])
        except Exception:
            pass
        finally:
            if sock is not None:
                sock.close()
    if _OS == "Windows":
        try:
            shown = subprocess.run(
                ["ipconfig"], capture_output=True, text=True,
                timeout=3, errors="replace",
            )
            for ip in re.findall(r"IPv4.*?:\s*(\d+\.\d+\.\d+\.\d+)", shown.stdout):
                add(ip)
        except Exception:
            pass
    found.sort(key=lambda ip: (not _is_lan(ip), _is_virtual(ip), ip))
    return found


def _get_local_ip_and_subnet() -> tuple[str, str]:
    """The private LAN of this computer, as a /24. A public address is refused."""
    for ip in _candidate_ips():
        if not _is_lan(ip):
            continue
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return ip, str(network)
    print("[network_scanner] nenhum endereço privado encontrado")
    return "", ""


def _ping_host(ip: str) -> bool:
    """Faz um ping rápido e silencioso em um IP. Retorna True se respondeu."""
    try:
        if _OS == "Windows":
            # -n 1 (1 ping), -w 300 (timeout 300ms)
            cmd = ["ping", "-n", "1", "-w", "300", ip]
        else:
            # -c 1 (1 ping), -W 1 (timeout 1s)
            cmd = ["ping", "-c", "1", "-W", "1", ip]

        # Oculta a janela preta no Windows
        startupinfo = None
        if _OS == "Windows":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            startupinfo=startupinfo,
        )
        return res.returncode == 0
    except Exception:
        return False


def _get_arp_table() -> dict[str, str]:
    """Lê a tabela ARP do SO e retorna um dicionário {IP: MAC}."""
    arp_dict = {}
    try:
        if _OS == "Windows":
            cmd = ["arp", "-a"]
        elif _OS == "Darwin":  # macOS
            cmd = ["arp", "-an"]
        else:  # Linux
            cmd = ["ip", "neigh"]
            import shutil

            if not shutil.which("ip"):
                cmd = ["arp", "-an"]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)

        mac_regex = re.compile(
            r"([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})"
        )
        ip_regex = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")

        for line in res.stdout.splitlines():
            mac_match = mac_regex.search(line)
            ip_match = ip_regex.search(line)

            if mac_match and ip_match:
                ip = ip_match.group(1)
                mac = mac_match.group(1).upper()
                # Ignora MACs de broadcast/multicast para não poluir a lista
                if mac.startswith("FF:FF:FF") or mac.startswith("01:00:5E"):
                    continue
                arp_dict[ip] = mac
    except Exception as e:
        print(f"[network_scanner] Falha ao ler tabela ARP: {e}")
    return arp_dict


def _resolve_hostname(ip: str) -> str:
    """Reverse DNS for one address, without changing the process timeout.

    The voice socket uses the process default. A global 0.5s timeout during
    this lookup is long enough to stall that socket while the scan runs.
    """
    if cancelled():
        return "Desconhecido"

    def lookup() -> str:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname.split(".")[0]

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lookup).result(timeout=0.5)
    except Exception:
        return "Desconhecido"


def _stopped() -> str:
    return failed(
        "network_scanner",
        "The user pressed interrupt and the scan stopped. "
        "Do not scan again unless the user asks for a new scan.",
    )


def run(parameters: dict, player=None, session_memory=None) -> str:
    if cancelled():
        return _stopped()

    subnet_arg = str(parameters.get("subnet", "")).strip()

    local_ip, auto_subnet = _get_local_ip_and_subnet()
    if not local_ip or not auto_subnet:
        return failed(
            "network_scanner",
            "Não encontrei uma rede privada neste computador. "
            "A saída da internet apontou para um endereço público, e essa faixa não é varrida.",
        )
    target_subnet = subnet_arg or auto_subnet

    try:
        network = ipaddress.IPv4Network(target_subnet, strict=False)
    except ValueError:
        return failed("network_scanner", f"Sub-rede inválida: {target_subnet}")
    if not network.is_private or network.num_addresses > 256:
        network = ipaddress.IPv4Network(auto_subnet, strict=False)

    if player:
        try:
            player.write_log(f"JARVIS: Escaneando rede {network}…")
        except Exception:
            pass

    hosts = list(network.hosts())
    # Remove o próprio IP da lista para não confundir
    hosts = [str(ip) for ip in hosts if str(ip) != local_ip]

    active_ips = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=50)
    futures = {pool.submit(_ping_host, ip): ip for ip in hosts}
    pending = set(futures)
    stopped = False
    try:
        while pending:
            if cancelled():
                stopped = True
                break
            done_now, pending = concurrent.futures.wait(
                pending, timeout=0.2,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done_now:
                if future.result():
                    active_ips.append(futures[future])
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    if stopped or cancelled():
        return _stopped()

    # 2. Pega a tabela ARP para encontrar os MAC addresses
    arp_table = _get_arp_table()

    # 3. Monta o JSON final
    devices = []

    # Adiciona o próprio host na lista
    my_mac = arp_table.get(local_ip, "Próprio Host")
    devices.append(
        {
            "ip": local_ip,
            "mac": my_mac,
            "hostname": "JARVIS (Este PC)",
            "status": "Você",
        }
    )

    for ip in active_ips:
        if cancelled():
            return _stopped()
        mac = arp_table.get(ip, "Desconhecido")
        hostname = _resolve_hostname(ip)
        devices.append({"ip": ip, "mac": mac, "hostname": hostname})

    # Ordena a lista pelo último octeto do IP para ficar organizado
    devices.sort(key=lambda x: int(x["ip"].split(".")[-1]))

    # 4. Prepara o retorno
    payload = {"network": str(network), "total_found": len(devices), "devices": devices}

    json_str = json.dumps(payload, indent=2, ensure_ascii=False)

    if player:
        try:
            player.write_log(f"JARVIS: Encontrei {len(devices)} dispositivos ativos.")
        except Exception:
            pass

    # O JARVIS lê o JSON diretamente no resultado da ferramenta
    return done("network_scanner", data=json_str)
