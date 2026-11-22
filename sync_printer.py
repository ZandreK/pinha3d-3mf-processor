#!/usr/bin/env python3
"""
Pinha 3D — Sincronizador de telemetria (Creality Sparkx i7)
===========================================================
Lê o status da impressora em tempo real via Klipper/Moonraker (porta 7125)
e publica a telemetria em um backend Supabase.

Configuração por variáveis de ambiente (veja .env.example):
    PRINTER_IP             IP da impressora na rede local   (ex: 192.168.0.31)
    PRINTER_PORT           porta do Moonraker               (padrão: 7125)
    SUPABASE_URL           URL do projeto Supabase
    SUPABASE_SERVICE_KEY   chave de serviço do Supabase

NUNCA coloque a chave de serviço direto no código ou em um repositório.
Mantenha-a apenas no arquivo .env local (que está no .gitignore).

Uso:
    python3 sync_printer.py          # loop contínuo (a cada 3s)
    python3 sync_printer.py --once   # uma única leitura
"""

import urllib.request
import json
import time
import sys
import re
import os


def load_dotenv(path=".env"):
    """Carrega pares CHAVE=valor de um arquivo .env, sem dependências externas."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

PRINTER_IP = os.environ.get("PRINTER_IP", "127.0.0.1")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "7125"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def require_config():
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SERVICE_KEY:
        missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        print(
            "Erro: variáveis de ambiente ausentes: " + ", ".join(missing) + ".\n"
            "Copie .env.example para .env e preencha os valores.",
            file=sys.stderr,
        )
        sys.exit(1)


def parse_time_from_filename(filename):
    # Procura padrões como "7h48m29s" ou "1h30m"
    m = re.search(r'(\d+)h(?:(\d+)m)?(?:(\d+)s)?', filename)
    if m:
        h = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mins * 60 + s
    m_min = re.search(r'(\d+)m(?:(\d+)s)?', filename)
    if m_min:
        mins = int(m_min.group(1) or 0)
        s = int(m_min.group(2) or 0)
        return mins * 60 + s
    return 0


def fetch_telemetry():
    url = f"http://{PRINTER_IP}:{PRINTER_PORT}/printer/objects/query?print_stats&extruder&heater_bed"
    req = urllib.request.Request(url, headers={"User-Agent": "Pinha3D-Sync/1.0"})
    with urllib.request.urlopen(req, timeout=3) as res:
        raw = json.loads(res.read().decode())
        status = raw.get("result", {}).get("status", {})
        print_stats = status.get("print_stats", {})
        extruder = status.get("extruder", {})
        heater_bed = status.get("heater_bed", {})

        state = (print_stats.get("state") or "standby").lower()
        duration = print_stats.get("print_duration", 0)
        filename = print_stats.get("filename", "")

        est_total = parse_time_from_filename(filename)
        progress = 0
        if est_total > 0 and duration > 0:
            progress = min(100, round((duration / est_total) * 100))
        elif state == "printing":
            progress = 50

        status_label = "Imprimindo" if state == "printing" else ("Ociosa / Em Espera" if state == "standby" else state.capitalize())

        return {
            "isConnected": True,
            "status": state,
            "statusLabel": status_label,
            "progressPercent": progress,
            "currentFile": filename,
            "nozzleTemp": round(extruder.get("temperature", 0), 1),
            "nozzleTarget": round(extruder.get("target", 0), 1),
            "bedTemp": round(heater_bed.get("temperature", 0), 1),
            "bedTarget": round(heater_bed.get("target", 0), 1),
            "printDurationSeconds": round(duration),
            "zPos": round(print_stats.get("z_pos", 0), 2),
            "filamentUsedMm": round(print_stats.get("filament_used", 0), 1),
            "lastUpdate": time.strftime("%H:%M:%S")
        }


def push_to_supabase(telemetry):
    url = f"{SUPABASE_URL}/rest/v1/system_settings"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    payload = {
        "key": "printer_telemetry",
        "value": telemetry,
        "description": "Telemetria em tempo real da Creality Sparkx i7",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=5)


def main():
    print(f"📡 Iniciando sincronização da Creality Sparkx i7 ({PRINTER_IP}:{PRINTER_PORT})...")
    while True:
        try:
            telemetry = fetch_telemetry()
            push_to_supabase(telemetry)
            print(f"[{time.strftime('%H:%M:%S')}] {telemetry['statusLabel']} | Bico: {telemetry['nozzleTemp']}°C | Mesa: {telemetry['bedTemp']}°C | {telemetry['progressPercent']}% | {telemetry['currentFile']}")
        except Exception as e:
            offline_telem = {
                "isConnected": False,
                "status": "offline",
                "statusLabel": "Offline / Sem Resposta",
                "progressPercent": 0,
                "nozzleTemp": 0,
                "nozzleTarget": 0,
                "bedTemp": 0,
                "bedTarget": 0,
                "lastUpdate": time.strftime("%H:%M:%S"),
                "errorMessage": str(e)
            }
            try:
                push_to_supabase(offline_telem)
            except Exception:
                pass
            print(f"[{time.strftime('%H:%M:%S')}] Erro: {e}")

        time.sleep(3)


if __name__ == "__main__":
    require_config()
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        telemetry = fetch_telemetry()
        push_to_supabase(telemetry)
        print("Sincronização realizada com sucesso!")
        print(json.dumps(telemetry, indent=2))
    else:
        main()
