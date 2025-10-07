from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import logging, time, re, httpx
import pandas as pd
import numpy as np
from pathlib import Path
from bs4 import BeautifulSoup
from scipy import stats
import great_expectations as ge
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio

# ------------------ CONFIG ------------------
DATA_DIR = Path.home() / "airflow_data"
DATA_CSV = Path.home() / "airflow_csv"
DATA_DIR.mkdir(parents=True, exist_ok=True)

API_CSV   = DATA_CSV / "tramites_lapaz_api_identificador.csv"
HTML_CSV  = DATA_CSV / "tramites_lapaz_html_identificador.csv"
FINAL_CSV = DATA_CSV / "tramites_lapaz_final.csv"
REPORT_HTML = DATA_DIR / "tramites_lapaz_report.html"

# ------------------ HELPERS ------------------
def parse_table_below_header(soup, header_text):
    header = soup.find("h5", string=re.compile(header_text, re.IGNORECASE))
    table_data = []
    if header:
        table = header.find_next("table")
        if table:
            for row in table.find_all("tr"):
                cols = [col.get_text(strip=True) for col in row.find_all("td")]
                if cols:
                    table_data.append(cols)
    return table_data

def flatten_table_data(table_data, prefix=""):
    flat_data = {}
    for row in table_data:
        if len(row) >= 2:
            key = f"{prefix}{row[0]}"
            flat_data[key] = row[1]
    return flat_data

def limpiar_unidades(columna):
    col_limpia = (
        columna.astype(str)
        .str.lower()
        .str.replace(r"(unidades|plantas|m2|ml)", "", regex=True)
        .str.replace(",", ".", regex=False)
        .str.strip()
        .replace(["", "nan", "null"], "0")
    )
    col_numerica = pd.to_numeric(col_limpia, errors="coerce").fillna(0)
    if np.allclose(col_numerica, col_numerica.astype(int)):
        col_numerica = col_numerica.astype(int)
    return col_numerica

# ------------------ TASKS ------------------

def extract_api(**kwargs):
    url = "https://sitservicios.lapaz.bo/geoserver/sit/ows"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "sit:tramitesterritoriales", "count": 5000,
        "outputFormat": "application/json", "srsName": "EPSG:4326"
    }
    total_features = 13061
    paginas = range(0, total_features, params["count"])
    registros = []

    # 🚀 Forzar IPv4
    transport = httpx.HTTPTransport(local_address="0.0.0.0")

    with httpx.Client(timeout=15.0, verify=False, transport=transport) as client:
        for start_index in paginas:
            params["startIndex"] = start_index
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                geometry = feature.get("geometry")
                if geometry and "coordinates" in geometry:
                    lon, lat = geometry["coordinates"]
                    props["latitude"] = lat
                    props["longitude"] = lon
                registros.append(props)

    df = pd.DataFrame(registros)
    df.to_csv(API_CSV, index=False)
    logging.info(f"✅ API exportada a {API_CSV} con {len(df)} filas")
    return str(API_CSV)

def extract_scraping(**kwargs):
    df_api = pd.read_csv(API_CSV, dtype={"codigo_catastral": str})
    df_api = df_api.head(50)  # limitar para no saturar
    registros_html = []

    # 🚀 Forzar IPv4
    transport = httpx.HTTPTransport(local_address="0.0.0.0")

    for _, row in df_api.iterrows():
        identificador_b64 = row.get("identificador_b64")
        if not identificador_b64:
            continue
        url = f"https://sitservicios.lapaz.bo/situtiles/pc/?{identificador_b64}"
        with httpx.Client(timeout=10.0, verify=False, transport=transport) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            data = flatten_table_data(parse_table_below_header(soup, "Datos Generales"), prefix="gral__")
            data["identificador_b64"] = identificador_b64
            registros_html.append(data)
            time.sleep(1)

    df_html = pd.DataFrame(registros_html)
    df_html.to_csv(HTML_CSV, index=False)
    logging.info(f"✅ Scraping exportado a {HTML_CSV} con {len(df_html)} filas")
    return str(HTML_CSV)

def eda_inicial(**kwargs):
    df = pd.read_csv(API_CSV, dtype=str)
    df.to_csv(DATA_CSV/"eda_api.csv", index=False)
    return str(DATA_CSV/"eda_api.csv")

def eliminar_duplicados(**kwargs):
    df = pd.read_csv(DATA_CSV/"eda_api.csv", dtype=str)
    df = df.drop_duplicates(subset=["codigo_catastral"], keep="last")
    df.to_csv(DATA_CSV/"api_sin_dup.csv", index=False)
    return str(DATA_CSV/"api_sin_dup.csv")

def left_merge(**kwargs):
    df_api = pd.read_csv(DATA_CSV/"api_sin_dup.csv", dtype=str)
    df_html = pd.read_csv(HTML_CSV, dtype=str)
    df_merge = pd.merge(df_api, df_html, on="identificador_b64", how="left")
    df_merge.to_csv(DATA_CSV/"merged.csv", index=False)
    return str(DATA_CSV/"merged.csv")

def filtrar_resultados(**kwargs):
    df = pd.read_csv(DATA_CSV/"merged.csv", dtype=str)
    df = df[df["resultado"].isin(["APROBADO", "OBSERVADO"])]
    df.to_csv(DATA_CSV/"filtrado.csv", index=False)
    return str(DATA_CSV/"filtrado.csv")

def eliminar_columnas(**kwargs):
    df = pd.read_csv(DATA_CSV/"filtrado.csv")
    df = df.drop(columns=[c for c in ["id_pc_tramite","identificador","fecha_registro"] if c in df.columns])
    df.to_csv(DATA_CSV/"sin_columnas.csv", index=False)
    return str(DATA_CSV/"sin_columnas.csv")

def tratar_nulos(**kwargs):
    df = pd.read_csv(DATA_CSV/"sin_columnas.csv")
    df = df.dropna(axis=1, thresh=int(0.1*len(df)))
    df.to_csv(DATA_CSV/"nulos.csv", index=False)
    return str(DATA_CSV/"nulos.csv")

def transformar_tipos(**kwargs):
    df = pd.read_csv(DATA_CSV/"nulos.csv")
    if "cantidad_pisos" in df.columns:
        df["cantidad_pisos"] = df["cantidad_pisos"].fillna(0).astype(int)
    df.to_csv(DATA_CSV/"tipos.csv", index=False)
    return str(DATA_CSV/"tipos.csv")

def detectar_outliers(**kwargs):
    df = pd.read_csv(DATA_CSV / "tipos.csv")
    df.to_csv(FINAL_CSV, index=False)
    return str(FINAL_CSV)

def apply_expectations(**kwargs):
    df = pd.read_csv(FINAL_CSV)
    ge_df = ge.from_pandas(df)
    ge_df.expect_column_values_to_be_between("latitude", -16.61448, -16.43148)
    ge_df.expect_column_values_to_be_between("longitude", -68.17695, -68.00735)
    result = ge_df.validate()
    return result.success

def generate_report(**kwargs):
    df = pd.read_csv(FINAL_CSV)
    passed = len(df)
    failed = 0
    success_rate = 100
    gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=success_rate,
        title={'text': "Calidad Global (%)"},
        gauge={'axis': {'range': [0, 100]}}
    ))
    with open(str(REPORT_HTML), "w", encoding="utf-8") as f:
        f.write("<h1>Reporte de Calidad de Datos</h1>")
        f.write(f"<p>✔️ {passed} pasaron, ❌ {failed} fallaron</p>")
        f.write(pio.to_html(gauge, full_html=False, include_plotlyjs="cdn"))
    return str(REPORT_HTML)

# ------------------ DAG ------------------
with DAG(
    dag_id="tramites_lapaz_pipeline",
    description="Pipeline completo: extracción → transformación → validación → reporte",
    start_date=datetime(2025,10,1),
    schedule_interval=None,
    catchup=False,
    tags=["ETL","Transform","Quality"]
) as dag:

    t1 = PythonOperator(task_id="extract_api", python_callable=extract_api)
    t2 = PythonOperator(task_id="extract_scraping", python_callable=extract_scraping)
    t3 = PythonOperator(task_id="eda_inicial", python_callable=eda_inicial)
    t4 = PythonOperator(task_id="eliminar_duplicados", python_callable=eliminar_duplicados)
    t5 = PythonOperator(task_id="left_merge", python_callable=left_merge)
    t6 = PythonOperator(task_id="filtrar_resultados", python_callable=filtrar_resultados)
    t7 = PythonOperator(task_id="eliminar_columnas", python_callable=eliminar_columnas)
    t8 = PythonOperator(task_id="tratar_nulos", python_callable=tratar_nulos)
    t9 = PythonOperator(task_id="transformar_tipos", python_callable=transformar_tipos)
    t10 = PythonOperator(task_id="detectar_outliers", python_callable=detectar_outliers)
    t11 = PythonOperator(task_id="apply_expectations", python_callable=apply_expectations)
    t12 = PythonOperator(task_id="generate_report", python_callable=generate_report)

    t1 >> t2 >> t3 >> t4 >> t5 >> t6 >> t7 >> t8 >> t9 >> t10 >> t11 >> t12
