from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from cartera_core import (
    DEFAULT_KEY_TEXT,
    LoadedPortfolio,
    build_excel_report,
    compare_presence,
    compare_status,
    dataframe_to_csv_bytes,
    duplicate_report,
    invalid_key_report,
    multi_disbursement_report,
    prepare_portfolio,
    read_portfolio_bytes,
    resolve_key_text,
)


st.set_page_config(page_title="Comparador de carteras", page_icon="📊", layout="wide")


@st.cache_data(show_spinner=False)
def load_file(file_bytes: bytes, filename: str, upload_order: int) -> LoadedPortfolio:
    return read_portfolio_bytes(file_bytes, filename, upload_order)


def short_name(filename: str) -> str:
    return Path(filename).stem[:36]


def concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if frame is not None]
    return pd.concat(nonempty, ignore_index=True, sort=False) if nonempty else pd.DataFrame()


def show_downloads(frame: pd.DataFrame, label: str, filename_stem: str, key_prefix: str) -> None:
    left, right = st.columns(2)
    with left:
        st.download_button(
            f"Descargar {label} en Excel",
            data=build_excel_report({label: frame}),
            file_name=f"{filename_stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_xlsx",
            use_container_width=True,
        )
    with right:
        st.download_button(
            f"Descargar {label} en CSV",
            data=dataframe_to_csv_bytes(frame),
            file_name=f"{filename_stem}.csv",
            mime="text/csv",
            key=f"{key_prefix}_csv",
            use_container_width=True,
        )


st.title("📊 Comparador de carteras")
st.caption("Carga hasta tres cortes, compara clientes mediante una llave flexible y descarga los resultados en Excel.")

with st.sidebar:
    st.header("1. Cargar archivos")
    uploaded_files = st.file_uploader(
        "Carteras Excel o CSV",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        help="Formatos sugeridos: LATAM 2026-08-12.xlsx o PRESICO 2026-08-12.xlsx.",
    )
    sort_by_date = st.checkbox("Ordenar por fecha del nombre", value=True)
    st.divider()
    st.header("2. Definir llave")
    key_text = st.text_area(
        "Escribe las columnas o una descripción",
        value=DEFAULT_KEY_TEXT,
        help="No necesitas respetar mayúsculas, acentos ni guiones. Puedes separar campos con &, coma o punto y coma.",
    )

if not uploaded_files:
    st.info("Carga de una a tres carteras para comenzar. Con una cartera verás duplicados y multidesembolsos; con dos o tres también verás comparaciones entre cortes consecutivos.")
    st.markdown(
        "**Reglas aplicadas:** atraso = `dias_de_atraso > 0`; al corriente = `dias_de_atraso = 0`; "
        "si no existe la columna País, se agrega México; las filas con componentes vacíos en la llave se reportan y no se comparan."
    )
    st.stop()

if len(uploaded_files) > 3:
    st.error("Solo se permiten hasta tres archivos. Retira los archivos adicionales.")
    st.stop()

portfolios: list[LoadedPortfolio] = []
load_errors: list[str] = []
with st.spinner("Leyendo y validando archivos..."):
    for order, uploaded in enumerate(uploaded_files):
        try:
            portfolios.append(load_file(uploaded.getvalue(), uploaded.name, order))
        except Exception as exc:
            load_errors.append(f"{uploaded.name}: {exc}")

if load_errors:
    st.error("No fue posible leer uno o más archivos:\n\n" + "\n\n".join(load_errors))
    st.stop()

if sort_by_date:
    portfolios.sort(key=lambda item: (item.cutoff_date is None, item.cutoff_date or datetime.max.date(), item.upload_order))

st.subheader("Archivos reconocidos")
file_summary = pd.DataFrame([
    {
        "Orden": index + 1,
        "Archivo": portfolio.name,
        "Tipo": portfolio.portfolio_type,
        "Fecha detectada": portfolio.cutoff_date.isoformat() if portfolio.cutoff_date else "No detectada",
        "Hoja": portfolio.sheet_name,
        "Fila de encabezados": portfolio.header_row,
        "Registros": len(portfolio.dataframe),
        "Columnas": len(portfolio.dataframe.columns),
    }
    for index, portfolio in enumerate(portfolios)
])
st.dataframe(file_summary, hide_index=True, use_container_width=True)

reference = portfolios[0]
suggested_columns, resolution_details = resolve_key_text(key_text, reference.dataframe.columns)
if not suggested_columns:
    st.error("No pude asociar el texto de la llave con las columnas del primer archivo. Revisa la descripción.")
    st.dataframe(pd.DataFrame(resolution_details), hide_index=True, use_container_width=True)
    st.stop()

selection_key = hashlib.sha1((key_text + "|" + "|".join(suggested_columns)).encode("utf-8")).hexdigest()[:12]
selected_key_columns = st.multiselect(
    "Columnas interpretadas para la llave (puedes corregirlas)",
    options=list(reference.dataframe.columns),
    default=suggested_columns,
    key=f"key_columns_{selection_key}",
)
with st.expander("Ver cómo se interpretó el texto"):
    st.dataframe(pd.DataFrame(resolution_details), hide_index=True, use_container_width=True)

if not selected_key_columns:
    st.warning("Selecciona al menos una columna para formar la llave.")
    st.stop()

prepared: list[dict[str, object]] = []
prep_errors: list[str] = []
for portfolio in portfolios:
    try:
        frame, actual_key_columns, atraso_column = prepare_portfolio(portfolio, selected_key_columns)
        prepared.append({
            "portfolio": portfolio,
            "frame": frame,
            "key_columns": actual_key_columns,
            "atraso_column": atraso_column,
        })
    except Exception as exc:
        prep_errors.append(str(exc))

if prep_errors:
    st.error("La llave o la columna de atraso no pudo homologarse:\n\n" + "\n\n".join(prep_errors))
    st.stop()

st.success("Llave activa: " + " & ".join(selected_key_columns))

duplicate_tables: list[pd.DataFrame] = []
invalid_tables: list[pd.DataFrame] = []
multi_tables: list[pd.DataFrame] = []
duplicate_summary_rows: list[dict[str, object]] = []
multi_summary_rows: list[dict[str, object]] = []
multi_notes: list[str] = []

for item in prepared:
    portfolio = item["portfolio"]
    frame = item["frame"]
    key_columns = item["key_columns"]
    duplicates = duplicate_report(frame, key_columns)
    invalid = invalid_key_report(frame, key_columns)
    duplicates.insert(0, "archivo", portfolio.name)
    invalid.insert(0, "archivo", portfolio.name)
    duplicate_tables.append(duplicates)
    invalid_tables.append(invalid)
    duplicate_summary_rows.append({
        "archivo": portfolio.name,
        "llaves_unicas_validas": int(frame.loc[frame["_llave_valida"], "_llave"].nunique()),
        "llaves_repetidas": int(len(duplicates)),
        "filas_en_llaves_repetidas": int(duplicates["veces_repetida"].sum()) if not duplicates.empty else 0,
        "filas_con_llave_incompleta": int((~frame["_llave_valida"]).sum()),
    })
    try:
        multi, client_columns, disbursement_column, type_column = multi_disbursement_report(frame, key_columns)
        multi.insert(0, "archivo", portfolio.name)
        multi_tables.append(multi)
        multi_summary_rows.append({
            "archivo": portfolio.name,
            "clientes_con_mas_de_un_desembolso": len(multi),
            "llave_cliente_utilizada": " & ".join(client_columns),
            "columna_id_desembolso": disbursement_column,
            "columna_tipo_desembolso": type_column,
        })
    except Exception as exc:
        multi_notes.append(f"{portfolio.name}: {exc}")

presence_summaries: list[pd.DataFrame] = []
exit_tables: list[pd.DataFrame] = []
entry_tables: list[pd.DataFrame] = []
status_summaries: list[pd.DataFrame] = []
status_detail_tables: list[pd.DataFrame] = []

for first, second in zip(prepared, prepared[1:]):
    first_portfolio = first["portfolio"]
    second_portfolio = second["portfolio"]
    presence, exits, entries = compare_presence(
        first["frame"], second["frame"], short_name(first_portfolio.name), short_name(second_portfolio.name),
        first["key_columns"], second["key_columns"],
    )
    status, status_detail = compare_status(
        first["frame"], second["frame"], short_name(first_portfolio.name), short_name(second_portfolio.name),
        first["key_columns"], second["key_columns"],
    )
    presence_summaries.append(presence)
    exit_tables.append(exits)
    entry_tables.append(entries)
    status_summaries.append(status)
    status_detail_tables.append(status_detail)

presence_summary = concat_frames(presence_summaries)
exits_all = concat_frames(exit_tables)
entries_all = concat_frames(entry_tables)
status_summary = concat_frames(status_summaries)
status_detail_all = concat_frames(status_detail_tables)
duplicate_summary = pd.DataFrame(duplicate_summary_rows)
duplicates_all = concat_frames(duplicate_tables)
invalid_all = concat_frames(invalid_tables)
multi_summary = pd.DataFrame(multi_summary_rows)
multi_all = concat_frames(multi_tables)

report_sheets: dict[str, pd.DataFrame] = {
    "Archivos": file_summary,
    "Resumen duplicados": duplicate_summary,
    "Llaves repetidas": duplicates_all,
    "Llaves incompletas": invalid_all,
    "Resumen multidesembolso": multi_summary,
    "Multidesembolsos": multi_all,
}
if len(prepared) >= 2:
    report_sheets.update({
        "Resumen presencia": presence_summary,
        "Clientes que salieron": exits_all,
        "Clientes que entraron": entries_all,
        "Resumen cambios atraso": status_summary,
        "Detalle cambios atraso": status_detail_all,
    })

metadata = [
    ("Fecha de generación", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ("Llave solicitada", key_text),
    ("Llave interpretada", " & ".join(selected_key_columns)),
    ("Regla de atraso", "dias_de_atraso > 0"),
    ("Regla de corriente", "dias_de_atraso = 0"),
    ("Comparaciones", "Cortes consecutivos según el orden mostrado"),
]

st.download_button(
    "⬇️ Descargar reporte consolidado en Excel",
    data=build_excel_report(report_sheets, metadata),
    file_name="reporte_consolidado_carteras.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
    type="primary",
)

tab_names = ["Resumen", "Entradas y salidas", "Cambios de atraso", "Llaves repetidas", "Multidesembolsos", "Calidad de datos"]
tabs = st.tabs(tab_names)

with tabs[0]:
    st.subheader("Resumen ejecutivo")
    total_valid = sum(int(item["frame"].loc[item["frame"]["_llave_valida"], "_llave"].nunique()) for item in prepared)
    total_duplicates = int(duplicate_summary["llaves_repetidas"].sum()) if not duplicate_summary.empty else 0
    total_multi = int(multi_summary["clientes_con_mas_de_un_desembolso"].sum()) if not multi_summary.empty else 0
    total_transitions = int(status_detail_all.shape[0])
    cols = st.columns(4)
    cols[0].metric("Llaves válidas", f"{total_valid:,}")
    cols[1].metric("Llaves repetidas", f"{total_duplicates:,}")
    cols[2].metric("Clientes multidesembolso", f"{total_multi:,}")
    cols[3].metric("Cambios de estado", f"{total_transitions:,}")
    if not presence_summary.empty:
        st.markdown("**Comparaciones entre cortes**")
        st.dataframe(presence_summary, hide_index=True, use_container_width=True)
    st.markdown("**Calidad y duplicados por archivo**")
    st.dataframe(duplicate_summary, hide_index=True, use_container_width=True)

with tabs[1]:
    st.subheader("Clientes que salen o entran")
    if len(prepared) < 2:
        st.info("Carga al menos dos carteras para comparar presencia.")
    else:
        st.dataframe(presence_summary, hide_index=True, use_container_width=True)
        movement = st.radio("Mostrar", ["Salieron", "Entraron"], horizontal=True)
        movement_frame = exits_all if movement == "Salieron" else entries_all
        st.dataframe(movement_frame, hide_index=True, use_container_width=True)
        show_downloads(movement_frame, movement, f"clientes_{movement.casefold()}", f"presence_{movement}")

with tabs[2]:
    st.subheader("Cambios entre al corriente y atraso")
    st.caption("Para llaves repetidas dentro de un corte se usa el mayor número de días de atraso.")
    if len(prepared) < 2:
        st.info("Carga al menos dos carteras para comparar estados.")
    else:
        st.dataframe(status_summary, hide_index=True, use_container_width=True)
        st.dataframe(status_detail_all, hide_index=True, use_container_width=True)
        show_downloads(status_detail_all, "cambios de atraso", "cambios_estado_atraso", "status")

with tabs[3]:
    st.subheader("Llaves repetidas en cada cartera")
    st.dataframe(duplicate_summary, hide_index=True, use_container_width=True)
    st.dataframe(duplicates_all, hide_index=True, use_container_width=True)
    show_downloads(duplicates_all, "llaves repetidas", "llaves_repetidas", "duplicates")

with tabs[4]:
    st.subheader("Clientes con más de un desembolso")
    st.caption("La llave de cliente se obtiene quitando id_desembolso y tipo_desembolso de la llave activa. Así es posible contar desembolsos distintos del mismo cliente.")
    if multi_notes:
        st.warning("\n\n".join(multi_notes))
    if not multi_summary.empty:
        st.dataframe(multi_summary, hide_index=True, use_container_width=True)
    st.dataframe(multi_all, hide_index=True, use_container_width=True)
    show_downloads(multi_all, "multidesembolsos", "clientes_multidesembolso", "multi")

with tabs[5]:
    st.subheader("Filas con llave incompleta")
    st.caption("Estas filas se excluyen de comparaciones para evitar coincidencias falsas, pero permanecen disponibles en el reporte.")
    st.dataframe(invalid_all, hide_index=True, use_container_width=True)
    show_downloads(invalid_all, "llaves incompletas", "llaves_incompletas", "invalid")

with st.expander("Vista previa de datos originales"):
    selected_preview = st.selectbox("Archivo", [portfolio.name for portfolio in portfolios])
    preview_portfolio = next(portfolio for portfolio in portfolios if portfolio.name == selected_preview)
    st.dataframe(preview_portfolio.dataframe.head(100), hide_index=True, use_container_width=True)

st.caption("Los archivos se procesan en memoria durante la sesión. No incluyas carteras reales dentro del repositorio de GitHub.")
