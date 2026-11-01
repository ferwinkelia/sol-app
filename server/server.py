"""Sol — servidor de la app de registro.

Solo biblioteca estándar de Python 3.11+. Guarda documentos JSON en SQLite
(/data/sol.db) con la misma forma que la base de datos de la versión de Claude:
colecciones (tomas, panales, medicamentos, dosis, controles, config) de
documentos con id. Los celulares sincronizan con /api/sync (long-poll).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_PIN = os.environ.get("APP_PIN", "").strip()
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8090"))
DB_PATH = os.environ.get("DB_PATH", "/data/sol.db")
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "-3"))))
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
VERSION = os.environ.get("APP_VERSION", "")

if not APP_PIN:
    sys.exit("Falta APP_PIN en la configuración del stack")

NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_BODY = 600_000
cond = threading.Condition()
fallos = {}            # ip -> [instantes de intentos fallidos]
fallos_global = []     # todos los intentos fallidos de PIN, de cualquier lugar
bloqueo_hasta = [0.0]  # bloqueo general del ingreso con PIN
POR_IP, VENTANA_IP = 5, 900          # 5 fallos por conexión cada 15 min
GLOBAL, VENTANA_G, BLOQUEO_G = 20, 3600, 3600  # 20 fallos en total en 1 h -> 1 h sin ingreso con PIN


# ---------------- base de datos ----------------
db = None


def iniciar_db():
    global db
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS docs(
        col TEXT NOT NULL, id TEXT NOT NULL, data TEXT, seq INTEGER NOT NULL,
        PRIMARY KEY(col, id))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_seq ON docs(seq)")
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS celulares(hash TEXT PRIMARY KEY, nombre TEXT, creado INTEGER, ultimo INTEGER)")
    db.execute("INSERT OR IGNORE INTO meta VALUES('seq', 0)")
    db.commit()


def seq_actual():
    return db.execute("SELECT v FROM meta WHERE k='seq'").fetchone()[0]


def escribir(cambios):
    """cambios: lista de (col, id, data|None). Devuelve el nuevo seq."""
    with cond:
        s = seq_actual()
        for col, did, data in cambios:
            s += 1
            db.execute(
                "INSERT INTO docs(col,id,data,seq) VALUES(?,?,?,?) "
                "ON CONFLICT(col,id) DO UPDATE SET data=excluded.data, seq=excluded.seq",
                (col, did, None if data is None else json.dumps(data, ensure_ascii=False), s),
            )
        db.execute("UPDATE meta SET v=? WHERE k='seq'", (s,))
        db.commit()
        cond.notify_all()
        return s


def leer(col, did):
    with cond:
        r = db.execute("SELECT data FROM docs WHERE col=? AND id=?", (col, did)).fetchone()
    return json.loads(r[0]) if r and r[0] is not None else None


def coleccion(col):
    with cond:
        rows = db.execute("SELECT id, data FROM docs WHERE col=? AND data IS NOT NULL", (col,)).fetchall()
    return [dict(json.loads(d), id=i) for i, d in rows]


def cambios_desde(since):
    with cond:
        if since <= 0:
            rows = db.execute("SELECT col,id,data FROM docs WHERE data IS NOT NULL").fetchall()
        else:
            rows = db.execute("SELECT col,id,data FROM docs WHERE seq>? ORDER BY seq", (since,)).fetchall()
        s = seq_actual()
    return s, [{"col": c, "id": i, "data": json.loads(d) if d is not None else None} for c, i, d in rows]


def hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


def token_valido(t):
    if not t or len(t) < 30:
        return False
    h = hash_token(t)
    with cond:
        r = db.execute("SELECT 1 FROM celulares WHERE hash=?", (h,)).fetchone()
        if r:
            db.execute("UPDATE celulares SET ultimo=? WHERE hash=?", (ahora(), h))
            db.commit()
    return bool(r)


def nuevo_celular(nombre):
    t = secrets.token_urlsafe(32)
    with cond:
        db.execute("INSERT INTO celulares VALUES(?,?,?,?)", (hash_token(t), nombre[:60], ahora(), ahora()))
        db.commit()
    return t


def nuevo_id():
    return secrets.token_urlsafe(15).replace("-", "a").replace("_", "b")[:20]


# ---------------- utilidades para Home Assistant y Siri ----------------
def ahora():
    return int(time.time() * 1000)


def hora(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat(timespec="seconds") if ms else None


def config():
    c = leer("config", "ajustes") or {}
    return {"espera": int(c.get("espera") or 15), "nombre": c.get("nombre") or "Sol"}


def estado():
    t = ahora()
    tomas = coleccion("tomas")
    activa = next((x for x in sorted(tomas, key=lambda x: -x.get("inicio", 0)) if x.get("fin") is None), None)
    terminadas = [x for x in tomas if x.get("fin")]
    ult = max(terminadas, key=lambda x: x["fin"], default=None)
    hoy0 = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    hoy = [x for x in tomas if x.get("inicio", 0) >= hoy0]
    con_ml = [x["ml"] for x in hoy if isinstance(x.get("ml"), (int, float))]
    espera = config()["espera"]
    acostar = ult["fin"] + espera * 60000 if (ult and not activa) else None
    panales = coleccion("panales")
    ult_p = max(panales, key=lambda x: x.get("hora", 0), default=None)
    # próxima medicación "cada N horas"
    meds = [m for m in coleccion("medicamentos") if m.get("activo", True) and m.get("tipo") == "horas"]
    dosis = coleccion("dosis")
    prox = None
    for m in meds:
        ds = [d["hora"] for d in dosis if d.get("medId") == m["id"]]
        if ds:
            n = max(ds) + int(m.get("cada") or 6) * 3600000
            if not prox or n < prox[0]:
                prox = (n, m.get("nombre"))
    return {
        "estado": "tomando" if activa else ("esperar" if acostar and acostar > t else "libre"),
        "inicio_en_curso": iso(activa["inicio"]) if activa else None,
        "ultima_toma_inicio": iso(max((x["inicio"] for x in tomas), default=0)) if tomas else None,
        "ultima_fin": iso(ult["fin"]) if ult else None,
        "acostar_desde": iso(acostar),
        "minutos_restantes": max(0, -(-(acostar - t) // 60000)) if acostar else 0,
        "ml_hoy": sum(con_ml),
        "tomas_hoy": len(hoy),
        "promedio_hoy": round(sum(con_ml) / len(con_ml)) if con_ml else 0,
        "ultimo_panal": iso(ult_p["hora"]) if ult_p else None,
        "proxima_medicacion": iso(prox[0]) if prox else None,
        "proxima_medicacion_nombre": prox[1] if prox else None,
    }


def accion(nombre, d):
    t = ahora()
    tomas = coleccion("tomas")
    activa = next((x for x in tomas if x.get("fin") is None), None)
    if nombre == "empezo":
        if activa:
            return f"Ya hay una toma en curso desde las {hora(activa['inicio'])}."
        escribir([("tomas", nuevo_id(), {"inicio": t, "fin": None, "ml": None, "sobro": 0, "retomas": 0, "nota": ""})])
        return f"Listo, empezó a tomar a las {hora(t)}."
    if nombre == "termino":
        if not activa:
            return "No hay ninguna toma en curso."
        ml = d.get("ml")
        try:
            ml = None if ml in (None, "") else max(0, min(1000, round(float(ml))))
        except (TypeError, ValueError):
            ml = None
        data = {k: v for k, v in activa.items() if k != "id"}
        data["fin"] = t
        if ml is not None:
            data["ml"] = ml
        escribir([("tomas", activa["id"], data)])
        msg = f"Listo, terminó a las {hora(t)}."
        msg += f" Anoté {ml} mililitros." if ml is not None else " Falta anotar los mililitros."
        return msg + f" Se puede acostar a las {hora(t + config()['espera'] * 60000)}."
    if nombre == "panal":
        pis = bool(d.get("pis", True))
        caca = bool(d.get("caca", False))
        if not pis and not caca:
            pis = True
        escribir([("panales", nuevo_id(), {"hora": t, "pis": pis, "caca": caca, "nota": ""})])
        return "Listo, anoté el pañal de las " + hora(t) + "."
    return None


# ---------------- HTTP ----------------
def construir_index():
    """Arma static/index.html a partir de app/mamaderas.html (la misma app de Claude)."""
    src = (ROOT / "app" / "mamaderas.html").read_text(encoding="utf-8")
    head = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon-192.png">'
        '<link rel="apple-touch-icon" href="apple-touch-icon.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        '<meta name="apple-mobile-web-app-title" content="Sol"><meta name="theme-color" content="#0E151B">'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style>'
        '<script src="shim.js?v=' + VERSION + '"></script></head><body>'
    )
    return (head + src + "</body></html>").encode("utf-8")


INDEX = None
TIPOS = {".js": "text/javascript; charset=utf-8", ".png": "image/png", ".html": "text/html; charset=utf-8",
         ".webmanifest": "application/manifest+json"}
MANIFEST = json.dumps({
    "name": "Sol", "short_name": "Sol", "start_url": "./", "scope": "./", "display": "standalone",
    "background_color": "#0E151B", "theme_color": "#0E151B", "lang": "es-AR",
    "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
}).encode()


class App(BaseHTTPRequestHandler):
    server_version = "Sol"
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # el celular cerró la conexión (por ejemplo, al bloquear la pantalla)

    def log_message(self, fmt, *args):
        if os.environ.get("LOG") == "1":
            super().log_message(fmt, *args)

    def ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def responder(self, code, cuerpo, tipo="application/json; charset=utf-8", cache="no-store"):
        datos = cuerpo if isinstance(cuerpo, bytes) else (
            cuerpo.encode() if isinstance(cuerpo, str) else json.dumps(cuerpo, ensure_ascii=False).encode())
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", cache)
        if code >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(datos)

    def fallo(self, ip, global_tambien):
        t = time.time()
        fallos.setdefault(ip, []).append(t)
        if global_tambien:
            fallos_global[:] = [x for x in fallos_global if t - x < VENTANA_G] + [t]
            if len(fallos_global) >= GLOBAL:
                bloqueo_hasta[0] = t + BLOQUEO_G
                fallos_global.clear()
                print("Demasiados intentos de PIN: ingreso con PIN bloqueado por 1 hora", flush=True)
        time.sleep(0.8)

    def ip_bloqueada(self, ip):
        t = time.time()
        fallos[ip] = [x for x in fallos.get(ip, []) if t - x < VENTANA_IP]
        return len(fallos[ip]) >= POR_IP

    def autorizado(self):
        """Celulares: clave propia (X-Device). Home Assistant y Siri: Bearer API_TOKEN."""
        ip = self.ip()
        if self.ip_bloqueada(ip):
            return None
        auth = self.headers.get("Authorization", "")
        dev = self.headers.get("X-Device", "").strip()
        if API_TOKEN and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), API_TOKEN):
            return True
        if dev and token_valido(dev):
            return True
        self.fallo(ip, False)
        return False

    def login(self):
        ip = self.ip()
        if bloqueo_hasta[0] > time.time():
            return self.responder(429, {"error": "Hubo demasiados intentos. El ingreso con PIN está pausado; probá en una hora."})
        if self.ip_bloqueada(ip):
            return self.responder(429, {"error": "Demasiados intentos. Esperá 15 minutos."})
        try:
            d = self.cuerpo()
        except (ValueError, json.JSONDecodeError):
            d = {}
        pin = str(d.get("pin", "")).strip()
        if not pin or not hmac.compare_digest(pin, APP_PIN):
            self.fallo(ip, True)
            return self.responder(401, {"error": "PIN incorrecto"})
        fallos.pop(ip, None)
        nombre = str(d.get("nombre") or self.headers.get("User-Agent", ""))[:60]
        return self.responder(200, {"token": nuevo_celular(nombre)})

    def leer_cuerpo(self):
        """Lee siempre el cuerpo completo del pedido, se use o no."""
        if hasattr(self, "_raw"):
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000_000:
            self._raw = None
            return
        self._raw = self.rfile.read(n) if n > 0 else b""

    def cuerpo(self):
        self.leer_cuerpo()
        if self._raw is None or len(self._raw) > MAX_BODY:
            raise ValueError("Pedido demasiado grande")
        if not self._raw:
            return {}
        d = json.loads(self._raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("Se esperaba un objeto JSON")
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            return self.api("GET", u)
        nombre = u.path.lstrip("/") or "index.html"
        if nombre == "index.html":
            return self.responder(200, INDEX, TIPOS[".html"], "no-cache")
        if nombre == "manifest.webmanifest":
            return self.responder(200, MANIFEST, TIPOS[".webmanifest"], "no-cache")
        f = (STATIC / nombre).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self.responder(404, {"error": "No encontrado"})
        self.responder(200, f.read_bytes(), TIPOS.get(f.suffix, "application/octet-stream"), "no-cache")

    def do_POST(self):
        self.api("POST", urlparse(self.path))

    def do_PUT(self):
        self.api("PUT", urlparse(self.path))

    def do_PATCH(self):
        self.api("PATCH", urlparse(self.path))

    def do_DELETE(self):
        self.api("DELETE", urlparse(self.path))

    def api(self, metodo, u):
        self.__dict__.pop("_raw", None)  # la conexión se reutiliza: cada pedido lee su propio cuerpo
        self.leer_cuerpo()
        if self._raw is None:
            return self.responder(413, {"error": "Pedido demasiado grande"})
        if u.path == "/api/salud":
            return self.responder(200, {"ok": True, "version": VERSION})
        if u.path == "/api/login" and metodo == "POST":
            return self.login()
        ok = self.autorizado()
        if ok is None:
            return self.responder(429, {"error": "Demasiados intentos. Esperá 15 minutos."})
        if not ok:
            return self.responder(401, {"error": "Este celular no está autorizado"})
        q = parse_qs(u.query)
        partes = u.path.strip("/").split("/")[1:]
        try:
            if partes == ["sync"] and metodo == "GET":
                since = int(q.get("since", ["0"])[0] or 0)
                if q.get("wait") and since > 0:
                    limite = time.time() + 25
                    with cond:
                        while seq_actual() <= since and time.time() < limite:
                            cond.wait(timeout=limite - time.time())
                s, ch = cambios_desde(since)
                return self.responder(200, {"seq": s, "changes": ch, "version": VERSION})
            if len(partes) == 3 and partes[0] == "doc":
                col, did = partes[1], partes[2]
                if not NAME_RE.match(col) or not NAME_RE.match(did):
                    raise ValueError("Nombre inválido")
                if metodo == "PUT":
                    data = self.cuerpo()
                    escribir([(col, did, data)])
                    return self.responder(200, {"ok": True, "data": data})
                if metodo == "PATCH":
                    cur = leer(col, did)
                    if cur is None:
                        return self.responder(404, {"error": "El registro ya no existe"})
                    cur.update(self.cuerpo())
                    escribir([(col, did, cur)])
                    return self.responder(200, {"ok": True, "data": cur})
                if metodo == "DELETE":
                    escribir([(col, did, None)])
                    return self.responder(200, {"ok": True})
            if partes == ["import"] and metodo == "POST":
                d = self.cuerpo_grande()
                cols = d.get("colecciones") or {}
                pisar = bool(d.get("pisar"))
                cambios, cuenta = [], {}
                for col, docs in cols.items():
                    if not NAME_RE.match(col) or not isinstance(docs, list):
                        continue
                    for doc in docs:
                        if not isinstance(doc, dict) or not NAME_RE.match(str(doc.get("id", ""))):
                            continue
                        did = str(doc["id"])
                        if not pisar and leer(col, did) is not None:
                            continue
                        cambios.append((col, did, {k: v for k, v in doc.items() if k != "id"}))
                        cuenta[col] = cuenta.get(col, 0) + 1
                if cambios:
                    escribir(cambios)
                return self.responder(200, {"ok": True, "agregados": cuenta})
            if partes == ["export"] and metodo == "GET":
                s, ch = cambios_desde(0)
                out = {}
                for c in ch:
                    out.setdefault(c["col"], []).append(dict(c["data"], id=c["id"]))
                return self.responder(200, {"version": 1, "exportado": iso(ahora()), "colecciones": out})
            if partes == ["desconectar"] and metodo == "POST":
                with cond:
                    db.execute("DELETE FROM celulares")
                    db.commit()
                return self.responder(200, {"ok": True})
            if partes == ["estado"] and metodo == "GET":
                return self.responder(200, estado())
            if len(partes) == 1 and partes[0] in ("empezo", "termino", "panal") and metodo == "POST":
                msg = accion(partes[0], self.cuerpo())
                if q.get("texto"):
                    return self.responder(200, msg, "text/plain; charset=utf-8")
                return self.responder(200, {"mensaje": msg, **estado()})
            return self.responder(404, {"error": "No encontrado"})
        except (ValueError, json.JSONDecodeError) as e:
            return self.responder(400, {"error": str(e)})

    def cuerpo_grande(self):
        self.leer_cuerpo()
        if self._raw is None:
            raise ValueError("Archivo demasiado grande")
        d = json.loads(self._raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("Archivo inválido")
        return d


def main():
    global INDEX
    iniciar_db()
    INDEX = construir_index()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), App)
    srv.daemon_threads = True
    print(f"Sol escuchando en el puerto {PORT} (versión {VERSION or 'local'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
