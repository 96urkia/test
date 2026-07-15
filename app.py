"""
app.py
======
Aplicacion Streamlit para practicar test de oposiciones de biblioteca.

- Login contra una hoja de Google Sheets (columnas: usuario, contraseña, rol).
- La base de datos (examenes.db) YA NO vive en GitHub: se descarga desde
  Google Drive al iniciar sesión y, si un admin edita datos, se vuelve a
  subir a Drive automáticamente.
- Filtros por nivel, tipo_biblioteca y lugar (panel horizontal superior).
- Test de preguntas aleatorias con opciones en orden aleatorio (panel vertical).
- Panel de administración (solo rol "admin"): altas/bajas/ediciones de
  exámenes, preguntas y respuestas, más un editor SQL libre para casos
  que no cubra la interfaz (crear tablas nuevas, migraciones, etc.).

Requiere:
  - Un archivo "examenes.db" (generado con excel_a_db.py) subido a Google
    Drive y COMPARTIDO como Editor con el email de la cuenta de servicio
    (el mismo que usas en gcp_service_account).
  - Un Google Sheet con columnas "usuario", "contraseña" y "rol"
    ("admin" o "usuario"), compartido con esa misma cuenta de servicio.
  - Secretos de Streamlit (secrets.toml) configurados con:
      [gcp_service_account]  -> credenciales JSON de la cuenta de servicio
      sheet_id   = "..."     -> ID del Google Sheet de usuarios
      db_file_id = "..."     -> ID del archivo examenes.db en Google Drive
  - En Google Cloud Console: la Google Drive API debe estar habilitada
    para el proyecto de la cuenta de servicio (además de Sheets API).
  - requirements.txt debe incluir: google-api-python-client
"""

import io
import random
from datetime import datetime

import gspread
import pandas as pd
import sqlite3
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

st.set_page_config(page_title="Test Oposiciones Biblioteca", page_icon="📚", layout="wide")

st.markdown(
    """
    <style>
    .tarjeta-respuesta {
        border: 2px solid rgba(150, 150, 150, 0.4);
        border-radius: 16px;
        padding: 28px 16px;
        text-align: center;
        min-height: 150px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        margin-bottom: 6px;
        transition: border-color 0.2s ease, background-color 0.2s ease;
    }
    .tarjeta-respuesta .texto {
        font-size: 1.25rem;
        font-weight: 600;
        line-height: 1.35;
    }
    .tarjeta-seleccionada {
        border-color: #ff4b4b;
        background-color: rgba(255, 75, 75, 0.08);
    }
    .tarjeta-correcta {
        border-color: #21c354;
        background-color: rgba(33, 195, 84, 0.12);
    }
    .tarjeta-incorrecta {
        border-color: #ff4b4b;
        background-color: rgba(255, 75, 75, 0.12);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Copia de trabajo local: en Streamlit Cloud /tmp es escribible durante la sesión
DB_PATH = "/tmp/examenes.db"
DB_FILE_ID = st.secrets.get("db_file_id")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]


# ----------------------------------------------------------------------
# CLIENTES GOOGLE (Sheets para login, Drive para la base de datos)
# ----------------------------------------------------------------------

@st.cache_resource
def _credenciales_google():
    return Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=SCOPES)


@st.cache_resource
def _cliente_google_sheets():
    return gspread.authorize(_credenciales_google())


@st.cache_resource
def _cliente_drive():
    return build("drive", "v3", credentials=_credenciales_google())


# ----------------------------------------------------------------------
# BASE DE DATOS: descarga / subida a Google Drive
# ----------------------------------------------------------------------

def descargar_db_desde_drive():
    """Descarga examenes.db desde Drive a DB_PATH. Se llama una vez por sesión."""
    drive = _cliente_drive()
    solicitud = drive.files().get_media(fileId=DB_FILE_ID)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, solicitud)
    completado = False
    while not completado:
        _, completado = downloader.next_chunk()
    with open(DB_PATH, "wb") as f:
        f.write(buffer.getvalue())


def subir_db_a_drive():
    """Sube la copia local (ya modificada) de vuelta a Drive, sobrescribiendo el archivo."""
    drive = _cliente_drive()
    media = MediaFileUpload(DB_PATH, mimetype="application/x-sqlite3", resumable=True)
    drive.files().update(fileId=DB_FILE_ID, media_body=media).execute()
    st.session_state.db_actualizada_en = datetime.now().strftime("%H:%M:%S")
    # Limpia toda la caché de datos (más robusto que referenciar funciones concretas,
    # que en este punto del script pueden no estar definidas todavía)
    st.cache_data.clear()


def guardar_y_sincronizar():
    """Confirma la transacción sqlite local y sube el archivo actualizado a Drive."""
    subir_db_a_drive()


def _tabla_existe(cur, nombre):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nombre,))
    return cur.fetchone() is not None


def etiquetas_desde_input(texto_input: str):
    """El admin escribe 'Leyes, Historia, IFLA' separado por comas -> lista limpia."""
    return [t.strip() for t in (texto_input or "").split(",") if t.strip()]


def etiquetas_a_almacenamiento(lista_etiquetas):
    """Guarda como ',tag1,tag2,' para poder buscar con LIKE '%,tag,%' sin falsos positivos."""
    limpio = sorted(set(t for t in lista_etiquetas if t))
    return ("," + ",".join(limpio) + ",") if limpio else ""


def etiquetas_desde_almacenamiento(texto_guardado):
    if not isinstance(texto_guardado, str) or not texto_guardado.strip(","):
        return []
    return [t for t in texto_guardado.strip(",").split(",") if t]]


def etiquetas_como_texto_editable(texto_guardado: str):
    return ", ".join(etiquetas_desde_almacenamiento(texto_guardado))


def asegurar_esquema():
    """Migración idempotente: añade columnas/tablas nuevas si faltan. Se ejecuta una vez
    por sesión tras descargar la base de datos, así no hace falta tocar SQL a mano."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cambios = False

    columnas_preguntas = [fila[1] for fila in cur.execute("PRAGMA table_info(preguntas)").fetchall()]
    if "visible" not in columnas_preguntas:
        cur.execute("ALTER TABLE preguntas ADD COLUMN visible INTEGER DEFAULT 1")
        cambios = True
    if "etiquetas" not in columnas_preguntas:
        cur.execute("ALTER TABLE preguntas ADD COLUMN etiquetas TEXT")
        cambios = True

    if not _tabla_existe(cur, "informacion_extra"):
        cur.execute(
            """CREATE TABLE informacion_extra (
                   id_pregunta TEXT PRIMARY KEY,
                   contenido TEXT
               )"""
        )
        cambios = True

    if not _tabla_existe(cur, "preguntas_ocultas_usuario"):
        cur.execute(
            """CREATE TABLE preguntas_ocultas_usuario (
                   usuario TEXT NOT NULL,
                   id_pregunta TEXT NOT NULL,
                   PRIMARY KEY (usuario, id_pregunta)
               )"""
        )
        cambios = True

    if not _tabla_existe(cur, "historial_fallos"):
        cur.execute(
            """CREATE TABLE historial_fallos (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   usuario TEXT,
                   id_pregunta TEXT,
                   id_examen TEXT,
                   texto_pregunta TEXT,
                   respuesta_elegida TEXT,
                   respuesta_correcta TEXT,
                   fecha TEXT
               )"""
        )
        cambios = True

    con.commit()
    con.close()
    if cambios:
        subir_db_a_drive()


# ----------------------------------------------------------------------
# LOGIN (Google Sheets) — ahora también devuelve el rol
# ----------------------------------------------------------------------

def _obtener_usuarios():
    cliente = _cliente_google_sheets()
    hoja = cliente.open_by_key(st.secrets["sheet_id"]).sheet1
    return hoja.get_all_records()  # [{"usuario": ..., "contraseña": ..., "rol": ...}, ...]


def verificar_login(usuario: str, contraseña: str):
    """Devuelve el rol ('admin' / 'usuario') si las credenciales son correctas, o None."""
    try:
        registros = _obtener_usuarios()
    except Exception as e:
        st.error(f"No se pudo conectar con la hoja de usuarios: {e}")
        return None

    usuario = usuario.strip()
    for fila in registros:
        clave_usuario = fila.get("usuario", "")
        clave_pass = fila.get("contraseña", fila.get("contrasena", ""))
        if str(clave_usuario).strip() == usuario and str(clave_pass).strip() == contraseña:
            rol = str(fila.get("rol", "usuario")).strip().lower()
            return rol or "usuario"
    return None


def pantalla_login():
    st.title("🔐 Acceso")
    st.caption("Introduce tus credenciales.")
    with st.form("login_form"):
        usuario = st.text_input("Usuario")
        contraseña = st.text_input("Contraseña", type="password")
        enviado = st.form_submit_button("Entrar", type="primary")

    if enviado:
        if not usuario or not contraseña:
            st.warning("Rellena usuario y contraseña.")
        else:
            rol = verificar_login(usuario, contraseña)
            if rol is not None:
                st.session_state.autenticado = True
                st.session_state.usuario = usuario
                st.session_state.rol = rol
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")


for clave, valor in {"autenticado": False, "rol": "usuario", "db_lista": False}.items():
    if clave not in st.session_state:
        st.session_state[clave] = valor

if not st.session_state.autenticado:
    pantalla_login()
    st.stop()

# Descargar la base de datos desde Drive una vez por sesión, justo tras el login
if not st.session_state.db_lista:
    with st.spinner("Descargando base de datos desde Drive..."):
        try:
            descargar_db_desde_drive()
            asegurar_esquema()
            st.session_state.db_lista = True
        except Exception as e:
            st.error(f"No se pudo descargar examenes.db desde Drive: {e}")
            st.stop()

es_admin = st.session_state.rol == "admin"


# ----------------------------------------------------------------------
# CONSULTAS DE LECTURA (test)
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
    examenes_df = pd.read_sql_query(
        "SELECT id_examen, titulo, lugar, anio FROM examenes ORDER BY titulo", con
    )
    etiquetas_df = pd.read_sql_query(
        "SELECT etiquetas FROM preguntas WHERE etiquetas IS NOT NULL AND etiquetas <> ''", con
    )
    con.close()
    examenes_opciones = {
        f"{fila['titulo']} — {fila['lugar']} ({fila['anio']})": fila["id_examen"]
        for _, fila in examenes_df.iterrows()
    }
    todas_etiquetas = set()
    for valor in etiquetas_df["etiquetas"]:
        todas_etiquetas.update(etiquetas_desde_almacenamiento(valor))
    etiquetas_disponibles = sorted(todas_etiquetas)
    return niveles, tipos, lugares, examenes_opciones, etiquetas_disponibles


def obtener_pregunta_aleatoria(niveles_sel, tipos_sel, lugares_sel, examenes_sel=None, etiquetas_sel=None, usuario=None):
    con = sqlite3.connect(DB_PATH)
    # Las preguntas marcadas como no visibles quedan excluidas del test para todos los usuarios
    condiciones, params = ["(p.visible IS NULL OR p.visible = 1)"], []

    if niveles_sel:
        condiciones.append(f"e.nivel IN ({','.join(['?'] * len(niveles_sel))})")
        params += niveles_sel
    if tipos_sel:
        condiciones.append(f"e.tipo_biblioteca IN ({','.join(['?'] * len(tipos_sel))})")
        params += tipos_sel
    if lugares_sel:
        condiciones.append(f"e.lugar IN ({','.join(['?'] * len(lugares_sel))})")
        params += lugares_sel
    if examenes_sel:
        condiciones.append(f"e.id_examen IN ({','.join(['?'] * len(examenes_sel))})")
        params += examenes_sel
    if etiquetas_sel:
        sub = " OR ".join(["p.etiquetas LIKE ?"] * len(etiquetas_sel))
        condiciones.append(f"({sub})")
        params += [f"%,{t},%" for t in etiquetas_sel]
    if usuario:
        condiciones.append(
            "p.id_pregunta NOT IN (SELECT id_pregunta FROM preguntas_ocultas_usuario WHERE usuario = ?)"
        )
        params.append(usuario)

    where = "WHERE " + " AND ".join(condiciones)
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


def obtener_visible(id_pregunta):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT visible FROM preguntas WHERE id_pregunta = ?", con, params=(id_pregunta,))
    con.close()
    if df.empty or pd.isna(df.iloc[0]["visible"]):
        return True
    return bool(df.iloc[0]["visible"])


def obtener_etiquetas_pregunta(id_pregunta):
    """Devuelve las etiquetas de una pregunta como texto editable ('Leyes, IFLA')."""
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT etiquetas FROM preguntas WHERE id_pregunta = ?", con, params=(id_pregunta,))
    con.close()
    if df.empty or not df.iloc[0]["etiquetas"]:
        return ""
    return etiquetas_como_texto_editable(df.iloc[0]["etiquetas"])


def obtener_info_extra(id_pregunta):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT contenido FROM informacion_extra WHERE id_pregunta = ?", con, params=(id_pregunta,)
    )
    con.close()
    if df.empty or not df.iloc[0]["contenido"]:
        return ""
    return df.iloc[0]["contenido"]


def obtener_origen_pregunta(id_pregunta):
    """Devuelve un dict con los datos del examen del que procede la pregunta (o None)."""
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT e.id_examen, e.titulo, e.organismo, e.tipo_biblioteca, e.lugar, e.anio, e.nivel
           FROM preguntas p
           JOIN examenes e ON e.id_examen = p.id_examen
           WHERE p.id_pregunta = ?""",
        con, params=(id_pregunta,),
    )
    con.close()
    return None if df.empty else df.iloc[0].to_dict()


def ocultar_pregunta_para_usuario(usuario, id_pregunta):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO preguntas_ocultas_usuario (usuario, id_pregunta) VALUES (?, ?)",
        (usuario, id_pregunta),
    )
    con.commit()
    con.close()
    guardar_y_sincronizar()


def contar_preguntas_ocultas_usuario(usuario):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT COUNT(*) AS n FROM preguntas_ocultas_usuario WHERE usuario = ?", con, params=(usuario,)
    )
    con.close()
    return int(df.iloc[0]["n"])


def restablecer_preguntas_ocultas_usuario(usuario):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM preguntas_ocultas_usuario WHERE usuario = ?", (usuario,))
    con.commit()
    con.close()
    guardar_y_sincronizar()


def registrar_fallo(usuario, id_pregunta, texto_pregunta, respuesta_elegida, respuesta_correcta):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    id_examen = id_pregunta.split("-P")[0]
    cur.execute(
        """INSERT INTO historial_fallos
               (usuario, id_pregunta, id_examen, texto_pregunta, respuesta_elegida, respuesta_correcta, fecha)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (usuario, id_pregunta, id_examen, texto_pregunta, respuesta_elegida, respuesta_correcta,
         datetime.now().strftime("%d/%m/%Y %H:%M")),
    )
    con.commit()
    con.close()
    guardar_y_sincronizar()


def cargar_fallos_usuario(usuario):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM historial_fallos WHERE usuario = ? ORDER BY id DESC", con, params=(usuario,)
    )
    con.close()
    return df


def borrar_fallos_usuario(usuario):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM historial_fallos WHERE usuario = ?", (usuario,))
    con.commit()
    con.close()
    guardar_y_sincronizar()


def obtener_siguiente_de_repaso():
    """Devuelve la siguiente pregunta de la cola de repaso (st.session_state.cola_repaso),
    saltando las que hayan podido ser eliminadas mientras tanto. None si ya no quedan."""
    cola = st.session_state.get("cola_repaso", [])
    indice = st.session_state.get("indice_repaso", 0)
    while indice < len(cola):
        id_pregunta = cola[indice]
        indice += 1
        con = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(
            "SELECT texto_pregunta FROM preguntas WHERE id_pregunta = ?", con, params=(id_pregunta,)
        )
        con.close()
        if not df.empty:
            st.session_state.indice_repaso = indice
            return id_pregunta, df.iloc[0]["texto_pregunta"]
    st.session_state.indice_repaso = indice
    return None


# ----------------------------------------------------------------------
# CONSULTAS Y ACCIONES DE ADMINISTRACIÓN (solo rol admin)
# ----------------------------------------------------------------------

@st.cache_data(ttl=60)
def listar_examenes():
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT id_examen, organismo, tipo_biblioteca, lugar, anio, titulo, nivel FROM examenes ORDER BY id_examen",
        con,
    )
    con.close()
    return df


def cargar_examen(id_examen):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM examenes WHERE id_examen = ?", con, params=(id_examen,))
    con.close()
    return None if df.empty else df.iloc[0].to_dict()


def guardar_examen(datos: dict, es_nuevo: bool):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if es_nuevo:
        cur.execute(
            """INSERT INTO examenes (id_examen, organismo, tipo_biblioteca, lugar, anio, titulo, nivel)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datos["id_examen"], datos["organismo"], datos["tipo_biblioteca"], datos["lugar"],
             datos["anio"], datos["titulo"], datos["nivel"]),
        )
    else:
        cur.execute(
            """UPDATE examenes SET organismo=?, tipo_biblioteca=?, lugar=?, anio=?, titulo=?, nivel=?
               WHERE id_examen=?""",
            (datos["organismo"], datos["tipo_biblioteca"], datos["lugar"], datos["anio"],
             datos["titulo"], datos["nivel"], datos["id_examen"]),
        )
    con.commit()
    con.close()
    guardar_y_sincronizar()


def eliminar_examen(id_examen):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "DELETE FROM respuestas WHERE id_pregunta IN (SELECT id_pregunta FROM preguntas WHERE id_examen=?)",
        (id_examen,),
    )
    cur.execute("DELETE FROM preguntas WHERE id_examen=?", (id_examen,))
    cur.execute("DELETE FROM examenes WHERE id_examen=?", (id_examen,))
    con.commit()
    con.close()
    guardar_y_sincronizar()


def cargar_preguntas_examen(id_examen):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT id_pregunta, texto_pregunta, visible, etiquetas FROM preguntas WHERE id_examen = ? ORDER BY id_pregunta",
        con, params=(id_examen,),
    )
    con.close()
    return df


def cargar_respuestas_pregunta(id_pregunta):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT letra, texto_respuesta, es_correcta FROM respuestas WHERE id_pregunta = ? ORDER BY letra",
        con, params=(id_pregunta,),
    )
    con.close()
    return {fila["letra"]: fila for _, fila in df.iterrows()}


def siguiente_id_pregunta(id_examen):
    con = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT id_pregunta FROM preguntas WHERE id_examen = ?", con, params=(id_examen,))
    con.close()
    numeros = []
    for pid in df["id_pregunta"]:
        try:
            numeros.append(int(str(pid).split("-P")[-1]))
        except ValueError:
            continue
    siguiente = (max(numeros) + 1) if numeros else 1
    return f"{id_examen}-P{siguiente:02d}"


def guardar_pregunta(id_examen, id_pregunta, texto, respuestas: dict, correcta: str, es_nueva: bool,
                      visible: bool = True, info_extra: str = "", etiquetas_texto: str = ""):
    """respuestas = {'A': 'texto...', 'B': 'texto...', 'C': ..., 'D': ...}; correcta = letra correcta.
    visible=False oculta la pregunta del test para todos los usuarios.
    info_extra es el texto que se muestra con el botón '❓' una vez corregida la pregunta.
    etiquetas_texto es una cadena separada por comas escrita por el admin, ej: 'Leyes, IFLA'."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    etiquetas_guardado = etiquetas_a_almacenamiento(etiquetas_desde_input(etiquetas_texto))
    if es_nueva:
        cur.execute(
            "INSERT INTO preguntas (id_pregunta, id_examen, texto_pregunta, visible, etiquetas) VALUES (?, ?, ?, ?, ?)",
            (id_pregunta, id_examen, texto, 1 if visible else 0, etiquetas_guardado),
        )
    else:
        cur.execute(
            "UPDATE preguntas SET texto_pregunta=?, visible=?, etiquetas=? WHERE id_pregunta=?",
            (texto, 1 if visible else 0, etiquetas_guardado, id_pregunta),
        )

    cur.execute("DELETE FROM respuestas WHERE id_pregunta=?", (id_pregunta,))
    for letra, texto_resp in respuestas.items():
        cur.execute(
            "INSERT INTO respuestas (id_pregunta, letra, texto_respuesta, es_correcta) VALUES (?, ?, ?, ?)",
            (id_pregunta, letra, texto_resp, 1 if letra == correcta else 0),
        )

    info_extra = (info_extra or "").strip()
    if info_extra:
        cur.execute(
            """INSERT INTO informacion_extra (id_pregunta, contenido) VALUES (?, ?)
               ON CONFLICT(id_pregunta) DO UPDATE SET contenido = excluded.contenido""",
            (id_pregunta, info_extra),
        )
    else:
        cur.execute("DELETE FROM informacion_extra WHERE id_pregunta=?", (id_pregunta,))

    con.commit()
    con.close()
    guardar_y_sincronizar()


def eliminar_pregunta(id_pregunta):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM respuestas WHERE id_pregunta=?", (id_pregunta,))
    cur.execute("DELETE FROM preguntas WHERE id_pregunta=?", (id_pregunta,))
    con.commit()
    con.close()
    guardar_y_sincronizar()


def ejecutar_sql_libre(sentencia: str):
    """Ejecuta cualquier sentencia SQL (CREATE TABLE, ALTER, INSERT, UPDATE...).
    Si es una consulta SELECT, devuelve un DataFrame; si no, hace commit y sincroniza."""
    con = sqlite3.connect(DB_PATH)
    try:
        if sentencia.strip().lower().startswith("select"):
            df = pd.read_sql_query(sentencia, con)
            con.close()
            return df
        else:
            cur = con.cursor()
            cur.executescript(sentencia)
            con.commit()
            con.close()
            guardar_y_sincronizar()
            return None
    except Exception:
        con.close()
        raise


# ----------------------------------------------------------------------
# ESTADO DE SESION (test)
# ----------------------------------------------------------------------

valores_por_defecto = {
    "test_iniciado": False,
    "pregunta_actual": None,
    "opciones_actuales": None,
    "corregido": False,
    "aciertos": 0,
    "fallos": 0,
    "seleccion_actual": None,
    "editando_pregunta": False,
    "confirmar_eliminar_pregunta": False,
    "mostrar_info": False,
    "mostrar_origen": False,
    "modo_repaso": False,
    "cola_repaso": [],
    "indice_repaso": 0,
}
for clave, valor in valores_por_defecto.items():
    if clave not in st.session_state:
        st.session_state[clave] = valor


def nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel=None, etiquetas_sel=None):
    st.session_state.seleccion_actual = None
    st.session_state.editando_pregunta = False
    st.session_state.confirmar_eliminar_pregunta = False
    st.session_state.mostrar_info = False
    st.session_state.mostrar_origen = False

    if st.session_state.modo_repaso:
        resultado = obtener_siguiente_de_repaso()
    else:
        resultado = obtener_pregunta_aleatoria(
            niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel, st.session_state.usuario
        )
    if resultado is None:
        st.session_state.pregunta_actual = None
        st.session_state.opciones_actuales = None
        return
    id_pregunta, texto = resultado
    st.session_state.pregunta_actual = (id_pregunta, texto)
    st.session_state.opciones_actuales = obtener_respuestas(id_pregunta)
    st.session_state.corregido = False


# ----------------------------------------------------------------------
# CABECERA + NAVEGACIÓN
# ----------------------------------------------------------------------

cab_izq, cab_der = st.columns([5, 1])
with cab_izq:
    st.title("📚 Test Oposiciones Biblioteca")
with cab_der:
    st.write("")
    if st.button("🚪 Cerrar sesión"):
        st.session_state.clear()
        st.rerun()

opciones_navegacion = ["🧪 Test", "📉 Mis fallos"]
if es_admin:
    opciones_navegacion.append("⚙️ Administración")
if st.session_state.get("forzar_pagina"):
    st.session_state.nav_radio = st.session_state.forzar_pagina
    st.session_state.forzar_pagina = None

pagina = st.sidebar.radio("Navegación", opciones_navegacion, key="nav_radio")

st.sidebar.caption(f"Usuario: {st.session_state.usuario} ({st.session_state.rol})")
if st.session_state.get("db_actualizada_en"):
    st.sidebar.caption(f"Última sincronización con Drive: {st.session_state.db_actualizada_en}")

with st.sidebar.expander("🙈 Preguntas que ya no ves"):
    n_ocultas = contar_preguntas_ocultas_usuario(st.session_state.usuario)
    st.write(f"Tienes {n_ocultas} pregunta(s) ocultas para ti.")
    if n_ocultas and st.button("Volver a mostrarlas todas"):
        restablecer_preguntas_ocultas_usuario(st.session_state.usuario)
        st.rerun()


# ----------------------------------------------------------------------
# PÁGINA: TEST
# ----------------------------------------------------------------------

if pagina == "🧪 Test":
    if st.session_state.modo_repaso:
        total_repaso = len(st.session_state.cola_repaso)
        hechas_repaso = min(st.session_state.indice_repaso, total_repaso)
        st.info(f"🔁 Modo repaso de tus fallos — pregunta {hechas_repaso} de {total_repaso}")
        if st.button("❌ Salir del modo repaso"):
            st.session_state.modo_repaso = False
            st.session_state.test_iniciado = False
            st.session_state.pregunta_actual = None
            st.rerun()
        niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel = [], [], [], [], []
    else:
        niveles, tipos, lugares, examenes_opciones, etiquetas_disponibles = cargar_opciones_filtro()

        f1, f2, f3, f4, f5 = st.columns(5)
        with f1:
            niveles_sel = st.multiselect("Nivel", niveles)
        with f2:
            tipos_sel = st.multiselect("Tipo de biblioteca", tipos)
        with f3:
            lugares_sel = st.multiselect("Lugar", lugares)
        with f4:
            examenes_sel_nombres = st.multiselect("Examen concreto", list(examenes_opciones.keys()))
        with f5:
            etiquetas_sel = st.multiselect("Tema / etiqueta", etiquetas_disponibles)
        examenes_sel = [examenes_opciones[nombre] for nombre in examenes_sel_nombres]

    st.divider()

    st.session_state.filtros_actuales = (
        tuple(niveles_sel), tuple(tipos_sel), tuple(lugares_sel), tuple(examenes_sel), tuple(etiquetas_sel)
    )

    marcador_izq, marcador_der = st.columns(2)
    marcador_izq.metric("✅ Aciertos", st.session_state.aciertos)
    marcador_der.metric("❌ Fallos", st.session_state.fallos)

    if not st.session_state.test_iniciado:
        st.info("Ajusta los filtros si quieres (opcional) y pulsa Empezar test.")
        if st.button("▶️ Empezar test", type="primary"):
            st.session_state.test_iniciado = True
            st.session_state.aciertos = 0
            st.session_state.fallos = 0
            nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
            st.rerun()
    else:
        if st.session_state.pregunta_actual is None:
            if st.session_state.modo_repaso:
                st.success("🎉 Has repasado todas las preguntas de tu lista de fallos.")
                if st.button("Volver al test normal"):
                    st.session_state.modo_repaso = False
                    st.session_state.test_iniciado = False
                    st.rerun()
            else:
                st.warning("No hay preguntas disponibles con los filtros seleccionados.")
                if st.button("🔁 Reintentar"):
                    nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
                    st.rerun()
        else:
            id_pregunta, texto_pregunta = st.session_state.pregunta_actual

            if es_admin and not st.session_state.editando_pregunta and not st.session_state.confirmar_eliminar_pregunta:
                col_pregunta, col_editar, col_eliminar = st.columns([6, 1, 1])
                with col_pregunta:
                    st.subheader(texto_pregunta)
                with col_editar:
                    if st.button("✏️ Editar", key=f"editar_{id_pregunta}", use_container_width=True):
                        st.session_state.editando_pregunta = True
                        st.rerun()
                with col_eliminar:
                    if st.button("🗑️ Eliminar", key=f"eliminar_{id_pregunta}", use_container_width=True):
                        st.session_state.confirmar_eliminar_pregunta = True
                        st.rerun()
            else:
                st.subheader(texto_pregunta)

            if not st.session_state.editando_pregunta and not st.session_state.confirmar_eliminar_pregunta:
                if st.button("🙈 No volver a mostrarme esta pregunta", key=f"ocultar_yo_{id_pregunta}"):
                    ocultar_pregunta_para_usuario(st.session_state.usuario, id_pregunta)
                    nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
                    st.rerun()

            # --- Confirmación de borrado ------------------------------------
            if st.session_state.confirmar_eliminar_pregunta:
                st.warning(f"¿Seguro que quieres eliminar la pregunta **{id_pregunta}**? No se puede deshacer.")
                col_si, col_no = st.columns(2)
                with col_si:
                    if st.button("✅ Sí, eliminar", type="primary", key="confirmar_eliminar_si"):
                        eliminar_pregunta(id_pregunta)
                        st.session_state.confirmar_eliminar_pregunta = False
                        nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
                        st.success("Pregunta eliminada y sincronizada con Drive.")
                        st.rerun()
                with col_no:
                    if st.button("Cancelar", key="confirmar_eliminar_no"):
                        st.session_state.confirmar_eliminar_pregunta = False
                        st.rerun()

            # --- Edición inline de la pregunta ------------------------------
            elif st.session_state.editando_pregunta:
                respuestas_actuales = cargar_respuestas_pregunta(id_pregunta)
                visible_actual = obtener_visible(id_pregunta)
                info_actual = obtener_info_extra(id_pregunta)
                etiquetas_actual = obtener_etiquetas_pregunta(id_pregunta)
                letras = ["A", "B", "C", "D"]
                with st.form(f"form_editar_{id_pregunta}"):
                    nuevo_texto = st.text_area("Enunciado", value=texto_pregunta)
                    textos_resp = {}
                    for letra in letras:
                        valor_actual = respuestas_actuales.get(letra, {}).get("texto_respuesta", "")
                        textos_resp[letra] = st.text_input(f"Respuesta {letra}", value=valor_actual,
                                                            key=f"editar_resp_{id_pregunta}_{letra}")
                    correcta_actual = next(
                        (l for l, r in respuestas_actuales.items() if r.get("es_correcta")), "A"
                    )
                    correcta = st.radio("Respuesta correcta", letras, index=letras.index(correcta_actual),
                                         horizontal=True, key=f"editar_correcta_{id_pregunta}")
                    visible_chk = st.checkbox(
                        "👁️ Visible para todos en el test",
                        value=visible_actual,
                        help="Desmárcalo para dejar de mostrar esta pregunta a todos los usuarios "
                             "(por ejemplo, normativa regional que no te interese practicar).",
                        key=f"editar_visible_{id_pregunta}",
                    )
                    etiquetas_nuevas = st.text_input(
                        "🏷️ Etiquetas (separadas por comas, ej: Leyes, IFLA)",
                        value=etiquetas_actual, key=f"editar_etiquetas_{id_pregunta}",
                    )
                    info_nueva = st.text_area(
                        "ℹ️ Información complementaria (se muestra con el botón ❓ tras corregir)",
                        value=info_actual, height=100, key=f"editar_info_{id_pregunta}",
                    )
                    col_g, col_c = st.columns(2)
                    guardar_edicion = col_g.form_submit_button("💾 Guardar cambios", type="primary")
                    cancelar_edicion = col_c.form_submit_button("Cancelar")

                if guardar_edicion:
                    id_examen_actual = id_pregunta.split("-P")[0]
                    guardar_pregunta(id_examen_actual, id_pregunta, nuevo_texto, textos_resp, correcta, es_nueva=False,
                                      visible=visible_chk, info_extra=info_nueva, etiquetas_texto=etiquetas_nuevas)
                    st.session_state.editando_pregunta = False
                    if visible_chk:
                        st.session_state.pregunta_actual = (id_pregunta, nuevo_texto)
                        st.session_state.opciones_actuales = obtener_respuestas(id_pregunta)
                        st.session_state.seleccion_actual = None
                        st.success("Pregunta actualizada y sincronizada con Drive.")
                    else:
                        st.success("Pregunta actualizada, oculta y sincronizada con Drive. Pasando a la siguiente...")
                        nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
                    st.rerun()
                if cancelar_edicion:
                    st.session_state.editando_pregunta = False
                    st.rerun()

            # --- Panel normal: tarjetas de respuesta ------------------------
            else:
                opciones = st.session_state.opciones_actuales
                letras_mostradas = ["A", "B", "C", "D"][:len(opciones)]
                filas = [letras_mostradas[i:i + 2] for i in range(0, len(letras_mostradas), 2)]

                idx = 0
                for fila_letras in filas:
                    cols = st.columns(len(fila_letras))
                    for col, letra in zip(cols, fila_letras):
                        op = opciones[idx]
                        with col:
                            clases = "tarjeta-respuesta"
                            if st.session_state.corregido:
                                if op["es_correcta"]:
                                    clases += " tarjeta-correcta"
                                elif st.session_state.seleccion_actual == idx:
                                    clases += " tarjeta-incorrecta"
                            elif st.session_state.seleccion_actual == idx:
                                clases += " tarjeta-seleccionada"

                            st.markdown(
                                f"""<div class="{clases}">
                                        <div class="texto">{op['texto_respuesta']}</div>
                                    </div>""",
                                unsafe_allow_html=True,
                            )
                            if not st.session_state.corregido:
                                if st.button("Seleccionar", key=f"sel_{id_pregunta}_{idx}", use_container_width=True):
                                    st.session_state.seleccion_actual = idx
                                    st.rerun()
                        idx += 1

                seleccion = st.session_state.seleccion_actual

                col_corregir, col_siguiente = st.columns(2)

                with col_corregir:
                    if not st.session_state.corregido:
                        if st.button("✅ Corregir", disabled=seleccion is None, type="primary"):
                            st.session_state.corregido = True
                            elegida = opciones[seleccion]
                            if elegida["es_correcta"]:
                                st.session_state.aciertos += 1
                            else:
                                st.session_state.fallos += 1
                                correcta_texto = next((op["texto_respuesta"] for op in opciones if op["es_correcta"]), "")
                                registrar_fallo(st.session_state.usuario, id_pregunta, texto_pregunta,
                                                 elegida["texto_respuesta"], correcta_texto)
                            st.rerun()

                with col_siguiente:
                    if st.session_state.corregido:
                        if st.button("➡️ Siguiente pregunta", type="primary"):
                            nueva_pregunta(niveles_sel, tipos_sel, lugares_sel, examenes_sel, etiquetas_sel)
                            st.rerun()

                if st.session_state.corregido and seleccion is not None:
                    elegida = opciones[seleccion]
                    if elegida["es_correcta"]:
                        st.success("¡Correcto! ✅")
                    else:
                        correctas = [op["texto_respuesta"] for op in opciones if op["es_correcta"]]
                        if correctas:
                            st.error(f"Incorrecto ❌ — Respuesta correcta: {', '.join(correctas)}")
                        else:
                            st.warning("Incorrecto, y esta pregunta todavia no tiene respuesta correcta marcada en la base de datos.")

                if st.session_state.corregido:
                    info_texto = obtener_info_extra(id_pregunta)
                    col_info, col_origen = st.columns(2)
                    with col_info:
                        if info_texto:
                            if st.button("❓ Info adicional", key=f"info_{id_pregunta}", use_container_width=True):
                                st.session_state.mostrar_info = not st.session_state.mostrar_info
                    with col_origen:
                        if st.button("📖 Ver origen", key=f"origen_{id_pregunta}", use_container_width=True):
                            st.session_state.mostrar_origen = not st.session_state.mostrar_origen

                    if st.session_state.mostrar_info and info_texto:
                        st.info(info_texto)
                    if st.session_state.mostrar_origen:
                        origen = obtener_origen_pregunta(id_pregunta)
                        if origen:
                            st.caption(
                                f"📖 **{origen['titulo']}** — {origen['organismo']}, "
                                f"{origen['tipo_biblioteca']} ({origen['lugar']}, {origen['anio']}) · "
                                f"Nivel {origen['nivel']} · ID examen: {origen['id_examen']} · "
                                f"ID pregunta: {id_pregunta}"
                            )
                        else:
                            st.caption("No se ha encontrado el examen de origen de esta pregunta.")

        if st.button("⏹️ Terminar test"):
            st.session_state.test_iniciado = False
            st.session_state.pregunta_actual = None
            st.session_state.editando_pregunta = False
            st.session_state.confirmar_eliminar_pregunta = False
            st.session_state.modo_repaso = False
            st.rerun()


# ----------------------------------------------------------------------
# PÁGINA: MIS FALLOS (todos los usuarios)
# ----------------------------------------------------------------------

elif pagina == "📉 Mis fallos":
    st.subheader("📉 Preguntas que has fallado")
    fallos_df = cargar_fallos_usuario(st.session_state.usuario)

    if fallos_df.empty:
        st.info("Todavía no has fallado ninguna pregunta. ¡Sigue así!")
    else:
        st.caption(f"{len(fallos_df)} fallo(s) registrados.")
        col_repasar, col_borrar = st.columns(2)
        with col_repasar:
            if st.button("🔁 Repasar mis fallos", type="primary", use_container_width=True):
                ids_unicos = list(dict.fromkeys(fallos_df["id_pregunta"].tolist()))
                random.shuffle(ids_unicos)
                st.session_state.cola_repaso = ids_unicos
                st.session_state.indice_repaso = 0
                st.session_state.modo_repaso = True
                st.session_state.test_iniciado = True
                st.session_state.aciertos = 0
                st.session_state.fallos = 0
                nueva_pregunta([], [], [], [], [])
                st.session_state.forzar_pagina = "🧪 Test"
                st.rerun()
        with col_borrar:
            if st.button("🗑️ Borrar mi historial de fallos", use_container_width=True):
                borrar_fallos_usuario(st.session_state.usuario)
                st.rerun()

        for _, fila in fallos_df.iterrows():
            with st.expander(f"{fila['fecha']} — {fila['texto_pregunta'][:70]}"):
                origen = obtener_origen_pregunta(fila["id_pregunta"])
                if origen:
                    st.caption(
                        f"📖 {origen['titulo']} — {origen['organismo']}, {origen['tipo_biblioteca']} "
                        f"({origen['lugar']}, {origen['anio']}) · Nivel {origen['nivel']}"
                    )
                st.write(f"**Pregunta ({fila['id_pregunta']}):** {fila['texto_pregunta']}")
                st.error(f"Tu respuesta: {fila['respuesta_elegida']}")
                st.success(f"Respuesta correcta: {fila['respuesta_correcta']}")


# ----------------------------------------------------------------------
# PÁGINA: ADMINISTRACIÓN (solo rol admin)
# ----------------------------------------------------------------------

elif pagina == "⚙️ Administración":
    tab_examenes, tab_sql = st.tabs(["📖 Exámenes y preguntas", "🛠️ SQL avanzado"])

    # --- TAB 1: gestión de exámenes y preguntas -------------------------
    with tab_examenes:
        examenes_df = listar_examenes()
        opciones_examen = ["➕ Nuevo examen"] + examenes_df["id_examen"].tolist()
        seleccionado = st.selectbox("Selecciona un examen", opciones_examen)
        es_nuevo_examen = seleccionado == "➕ Nuevo examen"

        datos_examen = {} if es_nuevo_examen else cargar_examen(seleccionado)

        with st.form("form_examen"):
            st.markdown("#### Datos del examen")
            id_examen = st.text_input("id_examen", value="" if es_nuevo_examen else datos_examen["id_examen"],
                                       disabled=not es_nuevo_examen)
            organismo = st.text_input("Organismo", value=datos_examen.get("organismo", ""))
            tipo_biblioteca = st.text_input("Tipo de biblioteca", value=datos_examen.get("tipo_biblioteca", ""))
            lugar = st.text_input("Lugar", value=datos_examen.get("lugar", ""))
            anio = st.text_input("Año", value=str(datos_examen.get("anio", "")))
            titulo = st.text_input("Título", value=datos_examen.get("titulo", ""))
            nivel = st.text_input("Nivel", value=datos_examen.get("nivel", ""))
            guardar = st.form_submit_button("💾 Guardar examen", type="primary")

        if guardar:
            if es_nuevo_examen and not id_examen:
                st.warning("Indica un id_examen para el nuevo examen.")
            else:
                guardar_examen(
                    {"id_examen": id_examen or seleccionado, "organismo": organismo,
                     "tipo_biblioteca": tipo_biblioteca, "lugar": lugar, "anio": anio,
                     "titulo": titulo, "nivel": nivel},
                    es_nuevo=es_nuevo_examen,
                )
            st.success("Examen guardado y sincronizado con Drive.")
            st.rerun()

        if not es_nuevo_examen:
            if st.button("🗑️ Eliminar examen (y todas sus preguntas)"):
                eliminar_examen(seleccionado)
                st.success("Examen eliminado y sincronizado con Drive.")
                st.rerun()

            st.divider()
            st.markdown("#### Preguntas de este examen")

            preguntas_df = cargar_preguntas_examen(seleccionado)
            for _, fila in preguntas_df.iterrows():
                id_pregunta = fila["id_pregunta"]
                visible_actual = True if pd.isna(fila.get("visible")) else bool(fila.get("visible", 1))
                icono_oculta = "🙈 " if not visible_actual else ""
                with st.expander(f"{icono_oculta}{id_pregunta} — {fila['texto_pregunta'][:70]}"):
                    respuestas_actuales = cargar_respuestas_pregunta(id_pregunta)
                    info_actual = obtener_info_extra(id_pregunta)
                    etiquetas_actual = etiquetas_como_texto_editable(fila.get("etiquetas"))
                    with st.form(f"form_pregunta_{id_pregunta}"):
                        texto_pregunta = st.text_area("Enunciado", value=fila["texto_pregunta"])
                        letras = ["A", "B", "C", "D"]
                        textos_resp = {}
                        for letra in letras:
                            valor_actual = respuestas_actuales.get(letra, {}).get("texto_respuesta", "")
                            textos_resp[letra] = st.text_input(f"Respuesta {letra}", value=valor_actual,
                                                                key=f"resp_{id_pregunta}_{letra}")
                        correcta_actual = next(
                            (l for l, r in respuestas_actuales.items() if r.get("es_correcta")), "A"
                        )
                        correcta = st.radio("Respuesta correcta", letras,
                                             index=letras.index(correcta_actual),
                                             key=f"correcta_{id_pregunta}", horizontal=True)
                        visible_chk = st.checkbox(
                            "👁️ Visible para todos en el test",
                            value=visible_actual,
                            help="Desmárcalo para dejar de mostrar esta pregunta a todos los usuarios.",
                            key=f"visible_{id_pregunta}",
                        )
                        etiquetas_nuevas = st.text_input(
                            "🏷️ Etiquetas (separadas por comas, ej: Leyes, IFLA)",
                            value=etiquetas_actual, key=f"etiquetas_{id_pregunta}",
                        )
                        info_nueva = st.text_area(
                            "ℹ️ Información complementaria (se muestra con el botón ❓ tras corregir)",
                            value=info_actual, height=100, key=f"info_{id_pregunta}",
                        )
                        col_g, col_e = st.columns(2)
                        guardar_p = col_g.form_submit_button("💾 Guardar pregunta", type="primary")
                        eliminar_p = col_e.form_submit_button("🗑️ Eliminar pregunta")

                    if guardar_p:
                        guardar_pregunta(seleccionado, id_pregunta, texto_pregunta, textos_resp, correcta, es_nueva=False,
                                          visible=visible_chk, info_extra=info_nueva, etiquetas_texto=etiquetas_nuevas)
                        st.success("Pregunta guardada y sincronizada con Drive.")
                        st.rerun()
                    if eliminar_p:
                        eliminar_pregunta(id_pregunta)
                        st.success("Pregunta eliminada y sincronizada con Drive.")
                        st.rerun()

            st.divider()
            st.markdown("#### ➕ Añadir nueva pregunta")
            nuevo_id = siguiente_id_pregunta(seleccionado)
            st.caption(f"Se guardará con id: {nuevo_id}")
            with st.form("form_nueva_pregunta"):
                texto_nueva = st.text_area("Enunciado", key="texto_nueva_pregunta")
                letras = ["A", "B", "C", "D"]
                textos_resp_nuevos = {l: st.text_input(f"Respuesta {l}", key=f"nueva_resp_{l}") for l in letras}
                correcta_nueva = st.radio("Respuesta correcta", letras, key="nueva_correcta", horizontal=True)
                visible_nueva = st.checkbox("👁️ Visible para todos en el test", value=True, key="nueva_visible")
                etiquetas_nueva = st.text_input(
                    "🏷️ Etiquetas (separadas por comas, ej: Leyes, IFLA)", key="nueva_etiquetas",
                )
                info_nueva = st.text_area(
                    "ℹ️ Información complementaria (se muestra con el botón ❓ tras corregir)",
                    key="nueva_info", height=100,
                )
                crear = st.form_submit_button("💾 Crear pregunta", type="primary")

            if crear:
                if not texto_nueva or any(not v for v in textos_resp_nuevos.values()):
                    st.warning("Rellena el enunciado y las 4 respuestas.")
                else:
                    guardar_pregunta(seleccionado, nuevo_id, texto_nueva, textos_resp_nuevos, correcta_nueva, es_nueva=True,
                                      visible=visible_nueva, info_extra=info_nueva, etiquetas_texto=etiquetas_nueva)
                    st.success("Pregunta creada y sincronizada con Drive.")
                    st.rerun()

    # --- TAB 2: SQL libre (crear tablas, migraciones, arreglos puntuales) --
    with tab_sql:
        st.warning(
            "Modo avanzado: aquí puedes ejecutar cualquier sentencia SQL directamente "
            "sobre examenes.db (CREATE TABLE, ALTER TABLE, INSERT, UPDATE, DELETE...). "
            "Los cambios se suben a Drive al ejecutar. Úsalo con cuidado."
        )
        sentencia = st.text_area("Sentencia SQL", height=150,
                                  placeholder="Ej: CREATE TABLE notas (id_pregunta TEXT, comentario TEXT);")
        if st.button("▶️ Ejecutar", type="primary"):
            if not sentencia.strip():
                st.warning("Escribe alguna sentencia SQL.")
            else:
                try:
                    resultado = ejecutar_sql_libre(sentencia)
                    if resultado is not None:
                        st.dataframe(resultado, use_container_width=True)
                    else:
                        st.success("Ejecutado y sincronizado con Drive.")
                except Exception as e:
                    st.error(f"Error al ejecutar: {e}")
