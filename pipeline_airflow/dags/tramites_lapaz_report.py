from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import pandas as pd
import great_expectations as ge
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from pathlib import Path

# -------------------------
# Funciones del pipeline
# -------------------------
DATA_DIR = Path.home() / "airflow_data"
INPUT_CSV = DATA_DIR / "df_modelo_limpio_1.csv"
OUTPUT_REPORT = str(DATA_DIR / "tramites_lapaz_report.html")

def load_data(**context):
    df = pd.read_csv(INPUT_CSV)
    context['ti'].xcom_push(key="data", value=df.to_json())
    return "Datos cargados"

def apply_expectations(**context):
    df_json = context['ti'].xcom_pull(key="data")
    df = pd.read_json(df_json)

    ge_df = ge.from_pandas(df)

    # Expectativas (adaptadas de tu script)
    ge_df.expect_column_values_to_be_between("latitude", -16.61448, -16.43148)
    ge_df.expect_column_values_to_be_between("longitude", -68.17695, -68.00735)
    ge_df.expect_column_values_to_be_between("cantidad_pisos", 0, 40)
    ge_df.expect_column_values_to_be_between("superficie_legal", 0, 5000)
    ge_df.expect_column_values_to_be_between("superficie_construida", 0, 35000)
    ge_df.expect_column_values_to_be_in_set("resultado", ["APROBADO", "OBSERVADO"])
    ge_df.expect_column_values_to_not_be_null("macro_distrito")
    ge_df.expect_column_values_to_not_be_null("distrito_municipal")
    ge_df.expect_column_values_to_be_in_set("es_agil", [True, False])
    ge_df.expect_column_values_to_be_in_set("es_ampliacion", [True, False])

    result = ge_df.validate()
    context['ti'].xcom_push(key="validation", value=result.to_json_dict())
    return f"Validación completada: {result.success}"

def generate_report(**context):
    df_json = context['ti'].xcom_pull(key="data")
    df = pd.read_json(df_json)
    ge_result = context['ti'].xcom_pull(key="validation")

    # Resumen simple
    total = len(ge_result["results"])
    passed = sum(1 for r in ge_result["results"] if r["success"])
    failed = total - passed
    success_rate = round((passed / total) * 100, 2)

    gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=success_rate,
        title={'text': "Calidad Global (%)"},
        gauge={'axis': {'range': [0, 100]}}
    ))

    figs = [gauge]
    if "cantidad_pisos" in df.columns:
        figs.append(px.histogram(df, x="cantidad_pisos", title="Distribución de cantidad de pisos"))
    if "superficie_construida" in df.columns:
        figs.append(px.box(df, y="superficie_construida", title="Superficie construida"))

    with open(OUTPUT_REPORT, "w", encoding="utf-8") as f:
        f.write("<h1>Reporte de Calidad de Datos</h1>")
        f.write(f"<p>✔️ {passed} pasaron, ❌ {failed} fallaron, 🎯 Éxito global: {success_rate}%</p>")
        for fig in figs:
            f.write(pio.to_html(fig, full_html=False, include_plotlyjs="cdn"))

    return OUTPUT_REPORT

# -------------------------
# DAG Definition
# -------------------------
with DAG(
    dag_id="tramites_lapaz_report",
    start_date=datetime(2023, 1, 1),
    schedule_interval=None,
    catchup=False,
    tags=["quality", "great_expectations"]
) as dag:

    t1 = PythonOperator(
        task_id="load_data",
        python_callable=load_data,
        provide_context=True
    )

    t2 = PythonOperator(
        task_id="apply_expectations",
        python_callable=apply_expectations,
        provide_context=True
    )

    t3 = PythonOperator(
        task_id="generate_report",
        python_callable=generate_report,
        provide_context=True
    )

    t1 >> t2 >> t3
