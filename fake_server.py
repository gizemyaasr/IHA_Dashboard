from flask import Flask, request, jsonify, make_response
from datetime import datetime, timezone
import time, math, random
from flask import request
from flask_cors import CORS
from flask_socketio import SocketIO
from flask import send_file
from flask import send_from_directory
import os
import secrets

app = Flask(__name__)

CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")  # dev için *; prod’da domain kısıtla

# Basit kimlik & oturum
VALID_USERS = [
    {"kadi": "anafarta", "sifre": "123", "takim": 25},
    {"kadi": "yem", "sifre": "123456", "takim": 20},
    {"kadi": "deneme", "sifre": "deneme", "takim": 1}
]

ISSUED_TOKENS = {}  # token → team

# In-memory en son telemetri kayıtları: {takim_numarasi: {"telemetry": t, "ts": time.time()}}
_latest_telemetry = {}

# (Opsiyonel) kaç saniyeden eski telemetry'i düşman listesinden çıkarmak istersin
_TELEMETRY_STALE_SEC = 5.0  # örn. 5 saniye; gerçek testte network koşullarına göre arttırabilirsin
TEAM_NO = None

# HSS'ler uçağa gönderilsin mi?
HSS_SEND_ENABLED = True

# HSS sisteminin aktif/pasif durumu (UI'den kontrol edilebilir)
HSS_SYSTEM_ACTIVE = False  # Başlangıçta KAPALI

TOKEN = "fake_token_123"
SESSION_COOKIE = "sessionid"

# 2 Hz limiti (takım bazlı)
_last_telemetry_ts = {}
_RATE_PERIOD = 0.5  # saniye → 2 Hz

# --- HSS pencere kontrolü ---
_HSS_EMPTY1_SEC = 10  # ilk 10 saniye boş
_HSS_ACTIVE_SEC = 10  # sonraki 10 saniye dolu
_SERVER_START_MONO = time.monotonic()

# --- SQLite setup ---
import os, sqlite3, json

DB_PATH = os.path.join(os.path.dirname(__file__), "iha_logs.db")


def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL,
        takim INTEGER NOT NULL,
        enlem REAL,
        boylam REAL,
        irtifa REAL,
        hiz REAL,
        batarya INTEGER,
        raw_json TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_takim_ts ON telemetry(takim, ts_utc);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL,
        kaynak_takim INTEGER,
        kilitlenen_takim INTEGER,
        otonom_kilitlenme INTEGER NOT NULL,
        kilit_bitis_gps TEXT,
        extra_json TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_locks_ts ON locks(ts_utc);")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS kamikaze (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,              -- ISO8601 UTC (server zamanı)
            kaynak_takim INTEGER,              -- gönderende varsa
            qr_metni TEXT NOT NULL,
            baslangic_gps TEXT NOT NULL,       -- JSON string
            bitis_gps TEXT NOT NULL,           -- JSON string
            extra_json TEXT                    -- ham paket / ek alanlar
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_kamikaze_ts ON kamikaze(ts_utc)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS fences (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      kind TEXT NOT NULL,            -- 'polygon' (dikdörtgeni GeoJSON Polygon tutacağız)
      geojson TEXT NOT NULL,         -- GeoJSON Feature
      color TEXT DEFAULT '#ef4444',  -- kırmızı
      updated_at TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fences_updated ON fences(updated_at);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS hss (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      lat REAL NOT NULL,
      lon REAL NOT NULL,
      radius REAL NOT NULL,     -- metre
      active INTEGER NOT NULL DEFAULT 1,
      updated_at TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hss_active ON hss(active);")

    con.commit()
    con.close()
    print("✅ DB hazır:", DB_PATH)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def list_fences():
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    rows = cur.execute("SELECT id,name,kind,geojson,color,updated_at FROM fences ORDER BY id").fetchall()
    con.close()
    keys = ["id", "name", "kind", "geojson", "color", "updated_at"]
    out = [dict(zip(keys, r)) for r in rows]
    for r in out:
        try:
            r["geojson"] = json.loads(r["geojson"])
        except:
            pass
    return out


def list_hss():
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    rows = cur.execute("SELECT id,name,lat,lon,radius,active,updated_at FROM hss WHERE active=1 ORDER BY id").fetchall()
    con.close()
    keys = ["id", "name", "lat", "lon", "radius", "active", "updated_at"]
    return [dict(zip(keys, r)) for r in rows]


def distance_m(lat1, lon1, lat2, lon2):
    """Iki enlem/boylam arasındaki mesafeyi metre cinsinden hesapla (haversine)."""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def is_inside_hss(lat, lon):
    """Aktif HSS'lerden herhangi birinin içinde mi?"""
    try:
        for it in list_hss():
            h_lat = float(it["lat"])
            h_lon = float(it["lon"])
            r = float(it["radius"])
            d = distance_m(lat, lon, h_lat, h_lon)
            if d <= r:
                return True
    except Exception as e:
        print("⚠️ is_inside_hss hata:", e)
    return False


def insert_hss(name, lat, lon, radius):
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute("INSERT INTO hss(name,lat,lon,radius,active,updated_at) VALUES (?,?,?,?,1,?)",
                (name, float(lat), float(lon), float(radius), now_iso()))
    con.commit();
    _id = cur.lastrowid;
    con.close()
    return _id


def update_hss(hid, **fields):
    if not fields: return
    sets, args = [], []
    for k in ("name", "lat", "lon", "radius", "active"):
        if k in fields: sets.append(f"{k}=?"); args.append(fields[k])
    sets.append("updated_at=?");
    args.append(now_iso())
    args.append(int(hid))
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute(f"UPDATE hss SET {', '.join(sets)} WHERE id=?", args)
    con.commit();
    con.close()


def delete_hss(hid):
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute("DELETE FROM hss WHERE id=?", (int(hid),))
    con.commit();
    con.close()


def save_kamikaze_row(payload: dict):
    """/api/kamikaze_bilgisi gelen paketi kalıcı kaydet"""
    import json, datetime
    ts_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    kaynak = payload.get("kaynak_takim")  # yoksa None kalır
    qr = payload.get("qrMetni")
    kb = payload.get("kamikazeBaslangicZamani", {})
    ke = payload.get("kamikazeBitisZamani", {})

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO kamikaze (ts_utc, kaynak_takim, qr_metni, baslangic_gps, bitis_gps, extra_json)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (ts_utc, kaynak, qr, json.dumps(kb), json.dumps(ke), json.dumps(payload)))
    conn.commit()
    conn.close()


def query_kamikaze_history(kaynak=None, start=None, end=None, limit=1000):
    """UI tarih filtresi için socket’ten sorgulanır"""
    import json
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = "SELECT ts_utc, kaynak_takim, qr_metni, baslangic_gps, bitis_gps FROM kamikaze WHERE 1=1"
    args = []
    if kaynak not in (None, "", "null"):
        sql += " AND kaynak_takim = ?"
        args.append(int(kaynak))
    if start:
        sql += " AND ts_utc >= ?";
        args.append(start)
    if end:
        sql += " AND ts_utc <= ?";
        args.append(end)
    sql += " ORDER BY ts_utc DESC LIMIT ?"
    args.append(int(limit))

    rows = [dict(r) for r in cur.execute(sql, args).fetchall()]
    # JSON alanları dict’e çevir
    for r in rows:
        try:
            r["baslangic_gps"] = json.loads(r["baslangic_gps"]) if r["baslangic_gps"] else None
        except:
            pass
        try:
            r["bitis_gps"] = json.loads(r["bitis_gps"]) if r["bitis_gps"] else None
        except:
            pass
    conn.close()
    return rows


def query_telemetry_history(takim=None, start_iso=None, end_iso=None, limit=1000):
    """
    telemetry tablosundan filtreli veri çeker.
    start_iso / end_iso -> 'YYYY-MM-DDTHH:MM:SSZ' gibi ISO (UTC)
    """
    q = "SELECT ts_utc, takim, enlem, boylam, irtifa, hiz, batarya FROM telemetry WHERE 1=1"
    params = []
    if takim is not None and str(takim).strip() != "":
        q += " AND takim = ?"
        params.append(int(takim))
    if start_iso:
        q += " AND ts_utc >= ?"
        params.append(start_iso)
    if end_iso:
        q += " AND ts_utc <= ?"
        params.append(end_iso)
    q += " ORDER BY ts_utc DESC"
    if limit:
        q += f" LIMIT {int(limit)}"

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(q, params).fetchall()
    con.close()

    # dict listeye çevir
    keys = ["ts_utc", "takim", "enlem", "boylam", "irtifa", "hiz", "batarya"]
    return [dict(zip(keys, r)) for r in rows]


def query_locks_history(kaynak=None, kilitlenen=None, start_iso=None, end_iso=None, limit=1000):
    import sqlite3, json
    q = """SELECT ts_utc, kaynak_takim, kilitlenen_takim, otonom_kilitlenme, kilit_bitis_gps, extra_json
           FROM locks WHERE 1=1"""
    params = []
    if kaynak not in (None, "", "null", "None"):
        q += " AND kaynak_takim = ?";
        params.append(int(kaynak))
    if kilitlenen not in (None, "", "null", "None"):
        q += " AND kilitlenen_takim = ?";
        params.append(int(kilitlenen))
    if start_iso:
        q += " AND ts_utc >= ?";
        params.append(start_iso)
    if end_iso:
        q += " AND ts_utc <= ?";
        params.append(end_iso)
    q += " ORDER BY ts_utc DESC"
    if limit:
        q += f" LIMIT {int(limit)}"

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(q, params).fetchall()
    con.close()

    keys = ["ts_utc", "kaynak_takim", "kilitlenen_takim", "otonom_kilitlenme", "kilit_bitis_gps", "extra_json"]
    out = [dict(zip(keys, r)) for r in rows]

    for r in out:
        # kilit_bitis_gps string ise dict yap
        try:
            if isinstance(r["kilit_bitis_gps"], str) and r["kilit_bitis_gps"].strip():
                r["kilit_bitis_gps"] = json.loads(r["kilit_bitis_gps"])
        except:
            pass

        # extra_json’dan LOCK_FIELDS alanlarını çıkar
        r["hedef_merkez_X"] = r["hedef_merkez_Y"] = r["hedef_genislik"] = r["hedef_yukseklik"] = None
        try:
            ej = r.get("extra_json")
            if isinstance(ej, str) and ej.strip():
                ej = json.loads(ej)
            if isinstance(ej, dict):
                r["hedef_merkez_X"] = ej.get("hedef_merkez_X")
                r["hedef_merkez_Y"] = ej.get("hedef_merkez_Y")
                r["hedef_genislik"] = ej.get("hedef_genislik")
                r["hedef_yukseklik"] = ej.get("hedef_yukseklik")
        except:
            pass

        # artık istemiyoruz
        r.pop("extra_json", None)
    return out


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_telemetry_row(takim, t):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO telemetry (ts_utc, takim, enlem, boylam, irtifa, hiz, batarya, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        utc_now(), int(takim),
        float(t.get("iha_enlem", 0.0)),
        float(t.get("iha_boylam", 0.0)),
        float(t.get("iha_irtifa", 0.0)),
        float(t.get("iha_hiz", 0.0)),
        int(t.get("iha_batarya", 0)),
        json.dumps(t, ensure_ascii=False)
    ))
    con.commit()
    con.close()
    print("📝 telemetry→DB takım=", takim)


def save_lock_row(payload: dict):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO locks (ts_utc, kaynak_takim, kilitlenen_takim, otonom_kilitlenme, kilit_bitis_gps, extra_json)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        utc_now(),
        payload.get("kaynak_takim"),
        payload.get("kilitlenen_takim"),
        int(payload.get("otonom_kilitlenme", 0)),
        json.dumps(payload.get("kilitlenmeBitisZamani", {}), ensure_ascii=False),
        json.dumps(payload, ensure_ascii=False)
    ))
    con.commit()
    con.close()


@app.route('/static/<path:path>')
def static_files(path):
    return send_from_directory('static', path)


@app.route("/dashboard")
def dashboard():
    return send_file("ui.html")


def server_now_dict():
    now = datetime.now(timezone.utc)
    return {"gun": now.day, "saat": now.hour, "dakika": now.minute,
            "saniye": now.second, "milisaniye": int(now.microsecond / 1000)}


def ok_auth():
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False

    tok = hdr.split(" ", 1)[1].strip()

    # TEST MODE: fake token da kabul
    if tok == "fake_token_123":
        return True

    # Gerçek login tokenı
    if tok in ISSUED_TOKENS:
        return True

    return False


def deg2rad(d): return d * math.pi / 180.0


def rad2deg(r): return r * 180.0 / math.pi


def dest_from(lat, lon, bearing_deg, dist_m):
    R = 6371000.0
    br = deg2rad(bearing_deg)
    lat1, lon1 = deg2rad(lat), deg2rad(lon)
    d_R = dist_m / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d_R) + math.cos(lat1) * math.sin(d_R) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(d_R) * math.cos(lat1),
                             math.cos(d_R) - math.sin(lat1) * math.sin(lat2))
    return rad2deg(lat2), rad2deg(lon2)


@app.route("/api/giris", methods=["POST"])
def giris():
    data = request.get_json(silent=True) or {}
    kadi = data.get("kadi")
    sifre = data.get("sifre")

    # kullanıcıyı bul
    user = next((u for u in VALID_USERS if u["kadi"] == kadi and u["sifre"] == sifre), None)
    if not user:
        return ("Geçersiz kullanıcı adı veya şifre", 400)

    # rastgele token üret
    token = secrets.token_hex(16)
    ISSUED_TOKENS[token] = user["takim"]

    print(f"🔐 Login OK: {kadi} → team {user['takim']} | token={token}")

    return jsonify({
        "takim_numarasi": user["takim"],
        "token": token
    }), 200


@app.route("/api/sunucusaati", methods=["GET"])
def sunucusaati():
    return jsonify(server_now_dict()), 200


# Şeman: tam olarak kullanıcının gönderdiği alanlar
REQUIRED_FIELDS = [
    "takim_numarasi", "iha_enlem", "iha_boylam", "iha_irtifa",
    "iha_dikilme", "iha_yonelme", "iha_yatis", "iha_hiz",
    "iha_batarya", "iha_otonom", "iha_kilitlenme", "gps_saati"
]
LOCK_FIELDS = ["hedef_merkez_X", "hedef_merkez_Y", "hedef_genislik", "hedef_yukseklik"]


def validate_telemetry(t):
    try:
        for f in REQUIRED_FIELDS:
            if f not in t: return False
        if int(t["iha_kilitlenme"]) == 1:
            for f in LOCK_FIELDS:
                if f not in t: return False

        # Aralık kontrolleri (makul sınırlar)
        if not (-90 <= float(t["iha_enlem"]) <= 90): return False
        if not (-180 <= float(t["iha_boylam"]) <= 180): return False
        if not (0 <= float(t["iha_irtifa"]) <= 10000): return False
        if not (-90 <= float(t["iha_dikilme"]) <= 90): return False
        if not (0 <= float(t["iha_yonelme"]) <= 360): return False
        if not (-90 <= float(t["iha_yatis"]) <= 90): return False
        if not (0 <= float(t["iha_hiz"]) <= 200): return False
        if not (0 <= int(t["iha_batarya"]) <= 100): return False
        if int(t["iha_otonom"]) not in (0, 1): return False
        if int(t["iha_kilitlenme"]) not in (0, 1): return False

        gps = t["gps_saati"]
        for k in ("saat", "dakika", "saniye", "milisaniye"):
            if k not in gps: return False
        if not (0 <= int(gps["saat"]) < 24): return False
        if not (0 <= int(gps["dakika"]) < 60): return False
        if not (0 <= int(gps["saniye"]) < 60): return False
        if not (0 <= int(gps["milisaniye"]) < 1000): return False
        return True
    except Exception:
        return False


_enemy_angle = 0.0


@app.route("/api/hss_send_flag", methods=["POST"])
def hss_send_flag():
    global HSS_SEND_ENABLED
    if not ok_auth():
        return "401", 401

    d = request.get_json(silent=True) or {}
    # enabled true/false bekliyoruz
    HSS_SEND_ENABLED = bool(d.get("enabled"))
    print("🔁 HSS_SEND_ENABLED =", HSS_SEND_ENABLED)
    return jsonify({"ok": True, "enabled": HSS_SEND_ENABLED}), 200


@app.route("/api/hss_toggle", methods=["POST"])
def hss_toggle():
    """HSS sistemini aktif/pasif yap (uçak tarafında kaçınma aktif/pasif)"""
    global HSS_SYSTEM_ACTIVE
    if not ok_auth():
        return "401", 401

    d = request.get_json(silent=True) or {}

    # "active": true/false ile kontrol
    if "active" in d:
        HSS_SYSTEM_ACTIVE = bool(d["active"])
    else:
        # Toggle
        HSS_SYSTEM_ACTIVE = not HSS_SYSTEM_ACTIVE

    status = "AKTİF ✅" if HSS_SYSTEM_ACTIVE else "PASİF ❌"
    print(f"🔄 HSS SİSTEMİ: {status}")

    # SocketIO ile tüm clientlara bildir
    socketio.emit("hss_system_status", {"hss_aktif": HSS_SYSTEM_ACTIVE})

    return jsonify({
        "ok": True,
        "hss_aktif": HSS_SYSTEM_ACTIVE,
        "message": f"HSS sistemi {status}"
    }), 200


@app.route("/api/telemetri_gonder", methods=["POST"])
def telemetri():
    # TEAM_NO artık yayın/ayrım için kullanılmıyor; projede başka yerde kullanıyorsanız kalsın.
    global _latest_telemetry, TEAM_NO, _TELEMETRY_STALE_SEC, _last_telemetry_ts, _RATE_PERIOD

    if not ok_auth():
        return "401", 401

    t = request.get_json(silent=True) or {}
    print("📡 Gelen Telemetri:", t)

    # Şema/alan kontrolü: başarısızsa 204 (gövde yok)
    if not validate_telemetry(t):
        return "", 204

    # 0) Takım id'yi GÖNDERENDEN al
    try:
        takim = int(t["takim_numarasi"])
    except Exception:
        return ("bad request", 400)

    # 1) Rate limit (takım bazlı) — 2 Hz (0.5 s)
    now_m = time.monotonic()
    last = _last_telemetry_ts.get(takim, 0)
    if now_m - last < _RATE_PERIOD:
        return ("3", 400)  # hızlı gönderim
    _last_telemetry_ts[takim] = now_m

    # 2) In-memory son telemetri kaydı (takım bazlı)
    try:
        _latest_telemetry[takim] = {"telemetry": t, "ts": time.time()}
    except Exception as e:
        print("❌ _latest_telemetry güncelleme hatası:", e)

    # 3) (Opsiyonel) Geofence kontrolü — gönderene uygula
    try:
        lat = float(t.get("iha_enlem"))
        lon = float(t.get("iha_boylam"))
        if not is_inside_fences(lat, lon):
            socketio.emit("geofence_violation", {
                "takim": takim,
                "lat": lat,
                "lon": lon,
                "utc": now_iso()
            })
        else:
            socketio.emit("geofence_ok", {"takim": takim, "utc": now_iso()})
    except Exception as e:
        print("⚠️ Geofence kontrol hatası:", e)

    # 3b) (Opsiyonel) HSS kontrolü — gönderene uygula
    try:
        lat = float(t.get("iha_enlem"))
        lon = float(t.get("iha_boylam"))

        # HSS_SEND_ENABLED kapalıysa kimseyi HSS içinde sayma
        if HSS_SEND_ENABLED and HSS_SYSTEM_ACTIVE and is_inside_hss(lat, lon):
            socketio.emit("hss_inside", {
                "takim": takim,
                "lat": lat,
                "lon": lon,
                "utc": now_iso()
            })
        else:
            # ya HSS yoktur, ya dışarıdadır, ya da HSS tamamen devredışı
            socketio.emit("hss_ok", {"takim": takim, "utc": now_iso()})
    except Exception as e:
        print("⚠️ HSS kontrol hatası:", e)

    # 4) (Opsiyonel) DB'ye yaz
    try:
        save_telemetry_row(takim, t)
    except Exception as e:
        print("save_telemetry_row hata:", e)

    # 5) Enemies: diğer takımların güncel (bayat değilse) telemetrileri
    enemies = []
    try:
        now_ts = time.time()
        for tnum, info in list(_latest_telemetry.items()):
            ts = info.get("ts", 0)
            if _TELEMETRY_STALE_SEC is not None and (now_ts - ts) > _TELEMETRY_STALE_SEC:
                continue
            if tnum == takim:  # ← gönderenden farklı olanlar düşman
                continue

            packet = info.get("telemetry", {}) or {}

            def g(k, alts=()):
                if k in packet: return packet[k]
                for a in alts:
                    if a in packet: return packet[a]
                return None

            enemies.append({
                "takim_numarasi": int(tnum),
                "iha_enlem": g("iha_enlem", ["enlem", "lat", "latitude"]),
                "iha_boylam": g("iha_boylam", ["boylam", "lon", "longitude"]),
                "iha_irtifa": g("iha_irtifa", ["irtifa", "alt", "altitude"]),
                "iha_dikilme": g("iha_dikilme", ["dikilme", "pitch"]),
                "iha_yonelme": g("iha_yonelme", ["yonelme", "yaw", "heading"]),
                "iha_yatis": g("iha_yatis", ["yatis", "roll"]),
                "iha_hiz": g("iha_hiz", ["iha_hizi", "hiz", "speed"]),
                "iha_batarya": g("iha_batarya", ["batarya"]),
                "iha_otonom": g("iha_otonom", ["otonom"]),
                "iha_kilitlenme": g("iha_kilitlenme", ["kilitlenme"]),
                "hedef_merkez_X": g("hedef_merkez_X", ["target_center_x", "hx"]),
                "hedef_merkez_Y": g("hedef_merkez_Y", ["target_center_y", "hy"]),
                "hedef_genislik": g("hedef_genislik", ["target_w", "hw"]),
                "hedef_yukseklik": g("hedef_yukseklik", ["target_h", "hh"]),
                "gps_saati": g("gps_saati", ["gps_time", "time"]),
                "zaman_farki": 0
            })
    except Exception as e:
        print("enemies oluştururken hata:", e)
        enemies = []

    # 6) UI'ye yayın — 'takim' ve 'telemetry' gönderene ait
    try:
        socketio.emit('telemetry_update', {
            "takim": takim,

            # İHA ana telemetri bilgileri
            "lat": t.get("iha_enlem"),
            "lon": t.get("iha_boylam"),
            "alt": t.get("iha_irtifa"),
            "pitch": t.get("iha_dikilme"),
            "yaw": t.get("iha_yonelme"),
            "roll": t.get("iha_yatis"),
            "speed": t.get("iha_hiz"),
            "battery": t.get("iha_batarya"),
            "otonom": t.get("iha_otonom"),
            "kilit": t.get("iha_kilitlenme"),

            # HEDEF bilgileri
            "hedefX": t.get("hedef_merkez_X"),
            "hedefY": t.get("hedef_merkez_Y"),
            "hedefW": t.get("hedef_genislik"),
            "hedefH": t.get("hedef_yukseklik"),

            # GPS saati
            "gps": t.get("gps_saati"),

            # diğer takımlar
            "enemies": enemies,
            "sunucusaati": server_now_dict(),
            "telemetry": t
        })
    except Exception as _e:
        print("socketio.emit hata:", _e)

    # 7) HTTP cevabı aynı formatta (UI geriye uyumlu)
    return jsonify({
        "sunucusaati": server_now_dict(),
        "konumBilgileri": enemies,
        "hss_koordinat_bilgileri": [
            {"id": it["id"], "hssEnlem": it["lat"], "hssBoylam": it["lon"], "hssYaricap": it["radius"]}
            for it in list_hss()
        ] if HSS_SYSTEM_ACTIVE else []
    }), 200


@app.route("/api/kilitlenme_bilgisi", methods=["POST"])
def kilitlenme():
    if not ok_auth():
        print("401 kilitlenme_bilgisi")
        return "401", 401

    data = request.get_json(silent=True) or {}
    print("→ kilitlenme_bilgisi PAYLOAD:", data)

    kb = data.get("kilitlenmeBitisZamani", {})
    if not all(k in kb for k in ("saat", "dakika", "saniye", "milisaniye")):
        print("❌ VALIDATION FAIL (kilitlenmeBitisZamani alanları eksik)")
        return "", 204
    try:
        ok_flag = int(data.get("otonom_kilitlenme", -1))
    except Exception:
        ok_flag = -1
    if ok_flag not in (0, 1):
        print("❌ VALIDATION FAIL (otonom_kilitlenme 0/1 değil):", data.get("otonom_kilitlenme"))
        return "", 204

    # 1) DB'ye KAYDET
    try:
        save_lock_row(data)
        print("📝 lock→DB OK")
    except Exception as e:
        print("❌ save_lock_row HATA:", e, "| payload:", data)

    # 2) UI'ye CANLI YAYIN (TEK emit)
    kb = data.get("kilitlenmeBitisZamani", {})
    ok_flag = int(data.get("otonom_kilitlenme", 0)) == 1

    payload = {
        "otonom_kilitlenme": 1 if ok_flag else 0,
        "kilit_bitis_gps": kb
    }

    try:
        print("📢 lock_event emit:", payload)
        socketio.emit('lock_event', payload)  # tek yayın, broadcast
    except Exception as e:
        print("❌ socketio.emit lock HATA:", e)

    print("🔒 Kilitlenme OK")
    return "OK", 200


@app.route("/api/kamikaze_bilgisi", methods=["POST"])
def kamikaze():
    if not ok_auth():
        return "401", 401

    d = request.get_json(silent=True) or {}
    kb = d.get("kamikazeBaslangicZamani", {})
    ke = d.get("kamikazeBitisZamani", {})
    if not (all(k in kb for k in ("saat", "dakika", "saniye", "milisaniye")) and
            all(k in ke for k in ("saat", "dakika", "saniye", "milisaniye")) and
            ("qrMetni" in d)):
        return "", 204

    # 1) DB'ye kaydet
    try:
        save_kamikaze_row(d)
        print("📝 kamikaze→DB OK")
    except Exception as e:
        print("❌ save_kamikaze_row HATA:", e, "| payload:", d)

    # 2) UI'ye canlı yayın
    try:
        socketio.emit('kamikaze_event', {
            "kaynak_takim": d.get("kaynak_takim"),
            "qrMetni": d.get("qrMetni"),
            "kamikazeBaslangicZamani": d.get("kamikazeBaslangicZamani"),
            "kamikazeBitisZamani": d.get("kamikazeBitisZamani"),
            "sunucusaati": server_now_dict(),
        })
        print("📢 kamikaze_event emit")
    except Exception as e:
        print("❌ socketio.emit kamikaze HATA:", e)

    print("💥 Kamikaze:", d)
    return "OK", 200


@app.route("/api/qr_koordinati", methods=["GET"])
def qr():
    if not ok_auth():
        return "401", 401
    return jsonify({"qrEnlem": 41.51238882, "qrBoylam": 36.11935778}), 200


# helper:
def ok_auth_or_dev():
    # URL parametresi ile dev mod açılırsa yetkisiz erişime izin ver
    if request.args.get("dev") == "1":
        return True
    return ok_auth()


def ok_auth_or_public_hss():
    """
    HSS endpointi, yki_gorev_yazilimi'ndaki fetch_hss() ile
    (çoğu zaman Authorization/cookie olmadan) çağrılabildiği için
    auth YOKSA bile geçelim. Auth varsa yine kabul.
    """
    return ok_auth() or True  # HSS'i public yap


@app.route("/api/hss", methods=["GET"])
def api_hss_list():
    return jsonify({"ok": True, "items": list_hss()}), 200


@app.route("/api/hss", methods=["POST"])
def api_hss_create():
    if not ok_auth(): return "401", 401
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip() or "HSS"
    lat = d.get("lat")
    lon = d.get("lon")
    radius = d.get("radius")
    try:
        _id = insert_hss(name, float(lat), float(lon), float(radius))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        socketio.emit("hss_update", {"items": list_hss()})
    except:
        pass
    return jsonify({"ok": True, "id": _id}), 200


@app.route("/api/hss/<int:hid>", methods=["PUT"])
def api_hss_update(hid):
    if not ok_auth(): return "401", 401
    d = request.get_json(silent=True) or {}
    try:
        update_hss(hid, **d)
        socketio.emit("hss_update", {"items": list_hss()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True}), 200


@app.route("/api/hss/<int:hid>", methods=["DELETE"])
def api_hss_delete(hid):
    if not ok_auth(): return "401", 401
    delete_hss(hid)
    try:
        socketio.emit("hss_update", {"items": list_hss()})
    except:
        pass
    return jsonify({"ok": True}), 200


@app.route("/api/hss_koordinatlari", methods=["GET"])
def hss_public():
    if not HSS_SEND_ENABLED:
        hss_list = []
    else:
        items = list_hss()
        hss_list = [
            {"id": it["id"], "hssEnlem": it["lat"], "hssBoylam": it["lon"], "hssYaricap": it["radius"]}
            for it in items
        ]

    return jsonify({
        "sunucusaati": server_now_dict(),
        "hss_koordinat_bilgileri": hss_list,
        "durum": "aktif" if hss_list else "bos",
        "hss_aktif": HSS_SYSTEM_ACTIVE
    }), 200



@app.route("/api/fences", methods=["GET"])
def get_fences():
    return jsonify({"ok": True, "items": list_fences()}), 200


@app.route("/api/fences", methods=["POST"])
def create_fence():
    if not ok_auth(): return "401", 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "Geofence"
    kind = data.get("kind")
    gj = data.get("geojson")
    color = data.get("color") or "#ef4444"
    if kind not in ("polygon",) or not gj:
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute("INSERT INTO fences(name,kind,geojson,color,updated_at) VALUES(?,?,?,?,?)",
                (name, kind, json.dumps(gj), color, now_iso()))
    con.commit();
    con.close()
    items = list_fences()
    socketio.emit("fences_update", {"items": items})
    return jsonify({"ok": True, "items": items}), 200


@app.route("/api/fences/<int:fid>", methods=["PUT"])
def update_fence(fid):
    if not ok_auth(): return "401", 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "Geofence"
    kind = data.get("kind")
    gj = data.get("geojson")
    color = data.get("color") or "#ef4444"
    if kind not in ("polygon",) or not gj:
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute("UPDATE fences SET name=?,kind=?,geojson=?,color=?,updated_at=? WHERE id=?",
                (name, kind, json.dumps(gj), color, now_iso(), fid))
    con.commit();
    con.close()
    items = list_fences()
    socketio.emit("fences_update", {"items": items})
    return jsonify({"ok": True, "items": items}), 200


@app.route("/api/fences/<int:fid>", methods=["DELETE"])
def delete_fence(fid):
    if not ok_auth(): return "401", 401
    con = sqlite3.connect(DB_PATH);
    cur = con.cursor()
    cur.execute("DELETE FROM fences WHERE id=?", (fid,))
    con.commit();
    con.close()
    items = list_fences()
    socketio.emit("fences_update", {"items": items})
    return jsonify({"ok": True, "items": items}), 200


def point_in_polygon(lat, lon, polygon_latlon):
    # polygon_latlon: [[lat,lon], ...] (ilk/son kapanmasa da işler)
    x, y = lon, lat
    inside = False
    pts = [(p[1], p[0]) for p in polygon_latlon]  # (x,y) = (lon,lat)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def is_inside_fences(lat, lon):
    for f in list_fences():
        gj = f["geojson"]
        if f["kind"] == "polygon":
            try:
                outer = gj["geometry"]["coordinates"][0]  # [[lon,lat],...]
                poly = [[pt[1], pt[0]] for pt in outer]
                if point_in_polygon(lat, lon, poly): return True
            except:
                pass
    return False


@socketio.on('connect')
def on_connect():
    print("🔌 socket connected:", request.sid)


@socketio.on('disconnect')
def on_disconnect():
    print("🔌 socket disconnected:", request.sid)


from flask_socketio import emit
from flask import request


@socketio.on('fetch_history')
def on_fetch_history(payload):
    """
    payload: { takim?: int|string, start?: 'YYYY-MM-DDTHH:MM:SSZ', end?: 'YYYY-MM-DDTHH:MM:SSZ', limit?: int }
    """
    try:
        takim = payload.get("takim") if isinstance(payload, dict) else None
        start = payload.get("start") if isinstance(payload, dict) else None
        end = payload.get("end") if isinstance(payload, dict) else None
        limit = payload.get("limit") if isinstance(payload, dict) else 1000

        rows = query_telemetry_history(takim, start, end, limit)
        emit('history_result', {"ok": True, "rows": rows, "count": len(rows)}, room=request.sid)
    except Exception as e:
        print("❌ fetch_history hata:", e)
        emit('history_result', {"ok": False, "error": str(e)}, room=request.sid)


@socketio.on('fetch_locks')
def on_fetch_locks(payload):
    """
    payload: {
      kaynak?: int|string,         # kilitleyen takım (ops)
      kilitlenen?: int|string,     # kilitlenen takım (ops)
      start?: 'YYYY-MM-DDTHH:MM:SSZ',  # opss
      end?:   'YYYY-MM-DDTHH:MM:SSZ',  # ops
      limit?: int                  # varsayılan 1000
    }
    """
    try:
        kay = payload.get("kaynak") if isinstance(payload, dict) else None
        dst = payload.get("kilitlenen") if isinstance(payload, dict) else None
        start = payload.get("start") if isinstance(payload, dict) else None
        end = payload.get("end") if isinstance(payload, dict) else None
        limit = payload.get("limit") if isinstance(payload, dict) else 1000

        rows = query_locks_history(kay, dst, start, end, limit)
        emit('locks_result', {"ok": True, "rows": rows, "count": len(rows)}, room=request.sid)
    except Exception as e:
        print("❌ fetch_locks hata:", e)
        emit('locks_result', {"ok": False, "error": str(e)}, room=request.sid)


@socketio.on('fetch_kamikaze')
def on_fetch_kamikaze(payload):
    """
    payload: { kaynak?: int|string, start?: 'YYYY-MM-DDTHH:MM:SSZ', end?: 'YYYY-MM-DDTHH:MM:SSZ', limit?: int }
    """
    try:
        kaynak = payload.get("kaynak") if isinstance(payload, dict) else None
        start = payload.get("start") if isinstance(payload, dict) else None
        end = payload.get("end") if isinstance(payload, dict) else None
        limit = payload.get("limit") if isinstance(payload, dict) else 1000

        rows = query_kamikaze_history(kaynak, start, end, limit)
        emit('kamikaze_result', {"ok": True, "rows": rows, "count": len(rows)}, room=request.sid)
    except Exception as e:
        print("❌ fetch_kamikaze hata:", e)
        emit('kamikaze_result', {"ok": False, "error": str(e)}, room=request.sid)


if __name__ == "__main__":
    print("DB PATH =", os.path.abspath("iha_logs.db"))
    init_db()  # <-- şart
    socketio.run(app, host="0.0.0.0", port=10001, debug=True)
