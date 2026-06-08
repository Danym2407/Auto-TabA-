"""virtunum_api.py

Automatiza la web de VirtuNum usando Playwright para obtener numeros virtuales
y esperar el codigo SMS, igual que lo haria un usuario manualmente.

Primera vez: se abre una ventana del navegador y hay que iniciar sesion en
VirtuNum. Las cookies se guardan en .virtunum_profile/ y se reutilizan
automaticamente en ejecuciones posteriores.

Variables de entorno opcionales:
  VIRTUNUM_PRODUCT   Nombre del servicio a elegir (default: "Any other")
  VIRTUNUM_COUNTRY   Pais a elegir (default: "England")
  VIRTUNUM_HEADLESS  "1" para modo sin ventana (requiere sesion ya guardada)
  VIRTUNUM_CLOSE_BROWSER Se ignora en este flujo: Chrome queda abierto para no perder el OTP.
  VIRTUNUM_SMS_TIMEOUT  Segundos esperando el SMS (default: 180)
  VIRTUNUM_PROFILE_DIR  Directorio donde se guardan las cookies de sesion
"""

from __future__ import annotations

import os
import re
import shutil
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as _PWTimeout, Error as PlaywrightError
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False


class VirtuNumError(Exception):
    pass


@dataclass(frozen=True)
class ActivationOrder:
    order_id: str
    phone: str
    status: str


# Directorio de perfil de Chrome donde ya tienes sesion iniciada en VirtuNum.
# Por defecto usa el perfil "Diana" (Profile 34).
# Cambia VIRTUNUM_PROFILE_DIR si quieres usar otro perfil.
_CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\danym\AppData\Local"),
    "Google", "Chrome", "User Data",
)
_DEFAULT_PROFILE = os.path.join(_CHROME_USER_DATA, "Profile 34")

_VIRTUNUM_HOME = "https://virtunum.com/en"


def _copy_sqlite_best_effort(src: Path, dst: Path) -> bool:
    """Copia un archivo SQLite bloqueado usando sqlite3.backup con modo inmutable.

    Funciona con el archivo Cookies de Chrome aunque Chrome lo tenga abierto.
    """
    try:
        import sqlite3
        src_conn = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
        dst_conn = sqlite3.connect(str(dst))
        src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        return True
    except Exception:
        return False


def _copy_dir_skip_locked(src: Path, dst: Path) -> None:
    """Copia recursiva que omite (o lee via sqlite3) los archivos bloqueados."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dest = dst / item.name
        if item.is_dir():
            try:
                _copy_dir_skip_locked(item, dest)
            except Exception:
                pass
        else:
            try:
                shutil.copy2(item, dest)
            except OSError:
                # Fallback: leer DB SQLite en modo inmutable (funciona con Cookies de Chrome)
                _copy_sqlite_best_effort(item, dest)


class VirtuNumBrowser:
    """Controla el navegador para usar VirtuNum sin API.

    Flujo:
      1. Abre Chrome con perfil persistente (guarda login).
      2. Navega a VirtuNum y selecciona el servicio pedido.
      3. Lee el numero de telefono asignado.
      4. Espera a que aparezca el codigo SMS en la pagina.
      5. Devuelve (phone, code).
    """

    def __init__(
        self,
        service: str = "Any other",
        country: str = "England",
        headless: bool = False,
        profile_dir: str | None = None,  # si None, usa Profile 34 (Diana)
        sms_timeout: int = 180,
        close_browser_after_sms: bool = False,
    ):
        if not _PLAYWRIGHT_OK:
            raise VirtuNumError(
                "Playwright no instalado. Ejecuta:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        self.service = service
        self.country = country
        self.headless = headless
        self.profile_dir = profile_dir or os.environ.get(
              "VIRTUNUM_PROFILE_DIR", _DEFAULT_PROFILE
        )
        self.sms_timeout = sms_timeout
        self.close_browser_after_sms = close_browser_after_sms
        # Estado interno del navegador abierto entre open_and_get_phone y wait_for_sms
        self._pw_mgr = None
        self._p = None
        self._ctx = None
        self._page = None
        self._current_phone: str | None = None

    def _chrome_profile_launch_args(self) -> tuple[str, list[str]]:
        """Resuelve el directorio base de Chrome y el perfil a reutilizar.

        Si se recibe un path a un perfil concreto como ...\\User Data\\Profile 34,
        Playwright necesita abrir el directorio padre y pasar --profile-directory.
        """
        profile_path = Path(self.profile_dir)
        profile_name = profile_path.name
        if profile_name.lower().startswith("profile ") or profile_name in {"Default", "Guest Profile"}:
            return str(profile_path.parent), [f"--profile-directory={profile_name}"]
        return str(profile_path), []

    def _copy_locked_profile(self, user_data_dir: str, extra_args: list[str]) -> tuple[str, list[str]]:
        """Clona el perfil de Chrome a una carpeta temporal para evitar el lock.

        Los archivos bloqueados por Chrome (Cookies, Safe Browsing Cookies) se
        copian via sqlite3.backup con modo inmutable en lugar de shutil, lo que
        permite leerlos incluso mientras Chrome los tiene abiertos.
        """
        temp_root = tempfile.mkdtemp(prefix="virtunum-chrome-")
        source_root = Path(user_data_dir)

        local_state = source_root / "Local State"
        if local_state.exists():
            try:
                shutil.copy2(local_state, Path(temp_root) / "Local State")
            except OSError:
                _copy_sqlite_best_effort(local_state, Path(temp_root) / "Local State")

        for arg in extra_args:
            if not arg.startswith("--profile-directory="):
                continue
            profile_name = arg.split("=", 1)[1]
            source_profile = source_root / profile_name
            if source_profile.exists():
                _copy_dir_skip_locked(source_profile, Path(temp_root) / profile_name)

        return temp_root, extra_args

    def _launch_browser(self) -> None:
        """Abre Chrome con el perfil configurado y deja self._page listo."""
        # Limpiar cualquier loop asyncio existente antes de abrir Playwright
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                # Si hay un loop corriendo, no podemos hacer nada aquí
                print("VirtuNum: advertencia - hay un asyncio loop activo, intentando nuevo event loop")
                asyncio.set_event_loop(asyncio.new_event_loop())
            except RuntimeError:
                # No hay loop corriendo, verificar si hay un loop configurado
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        asyncio.set_event_loop(asyncio.new_event_loop())
                except RuntimeError:
                    asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass

        self._pw_mgr = sync_playwright()
        try:
            self._p = self._pw_mgr.start()
        except PlaywrightError as exc:
            if "using Playwright Sync API inside the asyncio loop" in str(exc):
                print("VirtuNum: detectado bucle asyncio activo durante start(), reiniciando event loop")
                self._close()
                try:
                    import asyncio
                    asyncio.set_event_loop(asyncio.new_event_loop())
                except Exception:
                    pass
                self._pw_mgr = sync_playwright()
                self._p = self._pw_mgr.start()
            else:
                raise

        user_data_dir, extra_args = self._chrome_profile_launch_args()
        print(f"VirtuNum: usando perfil Chrome {self.profile_dir} -> {user_data_dir} {extra_args}")
        try:
            self._ctx = self._p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="chrome",
                headless=self.headless,
                no_viewport=True,
                args=["--start-maximized", *extra_args],
            )
        except Exception as exc:
            if "Opening in existing browser session" not in str(exc):
                self._close()
                raise
            fallback_dir, fallback_args = self._copy_locked_profile(user_data_dir, extra_args)
            print(f"VirtuNum: perfil bloqueado, usando copia temporal {fallback_dir}")
            try:
                self._ctx = self._p.chromium.launch_persistent_context(
                    user_data_dir=fallback_dir,
                    channel="chrome",
                    headless=self.headless,
                    no_viewport=True,
                    args=["--start-maximized", *fallback_args],
                )
            except Exception:
                self._close()
                raise

        self._page = self._ctx.new_page()

    def open_existing_phone(self, phone: str) -> str:
        """Abre VirtuNum sin comprar otro numero para esperar el SMS de una orden existente."""
        self._launch_browser()
        try:
            print(f"VirtuNum: abriendo {_VIRTUNUM_HOME} para reutilizar {phone} ...")
            self._page.goto(_VIRTUNUM_HOME, wait_until="domcontentloaded", timeout=25_000)
            time.sleep(2)
            self._ensure_logged_in(self._page)

            phone_digits = re.sub(r"\D+", "", phone)
            suffix = phone_digits[-7:] if len(phone_digits) >= 7 else phone_digits
            if suffix:
                try:
                    self._page.get_by_text(re.compile(re.escape(suffix))).first.click(timeout=5_000)
                    time.sleep(1)
                except Exception:
                    pass
            self._current_phone = phone
            return phone
        except Exception:
            self._close()
            raise

    def _normalize_phone(self, phone: str) -> str:
        digits = re.sub(r"\D+", "", phone)
        return f"+{digits}" if digits else phone.strip()

    def _click_buy_next_number(self, page) -> bool:
        patterns = [
            r"buy next number",
            r"buy another number",
            r"comprar siguiente numero",
            r"comprar siguiente número",
            r"compra siguiente",
            r"next number",
            r"buy next",
        ]
        for pattern in patterns:
            try:
                locator = page.get_by_text(re.compile(pattern, re.IGNORECASE))
                if locator.count() > 0:
                    print(f"VirtuNum: clic en boton de siguiente numero ({pattern})")
                    locator.first.click(timeout=5_000)
                    time.sleep(1.5)
                    return True
            except Exception:
                pass
        return False

    def open_and_get_phone(self, skip_phones: set[str] | None = None) -> str:
        """Abre el navegador, navega a VirtuNum, compra el numero y lo devuelve.

        El navegador permanece abierto para que wait_for_sms pueda esperar el SMS
        despues de que el numero haya sido introducido en la app del movil.
        """
        self._launch_browser()
        try:
            print(f"VirtuNum: abriendo {_VIRTUNUM_HOME} ...")
            self._page.goto(_VIRTUNUM_HOME, wait_until="domcontentloaded", timeout=25_000)
            time.sleep(2)

            self._ensure_logged_in(self._page)

            print(f"VirtuNum: seleccionando servicio '{self.service}' ...")
            self._click_service(self._page)

            self._select_country(self._page)

            attempts = 0
            while True:
                phone = self._read_phone(self._page)
                if not phone:
                    raise VirtuNumError("No aparecio ningun numero de telefono en VirtuNum")
                phone = self._normalize_phone(phone)
                if skip_phones and phone in skip_phones:
                    attempts += 1
                    if attempts > 5:
                        raise VirtuNumError(
                            "No se pudo obtener un numero de VirtuNum diferente a los ya usados"
                        )
                    print(f"VirtuNum: numero {phone} ya fue usado. Buscando siguiente numero... ({attempts})")
                    if self._click_buy_next_number(self._page):
                        continue
                    try:
                        self._page.reload(wait_until="domcontentloaded", timeout=15_000)
                        time.sleep(1.5)
                        continue
                    except Exception:
                        raise VirtuNumError(
                            "No se pudo actualizar la pagina de VirtuNum para buscar otro numero"
                        )
                self._current_phone = phone
                print(f"VirtuNum: numero = {phone}")
                return phone
        except Exception:
            self._close()
            raise

    def wait_for_sms(self) -> str | None:
        """Espera el codigo SMS en la pagina ya abierta.

        Debe llamarse DESPUES de que el numero haya sido ingresado en la app del
        movil (para que la app dispare el SMS a VirtuNum).
        Por defecto deja el navegador abierto para poder revisar/reusar la orden.
        """
        if not self._page:
            raise VirtuNumError(
                "El navegador no esta abierto. Llama a open_and_get_phone primero."
            )
        try:
            target = f" para {self._current_phone}" if self._current_phone else ""
            print(f"VirtuNum: esperando SMS{target} (max {self.sms_timeout}s) ...")
            code = self._wait_sms(self._page)
            if code:
                print(f"VirtuNum: codigo recibido = {code}")
            else:
                print("VirtuNum: timeout, no llego el SMS")
            return code
        finally:
            if self.close_browser_after_sms:
                self._close()

    def _close(self) -> None:
        """Cierra el contexto del navegador y libera Playwright."""
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
        # Prefer stopping the Playwright instance returned by start()
        if self._p:
            try:
                # The SyncPlaywright object exposes a `stop()` method that delegates
                # to the context manager exit; call it when available.
                self._p.stop()
            except Exception as exc:
                print(f"VirtuNum: error al detener Playwright (self._p.stop): {exc}")

        # Best-effort cleanup of the context manager object kept in _pw_mgr.
        if self._pw_mgr:
            try:
                # Historically code called self._pw_mgr.stop(), but the
                # PlaywrightContextManager object doesn't implement `stop()`.
                # Only call it if present to avoid AttributeError.
                if hasattr(self._pw_mgr, "stop"):
                    self._pw_mgr.stop()
            except Exception as exc:
                print(f"VirtuNum: error al cerrar PlaywrightContextManager: {exc}")
            # Siempre intentar limpiar el asyncio loop, incluso si hay error
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                if loop and not loop.is_closed():
                    loop.close()
                asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass
            self._pw_mgr = None

        self._page = None
        self._p = None

    def get_number_and_sms(self) -> tuple[str, str | None]:
        """Compatibilidad: abre el navegador, obtiene el numero y espera el SMS."""
        phone = self.open_and_get_phone()
        code = self.wait_for_sms()
        return phone, code

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _ensure_logged_in(self, page) -> None:
        """Si la pagina muestra el boton de login, espera a que el usuario inicie sesion."""
        try:
            login_btn = page.locator("a[href*='/auth/login'], button:has-text('Login'), button:has-text('Sign in')")
            if login_btn.count() > 0:
                print("VirtuNum: inicia sesion en la ventana del navegador que se abrio...")
                print("  (las cookies se guardaran para la proxima vez)")
                # Esperar hasta que desaparezca el boton de login (max 3 min)
                page.wait_for_selector(
                    "text=Monedas, text=Coins, text=Credits",
                    timeout=180_000,
                )
                print("VirtuNum: sesion iniciada correctamente.")
                time.sleep(1)
        except Exception:
            pass  # Si no hay boton de login, ya esta logueado

    def _click_service(self, page) -> None:
        """Hace clic en el servicio en la lista de servicios.

        Usa el alt de la imagen para evitar colisiones de texto (ej: 'Any other'
        vs 'Any other call forwarding').
        """
        # Click directo via alt de imagen (evita colisiones de texto)
        try:
            page.locator(f"img[alt='{self.service}']").first.click(timeout=8_000)
            time.sleep(1.5)
            return
        except Exception:
            pass

        # Buscar en el campo de busqueda de servicios
        try:
            search = page.locator("input[placeholder*='Search services'], input[placeholder*='Buscar']")
            if search.count() > 0:
                search.first.fill(self.service)
                time.sleep(0.8)
                page.locator(f"img[alt='{self.service}']").first.click(timeout=5_000)
                time.sleep(1.5)
                return
        except Exception:
            pass

        raise VirtuNumError(f"No se encontro el servicio '{self.service}' en VirtuNum")

    def _select_country(self, page) -> None:
        """Busca el pais en la lista de paises y hace clic en Buy."""
        try:
            page.wait_for_selector(
                "input[placeholder*='Search countries'], input[placeholder*='countries']",
                timeout=12_000,
            )
        except Exception:
            raise VirtuNumError(
                "No aparecio el selector de pais en VirtuNum tras elegir el servicio"
            )

        country_input = page.locator(
            "input[placeholder*='Search countries'], input[placeholder*='countries']"
        ).first
        country_input.fill(self.country)
        time.sleep(1.2)

        print(f"VirtuNum: seleccionando pais '{self.country}' ...")
        # Clic en Buy del primer resultado filtrado
        buy_btn = page.locator("button:has-text('Buy')").first
        buy_btn.wait_for(state="visible", timeout=8_000)
        buy_btn.click(timeout=8_000)
        time.sleep(1.5)

    def _read_phone(self, page, timeout: int = 15) -> str | None:
        """Extrae el numero de telefono (+XXXXX) que aparece tras seleccionar servicio."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                visible_phone = page.evaluate(
                    """
                    () => {
                        const matches = [];
                        const phoneRegex = /\\+\\d{7,15}/g;
                        for (const el of document.querySelectorAll("body *")) {
                            const text = el.innerText || el.textContent || "";
                            const phones = text.match(phoneRegex);
                            if (!phones) continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (text.length > 200) continue;
                            for (const phone of phones) {
                                matches.push({ phone, top: rect.top, left: rect.left });
                            }
                        }
                        matches.sort((a, b) => a.top - b.top || a.left - b.left);
                        return matches.length ? matches[0].phone : "";
                    }
                    """
                )
                if visible_phone:
                    return visible_phone
            except Exception:
                pass
            content = page.content()
            match = re.search(r"\+\d{7,15}", content)
            if match:
                return match.group(0)
            time.sleep(1)
        return None

    def _wait_sms(self, page, poll: int = 5) -> str | None:
        """Recarga la pagina y busca el codigo SMS solo en la tarjeta del numero actual."""
        deadline = time.time() + self.sms_timeout
        start = time.time()
        # Strategy:
        # - For the first 2 minutes: poll frequently (default `poll`) without forcing a reload.
        # - After 2 minutes: increase polling interval and reload the page every iteration (refresh every minute).
        while time.time() < deadline:
            code = self._extract_code_for_current_phone(page)
            if code:
                return code

            elapsed = time.time() - start
            # If we've passed 2 minutes, switch to 60s refresh interval
            if elapsed >= 120:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15_000)
                    time.sleep(1.5)
                except Exception:
                    pass
                # wait a minute before next refresh (or until deadline)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(60, remaining))
                continue

            # First 2 minutes: short sleep rounds, do not force reloads to avoid rate limits
            time.sleep(poll)

        return None

    def _extract_code_for_current_phone(self, page) -> str | None:
        phone = self._current_phone
        if not phone:
            return None

        try:
            card_text = page.evaluate(
                """
                (phone) => {
                    const phoneDigits = String(phone).replace(/\\D/g, "");
                    const normalize = (value) => String(value || "").replace(/\\D/g, "");
                    const phoneMatches = (value) => String(value || "").match(/\\+\\d{7,15}/g) || [];
                    const seen = new Set();
                    const matches = [];

                    for (const el of document.querySelectorAll("body *")) {
                        const text = el.innerText || el.textContent || "";
                        if (!normalize(text).includes(phoneDigits)) continue;

                        let card = el;
                        for (let i = 0; i < 8 && card.parentElement; i++) {
                            const parentText = card.parentElement.innerText || card.parentElement.textContent || "";
                            if (!normalize(parentText).includes(phoneDigits)) break;
                            if (parentText.length > 1400) break;
                            if (phoneMatches(parentText).length > 1) break;
                            card = card.parentElement;
                        }

                        if (seen.has(card)) continue;
                        seen.add(card);
                        const cardText = card.innerText || card.textContent || "";
                        if (phoneMatches(cardText).length > 1) continue;
                        const rect = card.getBoundingClientRect();
                        matches.push({ text: cardText, top: rect.top, length: cardText.length });
                    }

                    matches.sort((a, b) => a.top - b.top || b.length - a.length);
                    return matches.length ? matches[0].text : "";
                }
                """,
                phone,
            )
        except Exception:
            return None

        if not card_text:
            return None
        return self._extract_code_from_card(card_text)

    @staticmethod
    def _extract_code_from_card(card_text: str) -> str | None:
        card_text = re.sub(r"\s+", " ", card_text)
        for pattern in (
            r"TRIBBU[^\d]{0,120}(\d{6})",
            r"(?:SMS|code|codigo|verification)[^\d]{0,120}(\d{6})",
            r"(\d{6})[^\d]{0,120}(?:ne le communique|communicate|compartas|nadie)",
        ):
            match = re.search(pattern, card_text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None
        """Extrae el OTP desde el texto de una sola tarjeta de VirtuNum."""
        card_text = re.sub(r"\s+", " ", card_text)
        patterns = [
            r"(?:SMS|code|codigo|c[oó]digo|verification)[^\d]{0,80}(\d{4,8})",
            r"(\d{4,8})[^\d]{0,80}(?:ne le communique|communicate|compartas|nadie)",
        ]
        for pattern in patterns:
            match = re.search(pattern, card_text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_code(html_content: str) -> str | None:
        """Extrae un codigo de 4-8 digitos de la pagina de VirtuNum."""
        # Busca el codigo en el bloque SMS: texto despues de "codigo" / "code" / "sms"
        patterns = [
            # VirtuNum muestra el codigo en un elemento cerca de "Código del SMS"
            r"[Cc][Óó]digo[^<]{0,80}?(\d{4,8})",
            r"SMS\s+[Cc]ode[^<]{0,80}?(\d{4,8})",
            r"[Cc]ode[^<]{0,60}?(\d{4,8})",
            # Codigo solo en elemento destacado (al menos 4 digitos solos)
            r">(\d{4,8})<",
        ]
        for pat in patterns:
            m = re.search(pat, html_content)
            if m:
                candidate = m.group(1)
                # Filtrar numeros de telefono (demasiado largos ya filtrados por {4,8})
                return candidate
        return None


# ---------------------------------------------------------------------------
# Interfaz compatible con el codigo existente en open_tribbu.py
# ---------------------------------------------------------------------------

class VirtuNumClient:
    """Envuelve VirtuNumBrowser con la misma interfaz que antes para
    no tener que cambiar open_tribbu.py."""

    def __init__(
        self,
        service: str = "Any other",
        country: str = "England",
        headless: bool = False,
        sms_timeout: int = 420,
        close_browser_after_sms: bool = False,
    ):
        self._browser = VirtuNumBrowser(
            service=service,
            country=country,
            headless=headless,
            sms_timeout=sms_timeout,
            close_browser_after_sms=close_browser_after_sms,
        )
        # Estado interno entre buy_activation y wait_sms_code
        self._pending_phone: str | None = None
        self._pending_code: str | None = None

    @classmethod
    def from_env(cls) -> "VirtuNumClient":
        service = os.environ.get("VIRTUNUM_PRODUCT", "Any other")
        country = os.environ.get("VIRTUNUM_COUNTRY", "England")
        headless = os.environ.get("VIRTUNUM_HEADLESS", "0").strip() in ("1", "true", "yes")
        sms_timeout = int(os.environ.get("VIRTUNUM_SMS_TIMEOUT", "420"))
        if os.environ.get("VIRTUNUM_CLOSE_BROWSER", "0").strip() in ("1", "true", "yes"):
            print("VirtuNum: VIRTUNUM_CLOSE_BROWSER ignorado; Chrome se deja abierto para conservar el OTP.")
        close_browser_after_sms = False
        return cls(
            service=service,
            country=country,
            headless=headless,
            sms_timeout=sms_timeout,
            close_browser_after_sms=close_browser_after_sms,
        )

    def buy_activation(
        self,
        country: str = "",
        product: str = "",
        operator: str = "",
        skip_phones: set[str] | None = None,
    ) -> ActivationOrder:
        """Abre el navegador, selecciona el servicio y devuelve el numero asignado.

        El navegador permanece abierto hasta que se llame a wait_sms_code(), lo que
        permite introducir el numero en la app del movil ANTES de esperar el SMS.
        """
        reuse_phone = os.environ.get("VIRTUNUM_REUSE_PHONE", "").strip()
        if reuse_phone:
            print(f"VirtuNum: reutilizando numero indicado por VIRTUNUM_REUSE_PHONE = {reuse_phone}")
            phone = self._browser.open_existing_phone(reuse_phone)
        else:
            phone = self._browser.open_and_get_phone(skip_phones=skip_phones)
        self._pending_phone = phone
        self._pending_code = None
        return ActivationOrder(order_id="browser", phone=phone, status="PENDING")

    def wait_sms_code(
        self,
        order_id: str = "",
        timeout: int = 0,
        poll: int = 5,
    ) -> tuple[str | None, str | None]:
        """Espera el codigo SMS en el navegador ya abierto y lo devuelve.

        Llamar DESPUES de haber introducido el numero en la app del movil para
        que la app dispare el SMS a VirtuNum.
        """
        code = self._browser.wait_for_sms()
        self._pending_code = code
        return code, None

    def finish_activation(self, order_id: str = "") -> None:
        """No-op: el navegador ya fue cerrado por buy_activation."""
        self._pending_code = None
        self._pending_phone = None
