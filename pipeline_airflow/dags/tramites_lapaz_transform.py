from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import pandas as pd
import numpy as np
from pathlib import Path
import logging
from scipy import stats

# ------------------ CONFIG ------------------
DATA_DIR = Path.home() / "airflow_csv"
INPUT_API = DATA_DIR / "tramites_lapaz_api_identificador.csv"
INPUT_HTML = DATA_DIR / "tramites_lapaz_html_identificador.csv"
OUTPUT_FINAL = DATA_DIR / "tramites_lapaz_final.csv"

# ------------------ HELPERS ------------------
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

def eda_inicial(**kwargs):
    df_api = pd.read_csv(INPUT_API, dtype={"codigo_catastral": str})
    logging.info(f"EDA inicial: {df_api.shape}, columnas={len(df_api.columns)}")
    df_api.to_csv(DATA_DIR/"eda_api.csv", index=False)
    return str(DATA_DIR/"eda_api.csv")

def eliminar_duplicados(**kwargs):
    df = pd.read_csv(DATA_DIR/"eda_api.csv", dtype=str)
    df = df.sort_values("fecha_registro").drop_duplicates(subset=["codigo_catastral"], keep="last")
    df.to_csv(DATA_DIR/"api_sin_dup.csv", index=False)
    logging.info(f"Eliminados duplicados: {df.shape}")
    return str(DATA_DIR/"api_sin_dup.csv")

def left_merge(**kwargs):
    df_api = pd.read_csv(DATA_DIR/"api_sin_dup.csv", dtype=str)
    df_html = pd.read_csv(INPUT_HTML, dtype=str)
    df_merge = pd.merge(df_api, df_html, on="identificador_b64", how="left")
    df_merge.to_csv(DATA_DIR/"merged.csv", index=False)
    logging.info(f"Merge completado: {df_merge.shape}")
    return str(DATA_DIR/"merged.csv")

def filtrar_resultados(**kwargs):
    df = pd.read_csv(DATA_DIR/"merged.csv", dtype=str)
    df = df[df["resultado"].isin(["APROBADO", "OBSERVADO"])]
    df.to_csv(DATA_DIR/"filtrado.csv", index=False)
    logging.info(f"Filtrados resultados: {df['resultado'].value_counts().to_dict()}")
    return str(DATA_DIR/"filtrado.csv")

def eliminar_columnas(**kwargs):
    df = pd.read_csv(DATA_DIR/"filtrado.csv")
    cols_drop = ["id_pc_tramite","descripcion","numero_tramite","identificador",
                 "identificador_b64","codigo_catastral","fecha_registro","fecha_aprobacion",
                 "gral__codigo_catastral","gral__fecha_aprobacion","nombre_archivo","url",
                 "error","nombre_edificio","solicitante","arquitecto_nombre",
                 "otro__arquitecto_reponsable","arquitecto_registro_nacional_cab",
                 "id_ins_documento","fecha_registro_arch","nro_inmueble",
                 "estado_tramite","tipo_proyecto","tipo_obra","anio_registro",
                 "gral__informe","gral__normativa_segun_informe","otro__tipo_de_obra",
                 "otro__tipo_de_proyecto"]
    df = df.drop(columns=[c for c in cols_drop if c in df.columns])
    df.to_csv(DATA_DIR/"sin_columnas.csv", index=False)
    logging.info(f"Dataset reducido: {df.shape}")
    return str(DATA_DIR/"sin_columnas.csv")

def tratar_nulos(**kwargs):
    df = pd.read_csv(DATA_DIR/"sin_columnas.csv")
    df = df.dropna(axis=1, thresh=int(0.1*len(df)))  # drop col >90% nulos
    for col in ["gral__patron_de_asentamiento","gral__zona_referencial"]:
        if col in df.columns:
            df[col] = df[col].fillna("DESCONOCIDO")
    df.to_csv(DATA_DIR/"nulos.csv", index=False)
    logging.info("Tratamiento de nulos completo")
    return str(DATA_DIR/"nulos.csv")

def transformar_tipos(**kwargs):
    df = pd.read_csv(DATA_DIR/"nulos.csv")

    # id_tipo_tramite -> es_agil
    if "id_tipo_tramite" in df.columns:
        df["es_agil"] = df["id_tipo_tramite"].astype(float).fillna(0).astype(int) == 12

    # id_proyecto_desarrollo
    if "id_proyecto_desarrollo" in df.columns:
        df["id_proyecto_desarrollo"] = df["id_proyecto_desarrollo"].fillna(12).astype(int)

    # id_tipo_obra -> es_ampliacion
    if "id_tipo_obra" in df.columns:
        df["id_tipo_obra"] = df["id_tipo_obra"].fillna(1).astype(int)
        df["es_ampliacion"] = df["id_tipo_obra"] == 2

    # cantidad_pisos
    if "cantidad_pisos" in df.columns:
        df["cantidad_pisos"] = df["cantidad_pisos"].fillna(0).astype(int)

    # superficies
    if "superficie_legal" in df.columns:
        df = df.dropna(subset=["superficie_legal"])
    if "superficie_construida" in df.columns:
        df["superficie_construida"] = df["superficie_construida"].fillna(0)

    # redondeo coords
    for col in ["latitude","longitude"]:
        if col in df.columns:
            df[col] = df[col].round(6)

    # limpieza strings
    cols_string_limpieza = [c for c in df.columns if c.startswith("par__")]
    for col in cols_string_limpieza:
        df[col] = limpiar_unidades(df[col])

    # conversion categóricas
    for col in ["resultado","macro_distrito","distrito_municipal",
                "gral__patron_de_asentamiento","gral__zona_referencial"]:
        if col in df.columns:
            df[col] = df[col].astype("category")

    # drop ids auxiliares
    df = df.drop(columns=[c for c in ["id_tipo_tramite","id_tipo_obra"] if c in df.columns])

    df.to_csv(DATA_DIR/"tipos.csv", index=False)
    logging.info("Transformación de tipos finalizada")
    return str(DATA_DIR/"tipos.csv")

def detectar_outliers(**kwargs):
    df = pd.read_csv(DATA_DIR / "tipos.csv")

    # Seleccionar solo columnas numéricas
    num_cols = df.select_dtypes(include=["float64", "int64"]).columns
    excluir = ["latitude", "longitude", "id_proyecto_desarrollo"]
    num_cols = [c for c in num_cols if c not in excluir]

    outlier_cols = []
    for col in num_cols:
        z = np.abs(stats.zscore(df[col], nan_policy="omit"))
        if np.any(z > 3):
            outlier_cols.append(col)

    logging.info("📦 Columnas con outliers detectados:")
    logging.info(outlier_cols)

    # Guardar dataset tal cual (sin borrar filas)
    df.to_csv(OUTPUT_FINAL, index=False)
    logging.info(f"✅ Dataset final generado (sin eliminar filas) → {OUTPUT_FINAL}, {df.shape}")

    # Retornar lista de columnas con outliers (útil en XCom si querés usarla después)
    return outlier_cols

# ------------------ DAG ------------------
with DAG(
    dag_id="tramites_lapaz_transform",
    start_date=datetime(2025,10,1),
    schedule_interval=None,
    catchup=False,
    tags=["Transformacion","ETL"]
) as dag:

    t1 = PythonOperator(task_id="eda_inicial", python_callable=eda_inicial)
    t2 = PythonOperator(task_id="eliminar_duplicados", python_callable=eliminar_duplicados)
    t3 = PythonOperator(task_id="left_merge", python_callable=left_merge)
    t4 = PythonOperator(task_id="filtrar_resultados", python_callable=filtrar_resultados)
    t5 = PythonOperator(task_id="eliminar_columnas", python_callable=eliminar_columnas)
    t6 = PythonOperator(task_id="tratar_nulos", python_callable=tratar_nulos)
    t7 = PythonOperator(task_id="transformar_tipos", python_callable=transformar_tipos)
    t8 = PythonOperator(task_id="eliminar_outliers", python_callable=detectar_outliers)

    t1 >> t2 >> t3 >> t4 >> t5 >> t6 >> t7 >> t8
