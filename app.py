"""
Servidor ADMS ZKTeco — Docentes (versión servidor / Render)

- Configuración por variables de entorno (Render → Environment).
- En local lee un archivo .env si existe (python-dotenv).
- Se ejecuta con gunicorn:  gunicorn app:app --workers 1 --threads 4
"""

from flask import Flask, request, Response, render_template_string, abort
from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3
import requests
import threading
import time
import os

# ══════════════════════════════════════════════════════
#  CONFIGURACIÓN (variables de entorno)
# ══════════════════════════════════════════════════════

try:
    from dotenv import load_dotenv
    load_dotenv()                      # solo tiene efecto en local
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

API_URL         = os.getenv("API_URL", "").strip().rstrip("/")
PORT            = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8080")))   # Render inyecta PORT
MODO_HISTORICO  = os.getenv("MODO_HISTORICO", "false").strip().lower() == "true"
DATA_DIR        = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))      # en Render: /var/data (disco)
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()                   # vacío = dashboard público
TZ              = ZoneInfo(os.getenv("TZ_NAME", "America/Lima"))
REENVIO_SEG     = int(os.getenv("REENVIO_SEG", "60"))

if not API_URL:
    raise RuntimeError("Falta la variable de entorno API_URL")

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.path.join(DATA_DIR, "asistencias-docente.db")
LOG_PATH = os.path.join(DATA_DIR, "errores-docente.log")

# ══════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════

log_lock = threading.Lock()

def ts():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def _escribir(linea):
    print(linea, flush=True)           # Render muestra stdout en "Logs"
    with log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(linea + "\n")

def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)

def log_error(msg):
    _escribir(f"[{ts()}] ERROR: {msg}")

def log_info(msg):
    _escribir(f"[{ts()}] INFO:  {msg}")

def iniciar_sesion_log():
    sep  = "=" * 60
    modo = "HISTÓRICO (Stamp=0)" if MODO_HISTORICO else "TIEMPO REAL (Stamp=9999)"
    for linea in (
        sep,
        f"  SESIÓN INICIADA : {ts()}",
        f"  API             : {API_URL}",
        f"  Puerto          : {PORT}",
        f"  BD              : {DB_PATH}",
        f"  Modo            : {modo}",
        sep,
    ):
        _escribir(linea)

# ══════════════════════════════════════════════════════
#  BASE DE DATOS
#  enviado: 0=pendiente  1=enviado OK  2=error
# ══════════════════════════════════════════════════════

def conectar():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")   # lecturas y escrituras concurrentes (threads)
    return con

def init_db():
    con = conectar()
    con.execute("""
        CREATE TABLE IF NOT EXISTS asistencias (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            pin        TEXT NOT NULL,
            fecha_hora TEXT NOT NULL,
            status     TEXT,
            verify     TEXT,
            sn_reloj   TEXT,
            enviado    INTEGER DEFAULT 0,
            respuesta  TEXT,
            creado_en  TEXT,
            UNIQUE(pin, fecha_hora)
        )
    """)
    con.commit()
    con.close()

def guardar(pin, fecha_hora, status, verify, sn):
    try:
        con = conectar()
        cur = con.execute("""
            INSERT OR IGNORE INTO asistencias
            (pin, fecha_hora, status, verify, sn_reloj, enviado, respuesta, creado_en)
            VALUES (?, ?, ?, ?, ?, 0, '', ?)
        """, (pin, fecha_hora, status, verify, sn, ts()))
        insertado = cur.rowcount > 0
        con.commit()
        con.close()
        return insertado
    except Exception as e:
        log_error(f"BD guardar PIN={pin} {fecha_hora} → {e}")
        return False

def marcar_enviado(row_id, respuesta, estado):
    con = conectar()
    con.execute("UPDATE asistencias SET enviado=?, respuesta=? WHERE id=?", (estado, respuesta, row_id))
    con.commit()
    con.close()

def marcar_por_clave(pin, fecha_hora, respuesta, estado):
    con = conectar()
    con.execute("UPDATE asistencias SET enviado=?, respuesta=? WHERE pin=? AND fecha_hora=?",
                (estado, respuesta, pin, fecha_hora))
    con.commit()
    con.close()

def get_pendientes():
    con = conectar()
    rows = con.execute(
        "SELECT id, pin, fecha_hora, status, verify, sn_reloj FROM asistencias WHERE enviado=0"
    ).fetchall()
    con.close()
    return rows

def get_stats():
    con = conectar()
    total, enviados, pendientes, errores = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN enviado=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN enviado=0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN enviado=2 THEN 1 ELSE 0 END)
        FROM asistencias
    """).fetchone()
    con.close()
    return {"total": total or 0, "enviados": enviados or 0,
            "pendientes": pendientes or 0, "errores": errores or 0}

def get_ultimos_registros(limite=50):
    con = conectar()
    rows = con.execute("""
        SELECT pin, fecha_hora, sn_reloj, enviado, respuesta, creado_en
        FROM asistencias ORDER BY id DESC LIMIT ?
    """, (limite,)).fetchall()
    con.close()
    return rows

# ══════════════════════════════════════════════════════
#  ESTADO EN MEMORIA (relojes vistos en esta sesión)
# ══════════════════════════════════════════════════════

relojes_sesion = {}
estado_lock    = threading.Lock()

def registrar_reloj(sn, ip):
    with estado_lock:
        relojes_sesion[sn] = {"ip": ip, "ultima_vez": ts()}

def ip_cliente():
    # Render está detrás de un proxy: la IP real viene en X-Forwarded-For
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else request.remote_addr

# ══════════════════════════════════════════════════════
#  ENVÍO A LA API
# ══════════════════════════════════════════════════════

http = requests.Session()

def enviar_api(pin, fecha_hora, status, verify, sn):
    url = f"{API_URL}/{pin}"
    try:
        dt = datetime.strptime(fecha_hora, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.now(TZ)

    payload = {
        "pin"       : pin,
        "fecha"     : dt.strftime("%Y-%m-%d"),
        "hora"      : dt.strftime("%H:%M:%S"),
        "fecha_hora": fecha_hora,
        "status"    : status,
        "verify"    : verify,
        "sn_reloj"  : sn,
    }
    try:
        resp  = http.post(url, json=payload, timeout=10)
        texto = resp.text[:300]
        ok    = resp.status_code < 400
        log(f"  → API [{resp.status_code}] PIN={pin} {fecha_hora}")
        if not ok:
            log_error(f"API {resp.status_code} PIN={pin} {fecha_hora} → {texto}")
        return {"ok": ok, "texto": texto, "codigo": resp.status_code}
    except Exception as e:
        log_error(f"API error PIN={pin} {fecha_hora} → {e}")
        return {"ok": False, "texto": str(e), "codigo": 0}   # 0 = sin respuesta (red/timeout)

def procesar_fichaje(pin, fecha_hora, status, verify, sn):
    log(f"  FICHAJE  PIN={pin}  Hora={fecha_hora}")
    if not guardar(pin, fecha_hora, status, verify, sn):
        log(f"  ⚠ Duplicado ignorado PIN={pin} {fecha_hora}")
        return

    r = enviar_api(pin, fecha_hora, status, verify, sn)
    if r["ok"]:
        marcar_por_clave(pin, fecha_hora, r["texto"], 1)
        log_info(f"Enviado OK PIN={pin} {fecha_hora}")
    else:
        # Se deja en 0 (pendiente) para que el hilo de reintentos lo vuelva a intentar
        marcar_por_clave(pin, fecha_hora, r["texto"], 0)
        log_error(f"Falló envío PIN={pin} {fecha_hora} → queda pendiente")

# ══════════════════════════════════════════════════════
#  HILO DE REINTENTOS
# ══════════════════════════════════════════════════════

MAX_INTENTOS_LOTE = 200   # evita bloquear el hilo con colas enormes

def reenviar_pendientes():
    while True:
        time.sleep(REENVIO_SEG)
        try:
            rows = get_pendientes()[:MAX_INTENTOS_LOTE]
            if not rows:
                continue
            log_info(f"Reenvío automático: {len(rows)} pendientes")
            for row_id, pin, fecha_hora, status, verify, sn in rows:
                r = enviar_api(pin, fecha_hora, status, verify, sn)
                if r["ok"]:
                    marcar_enviado(row_id, r["texto"], 1)
                    log_info(f"Reenvío OK PIN={pin} {fecha_hora}")
                elif 400 <= r["codigo"] < 500:
                    marcar_enviado(row_id, r["texto"], 2)   # error definitivo (dato rechazado por la API)
                    log_error(f"Rechazado por API PIN={pin} {fecha_hora} → {r['texto']}")
                else:
                    marcar_enviado(row_id, r["texto"], 0)   # se reintenta en el siguiente ciclo
        except Exception as e:
            log_error(f"Hilo de reenvío: {e}")

# ══════════════════════════════════════════════════════
#  ARRANQUE (se ejecuta al importar → funciona con gunicorn)
# ══════════════════════════════════════════════════════

_arrancado = False
_arranque_lock = threading.Lock()

def arrancar():
    global _arrancado
    with _arranque_lock:
        if _arrancado:
            return
        init_db()
        iniciar_sesion_log()
        threading.Thread(target=reenviar_pendientes, daemon=True).start()
        _arrancado = True

arrancar()

# ══════════════════════════════════════════════════════
#  DASHBOARD HTML
# ══════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>ADMS ZKTeco — Docentes</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  header { background: linear-gradient(135deg, #064e3b, #065f46); padding: 18px 30px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 2px 10px rgba(0,0,0,0.4); flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 1.3rem; font-weight: 700; }
  header h1 span { color: #34d399; }
  .refresh-info { font-size: 0.75rem; color: #94a3b8; }
  .container { max-width: 1300px; margin: 0 auto; padding: 24px 20px; }
  .modo-banner { background: #451a03; border: 1px solid #92400e; border-radius: 10px; padding: 12px 18px; margin-bottom: 20px; font-size: 0.82rem; color: #fbbf24; }
  .modo-banner.rt { background: #052e16; border-color: #166534; color: #34d399; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: #1e293b; border-radius: 12px; padding: 20px; border-left: 4px solid; text-align: center; }
  .stat-card.total { border-color: #34d399; } .stat-card.ok { border-color: #86efac; }
  .stat-card.pend  { border-color: #fbbf24; } .stat-card.error { border-color: #f87171; }
  .stat-num { font-size: 2.2rem; font-weight: 800; line-height: 1; }
  .stat-card.total .stat-num { color: #34d399; } .stat-card.ok .stat-num { color: #86efac; }
  .stat-card.pend  .stat-num { color: #fbbf24; } .stat-card.error .stat-num { color: #f87171; }
  .stat-label { font-size: 0.78rem; color: #94a3b8; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  .section-title { font-size: 0.85rem; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
  .relojes { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 24px; }
  .reloj-badge { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 10px 16px; font-size: 0.82rem; }
  .reloj-badge .sn { font-weight: 700; color: #34d399; }
  .reloj-badge .ip { color: #94a3b8; margin-left: 8px; }
  .reloj-badge .hora { color: #64748b; font-size: 0.75rem; display: block; margin-top: 3px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #34d399; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .card { background: #1e293b; border-radius: 12px; overflow: hidden; }
  .card-header { padding: 14px 18px; background: #0f172a; display: flex; justify-content: space-between; align-items: center; }
  .card-header span { font-size: 0.82rem; color: #64748b; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { background: #0f172a; color: #64748b; font-weight: 600; padding: 10px 14px; text-align: left; font-size: 0.72rem; text-transform: uppercase; }
  td { padding: 9px 14px; border-bottom: 1px solid #1e293b; color: #cbd5e1; }
  tr:hover td { background: #263448; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; }
  .badge-ok { background: #064e3b; color: #34d399; } .badge-pend { background: #451a03; color: #fbbf24; } .badge-err { background: #450a0a; color: #f87171; }
  .log-box { background: #0f172a; border-radius: 8px; padding: 14px; font-family: 'Courier New', monospace; font-size: 0.73rem; max-height: 420px; overflow-y: auto; line-height: 1.6; }
  .log-error { color: #f87171; } .log-info { color: #34d399; } .log-other { color: #94a3b8; }
  .api-url { font-size: 0.75rem; color: #a7f3d0; opacity: .6; margin-top: 4px; word-break: break-all; }
  @media(max-width:900px){ .stats{grid-template-columns:repeat(2,1fr);} .grid2{grid-template-columns:1fr;} }
</style>
</head>
<body>
<header>
  <div>
    <h1>ADMS ZKTeco — <span>Docentes</span></h1>
    <div class="api-url">{{ api_url }}</div>
  </div>
  <div class="refresh-info">Auto-refresh cada 10s &nbsp;|&nbsp; {{ ahora }}</div>
</header>
<div class="container">
  {% if modo_historico %}
  <div class="modo-banner">⚠️ <strong>MODO HISTÓRICO ACTIVO</strong> — El reloj enviará todo su historial (Stamp=0). Cuando termine, cambia <code>MODO_HISTORICO=false</code> en Render → Environment.</div>
  {% else %}
  <div class="modo-banner rt">✓ <strong>MODO TIEMPO REAL</strong> — Solo se reciben fichajes nuevos (Stamp=9999).</div>
  {% endif %}

  <div class="stats">
    <div class="stat-card total"><div class="stat-num">{{ stats.total }}</div><div class="stat-label">Total fichajes</div></div>
    <div class="stat-card ok"><div class="stat-num">{{ stats.enviados }}</div><div class="stat-label">Enviados OK</div></div>
    <div class="stat-card pend"><div class="stat-num">{{ stats.pendientes }}</div><div class="stat-label">Pendientes</div></div>
    <div class="stat-card error"><div class="stat-num">{{ stats.errores }}</div><div class="stat-label">Con error</div></div>
  </div>

  <div class="section-title">Relojes conectados desde el último reinicio</div>
  <div class="relojes">
    {% for sn, info in relojes.items() %}
      <div class="reloj-badge"><span class="dot"></span><span class="sn">{{ sn }}</span><span class="ip">{{ info.ip }}</span><span class="hora">Última vez: {{ info.ultima_vez }}</span></div>
    {% else %}
      <div style="color:#475569;font-size:0.82rem;">Ningún reloj conectado aún.</div>
    {% endfor %}
  </div>

  <div class="grid2">
    <div class="card">
      <div class="card-header"><div class="section-title" style="margin:0;">Últimos fichajes</div><span>últimos 50</span></div>
      <div style="overflow-x:auto;max-height:420px;overflow-y:auto;">
        <table>
          <thead><tr><th>PIN</th><th>Fecha/Hora</th><th>Reloj</th><th>Estado</th></tr></thead>
          <tbody>
            {% for pin, fh, sn, env, resp, creado in registros %}
            <tr>
              <td><strong>{{ pin }}</strong></td><td>{{ fh }}</td><td style="color:#64748b;">{{ sn or '—' }}</td>
              <td>{% if env == 1 %}<span class="badge badge-ok">Enviado</span>{% elif env == 2 %}<span class="badge badge-err" title="{{ resp }}">Error</span>{% else %}<span class="badge badge-pend">Pendiente</span>{% endif %}</td>
            </tr>
            {% else %}
            <tr><td colspan="4" style="color:#475569;text-align:center;padding:20px;">Sin registros aún.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><div class="section-title" style="margin:0;">Log de actividad</div><span>últimas 80 líneas</span></div>
      <div style="padding:14px;">
        <div class="log-box" id="logbox">
          {% for linea in log_lines %}
            {% if 'ERROR' in linea %}<div class="log-error">{{ linea }}</div>{% elif 'INFO' in linea %}<div class="log-info">{{ linea }}</div>{% else %}<div class="log-other">{{ linea }}</div>{% endif %}
          {% else %}<div class="log-other">Sin entradas en el log aún.</div>{% endfor %}
        </div>
      </div>
    </div>
  </div>
</div>
<script>const lb=document.getElementById('logbox'); if(lb) lb.scrollTop=lb.scrollHeight;</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════
#  FLASK
# ══════════════════════════════════════════════════════

app = Flask(__name__)

def proteger():
    """Si DASHBOARD_TOKEN está definido, exige ?token=... en las rutas de consulta."""
    if DASHBOARD_TOKEN and request.args.get("token") != DASHBOARD_TOKEN:
        abort(401)

TXT = {"Content-Type": "text/plain; charset=utf-8"}

# ---------- Rutas que usa el reloj (protocolo ADMS / PUSH) ----------

@app.route("/iclock/cdata", methods=["GET", "POST"])
def cdata():
    sn = request.args.get("SN", "?")

    if request.method == "GET":
        registrar_reloj(sn, ip_cliente())
        stamp = "0" if MODO_HISTORICO else "9999"
        log_info(f"Reloj conectado SN={sn} IP={ip_cliente()} Stamp={stamp}")
        return Response(
            f"GET OPTION FROM: {sn}\nStamp={stamp}\nDelay=10\nRealtime=1\nTransFlag=TransData AttLog\n",
            mimetype="text/plain"
        )

    registrar_reloj(sn, ip_cliente())
    if request.args.get("table") == "ATTLOG":
        raw = request.get_data().decode("utf-8", errors="replace").strip()
        for linea in raw.splitlines():
            partes = linea.split()
            if len(partes) >= 3:
                procesar_fichaje(
                    pin        = partes[0],
                    fecha_hora = f"{partes[1]} {partes[2]}",
                    status     = partes[3] if len(partes) > 3 else "0",
                    verify     = partes[4] if len(partes) > 4 else "0",
                    sn         = sn,
                )

    return Response("OK", mimetype="text/plain")


@app.route("/iclock/getrequest", methods=["GET"])
def heartbeat():
    registrar_reloj(request.args.get("SN", "?"), ip_cliente())
    return Response("OK", mimetype="text/plain")


@app.route("/iclock/devicecmd", methods=["POST"])
def devicecmd():
    return Response("OK", mimetype="text/plain")

# ---------- Salud (Render Health Check) ----------

@app.route("/health")
def health():
    return {"status": "ok", "hora": ts()}

# ---------- Consulta ----------

@app.route("/")
@app.route("/dashboard")
def dashboard():
    proteger()
    log_lines = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            log_lines = [l.rstrip() for l in f.readlines()[-80:] if l.strip()]
    with estado_lock:
        relojes = dict(relojes_sesion)

    return render_template_string(
        DASHBOARD_HTML,
        stats=get_stats(), registros=get_ultimos_registros(50), log_lines=log_lines,
        relojes=relojes, api_url=API_URL, modo_historico=MODO_HISTORICO, ahora=ts(),
    )


@app.route("/registros")
def ver_registros():
    proteger()
    con = conectar()
    rows = con.execute("SELECT pin, fecha_hora, enviado, respuesta FROM asistencias ORDER BY id DESC LIMIT 50").fetchall()
    con.close()
    if not rows:
        return "Sin registros aún.", 200, TXT
    estados = {0: "PENDIENTE", 1: "ENVIADO", 2: "ERROR"}
    lines = ["PIN    Fecha/Hora            Estado     Respuesta", "-" * 75]
    for pin, fh, env, resp in rows:
        lines.append(f"{pin:<6} {fh:<22} {estados.get(env,'?'):<10} {(resp or '')[:40]}")
    return "\n".join(lines), 200, TXT


@app.route("/log")
def ver_log():
    proteger()
    if not os.path.exists(LOG_PATH):
        return "Sin log aún.", 200, TXT
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        return f.read(), 200, TXT


@app.route("/pendientes")
def ver_pendientes():
    proteger()
    rows = get_pendientes()
    if not rows:
        return "Sin pendientes — todo enviado correctamente.", 200, TXT
    lines = [f"Total pendientes: {len(rows)}", "-" * 50] + [f"PIN={pin}  {fh}" for _, pin, fh, *_ in rows]
    return "\n".join(lines), 200, TXT


# ══════════════════════════════════════════════════════
#  LOCAL (en Render se usa gunicorn, no esto)
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Servidor local en http://localhost:{PORT}/")
    app.run(host="0.0.0.0", port=PORT, debug=False)
