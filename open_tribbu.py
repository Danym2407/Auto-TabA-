from __future__ import annotations

import argparse
import csv
import html
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import config
from tribbu_layout import build_page_table, page_count, role_for_tribbu, tribbu_to_slot
from virtunum_api import VirtuNumClient, VirtuNumError


ROOT = Path(__file__).resolve().parent

USED_VIRTUNUM_PHONES_PATH = ROOT / "used_virtunum_numbers.txt"
USED_VIRTUNUM_OTPS_PATH = ROOT / "used_virtunum_otps.txt"
USED_PERSONS_DB_PATH = ROOT / "used_persons.db"

# Instancia global de VirtuNumClient que se reutiliza entre llamadas
# para evitar crear múltiples loops asyncio de Playwright
_VIRTUNUM_CLIENT: VirtuNumClient | None = None


def get_virtunum_client() -> VirtuNumClient | None:
    """Obtiene o crea la instancia global del cliente VirtuNum.
    
    Se reutiliza la misma instancia entre llamadas para evitar conflictos
    con el loop asyncio de Playwright cuando se procesan múltiples apps.
    """
    global _VIRTUNUM_CLIENT
    if _VIRTUNUM_CLIENT is None:
        try:
            _VIRTUNUM_CLIENT = VirtuNumClient.from_env()
        except VirtuNumError:
            return None
    return _VIRTUNUM_CLIENT


def adb(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [config.ADB, "-s", config.SERIAL, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def shell(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return adb("shell", *args, timeout=timeout)


def screen_size() -> tuple[int, int]:
    result = shell("wm", "size", timeout=5)
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", result.stdout or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    return (1200, 1920)


def normalize_phone_number(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"+{digits}" if digits else phone.strip()


def load_used_virtunum_numbers() -> set[str]:
    if not USED_VIRTUNUM_PHONES_PATH.exists():
        return set()
    try:
        text = USED_VIRTUNUM_PHONES_PATH.read_text(encoding="utf-8")
    except Exception:
        return set()
    return {
        normalize_phone_number(line)
        for line in text.splitlines()
        if line.strip()
    }


def save_used_virtunum_number(phone: str) -> None:
    normalized = normalize_phone_number(phone)
    if not normalized:
        return
    used = load_used_virtunum_numbers()
    if normalized in used:
        return
    try:
        with USED_VIRTUNUM_PHONES_PATH.open("a", encoding="utf-8") as handle:
            handle.write(normalized + "\n")
    except Exception as exc:
        print(f"No pude guardar numero usado en {USED_VIRTUNUM_PHONES_PATH}: {exc}")
    else:
        print(f"Guardado numero usado: {normalized}")


def save_used_virtunum_otp(phone: str, otp: str) -> None:
    normalized_phone = normalize_phone_number(phone)
    otp_value = otp.strip()
    if not normalized_phone or not otp_value:
        return
    line = f"{normalized_phone} {otp_value}"
    try:
        with USED_VIRTUNUM_OTPS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:
        print(f"No pude guardar OTP usado en {USED_VIRTUNUM_OTPS_PATH}: {exc}")
    else:
        print(f"Guardado OTP usado: {line}")


def ui_dump_xml() -> str:
    shell("uiautomator", "dump", "/sdcard/open_tribbu.xml", timeout=20)
    result = shell("cat", "/sdcard/open_tribbu.xml", timeout=20)
    return result.stdout or ""


def _fold(value: str) -> str:
    value = html.unescape(value or "")
    value = value.lower().strip()
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return value


def _parse_nodes(xml: str) -> list[dict[str, str | int | bool]]:
    nodes: list[dict[str, str | int | bool]] = []
    for attrs in re.findall(r"<node\s+([^>]+?)\/?>", xml):
        text_m = re.search(r'text="([^"]*)"', attrs)
        desc_m = re.search(r'content-desc="([^"]*)"', attrs)
        class_m = re.search(r'class="([^"]*)"', attrs)
        b_m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', attrs)
        if not b_m:
            continue
        x1, y1, x2, y2 = map(int, b_m.groups())
        nodes.append(
            {
                "text": text_m.group(1).strip() if text_m else "",
                "desc": desc_m.group(1).strip() if desc_m else "",
                "class": class_m.group(1).strip() if class_m else "",
                "clickable": 'clickable="true"' in attrs,
                "checked": 'checked="true"' in attrs,
                "x": (x1 + x2) // 2,
                "y": (y1 + y2) // 2,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )
    return nodes


def find_icon_center(label: str) -> tuple[int, int] | None:
    xml = ui_dump_xml()
    for node in _parse_nodes(xml):
        text_val = str(node["text"]).strip()
        desc_val = str(node["desc"]).strip()
        if label not in (text_val, desc_val):
            continue
        x = int(node["x"])
        y = int(node["y"])
        # La etiqueta del launcher queda debajo del icono; tocar un poco arriba abre mejor la app.
        if "TextView" in str(node.get("class", "")):
            y = max(0, y - 75)
        return (x, y)
    return None


def tap_node_text(
    candidates: list[str],
    *,
    prefer_clickable: bool = True,
    min_y: int | None = None,
    max_y: int | None = None,
    settle: float = 0.9,
) -> bool:
    xml = ui_dump_xml()
    if not xml:
        return False
    wanted = {_fold(x) for x in candidates}

    first_fallback: tuple[int, int] | None = None
    for node in _parse_nodes(xml):
        text_val = _fold(str(node["text"]))
        desc_val = _fold(str(node["desc"]))
        if text_val not in wanted and desc_val not in wanted:
            continue

        x = int(node["x"])
        y = int(node["y"])
        if min_y is not None and y < min_y:
            continue
        if max_y is not None and y > max_y:
            continue

        if bool(node["clickable"]) or not prefer_clickable:
            print(f"Tap por texto {candidates} @ ({x}, {y})")
            tap(x, y, settle=settle)
            return True

        if first_fallback is None:
            first_fallback = (x, y)

    if first_fallback and not prefer_clickable:
        print(f"Tap por fallback de texto {candidates} @ {first_fallback}")
        tap(first_fallback[0], first_fallback[1], settle=settle)
        return True
    return False


def tap_node_contains(
    candidates: list[str],
    *,
    min_y: int | None = None,
    max_y: int | None = None,
    settle: float = 0.9,
) -> bool:
    xml = ui_dump_xml()
    if not xml:
        return False
    wanted = [_fold(x) for x in candidates]

    for node in _parse_nodes(xml):
        text_val = _fold(str(node["text"]))
        desc_val = _fold(str(node["desc"]))
        full_val = f"{text_val} {desc_val}".strip()
        if not any(w in full_val for w in wanted):
            continue

        x = int(node["x"])
        y = int(node["y"])
        if min_y is not None and y < min_y:
            continue
        if max_y is not None and y > max_y:
            continue

        print(f"Tap por texto parcial {candidates} @ ({x}, {y})")
        tap(x, y, settle=settle)
        return True

    return False


def press_keyevent(keycode: str, settle: float = 0.5) -> None:
    shell("input", "keyevent", keycode)
    time.sleep(settle)


def keyboard_is_visible() -> bool:
    result = shell("dumpsys", "input_method", timeout=5)
    output = result.stdout or ""
    visible_patterns = (
        r"mInputShown=true",
        r"inputShown=true",
        r"mIsInputViewShown=true",
        r"mImeWindowVis=0x[0-9a-fA-F]*2",
    )
    return any(re.search(pattern, output) for pattern in visible_patterns)


def dismiss_keyboard_if_visible(settle: float = 0.3) -> None:
    if keyboard_is_visible():
        print("Teclado visible; cerrandolo antes de continuar.")
        press_keyevent("KEYCODE_BACK", settle=settle)


def tap_not_sure_for_trips(settle: float = 1.1) -> bool:
    """Tap 'Not sure' only from the second question block: 'For my trips'."""
    xml = ui_dump_xml()
    if not xml:
        return False

    nodes = _parse_nodes(xml)
    trips_anchor_y: int | None = None
    for node in nodes:
        text_val = _fold(str(node["text"]))
        desc_val = _fold(str(node["desc"]))
        full_val = f"{text_val} {desc_val}".strip()
        if "for my trips" in full_val:
            trips_anchor_y = int(node["y"])
            break

    # If we can locate "For my trips", only accept options below that header.
    min_y = (trips_anchor_y + 40) if trips_anchor_y is not None else 1200

    return tap_node_text(
        ["not sure", "no estoy seguro", "no estoy segura"],
        prefer_clickable=False,
        min_y=min_y,
        settle=settle,
    )


def type_text_adb(value: str, settle: float = 0.2) -> None:
    safe = value.replace(" ", "%s")
    shell("input", "text", safe)
    time.sleep(settle)


def clean_adb_text(value: str) -> str:
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
        "Á": "A",
        "É": "E",
        "Í": "I",
        "Ó": "O",
        "Ú": "U",
        "Ü": "U",
        "Ñ": "N",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return re.sub(r"[^A-Za-z0-9 .'-]+", "", value).strip()


def clear_focused_text(max_chars: int = 24, settle: float = 0.1) -> None:
    press_keyevent("KEYCODE_MOVE_END", settle=0.05)
    for _ in range(max_chars):
        shell("input", "keyevent", "KEYCODE_DEL", timeout=5)
        time.sleep(0.02)
    time.sleep(settle)


def type_digits_keyevents(code: str, settle: float = 0.12) -> None:
    for ch in code:
        if not ch.isdigit():
            continue
        keycode = 7 + int(ch)
        shell("input", "keyevent", str(keycode))
        time.sleep(settle)


def tap_otp_entry_field(settle: float = 0.4) -> None:
    width, height = screen_size()
    x = max(70, int(width * 0.08))
    y = int(height * 0.44)
    print(f"Tocando primera casilla OTP @ ({x}, {y})")
    tap(x, y, settle=settle)


def init_used_persons_db() -> None:
    conn = sqlite3.connect(USED_PERSONS_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assigned_persons (
                tribbu_number INTEGER PRIMARY KEY,
                row_index INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                apellidos TEXT NOT NULL,
                dni TEXT NOT NULL,
                assigned_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def find_person_list_csv() -> Path:
    candidates = [
        ROOT / "lista_personas.csv",
        ROOT.parent / "lista_personas.csv",
        Path.cwd() / "lista_personas.csv",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise FileNotFoundError(
        "No se encontro lista_personas.csv. Coloca el archivo en tab-a9-tribbu/ o en la carpeta padre del proyecto."
    )


def load_person_list() -> list[dict[str, str]]:
    csv_path = find_person_list_csv()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows


def get_assigned_person_for_tribbu(tribbu_number: int) -> dict[str, str] | None:
    init_used_persons_db()
    conn = sqlite3.connect(USED_PERSONS_DB_PATH)
    try:
        cursor = conn.execute(
            "SELECT row_index, nombre, apellidos, dni FROM assigned_persons WHERE tribbu_number = ?",
            (tribbu_number,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "row_index": str(row[0]),
            "nombre": row[1],
            "apellidos": row[2],
            "dni": row[3],
        }
    finally:
        conn.close()


def get_assigned_row_indices() -> set[int]:
    init_used_persons_db()
    conn = sqlite3.connect(USED_PERSONS_DB_PATH)
    try:
        cursor = conn.execute("SELECT row_index FROM assigned_persons")
        return {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()


def assign_person_to_tribbu(tribbu_number: int, row_index: int, nombre: str, apellidos: str, dni: str) -> None:
    init_used_persons_db()
    conn = sqlite3.connect(USED_PERSONS_DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO assigned_persons (tribbu_number, row_index, nombre, apellidos, dni, assigned_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                tribbu_number,
                row_index,
                nombre,
                apellidos,
                dni,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_person_for_tribbu(tribbu_number: int) -> tuple[str, str, str]:
    assigned = get_assigned_person_for_tribbu(tribbu_number)
    if assigned is not None:
        return (
            clean_adb_text(assigned["nombre"]),
            clean_adb_text(assigned["apellidos"]),
            clean_adb_text(assigned["dni"]),
        )

    rows = load_person_list()
    used_rows = get_assigned_row_indices()
    for idx, row in enumerate(rows, start=1):
        if idx in used_rows:
            continue
        name = clean_adb_text(row.get("nombre", ""))
        surname = clean_adb_text(row.get("apellidos", ""))
        dni = clean_adb_text(row.get("dni", ""))
        if not name or not surname or not dni:
            continue
        assign_person_to_tribbu(tribbu_number, idx, name, surname, dni)
        print(f"Asignando persona TRIBBU {tribbu_number:03d} -> fila {idx}")
        return name, surname, dni

    raise RuntimeError(
        "No hay filas disponibles sin usar en lista_personas.csv. Actualiza el CSV o limpia used_persons.db."
    )


def wait_for_personal_data_screen(timeout: float = 25.0, poll: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = ui_dump_xml()
        nodes = _parse_nodes(xml)
        edit_count = sum(1 for node in nodes if "EditText" in str(node.get("class", "")))
        screen = current_screen_text()
        if edit_count >= 2 or any(token in screen for token in ("name", "nombre", "surname", "apellido")):
            return True
        time.sleep(poll)
    return False


def get_personal_edit_fields() -> list[dict[str, str | int | bool]]:
    xml = ui_dump_xml()
    fields = [
        node
        for node in _parse_nodes(xml)
        if "EditText" in str(node.get("class", ""))
    ]
    return sorted(fields, key=lambda node: int(node["y"]))


def _normalize_field_text(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(text or "")).lower()


def _wait_for_field_text(expected: str, field_index: int = 0, timeout: float = 4.0) -> bool:
    normalized_expected = _normalize_field_text(expected)
    if not normalized_expected:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        fields = get_personal_edit_fields()
        if field_index < len(fields):
            actual = _normalize_field_text(fields[field_index].get("text", ""))
            if actual == normalized_expected:
                return True
            if normalized_expected.startswith(actual) and len(actual) >= min(3, len(normalized_expected)):
                return True
        time.sleep(0.25)
    return False


def tap_first_personal_field(settle: float = 0.4) -> None:
    fields = get_personal_edit_fields()
    if fields:
        tap(int(fields[0]["x"]), int(fields[0]["y"]), settle=settle)
        return
    width, height = screen_size()
    tap(width // 2, int(height * 0.48), settle=settle)


def tap_personal_field(index: int, settle: float = 0.4) -> None:
    fields = get_personal_edit_fields()
    if not fields:
        width, height = screen_size()
        tap(width // 2, int(height * 0.48), settle=settle)
        return
    field = fields[index] if index < len(fields) else fields[-1]
    tap(int(field["x"]), int(field["y"]), settle=settle)


def wait_for_dni_screen(timeout: float = 25.0, poll: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        screen = current_screen_text()
        if any(token in screen for token in ("dni", "document", "identification", "identificacion", "passport", "passport number", "rut")):
            return True
        xml = ui_dump_xml()
        nodes = _parse_nodes(xml)
        edit_count = sum(1 for node in nodes if "EditText" in str(node.get("class", "")))
        if edit_count >= 1:
            return True
        time.sleep(poll)
    return False


def tap_dni_input_field(settle: float = 0.4) -> None:
    fields = get_personal_edit_fields()
    if not fields:
        width, height = screen_size()
        tap(width // 2, int(height * 0.48), settle=settle)
        return

    # El campo real de DNI suele ser el último campo de entrada o el que está vacío.
    empty_fields = [field for field in fields if not field.get("text")]
    if empty_fields:
        field = empty_fields[-1]
    else:
        field = max(fields, key=lambda node: int(node["x2"]) - int(node["x1"]))
    tap(int(field["x"]), int(field["y"]), settle=settle)


def fill_dni_field(dni: str) -> bool:
    print(f"Escribiendo DNI: {dni}")
    if not wait_for_dni_screen(timeout=25):
        print("No se detecto pantalla de DNI; igualmente intento escribir en el campo disponible.")

    for attempt in range(1, 2):
        tap_dni_input_field(settle=0.4)
        clear_focused_text(max_chars=30, settle=0.1)
        type_text_adb(dni, settle=0.5)

        if _wait_for_field_text(dni, field_index=0, timeout=4.0):
            print("DNI confirmado en el campo de texto.")
            return True

        print(f"DNI no confirmado tras el intento {attempt}; reintentando...")
        dismiss_keyboard_if_visible(settle=0.3)
        time.sleep(0.6)

    time.sleep(0.5)
    print("No se pudo verificar que el DNI se haya escrito correctamente.")
    return False


def fill_personal_name_fields(name: str, surname: str) -> None:
    print(f"Datos personales: nombre='{name}' apellidos='{surname}'")
    if not wait_for_personal_data_screen(timeout=25):
        print("No se detecto pantalla de datos personales; no escribo nombre/apellidos.")
        return

    fields = get_personal_edit_fields()
    if not fields:
        print("No se detectaron campos de texto para datos personales.")
        return

    # Campo nombre
    tap_personal_field(0, settle=0.4)
    clear_focused_text(max_chars=40, settle=0.1)
    type_text_adb(name, settle=0.5)

    # Si hay un segundo campo, completar apellido en él.
    if len(fields) >= 2:
        tap_personal_field(1, settle=0.4)
        clear_focused_text(max_chars=50, settle=0.1)
        type_text_adb(surname, settle=0.5)
        print("Datos personales escritos: nombre y apellido")
    else:
        print("Solo se detecto un campo de texto para datos personales; no se escribe apellido.")

    dismiss_keyboard_if_visible(settle=0.5)
    time.sleep(0.5)
    tap_primary_action_button(settle=0.8)
    # Return control to caller to handle DNI on the next screen.


def normalize_phone_for_country(phone: str, country_code: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    cc = "".join(ch for ch in country_code if ch.isdigit())
    if cc and digits.startswith(cc):
        return digits[len(cc):]
    return digits


COUNTRY_DIAL_CODES = {
    "england": "44",
    "united kingdom": "44",
    "uk": "44",
    "great britain": "44",
    "netherlands": "31",
    "mexico": "52",
    "usa": "1",
    "united states": "1",
}


def split_phone_for_tablet(phone: str) -> tuple[str, str]:
    digits = "".join(ch for ch in phone if ch.isdigit())
    country_hint = _fold(config.VIRTUNUM_COUNTRY)
    country_code = COUNTRY_DIAL_CODES.get(country_hint)
    if not country_code:
        configured = "".join(ch for ch in config.VIRTUNUM_COUNTRY_CODE if ch.isdigit())
        country_code = configured or "44"

    if digits.startswith(country_code):
        local_phone = digits[len(country_code):]
    else:
        local_phone = normalize_phone_for_country(phone, country_code)

    return f"+{country_code}", local_phone


def editable_nodes() -> list[dict[str, str | int | bool]]:
    nodes = _parse_nodes(ui_dump_xml())
    editables = []
    for node in nodes:
        class_val = str(node.get("class", ""))
        text_val = _fold(str(node.get("text", "")))
        desc_val = _fold(str(node.get("desc", "")))
        full_val = f"{text_val} {desc_val}".strip()
        if "EditText" in class_val or any(w in full_val for w in ("phone", "numero", "codigo", "code")):
            if int(node["y"]) > 250:
                editables.append(node)
    return sorted(editables, key=lambda n: (int(n["y"]), int(n["x"])))


def tap_phone_field_pair() -> tuple[tuple[int, int], tuple[int, int]]:
    nodes = editable_nodes()
    phoneish = [n for n in nodes if int(n["y"]) < 1200]
    if len(phoneish) >= 2:
        first_row_y = int(phoneish[0]["y"])
        same_row = [n for n in phoneish if abs(int(n["y"]) - first_row_y) < 90]
        if len(same_row) >= 2:
            same_row = sorted(same_row, key=lambda n: int(n["x"]))
            return (
                (int(same_row[0]["x"]), int(same_row[0]["y"])),
                (int(same_row[-1]["x"]), int(same_row[-1]["y"])),
            )
        return (
            (int(phoneish[0]["x"]), int(phoneish[0]["y"])),
            (int(phoneish[1]["x"]), int(phoneish[1]["y"])),
        )

    # Fallback para Tab A9+ vertical: selector de pais a la izquierda y telefono a la derecha.
    return (100, 519), (675, 519)


def current_screen_text() -> str:
    values = []
    for node in _parse_nodes(ui_dump_xml()):
        values.append(str(node.get("text", "")))
        values.append(str(node.get("desc", "")))
    return _fold(" ".join(values))


def selected_country_code_text() -> str:
    values = []
    for node in _parse_nodes(ui_dump_xml()):
        x = int(node["x"])
        y = int(node["y"])
        if 20 <= x <= 190 and 430 <= y <= 610:
            values.append(str(node.get("text", "")))
            values.append(str(node.get("desc", "")))
    return _fold(" ".join(values))


def validate_selected_country_code(country_code: str) -> bool:
    expected_digits = country_code.replace("+", "")
    selected = selected_country_code_text()
    screen = current_screen_text()
    selected_digits = "".join(ch for ch in selected if ch.isdigit())

    if country_code in selected or selected_digits == expected_digits:
        return True

    if "+52" in selected or selected_digits == "52":
        print(f"TRIBBU country code validation failed: selector is still Mexico (+52), expected {country_code}")
        return False

    # Fallback: some Compose screens expose the selected prefix only in the full tree.
    if country_code in screen and "+52" not in selected:
        return True

    safe_selected = selected.encode("ascii", errors="replace").decode("ascii")
    print(f"TRIBBU country code validation failed: selected='{safe_selected}', expected {country_code}")
    return False


def select_country_code(country_code: str) -> None:
    if not is_phone_verification_screen() and not is_country_picker_open():
        raise RuntimeError("TRIBBU no esta en la pantalla de telefono; no puedo elegir codigo de pais.")

    if not is_country_picker_open():
        tap(80, 519, settle=1.0)

    for attempt in range(8):
        nodes = _parse_nodes(ui_dump_xml())
        for node in nodes:
            text_val = str(node.get("text", "")).strip()
            x = int(node["x"])
            y = int(node["y"])
            if text_val == country_code and 35 <= x <= 210 and 200 <= y <= 1750:
                print(f"Seleccionando item exacto {country_code} en picker @ ({x}, {y})")
                tap(x, y, settle=0.9)
                return

        # En esta pantalla el picker abre con +44 visible casi al final del panel.
        # UIAutomator a veces tarda en exponer ese TextView; este tap evita escribir
        # "44" dentro del campo del telefono.
        if country_code == "+44" and attempt == 0 and is_country_picker_open():
            print("Seleccionando +44 por coordenada conocida del picker")
            tap(136, 1620, settle=0.9)
            return

        if not is_country_picker_open():
            tap(80, 519, settle=0.8)
            continue

        swipe(120, 1650, 120, 1350, 250, settle=0.5)

    raise RuntimeError(f"No se encontro {country_code} en el selector de pais de TRIBBU.")


def enter_phone_with_country_code(country_code: str, local_phone: str) -> None:
    _, phone_pos = tap_phone_field_pair()

    print(f"Seleccionando codigo de pais en TRIBBU: {country_code}")
    select_country_code(country_code)
    if not validate_selected_country_code(country_code):
        raise RuntimeError(f"No se pudo seleccionar el codigo de pais {country_code} en TRIBBU.")

    print(f"Escribiendo numero en TRIBBU: {local_phone}")
    tap(phone_pos[0], phone_pos[1], settle=0.4)
    clear_focused_text(max_chars=32)
    type_text_adb(local_phone, settle=0.6)


def accept_terms_if_present() -> None:
    screen = current_screen_text()
    if "terms" not in screen and "terminos" not in screen and "privacy" not in screen:
        return
    for node in _parse_nodes(ui_dump_xml()):
        x = int(node["x"])
        y = int(node["y"])
        if 180 <= x <= 320 and 1050 <= y <= 1220 and bool(node.get("checked")):
            return

    for node in _parse_nodes(ui_dump_xml()):
        text_val = _fold(str(node.get("text", "")))
        desc_val = _fold(str(node.get("desc", "")))
        full_val = f"{text_val} {desc_val}".strip()
        if "i have read" in full_val or "terms and conditions" in full_val:
            target_x = max(40, int(node.get("x1", 290)) - 45)
            target_y = int(node["y"])
            print(f"Marcando terminos en TRIBBU @ ({target_x}, {target_y})")
            tap(target_x, target_y, settle=0.5)
            return

    print("Marcando terminos en TRIBBU por coordenada fallback")
    tap(245, 1138, settle=0.5)


def tap_primary_action_button(settle: float = 0.8) -> bool:
    candidates = ["continue", "continuar", "next", "siguiente", "verify", "verificar", "activate", "activar"]
    if tap_node_text(
        candidates,
        prefer_clickable=False,
        min_y=1400,
        settle=settle,
    ):
        return True
    if tap_node_contains(
        candidates,
        min_y=1400,
        settle=settle,
    ):
        return True
    width, height = screen_size()
    fallback_x = width // 2
    fallback_y = min(height - 140, 1789)
    print(f"Boton principal no detectado por texto; fallback abajo @ ({fallback_x}, {fallback_y})")
    tap(fallback_x, fallback_y, settle=settle)
    return True


def tap_activate_button(settle: float = 0.8) -> bool:
    candidates = ["activate", "activar"]
    if tap_node_text(
        candidates,
        prefer_clickable=False,
        min_y=1400,
        settle=settle,
    ):
        return True
    if tap_node_contains(
        candidates,
        min_y=1400,
        settle=settle,
    ):
        return True

    if keyboard_is_visible():
        print("Teclado visible; intentando bajar teclado antes de pulsar Activate")
        dismiss_keyboard_if_visible(settle=0.2)
        time.sleep(0.4)
        fallback_coords = [(650, 1151), (665, 1720)]
    else:
        fallback_coords = [(665, 1720), (650, 1151)]

    print("Boton Activate no detectado por texto; intentando coordenadas fijas en dos intentos")
    for idx, (fallback_x, fallback_y) in enumerate(fallback_coords, start=1):
        print(f"Intento {idx}: Tap Activate en coordenadas @ ({fallback_x}, {fallback_y})")
        tap(fallback_x, fallback_y, settle=settle)
        time.sleep(0.4)

    return True


def wait_for_otp_screen(timeout: float = 18.0) -> bool:
    deadline = time.time() + timeout
    last_screen = ""
    while time.time() < deadline:
        screen = current_screen_text()
        last_screen = screen
        if any(token in screen for token in ("verification code", "enter code", "otp", "codigo")):
            return True
        if is_launcher_or_app_closed(screen):
            print("TRIBBU salio de la app despues de Next; no espero OTP para no perder el numero.")
            return False
        time.sleep(1)
    if last_screen:
        print(f"No aparecio pantalla OTP. Pantalla actual: {last_screen[:160]}")
    return False


def is_phone_verification_screen() -> bool:
    screen = current_screen_text()
    return any(
        token in screen
        for token in (
            "verify your phone number",
            "we'll send you an sms",
            "phone number",
            "terms and conditions",
            "privacy policy",
        )
    )


def is_launcher_or_app_closed(screen: str | None = None) -> bool:
    screen = screen if screen is not None else current_screen_text()
    launcher_tokens = (
        "finder",
        "search",
        "settings",
        "play store",
        "galaxy store",
        "tribbu 001",
        "tribbu 002",
    )
    return any(token in screen for token in launcher_tokens) and not is_phone_verification_screen()


def is_country_picker_open() -> bool:
    screen = current_screen_text()
    country_codes = ("+43", "+32", "+55", "+56", "+52", "+44")
    return sum(1 for code in country_codes if code in screen) >= 3


def wait_for_any_text(candidates: list[str], timeout: float = 18.0, poll: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tap_node_contains(candidates, settle=0.1):
            return True
        time.sleep(poll)
    return False


def verify_phone_with_virtunum(tribbu_number: int) -> bool:
    client = get_virtunum_client()
    if client is None:
        print(f"VirtuNum no disponible")
        return False

    used_numbers = load_used_virtunum_numbers()
    try:
        # buy_activation abre el navegador, selecciona el servicio y espera el SMS
        order = client.buy_activation(skip_phones=used_numbers)
    except VirtuNumError as exc:
        print(f"Error abriendo VirtuNum en el navegador: {exc}")
        return False

    country_code, local_phone = split_phone_for_tablet(order.phone)
    print(f"VirtuNum phone: {order.phone} -> country_code={country_code}, local={local_phone}")

    # Escribir el numero en la app del movil
    if not is_phone_verification_screen() and not is_country_picker_open():
        print("TRIBBU no esta en la pantalla de telefono despues de comprar el numero.")
        return False
    if not is_country_picker_open():
        tap_node_contains(["verify your phone number", "numero", "phone"], settle=0.3)
    enter_phone_with_country_code(country_code, local_phone)
    dismiss_keyboard_if_visible(settle=0.5)
    accept_terms_if_present()
    tap_primary_action_button(settle=0.8)
    time.sleep(1.0)
    if not wait_for_otp_screen(timeout=20):
        print("TRIBBU sigue en la pantalla de telefono; no escribo OTP para no pegarlo al numero.")
        return False

    # Esperar el SMS en VirtuNum (el numero ya fue enviado a TRIBBU, que disparara el SMS)
    print("Esperando 14 segundos para que llegue el OTP mas reciente...")
    time.sleep(14)
    code, _ = client.wait_sms_code()
    if not code:
        print("No llego OTP de VirtuNum dentro del timeout")
        save_used_virtunum_number(order.phone)
        return False

    print(f"OTP recibido: {code}")
    tap_otp_entry_field(settle=0.4)
    print(f"Escribiendo OTP de {len(code)} digitos")
    type_digits_keyevents(code, settle=0.22)
    tap_primary_action_button(settle=0.8)
    time.sleep(4)
    save_used_virtunum_number(order.phone)
    save_used_virtunum_otp(order.phone, code)
    name, surname, dni = load_person_for_tribbu(tribbu_number)
    fill_personal_name_fields(name, surname)
    # Wait a short period for the UI to transition to the DNI screen,
    # then write the DNI to avoid overwriting el nombre.
    time.sleep(7.0)
    if not fill_dni_field(dni):
        print("Error: no se pudo escribir el DNI correctamente.")
        return False
    dismiss_keyboard_if_visible(settle=0.4)
    tap_activate_button(settle=0.8)
    time.sleep(2.0)
    go_home()
    return True


def tap_allow_by_anchor(settle: float = 0.9) -> bool:
    xml = ui_dump_xml()
    if not xml:
        return False
    for node in _parse_nodes(xml):
        text_val = _fold(str(node["text"]))
        desc_val = _fold(str(node["desc"]))
        if "no permitir" not in (text_val, desc_val):
            continue
        x = int(node["x"])
        y = int(node["y"])
        # En Android permissions, "Permitir" suele estar arriba de "No permitir".
        target_y = max(0, y - 150)
        print(f"Tap heuristico Permitir desde ancla 'No permitir' @ ({x}, {target_y})")
        tap(x, target_y, settle=settle)
        return True
    return False


def dismiss_startup_popups(rounds: int = 3) -> None:
    accept_texts = ["aceptar", "accept", "ok"]
    allow_texts = ["permitir", "allow", "while using the app", "mientras se usa la app"]

    for _ in range(rounds):
        tapped_any = False
        tapped_any |= tap_node_text(accept_texts, prefer_clickable=False, settle=0.9)
        tapped_any |= tap_node_text(allow_texts, prefer_clickable=False, settle=0.9)
        if not tapped_any:
            tapped_any |= tap_allow_by_anchor(settle=0.9)
        if not tapped_any:
            return
        time.sleep(0.5)


def select_role(role: str, attempts: int = 3) -> bool:
    if role not in ("driver", "passenger"):
        raise ValueError(f"Rol invalido: {role}")

    role_texts = ["driver", "conductor"] if role == "driver" else ["passenger", "passanger", "pasajero"]
    role_screen_texts = ["i am going to be", "voy a ser"]
    trip_screen_texts = [
        "for my trips",
        "para mis viajes",
        "i already have companions",
        
        "im looking for companions",
        "i'm looking for companions",
    ]
    continue_texts = ["continue", "continuar", "next", "siguiente", "enter"]

    for attempt in range(1, attempts + 1):
        print(f"Seleccionando rol={role} (intento {attempt}/{attempts})")
        dismiss_startup_popups(rounds=2)

        # Situa la UI en la primera decision: "I am going to be".
        tap_node_contains(role_screen_texts, max_y=1400, settle=0.3)

        tapped_role = tap_node_text(role_texts, prefer_clickable=False, settle=1.1)
        dismiss_startup_popups(rounds=2)

        if tapped_role:
            # Ir a la segunda decision: "For my trips".
            tap_node_contains(trip_screen_texts, min_y=1200, settle=0.4)
            press_keyevent("61", settle=0.6)  # KEYCODE_TAB

            tapped_not_sure = tap_not_sure_for_trips(settle=1.1)
            if not tapped_not_sure:
                press_keyevent("61", settle=0.6)  # un TAB extra si no encontro opcion
                tapped_not_sure = tap_not_sure_for_trips(settle=1.1)

            if tapped_not_sure:
                tap_node_text(continue_texts, prefer_clickable=False, settle=1.1)
                dismiss_startup_popups(rounds=2)
                return True

            print("No se detecto 'Not sure' en esta vuelta; reintentando onboarding...")

        time.sleep(1.0)

    return False


def start_registration(tribbu_number: int, verify_phone: bool = False) -> None:
    open_tribbu(tribbu_number)
    role = role_for_tribbu(tribbu_number)
    print(f"Iniciando registro para TRIBBU {tribbu_number:03d} -> {role}")
    dismiss_startup_popups(rounds=3)
    if verify_phone and is_country_picker_open():
        print("Selector de pais abierto; cerrandolo antes de continuar.")
        press_keyevent("KEYCODE_BACK", settle=0.6)
    already_on_phone = verify_phone and is_phone_verification_screen()
    if already_on_phone:
        print(f"TRIBBU {tribbu_number:03d} ya esta en Verify your phone number; salto seleccion de rol.")
    else:
        selected = select_role(role, attempts=4)
        if not selected:
            print(f"No se pudo confirmar seleccion de rol para TRIBBU {tribbu_number:03d}")
            return
        print(f"Rol aplicado para TRIBBU {tribbu_number:03d}: {role}")
    if verify_phone:
        ok = verify_phone_with_virtunum(tribbu_number)
        if not ok:
            print(f"Fallo Verify your phone number en TRIBBU {tribbu_number:03d}")
            tap_activate_button(settle=0.8)
            return
        print(f"Verify your phone number completado en TRIBBU {tribbu_number:03d}")


def tap(x: int, y: int, settle: float = 0.4) -> None:
    shell("input", "tap", str(x), str(y))
    time.sleep(settle)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int, settle: float) -> None:
    shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))
    time.sleep(settle)


def go_home() -> None:
    shell("input", "keyevent", "KEYCODE_HOME")
    time.sleep(config.HOME_SETTLE_SECONDS)
    # En One UI, un segundo HOME suele llevar a la pagina principal del escritorio.
    shell("input", "keyevent", "KEYCODE_HOME")
    time.sleep(config.HOME_SETTLE_SECONDS)


def go_to_first_page() -> None:
    # Repite varios swipes hacia la pagina anterior hasta asegurarnos de caer en la primera.
    for _ in range(page_count() + 1):
        swipe(*config.SWIPE_TO_PREV_PAGE, settle=config.PAGE_SETTLE_SECONDS)


def go_to_page(page_index: int) -> None:
    go_home()
    # Si buscamos la pagina 1, evitamos swipes innecesarios que en Samsung
    # pueden generar rebotes o quedarse "en bucle" entre paginas.
    if page_index == 0:
        return
    go_to_first_page()
    for _ in range(page_index):
        swipe(*config.SWIPE_TO_NEXT_PAGE, settle=config.PAGE_SETTLE_SECONDS)


def open_tribbu(tribbu_number: int) -> None:
    slot = tribbu_to_slot(tribbu_number)
    label = f"TRIBBU {slot.tribbu_number:03d}"
    role = role_for_tribbu(slot.tribbu_number)
    print(
        f"Abriendo TRIBBU {slot.tribbu_number:03d} | "
        f"role={role} | "
        f"pagina={slot.page_index + 1} fila={slot.row_index + 1} columna={slot.col_index + 1} "
        f"tap=({slot.x},{slot.y})"
    )
    go_to_page(slot.page_index)

    center = find_icon_center(label)
    if center is not None:
        print(f"Icono detectado por UI: {label} @ {center}")
        tap(center[0], center[1], settle=config.APP_LAUNCH_SETTLE_SECONDS)
        return

    print(f"No se detecto {label} en UI; usando fallback por grid.")
    tap(slot.x, slot.y, settle=config.APP_LAUNCH_SETTLE_SECONDS)


def print_slot(tribbu_number: int) -> None:
    slot = tribbu_to_slot(tribbu_number)
    print(f"TRIBBU {slot.tribbu_number:03d}")
    print(f"  role   : {role_for_tribbu(slot.tribbu_number)}")
    print(f"  pagina : {slot.page_index + 1}")
    print(f"  fila   : {slot.row_index + 1}")
    print(f"  columna: {slot.col_index + 1}")
    print(f"  tap    : ({slot.x}, {slot.y})")


def print_role(tribbu_number: int) -> None:
    print(f"TRIBBU {tribbu_number:03d} -> {role_for_tribbu(tribbu_number)}")


def print_table(page_index: int) -> None:
    table = build_page_table(page_index)
    print(f"Pagina {page_index + 1}")
    for row in table:
        print("  ".join("---" if n is None else f"{n:03d}" for n in row))


def column_numbers(start_tribbu: int) -> list[int]:
    if not 1 <= start_tribbu <= config.MAX_TRIBBU:
        raise ValueError(f"TRIBBU fuera de rango: {start_tribbu}. Usa 1..{config.MAX_TRIBBU}.")
    # En tu layout, bajar por una columna suma 1 hasta completar las 5 filas visibles,
    # y luego se repite el mismo patron en la pagina siguiente sumando 30.
    slot = tribbu_to_slot(start_tribbu)
    if slot.row_index != 0:
        raise ValueError(
            f"TRIBBU {start_tribbu:03d} no es la cabecera de una columna. "
            "Usa por ejemplo 001, 006, 011, 016, 021 o 026."
        )

    numbers: list[int] = []
    current = start_tribbu
    while current <= config.MAX_TRIBBU:
        for offset in range(config.ROWS_PER_PAGE):
            value = current + offset
            if value <= config.MAX_TRIBBU:
                numbers.append(value)
        current += config.APPS_PER_PAGE
    return numbers


def print_column(start_tribbu: int) -> None:
    nums = column_numbers(start_tribbu)
    print(f"Columna que inicia en TRIBBU {start_tribbu:03d}")
    for n in nums:
        print(f"TRIBBU {n:03d}")


def open_column(start_tribbu: int, pause_seconds: float = 1.5) -> None:
    nums = column_numbers(start_tribbu)
    print(f"Abriendo columna desde TRIBBU {start_tribbu:03d}: {', '.join(f'{n:03d}' for n in nums)}")
    for idx, n in enumerate(nums, start=1):
        print(f"[{idx}/{len(nums)}]")
        open_tribbu(n)
        time.sleep(pause_seconds)


def sequence_numbers(start_tribbu: int, limit: int) -> list[int]:
    if not 1 <= start_tribbu <= config.MAX_TRIBBU:
        raise ValueError(f"TRIBBU fuera de rango: {start_tribbu}. Usa 1..{config.MAX_TRIBBU}.")
    if limit <= 0:
        raise ValueError("limit debe ser mayor que 0")
    end_tribbu = min(config.MAX_TRIBBU, start_tribbu + limit - 1)
    return list(range(start_tribbu, end_tribbu + 1))


def print_sequence(start_tribbu: int = 1, limit: int = 5) -> None:
    nums = sequence_numbers(start_tribbu, limit)
    print(
        f"Prueba secuencial desde TRIBBU {start_tribbu:03d} "
        f"con limit={limit}: {', '.join(f'{n:03d}' for n in nums)}"
    )


def open_sequence(start_tribbu: int = 1, limit: int = 5, pause_seconds: float = 1.5) -> None:
    nums = sequence_numbers(start_tribbu, limit)
    print(
        f"Abriendo secuencia desde TRIBBU {start_tribbu:03d} "
        f"con limit={limit}: {', '.join(f'{n:03d}' for n in nums)}"
    )
    for idx, n in enumerate(nums, start=1):
        print(f"[{idx}/{len(nums)}]")
        open_tribbu(n)
        time.sleep(pause_seconds)


def register_sequence(
    start_tribbu: int = 1,
    limit: int = 5,
    pause_seconds: float = 1.8,
    verify_phone: bool = False,
) -> None:
    nums = sequence_numbers(start_tribbu, limit)
    print(
        f"Iniciando registro secuencial desde TRIBBU {start_tribbu:03d} "
        f"con limit={limit}: {', '.join(f'{n:03d}' for n in nums)}"
    )
    for idx, n in enumerate(nums, start=1):
        print(f"[{idx}/{len(nums)}]")
        start_registration(n, verify_phone=verify_phone)
        time.sleep(pause_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launcher TRIBBU para Samsung Tab A9+.")
    sub = parser.add_subparsers(dest="command", required=True)

    open_cmd = sub.add_parser("open", help="Abre una app TRIBBU por numero.")
    open_cmd.add_argument("tribbu_number", type=int)

    show_cmd = sub.add_parser("show", help="Muestra pagina/fila/columna de un TRIBBU.")
    show_cmd.add_argument("tribbu_number", type=int)

    table_cmd = sub.add_parser("table", help="Imprime el tablero de una pagina.")
    table_cmd.add_argument("page", type=int, nargs="?", default=1, help="Pagina 1..N")

    role_cmd = sub.add_parser("role", help="Muestra si un TRIBBU es driver o passenger.")
    role_cmd.add_argument("tribbu_number", type=int)

    col_cmd = sub.add_parser("column", help="Imprime los TRIBBU de una columna completa.")
    col_cmd.add_argument("start_tribbu", type=int, help="Cabecera de columna: 001, 006, 011, etc.")

    open_col_cmd = sub.add_parser("open-column", help="Abre secuencialmente los TRIBBU de una columna.")
    open_col_cmd.add_argument("start_tribbu", type=int, help="Cabecera de columna: 001, 006, 011, etc.")
    open_col_cmd.add_argument("--pause", type=float, default=1.5, help="Pausa entre aperturas.")

    seq_cmd = sub.add_parser("sequence", help="Imprime una prueba secuencial de TRIBBUs consecutivos.")
    seq_cmd.add_argument("--start", type=int, default=1, help="TRIBBU inicial. Por defecto 001.")
    seq_cmd.add_argument("--limit", type=int, default=5, help="Cantidad de TRIBBUs consecutivos.")

    open_seq_cmd = sub.add_parser("open-sequence", help="Abre una prueba secuencial de TRIBBUs consecutivos.")
    open_seq_cmd.add_argument("--start", type=int, default=1, help="TRIBBU inicial. Por defecto 001.")
    open_seq_cmd.add_argument("--limit", type=int, default=5, help="Cantidad de TRIBBUs consecutivos.")
    open_seq_cmd.add_argument("--pause", type=float, default=1.5, help="Pausa entre aperturas.")

    reg_cmd = sub.add_parser(
        "register-sequence",
        help="Abre TRIBBUs consecutivos e inicia registro (Aceptar/Permitir + rol driver/passenger).",
    )
    reg_cmd.add_argument("--start", type=int, default=1, help="TRIBBU inicial. Por defecto 001.")
    reg_cmd.add_argument("--limit", type=int, default=5, help="Cantidad de TRIBBUs consecutivos.")
    reg_cmd.add_argument("--pause", type=float, default=1.8, help="Pausa entre apps.")
    reg_cmd.add_argument(
        "--verify-phone",
        action="store_true",
        help="Ejecuta etapa Verify your phone number comprando numero y OTP en VirtuNum.",
    )

    args = parser.parse_args()

    if args.command == "open":
        open_tribbu(args.tribbu_number)
        return 0
    if args.command == "show":
        print_slot(args.tribbu_number)
        return 0
    if args.command == "table":
        page_index = args.page - 1
        if page_index < 0 or page_index >= page_count():
            raise ValueError(f"Pagina invalida: {args.page}. Usa 1..{page_count()}.")
        print_table(page_index)
        return 0
    if args.command == "role":
        print_role(args.tribbu_number)
        return 0
    if args.command == "column":
        print_column(args.start_tribbu)
        return 0
    if args.command == "open-column":
        open_column(args.start_tribbu, pause_seconds=args.pause)
        return 0
    if args.command == "sequence":
        print_sequence(start_tribbu=args.start, limit=args.limit)
        return 0
    if args.command == "open-sequence":
        open_sequence(start_tribbu=args.start, limit=args.limit, pause_seconds=args.pause)
        return 0
    if args.command == "register-sequence":
        register_sequence(
            start_tribbu=args.start,
            limit=args.limit,
            pause_seconds=args.pause,
            verify_phone=args.verify_phone,
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
