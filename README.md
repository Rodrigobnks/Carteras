# Comparador de carteras en Streamlit

Tablero para cargar de una a tres carteras, homologar una llave escrita en lenguaje flexible y generar reportes descargables.

## Qué hace

- Reconoce nombres como `LATAM 2026-08-12.xlsx` y `PRESICO 2026-08-12.xlsx`.
- Si no existe una columna de país, agrega `México`; en archivos `PRESICO` fija el país como México.
- Usa por defecto `Marca & Pais & id_desembolso & tipo_desembolso & dbc_cliente_id`.
- Tolera diferencias de mayúsculas, acentos, espacios y guiones en los nombres de columnas.
- Permite corregir manualmente las columnas que interpretó para la llave.
- Compara carteras consecutivas para encontrar clientes que entraron o salieron.
- Cuenta cambios de `Al corriente` a `Atraso` y viceversa. Atraso significa `dias_de_atraso > 0`.
- Detecta llaves repetidas y filas con componentes incompletos.
- Detecta clientes con más de un desembolso y muestra IDs y tipos de desembolso.
- Descarga cada detalle en Excel o CSV y genera un Excel consolidado con todas las pestañas.

## Archivos que debes subir a GitHub

Sube todo el contenido de esta carpeta, conservando la subcarpeta `.streamlit`:

```text
cartera_streamlit_dashboard/
├── .streamlit/
│   └── config.toml
├── .gitignore
├── cartera_core.py
├── requirements.txt
├── README.md
└── streamlit_app.py
```

No subas archivos reales de cartera al repositorio. Los usuarios los cargarán desde la pantalla de la app.

## Ejecutar en tu computadora

Necesitas Python 3.11 o superior.

```bash
python -m venv .venv
```

En Windows:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run streamlit_app.py
```

En macOS o Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Publicar desde GitHub con Streamlit Community Cloud

1. Crea un repositorio nuevo en GitHub.
2. Sube los archivos indicados arriba a la raíz del repositorio.
3. Entra a [Streamlit Community Cloud](https://share.streamlit.io/) e inicia sesión con GitHub.
4. Crea una app, selecciona el repositorio, la rama y `streamlit_app.py` como archivo principal.
5. Pulsa **Deploy**. Las dependencias se instalarán desde `requirements.txt`.

## Reglas importantes del análisis

- Las comparaciones se hacen entre cortes consecutivos en el orden mostrado.
- Si el nombre contiene una fecha `AAAA-MM-DD`, la app puede ordenar los archivos por esa fecha.
- Una llave se excluye de comparaciones cuando cualquiera de sus componentes está vacío; esas filas aparecen en **Calidad de datos**.
- Si una llave se repite en un corte, el estado del cliente se calcula con el máximo `dias_de_atraso` de sus filas.
- Para detectar múltiples desembolsos, la app quita `id_desembolso` y `tipo_desembolso` de la llave activa. Con la llave por defecto, la identidad de cliente queda como `Marca & Pais & dbc_cliente_id`.
- El formato de descarga Excel usa hojas separadas, filtros y encabezados congelados.

## Privacidad

La aplicación no guarda archivos por sí sola: los procesa en la memoria de la sesión. Aun así, antes de desplegar carteras sensibles en una nube pública, valida las políticas de seguridad y tratamiento de datos de tu organización.
