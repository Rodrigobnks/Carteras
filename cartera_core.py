from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd


DEFAULT_KEY_TEXT = "Marca & Pais & id_desembolso & tipo_desembolso & dbc_cliente_id"
EMPTY_KEY_PART = "<VACIO>"

COLUMN_CONCEPTS: dict[str, list[str]] = {
    "marca": ["marca", "brand"],
    "pais": ["pais", "país", "country"],
    "id_desembolso": [
        "id desembolso",
        "desembolso id",
        "numero desembolso",
        "número desembolso",
        "credito id",
        "crédito id",
        "id credito",
    ],
    "tipo_desembolso": [
        "tipo desembolso",
        "desembolso tipo",
        "producto desembolso",
        "tipo credito",
        "tipo crédito",
        "producto",
    ],
    "dbc_cliente_id": [
        "dbc cliente id",
        "id cliente dbc",
        "cliente dbc",
        "dbc cliente",
        "dbc",
        "cliente",
    ],
    "dias_de_atraso": [
        "dias de atraso",
        "días de atraso",
        "dias atraso",
        "mora dias",
        "días mora",
        "atraso",
    ],
}


@dataclass
class LoadedPortfolio:
    name: str
    dataframe: pd.DataFrame
    sheet_name: str
    header_row: int
    portfolio_type: str
    cutoff_date: date | None
    upload_order: int


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    direct = SequenceMatcher(None, left, right).ratio() * 100
    left_tokens = "_".join(sorted(set(left.split("_"))))
    right_tokens = "_".join(sorted(set(right.split("_"))))
    token_score = SequenceMatcher(None, left_tokens, right_tokens).ratio() * 100
    containment = 92.0 if left in right or right in left else 0.0
    return max(direct, token_score, containment)


def make_unique_columns(columns: Iterable[object]) -> list[str]:
    result: list[str] = []
    used: dict[str, int] = {}
    for index, raw in enumerate(columns, start=1):
        base = str(raw).strip() if not pd.isna(raw) else f"columna_{index}"
        base = base or f"columna_{index}"
        count = used.get(base, 0)
        used[base] = count + 1
        result.append(base if count == 0 else f"{base}_{count + 1}")
    return result


def _header_score(values: Sequence[object]) -> int:
    normalized = {normalize_text(value) for value in values if not pd.isna(value)}
    aliases = {
        normalize_text(alias)
        for concept_aliases in COLUMN_CONCEPTS.values()
        for alias in concept_aliases
    }
    score = len(normalized & aliases) * 10
    score += sum(1 for value in normalized if value in {"corte", "cliente_id", "nombre_cliente"})
    return score


def _best_header_row(preview: pd.DataFrame) -> tuple[int, int]:
    if preview.empty:
        return 0, 0
    scores = [(_header_score(row.tolist()), int(index)) for index, row in preview.iterrows()]
    score, index = max(scores, default=(0, 0))
    return index, score


def parse_filename_metadata(filename: str) -> tuple[str, date | None]:
    upper_name = filename.upper()
    portfolio_type = "LATAM" if "LATAM" in upper_name else "México"
    parsed_date: date | None = None

    date_patterns = (
        (r"(?<!\d)(20\d{2})[-_ ](0[1-9]|1[0-2])[-_ ]([0-2]\d|3[01])(?!\d)", "%Y-%m-%d", "ymd"),
        (r"(?<!\d)([0-2]\d|3[01])[-_ ](0[1-9]|1[0-2])[-_ ](20\d{2})(?!\d)", "%d-%m-%Y", "dmy"),
        (r"(?<!\d)([0-2]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)", "%d%m%Y", "compact"),
        (r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)", "%Y%m%d", "compact"),
    )
    for pattern, date_format, style in date_patterns:
        match = re.search(pattern, filename)
        if not match:
            continue
        separator = "-" if style in {"ymd", "dmy"} else ""
        try:
            parsed_date = datetime.strptime(separator.join(match.groups()), date_format).date()
            break
        except ValueError:
            continue
    return portfolio_type, parsed_date


def detect_cutoff_date(frame: pd.DataFrame) -> date | None:
    corte_column, _ = resolve_column("corte", frame.columns, minimum_score=75)
    if corte_column is None:
        return None

    values = frame[corte_column].dropna()
    if values.empty:
        return None

    sample = values.iloc[0]
    try:
        if isinstance(sample, (pd.Timestamp, datetime, date)):
            return pd.Timestamp(sample).date()

        numeric = pd.to_numeric(pd.Series([sample]), errors="coerce").iloc[0]
        if pd.notna(numeric) and 1 <= float(numeric) <= 100000:
            return pd.to_datetime(float(numeric), unit="D", origin="1899-12-30").date()

        parsed = pd.to_datetime(sample, errors="coerce", dayfirst=True)
        return parsed.date() if pd.notna(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _read_csv_bytes(file_bytes: bytes) -> tuple[pd.DataFrame, str, int]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            preview = pd.read_csv(
                io.BytesIO(file_bytes),
                sep=None,
                engine="python",
                encoding=encoding,
                header=None,
                nrows=20,
            )
            header_row, _ = _best_header_row(preview)
            frame = pd.read_csv(
                io.BytesIO(file_bytes),
                sep=None,
                engine="python",
                encoding=encoding,
                header=header_row,
            )
            return frame, "CSV", header_row + 1
        except Exception as exc:  # pragma: no cover - only reached for malformed files
            last_error = exc
    raise ValueError(f"No fue posible leer el CSV: {last_error}")


def read_portfolio_bytes(file_bytes: bytes, filename: str, upload_order: int = 0) -> LoadedPortfolio:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        frame, sheet_name, header_row = _read_csv_bytes(file_bytes)
    else:
        excel = pd.ExcelFile(io.BytesIO(file_bytes))
        best: tuple[int, str, int] | None = None
        for sheet in excel.sheet_names:
            preview = pd.read_excel(excel, sheet_name=sheet, header=None, nrows=20)
            row, score = _best_header_row(preview)
            candidate = (score, sheet, row)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            raise ValueError("El libro no contiene hojas legibles.")
        _, sheet_name, zero_based_header = best
        frame = pd.read_excel(excel, sheet_name=sheet_name, header=zero_based_header)
        header_row = zero_based_header + 1

    frame = frame.dropna(axis=0, how="all").reset_index(drop=True)
    # Preserve named columns even when the current cut contains no values; a key
    # component such as dbc_cliente_id can legitimately be blank in a sample.
    keep_columns = [
        column
        for column in frame.columns
        if not frame[column].isna().all() or not str(column).casefold().startswith("unnamed")
    ]
    frame = frame.loc[:, keep_columns]
    frame.columns = make_unique_columns(frame.columns)
    portfolio_type, cutoff_date = parse_filename_metadata(filename)

    if cutoff_date is None:
        cutoff_date = detect_cutoff_date(frame)

    country_column = resolve_column("pais", frame.columns, minimum_score=72)[0]
    if country_column is None:
        frame.insert(min(2, len(frame.columns)), "Pais", "México")
        if portfolio_type == "No identificado":
            portfolio_type = "México"
    elif portfolio_type == "México":
        frame[country_column] = "México"

    return LoadedPortfolio(
        name=filename,
        dataframe=frame,
        sheet_name=sheet_name,
        header_row=header_row,
        portfolio_type=portfolio_type,
        cutoff_date=cutoff_date,
        upload_order=upload_order,
    )


def _concept_for_query(query: str) -> str | None:
    normalized = normalize_text(query)
    best: tuple[float, str] | None = None
    for concept, aliases in COLUMN_CONCEPTS.items():
        for alias in [concept, *aliases]:
            candidate = normalize_text(alias)
            if normalized == candidate:
                score = 100.0
            elif candidate in normalized or normalized in candidate:
                score = 93.0 if len(normalized) >= 4 else 75.0
            else:
                score = _similarity(normalized, candidate)
            if best is None or score > best[0]:
                best = (score, concept)
    return best[1] if best and best[0] >= 70 else None


def resolve_column(
    query: str,
    columns: Iterable[object],
    minimum_score: float = 55,
) -> tuple[str | None, float]:
    column_list = [str(column) for column in columns]
    normalized_query = normalize_text(query)
    normalized_columns = {column: normalize_text(column) for column in column_list}

    for column, normalized_column in normalized_columns.items():
        if normalized_column == normalized_query:
            return column, 100.0

    concept = _concept_for_query(query)
    search_terms = [normalized_query]
    if concept:
        search_terms.extend(normalize_text(alias) for alias in [concept, *COLUMN_CONCEPTS[concept]])

    best_column: str | None = None
    best_score = -1.0
    for column, normalized_column in normalized_columns.items():
        scores = [_similarity(term, normalized_column) for term in search_terms if term]
        if concept and normalized_column == normalize_text(concept):
            scores.append(100.0)
        score = max(scores, default=0.0)
        if score > best_score:
            best_column, best_score = column, score
    if best_score < minimum_score:
        return None, best_score
    return best_column, best_score


def split_key_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if re.search(r"[&,;+|\n]", text):
        return [part.strip() for part in re.split(r"[&,;+|\n]+", text) if part.strip()]

    normalized = normalize_text(text)
    found: list[tuple[int, str]] = []
    for concept, aliases in COLUMN_CONCEPTS.items():
        if concept == "dias_de_atraso":
            continue
        positions = [normalized.find(normalize_text(alias)) for alias in [concept, *aliases]]
        positions = [position for position in positions if position >= 0]
        if positions:
            found.append((min(positions), concept))
    if len(found) > 1:
        return [concept for _, concept in sorted(found)]
    return [text]


def resolve_key_text(text: str, columns: Iterable[object]) -> tuple[list[str], list[dict[str, object]]]:
    tokens = split_key_text(text)
    resolved: list[str] = []
    details: list[dict[str, object]] = []
    for token in tokens:
        column, score = resolve_column(token, columns)
        details.append({"texto": token, "columna": column or "No encontrada", "confianza": round(score, 1)})
        if column is not None and column not in resolved:
            resolved.append(column)
    return resolved, details


def resolve_columns_for_frame(labels: Sequence[str], columns: Iterable[object]) -> tuple[list[str], list[str]]:
    resolved: list[str] = []
    missing: list[str] = []
    for label in labels:
        column, _ = resolve_column(label, columns)
        if column is None:
            missing.append(label)
        elif column not in resolved:
            resolved.append(column)
    return resolved, missing


def _normalize_key_value(value: object) -> str:
    if pd.isna(value) or str(value).strip() == "":
        return EMPTY_KEY_PART
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    normalized = normalize_text(value)
    return normalized.upper() if normalized else EMPTY_KEY_PART


def prepare_portfolio(
    portfolio: LoadedPortfolio,
    reference_key_columns: Sequence[str],
) -> tuple[pd.DataFrame, list[str], str]:
    frame = portfolio.dataframe.copy()
    key_columns, missing = resolve_columns_for_frame(reference_key_columns, frame.columns)
    if missing or len(key_columns) != len(reference_key_columns):
        raise ValueError(f"No se pudieron asociar estas columnas en {portfolio.name}: {', '.join(missing)}")

    atraso_column, _ = resolve_column("dias_de_atraso", frame.columns, minimum_score=60)
    if atraso_column is None:
        raise ValueError(f"No se encontró la columna de días de atraso en {portfolio.name}.")

    normalized_parts = pd.DataFrame(
        {column: frame[column].map(_normalize_key_value) for column in key_columns},
        index=frame.index,
    )
    frame["_llave"] = normalized_parts.agg("¦".join, axis=1)
    frame["_llave_valida"] = ~(normalized_parts == EMPTY_KEY_PART).any(axis=1)
    frame["_dias_atraso"] = pd.to_numeric(frame[atraso_column], errors="coerce").fillna(0)
    frame["_estado"] = frame["_dias_atraso"].gt(0).map({True: "Atraso", False: "Al corriente"})
    frame["_fila_origen"] = frame.index + portfolio.header_row + 1
    return frame, key_columns, atraso_column


def duplicate_report(frame: pd.DataFrame, key_columns: Sequence[str]) -> pd.DataFrame:
    counts = frame.groupby("_llave", dropna=False).size().rename("veces_repetida").reset_index()
    duplicated = counts[counts["veces_repetida"] > 1]
    if duplicated.empty:
        return pd.DataFrame(columns=[*key_columns, "llave", "llave_valida", "veces_repetida"])
    sample = frame.drop_duplicates("_llave")[["_llave", "_llave_valida", *key_columns]]
    result = duplicated.merge(sample, on="_llave", how="left")
    return result[[*key_columns, "_llave", "_llave_valida", "veces_repetida"]].rename(
        columns={"_llave": "llave", "_llave_valida": "llave_valida"}
    ).sort_values("veces_repetida", ascending=False)


def invalid_key_report(frame: pd.DataFrame, key_columns: Sequence[str]) -> pd.DataFrame:
    columns = ["_fila_origen", *key_columns, "_llave"]
    result = frame.loc[~frame["_llave_valida"], columns].copy()
    return result.rename(columns={"_fila_origen": "fila_origen", "_llave": "llave"})


def _key_summary(frame: pd.DataFrame, key_columns: Sequence[str]) -> pd.DataFrame:
    valid = frame[frame["_llave_valida"]].copy()
    if valid.empty:
        return pd.DataFrame(columns=["llave", *key_columns, "dias_atraso", "estado", "filas"])
    summary = (
        valid.groupby("_llave", as_index=False)
        .agg(dias_atraso=("_dias_atraso", "max"), filas=("_llave", "size"))
        .rename(columns={"_llave": "llave"})
    )
    summary["estado"] = summary["dias_atraso"].gt(0).map({True: "Atraso", False: "Al corriente"})
    sample = valid.drop_duplicates("_llave")[["_llave", *key_columns]].rename(columns={"_llave": "llave"})
    return summary.merge(sample, on="llave", how="left")[["llave", *key_columns, "dias_atraso", "estado", "filas"]]


def compare_presence(
    first: pd.DataFrame,
    second: pd.DataFrame,
    first_name: str,
    second_name: str,
    first_key_columns: Sequence[str],
    second_key_columns: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    first_summary = _key_summary(first, first_key_columns)
    second_summary = _key_summary(second, second_key_columns)
    first_keys = set(first_summary["llave"])
    second_keys = set(second_summary["llave"])

    exits = first_summary[first_summary["llave"].isin(first_keys - second_keys)].copy()
    exits.insert(0, "comparacion", f"{first_name} → {second_name}")
    exits.insert(1, "movimiento", "Salió")

    entries = second_summary[second_summary["llave"].isin(second_keys - first_keys)].copy()
    entries.insert(0, "comparacion", f"{first_name} → {second_name}")
    entries.insert(1, "movimiento", "Entró")

    summary = pd.DataFrame(
        [{
            "comparacion": f"{first_name} → {second_name}",
            "cartera_inicial": len(first_keys),
            "cartera_siguiente": len(second_keys),
            "salieron": len(exits),
            "entraron": len(entries),
            "permanecen": len(first_keys & second_keys),
        }]
    )
    return summary, exits, entries


def compare_status(
    first: pd.DataFrame,
    second: pd.DataFrame,
    first_name: str,
    second_name: str,
    first_key_columns: Sequence[str],
    second_key_columns: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    left = _key_summary(first, first_key_columns)
    right = _key_summary(second, second_key_columns)
    merged = left.merge(right, on="llave", how="inner", suffixes=("_antes", "_despues"))
    if merged.empty:
        detail = pd.DataFrame(columns=["comparacion", "llave", "estado_antes", "estado_despues"])
    else:
        detail = merged[
            merged["estado_antes"].ne(merged["estado_despues"])
        ][["llave", "dias_atraso_antes", "estado_antes", "dias_atraso_despues", "estado_despues"]].copy()
        detail.insert(0, "comparacion", f"{first_name} → {second_name}")

    corriente_atraso = int(
        ((merged.get("estado_antes") == "Al corriente") & (merged.get("estado_despues") == "Atraso")).sum()
    ) if not merged.empty else 0
    atraso_corriente = int(
        ((merged.get("estado_antes") == "Atraso") & (merged.get("estado_despues") == "Al corriente")).sum()
    ) if not merged.empty else 0
    summary = pd.DataFrame(
        [{
            "comparacion": f"{first_name} → {second_name}",
            "clientes_comunes": len(merged),
            "corriente_a_atraso": corriente_atraso,
            "atraso_a_corriente": atraso_corriente,
            "sin_cambio": len(merged) - corriente_atraso - atraso_corriente,
        }]
    )
    return summary, detail


def multi_disbursement_report(
    frame: pd.DataFrame,
    key_columns: Sequence[str],
) -> tuple[pd.DataFrame, list[str], str, str]:
    disbursement_column, _ = resolve_column("id_desembolso", frame.columns, minimum_score=60)
    type_column, _ = resolve_column("tipo_desembolso", frame.columns, minimum_score=60)
    if disbursement_column is None or type_column is None:
        raise ValueError("Se requieren las columnas de id y tipo de desembolso para este análisis.")

    client_columns = [
        column
        for column in key_columns
        if column not in {disbursement_column, type_column}
        and _concept_for_query(column) not in {"id_desembolso", "tipo_desembolso"}
    ]
    if not client_columns:
        fallback, _ = resolve_column("dbc_cliente_id", frame.columns, minimum_score=60)
        if fallback:
            client_columns = [fallback]
        else:
            raise ValueError("La llave no deja columnas disponibles para identificar al cliente.")

    parts = pd.DataFrame({column: frame[column].map(_normalize_key_value) for column in client_columns})
    valid = ~(parts == EMPTY_KEY_PART).any(axis=1)
    working = frame.loc[valid].copy()
    working["_cliente_llave"] = parts.loc[valid].agg("¦".join, axis=1)

    def join_unique(series: pd.Series) -> str:
        values = sorted({_normalize_key_value(value) for value in series if _normalize_key_value(value) != EMPTY_KEY_PART})
        return " | ".join(values)

    grouped = (
        working.groupby("_cliente_llave", as_index=False)
        .agg(
            cantidad_desembolsos=(disbursement_column, lambda values: values.map(_normalize_key_value).replace(EMPTY_KEY_PART, pd.NA).nunique()),
            ids_desembolso=(disbursement_column, join_unique),
            tipos_desembolso=(type_column, join_unique),
            cantidad_tipos=(type_column, lambda values: values.map(_normalize_key_value).replace(EMPTY_KEY_PART, pd.NA).nunique()),
            filas=("_cliente_llave", "size"),
        )
    )
    grouped = grouped[grouped["cantidad_desembolsos"] > 1].copy()
    sample = working.drop_duplicates("_cliente_llave")[["_cliente_llave", *client_columns]]
    result = grouped.merge(sample, on="_cliente_llave", how="left").rename(columns={"_cliente_llave": "llave_cliente"})
    result = result[[*client_columns, "llave_cliente", "cantidad_desembolsos", "cantidad_tipos", "tipos_desembolso", "ids_desembolso", "filas"]]
    return result.sort_values("cantidad_desembolsos", ascending=False), client_columns, disbursement_column, type_column


def safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\\/*?:\[\]]", "_", name).strip() or "Reporte"
    cleaned = cleaned[:31]
    candidate = cleaned
    index = 2
    while candidate.casefold() in used:
        suffix = f"_{index}"
        candidate = f"{cleaned[:31 - len(suffix)]}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def build_excel_report(
    sheets: Mapping[str, pd.DataFrame],
    metadata: Sequence[tuple[str, object]] | None = None,
) -> bytes:
    output = io.BytesIO()
    used_names: set[str] = set()
    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
        engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}},
    ) as writer:
        workbook = writer.book
        header_format = workbook.add_format({
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#0F4C5C",
            "border": 0,
            "text_wrap": True,
            "valign": "vcenter",
        })
        title_format = workbook.add_format({"bold": True, "font_size": 16, "font_color": "#0F4C5C"})
        note_format = workbook.add_format({"font_color": "#475569"})

        if metadata:
            metadata_frame = pd.DataFrame(metadata, columns=["Concepto", "Valor"])
            sheet_name = safe_sheet_name("Resumen", used_names)
            metadata_frame.to_excel(writer, sheet_name=sheet_name, index=False, startrow=2)
            worksheet = writer.sheets[sheet_name]
            worksheet.write(0, 0, "Reporte de análisis de cartera", title_format)
            worksheet.write(1, 0, "Generado desde el tablero; cada pestaña contiene un resultado descargable.", note_format)
            worksheet.set_row(2, 30)
            worksheet.set_column(0, 0, 30)
            worksheet.set_column(1, 1, 80)
            for col_num, value in enumerate(metadata_frame.columns):
                worksheet.write(2, col_num, value, header_format)
            worksheet.freeze_panes(3, 0)

        for requested_name, frame in sheets.items():
            sheet_name = safe_sheet_name(requested_name, used_names)
            safe_frame = frame.copy()
            safe_frame.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, max(len(safe_frame), 1), max(len(safe_frame.columns) - 1, 0))
            worksheet.set_row(0, 32)
            for col_num, column in enumerate(safe_frame.columns):
                values = safe_frame[column].astype(str).head(500) if not safe_frame.empty else pd.Series(dtype=str)
                width = min(max(len(str(column)) + 2, int(values.map(len).max()) + 2 if not values.empty else 12), 45)
                worksheet.set_column(col_num, col_num, width)
                worksheet.write(0, col_num, column, header_format)
            if safe_frame.empty and len(safe_frame.columns) == 0:
                worksheet.write(0, 0, "Sin resultados")
    output.seek(0)
    return output.getvalue()


def dataframe_to_csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")
