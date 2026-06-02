from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass(frozen=True)
class TribbuSlot:
    tribbu_number: int
    page_index: int
    row_index: int
    col_index: int
    x: int
    y: int


def tribbu_to_slot(tribbu_number: int) -> TribbuSlot:
    if not 1 <= tribbu_number <= config.MAX_TRIBBU:
        raise ValueError(f"TRIBBU fuera de rango: {tribbu_number}. Usa 1..{config.MAX_TRIBBU}.")

    zero_based = tribbu_number - 1
    page_index = zero_based // config.APPS_PER_PAGE
    local_index = zero_based % config.APPS_PER_PAGE

    # Tu launcher esta ordenado por columnas de 5:
    # 001,006,011,016,021,026 en la primera fila.
    row_index = local_index % config.ROWS_PER_PAGE
    col_index = local_index // config.ROWS_PER_PAGE

    return TribbuSlot(
        tribbu_number=tribbu_number,
        page_index=page_index,
        row_index=row_index,
        col_index=col_index,
        x=config.X_CENTERS[col_index],
        y=config.Y_CENTERS[row_index],
    )


def build_page_table(page_index: int) -> list[list[int | None]]:
    if page_index < 0:
        raise ValueError("page_index no puede ser negativo")

    page_start = page_index * config.APPS_PER_PAGE + 1
    page_end = min(page_start + config.APPS_PER_PAGE - 1, config.MAX_TRIBBU)

    table = [[None for _ in range(config.COLS_PER_PAGE)] for _ in range(config.ROWS_PER_PAGE)]
    for tribbu_number in range(page_start, page_end + 1):
        slot = tribbu_to_slot(tribbu_number)
        table[slot.row_index][slot.col_index] = tribbu_number
    return table


def page_count() -> int:
    q, r = divmod(config.MAX_TRIBBU, config.APPS_PER_PAGE)
    return q + (1 if r else 0)


def is_driver_tribbu(tribbu_number: int) -> bool:
    if not 1 <= tribbu_number <= config.MAX_TRIBBU:
        raise ValueError(f"TRIBBU fuera de rango: {tribbu_number}. Usa 1..{config.MAX_TRIBBU}.")
    return tribbu_number in config.DRIVER_TRIBBUS


def role_for_tribbu(tribbu_number: int) -> str:
    return "driver" if is_driver_tribbu(tribbu_number) else "passenger"
