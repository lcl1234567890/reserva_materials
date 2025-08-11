import os
from datetime import date, datetime, time
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# CONFIGURACIÓ
# -----------------------------------------------------------------------------
load_dotenv()
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres@localhost:5432/reserva_material"
)
engine = create_engine(DATABASE_URL)

def normalize_range(start_date, end_date):
    start_dt = datetime.combine(start_date, time(0, 0, 0))
    end_dt = datetime.combine(end_date, time(23, 59, 59))
    return start_dt, end_dt

# -----------------------------------------------------------------------------
# FUNCIONS BD
# -----------------------------------------------------------------------------
def get_materials():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nom, total_packs, descripcio FROM materials ORDER BY nom"
        )).mappings().all()
    return list(rows)

def check_availability(material_id, start_dt, end_dt, requested_qty):
    with engine.connect() as conn:
        total_packs = conn.execute(
            text("SELECT total_packs FROM materials WHERE id=:id"),
            {"id": material_id}
        ).scalar_one()

        if total_packs is None:
            return True, None

        reserved = conn.execute(text("""
            SELECT COALESCE(SUM(rm.quantitat_packs), 0)
            FROM reserves_material rm
            JOIN reserves r ON r.id = rm.reserva_id
            WHERE rm.material_id = :mat_id
              AND r.estat = 'confirmada'
              AND :end_ts > r.data_recollida
              AND :start_ts < r.data_retorn
        """), {"mat_id": material_id, "start_ts": start_dt, "end_ts": end_dt}).scalar_one()

    available = total_packs - reserved
    return available >= requested_qty, available

def create_reservation(data, materials_selected):
    with engine.begin() as conn:
        res_id = conn.execute(text("""
            INSERT INTO reserves (
                adreca_electronica, nom_centre, nif_cif, telefon, adreca,
                poblacio, codi_postal, data_recollida, data_retorn,
                responsable_nom, responsable_dni, responsable_telefon,
                responsable_email, responsable_poblacio, responsable_codi_postal
            )
            VALUES (
                :adreca_electronica, :nom_centre, :nif_cif, :telefon, :adreca,
                :poblacio, :codi_postal, :data_recollida, :data_retorn,
                :responsable_nom, :responsable_dni, :responsable_telefon,
                :responsable_email, :responsable_poblacio, :responsable_codi_postal
            )
            RETURNING id
        """), data).scalar_one()

        for mat_id, qty in materials_selected:
            conn.execute(text("""
                INSERT INTO reserves_material (reserva_id, material_id, quantitat_packs)
                VALUES (:res_id, :mat_id, :qty)
            """), {"res_id": res_id, "mat_id": mat_id, "qty": qty})

    return res_id

def get_all_reservations():
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT r.id, r.nom_centre, r.nif_cif, r.data_recollida, r.data_retorn,
                   r.responsable_nom, r.estat,
                   array_agg(m.nom || ' x' || rm.quantitat_packs) AS materials
            FROM reserves r
            JOIN reserves_material rm ON rm.reserva_id = r.id
            JOIN materials m ON m.id = rm.material_id
            GROUP BY r.id
            ORDER BY r.data_recollida DESC
        """)).mappings().all()
    return list(rows)

def update_reservation_status(res_id, new_status):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE reserves SET estat=:estat WHERE id=:id
        """), {"estat": new_status, "id": res_id})

def mark_finished_reservations():
    now = datetime.now()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE reserves
            SET estat='finalitzada'
            WHERE estat='confirmada' AND data_retorn < :now
        """), {"now": now})

# -----------------------------------------------------------------------------
# UI STREAMLIT
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Reserves de Material", page_icon="📦", layout="wide")
page = st.sidebar.selectbox("Navegació", ["Formulari públic", "Administració"])

# ---------------------- FORMULARI PÚBLIC ----------------------
if page == "Formulari públic":
    st.title("📦 Formulari de reserva de material")
    materials_list = get_materials()

    with st.form("reserva_form"):
        st.subheader("Dades del Centre/Institució")
        adreca_electronica = st.text_input("Adreça electrònica *", key="adreca_electronica")
        nom_centre = st.text_input("Nom del Centre/Institució *", key="nom_centre")
        nif_cif = st.text_input("N.I.F / C.I.F de l'entitat *", key="nif_cif")
        telefon_centre = st.text_input("Telèfon", key="telefon_centre")
        adreca_centre = st.text_input("Adreça", key="adreca_centre")
        poblacio_centre = st.text_input("Població", key="poblacio_centre")
        cp_centre = st.text_input("Codi Postal", key="cp_centre")

        st.subheader("Dades de la reserva")
        data_recollida = st.date_input("Data de recollida del material *", value=date.today(), key="data_recollida")
        data_retorn = st.date_input("Data de retorn del material *", value=date.today(), key="data_retorn")

        st.subheader("Dades del responsable")
        responsable_nom = st.text_input("Responsable del material *", key="responsable_nom")
        responsable_dni = st.text_input("DNI *", key="responsable_dni")
        responsable_telefon = st.text_input("Telèfon", key="telefon_responsable")
        responsable_email = st.text_input("Correu electrònic", key="email_responsable")
        responsable_poblacio = st.text_input("Població", key="poblacio_responsable")
        responsable_cp = st.text_input("Codi Postal", key="cp_responsable")

        st.subheader("Relació de material a sol·licitar")
        selected_materials = []
        for mat in materials_list:
            qty = st.number_input(
                f"{mat['nom']} ({mat['descripcio']}) - Disponibilitat: {'il·limitada' if mat['total_packs'] is None else mat['total_packs']}",
                min_value=0, step=1, value=0, key=f"mat_{mat['id']}"
            )
            if qty > 0:
                selected_materials.append((mat['id'], qty))

        submit_btn = st.form_submit_button("Enviar reserva")

    if submit_btn:
        if not adreca_electronica or not nom_centre or not nif_cif or not responsable_nom or not responsable_dni:
            st.error("Omple tots els camps obligatoris (*)")
        elif data_retorn < data_recollida:
            st.error("La data de retorn ha de ser igual o posterior a la de recollida.")
        elif not selected_materials:
            st.error("Has de seleccionar almenys un material amb quantitat > 0.")
        else:
            start_dt, end_dt = normalize_range(data_recollida, data_retorn)
            all_available = True
            for mat_id, qty in selected_materials:
                ok, avail = check_availability(mat_id, start_dt, end_dt, qty)
                if not ok:
                    mat_name = next(m['nom'] for m in materials_list if m['id'] == mat_id)
                    st.error(f"No hi ha prou disponibilitat per {mat_name}. Disponibles: {avail}")
                    all_available = False
            if all_available:
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
                res_id = create_reservation(data_dict, selected_materials)
                st.success(f"Reserva #{res_id} creada correctament! ✅")

# ---------------------- ADMINISTRACIÓ ----------------------
elif page == "Administració":
    st.title("📋 Administració de reserves")

    if st.button("Marcar reserves passades com a finalitzades"):
        mark_finished_reservations()
        st.success("Reserves actualitzades.")
        st.experimental_rerun()

    reservations = get_all_reservations()
    if not reservations:
        st.info("No hi ha reserves.")
    else:
        df = pd.DataFrame(reservations)
        st.dataframe(df, use_container_width=True)

        for r in reservations:
            with st.expander(f"#{r['id']} · {r['nom_centre']} · {r['data_recollida'].date()} → {r['data_retorn'].date()}"):
                st.write(f"**NIF/CIF:** {r['nif_cif']}")
                st.write(f"**Responsable:** {r['responsable_nom']}")
                st.write(f"**Estat:** {r['estat']}")
                st.write("**Materials:**")
                for mat in r['materials']:
                    st.write(f"- {mat}")

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Cancel·lar", key=f"cancel_{r['id']}"):
                        update_reservation_status(r['id'], 'cancel·lada')
                        st.success("Reserva cancel·lada.")
                        st.experimental_rerun()
                with col2:
                    if st.button("Finalitzar", key=f"finish_{r['id']}"):
                        update_reservation_status(r['id'], 'finalitzada')
                        st.success("Reserva finalitzada.")
                        st.experimental_rerun()
