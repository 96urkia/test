"""
app.py
======
Aplicacion Streamlit para practicar test de oposiciones de biblioteca.

- Login contra una hoja de Google Sheets (columnas: usuario, contraseña).
- Filtros por nivel, tipo_biblioteca y lugar (panel horizontal superior).
- Test de preguntas aleatorias con opciones en orden aleatorio (panel vertical).

Requiere:
  - Un archivo "examenes.db" (generado con excel_a_db.py) en la misma carpeta
    que este script (o cambia DB_PATH mas abajo).
  - Un Google Sheet con columnas "usuario" y "contraseña", compartido con el
    email de una cuenta de servicio de Google Cloud.
  - Los secretos de Streamlit (ver README.md) configurados con:
      [gcp_service_account]  -> credenciales JSON de la cuenta de servicio
      sheet_id = "..."       -> ID del Google Sheet de usuarios
"""

import random

import gspread
import pandas as pd
import sqlite3
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Test Oposiciones Biblioteca", page_icon="📚", layout="wide")

DB_PATH = "examenes.db"


# ----------------------------------------------------------------------
# LOGIN (Google Sheets)
# ----------------------------------------------------------------------

@st.cache_resource
def _cliente_google_sheets():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)


def _obtener_usuarios():
    cliente = _cliente_google_sheets()
    hoja = cliente.open_by_key(st.secrets["sheet_id"]).sheet1
    return hoja.get_all_records()  # lista de dicts: [{"usuario": ..., "contraseña": ...}, ...]


def verificar_login(usuario: str, contraseña: str) -> bool:
    try:
        registros = _obtener_usuarios()
    except Exception as e:
        st.error(f"No se pudo conectar con la hoja de usuarios: {e}")
        return False

    usuario = usuario.strip()
    for fila in registros:
        # admite "contraseña" o "contrasena" como nombre de columna
        clave_usuario = fila.get("usuario", "")
        clave_pass = fila.get("contraseña", fila.get("contrasena", ""))
        if str(clave_usuario).strip() == usuario and str(clave_pass).strip() == contraseña:
            return True
    return False


def pantalla_login():
    st.title("🔐 Acceso — Test Oposiciones Biblioteca")
    st.caption("Introduce tus credenciales para acceder al test.")
    with st.form("login_form"):
        usuario = st.text_input("Usuario")
        contraseña = st.text_input("Contraseña", type="password")
        enviado = st.form_submit_button("Entrar", type="primary")

    if enviado:
        if not usuario or not contraseña:
            st.warning("Rellena usuario y contraseña.")
        elif verificar_login(usuario, contraseña):
            st.session_state.autenticado = True
            st.session_state.usuario = usuario
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")


if "autenticado" not in st.session_state:
    st.session_state.autenticado = False

if not st.session_state.autenticado:
    pantalla_login()
    st.stop()


# ----------------------------------------------------------------------
# ACCESO A LA BASE DE DATOS
# ----------------------------------------------------------------------

@st.cache_data(ttl=300)
def cargar_opciones_filtro():
    con = sqlite3.connect(DB_PATH)
    niveles = pd.read_sql_query(
        "SELECT DISTINCT nivel FROM examenes WHERE nivel IS NOT NULL AND nivel <> '' ORDER BY nivel", con
    )["nivel"].tolist()
    tipos = pd.read_sql_query(
        "SELECT DISTINCT tipo_biblioteca FROM examenes WHERE tipo_biblioteca IS NOT NULL AND tipo_biblioteca <> '' ORDER BY tipo_biblioteca", con
    )["tipo_biblioteca"].tolist()
    lugares = pd.read_sql_query(
        "SELECT DISTINCT lugar FROM examenes WHERE lugar IS NOT NULL AND lugar <> '' ORDER BY lugar", con
    )["lugar"].tolist()
    con.close()
    return niveles, tipos, lugares


def obtener_pregunta_aleatoria(niveles_sel, tipos_sel, lugares_sel):
    con = sqlite3.connect(DB_PATH)
    condiciones, params = [], []

    if niveles_sel:
        condiciones.append(f"e.nivel IN ({','.join(['?'] * len(niveles_sel))})")
        params += niveles_sel
    if tipos_sel:
        condiciones.append(f"e.tipo_biblioteca IN ({','.join(['?'] * len(tipos_sel))})")
        params += tipos_sel
    if lugares_sel:
        condiciones.append(f"e.lugar IN ({','.join(['?'] * len(lugares_sel))})")
        params += lugares_sel

    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    query = f"""
        SELECT p.id_pregunta, p.texto_pregunta
        FROM preguntas p
        JOIN examenes e ON e.id_examen = p.id_examen
        {where}
    """
    df = pd.read_sql_query(query, con, params=params)
    con.close()

    if df.empty:
        return None
    fila = df.sample(1).iloc[0]
    return fila["id_pregunta"], fila["texto_pregunta"]


def obtener_respuestas(id_pregunta):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT letra, texto_respuesta, es_correcta FROM respuestas WHERE id_pregunta = ?",
        con,
        params=(id_pregunta,),
    )
    con.close()
    opciones = df.to_dict("records")
    random.shuffle(opciones)
    return opciones


# ----------------------------------------------------------------------
# ESTADO DE SESION
# ----------------------------------------------------------------------

valores_por_defecto = {
    "test_iniciado": False,
    "pregunta_actual": None,
    "opciones_actuales": None,
    "corregido": False,
    "aciertos": 0,
    "fallos": 0,
}
for clave, valor in valores_por_defecto.items():
    if clave not in st.session_state:
        st.session_state[clave] = valor


def nueva_pregunta(niveles_sel, tipos_sel, lugares_sel):
    resultado = obtener_pregunta_aleatoria(niveles_sel, tipos_sel, lugares_sel)
    if resultado is None:
        st.session_state.pregunta_actual = None
        st.session_state.opciones_actuales = None
        return
    id_pregunta, texto = resultado
    st.session_state.pregunta_actual = (id_pregunta, texto)
    st.session_state.opciones_actuales = obtener_respuestas(id_pregunta)
    st.session_state.corregido = False


# ----------------------------------------------------------------------
# CABECERA + PANEL HORIZONTAL DE FILTROS
# ----------------------------------------------------------------------

cab_izq, cab_der = st.columns([5, 1])
with cab_izq:
    st.title("📚 Test Oposiciones Biblioteca")
with cab_der:
    st.write("")
    if st.button("🚪 Cerrar sesión"):
        st.session_state.clear()
        st.rerun()

niveles, tipos, lugares = cargar_opciones_filtro()

f1, f2, f3 = st.columns(3)
with f1:
    niveles_sel = st.multiselect("Nivel", niveles)
with f2:
    tipos_sel = st.multiselect("Tipo de biblioteca", tipos)
with f3:
    lugares_sel = st.multiselect("Lugar", lugares)

st.divider()

# Si cambian los filtros durante un test en marcha, se guarda para usarlos
# en la siguiente pregunta que se pida.
st.session_state.filtros_actuales = (tuple(niveles_sel), tuple(tipos_sel), tuple(lugares_sel))


# ----------------------------------------------------------------------
# PANEL VERTICAL: TEST
# ----------------------------------------------------------------------

marcador_izq, marcador_der = st.columns(2)
marcador_izq.metric("✅ Aciertos", st.session_state.aciertos)
marcador_der.metric("❌ Fallos", st.session_state.fallos)

if not st.session_state.test_iniciado:
    st.info("Ajusta los filtros si quieres (opcional) y pulsa Empezar test.")
    if st.button("▶️ Empezar test", type="primary"):
        st.session_state.test_iniciado = True
        st.session_state.aciertos = 0
        st.session_state.fallos = 0
        nueva_pregunta(niveles_sel, tipos_sel, lugares_sel)
        st.rerun()
else:
    if st.session_state.pregunta_actual is None:
        st.warning("No hay preguntas disponibles con los filtros seleccionados.")
        if st.button("🔁 Reintentar"):
            nueva_pregunta(niveles_sel, tipos_sel, lugares_sel)
            st.rerun()
    else:
        id_pregunta, texto_pregunta = st.session_state.pregunta_actual
        st.subheader(texto_pregunta)

        etiquetas = [op["texto_respuesta"] for op in st.session_state.opciones_actuales]
        seleccion = st.radio(
            "Selecciona una respuesta:",
            options=list(range(len(etiquetas))),
            format_func=lambda i: etiquetas[i],
            index=None,
            key=f"radio_{id_pregunta}",
            disabled=st.session_state.corregido,
        )

        col_corregir, col_siguiente = st.columns(2)

        with col_corregir:
            if not st.session_state.corregido:
                if st.button("✅ Corregir", disabled=seleccion is None, type="primary"):
                    st.session_state.corregido = True
                    elegida = st.session_state.opciones_actuales[seleccion]
                    if elegida["es_correcta"]:
                        st.session_state.aciertos += 1
                    else:
                        st.session_state.fallos += 1
                    st.rerun()

        with col_siguiente:
            if st.session_state.corregido:
                if st.button("➡️ Siguiente pregunta", type="primary"):
                    nueva_pregunta(niveles_sel, tipos_sel, lugares_sel)
                    st.rerun()

        if st.session_state.corregido and seleccion is not None:
            elegida = st.session_state.opciones_actuales[seleccion]
            if elegida["es_correcta"]:
                st.success("¡Correcto! ✅")
            else:
                correctas = [
                    op["texto_respuesta"]
                    for op in st.session_state.opciones_actuales
                    if op["es_correcta"]
                ]
                if correctas:
                    st.error(f"Incorrecto ❌ — Respuesta correcta: {', '.join(correctas)}")
                else:
                    st.warning("Incorrecto, y esta pregunta todavia no tiene respuesta correcta marcada en la base de datos.")

    if st.button("⏹️ Terminar test"):
        st.session_state.test_iniciado = False
        st.session_state.pregunta_actual = None
        st.rerun()
