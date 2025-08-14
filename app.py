import os
from datetime import date, datetime, time, timedelta, timezone
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Google Calendar & Email
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# -----------------------------------------------------------------------------
# CONFIGURACIÓ (ENV primer, si no, Secrets)
# -----------------------------------------------------------------------------
load_dotenv()

def cfg(name: str, default: str = None):
    """Llegeix de l'entorn (.env) i, si no hi és, intenta de st.secrets (Cloud)."""
    val = os.getenv(name)
    if val is not None:
        return val
    try:
        return st.secrets.get(name, default)  # això només existeix al Cloud
    except Exception:
        return default

# DB URL (sense fallback a localhost!)
DATABASE_URL = cfg("DATABASE_URL")
if not DATABASE_URL:
    st.error("Falta DATABASE_URL (posa-la al .env en local o als Secrets al Cloud).")
    st.stop()
engine = create_engine(DATABASE_URL)

# Google & Email
GOOGLE_CLIENT_ID     = cfg("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = cfg("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = cfg("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_ID   = cfg("GOOGLE_CALENDAR_ID") or "primary"

EMAIL_FROM     = cfg("EMAIL_FROM")
EMAIL_PASSWORD = (cfg("EMAIL_PASSWORD") or "").replace(" ", "")  # app password sense espais

# Toggles útils per provar en local (opcional)
ENABLE_CALENDAR = cfg("ENABLE_CALENDAR", "true").lower() == "true"
ENABLE_EMAIL    = cfg("ENABLE_EMAIL", "true").lower() == "true"


# -----------------------------------------------------------------------------
# UTILITATS
# -----------------------------------------------------------------------------
def normalize_range(start_date, end_date):
    start_dt = datetime.combine(start_date, time(0, 0, 0))
    end_dt = datetime.combine(end_date, time(23, 59, 59))
    return start_dt, end_dt

def google_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    # refresca per assegurar token d'accés vàlid
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)

def create_google_calendar_event(start_dt, end_dt, summary, description):
    service = google_calendar_service()
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Madrid"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Madrid"},
    }
    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("htmlLink"), created.get("id")

def delete_google_calendar_event(event_id):
    try:
        service = google_calendar_service()
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        print(f"Error esborrant esdeveniment: {e}")

def enviar_email(destinatari: str, assumpte: str, cos: str):
    if not (EMAIL_FROM and EMAIL_PASSWORD):
        raise RuntimeError("EMAIL_FROM/EMAIL_PASSWORD no configurats als Secrets.")
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = destinatari
    msg["Subject"] = assumpte
    msg.attach(MIMEText(cos, "plain", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)

# -----------------------------------------------------------------------------
# FUNCIONS BD (NOU ESQUEMA)
# -----------------------------------------------------------------------------

# 🧭 Esports
def get_sports():
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, nom, mode, descripcio
            FROM sports
            ORDER BY nom
        """)).mappings().all()
    return list(rows)

def get_sport(sport_id: int):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, nom, mode, descripcio
            FROM sports
            WHERE id = :sid
        """), {"sid": sport_id}).mappings().first()
    return row

# 🧱 Materials per esport (per a mode flexible o per pintar l’info)
def get_materials_by_sport(sport_id: int):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT m.id, m.nom, m.descripcio, m.stock
            FROM materials m
            JOIN sport_material sm ON sm.material_id = m.id
            WHERE sm.sport_id = :sid
            ORDER BY m.nom
        """), {"sid": sport_id}).mappings().all()
    return list(rows)

# 🧩 Pack d’un esport (0..1)
def get_pack_for_sport(sport_id: int):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT p.id, p.nom, p.descripcio
            FROM packs p
            WHERE p.sport_id = :sid
        """), {"sid": sport_id}).mappings().first()
    return row

def get_pack_components(pack_id: int):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT pc.material_id, pc.qty_required, m.nom, m.stock, m.descripcio
            FROM pack_components pc
            JOIN materials m ON m.id = pc.material_id
            WHERE pc.pack_id = :pid
            ORDER BY m.nom
        """), {"pid": pack_id}).mappings().all()
    return list(rows)

# 🧮 Disponibilitat de MATERIAL (compta reserves de material i packs)
def get_material_available(material_id: int, start_dt: datetime, end_dt: datetime):
    with engine.connect() as conn:
        row = conn.execute(text("""
            WITH base AS (
                SELECT stock FROM materials WHERE id = :mid
            ),
            direct_reserved AS (
                SELECT COALESCE(SUM(rl.qty), 0) AS qty
                FROM reserve_lines rl
                JOIN reserves r ON r.id = rl.reserva_id
                WHERE rl.material_id = :mid
                  AND r.estat = 'confirmada'
                  AND :end_ts > r.data_recollida
                  AND :start_ts < r.data_retorn
            ),
            packs_reserved AS (
                SELECT COALESCE(SUM(rl.qty * pc.qty_required), 0) AS qty
                FROM reserve_lines rl
                JOIN reserves r ON r.id = rl.reserva_id
                JOIN pack_components pc ON pc.pack_id = rl.pack_id
                WHERE rl.pack_id IS NOT NULL
                  AND pc.material_id = :mid
                  AND r.estat = 'confirmada'
                  AND :end_ts > r.data_recollida
                  AND :start_ts < r.data_retorn
            )
            SELECT
              CASE
                WHEN base.stock IS NULL THEN NULL
                ELSE GREATEST(base.stock - (direct_reserved.qty + packs_reserved.qty), 0)
              END AS available
            FROM base, direct_reserved, packs_reserved
        """), {"mid": material_id, "start_ts": start_dt, "end_ts": end_dt}).scalar_one()
    return row  # pot ser None (il·limitat) o un enter

# 🧮 Disponibilitat de PACK (mínim per components)
def get_pack_available(pack_id: int, start_dt: datetime, end_dt: datetime):
    comps = get_pack_components(pack_id)
    if not comps:
        return 0
    per_component = []
    has_infinite = False
    for c in comps:
        avail = get_material_available(c["material_id"], start_dt, end_dt)
        if avail is None:
            has_infinite = True
        else:
            per_component.append(avail // c["qty_required"])

    if has_infinite:
        # Si algun component és il·limitat, el pack depèn dels altres
        if per_component:
            return min(per_component)
        else:
            return None  # tots infinits -> il·limitat
    return min(per_component) if per_component else 0

# 🗺️ Map de disponibilitat per pintar UI
def get_availability_map(sport_id: int, start_dt: datetime, end_dt: datetime):
    sport = get_sport(sport_id)
    if not sport:
        return {}, None

    if sport["mode"] == "flexible":
        mats = get_materials_by_sport(sport_id)
        avail = {}
        for m in mats:
            avail[m["id"]] = get_material_available(m["id"], start_dt, end_dt)
        return avail, None
    else:
        pack = get_pack_for_sport(sport_id)
        if not pack:
            return {}, {"pack_id": None, "pack_nom": "(sense pack)", "pack_available": 0}
        pa = get_pack_available(pack["id"], start_dt, end_dt)
        return {}, {"pack_id": pack["id"], "pack_nom": pack["nom"], "pack_available": pa}

# ➕ Crear reserva (accepta línies de materials o pack)
def create_reservation(data_header: dict, lines: list, sport_id: int):
    """
    data_header: dict amb els camps de reserves (adreces, dates…)
    lines:
      - flexible: [(material_id, qty)]
      - pack_fixe: [(pack_id, qty)]
    """
    with engine.begin() as conn:
        res_id = conn.execute(text("""
            INSERT INTO reserves (
                sport_id,
                adreca_electronica, nom_centre, nif_cif, telefon, adreca,
                poblacio, codi_postal, data_recollida, data_retorn,
                responsable_nom, responsable_dni, responsable_telefon,
                responsable_email, responsable_poblacio, responsable_codi_postal
            )
            VALUES (
                :sport_id,
                :adreca_electronica, :nom_centre, :nif_cif, :telefon, :adreca,
                :poblacio, :codi_postal, :data_recollida, :data_retorn,
                :responsable_nom, :responsable_dni, :responsable_telefon,
                :responsable_email, :responsable_poblacio, :responsable_codi_postal
            )
            RETURNING id
        """), {**data_header, "sport_id": sport_id}).scalar_one()

        for kind, ref_id, qty in lines:
            # kind: "material" o "pack"
            if kind == "material":
                conn.execute(text("""
                    INSERT INTO reserve_lines (reserva_id, material_id, qty)
                    VALUES (:rid, :mid, :qty)
                """), {"rid": res_id, "mid": ref_id, "qty": qty})
            else:
                conn.execute(text("""
                    INSERT INTO reserve_lines (reserva_id, pack_id, qty)
                    VALUES (:rid, :pid, :qty)
                """), {"rid": res_id, "pid": ref_id, "qty": qty})
    return res_id

# 📋 Llistat dreserves amb línies (materials i packs)
def get_all_reservations():
    with engine.connect() as conn:
        rows = conn.execute(text("""
            WITH lines AS (
              SELECT rl.reserva_id,
                     CASE WHEN rl.material_id IS NOT NULL THEN m.nom ELSE p.nom END AS item_nom,
                     CASE WHEN rl.material_id IS NOT NULL THEN 'material' ELSE 'pack' END AS tipus,
                     rl.qty
              FROM reserve_lines rl
              LEFT JOIN materials m ON m.id = rl.material_id
              LEFT JOIN packs p ON p.id = rl.pack_id
            )
            SELECT r.id, r.nom_centre, r.nif_cif, r.data_recollida, r.data_retorn,
                   r.responsable_nom, r.estat,
                   COALESCE(string_agg(
                     CASE WHEN l.tipus='pack' THEN '[PACK] '||l.item_nom||' x'||l.qty
                          ELSE l.item_nom||' x'||l.qty END,
                     E'\n' ORDER BY l.tipus DESC, l.item_nom
                   ), '(sense línies)') AS materials
            FROM reserves r
            LEFT JOIN lines l ON l.reserva_id = r.id
            GROUP BY r.id
            ORDER BY r.data_recollida DESC
        """)).mappings().all()
    # Per compatibilitat amb el teu render: convertim materials a llista de línies
    out = []
    for r in rows:
        out.append({
            **r,
            "materials": r["materials"].split("\n") if r["materials"] else []
        })
    return out

def mark_finished_reservations():
    now = datetime.now()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE reserves
            SET estat='finalitzada'
            WHERE estat='confirmada' AND data_retorn < :now
        """), {"now": now})

# 🗓️ (opcional) si uses esdeveniments de calendari, cal una columna a reserves:
# ALTER TABLE reserves ADD COLUMN IF NOT EXISTS calendar_event_id TEXT;
def get_event_id_by_reservation(res_id):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT calendar_event_id FROM reserves WHERE id = :id"),
                           {"id": res_id}).first()
        return row["calendar_event_id"] if row and row["calendar_event_id"] else None

def update_calendar_event_id(res_id, event_id):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE reserves SET calendar_event_id = :eid WHERE id = :rid
        """), {"eid": event_id, "rid": res_id})

def update_reservation_status(res_id, new_status):
    with engine.begin() as conn:
        conn.execute(text("UPDATE reserves SET estat=:estat WHERE id=:id"),
                     {"estat": new_status, "id": res_id})

def update_reservation_status_with_calendar(res_id, new_status):
    if ENABLE_CALENDAR and new_status in ["cancel·lada", "finalitzada"]:
        event_id = get_event_id_by_reservation(res_id)
        if event_id:
            delete_google_calendar_event(event_id)
    update_reservation_status(res_id, new_status)



# -----------------------------------------------------------------------------
# UI STREAMLIT
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Reserves de Material", page_icon="📦", layout="wide")
page = st.sidebar.selectbox("Navegació", ["Formulari públic", "Administració"])

# Botó de test de calendari a la sidebar
#if st.sidebar.button("🔁 Fer test de calendari"):
#   try:
#        now_utc = datetime.now(timezone.utc)
#        link = create_google_calendar_event(
#            start_dt=now_utc,
#            end_dt=now_utc + timedelta(hours=1),
#            summary="🔧 Test Reserva Streamlit",
#            description="Test d'integració amb Google Calendar"
#        )
#        st.sidebar.success("Esdeveniment creat!")
#        st.sidebar.markdown(f"[Veure al calendari]({link})")
#    except Exception as e:
#        st.sidebar.error(f"Error: {e}")


# ---------------------- FORMULARI PÚBLIC ----------------------
if page == "Formulari públic":
    st.title("Formulari de reserva de material")

    # 1) DATES PRIMER
    st.subheader("Dades de la reserva")
    data_recollida = st.date_input("Data de recollida *", value=date.today(), key="data_recollida_out")
    data_retorn    = st.date_input("Data de retorn *", value=date.today(), key="data_retorn_out")

    if data_retorn < data_recollida:
        st.error("La data de retorn ha de ser igual o posterior a la de recollida.")

    start_dt_preview, end_dt_preview = normalize_range(data_recollida, data_retorn)

    # 2) DESPRÉS L'ESPORT + MATERIALS/PACKS
    st.subheader("Esport i selecció d’articles")

    sports = get_sports()
    if not sports:
        st.info("No hi ha esports definits.")
        st.stop()

    # selector d’esport
    sport_options = {f"{s['nom']}": s["id"] for s in sports}
    sport_label = st.selectbox("Esport", list(sport_options.keys()), key="sport_select")
    sport_id = sport_options[sport_label]
    sport = get_sport(sport_id)
    st.markdown(f"{sport.get('descripcio') or ''}")

    # disponibilitats segons esport + dates
    avail_map, pack_info = get_availability_map(sport_id, start_dt_preview, end_dt_preview)

    # widgets de selecció (fora del form)
    selected_lines = []  # ("material"/"pack", id, qty)

    if sport["mode"] == "flexible":
        mats = get_materials_by_sport(sport_id)
        for m in mats:
            avail = avail_map.get(m["id"], 0)
            if m["stock"] is None:
                shown = "il·limitada"
                max_val = 9999
            else:
                shown = f"{avail if avail is not None else '∞'} de {m['stock']}"
                max_val = int(avail) if isinstance(avail, int) and avail >= 0 else 0
            qty = st.number_input(
                f"{m['nom']} ({m.get('descripcio') or ''}) — Disponibilitat: {shown}",
                min_value=0, max_value=max_val, step=1, value=0,
                key=f"mat_{sport_id}_{m['id']}"
            )
            if qty > 0:
                selected_lines.append(("material", m["id"], qty))
    else:
        if pack_info and pack_info["pack_id"]:
            pa = pack_info["pack_available"]
            shown = "il·limitada" if pa is None else str(int(pa))
            qty = st.number_input(
                f"{pack_info['pack_nom']} — Packs disponibles: {shown}",
                min_value=0,
                max_value=9999 if pa is None else int(pa) if pa >= 0 else 0,
                step=1, value=0, key=f"pack_{pack_info['pack_id']}"
            )
            if qty > 0:
                selected_lines.append(("pack", pack_info["pack_id"], qty))
        else:
            st.warning("Aquest esport no té cap pack definit.")

    st.divider()

    # 3) FINALMENT EL FORMULARI DEL CENTRE/RESPONSABLE + ENVIAR
    with st.form("reserva_form"):
        st.subheader("Dades de Facturació")
        adreca_electronica = st.text_input("Adreça electrònica *", key="adreca_electronica")
        nom_centre         = st.text_input("Nom del Centre/Institució *", key="nom_centre")
        nif_cif            = st.text_input("N.I.F / C.I.F de l'entitat *", key="nif_cif")
        telefon_centre     = st.text_input("Telèfon *", key="telefon_centre")
        adreca_centre      = st.text_input("Adreça *", key="adreca_centre")
        poblacio_centre    = st.text_input("Població *", key="poblacio_centre")
        cp_centre          = st.text_input("Codi Postal *", key="cp_centre")

        st.subheader("Dades del responsable")
        responsable_nom       = st.text_input("Responsable del material *", key="responsable_nom")
        responsable_dni       = st.text_input("DNI *", key="responsable_dni")
        responsable_telefon   = st.text_input("Telèfon *", key="telefon_responsable")
        responsable_email     = st.text_input("Correu electrònic *", key="email_responsable")
        responsable_poblacio  = st.text_input("Població", key="poblacio_responsable")
        responsable_cp        = st.text_input("Codi Postal", key="cp_responsable")

        submit_btn = st.form_submit_button("Enviar reserva")

    # Validació i creació
    if submit_btn:
        if not adreca_electronica or not nom_centre or not nif_cif or not telefon_centre or not adreça_centre or not poblacio_centre or not cp_centre or not responsable_nom or not responsable_dni or not responsable_telefon or not responsable_email:
            st.error("Omple tots els camps obligatoris (*)")
        elif data_retorn < data_recollida:
            st.error("La data de retorn ha de ser igual o posterior a la de recollida.")
        elif not selected_lines:
            st.error("Has de seleccionar almenys un element amb quantitat > 0 (a l’apartat d’Esport).")
        else:
            start_dt, end_dt = normalize_range(data_recollida, data_retorn)

            # Revalidació d’estoc amb el rang definitiu
            ok_all = True
            if sport["mode"] == "flexible":
                for _, mid, qty in selected_lines:
                    avail = get_material_available(mid, start_dt, end_dt)
                    if (avail is not None) and (qty > avail):
                        mname = next(m['nom'] for m in get_materials_by_sport(sport_id) if m['id'] == mid)
                        st.error(f"No hi ha prou disponibilitat per {mname}. Disponibles: {avail}")
                        ok_all = False
            else:
                for _, pid, qty in selected_lines:
                    pa = get_pack_available(pid, start_dt, end_dt)
                    if (pa is not None) and (qty > pa):
                        st.error(f"No hi ha packs suficients. Disponibles: {pa}")
                        ok_all = False

            if ok_all:
                data_dict = {
                    "adreca_electronica": adreca_electronica,
                    "nom_centre": nom_centre,
                    "nif_cif": nif_cif,
                    "telefon": telefon_centre,
                    "adreca": adreca_centre,
                    "poblacio": poblacio_centre,
                    "codi_postal": cp_centre,
                    "data_recollida": start_dt,
                    "data_retorn": end_dt,
                    "responsable_nom": responsable_nom,
                    "responsable_dni": responsable_dni,
                    "responsable_telefon": responsable_telefon,
                    "responsable_email": responsable_email,
                    "responsable_poblacio": responsable_poblacio,
                    "responsable_codi_postal": responsable_cp
                }
                res_id = create_reservation(data_dict, selected_lines, sport_id)

                # text per email/calendari
                llista_items = []
                for kind, refid, qty in selected_lines:
                    if kind == "material":
                        mname = next(m['nom'] for m in get_materials_by_sport(sport_id) if m['id'] == refid)
                        llista_items.append(f" - {mname} x{qty}")
                    else:
                        p = get_pack_for_sport(sport_id)
                        llista_items.append(f" - [PACK] {p['nom']} x{qty}")
                llista_text = "\n".join(llista_items)

                email_text = (
                    f"Hola {responsable_nom},\n\n"
                    f"Hem registrat la teva reserva:\n{llista_text}\n\n"
                    f"Recollida: {data_recollida.strftime('%Y-%m-%d')}\n"
                    f"Retorn:    {data_retorn.strftime('%Y-%m-%d')}\n\n"
                    "Gràcies!"
                )

                if ENABLE_EMAIL:
                    try:
                        if responsable_email:
                            enviar_email(responsable_email, f"Confirmació reserva #{res_id}", email_text)
                        if EMAIL_FROM:
                            enviar_email(EMAIL_FROM, f"[Còpia] reserva #{res_id}", email_text)
                    except Exception as e:
                        st.warning(f"No s'ha pogut enviar el correu: {e}")

                if ENABLE_CALENDAR:
                    try:
                        summary = f"Reserva material ({nom_centre}) — {sport['nom']}"
                        descr = (
                            f"Esport: {sport['nom']} ({sport['mode']})\n"
                            f"Centre: {nom_centre} ({nif_cif})\n"
                            f"Responsable: {responsable_nom} ({responsable_dni})\n\n"
                            f"Items:\n{llista_text}\n\n"
                            f"Contacte centre: {adreca_electronica} / {telefon_centre or ''}\n"
                            f"Adreça: {adreca_centre or ''}, {poblacio_centre or ''} {cp_centre or ''}"
                        )
                        link, event_id = create_google_calendar_event(start_dt, end_dt, summary, descr)
                        update_calendar_event_id(res_id, event_id)
                        #st.info(f"📅 Esdeveniment creat al teu calendari: {link}")
                    except Exception as e:
                        st.warning(f"No s'ha pogut crear l'esdeveniment al calendari: {e}")

                st.success(f"Reserva #{res_id} creada correctament! ✅")




# ---------------------- ADMINISTRACIÓ ----------------------
elif page == "Administració":
    st.title("Administració de reserves")

    if st.button("Marcar reserves passades com a finalitzades"):
        mark_finished_reservations()
        st.success("Reserves actualitzades.")
        st.rerun()

    reservations = get_all_reservations()
    if not reservations:
        st.info("No hi ha reserves.")
    else:
        df = pd.DataFrame(reservations)
        st.dataframe(df, use_container_width=True)

        if "disabled_buttons" not in st.session_state:
            st.session_state["disabled_buttons"] = set()
        if "confirm_action" not in st.session_state:
            st.session_state["confirm_action"] = None
        if "confirm_id" not in st.session_state:
            st.session_state["confirm_id"] = None

        for r in reservations:
            with st.expander(f"#{r['id']} · {r['nom_centre']} · {r['data_recollida'].date()} → {r['data_retorn'].date()}"):
                st.write(f"**NIF/CIF:** {r['nif_cif']}")
                st.write(f"**Responsable:** {r['responsable_nom']}")
                st.write(f"**Estat:** {r['estat']}")
                st.write("**Línies:**")
                for line in r['materials']:
                    st.write(f"- {line}")

                disabled_by_status = r["estat"] in ("cancel·lada", "finalitzada")

                col1, col2 = st.columns(2)
                with col1:
                    cancel_disabled = disabled_by_status or ((r['id'], "cancel") in st.session_state["disabled_buttons"])
                    if st.button("Cancel·lar", key=f"cancel_{r['id']}", disabled=cancel_disabled):
                        st.session_state["confirm_action"] = "cancel"
                        st.session_state["confirm_id"] = r["id"]
                        st.rerun()

                with col2:
                    finish_disabled = disabled_by_status or ((r['id'], "finish") in st.session_state["disabled_buttons"])
                    if st.button("Finalitzar", key=f"finish_{r['id']}", disabled=finish_disabled):
                        st.session_state["confirm_action"] = "finish"
                        st.session_state["confirm_id"] = r["id"]
                        st.rerun()

        if st.session_state["confirm_action"] and st.session_state["confirm_id"]:
            rid = st.session_state["confirm_id"]
            act = st.session_state["confirm_action"]
            verb = "cancel·lar" if act == "cancel" else "finalitzar"

            st.warning(f"Segur que vols {verb} la reserva #{rid}? Aquesta acció no es pot desfer.")

            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("✅ Sí, confirmar", key="do_confirm"):
                    if act == "cancel":
                        update_reservation_status_with_calendar(rid, "cancel·lada")
                        st.success(f"Reserva #{rid} cancel·lada.")
                    else:
                        update_reservation_status_with_calendar(rid, "finalitzada")
                        st.success(f"Reserva #{rid} finalitzada.")
                    st.session_state["disabled_buttons"].add((rid, act))
                    st.session_state["confirm_action"] = None
                    st.session_state["confirm_id"] = None
                    st.rerun()
            with cc2:
                if st.button("❌ Cancel·lar acció", key="cancel_confirm"):
                    st.session_state["confirm_action"] = None
                    st.session_state["confirm_id"] = None
                    st.info("Acció cancel·lada.")
