# Test Oposiciones Biblioteca — despliegue en Streamlit

Esta guía explica cómo subir la app a GitHub y publicarla gratis en
**Streamlit Community Cloud**, con login contra un Google Sheet.

## 1. Estructura del repositorio

Crea un repositorio en GitHub (puede ser privado) con esta estructura:

```
tu-repo/
├── app.py
├── requirements.txt
├── examenes.db          <- tu base de datos generada con excel_a_db.py
└── .streamlit/
    └── secrets.toml      <- SOLO EN LOCAL, no lo subas a GitHub
```

Añade un `.gitignore` con al menos:

```
.streamlit/secrets.toml
__pycache__/
*.pyc
```

## 2. Crear la cuenta de servicio de Google (para el login)

1. Ve a [Google Cloud Console](https://console.cloud.google.com/) y crea (o
   reutiliza) un proyecto.
2. Activa la **Google Sheets API** para ese proyecto.
3. En "Credenciales" crea una **cuenta de servicio** (Service Account).
4. Dentro de la cuenta de servicio, crea una **clave** en formato JSON y
   descárgala. Contiene campos como `client_email` y `private_key`.
5. Abre tu Google Sheet de usuarios (columnas `usuario` y `contraseña`) y
   compártelo (botón "Compartir") con el email que aparece en
   `client_email` del JSON, dándole permiso de **lector**.
6. Copia el ID de la hoja: es la parte de la URL entre `/d/` y `/edit`,
   por ejemplo:
   `https://docs.google.com/spreadsheets/d/ESTE_ES_EL_ID/edit`

## 3. Configurar los "Secrets" de Streamlit

Streamlit Cloud no lee archivos `.json`; las credenciales se guardan como
"Secrets" en formato TOML. En el panel de tu app en Streamlit Cloud
(Settings → Secrets) pega algo así (sustituyendo por tus valores reales
del JSON descargado):

```toml
sheet_id = "ID_DE_TU_GOOGLE_SHEET"

[gcp_service_account]
type = "service_account"
project_id = "tu-proyecto"
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "tu-cuenta@tu-proyecto.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

Para probarlo en local antes de subirlo, guarda ese mismo contenido en
`.streamlit/secrets.toml` (recuerda no subir este archivo a GitHub).

## 4. Subir el archivo `.db` al repositorio

1. Genera/actualiza tu base de datos localmente con `excel_a_db.py`
   (ahora incluye la columna `nivel`).
2. Copia el archivo `examenes.db` a la raíz del repositorio.
3. Súbelo con git normal:
   ```bash
   git add examenes.db
   git commit -m "Actualiza base de datos de examenes"
   git push
   ```
4. `app.py` ya apunta a `DB_PATH = "examenes.db"`, es decir, al archivo
   que está junto a él en el repo — no hace falta configurar nada más.

   Nota: GitHub avisa si un archivo supera 50 MB y bloquea archivos de
   más de 100 MB. Si tu base de datos crece mucho, usa
   [Git LFS](https://git-lfs.com/) para versionarla.

## 5. Publicar en Streamlit Community Cloud

1. Entra en [share.streamlit.io](https://share.streamlit.io/) con tu
   cuenta de GitHub.
2. "New app" → selecciona tu repositorio, la rama (`main`) y el archivo
   principal (`app.py`).
3. Antes de darle a "Deploy", añade los Secrets del paso 3.
4. Despliega. Cada `git push` a la rama configurada actualiza la app
   automáticamente (incluida la base de datos si la vuelves a subir).

## 6. Actualizar preguntas más adelante

Cuando tengas exámenes nuevos:

1. Ejecuta `excel_a_db.py` en Colab/local para actualizar tu
   `examenes.db`.
2. Sustituye el archivo en el repositorio y haz `git push`.
3. Streamlit Cloud redepliega solo con los datos nuevos.

## 7. Gestionar usuarios

Para añadir o quitar usuarios, simplemente edita el Google Sheet
(columnas `usuario` y `contraseña`) — no hace falta tocar el código ni
volver a desplegar nada.
