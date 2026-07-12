import os
import json
import time
import asyncio
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

app = FastAPI(title="Link Extractor API")

# ------------------ Global driver (reused across requests) ------------------
_driver = None
_driver_lock = asyncio.Lock()
processing_lock = asyncio.Lock()   # ensures only one request uses the driver at a time

DOWNLOAD_TIMEOUT = 10  # reduced from 15s

_AGGRESSIVE_JS = """
(function() {
    if (window.__timerBypassDone) return;
    window.__timerBypassDone = true;
    const _st = window.setTimeout;
    const _si = window.setInterval;
    window.setTimeout = function(fn, d, ...a) { if (typeof d === 'number' && d > 50) d = 1; return _st(fn, d, ...a); };
    window.setInterval = function(fn, d, ...a) { if (typeof d === 'number' && d > 50) d = 1; return _si(fn, d, ...a); };
    setInterval(function() {
        ['downloadCountdown','countdown','seconds','count','wait','timer','countdownNum','timerValue','timeLeft'].forEach(varName => {
            if (typeof window[varName] !== 'undefined' && typeof window[varName] === 'number' && window[varName] > 0) window[varName] = 0;
        });
        document.querySelectorAll('[class*="countdown"], [class*="timer"], [class*="seconds"]').forEach(el => {
            if (el.textContent && /^\\d+$/.test(el.textContent.trim()) && parseInt(el.textContent.trim()) > 0) el.textContent = '0';
        });
        document.querySelectorAll('button[disabled]').forEach(btn => {
            btn.disabled = false;
            btn.removeAttribute('disabled');
        });
    }, 50);  // reduced frequency to save CPU
})();
"""

class UrlRequest(BaseModel):
    url: HttpUrl

def get_driver():
    global _driver
    if _driver is None:
        options = webdriver.ChromeOptions()
        options.page_load_strategy = 'eager'   # don't wait for all resources

        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--mute-audio")

        if os.path.exists("/usr/bin/chromium-browser"):
            options.binary_location = "/usr/bin/chromium-browser"
        elif os.path.exists("/usr/bin/chromium"):
            options.binary_location = "/usr/bin/chromium"

        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.managed_default_content_settings.stylesheets": 2,
        }
        options.add_experimental_option("prefs", prefs)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        _driver = webdriver.Chrome(options=options)
        _driver.set_page_load_timeout(12)   # slightly lower than before
        _driver.set_script_timeout(10)
    return _driver

def wait_cdp_download(driver, timeout):
    deadline = time.time() + timeout
    seen = set()
    exts = (".rar", ".zip", ".7z", ".exe", ".part", ".iso", ".mkv", ".mp4", ".bin")
    while time.time() < deadline:
        try:
            logs = driver.get_log("performance")
        except Exception:
            time.sleep(0.05)
            continue
        for entry in logs:
            try:
                msg = json.loads(entry["message"]).get("message", {})
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Page.downloadWillBegin":
                    return params.get("url", "")
                if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                    url = params.get("request", {}).get("url", "") if method == "Network.requestWillBeSent" else params.get("response", {}).get("url", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    if url.lower().endswith(exts) or any(e in url.lower() for e in exts):
                        return url
            except Exception:
                continue
        time.sleep(0.05)
    return None

def execute_extraction(url: str):
    driver = get_driver()   # reused driver

    try:
        # Inject timer killer before navigation
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _AGGRESSIVE_JS})
        except Exception:
            pass

        driver.get(url)

        # ---- Click sequence with reduced timeouts (2s each) ----
        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[@id='method_free']")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Free Download')]")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        try:
            btn = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Start Download')]")))
            driver.execute_script("arguments[0].click();", btn)
        except TimeoutException:
            pass

        # Clear old logs
        try:
            driver.get_log("performance")
        except Exception:
            pass

        dl_url = wait_cdp_download(driver, timeout=DOWNLOAD_TIMEOUT)
        if dl_url:
            return {"status": "success", "original_url": url, "download_url": dl_url}
        else:
            raise HTTPException(status_code=404, detail="Download URL not captured within timeout")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

@app.post("/extract")
async def extract_url(payload: UrlRequest):
    async with processing_lock:          # only one request at a time
        # Run the synchronous extraction in a thread to avoid blocking the event loop
        result = await asyncio.to_thread(execute_extraction, str(payload.url))
        return result

@app.get("/health")
async def health():
    return {"status": "ok"}
