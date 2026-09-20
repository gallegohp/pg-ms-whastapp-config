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
PHONE_NUMBER_SHORTCUT_SELECTOR = "[data-testid='link_device_qr_phone_number_shortcut_link']"
PHONE_NUMBER_INPUT_SELECTOR = "[data-testid='phone-number-input']"
LINK_CODE_CELLS_SELECTOR = "[data-testid='link-with-phone-number-code-cells']"
COUNTRY_SELECTOR_BUTTON = "[data-testid='phone-number-country-selector']"
COUNTRY_SEARCH_INPUT = "[data-testid='search-input']"
COUNTRY_FIRST_RESULT = "[data-testid='list-item-0']"


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
                # Sin este lock, este loop de fondo compite por la misma
                # sesion de Selenium (una sola conexion HTTP a chromedriver)
                # con cualquier llamada a estado()/enviar_mensaje()/
                # solicitar_codigo(), causando comandos perdidos o
                # entremezclados ("Connection pool is full, discarding
                # connection") y navegaciones que parecen "revertirse" solas.
                with self._lock:
                    if self._chat_list_visible():
                        self.logged_in = True
                        logger.info("Sesion de WhatsApp Web activa.")
                        return
                    hay_qr = self._guardar_qr_si_existe()
                if hay_qr:
                    with open(self.qr_path, "rb") as f:
                        actual = f.read()
                    if actual != ultimo_qr_bytes:
                        ultimo_qr_bytes = actual
                        ultimo_cambio = time.time()
                    elif time.time() - ultimo_cambio > qr_stale_segundos:
                        logger.info("QR estancado por %ss, recargando pagina...", qr_stale_segundos)
                        with self._lock:
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

    def solicitar_codigo(self, telefono: str, pais: str = "Colombia", timeout_segundos: int = 20) -> str:
        """Pide un codigo de 8 caracteres para vincular el dispositivo usando el
        numero de telefono, como alternativa a escanear el QR (util cuando no
        hay forma comoda de mostrar una imagen, o cuando escanear falla por un
        QR ya vencido). Solo tiene sentido si todavia no hay sesion activa.

        `telefono` va sin el codigo de pais (WhatsApp lo agrega segun el pais
        seleccionado). Al entrar a esta pantalla WhatsApp resuelve el pais por
        defecto de forma asincrona (geolocalizacion): por un instante muestra
        un pais transitorio incorrecto (en pruebas, "Greece") antes de asentarse
        en el real. Por eso se espera a que el campo de telefono ya tenga un
        prefijo antes de tocar nada, y ademas se selecciona el pais deseado
        explicitamente (no basta con esperar, porque el default resuelto puede
        no ser el que se quiere si el servidor esta en otro pais).

        El codigo se lee directamente del atributo `data-link-code` del DOM en
        vez de tomarlo de una captura de pantalla, para evitar errores de
        lectura (p.ej. confundir O/0, I/1) en los caracteres mostrados.
        """
        if self.driver is None:
            raise RuntimeError("El cliente de WhatsApp Web no ha sido iniciado")

        with self._lock:
            if self._chat_list_visible():
                raise RuntimeError("Ya hay una sesion activa, no se necesita codigo")

            wait = WebDriverWait(self.driver, timeout_segundos)

            try:
                atajo = wait.until(
                    lambda d: d.find_element(By.CSS_SELECTOR, PHONE_NUMBER_SHORTCUT_SELECTOR)
                )
                self._click(atajo)
            except TimeoutException:
                # Puede que ya estemos en la pantalla de ingresar el numero
                # (p.ej. si se llamo antes y quedo a mitad de camino).
                logger.info("Atajo de 'vincular con numero' no visible, se asume que ya se paso esa pantalla.")

            # Esperar a que se resuelva el pais transitorio antes de tocar el
            # selector, o el click puede caer sobre el dropdown a mitad de un
            # re-render y no abrir nada.
            wait.until(
                lambda d: (d.find_element(By.CSS_SELECTOR, PHONE_NUMBER_INPUT_SELECTOR).get_attribute("value") or "").strip().startswith("+")
            )

            selector_pais = wait.until(
                lambda d: d.find_element(By.CSS_SELECTOR, COUNTRY_SELECTOR_BUTTON)
            )
            self._click(selector_pais)

            busqueda = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, COUNTRY_SEARCH_INPUT))
            busqueda.send_keys(pais)

            def _resultado_filtrado(d):
                # El primer item de la lista ya existe desde antes de escribir
                # (con el listado sin filtrar), asi que no basta con esperar a
                # que exista: hay que esperar a que su texto refleje el
                # resultado de la busqueda, o se puede terminar seleccionando
                # cualquier pais.
                el = d.find_element(By.CSS_SELECTOR, COUNTRY_FIRST_RESULT)
                return el if pais.lower() in el.text.lower() else False

            primer_resultado = wait.until(_resultado_filtrado)
            self._click(primer_resultado)

            campo = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, PHONE_NUMBER_INPUT_SELECTOR))
            # No se limpia el campo antes de escribir: un Ctrl+A + Delete aqui
            # deja el campo "vacio" momentaneamente, lo que le hace perder el
            # pais ya seleccionado (vuelve a mostrar el pais transitorio, p.ej.
            # "Greece"). Como llegamos aqui con el campo recien inicializado,
            # ya deberia estar vacio salvo por el prefijo del pais.
            campo.send_keys(telefono)

            boton = self.driver.find_element(By.XPATH, "//button[contains(., 'Next')]")
            self._click(boton)

            celdas = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, LINK_CODE_CELLS_SELECTOR))
            crudo = celdas.get_attribute("data-link-code")  # ej: "T,7,R,J,R,B,R,4"
            caracteres = crudo.split(",")
            codigo = "".join(caracteres)
            return f"{codigo[:4]}-{codigo[4:]}"

    def _click(self, elemento):
        try:
            elemento.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", elemento)
