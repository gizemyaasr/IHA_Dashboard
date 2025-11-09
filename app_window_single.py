import time, socket, subprocess, sys
import webview

# Sunucunun gösterdiği panel adresi
HOST_IP = socket.gethostbyname(socket.gethostname())  # kendi IP adresini otomatik bulur
URL = f"http://{HOST_IP}:10001/dashboard"

def wait_port(host=HOST_IP, port=10001, timeout=20):
    start = time.time()
    while time.time()-start < timeout:
        try:
            # Sunucu ayağa kalktı mı?
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False

if __name__ == "__main__":
    # Aynı klasördeki fake_server.py'yi arka planda başlat
    srv = subprocess.Popen([sys.executable, "fake_server.py"])
    try:
        wait_port()
        # Ubuntu 20.04 için Qt backend en sorunsuz
        webview.create_window("IHA Telemetri Dashboard", URL, width=1200, height=800)
        webview.start(gui="qt")
    finally:
        # Pencere kapanınca sunucuyu da kapat
        srv.terminate()
