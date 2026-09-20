import logging
import os
import threading
import time
import urllib.parse

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("whatsapp_client")

CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/chromium")
CHROMEDRIVER_BIN = os.getenv("CHROMEDRIVER_BIN", "/usr/bin/chromedriver")

WHATSAPP_URL = "https://web.whatsapp.com"
QR_CANVAS_SELECTOR = "canvas[aria-label='Scan this QR code to link a device!'], div[data-testid='qrcode'] canvas"
CHAT_LIST_SELECTOR = "div[aria-label='Chat list'], div[id='pane-side']"
MESSAGE_BOX_SELECTOR = "div[contenteditable='true'][data-tab='10'], footer div[contenteditable='true']"
SEND_BUTTON_SELECTOR = "button[aria-label='Send'], span[data-icon='send']"
INVALID_NUMBER_TEXT = "Phone number shared via url is invalid"


class WhatsAppWebClient:
    """
    Envoltorio sobre Selenium para automatizar WhatsApp Web sin usar la API
    oficial de Meta. Requiere vincular manualmente el dispositivo una vez
    (escaneando el QR) y mantiene la sesion en un perfil de Chrome persistente.
    """

    def __init__(self, profile_dir: str, qr_path: str, headless: bool = True):
        self.profile_dir = profile_dir
        self.qr_path = qr_path
        self.headless = headless
        self.driver = None
        self.logged_in = False
        # Selenium no es seguro para comandos concurrentes sobre la misma sesion
        # de navegador: si dos notificaciones llegan casi al mismo tiempo, sin
        # este lock una puede leer el estado de la otra a mitad de una
        # navegacion (p.ej. justo cuando la otra esta en el chat de destino) y
        # reportar erroneamente que no hay sesion activa.
        self._lock = threading.Lock()

    def iniciar(self):
        options = Options()
        options.binary_location = CHROME_BIN
        options.add_argument(f"--user-data-dir={self.profile_dir}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--lang=es-CO")
        # WhatsApp Web muestra una pantalla de "actualiza tu navegador" cuando
        # detecta que es un navegador automatizado (via navigator.webdriver),
        # no por la version real de Chromium. Estos flags evitan esa deteccion.
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        if self.headless:
            options.add_argument("--headless=new")

        service = Service(CHROMEDRIVER_BIN)
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        # Chrome headless suspende los timers (rAF/visibilitychange) de páginas
        # que no considera "enfocadas", lo que congela el contador de refresco
        # del QR de WhatsApp Web tras el primer render. Esto fuerza a Chrome a
        # tratar la pestaña como siempre enfocada.
        self.driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self.driver.get(WHATSAPP_URL)
        logger.info("WhatsApp Web abierto, esperando sesion o QR...")
        self._esperar_sesion_o_qr()

    def _esperar_sesion_o_qr(self, qr_stale_segundos: int = 45):
        """Corre indefinidamente hasta que haya sesion activa. No tiene deadline
        porque este metodo corre en un hilo de fondo durante toda la vida del
        proceso: si se rindiera tras un timeout, el QR quedaria congelado para
        siempre sin que nada lo vuelva a refrescar."""
        ultimo_qr_bytes = None
        ultimo_cambio = time.time()
        while True:
            try:
                if self._chat_list_visible():
                    self.logged_in = True
                    logger.info("Sesion de WhatsApp Web activa.")
                    return
                if self._guardar_qr_si_existe():
                    with open(self.qr_path, "rb") as f:
                        actual = f.read()
                    if actual != ultimo_qr_bytes:
                        ultimo_qr_bytes = actual
                        ultimo_cambio = time.time()
                    elif time.time() - ultimo_cambio > qr_stale_segundos:
                        logger.info("QR estancado por %ss, recargando pagina...", qr_stale_segundos)
                        self.driver.refresh()
                        ultimo_qr_bytes = None
                        ultimo_cambio = time.time()
                        time.sleep(3)
                        continue
                    logger.info("QR disponible en %s, esperando escaneo...", self.qr_path)
            except Exception:
                logger.exception("Error en el loop de espera de sesion/QR, reintentando...")
            time.sleep(2)

    def _chat_list_visible(self) -> bool:
        try:
            self.driver.find_element(By.CSS_SELECTOR, CHAT_LIST_SELECTOR)
            return True
        except NoSuchElementException:
            return False

    def _guardar_qr_si_existe(self) -> bool:
        try:
            qr = self.driver.find_element(By.CSS_SELECTOR, QR_CANVAS_SELECTOR)
            qr.screenshot(self.qr_path)
            return True
        except NoSuchElementException:
            return False

    def estado(self) -> dict:
        if self.driver is None:
            return {"logged_in": False, "iniciado": False}
        with self._lock:
            logged_in = self._chat_list_visible()
        self.logged_in = logged_in
        return {"logged_in": logged_in, "iniciado": True}

    def enviar_mensaje(self, telefono: str, mensaje: str) -> dict:
        if self.driver is None:
            raise RuntimeError("El cliente de WhatsApp Web no ha sido iniciado")

        with self._lock:
            return self._enviar_mensaje_sin_lock(telefono, mensaje)

    def _enviar_mensaje_sin_lock(self, telefono: str, mensaje: str) -> dict:
        texto = urllib.parse.quote(mensaje)
        url = f"{WHATSAPP_URL}/send?phone={telefono}&text={texto}"
        self.driver.get(url)

        wait = WebDriverWait(self.driver, 30)
        try:
            wait.until(lambda d: self._numero_invalido() or self._caja_mensaje_lista())
        except TimeoutException:
            return {"success": False, "error": "Timeout esperando que cargara el chat"}

        if self._numero_invalido():
            return {"success": False, "error": "El numero no tiene WhatsApp o es invalido"}

        try:
            boton = self.driver.find_element(By.CSS_SELECTOR, SEND_BUTTON_SELECTOR)
            boton.click()
        except NoSuchElementException:
            caja = self.driver.find_element(By.CSS_SELECTOR, MESSAGE_BOX_SELECTOR)
            caja.send_keys("")  # Enter

        time.sleep(1)
        return {"success": True}

    def _numero_invalido(self) -> bool:
        return INVALID_NUMBER_TEXT in self.driver.page_source

    def _caja_mensaje_lista(self) -> bool:
        try:
            self.driver.find_element(By.CSS_SELECTOR, MESSAGE_BOX_SELECTOR)
            return True
        except NoSuchElementException:
            return False
