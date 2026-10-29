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

import base64
import hashlib
import hmac
import html
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode
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
PLACANJE = ["Nije plaćeno", "Plaćeno"]  # indeks = vrijednost kolone placeno (0 / 1)

# Koliko često se sobe i kalendar sami osvježavaju na svim uređajima (u sekundama)
OSVJEZAVANJE_SEKUNDI = 20

# Izvještaj upisan poslije ponoći, a prije ovog sata, računa se u prethodni dan
# (npr. druga smjena koja zatvara u 01:00).
RADNI_DAN_POCINJE_U = 6

NOVAC = ["pazar_kafana", "rashod_kafana", "pazar_kuhinja", "rashod_kuhinja", "pazar_smjestaj"]

# Ikonica aplikacije: fajl ikona.png stoji u GitHub repozitorijumu pored app.py.
# IKONA_URL je javna adresa tog fajla (ako se repozitorijum preimenuje, promijeni i ovo).
IKONA_URL = "https://raw.githubusercontent.com/raic1802/restoran-app/main/ikona.png"
IKONA_FAJL = Path(__file__).with_name("ikona.png")
# Fotografija hotela na vrhu ekrana (fajl hotel.jpg u repozitorijumu; pregledač je čuva u memoriji)
HOTEL_URL = "https://raw.githubusercontent.com/raic1802/restoran-app/main/hotel.jpg"
HOTEL_FAJL = Path(__file__).with_name("hotel.jpg")

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
    # ── verzija 2: ko je i kada uredio rezervaciju + istorija izmjena ─────────
    [
        "ALTER TABLE rezervacije ADD COLUMN uredio TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE rezervacije ADD COLUMN uredjeno TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS izmjene_rezervacija (
            id {ID},
            rezervacija_id INTEGER NOT NULL,
            kreirano TEXT NOT NULL,
            ko TEXT NOT NULL,
            opis TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_izmjene_rezervacija ON izmjene_rezervacija (rezervacija_id)",
    ],
    # ── verzija 3: plaćeno / nije plaćeno ────────────────────────────────────
    [
        "ALTER TABLE rezervacije ADD COLUMN placeno INTEGER NOT NULL DEFAULT 0",
    ],
    # ── verzija 4: kome je narudžba poslata ──────────────────────────────────
    [
        "ALTER TABLE narudzbine ADD COLUMN primalac TEXT NOT NULL DEFAULT ''",
    ],
    # ── verzija 5: ovdje dodaj buduće izmjene, npr. ─────────────────────────
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
def _motor(adresa: str):
    url, parametri = pripremi_url(adresa)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args=parametri)
    return create_engine(url, connect_args=parametri, pool_pre_ping=True, pool_recycle=300)


@st.cache_resource(show_spinner=False)
def _sema_azurna(adresa: str, broj_migracija: int) -> bool:
    """Primjenjuje nove migracije čim se u kodu pojavi nova (i bez restarta aplikacije)."""
    _migriraj(_motor(adresa))
    return True


def baza():
    adresa = tajna("DATABASE_URL", "sqlite:///restoran.db")
    _sema_azurna(adresa, len(MIGRACIJE))
    return _motor(adresa)


def citanje(funkcija):
    """Rezultat čitanja iz baze dijele svi uređaji, dok neko ne sačuva izmjenu.

    Tako automatsko osvježavanje (svakih 20 s) ne budi bazu bez potrebe, a čim
    neko upiše nešto (izvrsi), svi uređaji pri sljedećem osvježavanju vide novo stanje.
    """
    return st.cache_data(show_spinner=False, max_entries=500)(funkcija)


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
        broj_redova = conn.execute(text(sql), parametri).rowcount
    st.cache_data.clear()  # svi uređaji odmah čitaju novo stanje
    return broj_redova


# ── Podešavanja ──────────────────────────────────────────────────────────────

@citanje
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


@citanje
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

@citanje
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


def obrisi_artikle(ids: list[int]) -> int:
    """Trajno briše više artikala odjednom (u jednoj transakciji)."""
    with baza().begin() as conn:
        for id_ in ids:
            conn.execute(text("DELETE FROM artikli WHERE id = :id"), {"id": int(id_)})
    st.cache_data.clear()
    return len(ids)


# ── Narudžbine ───────────────────────────────────────────────────────────────

def sacuvaj_narudzbinu(
    narucio: str, stavke: list[dict], napomena: str, poruka: str, trenutak: datetime, primalac: str = ""
) -> None:
    izvrsi(
        """INSERT INTO narudzbine (kreirano, narucio, stavke, napomena, poruka, primalac)
           VALUES (:t, :n, :s, :nap, :p, :pr)""",
        t=trenutak.isoformat(sep=" "), n=narucio, s=json.dumps(stavke, ensure_ascii=False),
        nap=napomena, p=poruka, pr=primalac,
    )


def primaoci() -> list[dict]:
    """Brojevi kojima admin šalje spisak za nabavku, npr. [{"ime": "Tata", "telefon": "+382…"}]."""
    try:
        lista = json.loads(procitaj_podesavanje("primaoci", "[]") or "[]")
    except ValueError:
        lista = []
    lista = [p for p in lista if isinstance(p, dict) and p.get("ime") and p.get("telefon")]
    if not lista and procitaj_podesavanje("telefon_nabavke"):
        lista = [{"ime": "Dobavljač", "telefon": procitaj_podesavanje("telefon_nabavke")}]
    return lista


def sacuvaj_primaoce(lista: list[dict]) -> None:
    sacuvaj_podesavanje("primaoci", json.dumps(lista, ensure_ascii=False))


@citanje
def narudzbine_za_period(od: date, do: date) -> pd.DataFrame:
    return upit_df(
        "SELECT * FROM narudzbine WHERE kreirano >= :od AND kreirano < :do ORDER BY kreirano DESC",
        od=od.isoformat(),
        do=(do + timedelta(days=1)).isoformat(),
    )


@citanje
def posljednje_narudzbine(n: int = 5) -> list[dict]:
    return upit("SELECT * FROM narudzbine ORDER BY kreirano DESC LIMIT :n", n=n)


# ── Rezervacije ──────────────────────────────────────────────────────────────

def preklapanja(soba: int, check_in: date, check_out: date, osim_id: int = 0) -> list[dict]:
    """Aktivne rezervacije iste sobe u tom periodu (osim_id = rezervacija koja se uređuje)."""
    return upit(
        """SELECT * FROM rezervacije
           WHERE otkazana = 0 AND soba = :soba AND check_in < :co AND check_out > :ci AND id <> :osim
           ORDER BY check_in""",
        soba=soba, ci=check_in.isoformat(), co=check_out.isoformat(), osim=int(osim_id),
    )


def rezervacija_po_id(id_: int) -> dict | None:
    redovi = upit("SELECT * FROM rezervacije WHERE id = :id", id=int(id_))
    return redovi[0] if redovi else None


@citanje
def izmjene_rezervacije(id_: int) -> list[dict]:
    return upit(
        "SELECT * FROM izmjene_rezervacija WHERE rezervacija_id = :id ORDER BY kreirano DESC, id DESC",
        id=int(id_),
    )


def zabiljezi_izmjenu(id_: int, ko: str, opis: str, trenutak: datetime) -> None:
    izvrsi(
        "INSERT INTO izmjene_rezervacija (rezervacija_id, kreirano, ko, opis) VALUES (:id, :t, :ko, :opis)",
        id=int(id_), t=trenutak.isoformat(sep=" "), ko=ko, opis=opis,
    )


def izmijeni_rezervaciju(id_: int, zapis: dict, ko: str, opis: str) -> None:
    trenutak = sada()
    izvrsi(
        """UPDATE rezervacije SET gost = :gost, soba = :soba, cijena = :cijena, izvor = :izvor,
               check_in = :check_in, check_out = :check_out, napomena = :napomena,
               uredio = :ko, uredjeno = :t
           WHERE id = :id""",
        **zapis, ko=ko, t=trenutak.isoformat(sep=" "), id=int(id_),
    )
    zabiljezi_izmjenu(id_, ko, opis, trenutak)


SQL_NOVA_REZERVACIJA = """INSERT INTO rezervacije (gost, soba, cijena, izvor, check_in, check_out,
           napomena, upisao, kreirano, placeno)
       VALUES (:gost, :soba, :cijena, :izvor, :check_in, :check_out,
           :napomena, :upisao, :kreirano, :placeno)"""


def sacuvaj_rezervaciju(zapis: dict) -> None:
    izvrsi(SQL_NOVA_REZERVACIJA, **{"placeno": 0, **zapis})


def sacuvaj_rezervacije(zapisi: list[dict]) -> int:
    """Više rezervacija odjednom (uvoz iz fajla) – sve ili ništa, u jednoj transakciji."""
    if not zapisi:
        return 0
    with baza().begin() as conn:
        for zapis in zapisi:
            conn.execute(text(SQL_NOVA_REZERVACIJA), {"placeno": 0, **zapis})
    st.cache_data.clear()
    return len(zapisi)


def aktivne_rezervacije_izmedju(od: date, do: date) -> list[dict]:
    """Aktivne rezervacije koje se preklapaju sa periodom [od, do) – za provjeru uvoza (bez keša)."""
    return upit(
        "SELECT * FROM rezervacije WHERE otkazana = 0 AND check_in < :do AND check_out > :od ORDER BY soba, check_in",
        od=od.isoformat(), do=do.isoformat(),
    )


def postavi_placeno(id_: int, placeno: bool, ko: str) -> bool:
    """Mijenja plaćeno / nije plaćeno i bilježi ko je i kada to promijenio."""
    r = rezervacija_po_id(id_)
    if not r or int(r["placeno"]) == int(placeno):
        return False
    trenutak = sada()
    izvrsi(
        "UPDATE rezervacije SET placeno = :p, uredio = :ko, uredjeno = :t WHERE id = :id",
        p=int(placeno), ko=ko, t=trenutak.isoformat(sep=" "), id=int(id_),
    )
    zabiljezi_izmjenu(
        id_, ko, f"plaćanje: {PLACANJE[int(r['placeno'])]} → {PLACANJE[int(placeno)]}", trenutak
    )
    return True


@citanje
def rezervacije_u_periodu(od: date, do: date) -> list[dict]:
    """Aktivne rezervacije koje zauzimaju bar jednu noć između `od` i `do` (uključivo)."""
    return upit(
        """SELECT * FROM rezervacije
           WHERE otkazana = 0 AND check_in <= :do AND check_out > :od
           ORDER BY soba, check_in""",
        od=od.isoformat(), do=do.isoformat(),
    )


@citanje
def dolasci_na_dan(dan: date) -> list[dict]:
    return upit("SELECT * FROM rezervacije WHERE otkazana = 0 AND check_in = :d ORDER BY soba", d=dan.isoformat())


@citanje
def odlasci_na_dan(dan: date) -> list[dict]:
    return upit("SELECT * FROM rezervacije WHERE otkazana = 0 AND check_out = :d ORDER BY soba", d=dan.isoformat())


@citanje
def sve_rezervacije() -> pd.DataFrame:
    return upit_df("SELECT * FROM rezervacije ORDER BY check_in DESC, soba")


def otkazi_rezervaciju(id_: int, ko: str) -> None:
    trenutak = sada()
    izvrsi(
        "UPDATE rezervacije SET otkazana = 1, otkazao = :ko, otkazano = :t WHERE id = :id",
        ko=ko, t=trenutak.isoformat(sep=" "), id=int(id_),
    )
    zabiljezi_izmjenu(id_, ko, "Rezervacija otkazana", trenutak)


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


def fmt_dtk(v) -> str:
    return u_vrijeme(v).strftime("%d.%m. %H:%M")


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


def cijena_tekst(iznos) -> str:
    return "CIJENA NA UPIT" if broj(iznos) == 0 else eur(iznos)


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


# ── Uvoz rezervacija iz CSV fajla (npr. iz Google kalendara) ─────────────────
# Kolone: soba, gost, check_in, check_out, cijena, izvor, placeno, napomena
# (prve četiri su obavezne; datum kao 2026-10-12 ili 12.10.2026.)
UVOZ_KOLONE = ["soba", "gost", "check_in", "check_out", "cijena", "izvor", "placeno", "napomena"]
UVOZ_UPISAO = "Google kalendar"
UVOZ_DA = {"1", "da", "yes", "true", "x", "placeno", "plaćeno"}


def datum_iz_teksta(tekst) -> date:
    t = ocisti(tekst).rstrip(".")
    for oblik in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(t[:10] if oblik == "%Y-%m-%d" else t, oblik).date()
        except ValueError:
            pass
    raise ValueError(f"neispravan datum: {tekst!r}")


def cijena_iz_teksta(tekst) -> float:
    t = re.sub(r"[^\d,.]", "", str(tekst or ""))
    if "," in t and "." in t:
        t = t.replace(".", "")
    return broj(t.replace(",", "."))


def procitaj_fajl_uvoza(fajl) -> pd.DataFrame:
    """CSV (odvojen zarezom ili tačka-zarezom) → tabela sa kolonama UVOZ_KOLONE, sve kao tekst."""
    sadrzaj = fajl.getvalue() if hasattr(fajl, "getvalue") else fajl.read()
    tekst = sadrzaj.decode("utf-8-sig") if isinstance(sadrzaj, bytes) else str(sadrzaj)
    prvi_red = tekst.split("\n", 1)[0]
    sep = ";" if prvi_red.count(";") > prvi_red.count(",") else ","
    df = pd.read_csv(io.StringIO(tekst), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [ocisti(k).casefold().replace("-", "_").replace(" ", "_") for k in df.columns]
    nema = [k for k in ("soba", "gost", "check_in", "check_out") if k not in df.columns]
    if nema:
        raise ValueError("U fajlu nedostaju kolone: " + ", ".join(nema))
    for k in UVOZ_KOLONE:
        if k not in df.columns:
            df[k] = ""
    return df[UVOZ_KOLONE]


def provjeri_uvoz(df: pd.DataFrame, upisao: str, trenutak: datetime) -> list[dict]:
    """Za svaki red fajla: šta se upisuje ili zašto se preskače (preklapanje, greška)."""
    stavke: list[dict] = []
    for red in df.to_dict("records"):
        s = {
            "Soba": ocisti(red["soba"]), "Gost": ocisti(red["gost"]), "Dolazak": ocisti(red["check_in"]),
            "Odlazak": ocisti(red["check_out"]), "Cijena": "", "Izvor": "", "Plaćeno": "", "Status": "",
            "zapis": None,
        }
        stavke.append(s)
        try:
            soba = int(float(s["Soba"].replace(",", ".")))
            ci, co = datum_iz_teksta(red["check_in"]), datum_iz_teksta(red["check_out"])
        except ValueError:
            s["Status"] = "✗ neispravna soba ili datum"
            continue
        izvor = "Booking" if re.search(r"book|buk", ocisti(red["izvor"]).casefold()) else "Privatno"
        cijena = cijena_iz_teksta(red["cijena"])
        placeno = int(ocisti(red["placeno"]).casefold() in UVOZ_DA)
        s.update(Soba=str(soba), Dolazak=fmt_dk(ci), Odlazak=fmt_dk(co), Cijena=cijena_tekst(cijena),
                 Izvor=izvor, **{"Plaćeno": "da" if placeno else "ne"})
        if soba not in SOBE:
            s["Status"] = f"✗ soba {soba} ne postoji"
        elif not s["Gost"]:
            s["Status"] = "✗ nema imena gosta"
        elif co <= ci:
            s["Status"] = "✗ odlazak mora biti poslije dolaska"
        else:
            s["zapis"] = dict(
                gost=s["Gost"], soba=soba, cijena=cijena, izvor=izvor, check_in=ci.isoformat(),
                check_out=co.isoformat(), napomena=ocisti(red["napomena"]), upisao=upisao,
                kreirano=trenutak.isoformat(sep=" "), placeno=placeno,
            )
    # preklapanja: sa već upisanim rezervacijama (jedan upit) i unutar samog fajla
    ispravne = [s for s in stavke if s["zapis"]]
    if ispravne:
        od = min(date.fromisoformat(s["zapis"]["check_in"]) for s in ispravne)
        do = max(date.fromisoformat(s["zapis"]["check_out"]) for s in ispravne)
        zauzeto = [(int(r["soba"]), str(r["check_in"]), str(r["check_out"]), str(r["gost"]), False)
                   for r in aktivne_rezervacije_izmedju(od, do)]
        for s in ispravne:
            z = s["zapis"]
            sudar = next((x for x in zauzeto if x[0] == z["soba"] and x[1] < z["check_out"] and x[2] > z["check_in"]), None)
            if sudar:
                s["zapis"] = None
                s["Status"] = (f"↷ duplo u fajlu ({sudar[3]})" if sudar[4]
                               else f"↷ već upisano: {sudar[3]} {fmt_dk(sudar[1])}–{fmt_dk(sudar[2])}")
            else:
                s["Status"] = "✓ novo"
                zauzeto.append((z["soba"], z["check_in"], z["check_out"], z["gost"], True))
    return stavke


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
    st.session_state["_odjava"] = True  # ne prijavljuj ponovo iz zapamćenog kolačića


# ── Zapamćena prijava na uređaju ─────────────────────────────────────────────
# Poslije prijave, telefon/računar čuva ključ prijave (kolačić + memorija
# pregledača), pa ostaje prijavljen i kad se aplikacija zatvori, sve dok se
# ne pritisne „Odjava". Ključ je potpisan lozinkom: promjena ADMIN_LOZINKA
# (ili KOD_OBJEKTA) poništava sve zapamćene prijave.

KOLACIC = "vidikovac_prijava"

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


def prijava_za_pamcenje() -> str:
    """Ključ prijave u obliku 'k=…' ili 'ime=…&kod=…' (prazno ako niko nije prijavljen)."""
    parametri = parametri_uredjaja()
    return urlencode(parametri, quote_via=quote) if parametri else ""


def _korisnik_iz_parametara(p: dict) -> dict | None:
    k = str(p.get("k") or "")
    ime = ocisti(p.get("ime"))
    kod = str(p.get("kod") or "")
    if k:
        ocekivani = admin_kljuc()
        if ocekivani and hmac.compare_digest(k.encode(), ocekivani.encode()):
            return {"ime": "Admin", "uloga": "admin"}
        return None
    if ime_ok(ime):
        ocekivani = radnik_kljuc()
        if not ocekivani or hmac.compare_digest(kod.encode(), ocekivani.encode()):
            return {"ime": ime, "uloga": "radnik"}
    return None


def _parametri_iz_kolacica() -> dict:
    try:
        vrijednost = st.context.cookies.get(KOLACIC) or ""
    except Exception:
        return {}
    procitano = parse_qs(unquote(str(vrijednost)))
    return {k: v[0] for k, v in procitano.items() if v}


def automatska_prijava() -> None:
    if korisnik():
        return
    izvori = []
    try:
        izvori.append({k: st.query_params.get(k, "") for k in ("k", "ime", "kod")})
    except Exception:
        pass
    if not st.session_state.get("_odjava"):
        izvori.append(_parametri_iz_kolacica())
    for parametri in izvori:
        nadjen = _korisnik_iz_parametara(parametri)
        if nadjen:
            st.session_state["korisnik"] = nadjen
            return


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
    """Spisak za nabavku, složen po kategorijama:

    NABAVKA – Vidikovac
    24.10.2026. 09:15 · Admin

    PIĆE (2):
    • Coca-Cola 0,25 l
    • Kisjela voda
    """
    naziv = procitaj_podesavanje("naziv_objekta", "Restoran")
    po_kategoriji: dict[str, list[str]] = {}
    for s in stavke:
        po_kategoriji.setdefault(s["kategorija"], []).append(s["naziv"])
    linije = [f"NABAVKA – {naziv}", f"{fmt_dt(trenutak)} · {narucio}"]
    for kategorija, nazivi in po_kategoriji.items():
        linije += ["", f"{kategorija.upper()} ({len(nazivi)}):"]
        linije += [f"• {n}" for n in nazivi]
    if napomena.strip():
        linije += ["", f"Napomena: {napomena.strip()}"]
    linije += ["", f"Ukupno artikala: {len(stavke)}"]
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

# Paleta (po uzoru na priloženi dizajn, sa mat plavom umjesto roze)
PLAVA = "#3E5C8A"          # glavna boja
PLAVA_TAMNA = "#2E4669"
PLAVA_SVIJETLA = "#E9EFF7" # pozadina ikonica
POZADINA = "#F3F4F6"
TEKST = "#1F2430"
SIVA = "#8A8F98"
LINIJA = "#E6E8EC"

CSS = """<style>
.block-container, [data-testid="stMainBlockContainer"] {max-width: 1000px; padding-top: 3.75rem; padding-bottom: 3rem;}
@media (max-width: 640px) {
  .block-container, [data-testid="stMainBlockContainer"] {padding-left: .8rem; padding-right: .8rem;}
}
[data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button, [data-testid="stLinkButton"] a, [data-testid="stDownloadButton"] button {min-height: 3rem; border-radius: 14px; font-weight: 600;}
[data-testid="stCheckbox"] {padding: .55rem .25rem; border-bottom: 1px solid #E6E8EC;}
[data-testid="stCheckbox"] label {width: 100%; font-size: 1.05rem;}
input, textarea {font-size: 16px !important;}
h1 {font-size: 1.6rem !important; font-weight: 700 !important;}

/* ── kartice (bijele, zaobljene, blaga sjenka) ── */
.st-key-sobe_lista, [class*="st-key-kartica_"], .st-key-profil_kartica {background: #fff; border-radius: 20px; padding: 1rem 1rem 1.1rem; box-shadow: 0 2px 14px rgba(20,30,50,.06);}

/* ── vrh početne: slika hotela + profil ── */
.hero {position: relative; border-radius: 24px; min-height: 13.5rem; padding: 1rem 1.1rem 3.2rem; color: #fff; background-color: #2E4669; background-size: cover; background-position: center 55%; display: flex; flex-direction: column; overflow: hidden;}
@media (min-width: 641px) {.hero {min-height: 17rem;}}
.hero-red {display: flex; align-items: center; justify-content: space-between; gap: .6rem;}
.hero-mjesto {display: inline-flex; align-items: center; gap: .3rem; padding: .38rem .8rem; border-radius: 999px; background: rgba(255,255,255,.18); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); font-size: .82rem; font-weight: 600; white-space: nowrap;}
.hero-zvono {position: relative; width: 2.5rem; height: 2.5rem; border-radius: 50%; background: rgba(255,255,255,.18); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); display: flex; align-items: center; justify-content: center;}
.hero-zvono b {position: absolute; top: -.15rem; right: -.15rem; min-width: 1.15rem; height: 1.15rem; padding: 0 .25rem; border-radius: 999px; background: #fff; color: #3E5C8A; font-size: .68rem; display: flex; align-items: center; justify-content: center; box-sizing: border-box;}
.hero svg {width: 1.05rem; height: 1.05rem; fill: currentColor; flex: none;}
.hero-pozdrav {margin-top: auto; padding-top: 1.6rem; font-size: 1.65rem; font-weight: 700; line-height: 1.2;}
.hero-pod {margin-top: .25rem; font-size: .88rem; opacity: .9;}
.avatar {flex: none; width: 2.6rem; height: 2.6rem; border-radius: 50%; object-fit: cover; background: #E9EFF7; color: #3E5C8A; display: inline-flex; align-items: center; justify-content: center; font-weight: 700; font-size: .95rem; border: 2px solid rgba(255,255,255,.85); box-sizing: border-box;}
.st-key-pretraga {margin-top: -2.55rem; padding: 0 1rem; position: relative; z-index: 2;}
.st-key-pretraga [data-baseweb="input"] {border-radius: 14px !important; background: #fff !important; border: none !important; box-shadow: 0 6px 18px rgba(20,30,50,.12); min-height: 3rem;}

/* ── telefon: traka na dnu (sve stranice): Izvještaji · (+) · Kalendar ──
   Sve je UNUTAR trake (plus nije izdignut), pa ništa ne može da se odsiječe. */
.st-key-donja_traka {position: fixed; left: 1rem; right: 1rem; bottom: calc(.75rem + env(safe-area-inset-bottom)); max-width: 18rem; box-sizing: border-box; margin: 0 auto; z-index: 1000; overflow: visible !important; background: rgba(255,255,255,.97); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); border: 1px solid #E6E8EC; border-radius: 28px; box-shadow: 0 12px 30px rgba(20,30,50,.16); padding: .45rem .5rem;}
.st-key-donja_traka [data-testid="stHorizontalBlock"] {gap: 0 !important; align-items: center !important; flex-wrap: nowrap !important;}
.st-key-donja_traka [data-testid="stHorizontalBlock"], .st-key-donja_traka [data-testid="stColumn"], .st-key-donja_traka [data-testid="stVerticalBlock"], .st-key-donja_traka [data-testid="stElementContainer"], .st-key-donja_traka [data-testid="stLayoutWrapper"] {overflow: visible !important; min-width: 0;}
.st-key-donja_traka [data-testid="stColumn"] [data-testid="stVerticalBlock"] {align-items: center; justify-content: center; gap: 0;}
.st-key-donja_traka [data-testid="stPageLink-NavLink"] {flex-direction: column; align-items: center; justify-content: center; gap: .1rem; padding: .35rem .7rem; margin: 0 auto; border-radius: 16px; background: transparent !important; text-decoration: none;}
.st-key-donja_traka [data-testid="stPageLink-NavLink"] * {color: #5B6577 !important; font-size: .7rem; font-weight: 600; letter-spacing: .01em; white-space: nowrap; line-height: 1.2;}
.st-key-donja_traka [data-testid="stPageLink-NavLink"] [data-testid="stIconMaterial"] {font-size: 1.5rem !important; color: #3E5C8A !important; margin: 0 !important;}
.st-key-donja_traka [class*="st-key-dt_aktivan_"] [data-testid="stPageLink-NavLink"] {background: #E9EFF7 !important;}
.st-key-donja_traka [class*="st-key-dt_aktivan_"] [data-testid="stPageLink-NavLink"] * {color: #3E5C8A !important;}
.st-key-donja_traka .st-key-dt_nova {display: flex !important; justify-content: center; align-items: center; width: 100%;}
.st-key-donja_traka .st-key-dt_nova [data-testid="stButton"] {display: flex; justify-content: center; width: 100%;}
.st-key-donja_traka .st-key-dt_nova button {position: relative; display: flex; align-items: center; justify-content: center; flex: none; width: 3.25rem; height: 3.25rem; min-height: 0 !important; padding: 0 !important; margin: 0 auto; border-radius: 50% !important; border: none !important; background: linear-gradient(145deg, #3DB374 0%, #2E9D63 60%, #26895A 100%) !important; color: #fff !important; box-shadow: 0 0 0 4px #E6F5EC, 0 4px 10px rgba(46,157,99,.30);}
.st-key-donja_traka .st-key-dt_nova button:hover, .st-key-donja_traka .st-key-dt_nova button:focus, .st-key-donja_traka .st-key-dt_nova button:active {background: #23804F !important; color: #fff !important;}
.st-key-donja_traka .st-key-dt_nova button > div {gap: 0 !important; margin: 0 !important; justify-content: center;}
.st-key-donja_traka .st-key-dt_nova [data-testid="stMarkdownContainer"] {position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;}
.st-key-donja_traka .st-key-dt_nova [data-testid="stIconMaterial"] {font-size: 1.9rem !important; color: #fff !important; margin: 0 !important;}
[data-testid="stMainBlockContainer"]:has(.st-key-donja_traka), .block-container:has(.st-key-donja_traka) {padding-bottom: 7.5rem;}


/* ── sobe ── */
.st-key-sobe_lista {gap: .4rem !important; margin-bottom: .3rem;}
.sg-naslov {display: flex; flex-direction: column; line-height: 1.2;}
.sg-naslov b {font-size: 1.1rem; color: #1F2430;}
.sg-naslov span {font-size: .8rem; color: #8A8F98;}
.sg-datum {font-size: .85rem; font-weight: 600; color: #1F2430; white-space: nowrap; text-align: center;}
.sg-datum small {display: block; font-size: .68rem; font-weight: 600; color: #3E5C8A;}
.st-key-sobe_glava [data-testid="stButton"] button {min-height: 2.1rem; width: 2.1rem; padding: 0; border-radius: 50%; background: #F1F3F6; border: none; color: #3E5C8A;}
[class*="st-key-otvori_sobu_"] button, [class*="st-key-rezervisi_sobu_"] button {min-height: 2.6rem; padding: 0 .9rem; border-radius: 12px; justify-content: flex-start; text-align: left; box-shadow: none;}
[class*="st-key-otvori_sobu_"] button > div, [class*="st-key-rezervisi_sobu_"] button > div {justify-content: flex-start; flex: 1 1 auto; min-width: 0; overflow: hidden;}
[class*="st-key-otvori_sobu_"] button p, [class*="st-key-rezervisi_sobu_"] button p {font-size: .95rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
[class*="st-key-otvori_sobu_"] button [data-testid="stMarkdownContainer"] {display: flex; align-items: center; flex: 1 1 auto; min-width: 0; overflow: hidden;}
[class*="st-key-otvori_sobu_"] button [data-testid="stMarkdownContainer"] p {flex: 0 1 auto; min-width: 0;}
[class*="st-key-otvori_sobu_"] button [data-testid="stMarkdownContainer"]::after {flex: none; margin-left: .45rem; font-size: .88rem; font-weight: 600; white-space: nowrap;}
[class*="st-key-otvori_sobu_"] button {background: #3E5C8A; border: none; color: #fff;}
[class*="st-key-otvori_sobu_"] button:hover, [class*="st-key-otvori_sobu_"] button:focus {background: #2E4669; color: #fff;}
[class*="st-key-otvori_sobu_"] button::before,
[class*="st-key-otvori_sobu_"] button::after {flex: none; margin-left: .5rem; padding: .2rem .6rem; border-radius: 999px; font-size: .68rem; font-weight: 700; letter-spacing: .04em; line-height: 1.2; white-space: nowrap;}
[class*="st-key-otvori_sobu_"] button::before {order: 2; padding-left: 1.45rem; border: 1px solid rgba(255,255,255,.55); color: #fff; background: var(--pribor-bijeli) .5rem center / .8rem .8rem no-repeat;}
[class*="st-key-otvori_sobu_"] button::after {order: 3;}
[class*="st-key-rezervisi_sobu_"] button {background: #F5F6F8; border: 1px dashed #D5DAE1; color: #8A8F98;}
[class*="st-key-rezervisi_sobu_"] button:hover, [class*="st-key-rezervisi_sobu_"] button:focus {background: #E9EFF7; border-color: #3E5C8A; color: #3E5C8A;}
[class*="st-key-rezervisi_sobu_"] button::after {content: "+"; order: 3; margin-left: auto; padding-left: .8rem; font-size: 1.2rem; font-weight: 600; color: #3E5C8A;}
@media (max-width: 640px) {
  .st-key-sobe_lista, [class*="st-key-kartica_"] {padding: .85rem .75rem .95rem; border-radius: 18px;}
  [class*="st-key-otvori_sobu_"] button, [class*="st-key-rezervisi_sobu_"] button {min-height: 2.45rem; padding: 0 .7rem;}
  [class*="st-key-otvori_sobu_"] button p, [class*="st-key-rezervisi_sobu_"] button p {font-size: .88rem;}
  [class*="st-key-otvori_sobu_"] button::before,
  [class*="st-key-otvori_sobu_"] button::after {margin-left: .35rem; padding: .15rem .45rem; font-size: .58rem;}
  .hero {min-height: 12.5rem;}
  .hero-pozdrav {font-size: 1.45rem;}
}
.kartica-naslov {display: flex; align-items: center; gap: .5rem; font-weight: 700; color: #1F2430; margin-bottom: .2rem;}
.kartica-naslov i {width: 2rem; height: 2rem; border-radius: 50%; background: #E9EFF7; color: #3E5C8A; display: inline-flex; align-items: center; justify-content: center; font-style: normal; font-size: 1rem;}

.st-key-traka_nazad [data-testid="stPageLink-NavLink"] {display: inline-flex; padding: .35rem .8rem; border-radius: 999px; background: #fff; box-shadow: 0 2px 10px rgba(20,30,50,.07);}

/* ── ☰ Meni lijevo gore (telefon i računar; uvijek vidljiv, i kad se skroluje) ── */
.st-key-meni_traka {position: fixed; top: .55rem; left: 1rem; z-index: 999991; width: auto !important;}
.st-key-meni_traka [data-testid="stPopover"] button {min-height: 2.7rem; padding: 0 1.1rem; border-radius: 999px; background: #fff; border: 1px solid #E6E8EC; box-shadow: 0 4px 14px rgba(20,30,50,.10); color: #1F2430; font-weight: 600;}
.st-key-meni_traka [data-testid="stPopover"] button:hover, .st-key-meni_traka [data-testid="stPopover"] button:focus {border-color: #3E5C8A; color: #3E5C8A;}
[data-testid="stPopoverBody"]:has([data-testid="stPageLink-NavLink"]) {min-width: 16rem;}
[data-testid="stPopoverBody"] [data-testid="stPageLink-NavLink"] {padding: .5rem .7rem; border-radius: 12px;}
[data-testid="stPopoverBody"] [data-testid="stPageLink-NavLink"]:hover {background: #E9EFF7;}
[data-testid="stPopoverBody"] [data-testid="stPageLink-NavLink"] [data-testid="stIconMaterial"] {color: #3E5C8A;}

/* ── profil ── */
.profil {display: flex; flex-direction: column; align-items: center; text-align: center; gap: .35rem; padding: .4rem 0 .2rem;}
.profil .avatar {width: 5.5rem; height: 5.5rem; font-size: 1.9rem; border: 3px solid #E9EFF7;}
.profil-ime {font-size: 1.35rem; font-weight: 700; color: #1F2430;}
.profil-uloga {display: inline-block; padding: .2rem .7rem; border-radius: 999px; background: #E9EFF7; color: #3E5C8A; font-size: .75rem; font-weight: 700; letter-spacing: .04em;}
.statistika {display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr)); gap: .6rem; margin-top: .4rem;}
.statistika div {background: #F5F6F8; border-radius: 14px; padding: .65rem .5rem; text-align: center;}
.statistika b {display: block; font-size: 1.35rem; color: #3E5C8A;}
.statistika span {font-size: .72rem; color: #8A8F98;}

/* ── kalendar ── */
.kal-okvir {overflow-x: auto; margin: .25rem 0 1rem; background: #fff; border-radius: 18px; padding: .6rem; box-shadow: 0 2px 14px rgba(20,30,50,.06);}
table.kal {width: 100%; min-width: 320px; border-collapse: separate !important; border-spacing: 3px; table-layout: fixed; font-size: .8rem; border: none !important; margin: 0;}
table.kal tr {background: transparent !important;}
table.kal th, table.kal td {border: none !important; padding: 0 !important; text-align: center;}
table.kal th {font-weight: 600; color: #8A8F98; height: 1.8rem;}
table.kal th.dan {text-align: left; width: 4.4rem; white-space: nowrap; padding-left: .2rem !important;}
table.kal td {height: 1.9rem; border-radius: 6px; background: #EEF1F5; color: #fff; font-weight: 700; font-size: .72rem; overflow: hidden; white-space: nowrap;}
table.kal td.z {background: #3E5C8A;}
table.kal tr.vikend th.dan {opacity: .6;}
table.kal tr.danas th.dan {color: #3E5C8A;}
table.kal tr.danas td {outline: 2px solid rgba(62,92,138,.45); outline-offset: -1px;}
</style>"""


def ubaci_stil() -> None:
    # ikonica viljuške i noža (doručak) – kao CSS varijable, da se koristi i na telefonu i na računaru
    ikone = (
        f"<style>:root {{--pribor-bijeli: {pribor_svg_url('#ffffff')};}}</style>"
    )
    st.markdown(CSS.replace("</style>", "</style>" + ikone, 1), unsafe_allow_html=True)


def ubaci_skriptu() -> None:
    """Pamti prijavu na uređaju i postavlja logo kao ikonicu na početnom ekranu."""
    naziv = procitaj_podesavanje("naziv_objekta", "Restoran")
    vrijednosti = {
        "__IKONA__": IKONA_URL,
        "__NAZIV__": naziv,
        "__PRIJAVA__": prijava_za_pamcenje(),
        "__ODJAVA__": bool(st.session_state.get("_odjava")),
        "__KLJUC__": KOLACIC,
    }
    skripta = """<script>
(function () {
  // ── pamćenje prijave ──
  var KLJUC = __KLJUC__, prijava = __PRIJAVA__, odjava = __ODJAVA__, POKUSAJ = KLJUC + '_pokusaj';
  function kolacic(v, trajanje) {
    var sigurno = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = KLJUC + '=' + encodeURIComponent(v) + '; Max-Age=' + trajanje + '; Path=/; SameSite=Lax' + sigurno;
  }
  try {
    if (prijava) {
      localStorage.setItem(KLJUC, prijava);
      sessionStorage.removeItem(POKUSAJ);
      kolacic(prijava, 34560000);
    } else if (odjava) {
      localStorage.removeItem(KLJUC);
      kolacic('', 0);
    } else {
      var sacuvano = localStorage.getItem(KLJUC);
      if (sacuvano && !sessionStorage.getItem(POKUSAJ)) {
        sessionStorage.setItem(POKUSAJ, '1');
        location.replace(location.pathname + '?' + sacuvano);
      } else if (sacuvano) {
        localStorage.removeItem(KLJUC);
        kolacic('', 0);
      }
    }
  } catch (e) {}

  // ── ikonica na početnom ekranu ──
  var ikona = __IKONA__, naziv = __NAZIV__;
  if (!ikona) return;
  function meta(d, ime, sadrzaj) {
    var m = d.querySelector('meta[name="' + ime + '"]');
    if (!m) { m = d.createElement('meta'); m.setAttribute('name', ime); d.head.appendChild(m); }
    m.setAttribute('content', sadrzaj);
  }
  function link(d, rel, velicina) {
    var l = d.createElement('link');
    l.setAttribute('rel', rel); l.setAttribute('href', ikona); l.setAttribute('data-ikona', '1');
    if (velicina) { l.setAttribute('sizes', velicina); l.setAttribute('type', 'image/png'); }
    d.head.appendChild(l);
  }
  function postavi(d) {
    if (!d || !d.head) return;
    d.querySelectorAll('link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"], link[data-ikona]')
      .forEach(function (e) { e.remove(); });
    link(d, 'apple-touch-icon', '');
    link(d, 'icon', '512x512');
    meta(d, 'apple-mobile-web-app-title', naziv);
    meta(d, 'application-name', naziv);
    meta(d, 'apple-mobile-web-app-capable', 'yes');
    meta(d, 'mobile-web-app-capable', 'yes');
  }
  postavi(document);
  try { if (window.top && window.top !== window) { postavi(window.top.document); } } catch (e) {}
})();
</script>"""
    for oznaka, vrijednost in vrijednosti.items():
        skripta = skripta.replace(oznaka, json.dumps(vrijednost).replace("</", "<\\/"))
    st.html(skripta, unsafe_allow_javascript=True)


# ═════════════════════════════════════════════════════════════════════════════
# 5. STRANICE
# ═════════════════════════════════════════════════════════════════════════════

# ── Prijava ──────────────────────────────────────────────────────────────────

def stranica_prijava() -> None:
    hero("Dobro došli 👋", dugi_datum(danas()), prijavljen=False)

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
                st.session_state.pop("_odjava", None)
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
                st.session_state.pop("_odjava", None)
                st.rerun()
            else:
                st.error("Pogrešan e-mail ili lozinka.")


UPUTSTVO_IKONICA = (
    "Ovaj telefon pamti prijavu dok ne pritisneš **Odjava**.\n\n"
    "Da dodaš ikonicu na početni ekran:\n"
    "- **iPhone (Safari):** dugme za dijeljenje (kvadrat sa strelicom) → "
    "**Add to Home Screen / Dodaj na početni ekran** → **Add / Dodaj**\n"
    "- **Android (Chrome):** meni **⋮** → **Add to Home screen / Dodaj na početni ekran** → "
    "ako pita, izaberi **Create shortcut / Napravi prečicu**\n\n"
    "Kad prvi put otvoriš ikonicu, prijavi se još jednom. Poslije toga ulaziš direktno.\n\n"
    "Ako se telefon izgubi, promijeni ADMIN_LOZINKA u Secrets na Streamlitu. "
    "Tako se poništavaju sve zapamćene prijave."
)


PRIBOR_PUTANJA = (
    "M280-80v-366q-51-14-85.5-56T160-600v-280h80v280h40v-280h80v280h40v-280h80v280q0 56-34.5 98T360-446v366h-80Z"
    "m400 0v-320H560v-280q0-83 58.5-141.5T760-880v800h-80Z"
)


def pribor_svg_url(boja: str) -> str:
    """Viljuška i nož kao mala vektorska slika (data URL za CSS)."""
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 -960 960 960' fill='{boja}'>"
        f"<path d='{PRIBOR_PUTANJA}'/></svg>"
    )
    return 'url("data:image/svg+xml,' + quote(svg, safe="/:=' ") + '")'


def cijena_kratko(iznos) -> str:
    """Kraća cijena za usku traku na telefonu: 54 €, 58,50 €, NA UPIT."""
    v = broj(iznos)
    if v == 0:
        return "NA UPIT"
    return f"{int(v)} €" if v == int(v) else eur(v)


def natpis_trake(s: int, r: dict, telefon: bool) -> str:
    """Natpis zauzete sobe: broj sobe, ime gosta, cijena (kao u Google Kalendaru).

    Na telefonu natpis je samo broj sobe i ime; cijenu iza imena dodaje CSS (lista_soba),
    tako da se predugo ime skrati sa „…“, a cijena uvijek ostane vidljiva.
    """
    ime = md(ocisti(r["gost"]))
    if telefon:
        return f"**{s}**  {ime}"
    return f"**{s}**  {ime}  ·  {cijena_tekst(r['cijena'])}"


def ima_dorucak(r: dict) -> bool:
    """Rezervacije preko Bookinga imaju uključen doručak."""
    return str(r.get("izvor") or "") == "Booking"


def je_telefon() -> bool:
    """Telefon: fiksna traka na dnu i kraći natpisi soba; računar/tablet: samo ☰ Meni."""
    try:
        ua = str(st.context.headers.get("user-agent") or "").lower()
    except Exception:
        return False
    return any(oznaka in ua for oznaka in ("iphone", "ipod", "android", "mobile"))


# ── Ikonice (vektorske, iste boje kao tekst) ─────────────────────────────────
IKONA_MJESTO = (
    '<svg viewBox="0 0 24 24"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z'
    'm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/></svg>'
)
IKONA_ZVONO = (
    '<svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4'
    'c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>'
)


# ── Slike: hotel i profili (čuvaju se u bazi, smanjene) ─────────────────────

def slika_u_base64(fajl, najvise_px: int, kvadrat: bool = False) -> str:
    """Smanji sliku (JPG/PNG sa telefona) i vrati je kao tekst za bazu."""
    from PIL import Image, ImageOps

    slika = ImageOps.exif_transpose(Image.open(fajl)).convert("RGB")
    if kvadrat:
        strana = min(slika.size)
        lijevo, gore = (slika.width - strana) // 2, (slika.height - strana) // 2
        slika = slika.crop((lijevo, gore, lijevo + strana, gore + strana))
    slika.thumbnail((najvise_px, najvise_px))
    bafer = io.BytesIO()
    slika.save(bafer, "JPEG", quality=80, optimize=True, progressive=True)
    return base64.b64encode(bafer.getvalue()).decode("ascii")


def slika_url(kljuc: str) -> str:
    b64 = procitaj_podesavanje(kljuc)
    return f"data:image/jpeg;base64,{b64}" if b64 else ""


def slika_hotela_url() -> str:
    """Slika izabrana u Podešavanjima, a ako je nema – hotel.jpg iz repozitorijuma."""
    return slika_url("slika_hotela") or (HOTEL_URL if HOTEL_FAJL.exists() else "")


def kljuc_avatara(ime: str) -> str:
    return "avatar:" + ocisti(ime).casefold()


def prikazno_ime() -> str:
    """Ime u pozdravu i na profilu (admin može upisati svoje ime u Profilu)."""
    if je_admin():
        return procitaj_podesavanje("admin_ime") or "Admin"
    return ime_korisnika()


def _sacuvaj_sliku(kljuc_widgeta: str, kljuc_podesavanja: str, najvise_px: int, kvadrat: bool, poruka: str) -> None:
    fajl = st.session_state.get(kljuc_widgeta)
    if fajl is None:
        return
    try:
        sacuvaj_podesavanje(kljuc_podesavanja, slika_u_base64(fajl, najvise_px, kvadrat))
        poruka_uspjeha(poruka)
    except Exception:
        st.session_state["_greska_slike"] = "Ova slika ne može da se otvori. Probaj drugu (JPG ili PNG)."
    st.session_state["_upload_brojac"] = int(st.session_state.get("_upload_brojac", 0)) + 1  # prazan izbor


def izbor_slike(oznaka: str, kljuc_podesavanja: str, najvise_px: int, kvadrat: bool, poruka: str) -> None:
    kljuc = f"upl_{kljuc_podesavanja}_{st.session_state.get('_upload_brojac', 0)}"
    st.file_uploader(
        oznaka, type=["jpg", "jpeg", "png"], key=kljuc, on_change=_sacuvaj_sliku,
        args=(kljuc, kljuc_podesavanja, najvise_px, kvadrat, poruka),
    )
    greska = st.session_state.pop("_greska_slike", None)
    if greska:
        st.error(greska)


# ── Zajednički dijelovi ekrana ───────────────────────────────────────────────

def avatar_html(ime: str, kljuc_slike: str, klasa: str = "avatar") -> tuple[str, str]:
    """Krug sa slikom profila ili inicijalima. Vraća (html, css)."""
    url = slika_url(kljuc_slike) if kljuc_slike else ""
    if url:
        oznaka = "av-" + hashlib.md5(kljuc_slike.encode()).hexdigest()[:8]
        return (
            f'<span class="{klasa} {oznaka}"></span>',
            f".{oznaka} {{background: center / cover no-repeat url('{url}');}}",
        )
    return f'<span class="{klasa}">{esc(inicijali(ime) or "?")}</span>', ""


def hero(naslov: str, podnaslov: str, prijavljen: bool) -> None:
    """Vrh ekrana: slika hotela (ili plava pozadina), profil, objekat, pozdrav."""
    naziv = procitaj_podesavanje("naziv_objekta", "Restoran")
    slika = slika_hotela_url()
    zatamnjenje = "linear-gradient(180deg, rgba(15,23,38,.45) 0%, rgba(15,23,38,.12) 45%, rgba(15,23,38,.72) 100%)"
    stil = [
        f".hero-pozadina {{background-image: {zatamnjenje}, url('{slika}');}}" if slika
        else ".hero-pozadina {background-image: linear-gradient(135deg, #2E4669 0%, #3E5C8A 60%, #5679AE 100%);}"
    ]
    if prijavljen:
        av_html, av_css = avatar_html(prikazno_ime(), kljuc_avatara(ime_korisnika()))
        stil.append(av_css)
        dolasci = len(dolasci_na_dan(danas()))
        zvono = (
            f'<span class="hero-zvono" title="Dolasci danas">{IKONA_ZVONO}'
            + (f"<b>{dolasci}</b>" if dolasci else "") + "</span>"
        )
        red = f'<div class="hero-red">{av_html}<span class="hero-mjesto">{IKONA_MJESTO}{esc(naziv)}</span>{zvono}</div>'
    else:
        red = f'<div class="hero-red"><span class="hero-mjesto">{IKONA_MJESTO}{esc(naziv)}</span></div>'
    st.html(
        "<style>" + "\n".join(x for x in stil if x) + "</style>"
        f'<div class="hero hero-pozadina">{red}'
        f'<div class="hero-pozdrav">{esc(naslov)}</div><div class="hero-pod">{esc(podnaslov)}</div></div>'
    )


def traka_nazad() -> None:
    with st.container(key="traka_nazad"):
        st.page_link(STRANICE["pocetna"], label="Početna", icon=":material/arrow_back:")


STAVKE_RAD = [
    ("pocetna", "Početna", ":material/home:"),
    ("smjena", "Kraj smjene", ":material/point_of_sale:"),
    ("smjestaj", "Rezervacije", ":material/hotel:"),
    ("kalendar", "Kalendar", ":material/calendar_month:"),
]
STAVKE_ADMIN = [
    ("nabavka", "Nabavka", ":material/shopping_cart:"),
    ("izvjestaji", "Izvještaji", ":material/bar_chart:"),
    ("artikli", "Artikli", ":material/inventory_2:"),
    ("narudzbine", "Narudžbe", ":material/receipt_long:"),
    ("podesavanja", "Podešavanja", ":material/settings:"),
]


def glavni_meni(aktivna: str) -> None:
    """Dugme ☰ Meni lijevo gore (telefon i računar, uvijek vidljivo) – sve stranice i odjava na jednom mjestu."""
    if st.session_state.get("_meni_stranica") != aktivna:
        st.session_state["meni"] = False  # poslije prelaska na drugu stranicu meni se zatvori
    st.session_state["_meni_stranica"] = aktivna
    with st.container(key="meni_traka", width="content"):
        with st.popover("Meni", icon=":material/menu:", key="meni", on_change="rerun"):
            st.caption(f"{prikazno_ime()} · {'admin' if je_admin() else 'zaposleni'}")
            for kljuc, naziv, ikona in STAVKE_RAD:
                st.page_link(STRANICE[kljuc], label=naziv, icon=ikona, width="stretch")
            if je_admin():
                st.caption("Administracija")
                for kljuc, naziv, ikona in STAVKE_ADMIN:
                    st.page_link(STRANICE[kljuc], label=naziv, icon=ikona, width="stretch")
            st.divider()
            st.page_link(STRANICE["profil"], label="Profil", icon=":material/person:", width="stretch")
            if st.button("Odjava", key="meni_odjava", icon=":material/logout:", width="stretch"):
                _odjavi_se()


# ── Početna ──────────────────────────────────────────────────────────────────

def lista_soba(zauzete: dict[int, dict], dan: date) -> None:
    """Sobe jedna ispod druge (računar i telefon), u bijeloj kartici.

    Traka prikazuje broj sobe, ime gosta i cijenu; sve ostalo se vidi dodirom (prozor sobe).
    Oznake plaćanja i doručka idu na desni kraj trake (CSS ::before / ::after, bez JavaScripta).
    Na telefonu je oznaka doručka samo viljuška i nož, da sve stane u jedan red.
    """
    slobodno = BROJ_SOBA - len(zauzete)
    telefon = je_telefon()
    je_danas = dan == danas()
    placeno, nije, dorucak, cijene = [], [], [], []
    with st.container(key="sobe_lista", gap="xxsmall"):
        with st.container(horizontal=True, vertical_alignment="center", key="sobe_glava", gap="small"):
            st.html(f'<div class="sg-naslov"><b>Sobe</b><span>slobodno {slobodno} od {BROJ_SOBA}</span></div>')
            st.button("◀", key="dan_nazad", on_click=_pomjeri_dan, args=(-1,), help="Prethodni dan")
            st.html(
                f'<div class="sg-datum">{"<small>DANAS</small>" if je_danas else ""}'
                f"{DANI_KRATKO[dan.weekday()]} {dan.day}.{dan.month}.</div>",
                width="content",
            )
            st.button("▶", key="dan_naprijed", on_click=_pomjeri_dan, args=(1,), help="Sljedeći dan")
        if not je_danas:
            st.button("Vrati na danas", key="dan_danas", on_click=_vrati_na_danas, icon=":material/today:",
                      type="tertiary")
        for s in SOBE:
            r = zauzete.get(s)
            if r:
                sel = f".st-key-otvori_sobu_{s} button"
                (placeno if int(r.get("placeno") or 0) else nije).append(sel + "::after")
                if ima_dorucak(r):
                    dorucak.append(sel + "::before")
                if telefon:  # cijena odmah iza imena; nikad se ne skraćuje (ime se skrati sa „…“)
                    cijena = json.dumps("· " + cijena_kratko(r["cijena"]), ensure_ascii=False)
                    cijene.append(f'{sel} [data-testid="stMarkdownContainer"]::after {{content: {cijena};}}')
                if st.button(natpis_trake(s, r, telefon), key=f"otvori_sobu_{s}", width="stretch", wrap=False):
                    otvori_dijalog({"tip": "rezervacija", "id": int(r["id"])})
            else:
                if st.button(f"**{s}**  slobodna", key=f"rezervisi_sobu_{s}", width="stretch", wrap=False):
                    otvori_dijalog({"tip": "nova", "soba": s, "dan": dan.isoformat()})
    pravila = []
    if placeno:
        pravila.append(", ".join(placeno) + ' {content: "PLAĆENO"; background: #E3F4E8; color: #1E7B3A;}')
    if nije:
        pravila.append(", ".join(nije) + ' {content: "NIJE PLAĆENO"; background: #FFF0D4; color: #8A5300;}')
    if dorucak:
        if telefon:
            pravila.append(
                ", ".join(dorucak)
                + ' {content: ""; width: 1.55rem !important; height: 1.3rem !important; padding: 0 !important;'
                + ' background-position: center !important;}'
            )
        else:
            pravila.append(", ".join(dorucak) + ' {content: "DORUČAK";}')
    pravila += cijene
    if pravila:
        st.html("<style>" + "\n".join(pravila) + "</style>")


@st.fragment(run_every=OSVJEZAVANJE_SEKUNDI)
def stanje_soba_uzivo() -> None:
    """Sobe, dolasci i odlasci za izabrani dan – samo se osvježava na svim uređajima."""
    dan = izabrani_dan()
    je_danas = dan == danas()
    zauzete = {int(r["soba"]): r for r in rezervacije_u_periodu(dan, dan)}
    lista_soba(zauzete, dan)

    c1, c2 = st.columns(2)
    with c1, st.container(key="kartica_dolasci"):
        st.markdown(f":material/login: **{'Dolasci danas' if je_danas else 'Dolasci'}**")
        dolasci = dolasci_na_dan(dan)
        if not dolasci:
            st.caption("Nema dolazaka.")
        for r in dolasci:
            st.markdown(
                f"**Soba {r['soba']}** · {md(r['gost'])} · do {fmt_dk(r['check_out'])} · {r['izvor']} · "
                f"{'plaćeno' if int(r.get('placeno') or 0) else 'nije plaćeno'}"
                + (" · :material/restaurant: doručak" if ima_dorucak(r) else "")
                + (f"  \n_{md(r['napomena'])}_" if r["napomena"] else "")
            )
            st.caption(f"upisao {r['upisao']} · {fmt_dtk(r['kreirano'])}")
    with c2, st.container(key="kartica_odlasci"):
        st.markdown(f":material/logout: **{'Odlasci danas' if je_danas else 'Odlasci'}**")
        odlasci = odlasci_na_dan(dan)
        if not odlasci:
            st.caption("Nema odlazaka.")
        for r in odlasci:
            st.markdown(f"**Soba {r['soba']}** · {md(r['gost'])}")


@st.fragment(run_every=OSVJEZAVANJE_SEKUNDI)
def kalendar_uzivo() -> None:
    kalendar_soba()


def donja_traka(aktivna: str) -> None:
    """Telefon: traka koja stoji na dnu ekrana (na svim stranicama), uvijek pri ruci.

    Admin:  Izvještaji · (+) · Kalendar      Radnik: Rezervacije · (+) · Kalendar
    Zeleni plus u sredini otvara novu rezervaciju. Sve ostalo je u ☰ Meni.
    """
    lijevo = (("izvjestaji", "Izvještaji", ":material/analytics:") if je_admin()
              else ("smjestaj", "Rezervacije", ":material/hotel:"))
    stavke = [lijevo, ("nova", "Nova rezervacija", ":material/add:"),
              ("kalendar", "Kalendar", ":material/calendar_month:")]
    with st.container(key="donja_traka"):
        kolone = st.columns(len(stavke), wrap=False, gap="small", vertical_alignment="center")
        for kolona, (kljuc, naziv, ikona) in zip(kolone, stavke):
            with kolona:
                if kljuc == "nova":
                    if st.button(naziv, key="dt_nova", icon=ikona):
                        dan = izabrani_dan() if aktivna == "pocetna" else danas()
                        otvori_dijalog({"tip": "nova", "dan": dan.isoformat()})
                else:
                    with st.container(key=("dt_aktivan_" if kljuc == aktivna else "dt_") + kljuc):
                        st.page_link(STRANICE[kljuc], label=naziv, icon=ikona)


def pretraga_gostiju() -> None:
    with st.container(key="pretraga"):
        trazi = st.text_input(
            "Pretraga", key="pretraga_gosta", placeholder="Traži gosta ili sobu…",
            label_visibility="collapsed", icon=":material/search:",
        )
    tekst = ocisti(trazi).casefold()
    if not tekst:
        return
    df = sve_rezervacije()
    rezultati = []
    if not df.empty:
        for r in df.itertuples(index=False):
            if int(r.otkazana):
                continue
            if tekst in str(r.gost).casefold() or tekst in (str(r.soba), f"soba {r.soba}"):
                rezultati.append(r)
    rezultati.sort(key=lambda r: abs((u_datum(r.check_in) - danas()).days))
    with st.container(key="kartica_pretraga"):
        st.markdown(f":material/search: **Rezultati** ({len(rezultati)})")
        if not rezultati:
            st.caption("Nema rezervacija za tu pretragu.")
        for r in rezultati[:8]:
            natpis = f"**{r.soba}**  {md(r.gost)}  ·  {fmt_dk(r.check_in)} – {fmt_dk(r.check_out)}"
            if st.button(natpis, key=f"trazi_{int(r.id)}", width="stretch", wrap=False):
                otvori_dijalog({"tip": "rezervacija", "id": int(r.id)})


def izabrani_dan() -> date:
    """Dan prikazan na Početnoj (pomak od danas, pa se poslije ponoći sam pomjeri)."""
    return danas() + timedelta(days=int(st.session_state.get("pocetna_pomak", 0)))


def _pomjeri_dan(n: int) -> None:
    st.session_state["pocetna_pomak"] = int(st.session_state.get("pocetna_pomak", 0)) + n


def _vrati_na_danas() -> None:
    st.session_state["pocetna_pomak"] = 0


def stranica_pocetna() -> None:
    zapamti_uredjaj()
    dan = danas()
    slobodno = BROJ_SOBA - len({int(r["soba"]) for r in rezervacije_u_periodu(dan, dan)})
    hero(
        f"Zdravo, {prikazno_ime().split()[0]} 👋",
        f"{DANI[dan.weekday()]}, {dan.day}. {MJESECI_GEN[dan.month - 1]} · slobodno {slobodno} od {BROJ_SOBA}",
        prijavljen=True,
    )
    pretraga_gostiju()
    prikazi_poruku()
    stanje_soba_uzivo()


# ── Profil ───────────────────────────────────────────────────────────────────

def _odjavi_se() -> None:
    """Poziva se iz tijela stranice (ne iz on_click), jer Streamlit u on_click ignoriše promjenu stranice."""
    _odjava()
    st.switch_page(STRANICE["pocetna"])


def stranica_profil() -> None:
    prikazi_poruku()
    ime = prikazno_ime()
    av_html, av_css = avatar_html(ime, kljuc_avatara(ime_korisnika()))
    pocetak_mjeseca = danas().replace(day=1)
    moje = 0
    df = sve_rezervacije()
    if not df.empty:
        moje = int(((df["upisao"] == ime_korisnika()) & (df["kreirano"].astype(str) >= pocetak_mjeseca.isoformat())).sum())
    izmjene = int(jedna_vrijednost(
        "SELECT COUNT(*) FROM izmjene_rezervacija WHERE ko = :ko AND kreirano >= :od",
        ko=ime_korisnika(), od=pocetak_mjeseca.isoformat(),
    ) or 0)
    with st.container(key="profil_kartica"):
        st.html(
            (f"<style>{av_css}</style>" if av_css else "")
            + f'<div class="profil">{av_html}<div class="profil-ime">{esc(ime)}</div>'
            f'<span class="profil-uloga">{"ADMIN" if je_admin() else "ZAPOSLENI"}</span></div>'
            f'<div class="statistika"><div><b>{moje}</b><span>rezervacija ovog mjeseca</span></div>'
            f'<div><b>{izmjene}</b><span>izmjena ovog mjeseca</span></div></div>'
        )

    with st.expander("Slika profila", icon=":material/photo_camera:"):
        izbor_slike("Izaberi sliku", kljuc_avatara(ime_korisnika()), 400, True, "Slika profila je sačuvana.")
        if procitaj_podesavanje(kljuc_avatara(ime_korisnika())):
            if st.button("Ukloni sliku", key="avatar_ukloni", icon=":material/delete:", type="tertiary"):
                sacuvaj_podesavanje(kljuc_avatara(ime_korisnika()), "")
                poruka_uspjeha("Slika profila je uklonjena.")
                st.rerun()

    if je_admin():
        with st.expander("Ime za prikaz", icon=":material/badge:"):
            with st.form("forma_admin_ime", border=False):
                novo = st.text_input("Ime i prezime", value=procitaj_podesavanje("admin_ime"), key="prof_ime")
                if st.form_submit_button("Sačuvaj", type="primary", width="stretch"):
                    sacuvaj_podesavanje("admin_ime", ocisti(novo))
                    poruka_uspjeha("Ime je sačuvano.")
                    st.rerun()
        with st.container(key="kartica_admin"):
            st.markdown(":material/admin_panel_settings: **Administracija**")
            for kljuc in ("nabavka", "izvjestaji", "artikli", "narudzbine", "podesavanja"):
                st.page_link(STRANICE[kljuc], width="stretch")

    with st.expander("Ikonica na telefonu", icon=":material/smartphone:"):
        st.markdown(UPUTSTVO_IKONICA)
    if st.button("Odjava", key="odjava", icon=":material/logout:", width="stretch"):
        _odjavi_se()


# ── Prozor (dijalog) za sobu: pregled, uređivanje, otkazivanje ───────────────
# Otvoreni prozor se pamti u session_state i poziva iz main(), van automatskog
# osvježavanja, da se ne bi zatvorio dok neko kuca.

def otvori_dijalog(podaci: dict) -> None:
    st.session_state["dijalog"] = podaci
    st.session_state["dijalog_mod"] = "pregled"
    st.rerun()


def _zatvori_dijalog() -> None:
    st.session_state.pop("dijalog", None)
    st.session_state.pop("dijalog_mod", None)
    zakazi_reset("urf_", "rezd_", "plac_")


def _postavi_mod(mod: str) -> None:
    st.session_state["dijalog_mod"] = mod


def _promijeni_placanje(rid: int) -> None:
    kljuc = f"plac_{rid}"
    izbor = st.session_state.get(kljuc)
    r = rezervacija_po_id(rid)
    if not r:
        return
    if izbor is None:  # dodir na već izabrano dugme ga odznači – vrati trenutno stanje
        st.session_state[kljuc] = PLACANJE[int(r.get("placeno") or 0)]
        return
    if postavi_placeno(rid, izbor == PLACANJE[1], ime_korisnika()):
        poruka_uspjeha(f"Soba {r['soba']} · {r['gost']}: {izbor.lower()} (upisao {ime_korisnika()}, {fmt_dt(sada())})")


def opis_izmjena(staro: dict, novo: dict) -> list[str]:
    promjene = []
    if staro["gost"] != novo["gost"]:
        promjene.append(f"gost: {staro['gost']} → {novo['gost']}")
    if int(staro["soba"]) != int(novo["soba"]):
        promjene.append(f"soba: {staro['soba']} → {novo['soba']}")
    if staro["izvor"] != novo["izvor"]:
        promjene.append(f"izvor: {staro['izvor']} → {novo['izvor']}")
    if u_datum(staro["check_in"]) != u_datum(novo["check_in"]):
        promjene.append(f"check-in: {fmt_dk(staro['check_in'])} → {fmt_dk(novo['check_in'])}")
    if u_datum(staro["check_out"]) != u_datum(novo["check_out"]):
        promjene.append(f"check-out: {fmt_dk(staro['check_out'])} → {fmt_dk(novo['check_out'])}")
    if round(broj(staro["cijena"]), 2) != round(broj(novo["cijena"]), 2):
        promjene.append(f"cijena: {cijena_tekst(staro['cijena'])} → {cijena_tekst(novo['cijena'])}")
    if (staro["napomena"] or "") != (novo["napomena"] or ""):
        promjene.append("napomena izmijenjena")
    return promjene


def forma_izmjene_rezervacije(r: dict) -> None:
    rid = int(r["id"])
    p = f"urf_{rid}_"
    with st.form(f"forma_izmjena_rezervacije_{rid}", border=False):
        gost = st.text_input("Ime i prezime gosta", value=r["gost"], key=p + "gost")
        soba = st.pills(
            "Broj sobe", SOBE, default=int(r["soba"]), format_func=lambda s: f"Soba {s}", key=p + "soba"
        )
        izvor = st.segmented_control(
            "Izvor rezervacije", IZVORI, default=r["izvor"] if r["izvor"] in IZVORI else None,
            key=p + "izvor", width="stretch",
        )
        c1, c2 = st.columns(2, wrap=False)
        check_in = c1.date_input("Check-in", value=u_datum(r["check_in"]), format="DD.MM.YYYY", key=p + "in")
        check_out = c2.date_input("Check-out", value=u_datum(r["check_out"]), format="DD.MM.YYYY", key=p + "out")
        cijena = st.number_input(
            "Cijena rezervacije (€) – 0 znači „cijena na upit“", min_value=0.0, value=float(broj(r["cijena"])),
            step=1.0, format="%.2f", key=p + "cijena",
        )
        napomena = st.text_area("Napomena", value=r["napomena"] or "", key=p + "napomena", height=100)
        c3, c4 = st.columns(2, wrap=False)
        sacuvaj = c3.form_submit_button("Sačuvaj izmjene", type="primary", width="stretch")
        nazad = c4.form_submit_button("Nazad", width="stretch")

    if nazad:
        _postavi_mod("pregled")
        st.rerun(scope="fragment")
    if not sacuvaj:
        return

    gost = ocisti(gost)
    greske = []
    if len(gost) < 3:
        greske.append("Upiši ime i prezime gosta.")
    if soba is None:
        greske.append("Odaberi sobu.")
    if not izvor:
        greske.append("Odaberi izvor rezervacije.")
    if not check_in or not check_out or check_out <= check_in:
        greske.append("Check-out mora biti poslije check-in datuma.")
    if not greske:
        for z in preklapanja(int(soba), check_in, check_out, osim_id=rid):
            greske.append(
                f"Soba {soba} je zauzeta od {fmt_d(z['check_in'])} do {fmt_d(z['check_out'])} (gost: {z['gost']})."
            )
    if greske:
        for g in greske:
            st.error(g)
        return

    novo = {
        "gost": gost, "soba": int(soba), "cijena": broj(cijena), "izvor": izvor,
        "check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "napomena": ocisti(napomena),
    }
    promjene = opis_izmjena(r, novo)
    if not promjene:
        st.info("Ništa nije promijenjeno.")
        return
    izmijeni_rezervaciju(rid, novo, ime_korisnika(), "; ".join(promjene))
    _zatvori_dijalog()
    poruka_uspjeha(f"Rezervacija je izmijenjena (Soba {soba} · {gost}). Uredio: {ime_korisnika()}, {fmt_dt(sada())}")
    st.rerun()


@st.dialog("Rezervacija", width="medium", on_dismiss=_zatvori_dijalog)
def dijalog_rezervacije(rid: int) -> None:
    r = rezervacija_po_id(rid)
    if not r or int(r["otkazana"]):
        st.info("Ova rezervacija više nije aktivna.")
        if st.button("Zatvori", width="stretch", key="dlg_zatvori_neaktivna"):
            _zatvori_dijalog()
            st.rerun()
        return

    ci, co = u_datum(r["check_in"]), u_datum(r["check_out"])
    st.markdown(f"### Soba {r['soba']} · {md(r['gost'])}")

    mod = st.session_state.get("dijalog_mod", "pregled")
    if mod == "uredi":
        forma_izmjene_rezervacije(r)
        return
    if mod == "otkazi" and je_admin():
        st.warning(f"Otkazati rezervaciju: Soba {r['soba']} · {r['gost']}?")
        c1, c2 = st.columns(2, wrap=False)
        if c1.button("Da, otkaži", type="primary", width="stretch", key="dlg_otkazi_da"):
            otkazi_rezervaciju(rid, ime_korisnika())
            _zatvori_dijalog()
            poruka_uspjeha(f"Otkazana rezervacija: Soba {r['soba']} · {r['gost']}")
            st.rerun()
        if c2.button("Ne", width="stretch", key="dlg_otkazi_ne"):
            _postavi_mod("pregled")
            st.rerun(scope="fragment")
        return

    # Plaćeno / nije plaćeno – mogu i radnik i admin; promjena se odmah upisuje sa imenom i vremenom
    st.segmented_control(
        "Plaćanje", PLACANJE, default=PLACANJE[int(r.get("placeno") or 0)],
        format_func=lambda o: "✓ Plaćeno" if o == PLACANJE[1] else "Nije plaćeno",
        key=f"plac_{rid}", on_change=_promijeni_placanje, args=(rid,), width="stretch",
    )

    # Glavne opcije odmah na vrhu: admin → Uredi + Otkaži, radnik → samo Uredi
    with st.container(horizontal=True):
        st.button(
            "Uredi", icon=":material/edit:", type="primary", width="stretch",
            on_click=_postavi_mod, args=("uredi",), key="dlg_uredi",
        )
        if je_admin():
            st.button(
                "Otkaži", icon=":material/event_busy:", width="stretch",
                on_click=_postavi_mod, args=("otkazi",), key="dlg_otkazi",
            )

    st.markdown(
        f"{fmt_d(ci)} → {fmt_d(co)} · {noci_tekst((co - ci).days)}  \n"
        f"**{cijena_tekst(r['cijena'])}** · {r['izvor']}"
    )
    if ima_dorucak(r):
        st.markdown(":material/restaurant: **Doručak uključen** (Booking)")
    if r["napomena"]:
        st.markdown(f"Napomena: _{md(r['napomena'])}_")
    st.caption(f"Upisao: {r['upisao']} · {fmt_dt(r['kreirano'])}")
    for i in izmjene_rezervacije(rid):
        st.caption(f"Uredio: {i['ko']} · {fmt_dt(i['kreirano'])} — {i['opis']}")


@st.dialog("Nova rezervacija", width="medium", on_dismiss=_zatvori_dijalog)
def dijalog_nove_rezervacije(soba: int | None, dan: date) -> None:
    """Sa trake slobodne sobe – soba je izabrana; sa zelenog plusa – soba se bira u formi."""
    if soba:
        st.markdown(f"### Soba {soba}")
    forma_rezervacije(prefiks="rezd_", fiksna_soba=soba, pocetni_dan=dan)


def prikazi_dijalog() -> None:
    podaci = st.session_state.get("dijalog")
    if not podaci or not korisnik():
        return
    if podaci.get("tip") == "rezervacija":
        dijalog_rezervacije(int(podaci["id"]))
    elif podaci.get("tip") == "nova":
        soba = int(podaci["soba"]) if podaci.get("soba") else None
        dijalog_nove_rezervacije(soba, u_datum(podaci.get("dan") or danas()))


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


def _poslano(ime: str, kanal: str) -> None:
    """Dodir na Viber / SMS / WhatsApp: narudžba ide u istoriju, a izbor se čisti."""
    priprema = st.session_state.get("nab_priprema") or {}
    if not priprema.get("stavke"):
        return
    sacuvaj_narudzbinu(
        ime_korisnika(), priprema["stavke"], priprema["napomena"], priprema["poruka"], sada(), f"{ime} · {kanal}"
    )
    st.session_state["nab_odabrani"] = set()
    st.session_state.pop("nab_priprema", None)
    zakazi_reset("nab_napomena", "nab_pretraga")
    poruka_uspjeha(
        f"Spisak ({len(priprema['stavke'])} art.) poslat: {ime} · {kanal}. Sačuvan je u istoriji narudžbi."
    )


def _kljuc_kategorije(kategorija: str) -> str:
    return "nab_kat_" + hashlib.md5(kategorija.encode()).hexdigest()[:10]


def stranica_nabavka() -> None:
    samo_admin()
    st.title("Nabavka")
    prikazi_poruku()

    artikli = svi_artikli()
    if artikli.empty:
        st.info("Još nema artikala. Dodaj ih na stranici „Artikli“.")
        return

    st.caption("Otvori kategoriju i označi šta treba. Na dnu dodirni ime i izaberi Viber, SMS ili WhatsApp.")
    odabrani: set = st.session_state.setdefault("nab_odabrani", set())
    odabrani.intersection_update(set(artikli["id"].tolist()))

    def kvadratic(red) -> None:
        id_ = int(red.id)
        kljuc = f"nab_cb_{id_}"
        st.session_state[kljuc] = id_ in odabrani
        st.checkbox(red.naziv, key=kljuc, on_change=_prebaci_artikal, args=(id_,))

    pretraga = st.text_input(
        "Pretraga", key="nab_pretraga", placeholder="Traži artikal…", label_visibility="collapsed",
        icon=":material/search:",
    )
    if ocisti(pretraga):
        # pretraga: odmah prikaži sve pogođene artikle, bez otvaranja kategorija
        prikaz = artikli[artikli["naziv"].str.contains(ocisti(pretraga), case=False, regex=False)]
        if prikaz.empty:
            st.caption("Nema artikala za tu pretragu.")
        for kategorija, grupa in prikaz.groupby("kategorija", sort=False):
            st.markdown(f"**{md(kategorija)}**")
            for red in grupa.itertuples(index=False):
                kvadratic(red)
    else:
        # kategorije kao spisak – dodir na kategoriju otvara njene artikle
        for kategorija, grupa in artikli.groupby("kategorija", sort=False):
            with st.expander(f"{kategorija} ({len(grupa)})", key=_kljuc_kategorije(kategorija)):
                for red in grupa.itertuples(index=False):
                    kvadratic(red)

    napomena = st.text_area(
        "Napomena (količine, rok…)",
        key="nab_napomena",
        placeholder="npr. Coca-Cola 2 gajbe",
        height=100,
    )

    izabrani = artikli[artikli["id"].isin(odabrani)]
    stavke = [{"naziv": r.naziv, "kategorija": r.kategorija} for r in izabrani.itertuples(index=False)]
    poruka = sastavi_poruku(stavke, napomena, ime_korisnika(), sada()) if stavke else ""
    st.session_state["nab_priprema"] = {"stavke": stavke, "napomena": ocisti(napomena), "poruka": poruka}

    st.subheader("Pošalji spisak")
    if stavke:
        st.markdown(f"**Označeno ({len(stavke)}):** " + ", ".join(md(s["naziv"]) for s in stavke))
    else:
        st.caption("Prvo označi artikle iznad.")

    lista = primaoci()
    if not lista:
        st.caption("Brojeve (Tata, Mama…) dodaješ u „Podešavanjima“. Do tada biraš kontakt sam.")
        lista = [{"ime": "Izaberi kontakt", "telefon": ""}]
    for i, p in enumerate(lista):
        with st.popover(
            p["ime"], icon=":material/person:", width="stretch", disabled=not stavke, key=f"nab_pop_{i}"
        ):
            linkovi = linkovi_za_poruku(poruka, p["telefon"])
            if p["telefon"]:
                st.caption(f"{p['ime']} · {p['telefon']}")
            st.link_button(
                "Viber", linkovi["viber"], type="primary", width="stretch", icon=":material/chat:",
                on_click=_poslano, args=(p["ime"], "Viber"), key=f"nab_viber_{i}",
            )
            st.link_button(
                "SMS", linkovi["sms"], width="stretch", icon=":material/sms:",
                on_click=_poslano, args=(p["ime"], "SMS"), key=f"nab_sms_{i}",
            )
            st.link_button(
                "WhatsApp", linkovi["whatsapp"], width="stretch", icon=":material/send:",
                on_click=_poslano, args=(p["ime"], "WhatsApp"), key=f"nab_wa_{i}",
            )
            st.caption("Na Viberu tekst je već upisan – samo izaberi kontakt sa liste.")

    if poruka:
        with st.expander("Tekst poruke"):
            st.code(poruka, language=None, wrap_lines=True)

    posljednje = posljednje_narudzbine(5)
    if posljednje:
        with st.expander("Posljednje narudžbine"):
            for n in posljednje:
                stavke_n = json.loads(n["stavke"] or "[]")
                st.markdown(
                    f"**{fmt_dt(n['kreirano'])}** · {md(n['narucio'])}"
                    + (f" → {md(n['primalac'])}" if n.get("primalac") else "")
                    + "  \n" + ", ".join(md(s["naziv"]) for s in stavke_n)
                )


# ── Smještaj: rezervacije, kalendar, istorija ────────────────────────────────

def forma_rezervacije(
    prefiks: str = "rezf_", fiksna_soba: int | None = None, pocetni_dan: date | None = None
) -> None:
    """Nova rezervacija. U prozoru sa Početne soba (i dan) su već izabrani."""
    p = prefiks
    pocetak = pocetni_dan or danas()
    with st.form(f"forma_rezervacija_{p}", border=fiksna_soba is None):
        gost = st.text_input("Ime i prezime gosta", key=p + "gost")
        if fiksna_soba is None:
            soba = st.pills("Broj sobe", SOBE, format_func=lambda s: f"Soba {s}", key=p + "soba")
        else:
            soba = fiksna_soba
        izvor = st.segmented_control("Izvor rezervacije", IZVORI, key=p + "izvor", width="stretch")
        c1, c2 = st.columns(2, wrap=False)
        check_in = c1.date_input("Check-in", value=pocetak, format="DD.MM.YYYY", key=p + "in")
        check_out = c2.date_input(
            "Check-out", value=pocetak + timedelta(days=1), format="DD.MM.YYYY", key=p + "out"
        )
        cijena = iznos("Cijena rezervacije (€) – upiši 0 za „cijena na upit“", p + "cijena")
        placanje = st.segmented_control(
            "Plaćanje", PLACANJE, default=PLACANJE[0], key=p + "placanje", width="stretch"
        )
        napomena = st.text_area(
            "Napomena", key=p + "napomena", placeholder="npr. Potreban dječiji krevetac u sobi", height=100
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
            "placeno": 1 if placanje == PLACANJE[1] else 0,
        }
    )
    noci = (check_out - check_in).days
    poruka_uspjeha(
        f"Rezervacija sačuvana: Soba {soba} · {gost} · {fmt_d(check_in)} → {fmt_d(check_out)} "
        f"({noci_tekst(noci)}) · upisao {ime_korisnika()}, {fmt_dt(trenutak)}"
    )
    _zatvori_dijalog()
    zakazi_reset("urf_", "rezd_", "plac_", p)
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
                    f"{fmt_dk(r['check_in'])}–{fmt_dk(r['check_out'])} · upisao {r['upisao']}"
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
    pogled = st.segmented_control(
        "Prikaz", ["Sedmica", "Mjesec"], default="Sedmica", key="kal_pogled", width="stretch"
    ) or "Sedmica"

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
                f"{fmt_dk(r['check_out'])} · {r['izvor']} · "
                f"_upisao {md(r['upisao'])}, {fmt_dtk(r['kreirano'])}_"
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
                    f"{r.izvor} · {cijena_tekst(r.cijena)} · "
                    f"{'Plaćeno' if int(r.placeno or 0) else 'Nije plaćeno'}"
                )
                if r.napomena:
                    st.markdown(f"Napomena: _{md(r.napomena)}_")
                st.caption(
                    f"Upisao: {r.upisao} · {fmt_dt(r.kreirano)}"
                    + (f"  \nUredio: {r.uredio} · {fmt_dt(r.uredjeno)}" if r.uredio else "")
                )
                if st.button("Otvori", key=f"otvori_rez_{int(r.id)}", icon=":material/edit_note:"):
                    otvori_dijalog({"tip": "rezervacija", "id": int(r.id)})
    else:
        prikaz = df[df["otkazana"] == 1] if filter_ == "Otkazane" else df
        tabela = pd.DataFrame(
            {
                "Soba": prikaz["soba"].astype(int),
                "Gost": prikaz["gost"],
                "Check-in": prikaz["ci"].map(fmt_d),
                "Check-out": prikaz["co"].map(fmt_d),
                "Noći": [(co - ci).days for ci, co in zip(prikaz["ci"], prikaz["co"])],
                "Cijena": prikaz["cijena"].map(cijena_tekst),
                "Plaćanje": [PLACANJE[int(p or 0)] for p in prikaz["placeno"]],
                "Izvor": prikaz["izvor"],
                "Napomena": prikaz["napomena"],
                "Upisao": prikaz["upisao"],
                "Upisano": prikaz["kreirano"].map(fmt_dt),
                "Uredio": [
                    f"{ko}, {fmt_dt(kad)}" if ko else ""
                    for ko, kad in zip(prikaz["uredio"], prikaz["uredjeno"])
                ],
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
    tab_nova, tab_lista = st.tabs(["Nova rezervacija", "Rezervacije"])
    with tab_nova:
        forma_rezervacije()
    with tab_lista:
        lista_rezervacija()


def stranica_kalendar() -> None:
    st.title("Kalendar soba")
    kalendar_uzivo()


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
    st.caption("Za brisanje više artikala odjednom označi kvadratiće lijevo od artikala.")
    dogadjaj = st.dataframe(
        artikli[["kategorija", "naziv"]].rename(columns={"kategorija": "Kategorija", "naziv": "Naziv"}),
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key="art_tabela",
    )
    redovi = [i for i in (dogadjaj.selection.rows if dogadjaj else []) if 0 <= i < len(artikli)]
    if redovi:
        oznaceni = artikli.iloc[redovi]
        ids = [int(i) for i in oznaceni["id"]]
        st.warning(f"Označeno za brisanje ({len(ids)}): " + ", ".join(md(n) for n in oznaceni["naziv"]))
        potvrda = st.checkbox(f"Potvrđujem trajno brisanje ({len(ids)})", key="artb_potvrda")
        if st.button(
            f"Obriši označene ({len(ids)})", type="primary", disabled=not potvrda,
            icon=":material/delete:", key="artb_dugme",
        ):
            obrisi_artikle(ids)
            poruka_uspjeha(f"Trajno obrisano {len(ids)} artikala. Istorija ranijih narudžbi ostaje sačuvana.")
            zakazi_reset("art_tabela", "artb_")
            st.rerun()


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
        primalac = f" → {r.primalac}" if getattr(r, "primalac", "") else ""
        with st.expander(f"{fmt_dt(r.kreirano)} · {r.narucio}{primalac} · {len(stavke)} art."):
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

def uvoz_rezervacija() -> None:
    """Učitaj CSV → pregled (šta je novo, šta se preskače) → potvrda."""
    kljuc = f"uvoz_fajl_{st.session_state.get('_uvoz_brojac', 0)}"
    fajl = st.file_uploader("Fajl sa rezervacijama (CSV)", type=["csv"], key=kljuc)
    if fajl is None:
        return
    try:
        stavke = provjeri_uvoz(procitaj_fajl_uvoza(fajl), f"{UVOZ_UPISAO} (uvezao {ime_korisnika()})", sada())
    except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as greska:
        st.error(f"Fajl ne može da se pročita: {greska}")
        return
    nove = [s["zapis"] for s in stavke if s["zapis"]]
    st.dataframe(
        pd.DataFrame([{k: v for k, v in s.items() if k != "zapis"} for s in stavke]),
        hide_index=True, width="stretch",
    )
    preskace = len(stavke) - len(nove)
    st.caption(f"Novih: **{len(nove)}** · preskače se: **{preskace}** (već upisane ili sa greškom)")
    if not nove:
        st.info("Nema novih rezervacija za upis.")
        return
    if st.button(f"Upiši {len(nove)} rezervacija", key="uvoz_potvrdi", type="primary",
                 icon=":material/download_done:", width="stretch"):
        upisano = sacuvaj_rezervacije(nove)
        st.session_state["_uvoz_brojac"] = int(st.session_state.get("_uvoz_brojac", 0)) + 1  # isprazni izbor fajla
        poruka_uspjeha(f"Upisano {upisano} rezervacija iz fajla.")
        st.rerun()


def stranica_podesavanja() -> None:
    samo_admin()
    st.title("Podešavanja")
    prikazi_poruku()

    with st.form("forma_podesavanja"):
        naziv = st.text_input(
            "Naziv objekta", value=procitaj_podesavanje("naziv_objekta", "Restoran"), key="pod_naziv"
        )
        sacuvaj = st.form_submit_button("Sačuvaj naziv", type="primary", width="stretch")
    if sacuvaj:
        sacuvaj_podesavanje("naziv_objekta", ocisti(naziv) or "Restoran")
        poruka_uspjeha("Naziv je sačuvan.")
        zakazi_reset("pod_")
        st.rerun()

    st.markdown("#### Slika hotela")
    st.caption("Prikazuje se na vrhu početne stranice i na prijavi. Najbolje izgleda položena fotografija.")
    if slika_url("slika_hotela"):
        st.image(io.BytesIO(base64.b64decode(procitaj_podesavanje("slika_hotela"))), width="stretch")
        vrati = "Vrati osnovnu fotografiju" if HOTEL_FAJL.exists() else "Ukloni sliku hotela"
        if st.button(vrati, key="hotel_ukloni", icon=":material/delete:", type="tertiary"):
            sacuvaj_podesavanje("slika_hotela", "")
            poruka_uspjeha("Slika hotela je uklonjena.")
            st.rerun()
    elif HOTEL_FAJL.exists():
        st.image(str(HOTEL_FAJL), width="stretch", caption="Osnovna fotografija (hotel.jpg)")
    izbor_slike("Izaberi drugu sliku hotela", "slika_hotela", 1400, False, "Slika hotela je sačuvana.")

    st.markdown("#### Uvoz rezervacija")
    st.caption(
        "Učitaj fajl sa rezervacijama (CSV, npr. iz Google kalendara). Prvo vidiš pregled, pa potvrdiš. "
        "Rezervacije koje se poklapaju sa već upisanim se preskaču, pa isti fajl može bezbjedno da se učita ponovo."
    )
    uvoz_rezervacija()

    st.markdown("#### Brojevi za slanje nabavke")
    st.caption("Kome admin šalje spisak za nabavku preko WhatsAppa ili SMS-a (npr. Tata, Mama, Ja).")
    lista = primaoci()
    if not lista:
        st.caption("Još nema brojeva.")
    for i, p in enumerate(lista):
        with st.container(horizontal=True, vertical_alignment="center", border=True):
            st.markdown(f"**{md(p['ime'])}** · {md(p['telefon'])}")
            if st.button("Ukloni", key=f"prim_ukloni_{i}", icon=":material/delete:", type="tertiary"):
                sacuvaj_primaoce([x for j, x in enumerate(lista) if j != i])
                sacuvaj_podesavanje("telefon_nabavke", "")  # stari pojedinačni broj više se ne koristi
                poruka_uspjeha(f"Uklonjen broj: {p['ime']}")
                st.rerun()
    with st.form("forma_primalac"):
        ime = st.text_input("Ime", placeholder="npr. Tata", key="prim_ime")
        telefon = st.text_input("Broj telefona", placeholder="+382 67 123 456", key="prim_tel")
        dodaj = st.form_submit_button("Dodaj broj", width="stretch", icon=":material/person_add:")
    if dodaj:
        ime, telefon = ocisti(ime), ocisti(telefon)
        if not ime:
            st.error("Upiši ime (npr. Tata).")
        elif len(normalizuj_telefon(telefon)) < 8:
            st.error("Upiši ispravan broj telefona, npr. +382 67 123 456 ili 067 123 456.")
        elif ime.casefold() in {p["ime"].casefold() for p in lista}:
            st.error(f"Ime „{ime}“ već postoji na spisku.")
        else:
            sacuvaj_primaoce(lista + [{"ime": ime, "telefon": telefon}])
            poruka_uspjeha(f"Dodat broj: {ime} · {telefon}")
            zakazi_reset("prim_")
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
        page_icon=str(IKONA_FAJL) if IKONA_FAJL.exists() else "🍽️",
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
        ubaci_skriptu()
        return

    STRANICE.clear()
    STRANICE.update(
        {
            "pocetna": st.Page(stranica_pocetna, title="Početna", icon=":material/home:", default=True),
            "smjena": st.Page(
                stranica_smjena, title="Kraj smjene", icon=":material/point_of_sale:", url_path="smjena"
            ),
            "smjestaj": st.Page(
                stranica_smjestaj, title="Smještaj", icon=":material/hotel:", url_path="smjestaj"
            ),
            "kalendar": st.Page(
                stranica_kalendar, title="Kalendar", icon=":material/calendar_month:", url_path="kalendar"
            ),
            "profil": st.Page(stranica_profil, title="Profil", icon=":material/person:", url_path="profil"),
        }
    )
    rad = list(STRANICE.values())

    if je_admin():
        STRANICE.update(
            {
                "nabavka": st.Page(
                    stranica_nabavka, title="Nabavka", icon=":material/shopping_cart:", url_path="nabavka"
                ),
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
        meni = {
            "Rad": rad,
            "Admin": [STRANICE[k] for k in ("nabavka", "izvjestaji", "artikli", "narudzbine", "podesavanja")],
        }
    else:
        meni = {"": rad}

    # Streamlit-ov meni je skriven. Svuda: dugme ☰ Meni lijevo gore sa svim opcijama.
    # Telefon još ima fiksnu traku na dnu (zeleni plus za novu rezervaciju u sredini).
    stranica = st.navigation(meni, position="hidden")
    aktivna = next((k for k, v in STRANICE.items() if v.title == stranica.title), "")
    glavni_meni(aktivna)
    if aktivna != "pocetna":
        traka_nazad()
    stranica.run()
    if je_telefon():
        donja_traka(aktivna)
    prikazi_dijalog()
    ubaci_skriptu()


if __name__ == "__main__":
    main()
