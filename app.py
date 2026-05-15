from flask import Flask, request, jsonify, send_file
import subprocess, os, tempfile, hashlib, json, io

app = Flask(__name__)

BOARD     = "esp32dev"
PLATFORM  = "espressif32"
FRAMEWORK = "arduino"
CACHE_DIR = "/tmp/turin_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

LIBS = [
    "knolleary/PubSubClient@^2.8",
    "bblanchon/ArduinoJson@^6.21.0",
    "johnrickman/LiquidCrystal I2C@^1.1.2",
    "madhephaestus/ESP32Servo@^0.13.0"
]

# ─── Bloque OTA que se inyecta en TODO firmware generado ──────────────────────
OTA_HEADER = """
#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <Preferences.h>

// ── Turin OTA Core ─────────────────────────────────────────────────────────
WebServer _ota_server(8266);
Preferences _ota_prefs;

void _ota_setup() {
    _ota_prefs.begin("turin", false);
    String ssid = _ota_prefs.getString("ssid", "");
    String pass = _ota_prefs.getString("pass", "");

    if (ssid.length() > 0) {
        WiFi.begin(ssid.c_str(), pass.c_str());
        int tries = 0;
        while (WiFi.status() != WL_CONNECTED && tries < 20) {
            delay(500); tries++;
        }
    }

    if (WiFi.status() != WL_CONNECTED) {
        // Sin WiFi configurado: levantar hotspot de configuracion
        WiFi.softAP("TURIN-G-Setup", "12345678");
    }

    // Endpoint: recibir credenciales WiFi
    _ota_server.on("/wifi", HTTP_POST, []() {
        String body = _ota_server.arg("plain");
        StaticJsonDocument<256> doc;
        if (!deserializeJson(doc, body)) {
            _ota_prefs.putString("ssid", doc["ssid"].as<String>());
            _ota_prefs.putString("pass", doc["pass"].as<String>());
            _ota_server.send(200, "text/plain", "OK");
            delay(500); ESP.restart();
        } else {
            _ota_server.send(400, "text/plain", "Bad JSON");
        }
    });

    // Endpoint: recibir firmware OTA
    _ota_server.on("/ota", HTTP_POST, []() {
        bool ok = !Update.hasError();
        _ota_server.send(200, "text/plain", ok ? "OK" : "FAIL");
        if (ok) { delay(200); ESP.restart(); }
    }, []() {
        HTTPUpload& upload = _ota_server.upload();
        if (upload.status == UPLOAD_FILE_START) {
            Update.begin(UPDATE_SIZE_UNKNOWN);
        } else if (upload.status == UPLOAD_FILE_WRITE) {
            Update.write(upload.buf, upload.currentSize);
        } else if (upload.status == UPLOAD_FILE_END) {
            Update.end(true);
        }
    });

    // Endpoint: status (para que la app Android detecte el dispositivo)
    _ota_server.on("/status", HTTP_GET, []() {
        String ip  = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
        String msg = "{\\"device\\":\\"TURIN-G\\",\\"version\\":\\"1.5\\",\\"ip\\":\\"" + ip + "\\"}";
        _ota_server.send(200, "application/json", msg);
    });

    _ota_server.begin();
}

void _ota_loop() {
    _ota_server.handleClient();
}
// ── Fin Turin OTA Core ──────────────────────────────────────────────────────
"""

OTA_SETUP_CALL = "\n    _ota_setup(); // Turin OTA\n"
OTA_LOOP_CALL  = "\n    _ota_loop();  // Turin OTA\n"

def inject_ota(cpp_code: str) -> str:
    """Inyecta el bloque OTA en el codigo del usuario sin modificar su logica."""
    if "_ota_setup" in cpp_code:
        return cpp_code  # ya tiene OTA, no duplicar

    # Quitar #include <Arduino.h> si ya estaba (lo ponemos en OTA_HEADER)
    cpp_code = cpp_code.replace("#include <Arduino.h>", "")

    # Insertar header OTA al principio
    cpp_code = OTA_HEADER + "\n" + cpp_code

    # Inyectar _ota_setup() al inicio de setup()
    cpp_code = cpp_code.replace("void setup() {", "void setup() {" + OTA_SETUP_CALL)
    cpp_code = cpp_code.replace("void setup(){",  "void setup(){" + OTA_SETUP_CALL)

    # Inyectar _ota_loop() al inicio de loop()
    cpp_code = cpp_code.replace("void loop() {", "void loop() {" + OTA_LOOP_CALL)
    cpp_code = cpp_code.replace("void loop(){",  "void loop(){" + OTA_LOOP_CALL)

    return cpp_code

def get_pio():
    import shutil
    return shutil.which("pio") or shutil.which("platformio")

# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "TURIN-G Compile Server", "version": "2.0"})

@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    import shutil
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    return jsonify({"ok": True, "msg": "Cache limpiado"})

@app.route("/health", methods=["GET"])
def health():
    pio = get_pio()
    return jsonify({"ok": True, "pio": pio or "not found"})

@app.route("/compile", methods=["POST"])
def compile_code():
    data = request.get_json()
    if not data or "code" not in data:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    cpp_code = data["code"]
    cpp_code = inject_ota(cpp_code)          # <── OTA siempre presente

    # Cache
    code_hash = hashlib.md5(cpp_code.encode()).hexdigest()
    bin_path  = os.path.join(CACHE_DIR, f"{code_hash}.bin")
    meta_path = os.path.join(CACHE_DIR, f"{code_hash}.json")

    if os.path.isfile(bin_path):
        with open(bin_path, "rb") as f:
            return f.read(), 200, {
                "Content-Type": "application/octet-stream",
                "X-Cache": "HIT",
                "X-Hash": code_hash
            }

    pio = get_pio()
    if not pio:
        return jsonify({"ok": False, "error": "PlatformIO not found on server"}), 500

    # Proyecto temporal
    proj = tempfile.mkdtemp(prefix="turin_")
    src  = os.path.join(proj, "src")
    os.makedirs(src)

    with open(os.path.join(src, "main.cpp"), "w") as f:
        f.write(cpp_code)

    lib_deps = "\n    ".join(LIBS)
    with open(os.path.join(proj, "platformio.ini"), "w") as f:
        f.write(f"""[env:{BOARD}]
platform = {PLATFORM}
board    = {BOARD}
framework = {FRAMEWORK}
monitor_speed = 115200
board_build.partitions = min_spiffs.csv
build_flags = -DCORE_DEBUG_LEVEL=0 -w
lib_deps =
    {lib_deps}
""")

    result = subprocess.run(
        [pio, "run", "--project-dir", proj],
        capture_output=True, text=True, timeout=300
    )

    if result.returncode != 0:
        return jsonify({
            "ok": False,
            "error": "Compilation failed",
            "details": result.stdout[-2000:] + result.stderr[-1000:]
        }), 400

    # Buscar .bin
    bin_file = None
    for root, dirs, files in os.walk(proj):
        for fname in files:
            if fname.endswith(".bin") and "firmware" in fname:
                bin_file = os.path.join(root, fname); break
        if bin_file: break
    if not bin_file:
        for root, dirs, files in os.walk(proj):
            for fname in files:
                if fname.endswith(".bin"):
                    bin_file = os.path.join(root, fname); break
            if bin_file: break

    if not bin_file:
        return jsonify({"ok": False, "error": "Binary not found"}), 500

    with open(bin_file, "rb") as f:
        binary = f.read()

    # Guardar cache + metadata
    with open(bin_path, "wb") as f: f.write(binary)
    meta = {"hash": code_hash, "size": len(binary), "version": "1.5"}
    with open(meta_path, "w") as f: json.dump(meta, f)

    return binary, 200, {
        "Content-Type": "application/octet-stream",
        "X-Cache": "MISS",
        "X-Hash": code_hash
    }

@app.route("/firmware/latest/<code_hash>", methods=["GET"])
def firmware_latest(code_hash):
    """La app Android descarga el .bin por hash para enviarlo por OTA al ESP32."""
    bin_path  = os.path.join(CACHE_DIR, f"{code_hash}.bin")
    meta_path = os.path.join(CACHE_DIR, f"{code_hash}.json")
    if not os.path.isfile(bin_path):
        return jsonify({"ok": False, "error": "Firmware not found"}), 404
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path) as f: meta = json.load(f)
    return send_file(bin_path,
                     mimetype="application/octet-stream",
                     as_attachment=True,
                     download_name="firmware.bin")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
