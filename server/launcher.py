"""Sol — arranca el servidor y lo mantiene actualizado desde GitHub.

Cada UPDATE_MINUTES se fija si hay una versión nueva en el repositorio.
Si la hay, la baja, la prueba y la pone en marcha. Si la versión nueva no
arranca, vuelve sola a la anterior. Los datos (/data/sol.db) no se tocan.
"""
import hashlib
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = os.environ.get("GITHUB_REPO", "ferwinkelia/sol-app")
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
INTERVAL = max(60, int(os.environ.get("UPDATE_MINUTES", "5")) * 60)
BASE = os.environ.get("APP_DIR", "/data/app")
CUR, PREV, NEW = BASE + "/current", BASE + "/previous", BASE + "/new"
BAD_FILE = BASE + "/descartadas.txt"


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[actualizador]", *a, flush=True)


def get(url):
    h = {"User-Agent": "sol-app", "Accept": "application/vnd.github+json"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
        return r.read()


def ultimo_sha():
    return json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))["sha"]


def sha_de(d):
    try:
        return open(d + "/.sha").read().strip()
    except OSError:
        return ""


def bajar(sha, dest):
    data = get(f"https://codeload.github.com/{REPO}/tar.gz/{sha}")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            partes = m.name.split("/", 1)
            if len(partes) < 2 or not partes[1] or m.issym() or m.islnk():
                continue
            rel = os.path.normpath(partes[1])
            if rel.startswith("..") or os.path.isabs(rel):
                continue
            destino = os.path.join(dest, rel)
            if m.isdir():
                os.makedirs(destino, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(destino), exist_ok=True)
                with t.extractfile(m) as src, open(destino, "wb") as out:
                    out.write(src.read())
    with open(dest + "/.sha", "w") as f:
        f.write(sha)


def valida(d):
    try:
        if not (os.path.isfile(d + "/server/server.py") and os.path.isfile(d + "/app/mamaderas.html")):
            return False
        for f in ("server/server.py", "server/launcher.py"):
            py_compile.compile(os.path.join(d, f), doraise=True, cfile=os.devnull)
        return True
    except Exception as e:
        log("versión inválida:", e)
        return False


def huella(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return ""


def arrancar():
    env = dict(os.environ, APP_VERSION=sha_de(CUR)[:7])
    return subprocess.Popen([sys.executable, CUR + "/server/server.py"], env=env)


def parar(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def sana(p, segundos=20):
    fin = time.time() + segundos
    while time.time() < fin:
        if p.poll() is not None:
            return False
        time.sleep(1)
    return True


def descartadas():
    try:
        return set(open(BAD_FILE).read().split())
    except OSError:
        return set()


def main():
    os.makedirs(BASE, exist_ok=True)
    while not valida(CUR):
        try:
            sha = ultimo_sha()
            log("primera instalación, bajando", sha[:7])
            bajar(sha, CUR)
        except Exception as e:
            log("no se pudo bajar la app:", e, "- reintento en 30 s")
            time.sleep(30)
    lanzador = huella(CUR + "/server/launcher.py")
    p = arrancar()
    proxima = time.time() + 30
    while True:
        time.sleep(5)
        if p.poll() is not None:
            log("el servidor se detuvo (código", p.returncode, ")")
            time.sleep(3)
            p = arrancar()
            continue
        if time.time() < proxima:
            continue
        proxima = time.time() + INTERVAL
        try:
            sha = ultimo_sha()
        except Exception as e:
            log("no se pudo consultar GitHub:", e)
            continue
        if sha == sha_de(CUR) or sha in descartadas():
            continue
        try:
            log("hay versión nueva", sha[:7], "- bajando")
            bajar(sha, NEW)
        except Exception as e:
            log("falló la descarga:", e)
            continue
        if not valida(NEW):
            open(BAD_FILE, "a").write(sha + "\n")
            continue
        parar(p)
        shutil.rmtree(PREV, ignore_errors=True)
        os.rename(CUR, PREV)
        os.rename(NEW, CUR)
        p = arrancar()
        if not sana(p):
            log("la versión", sha[:7], "no arrancó: vuelvo a la anterior")
            parar(p)
            open(BAD_FILE, "a").write(sha + "\n")
            shutil.rmtree(NEW, ignore_errors=True)
            os.rename(CUR, NEW)
            os.rename(PREV, CUR)
            p = arrancar()
            continue
        log("actualizado a", sha[:7])
        if huella(CUR + "/server/launcher.py") != lanzador:
            log("el actualizador cambió: me reinicio")
            parar(p)
            os.execv(sys.executable, [sys.executable, CUR + "/server/launcher.py"])


if __name__ == "__main__":
    main()
