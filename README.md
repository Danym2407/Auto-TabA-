# TRIBBU Tab A9+

Herramienta nueva, separada del script viejo, para abrir clones TRIBBU en una Samsung Tab A9+ usando el orden real de tu escritorio:

- 6 columnas por pagina
- 5 filas por pagina
- 30 apps por pagina
- 100 clones en total

## Regla de orden

Tu escritorio sigue esta formula dentro de cada pagina:

- fila 1: `001 006 011 016 021 026`
- fila 2: `002 007 012 017 022 027`
- fila 3: `003 008 013 018 023 028`
- fila 4: `004 009 014 019 024 029`
- fila 5: `005 010 015 020 025 030`

Eso significa que para cualquier `TRIBBU NNN` el codigo puede calcular:

- pagina
- fila
- columna
- coordenada de tap

## Archivos

- [config.py](config.py): ADB, serial y coordenadas del grid
- [tribbu_layout.py](tribbu_layout.py): conversion `TRIBBU -> pagina/fila/columna`
- [open_tribbu.py](open_tribbu.py): CLI para abrir una app por numero

## Regla Driver / Passenger

Solo estos TRIBBU se consideran `driver`:

`001, 006, 011, 016, 021, 026, 031, 036, 041, 046, 051, 056, 061, 066, 071, 076, 081, 086, 091, 096`

Todos los demas se consideran `passenger`.

La validacion esta fija en [config.py](config.py) con `DRIVER_TRIBBUS`.

## Uso

Mostrar donde esta un clone:

```powershell
python open_tribbu.py show 1
python open_tribbu.py show 96
```

Validar rol:

```powershell
python open_tribbu.py role 1
python open_tribbu.py role 2
python open_tribbu.py role 96
```

Imprimir el tablero de una pagina:

```powershell
python open_tribbu.py table 1
python open_tribbu.py table 2
```

Abrir una app:

```powershell
python open_tribbu.py open 1
python open_tribbu.py open 26
python open_tribbu.py open 90
```

Prueba secuencial empezando por `TRIBBU 001`:

```powershell
python open_tribbu.py sequence --limit 5
python open_tribbu.py open-sequence --limit 5
python open_tribbu.py register-sequence --limit 5
```

Eso abrira solo:

```text
TRIBBU 001
TRIBBU 002
TRIBBU 003
TRIBBU 004
TRIBBU 005
```

Para iniciar registro (no solo abrir), usa:

```powershell
python open_tribbu.py register-sequence --limit 5
```

Para incluir la etapa `Verify your phone number` con VirtuNum:

```powershell
$env:VIRTUNUM_API_KEY = "TU_API_KEY"
python open_tribbu.py register-sequence --start 1 --limit 1 --verify-phone
```

Variables opcionales para VirtuNum:

```powershell
$env:VIRTUNUM_API_BASE = "https://virtunum.com/api/v1"
$env:VIRTUNUM_COUNTRY = "netherlands"
$env:VIRTUNUM_PRODUCT = "other"
$env:VIRTUNUM_OPERATOR = "any"
$env:VIRTUNUM_COUNTRY_CODE = "31"
$env:VIRTUNUM_SMS_TIMEOUT = "180"
```

Si tu cuenta usa rutas API diferentes, puedes redefinir endpoints:

```powershell
$env:VIRTUNUM_BUY_PATH = "/activations"
$env:VIRTUNUM_CHECK_PATH = "/activations/{id}"
$env:VIRTUNUM_FINISH_PATH = "/activations/{id}/finish"
```

Flujo por cada app:

1. Abre TRIBBU NNN.
2. Si aparece `Aceptar`, hace clic.
3. Si aparece `Permitir`, hace clic.
4. Selecciona `Driver` o `Passenger` segun la regla fija.

Regla fija de `Driver`:

`001, 006, 011, 016, 021, 026, 031, 036, 041, 046, 051, 056, 061, 066, 071, 076, 081, 086, 091, 096`

Todos los demas se toman como `Passenger`.

Tambien puedes cambiar el inicio:

```powershell
python open_tribbu.py sequence --start 31 --limit 5
python open_tribbu.py open-sequence --start 31 --limit 5
```

## Calibracion

Si el tap no cae exactamente en el icono correcto, ajusta en [config.py](config.py):

- `X_CENTERS`
- `Y_CENTERS`
- `SWIPE_TO_PREV_PAGE`
- `SWIPE_TO_NEXT_PAGE`

La logica del orden ya queda fija segun tu layout real. Solo tendrias que afinar coordenadas si cambia el launcher.
