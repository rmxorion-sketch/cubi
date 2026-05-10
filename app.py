from flask import Flask, request, jsonify
import subprocess, os, tempfile, hashlib, json

app = Flask(__name__)

BOARD     = "esp32dev"
PLATFORM  = "espressif32"
FRAMEWORK = "arduino"
CACHE_DIR = "/tmp/turin_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

LIBS = [
    "knolleary/PubSubClient@^2.8",
    "bblanchon/ArduinoJson@^6.21.0",
    "marcoschwartz/LiquidCrystal_I2C@^1.1.4",
    "madhephaestus/ESP32Servo@^0.13.0"
]

def get_pio():
    import shutil
    return shutil.which("pio") or shutil.which("platformio")

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "TURIN-G Compile Server", "version": "1.0"})

@app.route("/compile", methods=["POST"])
def compile_code():
    data = request.get_json()
    if not data or "code" not in data:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    cpp_code = data["code"]

    # Add Arduino.h if missing
    if "#include <Arduino.h>" not in cpp_code:
        cpp_code = "#include <Arduino.h>\n" + cpp_code

    # Cache check
    code_hash = hashlib.md5(cpp_code.encode()).hexdigest()
    bin_path  = os.path.join(CACHE_DIR, f"{code_hash}.bin")
    if os.path.isfile(bin_path):
        with open(bin_path, "rb") as f:
            return f.read(), 200, {
                "Content-Type": "application/octet-stream",
                "X-Cache": "HIT"
            }

    pio = get_pio()
    if not pio:
        return jsonify({"ok": False, "error": "PlatformIO not installed on server"}), 500

    # Create temp project
    proj = tempfile.mkdtemp(prefix="turin_")
    src  = os.path.join(proj, "src")
    os.makedirs(src)

    with open(os.path.join(src, "main.cpp"), "w") as f:
        f.write(cpp_code)

    lib_deps = "\n    ".join(LIBS)
    with open(os.path.join(proj, "platformio.ini"), "w") as f:
        f.write(f"""[env:{BOARD}]
platform = {PLATFORM}
board = {BOARD}
framework = {FRAMEWORK}
monitor_speed = 115200
lib_deps =
    {lib_deps}
""")

    # Compile
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

    # Find .bin file
    bin_file = None
    for root, dirs, files in os.walk(proj):
        for f in files:
            if f.endswith(".bin") and "firmware" in f:
                bin_file = os.path.join(root, f)
                break
        if bin_file: break

    if not bin_file:
        # Try alternate name
        for root, dirs, files in os.walk(proj):
            for f in files:
                if f.endswith(".bin"):
                    bin_file = os.path.join(root, f)
                    break
            if bin_file: break

    if not bin_file:
        return jsonify({"ok": False, "error": "Binary not found after compilation"}), 500

    # Cache and return
    with open(bin_file, "rb") as f:
        binary = f.read()

    with open(bin_path, "wb") as f:
        f.write(binary)

    return binary, 200, {"Content-Type": "application/octet-stream", "X-Cache": "MISS"}

@app.route("/health", methods=["GET"])
def health():
    pio = get_pio()
    return jsonify({"ok": True, "pio": pio or "not found"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
