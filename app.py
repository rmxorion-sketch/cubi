from flask import Flask, request, jsonify, send_file
import subprocess, os, tempfile, hashlib, json, io

app = Flask(__name__)

BOARD     = "esp32dev"
PLATFORM  = "espressif32@6.5.0"
FRAMEWORK = "arduino"
CACHE_DIR = "/tmp/turin_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

LIBS_BASE = [
    "bblanchon/ArduinoJson@^6.21.0",
    "madhephaestus/ESP32Servo@^0.13.0"
]

LIBS_OPTIONAL = {
    "LiquidCrystal": "marcoschwartz/LiquidCrystal_I2C@^1.1.4",
    "lcd":           "marcoschwartz/LiquidCrystal_I2C@^1.1.4",
    "LCD":           "marcoschwartz/LiquidCrystal_I2C@^1.1.4",
}

def get_libs(cpp_code: str) -> list:
    libs = list(LIBS_BASE)
    for keyword, lib in LIBS_OPTIONAL.items():
        if keyword in cpp_code and lib not in libs:
            libs.append(lib)
    return libs

def inject_ota(cpp_code: str) -> str:
    """Inyecta OTA como include separado para evitar conflictos de compilación."""
    if "_ota_setup" in cpp_code:
        return cpp_code  # ya tiene OTA
    cpp_code = cpp_code.replace("#include <Arduino.h>", "")
    header = '#include <Arduino.h>\n#include "turin_ota.h"\n\n'
    cpp_code = header + cpp_code
    cpp_code = cpp_code.replace("void setup() {",  "void setup() {\n    _ota_setup();\n")
    cpp_code = cpp_code.replace("void setup(){",   "void setup(){\n    _ota_setup();\n")
    cpp_code = cpp_code.replace("void loop() {",   "void loop() {\n    _ota_loop();\n")
    cpp_code = cpp_code.replace("void loop(){",    "void loop(){\n    _ota_loop();\n")
    return cpp_code

TURIN_OTA_H = """\
#pragma once
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <Preferences.h>
#include <ArduinoJson.h>

static WebServer _ota_server(8266);
static Preferences _ota_prefs;

inline void _ota_setup() {
    _ota_prefs.begin("turin", false);
    String ssid = _ota_prefs.getString("ssid", "");
    String pass = _ota_prefs.getString("pass", "");
    if (ssid.length() > 0) {
        WiFi.begin(ssid.c_str(), pass.c_str());
        int t = 0;
        while (WiFi.status() != WL_CONNECTED && t < 20) { delay(500); t++; }
    }
    if (WiFi.status() != WL_CONNECTED) WiFi.softAP("TURIN-G-Setup", "12345678");

    _ota_server.on("/status", HTTP_GET, []() {
        String ip = (WiFi.status()==WL_CONNECTED) ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
        _ota_server.send(200, "application/json",
            "{\\"device\\":\\"TURIN-G\\",\\"ip\\":\\"" + ip + "\\"}");
    });
    _ota_server.on("/wifi", HTTP_POST, []() {
        StaticJsonDocument<256> doc;
        if (!deserializeJson(doc, _ota_server.arg("plain"))) {
            _ota_prefs.putString("ssid", doc["ssid"].as<String>());
            _ota_prefs.putString("pass", doc["pass"].as<String>());
            _ota_server.send(200, "text/plain", "OK");
            delay(500); ESP.restart();
        } else { _ota_server.send(400, "text/plain", "Error"); }
    });
    _ota_server.on("/ota", HTTP_POST,
        []() {
            _ota_server.send(200, "text/plain", Update.hasError() ? "FAIL" : "OK");
            if (!Update.hasError()) { delay(200); ESP.restart(); }
        },
        []() {
            HTTPUpload& u = _ota_server.upload();
            if (u.status == UPLOAD_FILE_START)        Update.begin(UPDATE_SIZE_UNKNOWN);
            else if (u.status == UPLOAD_FILE_WRITE)   Update.write(u.buf, u.currentSize);
            else if (u.status == UPLOAD_FILE_END)     Update.end(true);
        });
    _ota_server.begin();
}

inline void _ota_loop() {
    _ota_server.handleClient();
}
"""

def get_pio():
    import shutil
    return shutil.which("pio") or shutil.which("platformio")

# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "TURIN-G Compile Server", "version": "2.4"})

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
    # OTA removido para reducir uso de RAM en compilación (Railway)
    # cpp_code = inject_ota(cpp_code)

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

    # turin_ota.h removido (OTA quitado para reducir RAM en compilación)

    lib_deps = "\n    ".join(get_libs(cpp_code))
    with open(os.path.join(proj, "platformio.ini"), "w") as f:
        f.write(f"""[env:{BOARD}]
platform = {PLATFORM}
board    = {BOARD}
framework = {FRAMEWORK}
monitor_speed = 115200
board_build.partitions = min_spiffs.csv
build_flags = -DCORE_DEBUG_LEVEL=0 -w -DCONFIG_BT_ENABLED=1
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
