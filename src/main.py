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


@app.get("/debug/screenshot")
def debug_screenshot():
    """Endpoint temporal de diagnostico: captura la pagina completa tal cual
    la ve Selenium, para depurar selectores cuando WhatsApp Web cambia su DOM."""
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


class ClickRequest(BaseModel):
    selector: str
    by: str = "css"


class TypeRequest(BaseModel):
    selector: str
    text: str
    by: str = "css"
    enter: bool = False


@app.post("/debug/click")
def debug_click(request: ClickRequest):
    from selenium.webdriver.common.by import By

    by = By.XPATH if request.by == "xpath" else By.CSS_SELECTOR
    el = client.driver.find_element(by, request.selector)
    try:
        el.click()
    except Exception:
        client.driver.execute_script("arguments[0].click();", el)
    return {"ok": True}


@app.post("/debug/type")
def debug_type(request: TypeRequest):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    by = By.XPATH if request.by == "xpath" else By.CSS_SELECTOR
    el = client.driver.find_element(by, request.selector)
    el.send_keys(request.text)
    if request.enter:
        el.send_keys(Keys.ENTER)
    return {"ok": True}


@app.get("/debug/text")
def debug_text(selector: str, by: str = "css"):
    from selenium.webdriver.common.by import By

    by_type = By.XPATH if by == "xpath" else By.CSS_SELECTOR
    els = client.driver.find_elements(by_type, selector)
    return {"count": len(els), "texts": [e.text for e in els]}


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
