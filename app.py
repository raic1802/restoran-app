# -*- coding: utf-8 -*-
"""
RESTORAN · KUHINJA · SMJEŠTAJ
Jednostavna cloud web aplikacija u jednom fajlu (Streamlit + SQL baza).

Pokretanje lokalno:
    pip install -r requirements.txt
    streamlit run app.py

Podešavanja (Secrets ili varijable okruženja):
    ADMIN_EMAIL     glavni admin e-mail
    ADMIN_LOZINKA   admin lozinka (bez nje admin prijava je isključena)
    DATABASE_URL    PostgreSQL adresa za trajno čuvanje u cloudu
                    (bez nje se koristi lokalni SQLite fajl "restoran.db")
    KOD_OBJEKTA     opciono: zajednički kod koji zaposleni upišu pri ulasku

Struktura fajla (svaka cjelina je odvojena da bi se lako dodavale nove opcije):
    1. KONSTANTE
    2. BAZA PODATAKA  (šema + migracije + funkcije za čitanje/pisanje)
    3. POMOĆNE FUNKCIJE  (datumi, novac, poruke, prijava)
    4. IZGLED  (CSS)
    5. STRANICE
    6. NAVIGACIJA  (ovdje se registruju stranice)

Nova opcija = nova funkcija u dijelu 5 + jedan red u dijelu 6.
Nova kolona ili tabela = nova stavka na kraju liste MIGRACIJE u dijelu 2.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text


# ═════════════════════════════════════════════════════════════════════════════
# 1. KONSTANTE
# ═════════════════════════════════════════════════════════════════════════════

TZ = ZoneInfo("Europe/Podgorica")

BROJ_SOBA = 8
SOBE = list(range(1, BROJ_SOBA + 1))

SMJENE = ["Prva smjena", "Druga smjena"]
KAFANA = "Kafana i smještaj"
KUHINJA = "Kuhinja"
ODJELJENJA = [KAFANA, KUHINJA]
IZVORI = ["Privatno", "Booking"]

# Izvještaj upisan poslije ponoći, a prije ovog sata, računa se u prethodni dan
# (npr. druga smjena koja zatvara u 01:00).
RADNI_DAN_POCINJE_U = 6

NOVAC = ["pazar_kafana", "rashod_kafana", "pazar_kuhinja", "rashod_kuhinja", "pazar_smjestaj"]

DANI = ["Ponedjeljak", "Utorak", "Srijeda", "Četvrtak", "Petak", "Subota", "Nedjelja"]
DANI_KRATKO = ["Pon", "Uto", "Sri", "Čet", "Pet", "Sub", "Ned"]
MJESECI = ["Januar", "Februar", "Mart", "April", "Maj", "Jun",
           "Jul", "Avgust", "Septembar", "Oktobar", "Novembar", "Decembar"]
MJESECI_GEN = ["januara", "februara", "marta", "aprila", "maja", "juna",
               "jula", "avgusta", "septembra", "oktobra", "novembra", "decembra"]


def tajna(kljuc: str, podrazumijevano: str = "") -> str:
    """Čita podešavanje iz varijabli okruženja ili iz Streamlit Secrets."""
    vrijednost = os.environ.get(kljuc)
    if vrijednost:
        return vrijednost
    if _postoje_secrets():
        try:
            if kljuc in st.secrets:
                return str(st.secrets[kljuc])
        except Exception:
            pass
    return podrazumijevano


def _postoje_secrets() -> bool:
    ucitaj = getattr(type(st.secrets), "load_if_toml_exists", None)
    if callable(ucitaj):
        try:
            return bool(ucitaj(st.secrets))
        except Exception:
            pass
    try:
        from streamlit import config as st_config

        putanje = st_config.get_option("secrets.files") or []
        return any(Path(p).expanduser().exists() for p in putanje)
    except Exception:
        return True


# ═════════════════════════════════════════════════════════════════════════════
# 2. BAZA PODATAKA
# ═════════════════════════════════════════════════════════════════════════════
# Datumi se čuvaju kao tekst "GGGG-MM-DD", a vrijeme kao "GGGG-MM-DD HH:MM:SS",
# pa isti SQL radi i na SQLite i na PostgreSQL bazi.
# {ID} se automatski zamjenjuje odgovarajućim tipom primarnog ključa.

MIGRACIJE: list[list[str]] = [
    # ── verzija 1: početne tabele ──────────────────────────────────────────
    [
        """CREATE TABLE IF NOT EXISTS izvjestaji (
            id {ID},
            kreirano TEXT NOT NULL,
            datum TEXT NOT NULL,
            smjena TEXT NOT NULL,
            odjeljenje TEXT NOT NULL,
            sef TEXT NOT NULL,
            pazar_kafana DOUBLE PRECISION NOT NULL DEFAULT 0,
            rashod_kafana DOUBLE PRECISION NOT NULL DEFAULT 0,
            opis_rashod_kafana TEXT NOT NULL DEFAULT '',
            pazar_kuhinja DOUBLE PRECISION NOT NULL DEFAULT 0,
            rashod_kuhinja DOUBLE PRECISION NOT NULL DEFAULT 0,
            opis_rashod_kuhinja TEXT NOT NULL DEFAULT '',
            pazar_smjestaj DOUBLE PRECISION NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS ix_izvjestaji_datum ON izvjestaji (datum)",
        """CREATE TABLE IF NOT EXISTS artikli (
            id {ID},
            naziv TEXT NOT NULL,
            kategorija TEXT NOT NULL,
            kreirano TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS narudzbine (
            id {ID},
            kreirano TEXT NOT NULL,
            narucio TEXT NOT NULL,
            stavke TEXT NOT NULL,
            napomena TEXT NOT NULL DEFAULT '',
            poruka TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_narudzbine_kreirano ON narudzbine (kreirano)",
        """CREATE TABLE IF NOT EXISTS rezervacije (
            id {ID},
            gost TEXT NOT NULL,
            soba INTEGER NOT NULL,
            cijena DOUBLE PRECISION NOT NULL DEFAULT 0,
            izvor TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL,
            napomena TEXT NOT NULL DEFAULT '',
            upisao TEXT NOT NULL,
            kreirano TEXT NOT NULL,
            otkazana INTEGER NOT NULL DEFAULT 0,
            otkazao TEXT NOT NULL DEFAULT '',
            otkazano TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS ix_rezervacije_soba ON rezervacije (soba, check_in, check_out)",
        """CREATE TABLE IF NOT EXISTS podesavanja (
            kljuc TEXT PRIMARY KEY,
            vrijednost TEXT NOT NULL DEFAULT ''
        )""",
    ],
    # ── verzija 2: ovdje dodaj buduće izmjene, npr. ─────────────────────────
    # ["ALTER TABLE artikli ADD COLUMN jedinica TEXT NOT NULL DEFAULT ''"],
]


def _migriraj(engine) -> None:
    id_kolona = (
        "SERIAL PRIMARY KEY"
        if engine.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS verzija_seme (verzija INTEGER NOT NULL)"))
        trenutna = conn.execute(text("SELECT MAX(verzija) FROM verzija_seme")).scalar() or 0
        for broj_verzije, naredbe in enumerate(MIGRACIJE, start=1):
            if broj_verzije <= trenutna:
                continue
            for sql in naredbe:
                conn.execute(text(sql.replace("{ID}", id_kolona)))
            conn.execute(text("INSERT INTO verzija_seme (verzija) VALUES (:v)"), {"v": broj_verzije})


def _postgres_drajver() -> str:
    """Bira instalirani PostgreSQL drajver (psycopg2 ili psycopg 3)."""
    try:
        import psycopg2  # noqa: F401

        return "psycopg2"
    except ImportError:
        return "psycopg"


def pripremi_url(url: str) -> tuple[str, dict]:
    """Vraća (adresa baze sa izričitim drajverom, dodatni parametri konekcije)."""
    url = url.strip()
    if url.startswith("sqlite"):
        return url, {"check_same_thread": False}
    for prefiks in ("postgres://", "postgresql://"):
        if url.startswith(prefiks):
            url = f"postgresql+{_postgres_drajver()}://" + url[len(prefiks):]
            break
    if url.startswith("postgresql+psycopg://"):
        # bez pripremljenih upita – sigurno i preko pooler konekcije (Neon, Supabase)
        return url, {"prepare_threshold": None}
    return url, {}


@st.cache_resource(show_spinner=False)
def baza():
    url, parametri = pripremi_url(tajna("DATABASE_URL", "sqlite:///restoran.db"))
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args=parametri)
    else:
        engine = create_engine(url, connect_args=parametri, pool_pre_ping=True, pool_recycle=300)
    _migriraj(engine)
    return engine


def upit(sql: str, **parametri) -> list[dict]:
    with baza().connect() as conn:
        return [dict(red) for red in conn.execute(text(sql), parametri).mappings().all()]


def upit_df(sql: str, **parametri) -> pd.DataFrame:
    with baza().connect() as conn:
        rezultat = conn.execute(text(sql), parametri)
        kolone = list(rezultat.keys())
        return pd.DataFrame([tuple(red) for red in rezultat.fetchall()], columns=kolone)


def jedna_vrijednost(sql: str, **parametri):
    with baza().connect() as conn:
        return conn.execute(text(sql), parametri).scalar()


def izvrsi(sql: str, **parametri) -> int:
    with baza().begin() as conn:
        return conn.execute(text(sql), parametri).rowcount


# ── Podešavanja ──────────────────────────────────────────────────────────────

def procitaj_podesavanje(kljuc: str, podrazumijevano: str = "") -> str:
    v = jedna_vrijednost("SELECT vrijednost FROM podesavanja WHERE kljuc = :k", k=kljuc)
    return v if v else podrazumijevano


def sacuvaj_podesavanje(kljuc: str, vrijednost: str) -> None:
    if izvrsi("UPDATE podesavanja SET vrijednost = :v WHERE kljuc = :k", k=kljuc, v=vrijednost) == 0:
        izvrsi("INSERT INTO podesavanja (kljuc, vrijednost) VALUES (:k, :v)", k=kljuc, v=vrijednost)


# ── Izvještaji smjena ────────────────────────────────────────────────────────

def sacuvaj_izvjestaj(zapis: dict) -> None:
    izvrsi(
        """INSERT INTO izvjestaji (kreirano, datum, smjena, odjeljenje, sef,
               pazar_kafana, rashod_kafana, opis_rashod_kafana,
               pazar_kuhinja, rashod_kuhinja, opis_rashod_kuhinja, pazar_smjestaj)
           VALUES (:kreirano, :datum, :smjena, :odjeljenje, :sef,
               :pazar_kafana, :rashod_kafana, :opis_rashod_kafana,
               :pazar_kuhinja, :rashod_kuhinja, :opis_rashod_kuhinja, :pazar_smjestaj)""",
        **zapis,
    )


def izvjestaji_za_period(od: date, do: date) -> pd.DataFrame:
    return upit_df(
        "SELECT * FROM izvjestaji WHERE datum >= :od AND datum <= :do ORDER BY datum, kreirano",
        od=od.isoformat(),
        do=do.isoformat(),
    )


def obrisi_izvjestaj(id_: int) -> None:
    izvrsi("DELETE FROM izvjestaji WHERE id = :id", id=id_)


def godine_za_izbor() -> list[int]:
    najranija = jedna_vrijednost("SELECT MIN(datum) FROM izvjestaji")
    ova = danas().year
    pocetak = min(int(str(najranija)[:4]), ova) if najranija else ova
    return list(range(ova, pocetak - 1, -1))


# ── Artikli ──────────────────────────────────────────────────────────────────

def svi_artikli() -> pd.DataFrame:
    df = upit_df("SELECT id, naziv, kategorija FROM artikli")
    if df.empty:
        return df
    df["id"] = df["id"].astype(int)
    return df.sort_values(["kategorija", "naziv"], key=lambda s: s.str.casefold()).reset_index(drop=True)


def dodaj_artikal(naziv: str, kategorija: str) -> None:
    izvrsi(
        "INSERT INTO artikli (naziv, kategorija, kreirano) VALUES (:n, :k, :t)",
        n=naziv, k=kategorija, t=sada().isoformat(sep=" "),
    )


def izmijeni_artikal(id_: int, naziv: str, kategorija: str) -> None:
    izvrsi("UPDATE artikli SET naziv = :n, kategorija = :k WHERE id = :id", n=naziv, k=kategorija, id=id_)


def obrisi_artikal(id_: int) -> None:
    izvrsi("DELETE FROM artikli WHERE id = :id", id=id_)


# ── Narudžbine ───────────────────────────────────────────────────────────────

def sacuvaj_narudzbinu(narucio: str, stavke: list[dict], napomena: str, poruka: str, trenutak: datetime) -> None:
    izvrsi(
        """INSERT INTO narudzbine (kreirano, narucio, stavke, napomena, poruka)
           VALUES (:t, :n, :s, :nap, :p)""",
        t=trenutak.isoformat(sep=" "), n=narucio, s=json.dumps(stavke, ensure_ascii=False),
        nap=napomena, p=poruka,
    )


def narudzbine_za_period(od: date, do: date) -> pd.DataFrame:
    return upit_df(
        "SELECT * FROM narudzbine WHERE kreirano >= :od AND kreirano < :do ORDER BY kreirano DESC",
        od=od.isoformat(),
        do=(do + timedelta(days=1)).isoformat(),
    )


def posljednje_narudzbine(n: int = 5) -> list[dict]:
    return upit("SELECT * FROM narudzbine ORDER BY kreirano DESC LIMIT :n", n=n)


# ── Rezervacije ──────────────────────────────────────────────────────────────

def preklapanja(soba: int, check_in: date, check_out: date) -> list[dict]:
    return upit(
        """SELECT * FROM rezervacije
           WHERE otkazana = 0 AND soba = :soba AND check_in < :co AND check_out > :ci
           ORDER BY check_in""",
        soba=soba, ci=check_in.isoformat(), co=check_out.isoformat(),
    )


def sacuvaj_rezervaciju(zapis: dict) -> None:
    izvrsi(
        """INSERT INTO rezervacije (gost, soba, cijena, izvor, check_in, check_out,
               napomena, upisao, kreirano)
           VALUES (:gost, :soba, :cijena, :izvor, :check_in, :check_out,
               :napomena, :upisao, :kreirano)""",
        **zapis,
    )


def rezervacije_u_periodu(od: date, do: date) -> list[dict]:
    """Aktivne rezervacije koje zauzimaju bar jednu noć između `od` i `do` (uključivo)."""
    return upit(
        """SELECT * FROM rezervacije
           WHERE otkazana = 0 AND check_in <= :do AND check_out > :od
           ORDER BY soba, check_in""",
        od=od.isoformat(), do=do.isoformat(),
    )


def dolasci_na_dan(dan: date) -> list[dict]:
    return upit("SELECT * FROM rezervacije WHERE otkazana = 0 AND check_in = :d ORDER BY soba", d=dan.isoformat())


def odlasci_na_dan(dan: date) -> list[dict]:
    return upit("SELECT * FROM rezervacije WHERE otkazana = 0 AND check_out = :d ORDER BY soba", d=dan.isoformat())


def sve_rezervacije() -> pd.DataFrame:
    return upit_df("SELECT * FROM rezervacije ORDER BY check_in DESC, soba")


def otkazi_rezervaciju(id_: int, ko: str) -> None:
    izvrsi(
        "UPDATE rezervacije SET otkazana = 1, otkazao = :ko, otkazano = :t WHERE id = :id",
        ko=ko, t=sada().isoformat(sep=" "), id=id_,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. POMOĆNE FUNKCIJE
# ═════════════════════════════════════════════════════════════════════════════

def sada() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def danas() -> date:
    return sada().date()


def radni_datum(trenutak: datetime | None = None) -> date:
    t = trenutak or sada()
    return (t - timedelta(hours=RADNI_DAN_POCINJE_U)).date()


def u_datum(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def u_vrijeme(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def fmt_d(d) -> str:
    return u_datum(d).strftime("%d.%m.%Y.")


def fmt_dk(d) -> str:
    return u_datum(d).strftime("%d.%m.")


def fmt_dt(v) -> str:
    return u_vrijeme(v).strftime("%d.%m.%Y. %H:%M")


def dugi_datum(d: date) -> str:
    return f"{DANI[d.weekday()]}, {d.day}. {MJESECI_GEN[d.month - 1]} {d.year}."


def dodaj_mjesec(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def kraj_mjeseca(d: date) -> date:
    return dodaj_mjesec(d, 1) - timedelta(days=1)


def broj(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f


def eur(iznos) -> str:
    s = f"{broj(iznos):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{s} €"


def ocisti(tekst) -> str:
    return " ".join(str(tekst or "").split())


def ime_ok(ime: str) -> bool:
    return len(ocisti(ime).split()) >= 2


def noci_tekst(n: int) -> str:
    return f"{n} noć" if n % 10 == 1 and n % 100 != 11 else f"{n} noći"


def inicijali(ime: str) -> str:
    dijelovi = ocisti(ime).split()
    if len(dijelovi) >= 2:
        return (dijelovi[0][0] + dijelovi[-1][0]).upper()
    return ocisti(ime)[:2].upper()


def esc(tekst) -> str:
    return html.escape(str(tekst or ""), quote=True)


def md(tekst) -> str:
    """Escapuje Markdown znakove da imena i napomene ne mijenjaju izgled teksta."""
    return re.sub(r"([\\`*_{}\[\]()#+\-!|>~<$])", r"\\\1", str(tekst or ""))


def raspon_iz_unosa(vrijednost, podrazumijevano: tuple[date, date]) -> tuple[date, date]:
    """st.date_input za raspon vraća 0, 1 ili 2 datuma dok korisnik bira."""
    if isinstance(vrijednost, (tuple, list)):
        if len(vrijednost) == 2:
            return vrijednost[0], vrijednost[1]
        if len(vrijednost) == 1:
            return vrijednost[0], vrijednost[0]
        return podrazumijevano
    if isinstance(vrijednost, date):
        return vrijednost, vrijednost
    return podrazumijevano


def csv_za_excel(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")


# ── Prijava i sesija ─────────────────────────────────────────────────────────

def korisnik() -> dict | None:
    return st.session_state.get("korisnik")


def je_admin() -> bool:
    k = korisnik()
    return bool(k) and k.get("uloga") == "admin"


def ime_korisnika() -> str:
    k = korisnik()
    return k["ime"] if k else ""


def admin_podesen() -> bool:
    return bool(tajna("ADMIN_LOZINKA"))


def provjeri_admina(email: str, lozinka: str) -> bool:
    ocekivana = tajna("ADMIN_LOZINKA")
    if not ocekivana:
        return False
    ocekivani_email = tajna("ADMIN_EMAIL").strip().lower()
    email_ok = (not ocekivani_email) or hmac.compare_digest(
        email.strip().lower().encode(), ocekivani_email.encode()
    )
    lozinka_ok = hmac.compare_digest(lozinka.encode(), ocekivana.encode())
    return email_ok and lozinka_ok


def samo_admin() -> None:
    if not je_admin():
        st.error("Ova stranica je dostupna samo adminu.")
        st.stop()


def _odjava() -> None:
    st.session_state.clear()
    st.query_params.clear()


# ── Zapamćen uređaj ──────────────────────────────────────────────────────────
# Poslije prijave, adresa početne stranice dobija ključ (?k=… za admina,
# ?ime=… za zaposlene). Ikonica na početnom ekranu telefona čuva tu adresu,
# pa se aplikacija otvara odmah prijavljena. Promjena ADMIN_LOZINKA poništava
# sve zapamćene admin uređaje.

def _potpis(tajni_kljuc: str, poruka: str) -> str:
    return hmac.new(tajni_kljuc.encode(), poruka.encode(), hashlib.sha256).hexdigest()[:24]


def admin_kljuc() -> str:
    lozinka = tajna("ADMIN_LOZINKA")
    return _potpis(lozinka, "admin:" + tajna("ADMIN_EMAIL").strip().lower()) if lozinka else ""


def radnik_kljuc() -> str:
    kod = tajna("KOD_OBJEKTA")
    return _potpis(kod, "radnik") if kod else ""


def parametri_uredjaja() -> dict[str, str]:
    k = korisnik()
    if not k:
        return {}
    if k.get("uloga") == "admin":
        return {"k": admin_kljuc()}
    parametri = {"ime": k["ime"]}
    if radnik_kljuc():
        parametri["kod"] = radnik_kljuc()
    return parametri


def zapamti_uredjaj() -> None:
    parametri = parametri_uredjaja()
    if parametri and st.query_params.to_dict() != parametri:
        st.query_params.from_dict(parametri)


def automatska_prijava() -> None:
    if korisnik():
        return
    try:
        k = st.query_params.get("k", "")
        ime = ocisti(st.query_params.get("ime", ""))
        kod = st.query_params.get("kod", "")
    except Exception:
        return
    if k:
        ocekivani = admin_kljuc()
        if ocekivani and hmac.compare_digest(k.encode(), ocekivani.encode()):
            st.session_state["korisnik"] = {"ime": "Admin", "uloga": "admin"}
        return
    if ime_ok(ime):
        ocekivani = radnik_kljuc()
        if not ocekivani or hmac.compare_digest(kod.encode(), ocekivani.encode()):
            st.session_state["korisnik"] = {"ime": ime, "uloga": "radnik"}


# ── Poruke i resetovanje formi poslije uspješnog unosa ───────────────────────

def poruka_uspjeha(tekst: str) -> None:
    st.session_state["_poruka"] = tekst


def prikazi_poruku() -> None:
    tekst = st.session_state.pop("_poruka", None)
    if tekst:
        st.success(tekst)


def zakazi_reset(*prefiksi: str) -> None:
    """Polja čiji ključ počinje datim prefiksom biće prazna pri sljedećem prikazu."""
    st.session_state["_reset"] = list(prefiksi)


def primijeni_reset() -> None:
    for prefiks in st.session_state.pop("_reset", []):
        for kljuc in [k for k in st.session_state.keys() if str(k).startswith(prefiks)]:
            del st.session_state[kljuc]


def iznos(oznaka: str, kljuc: str):
    return st.number_input(
        oznaka, min_value=0.0, value=None, step=1.0, format="%.2f", key=kljuc, placeholder="0,00"
    )


# ── Narudžbe: poruka i linkovi ───────────────────────────────────────────────

def sastavi_poruku(stavke: list[dict], napomena: str, narucio: str, trenutak: datetime) -> str:
    naziv = procitaj_podesavanje("naziv_objekta", "Restoran")
    linije = [f"NARUDŽBINA – {naziv}", f"{fmt_dt(trenutak)} · {narucio}"]
    trenutna_kategorija = None
    for s in stavke:
        if s["kategorija"] != trenutna_kategorija:
            linije += ["", f"{s['kategorija'].upper()}:"]
            trenutna_kategorija = s["kategorija"]
        linije.append(f"• {s['naziv']}")
    if napomena.strip():
        linije += ["", f"Napomena: {napomena.strip()}"]
    return "\n".join(linije)


def normalizuj_telefon(telefon: str) -> str:
    """Vraća samo cifre sa pozivnim brojem (npr. 38267123456)."""
    cifre = re.sub(r"\D", "", telefon or "")
    if cifre.startswith("00"):
        cifre = cifre[2:]
    elif cifre.startswith("0"):
        cifre = "382" + cifre[1:]  # lokalni crnogorski broj → +382
    return cifre


def linkovi_za_poruku(poruka: str, telefon: str) -> dict[str, str]:
    cifre = normalizuj_telefon(telefon)
    t = quote(poruka)
    return {
        "whatsapp": f"https://wa.me/{cifre}?text={t}" if cifre else f"https://wa.me/?text={t}",
        "viber": f"viber://forward?text={t}",
        "sms": f"sms:{'+' + cifre if cifre else ''}?&body={t}",
    }


# ═════════════════════════════════════════════════════════════════════════════
# 4. IZGLED
# ═════════════════════════════════════════════════════════════════════════════

CSS = """<style>
.block-container, [data-testid="stMainBlockContainer"] {max-width: 1080px; padding-top: 1.25rem; padding-bottom: 5rem;}
[data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button, [data-testid="stLinkButton"] a, [data-testid="stDownloadButton"] button {min-height: 3rem; border-radius: 12px; font-weight: 600;}
[data-testid="stCheckbox"] {padding: .55rem .25rem; border-bottom: 1px solid rgba(128,128,128,.18);}
[data-testid="stCheckbox"] label {width: 100%; font-size: 1.05rem;}
input, textarea {font-size: 16px !important;}
h1 {font-size: 1.75rem !important;}
.sobe {display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: .75rem; margin: .25rem 0 1.25rem;}
.soba {border-radius: 14px; padding: .9rem 1rem; border: 1px solid;}
.soba.slobodna {background: rgba(22,163,74,.08); border-color: rgba(22,163,74,.45);}
.soba.zauzeta {background: rgba(220,38,38,.08); border-color: rgba(220,38,38,.45);}
.soba .br {font-size: 1.1rem; font-weight: 700;}
.soba .status {display: inline-block; margin-top: .4rem; padding: .2rem .6rem; border-radius: 999px; font-size: .75rem; font-weight: 700; letter-spacing: .05em; color: #fff;}
.soba.slobodna .status {background: #15803d;}
.soba.zauzeta .status {background: #dc2626;}
.soba .gost {margin-top: .45rem; font-size: .85rem; line-height: 1.35; opacity: .85; overflow-wrap: anywhere;}
.kal-okvir {overflow-x: auto; margin: .25rem 0 1rem;}
table.kal {width: 100%; min-width: 320px; border-collapse: separate !important; border-spacing: 3px; table-layout: fixed; font-size: .8rem; border: none !important; margin: 0;}
table.kal tr {background: transparent !important;}
table.kal th, table.kal td {border: none !important; padding: 0 !important; text-align: center;}
table.kal th {font-weight: 600; opacity: .7; height: 1.8rem;}
table.kal th.dan {text-align: left; width: 4.4rem; white-space: nowrap; padding-left: .2rem !important;}
table.kal td {height: 1.9rem; border-radius: 6px; background: rgba(22,163,74,.13); color: #fff; font-weight: 700; font-size: .72rem; overflow: hidden; white-space: nowrap;}
table.kal td.z {background: #dc2626;}
table.kal tr.vikend th.dan {opacity: .45;}
table.kal tr.danas th.dan {opacity: 1; color: #dc2626;}
table.kal tr.danas td {outline: 2px solid rgba(128,128,128,.55); outline-offset: -1px;}
</style>"""


def ubaci_stil() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# 5. STRANICE
# ═════════════════════════════════════════════════════════════════════════════

# ── Prijava ──────────────────────────────────────────────────────────────────

def stranica_prijava() -> None:
    st.title(procitaj_podesavanje("naziv_objekta", "Restoran"))
    st.caption(dugi_datum(danas()))

    tab_radnik, tab_admin = st.tabs(["Zaposleni", "Admin"])

    with tab_radnik:
        kod_objekta = tajna("KOD_OBJEKTA")
        with st.form("prijava_radnik"):
            ime = st.text_input("Ime i prezime", key="prijava_ime", placeholder="npr. Marko Marković")
            kod = st.text_input("Kod objekta", key="prijava_kod", type="password") if kod_objekta else ""
            udji = st.form_submit_button("Uđi", type="primary", width="stretch")
        if udji:
            ime = ocisti(ime)
            if not ime_ok(ime):
                st.error("Upiši ime i prezime.")
            elif kod_objekta and not hmac.compare_digest(kod.encode(), kod_objekta.encode()):
                st.error("Pogrešan kod objekta.")
            else:
                st.session_state["korisnik"] = {"ime": ime, "uloga": "radnik"}
                st.rerun()

    with tab_admin:
        if not admin_podesen():
            st.info("Admin pristup još nije podešen. Dodaj ADMIN_EMAIL i ADMIN_LOZINKA u Secrets.")
        with st.form("prijava_admin"):
            email = st.text_input("E-mail", key="prijava_email")
            lozinka = st.text_input("Lozinka", key="prijava_lozinka", type="password")
            prijava = st.form_submit_button("Prijava", type="primary", width="stretch")
        if prijava:
            if provjeri_admina(email, lozinka):
                st.session_state["korisnik"] = {"ime": "Admin", "uloga": "admin", "email": email.strip()}
                st.rerun()
            else:
                st.error("Pogrešan e-mail ili lozinka.")


def zaglavlje() -> None:
    uloga = "Admin" if je_admin() else "Zaposleni"
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.caption(f"{uloga}: **{md(ime_korisnika())}**")
        st.button("Odjava", key="odjava", on_click=_odjava, type="tertiary", icon=":material/logout:")


# ── Početna: status soba danas ───────────────────────────────────────────────

def kartice_soba(zauzete: dict[int, dict]) -> None:
    kartice = []
    for s in SOBE:
        r = zauzete.get(s)
        if r:
            kartice.append(
                f'<div class="soba zauzeta"><div class="br">Soba {s}</div>'
                f'<span class="status">ZAUZETA</span>'
                f'<div class="gost">{esc(r["gost"])}<br>do {fmt_dk(r["check_out"])}</div></div>'
            )
        else:
            kartice.append(
                f'<div class="soba slobodna"><div class="br">Soba {s}</div>'
                f'<span class="status">SLOBODNA</span></div>'
            )
    st.markdown('<div class="sobe">' + "".join(kartice) + "</div>", unsafe_allow_html=True)


def stranica_pocetna() -> None:
    zapamti_uredjaj()
    dan = danas()
    st.title(dugi_datum(dan))
    zauzete = {int(r["soba"]): r for r in rezervacije_u_periodu(dan, dan)}
    st.caption(f"Sobe danas · slobodno {BROJ_SOBA - len(zauzete)} od {BROJ_SOBA}")
    kartice_soba(zauzete)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Dolasci danas")
        dolasci = dolasci_na_dan(dan)
        if not dolasci:
            st.caption("Nema dolazaka.")
        for r in dolasci:
            st.markdown(
                f"**Soba {r['soba']}** · {md(r['gost'])} · do {fmt_dk(r['check_out'])} · {r['izvor']}"
                + (f"  \n_{md(r['napomena'])}_" if r["napomena"] else "")
            )
    with c2:
        st.subheader("Odlasci danas")
        odlasci = odlasci_na_dan(dan)
        if not odlasci:
            st.caption("Nema odlazaka.")
        for r in odlasci:
            st.markdown(f"**Soba {r['soba']}** · {md(r['gost'])}")

    st.divider()
    with st.container(horizontal=True):
        if st.button("Kraj smjene", icon=":material/point_of_sale:", width="stretch", key="brzo_smjena"):
            st.switch_page(STRANICE["smjena"])
        if st.button("Nabavka", icon=":material/shopping_cart:", width="stretch", key="brzo_nabavka"):
            st.switch_page(STRANICE["nabavka"])
        if st.button("Nova rezervacija", icon=":material/hotel:", width="stretch", key="brzo_rezervacija"):
            st.switch_page(STRANICE["smjestaj"])

    with st.expander("Aplikacija na telefonu (ikonica na početnom ekranu)"):
        st.markdown(
            "Dok si na ovoj stranici, dodaj je na početni ekran telefona. "
            "Ikonica će otvarati aplikaciju odmah prijavljenu, bez kucanja.\n\n"
            "- **iPhone (Safari):** dugme za dijeljenje (kvadrat sa strelicom) → "
            "**Add to Home Screen / Dodaj na početni ekran** → **Add / Dodaj**\n"
            "- **Android (Chrome):** meni **⋮** gore desno → **Add to Home screen / "
            "Dodaj na početni ekran** → ako pita, izaberi **Create shortcut / Napravi prečicu**\n\n"
            "Ikonica u sebi nosi tvoju prijavu, zato link sa nje ne šalji drugima. "
            "Ako telefon promijeni vlasnika, obriši ikonicu (a za admina promijeni "
            "i ADMIN_LOZINKA u Secrets, što poništava sve zapamćene telefone)."
        )


# ── Kraj smjene: pazar i rashodi ─────────────────────────────────────────────

def stranica_smjena() -> None:
    st.title("Kraj smjene")
    prikazi_poruku()
    st.caption(
        f"Izvještaj se upisuje za **{dugi_datum(radni_datum())}** "
        "Datum i vrijeme unosa bilježe se automatski."
    )

    odjeljenje = st.segmented_control(
        "Odjeljenje", ODJELJENJA, default=KAFANA, key="sm_odjeljenje", width="stretch"
    )
    if not odjeljenje:
        st.info("Odaberi odjeljenje.")
        return

    pazar_kafana = rashod_kafana = pazar_kuhinja = rashod_kuhinja = pazar_smjestaj = None
    opis_kafana = opis_kuhinja = ""

    with st.form("forma_smjena"):
        sef = st.text_input(
            "Ime i prezime šefa smjene",
            value="" if je_admin() else ime_korisnika(),
            key="smf_sef",
        )
        smjena = st.segmented_control("Smjena", SMJENE, key="smf_smjena", width="stretch")
        if odjeljenje == KAFANA:
            pazar_kafana = iznos("Pazar kafana (€)", "smf_pazar_kafana")
            rashod_kafana = iznos("Rashod kafana (€)", "smf_rashod_kafana")
            opis_kafana = st.text_input(
                "Opis rashoda kafane", key="smf_opis_kafana", placeholder="npr. led, hljeb, sredstva za čišćenje"
            )
            pazar_smjestaj = iznos("Pazar od smještaja (€)", "smf_pazar_smjestaj")
        else:
            pazar_kuhinja = iznos("Pazar kuhinja (€)", "smf_pazar_kuhinja")
            rashod_kuhinja = iznos("Rashod kuhinja (€)", "smf_rashod_kuhinja")
            opis_kuhinja = st.text_input(
                "Opis rashoda kuhinje", key="smf_opis_kuhinja", placeholder="npr. povrće sa pijace, plin"
            )
        poslato = st.form_submit_button("Sačuvaj izvještaj", type="primary", width="stretch")

    if not poslato:
        return

    sef = ocisti(sef)
    greske = []
    if not ime_ok(sef):
        greske.append("Upiši ime i prezime šefa smjene.")
    if not smjena:
        greske.append("Odaberi smjenu (Prva ili Druga).")
    if odjeljenje == KAFANA:
        if pazar_kafana is None:
            greske.append("Upiši pazar kafane (može i 0).")
        if pazar_smjestaj is None:
            greske.append("Upiši pazar od smještaja (može i 0).")
        if broj(rashod_kafana) > 0 and not ocisti(opis_kafana):
            greske.append("Upiši kratak opis rashoda kafane.")
    else:
        if pazar_kuhinja is None:
            greske.append("Upiši pazar kuhinje (može i 0).")
        if broj(rashod_kuhinja) > 0 and not ocisti(opis_kuhinja):
            greske.append("Upiši kratak opis rashoda kuhinje.")
    if greske:
        for g in greske:
            st.error(g)
        return

    trenutak = sada()
    sacuvaj_izvjestaj(
        {
            "kreirano": trenutak.isoformat(sep=" "),
            "datum": radni_datum(trenutak).isoformat(),
            "smjena": smjena,
            "odjeljenje": odjeljenje,
            "sef": sef,
            "pazar_kafana": broj(pazar_kafana),
            "rashod_kafana": broj(rashod_kafana),
            "opis_rashod_kafana": ocisti(opis_kafana),
            "pazar_kuhinja": broj(pazar_kuhinja),
            "rashod_kuhinja": broj(rashod_kuhinja),
            "opis_rashod_kuhinja": ocisti(opis_kuhinja),
            "pazar_smjestaj": broj(pazar_smjestaj),
        }
    )
    if odjeljenje == KAFANA:
        sazetak = (
            f"Kafana: pazar {eur(pazar_kafana)}, rashod {eur(rashod_kafana)} · "
            f"Smještaj: pazar {eur(pazar_smjestaj)}"
        )
    else:
        sazetak = f"Kuhinja: pazar {eur(pazar_kuhinja)}, rashod {eur(rashod_kuhinja)}"
    poruka_uspjeha(
        f"Izvještaj je sačuvan ({odjeljenje} · {smjena} · {sef} · {fmt_dt(trenutak)}). {sazetak}"
    )
    zakazi_reset("smf_")
    st.rerun()


# ── Nabavka: štikliranje artikala i slanje narudžbine ────────────────────────

def _prebaci_artikal(id_: int) -> None:
    odabrani = st.session_state.setdefault("nab_odabrani", set())
    if st.session_state.get(f"nab_cb_{id_}"):
        odabrani.add(id_)
    else:
        odabrani.discard(id_)


def _zatvori_narudzbinu() -> None:
    st.session_state.pop("nab_rezultat", None)


def prikazi_rezultat_narudzbine(rezultat: dict) -> None:
    st.success(f"Narudžbina je sačuvana ({rezultat['broj']} art.). Izaberi kako je šalješ:")
    telefon = procitaj_podesavanje("telefon_nabavke")
    linkovi = linkovi_za_poruku(rezultat["poruka"], telefon)
    with st.container(horizontal=True):
        st.link_button("WhatsApp", linkovi["whatsapp"], type="primary", width="stretch")
        st.link_button("Viber", linkovi["viber"], width="stretch")
        st.link_button("SMS", linkovi["sms"], width="stretch")
    with st.expander("Tekst poruke (za kopiranje)"):
        st.code(rezultat["poruka"], language=None, wrap_lines=True)
    if not telefon:
        st.caption("Admin u „Podešavanjima“ može upisati broj dobavljača, pa se poruka otvara direktno njemu.")
    st.button("Gotovo", key="nab_gotovo", on_click=_zatvori_narudzbinu)


def stranica_nabavka() -> None:
    st.title("Nabavka")

    rezultat = st.session_state.get("nab_rezultat")
    if rezultat:
        prikazi_rezultat_narudzbine(rezultat)
        st.divider()

    artikli = svi_artikli()
    if artikli.empty:
        st.info("Još nema artikala. Admin ih dodaje na stranici „Artikli“.")
        return

    st.caption("Označi artikle koji fale, pa pritisni „Pošalji narudžbinu“ na dnu.")
    odabrani: set = st.session_state.setdefault("nab_odabrani", set())
    odabrani.intersection_update(set(artikli["id"].tolist()))

    pretraga = st.text_input(
        "Pretraga", key="nab_pretraga", placeholder="Traži artikal…", label_visibility="collapsed"
    )
    prikaz = artikli
    if ocisti(pretraga):
        prikaz = artikli[artikli["naziv"].str.contains(ocisti(pretraga), case=False, regex=False)]
        if prikaz.empty:
            st.caption("Nema artikala za tu pretragu.")

    for kategorija, grupa in prikaz.groupby("kategorija", sort=False):
        st.subheader(kategorija)
        for red in grupa.itertuples(index=False):
            id_ = int(red.id)
            kljuc = f"nab_cb_{id_}"
            st.session_state[kljuc] = id_ in odabrani
            st.checkbox(red.naziv, key=kljuc, on_change=_prebaci_artikal, args=(id_,))

    st.divider()
    napomena = st.text_area(
        "Napomena (količine, rok isporuke…)",
        key="nab_napomena",
        placeholder="npr. Coca-Cola 2 gajbe, isporuka do 10h",
        height=100,
    )
    izabrani = artikli[artikli["id"].isin(odabrani)]
    if not izabrani.empty:
        st.markdown(f"**Odabrano ({len(izabrani)}):** " + ", ".join(md(n) for n in izabrani["naziv"]))

    if st.button(
        "Pošalji narudžbinu", type="primary", width="stretch", disabled=izabrani.empty, key="nab_posalji"
    ):
        trenutak = sada()
        stavke = [{"naziv": r.naziv, "kategorija": r.kategorija} for r in izabrani.itertuples(index=False)]
        poruka = sastavi_poruku(stavke, napomena, ime_korisnika(), trenutak)
        sacuvaj_narudzbinu(ime_korisnika(), stavke, ocisti(napomena), poruka, trenutak)
        st.session_state["nab_rezultat"] = {"poruka": poruka, "broj": len(stavke)}
        st.session_state["nab_odabrani"] = set()
        zakazi_reset("nab_napomena", "nab_pretraga")
        st.rerun()

    posljednje = posljednje_narudzbine(5)
    if posljednje:
        with st.expander("Posljednje narudžbine"):
            for n in posljednje:
                stavke = json.loads(n["stavke"] or "[]")
                st.markdown(
                    f"**{fmt_dt(n['kreirano'])}** · {md(n['narucio'])}  \n"
                    + ", ".join(md(s["naziv"]) for s in stavke)
                )


# ── Smještaj: rezervacije, kalendar, istorija ────────────────────────────────

def forma_rezervacije() -> None:
    with st.form("forma_rezervacija"):
        gost = st.text_input("Ime i prezime gosta", key="rezf_gost")
        soba = st.pills("Broj sobe", SOBE, format_func=lambda s: f"Soba {s}", key="rezf_soba")
        izvor = st.segmented_control("Izvor rezervacije", IZVORI, key="rezf_izvor", width="stretch")
        c1, c2 = st.columns(2, wrap=False)
        check_in = c1.date_input("Check-in", value=danas(), format="DD.MM.YYYY", key="rezf_in")
        check_out = c2.date_input(
            "Check-out", value=danas() + timedelta(days=1), format="DD.MM.YYYY", key="rezf_out"
        )
        cijena = iznos("Cijena rezervacije (€)", "rezf_cijena")
        napomena = st.text_area(
            "Napomena", key="rezf_napomena", placeholder="npr. Potreban dječiji krevetac u sobi", height=100
        )
        poslato = st.form_submit_button("Sačuvaj rezervaciju", type="primary", width="stretch")

    if not poslato:
        return

    gost = ocisti(gost)
    greske = []
    if len(gost) < 3:
        greske.append("Upiši ime i prezime gosta.")
    if soba is None:
        greske.append("Odaberi sobu.")
    if not izvor:
        greske.append("Odaberi izvor rezervacije (Privatno ili Booking).")
    if not check_in or not check_out or check_out <= check_in:
        greske.append("Check-out mora biti poslije check-in datuma.")
    if cijena is None:
        greske.append("Upiši cijenu rezervacije (može i 0).")
    if not greske:
        for r in preklapanja(int(soba), check_in, check_out):
            greske.append(
                f"Soba {soba} je već zauzeta od {fmt_d(r['check_in'])} do {fmt_d(r['check_out'])} "
                f"(gost: {r['gost']})."
            )
    if greske:
        for g in greske:
            st.error(g)
        return

    trenutak = sada()
    sacuvaj_rezervaciju(
        {
            "gost": gost,
            "soba": int(soba),
            "cijena": broj(cijena),
            "izvor": izvor,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "napomena": ocisti(napomena),
            "upisao": ime_korisnika(),
            "kreirano": trenutak.isoformat(sep=" "),
        }
    )
    noci = (check_out - check_in).days
    poruka_uspjeha(
        f"Rezervacija sačuvana: Soba {soba} · {gost} · {fmt_d(check_in)} → {fmt_d(check_out)} "
        f"({noci_tekst(noci)}) · upisao {ime_korisnika()}, {fmt_dt(trenutak)}"
    )
    zakazi_reset("rezf_")
    st.rerun()


def _kal_pomjeri(smjer: int) -> None:
    d = st.session_state.get("kal_datum") or danas()
    if st.session_state.get("kal_pogled") == "Mjesec":
        st.session_state["kal_datum"] = dodaj_mjesec(d, smjer)
    else:
        st.session_state["kal_datum"] = d + timedelta(days=7 * smjer)


def _kal_danas() -> None:
    st.session_state["kal_datum"] = danas()


def html_kalendar(od: date, do: date, rezervacije: list[dict]) -> str:
    zauzeto: dict[tuple[date, int], dict] = {}
    for r in rezervacije:
        ci, co = u_datum(r["check_in"]), u_datum(r["check_out"])
        d = max(ci, od)
        while d < co and d <= do:
            zauzeto[(d, int(r["soba"]))] = r
            d += timedelta(days=1)

    zaglavlje = '<tr><th class="dan">Soba</th>' + "".join(f"<th>{s}</th>" for s in SOBE) + "</tr>"
    redovi = []
    dan, d = danas(), od
    while d <= do:
        klase = " ".join(k for k, uslov in (("danas", d == dan), ("vikend", d.weekday() >= 5)) if uslov)
        celije = []
        for s in SOBE:
            r = zauzeto.get((d, s))
            if r:
                opis = (
                    f"Soba {s}: {r['gost']} ({r['izvor']}) "
                    f"{fmt_dk(r['check_in'])}–{fmt_dk(r['check_out'])}"
                )
                celije.append(f'<td class="z" title="{esc(opis)}">{esc(inicijali(r["gost"]))}</td>')
            else:
                celije.append("<td></td>")
        redovi.append(
            f'<tr class="{klase}"><th class="dan">{DANI_KRATKO[d.weekday()]} {d.day}.</th>{"".join(celije)}</tr>'
        )
        d += timedelta(days=1)
    return (
        '<div class="kal-okvir"><table class="kal"><thead>'
        + zaglavlje
        + "</thead><tbody>"
        + "".join(redovi)
        + "</tbody></table></div>"
    )


def kalendar_soba() -> None:
    if not st.session_state.get("kal_datum"):
        st.session_state["kal_datum"] = danas()
    pogled = st.segmented_control("Prikaz", ["Sedmica", "Mjesec"], default="Sedmica", key="kal_pogled") or "Sedmica"

    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.button("◀", key="kal_nazad", on_click=_kal_pomjeri, args=(-1,), help="Prethodni period")
        st.date_input("Datum", value=None, key="kal_datum", format="DD.MM.YYYY", label_visibility="collapsed")
        st.button("▶", key="kal_naprijed", on_click=_kal_pomjeri, args=(1,), help="Sljedeći period")
        st.button("Danas", key="kal_danas", on_click=_kal_danas)

    d = st.session_state["kal_datum"]
    if pogled == "Mjesec":
        od, do = d.replace(day=1), kraj_mjeseca(d)
        naslov = f"{MJESECI[d.month - 1]} {d.year}."
    else:
        od = d - timedelta(days=d.weekday())
        do = od + timedelta(days=6)
        naslov = f"{fmt_dk(od)} – {fmt_d(do)}"

    rezervacije = rezervacije_u_periodu(od, do)
    st.markdown(f"**{naslov}**")
    st.markdown(html_kalendar(od, do, rezervacije), unsafe_allow_html=True)
    st.caption("Brojevi gore su sobe · crveno = zauzeto (inicijali gosta) · zeleno = slobodno")

    if rezervacije:
        st.markdown(
            "\n".join(
                f"- **Soba {r['soba']}** · {md(r['gost'])} · {fmt_dk(r['check_in'])} → "
                f"{fmt_dk(r['check_out'])} · {r['izvor']}"
                for r in rezervacije
            )
        )


def lista_rezervacija() -> None:
    filter_ = st.segmented_control(
        "Prikaži", ["Predstojeće", "Sve", "Otkazane"], default="Predstojeće", key="rez_filter"
    ) or "Predstojeće"

    df = sve_rezervacije()
    if df.empty:
        st.info("Još nema rezervacija.")
        return
    df["ci"] = df["check_in"].map(u_datum)
    df["co"] = df["check_out"].map(u_datum)
    df["otkazana"] = df["otkazana"].astype(int)

    if filter_ == "Predstojeće":
        prikaz = df[(df["otkazana"] == 0) & (df["co"] >= danas())].sort_values(["ci", "soba"])
        if prikaz.empty:
            st.caption("Nema predstojećih rezervacija.")
        for r in prikaz.head(100).itertuples(index=False):
            with st.container(border=True):
                st.markdown(f"**Soba {r.soba} · {md(r.gost)}**")
                st.markdown(
                    f"{fmt_d(r.ci)} → {fmt_d(r.co)} · {noci_tekst((r.co - r.ci).days)} · "
                    f"{r.izvor} · {eur(r.cijena)}"
                )
                if r.napomena:
                    st.markdown(f"Napomena: _{md(r.napomena)}_")
                st.caption(f"Upisao: {r.upisao} · {fmt_dt(r.kreirano)}")
    else:
        prikaz = df[df["otkazana"] == 1] if filter_ == "Otkazane" else df
        tabela = pd.DataFrame(
            {
                "Soba": prikaz["soba"].astype(int),
                "Gost": prikaz["gost"],
                "Check-in": prikaz["ci"].map(fmt_d),
                "Check-out": prikaz["co"].map(fmt_d),
                "Noći": [(co - ci).days for ci, co in zip(prikaz["ci"], prikaz["co"])],
                "Cijena": prikaz["cijena"].map(eur),
                "Izvor": prikaz["izvor"],
                "Napomena": prikaz["napomena"],
                "Upisao": prikaz["upisao"],
                "Upisano": prikaz["kreirano"].map(fmt_dt),
                "Status": [
                    f"Otkazana ({ko}, {fmt_dt(kad)})" if otk else "Aktivna"
                    for otk, ko, kad in zip(prikaz["otkazana"], prikaz["otkazao"], prikaz["otkazano"])
                ],
            }
        )
        st.caption(f"{len(tabela)} rezervacija")
        st.dataframe(tabela, hide_index=True)
        if je_admin() and not tabela.empty:
            st.download_button(
                "Preuzmi tabelu (CSV za Excel)",
                csv_za_excel(tabela),
                file_name=f"rezervacije_{danas().isoformat()}.csv",
                mime="text/csv",
                key="rez_csv",
            )

    if je_admin():
        aktivne = df[df["otkazana"] == 0].sort_values(["ci", "soba"])
        if not aktivne.empty:
            with st.expander("Otkaži rezervaciju"):
                opisi = {
                    int(r.id): f"Soba {r.soba} · {r.gost} · {fmt_dk(r.ci)} → {fmt_d(r.co)}"
                    for r in aktivne.itertuples(index=False)
                }
                izbor = st.selectbox(
                    "Rezervacija", list(opisi), format_func=opisi.get, index=None,
                    placeholder="Odaberi rezervaciju", key="rezo_izbor",
                )
                potvrda = st.checkbox("Potvrđujem otkazivanje", key="rezo_potvrda")
                if st.button("Otkaži rezervaciju", disabled=not (izbor and potvrda), key="rezo_dugme"):
                    otkazi_rezervaciju(izbor, ime_korisnika())
                    poruka_uspjeha(f"Otkazano: {opisi[izbor]}")
                    zakazi_reset("rezo_")
                    st.rerun()


def stranica_smjestaj() -> None:
    st.title("Smještaj")
    prikazi_poruku()
    tab_nova, tab_kalendar, tab_lista = st.tabs(["Nova rezervacija", "Kalendar", "Rezervacije"])
    with tab_nova:
        forma_rezervacije()
    with tab_kalendar:
        kalendar_soba()
    with tab_lista:
        lista_rezervacija()


# ── Admin: izvještaji ────────────────────────────────────────────────────────

def izbor_perioda(period: str) -> tuple[date, date, str, str]:
    """Vraća (od, do, naslov, jedinica za grafikon: 'dan' ili 'mjesec')."""
    if period == "Dan":
        d = st.date_input("Datum", value=radni_datum(), format="DD.MM.YYYY", key="izv_dan")
        return d, d, dugi_datum(d), "dan"
    if period == "Sedmica":
        d = st.date_input("Bilo koji dan u sedmici", value=danas(), format="DD.MM.YYYY", key="izv_sedmica")
        od = d - timedelta(days=d.weekday())
        do = od + timedelta(days=6)
        return od, do, f"Sedmica {fmt_dk(od)} – {fmt_d(do)}", "dan"
    godine = godine_za_izbor()
    if period == "Mjesec":
        c1, c2 = st.columns(2, wrap=False)
        mjesec = c1.selectbox(
            "Mjesec", list(range(1, 13)), index=danas().month - 1,
            format_func=lambda m: MJESECI[m - 1], key="izv_mjesec",
        )
        godina = c2.selectbox("Godina", godine, key="izv_godina_m")
        od = date(godina, mjesec, 1)
        return od, kraj_mjeseca(od), f"{MJESECI[mjesec - 1]} {godina}.", "dan"
    if period == "Godina":
        godina = st.selectbox("Godina", godine, key="izv_godina")
        return date(godina, 1, 1), date(godina, 12, 31), f"Godina {godina}.", "mjesec"
    podrazumijevano = (danas() - timedelta(days=29), danas())
    od, do = raspon_iz_unosa(
        st.date_input("Od – do", value=podrazumijevano, format="DD.MM.YYYY", key="izv_raspon"),
        podrazumijevano,
    )
    return od, do, f"{fmt_d(od)} – {fmt_d(do)}", "dan" if (do - od).days <= 92 else "mjesec"


def pripremi_izvjestaje(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for k in NOVAC:
        df[k] = pd.to_numeric(df[k], errors="coerce").fillna(0.0).astype(float)
    df["datum"] = df["datum"].map(u_datum)
    df["kreirano"] = df["kreirano"].map(u_vrijeme)
    df["redosljed_smjene"] = df["smjena"].map(lambda s: SMJENE.index(s) if s in SMJENE else 9)
    for k in ("opis_rashod_kafana", "opis_rashod_kuhinja"):
        df[k] = df[k].fillna("")
    return df


def sumiraj(df: pd.DataFrame) -> dict:
    s = {k: float(df[k].sum()) for k in NOVAC}
    s["prihod"] = s["pazar_kafana"] + s["pazar_kuhinja"] + s["pazar_smjestaj"]
    s["rashod"] = s["rashod_kafana"] + s["rashod_kuhinja"]
    s["neto"] = s["prihod"] - s["rashod"]
    return s


def tabela_po_odjeljenjima(s: dict) -> pd.DataFrame:
    redovi = [
        ("Kafana", s["pazar_kafana"], s["rashod_kafana"]),
        ("Kuhinja", s["pazar_kuhinja"], s["rashod_kuhinja"]),
        ("Smještaj", s["pazar_smjestaj"], 0.0),
        ("UKUPNO", s["prihod"], s["rashod"]),
    ]
    return pd.DataFrame(
        [{"Odjeljenje": o, "Prihod": eur(p), "Rashod": eur(r), "Neto": eur(p - r)} for o, p, r in redovi]
    )


def tabela_po_periodu(df: pd.DataFrame, jedinica: str) -> pd.DataFrame:
    kljuc = df["datum"].map(lambda d: d if jedinica == "dan" else d.replace(day=1))
    g = df.groupby(kljuc)[NOVAC].sum().sort_index()
    redovi = []
    for d, r in g.iterrows():
        prihod = r["pazar_kafana"] + r["pazar_kuhinja"] + r["pazar_smjestaj"]
        rashod = r["rashod_kafana"] + r["rashod_kuhinja"]
        oznaka = f"{DANI_KRATKO[d.weekday()]} {fmt_d(d)}" if jedinica == "dan" else f"{MJESECI[d.month - 1]} {d.year}."
        redovi.append(
            {
                "Period": oznaka,
                "Kafana": eur(r["pazar_kafana"]),
                "Kuhinja": eur(r["pazar_kuhinja"]),
                "Smještaj": eur(r["pazar_smjestaj"]),
                "Prihod": eur(prihod),
                "Rashod": eur(rashod),
                "Neto": eur(prihod - rashod),
            }
        )
    return pd.DataFrame(redovi)


def grafikon_prihoda(df: pd.DataFrame, od: date, do: date, jedinica: str) -> None:
    kljuc = df["datum"].map(lambda d: d if jedinica == "dan" else d.replace(day=1))
    g = df.groupby(kljuc)[["pazar_kafana", "pazar_kuhinja", "pazar_smjestaj"]].sum()
    if jedinica == "dan":
        sve = [od + timedelta(days=i) for i in range((do - od).days + 1)]
    else:
        sve, m = [], od.replace(day=1)
        while m <= do:
            sve.append(m)
            m = dodaj_mjesec(m, 1)
    g = g.reindex(sve, fill_value=0.0)
    g.index = pd.to_datetime(g.index)
    g.columns = ["Kafana", "Kuhinja", "Smještaj"]
    st.bar_chart(g, height=260, y_label="€")


def tabela_smjena(df: pd.DataFrame) -> pd.DataFrame:
    redovi = []
    for r in df.sort_values(["datum", "redosljed_smjene", "odjeljenje", "kreirano"]).itertuples(index=False):
        kafana = r.odjeljenje == KAFANA
        neto = r.pazar_kafana + r.pazar_kuhinja + r.pazar_smjestaj - r.rashod_kafana - r.rashod_kuhinja
        redovi.append(
            {
                "Datum": fmt_d(r.datum),
                "Smjena": r.smjena,
                "Odjeljenje": r.odjeljenje,
                "Šef smjene": r.sef,
                "Upisano": fmt_dt(r.kreirano),
                "Kafana pazar": eur(r.pazar_kafana) if kafana else "",
                "Kafana rashod": eur(r.rashod_kafana) if kafana else "",
                "Opis rashoda kafane": r.opis_rashod_kafana if kafana else "",
                "Smještaj pazar": eur(r.pazar_smjestaj) if kafana else "",
                "Kuhinja pazar": "" if kafana else eur(r.pazar_kuhinja),
                "Kuhinja rashod": "" if kafana else eur(r.rashod_kuhinja),
                "Opis rashoda kuhinje": "" if kafana else r.opis_rashod_kuhinja,
                "Neto": eur(neto),
            }
        )
    return pd.DataFrame(redovi)


def stranica_izvjestaji() -> None:
    samo_admin()
    st.title("Izvještaji")
    prikazi_poruku()

    period = st.segmented_control(
        "Period", ["Dan", "Sedmica", "Mjesec", "Godina", "Raspon"], default="Dan", key="izv_period"
    ) or "Dan"
    od, do, naslov, jedinica = izbor_perioda(period)
    sirovi = izvjestaji_za_period(od, do)

    st.subheader(naslov)
    if period == "Dan":
        upisano = set(zip(sirovi["smjena"], sirovi["odjeljenje"])) if not sirovi.empty else set()
        nedostaju = [f"{s} – {o}" for s in SMJENE for o in ODJELJENJA if (s, o) not in upisano]
        if nedostaju:
            st.warning("Još nije upisano: " + "; ".join(nedostaju))
    if sirovi.empty:
        st.info("Nema unesenih izvještaja za ovaj period.")
        return

    df = pripremi_izvjestaje(sirovi)
    ukupno = sumiraj(df)

    m1, m2, m3 = st.columns(3)
    m1.metric("Prihod", eur(ukupno["prihod"]), border=True)
    m2.metric("Rashod", eur(ukupno["rashod"]), border=True)
    m3.metric("Neto", eur(ukupno["neto"]), border=True)

    st.dataframe(tabela_po_odjeljenjima(ukupno), hide_index=True)

    if period != "Dan":
        st.markdown("#### Prihod po " + ("danima" if jedinica == "dan" else "mjesecima"))
        grafikon_prihoda(df, od, do, jedinica)
        st.dataframe(tabela_po_periodu(df, jedinica), hide_index=True)

    st.markdown("#### Obračun po smjenama")
    smjene = tabela_smjena(df)
    st.dataframe(smjene, hide_index=True)

    rashodi = []
    for r in df.sort_values(["datum", "redosljed_smjene"]).itertuples(index=False):
        for odjeljenje, iznos_r, opis in (
            ("Kafana", r.rashod_kafana, r.opis_rashod_kafana),
            ("Kuhinja", r.rashod_kuhinja, r.opis_rashod_kuhinja),
        ):
            if iznos_r > 0:
                rashodi.append(
                    {"Datum": fmt_d(r.datum), "Smjena": r.smjena, "Odjeljenje": odjeljenje,
                     "Iznos": eur(iznos_r), "Opis": opis, "Upisao": r.sef}
                )
    with st.expander(f"Svi rashodi sa opisom ({len(rashodi)})"):
        if rashodi:
            st.dataframe(pd.DataFrame(rashodi), hide_index=True)
        else:
            st.caption("Nema rashoda u ovom periodu.")

    st.download_button(
        "Preuzmi obračun (CSV za Excel)",
        csv_za_excel(df.drop(columns=["redosljed_smjene"])),
        file_name=f"obracun_{od.isoformat()}_{do.isoformat()}.csv",
        mime="text/csv",
        key="izv_csv",
    )

    with st.expander("Brisanje pogrešnog unosa"):
        opisi = {
            int(r.id): f"{fmt_d(r.datum)} · {r.smjena} · {r.odjeljenje} · {r.sef} ({fmt_dt(r.kreirano)})"
            for r in df.sort_values("kreirano", ascending=False).itertuples(index=False)
        }
        izbor = st.selectbox(
            "Unos", list(opisi), format_func=opisi.get, index=None,
            placeholder="Odaberi unos", key="izvb_izbor",
        )
        potvrda = st.checkbox("Potvrđujem brisanje", key="izvb_potvrda")
        if st.button("Obriši unos", disabled=not (izbor and potvrda), key="izvb_dugme"):
            obrisi_izvjestaj(izbor)
            poruka_uspjeha(f"Obrisano: {opisi[izbor]}")
            zakazi_reset("izvb_")
            st.rerun()


# ── Admin: artikli ───────────────────────────────────────────────────────────

def stranica_artikli() -> None:
    samo_admin()
    st.title("Artikli")
    prikazi_poruku()

    artikli = svi_artikli()
    kategorije = sorted(artikli["kategorija"].unique().tolist(), key=str.casefold) if not artikli.empty else []
    postojeci = set(artikli["naziv"].str.casefold()) if not artikli.empty else set()

    with st.form("forma_artikal"):
        st.markdown("**Novi artikal**")
        naziv = st.text_input("Naziv artikla", key="artf_naziv", placeholder="npr. Coca-Cola 0,25 l")
        kategorija = st.selectbox(
            "Kategorija", kategorije, index=None, accept_new_options=True,
            placeholder="Odaberi ili upiši novu kategoriju", key="artf_kategorija",
        )
        dodaj = st.form_submit_button("Dodaj artikal", type="primary", width="stretch")
    if dodaj:
        naziv, kategorija = ocisti(naziv), ocisti(kategorija)
        if not naziv or not kategorija:
            st.error("Upiši naziv i kategoriju.")
        elif naziv.casefold() in postojeci:
            st.error(f"Artikal „{naziv}“ već postoji.")
        else:
            dodaj_artikal(naziv, kategorija)
            poruka_uspjeha(f"Dodat artikal: {naziv} ({kategorija})")
            zakazi_reset("artf_")
            st.rerun()

    with st.expander("Dodaj više artikala odjednom"):
        with st.form("forma_vise_artikala"):
            kat_vise = st.selectbox(
                "Kategorija", kategorije, index=None, accept_new_options=True,
                placeholder="Odaberi ili upiši novu kategoriju", key="artv_kategorija",
            )
            spisak = st.text_area("Artikli – jedan po redu", key="artv_spisak", height=160)
            dodaj_vise = st.form_submit_button("Dodaj sve", width="stretch")
        if dodaj_vise:
            kat_vise = ocisti(kat_vise)
            nazivi = []
            for linija in (spisak or "").splitlines():
                n = ocisti(linija)
                if n and n.casefold() not in postojeci and n.casefold() not in {x.casefold() for x in nazivi}:
                    nazivi.append(n)
            if not kat_vise:
                st.error("Odaberi kategoriju.")
            elif not nazivi:
                st.error("Nema novih artikala za dodavanje.")
            else:
                for n in nazivi:
                    dodaj_artikal(n, kat_vise)
                poruka_uspjeha(f"Dodato {len(nazivi)} artikala u kategoriju {kat_vise}.")
                zakazi_reset("artv_")
                st.rerun()

    st.markdown("#### Izmjena i brisanje")
    if artikli.empty:
        st.caption("Još nema artikala.")
        return

    opisi = {int(r.id): f"{r.naziv} · {r.kategorija}" for r in artikli.itertuples(index=False)}
    izbor = st.selectbox(
        "Odaberi artikal", list(opisi), format_func=opisi.get, index=None,
        placeholder="Traži artikal…", key="arti_izbor",
    )
    if izbor:
        red = artikli[artikli["id"] == izbor].iloc[0]
        with st.form(f"forma_izmjena_{izbor}"):
            novi_naziv = st.text_input("Naziv", value=red["naziv"], key=f"arte_naziv_{izbor}")
            nova_kat = st.selectbox(
                "Kategorija", kategorije, index=kategorije.index(red["kategorija"]),
                accept_new_options=True, key=f"arte_kat_{izbor}",
            )
            potvrda = st.checkbox("Potvrđujem brisanje (samo za brisanje)", key=f"arte_potvrda_{izbor}")
            c1, c2 = st.columns(2, wrap=False)
            sacuvaj = c1.form_submit_button("Sačuvaj", type="primary", width="stretch")
            obrisi = c2.form_submit_button("Obriši", width="stretch")
        if sacuvaj:
            novi_naziv, nova_kat = ocisti(novi_naziv), ocisti(nova_kat)
            drugi = postojeci - {str(red["naziv"]).casefold()}
            if not novi_naziv or not nova_kat:
                st.error("Naziv i kategorija ne smiju biti prazni.")
            elif novi_naziv.casefold() in drugi:
                st.error(f"Artikal „{novi_naziv}“ već postoji.")
            else:
                izmijeni_artikal(izbor, novi_naziv, nova_kat)
                poruka_uspjeha(f"Sačuvano: {novi_naziv} ({nova_kat})")
                zakazi_reset("arte_", "arti_")
                st.rerun()
        if obrisi:
            if not potvrda:
                st.error("Za brisanje označi „Potvrđujem brisanje“.")
            else:
                obrisi_artikal(izbor)
                poruka_uspjeha(f"Obrisan artikal: {red['naziv']}")
                zakazi_reset("arte_", "arti_")
                st.rerun()

    st.markdown(f"#### Svi artikli ({len(artikli)})")
    st.dataframe(
        artikli[["kategorija", "naziv"]].rename(columns={"kategorija": "Kategorija", "naziv": "Naziv"}),
        hide_index=True,
    )


# ── Admin: istorija narudžbi ─────────────────────────────────────────────────

def stranica_narudzbine() -> None:
    samo_admin()
    st.title("Istorija narudžbi")
    podrazumijevano = (danas() - timedelta(days=30), danas())
    od, do = raspon_iz_unosa(
        st.date_input("Period", value=podrazumijevano, format="DD.MM.YYYY", key="nar_period"),
        podrazumijevano,
    )
    df = narudzbine_za_period(od, do)
    st.caption(f"{len(df)} narudžbi od {fmt_d(od)} do {fmt_d(do)}")
    if df.empty:
        st.info("Nema narudžbi u ovom periodu.")
        return

    for r in df.head(300).itertuples(index=False):
        stavke = json.loads(r.stavke or "[]")
        with st.expander(f"{fmt_dt(r.kreirano)} · {r.narucio} · {len(stavke)} art."):
            po_kategoriji: dict[str, list[str]] = {}
            for s in stavke:
                po_kategoriji.setdefault(s["kategorija"], []).append(s["naziv"])
            for kat, nazivi in po_kategoriji.items():
                st.markdown(f"**{md(kat)}:** " + ", ".join(md(n) for n in nazivi))
            if r.napomena:
                st.markdown(f"Napomena: _{md(r.napomena)}_")

    izvoz = pd.DataFrame(
        {
            "Vrijeme": df["kreirano"].map(fmt_dt),
            "Naručio": df["narucio"],
            "Artikli": df["stavke"].map(lambda s: ", ".join(x["naziv"] for x in json.loads(s or "[]"))),
            "Napomena": df["napomena"],
        }
    )
    st.download_button(
        "Preuzmi (CSV za Excel)",
        csv_za_excel(izvoz),
        file_name=f"narudzbine_{od.isoformat()}_{do.isoformat()}.csv",
        mime="text/csv",
        key="nar_csv",
    )


# ── Admin: podešavanja ───────────────────────────────────────────────────────

def stranica_podesavanja() -> None:
    samo_admin()
    st.title("Podešavanja")
    prikazi_poruku()

    with st.form("forma_podesavanja"):
        naziv = st.text_input(
            "Naziv objekta", value=procitaj_podesavanje("naziv_objekta", "Restoran"), key="pod_naziv"
        )
        telefon = st.text_input(
            "Broj dobavljača za narudžbe (WhatsApp / Viber / SMS)",
            value=procitaj_podesavanje("telefon_nabavke"),
            placeholder="+382 67 123 456",
            key="pod_telefon",
        )
        sacuvaj = st.form_submit_button("Sačuvaj", type="primary", width="stretch")
    if sacuvaj:
        sacuvaj_podesavanje("naziv_objekta", ocisti(naziv) or "Restoran")
        sacuvaj_podesavanje("telefon_nabavke", ocisti(telefon))
        poruka_uspjeha("Podešavanja su sačuvana.")
        zakazi_reset("pod_")
        st.rerun()

    st.markdown("#### Sistem")
    if baza().dialect.name == "sqlite":
        st.warning(
            "Podaci se čuvaju u lokalnom SQLite fajlu. Na Streamlit Community Cloud taj fajl se briše "
            "pri svakom restartu aplikacije. Za trajno čuvanje upiši DATABASE_URL (PostgreSQL) u Secrets."
        )
    else:
        st.success("Baza: PostgreSQL – podaci se čuvaju trajno.")
    if not tajna("KOD_OBJEKTA"):
        st.caption(
            "Zaposleni ulaze samo sa imenom i prezimenom. Ako želiš dodatnu zaštitu linka, "
            "postavi KOD_OBJEKTA u Secrets."
        )


# ═════════════════════════════════════════════════════════════════════════════
# 6. NAVIGACIJA
# ═════════════════════════════════════════════════════════════════════════════

STRANICE: dict = {}


def main() -> None:
    st.set_page_config(
        page_title=procitaj_podesavanje("naziv_objekta", "Restoran"),
        page_icon="🍽️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    primijeni_reset()
    ubaci_stil()
    automatska_prijava()

    if not korisnik():
        st.navigation(
            [st.Page(stranica_prijava, title="Prijava", icon=":material/login:")], position="hidden"
        ).run()
        return

    zaglavlje()

    STRANICE.clear()
    STRANICE.update(
        {
            "pocetna": st.Page(stranica_pocetna, title="Početna", icon=":material/home:", default=True),
            "smjena": st.Page(
                stranica_smjena, title="Kraj smjene", icon=":material/point_of_sale:", url_path="smjena"
            ),
            "nabavka": st.Page(
                stranica_nabavka, title="Nabavka", icon=":material/shopping_cart:", url_path="nabavka"
            ),
            "smjestaj": st.Page(
                stranica_smjestaj, title="Smještaj", icon=":material/hotel:", url_path="smjestaj"
            ),
        }
    )
    rad = list(STRANICE.values())

    if je_admin():
        STRANICE.update(
            {
                "izvjestaji": st.Page(
                    stranica_izvjestaji, title="Izvještaji", icon=":material/bar_chart:", url_path="izvjestaji"
                ),
                "artikli": st.Page(
                    stranica_artikli, title="Artikli", icon=":material/inventory_2:", url_path="artikli"
                ),
                "narudzbine": st.Page(
                    stranica_narudzbine, title="Narudžbe", icon=":material/receipt_long:", url_path="narudzbe"
                ),
                "podesavanja": st.Page(
                    stranica_podesavanja, title="Podešavanja", icon=":material/settings:", url_path="podesavanja"
                ),
            }
        )
        meni = {"Rad": rad, "Admin": [STRANICE[k] for k in ("izvjestaji", "artikli", "narudzbine", "podesavanja")]}
    else:
        meni = rad

    st.navigation(meni, position="top").run()


if __name__ == "__main__":
    main()
