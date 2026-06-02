from __future__ import annotations

import os
import shutil
from pathlib import Path


def _as_adb_executable(path_value: str | os.PathLike[str] | None) -> str | None:
    if not path_value:
        return None
    p = Path(path_value)
    if p.exists() and p.is_dir():
        p = p / "adb.exe"
    if p.exists() and p.is_file():
        return str(p)
    return None


def _find_adb() -> str:
    env = os.environ.get("ADB")
    env_adb = _as_adb_executable(env)
    if env_adb:
        return env_adb
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    default = _as_adb_executable("C:/adb/platform-tools")
    if default:
        return default
    raise FileNotFoundError("adb no encontrado. Define ADB o agrega adb al PATH.")


ADB = _find_adb()
SERIAL = os.environ.get("ANDROID_SERIAL", "R92XC0G615P").strip()

# Samsung Galaxy Tab A9+ en vertical: 6 columnas x 5 filas por pagina.
# Ajusta estos centros si tu launcher cambia espaciado.
X_CENTERS = [127, 317, 507, 696, 886, 1075]
Y_CENTERS = [318, 637, 956, 1275, 1594]

# Para volver a la primera pagina del escritorio.
SWIPE_TO_PREV_PAGE = (220, 960, 1080, 960, 250)
# Para avanzar a la siguiente pagina del escritorio.
SWIPE_TO_NEXT_PAGE = (1080, 960, 220, 960, 250)

HOME_SETTLE_SECONDS = 1.0
PAGE_SETTLE_SECONDS = 0.7
APP_LAUNCH_SETTLE_SECONDS = 2.5

COLS_PER_PAGE = 6
ROWS_PER_PAGE = 5
APPS_PER_PAGE = COLS_PER_PAGE * ROWS_PER_PAGE
MAX_TRIBBU = 100

# Regla fija de negocio: solo estos TRIBBU se registran como Driver.
DRIVER_TRIBBUS = {
    1, 6, 11, 16, 21, 26, 31, 36, 41, 46,
    51, 56, 61, 66, 71, 76, 81, 86, 91, 96,
}

# VirtuNum (Verify your phone number)
VIRTUNUM_COUNTRY = os.environ.get("VIRTUNUM_COUNTRY", "England").strip()
VIRTUNUM_PRODUCT = os.environ.get("VIRTUNUM_PRODUCT", "Any other").strip()
VIRTUNUM_OPERATOR = os.environ.get("VIRTUNUM_OPERATOR", "any").strip()
VIRTUNUM_COUNTRY_CODE = os.environ.get("VIRTUNUM_COUNTRY_CODE", "44").strip()
VIRTUNUM_SMS_TIMEOUT = int(os.environ.get("VIRTUNUM_SMS_TIMEOUT", "180"))
