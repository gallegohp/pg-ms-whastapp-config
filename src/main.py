import logging
import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .whatsapp_client import WhatsAppWebClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-web-service")

PROFILE_DIR = os.getenv("CHROME_PROFILE_DIR", "/app/chrome-profile")
QR_PATH = os.getenv("QR_PATH", "/app/data/qr.png")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(QR_PATH), exist_ok=True)

app = FastAPI(title="pg-ms-whatsapp-web")
client = WhatsAppWebClient(profile_dir=PROFILE_DIR, qr_path=QR_PATH, headless=HEADLESS)


class EnvioRequest(BaseModel):
    telefono: str
    mensaje: str


class CodigoRequest(BaseModel):
    telefono: str
    pais: str = os.getenv("WHATSAPP_DEFAULT_COUNTRY_NAME", "Colombia")


@app.on_event("startup")
def iniciar_cliente():
    def _run():
        try:
            client.iniciar()
        except Exception:
            logger.exception("Error iniciando el cliente de WhatsApp Web")

    threading.Thread(target=_run, daemon=True).start()


@app.on_event("shutdown")
def cerrar_cliente():
    # Sin este cierre ordenado, "docker stop/restart" mata a Chrome de golpe y
    # deja el SingletonLock del perfil huerfano, lo que hace que el proximo
    # arranque falle con "Chrome instance exited" y obligue a borrar el volumen
    # (perdiendo la sesion de WhatsApp ya vinculada) para poder recuperarse.
    if client.driver is not None:
        try:
            client.driver.quit()
        except Exception:
            logger.exception("Error cerrando el driver de Chrome")


@app.get("/status")
def status():
    return client.estado()


@app.get("/qr")
def obtener_qr():
    if not os.path.exists(QR_PATH):
        raise HTTPException(status_code=404, detail="QR no disponible todavia (o ya hay sesion activa)")
    return FileResponse(QR_PATH, media_type="image/png")


@app.post("/codigo")
def solicitar_codigo(request: CodigoRequest):
    """Pide un codigo de 8 caracteres para vincular el dispositivo con el
    numero dado, como alternativa a escanear el QR de /qr. El codigo se
    ingresa en el telefono en WhatsApp > Dispositivos vinculados > Vincular
    un dispositivo > Vincular con numero de telefono."""
    try:
        codigo = client.solicitar_codigo(request.telefono, pais=request.pais)
        return {"codigo": codigo}
    except RuntimeError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except Exception as e:
        logger.exception("Error solicitando codigo de vinculacion")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/reiniciar-sesion")
def reiniciar_sesion():
    """Cierra la sesion de WhatsApp Web actual y arranca una nueva desde cero,
    para poder vincular un numero distinto al que esta activo (con /qr o
    /codigo). No bloquea: la sesion vieja se cierra y el nuevo QR/codigo queda
    listo en unos segundos, se puede consultar el progreso con /status."""

    def _run():
        try:
            client.reiniciar_sesion()
        except Exception:
            logger.exception("Error reiniciando la sesion de WhatsApp Web")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "mensaje": "Reiniciando sesion. Consulta /status o pide un /codigo o /qr en unos segundos."}


@app.get("/debug/screenshot")
def debug_screenshot():
    """Diagnostico de solo lectura: captura la pagina tal cual la ve
    Selenium. No permite interactuar con la pagina (a diferencia de los
    endpoints de click/type que existieron antes, se quitaron por ser un
    riesgo si este puerto llegara a exponerse publicamente)."""
    if client.driver is None:
        raise HTTPException(status_code=409, detail="El driver no ha sido iniciado")
    path = "/app/data/debug.png"
    client.driver.save_screenshot(path)
    return FileResponse(path, media_type="image/png")


@app.get("/debug/html")
def debug_html():
    if client.driver is None:
        raise HTTPException(status_code=409, detail="El driver no ha sido iniciado")
    return client.driver.page_source


@app.post("/enviar")
def enviar(request: EnvioRequest):
    estado = client.estado()
    if not estado.get("logged_in"):
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": "WhatsApp Web no tiene sesion activa. Escanea el QR en /qr"},
        )

    try:
        resultado = client.enviar_mensaje(request.telefono, request.mensaje)
    except Exception as e:
        logger.exception("Error enviando mensaje")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

    status_code = 200 if resultado.get("success") else 422
    return JSONResponse(status_code=status_code, content=resultado)
