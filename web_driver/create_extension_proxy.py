import json
import shutil
import zipfile

from pathlib import Path


def create_firefox_proxy_addon(out_dir: str, proxy: str, scheme: str = "http") -> str:
    """
    Делает временное Firefox-расширение (.xpi) с прокси-авторизацией.
    proxy: строка вида http://login:pass@host:port (scheme игнорируется, берём из аргумента)
    Возвращает путь к .xpi
    """
    # разбор строки прокси
    proxy = proxy.replace("://", "://", 1)  # на случай других схем
    creds, hostport = proxy.split("@")
    proxy_user, proxy_pass = creds.split("://", 1)[1].split(":", 1)
    proxy_host, proxy_port = hostport.split(":")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"proxy_addon_{proxy_user}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir()

    # Firefox: manifest_version=2 (надёжнее для локального временного инстала)
    manifest = {
        "manifest_version": 2,
        "name": "Proxy+Stealth (Firefox)",
        "version": "1.0.1",
        "permissions": [
            "proxy",
            "webRequest",
            "webRequestBlocking",
            "<all_urls>"
        ],
        "background": {"scripts": ["background.js"]},
        "content_scripts": [
            {
                "matches": ["<all_urls>"],
                "js": ["stealth.js"],
                "run_at": "document_start",
                "all_frames": True
            }
        ],
        "applications": {
            "gecko": {"id": "proxy-stealth-evirma@example.com"}
        }
    }

    # Proxy через proxy.onRequest + авторизация через webRequest.onAuthRequired
    background_js = f"""
    // Настройка прокси
    browser.proxy.onRequest.addListener(
      () => ({{ type: "http", host: "{proxy_host}", port: {int(proxy_port)} }}),
      {{ urls: ["<all_urls>"] }}
    );
    browser.proxy.onError.addListener(e => console.error("proxy error", e));

    // Авторизация (Basic/Proxy-Auth)
    browser.webRequest.onAuthRequired.addListener(
      details => {{
        return {{ authCredentials: {{ username: "{proxy_user}", password: "{proxy_pass}" }} }};
      }},
      {{ urls: ["<all_urls>"] }},
      ["blocking"]
    );
    """.strip()

    stealth_page_js = r"""
    // webdriver -> undefined
    try {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch(e) {}

    // fake window.chrome
    try {
      if (!window.chrome) {
        Object.defineProperty(window, 'chrome', { value: { runtime: {} } });
      }
    } catch(e) {}

    // permissions.query patch (notifications)
    try {
      const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
      if (originalQuery) {
        window.navigator.permissions.query = (parameters) => (
          parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
        );
      }
    } catch(e) {}

    // optional: small Canvas noise (микро-рандомизация отпечатка)
    try {
      const toDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function() {
        try {
          const ctx = this.getContext('2d');
          if (ctx) {
            const {width, height} = this;
            ctx.globalCompositeOperation = 'multiply';
            ctx.fillStyle = 'rgba(1,1,1,0.0015)';
            ctx.fillRect(0,0,width,height);
          }
        } catch(e) {}
        return toDataURL.apply(this, arguments);
      };
    } catch(e) {}
    """.strip()

    # контент-скрипт, который вставляет вышеуказанный код в контекст страницы
    stealth_js = f"""
    (function inject(){{
      const code =
    {json.dumps(stealth_page_js)};
    const
    s = document.createElement('script');
    s.textContent = code;
    (document.documentElement || document.head || document.documentElement).appendChild(s);
    s.remove();
    }})();
    """

    (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    (work_dir / "background.js").write_text(background_js, "utf-8")
    (work_dir / "stealth.js").write_text(stealth_js, "utf-8")

    # упаковать в .xpi (обычный zip)
    xpi_path = out_dir / f"proxy_{proxy_user}.xpi"
    with zipfile.ZipFile(xpi_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(work_dir / "manifest.json", "manifest.json")
        z.write(work_dir / "background.js", "background.js")
        z.write(work_dir / "stealth.js", "stealth.js")

    return str(xpi_path)
