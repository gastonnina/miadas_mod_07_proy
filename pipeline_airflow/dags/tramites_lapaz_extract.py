from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import logging
import httpx
import pandas as pd
from pathlib import Path
from bs4 import BeautifulSoup
import re
import time

# ------------------ CONFIG ------------------
MODO_TEST = False   # Producción
DATA_DIR = Path.home() / "airflow_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_API = DATA_DIR / "tramites_lapaz_api_identificador.csv"
OUTPUT_HTML = DATA_DIR / "tramites_lapaz_html_identificador.csv"

# ------------------ LOGGING ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

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

# ------------------ TASK 1: API ------------------
def extract_api(**kwargs):
    url = "https://sitservicios.lapaz.bo/geoserver/sit/ows"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "sit:tramitesterritoriales",
        "count": 5000,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
    }

    total_features = 13061
    paginas = range(0, total_features, params["count"])

    logging.info(f"🌐 Descargando {total_features} registros desde API en bloques de {params['count']}...")
    registros = []
    transport = httpx.HTTPTransport(local_address="0.0.0.0")

    try:
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
                logging.info(f"✅ Página {start_index} → acumulados {len(registros)} registros")

        df = pd.DataFrame(registros)
        df.to_csv(OUTPUT_API, index=False, encoding="utf-8")
        logging.info(f"✅ API exportada a {OUTPUT_API} con {len(df)} filas")

    except Exception as e:
        logging.error(f"❌ Error al obtener datos de la API: {e}")
        pd.DataFrame().to_csv(OUTPUT_API, index=False)
        logging.warning("⚠️ Se creó un CSV vacío porque la API no respondió")

    return str(OUTPUT_API)

# ------------------ TASK 2: SCRAPING ------------------
def extract_scraping(**kwargs):
    try:
        df_api = pd.read_csv(OUTPUT_API, dtype={"codigo_catastral": str})
    except Exception as e:
        logging.error(f"❌ No se pudo leer {OUTPUT_API}: {e}")
        pd.DataFrame().to_csv(OUTPUT_HTML, index=False)
        return str(OUTPUT_HTML)

    if df_api.empty:
        logging.warning("⚠️ CSV de API está vacío, no hay nada que scrapear")
        pd.DataFrame().to_csv(OUTPUT_HTML, index=False)
        return str(OUTPUT_HTML)

    # 🔎 igual que en tu notebook
    estado_col = next((c for c in ["resultado", "estado_tramite", "descripcion"] if c in df_api.columns), None)
    if estado_col:
        df_api = df_api[df_api[estado_col].astype(str).str.upper().str.contains("APROBADO", na=False)]
        logging.info(f"Filtrados {len(df_api)} trámites aprobados para scraping")
    else:
        logging.warning("⚠️ No encontré columna de estado, continuo con todos los trámites")

    # Limitar a 100 registros para evitar sobrecarga
    df_api = df_api.head(100)

    registros_html = []
    transport = httpx.HTTPTransport(local_address="0.0.0.0")

    for _, row in df_api.iterrows():
        identificador_b64 = row.get("identificador_b64")
        if not identificador_b64:
            continue
        url = f"https://sitservicios.lapaz.bo/situtiles/pc/?{identificador_b64}"
        try:
            with httpx.Client(timeout=10.0, verify=False, transport=transport) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                data = flatten_table_data(parse_table_below_header(soup, "Datos Generales"), prefix="gral__")
                data["identificador_b64"] = identificador_b64
                data["codigo_catastral"] = row.get("codigo_catastral")
                registros_html.append(data)
                time.sleep(1)  # pausa entre requests
        except Exception as e:
            logging.error(f"Error scraping {identificador_b64}: {e}")

    if registros_html:
        df_html = pd.DataFrame(registros_html)
        df_html.to_csv(OUTPUT_HTML, index=False, encoding="utf-8")
        logging.info(f"✅ Scraping exportado a {OUTPUT_HTML} con {len(df_html)} filas")
    else:
        pd.DataFrame().to_csv(OUTPUT_HTML, index=False)
        logging.warning("⚠️ No se pudo scrapear nada, se creó CSV vacío")

    return str(OUTPUT_HTML)


# ------------------ DAG ------------------
with DAG(
    dag_id="tramites_lapaz_extract",
    description="ETL de trámites La Paz (API + Scraping hasta 100 aprobados)",
    start_date=datetime(2025, 10, 1),
    schedule_interval=None,
    catchup=False,
    tags=["ETL", "Tramites", "LaPaz", "Prod"],
) as dag:

    t1_api = PythonOperator(
        task_id="extract_api",
        python_callable=extract_api,
    )

    t2_scraping = PythonOperator(
        task_id="extract_scraping",
        python_callable=extract_scraping,
    )

    t1_api >> t2_scraping
