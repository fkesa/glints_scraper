#!/usr/bin/env python3
"""
Scrape Glints job listings (live DOM via Selenium) lalu kelompokkan dengan Gemini 2.5 Flash.
- Target container bisa di-set via --container-xpath (default sesuai yang kamu berikan).
- Ekstraksi field langsung dari job card (data-attributes + CSS yang stabil), bukan page_source.
- Output: CSV + JSONL.

Contoh pakai:
  python glints_scrape_gemini.py ^
    --keyword "social media" ^
    --country ID ^
    --no-headless ^
    --max-scrolls 40 ^
    --out jobs_social_media
    --keep-tabs

Gemini:
  Buat .env berisi GEMINI_API_KEY=xxx
  Tambah flag --ai untuk AKTIFKAN pengelompokan AI (default: mati).
"""

from __future__ import annotations
import os
import re
import csv
import json
import time
import random
import argparse
import google.generativeai as genai
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
from dotenv import load_dotenv
from pathlib import Path
import undetected_chromedriver as uc
# ==== Selenium ====
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    WebDriverException,
    NoSuchWindowException
)

try:
    HAS_UC = True
    # >>> matikan destructor yang suka bikin OSError di Windows <<<
    try:
        uc.Chrome.__del__ = lambda self: None
    except Exception:
        pass
except Exception:
    HAS_UC = False

# ==== Cookies helper ====
def _normalize_cookie(c: Dict[str, Any]) -> Dict[str, Any]:
    """Ambil hanya field yang didukung Selenium dan normalisasi nama kunci."""
    if not isinstance(c, dict):
        return {}
    out = {
        "name": c.get("name"),
        "value": c.get("value"),
        "domain": c.get("domain") or "glints.com",
        "path": c.get("path") or "/",
        "secure": bool(c.get("secure", False)),
    }
    # variasi kunci expiry
    exp = c.get("expiry") or c.get("expires") or c.get("expirationDate")
    if exp is not None:
        try:
            out["expiry"] = int(float(exp))
        except Exception:
            pass
    # samesite (opsional; Selenium 4 mendukung)
    if c.get("sameSite"):
        out["sameSite"] = c["sameSite"]
    return {k: v for k, v in out.items() if v is not None and k in {"name","value","domain","path","secure","expiry","sameSite"}}

def _parse_cookie_header(s: str) -> List[Dict[str, Any]]:
    """Parse 'a=1; b=2' -> [{'name':'a','value':'1'}, {'name':'b','value':'2'}]."""
    items = []
    for part in s.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            items.append({"name": name, "value": value, "domain": "glints.com", "path": "/"})
    return items

def _read_cookies_from_file(path: str) -> List[Dict[str, Any]]:
    """Dukung JSON array/dict, JSONL, dan Netscape cookies.txt."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    # Coba JSON (array/dict)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            obj = [obj]
        if isinstance(obj, list):
            return [_normalize_cookie(c) for c in obj if _normalize_cookie(c).get("name")]
    except Exception:
        pass

    # Coba JSONL
    cookies = []
    try:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                nc = _normalize_cookie(c)
                if nc.get("name"):
                    cookies.append(nc)
            except Exception:
                continue
        if cookies:
            return cookies
    except Exception:
        pass

    # Coba Netscape cookies.txt
    cookies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        # domain \t flag \t path \t secure \t expiry \t name \t value
        if len(parts) >= 7:
            domain, _flag, path, secure, expiry, name, value = parts[:7]
            try:
                expiry = int(expiry)
            except Exception:
                expiry = None
            cookies.append(_normalize_cookie({
                "domain": domain.lstrip(".") or "glints.com",
                "path": path or "/",
                "secure": (secure.upper() == "TRUE"),
                "expiry": expiry,
                "name": name,
                "value": value,
            }))
    return [c for c in cookies if c.get("name")]

def load_cookies_arg(cookies_arg: str) -> List[Dict[str, Any]]:
    """
    Terima path file atau string 'a=1; b=2'. Kembalikan list cookie siap add_cookie.
    """
    if not cookies_arg:
        return []
    p = Path(cookies_arg)
    if p.exists() and p.is_file():
        return _read_cookies_from_file(str(p))
    # fallback: treat as header string
    return [_normalize_cookie(c) for c in _parse_cookie_header(cookies_arg)]

def inject_cookies(driver: webdriver.Chrome, cookies: List[Dict[str, Any]], base_url: str = "https://glints.com/"):
    """
    Buka domain basis, set cookies satu per satu, lalu refresh.
    """
    if not cookies:
        return
    try:
        driver.get(base_url)
        polite_sleep(0.4, 0.8)
    except Exception:
        pass

    ok = 0
    for c in cookies:
        try:
            # Pastikan domain cocok
            if not c.get("domain"):
                c["domain"] = "glints.com"
            driver.add_cookie(c)
            ok += 1
        except Exception:
            # Kadang domain subdomain: coba hapus domain agar default ke host saat ini
            try:
                c2 = dict(c)
                c2.pop("domain", None)
                driver.add_cookie(c2)
                ok += 1
            except Exception:
                continue
    if ok:
        try:
            driver.refresh()
            polite_sleep(0.6, 1.0)
        except Exception:
            pass

# ==== Cleaners for CSV / JSONL ====

def flatten_ws(s: str) -> str:
    """Hapus newline/CR/NBSP, rapikan spasi berlebih."""
    if not isinstance(s, str):
        return "" if s is None else str(s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\u00A0", " ")
    return re.sub(r"\s+", " ", s).strip()

def clean_salary(val: str, title: str = "") -> str:
    t = flatten_ws(val)
    if not t:
        return ""
    # hapus title kalau nyampur
    if title and t.lower().startswith(title.lower()):
        t = t[len(title):].strip()

    if re.search(r"gaji\s+tidak\s+ditampilkan|not\s+disclosed", t, re.I):
        return "Gaji Tidak Ditampilkan"
    m = re.search(r"(Rp[^A-Za-z]*?\d[\d\.\,\s\-–to+]*\d(?:\s*jt)?)", t, re.I)
    if m:
        return flatten_ws(m.group(1))
    m2 = re.search(r"(USD[^A-Za-z]*?\d[\d\.\,\s\-–to+]*\d)", t, re.I)
    return flatten_ws(m2.group(1)) if m2 else t

def absolutize_link(href: str) -> str:
    t = (href or "").strip()
    if not t:
        return ""
    if t.startswith(("http://","https://")):
        return t
    if not t.startswith("/"):
        t = "/" + t
    return "https://glints.com" + t

def join_list(v):
    if isinstance(v, list):
        return ", ".join(flatten_ws(x) for x in v if str(x).strip())
    return flatten_ws(v)

# ==== Glints ====
GLINTS_BASE_URL = (
    "https://glints.com/id/opportunities/jobs/explore"
    "?keyword={keyword}&country={country}"
    "&locationName=All+Cities%2FProvinces&lowestLocationLevel=1"
)

def find_container_auto(driver, container_xpath: str | None):
    """
    Usahakan pakai container_xpath bila ada.
    Kalau timeout/invalid, cari container via CSS yang stabil.
    """
    # 1) coba pakai XPATH kalau diisi
    if container_xpath:
        try:
            el = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.XPATH, container_xpath))
            )
            return el
        except TimeoutException:
            pass

    # 2) tunggu minimal satu kartu muncul di DOM
    first_card = WebDriverWait(driver, 25).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-gtm-job-id]"))
    )

    # 3) coba naik ke ancestor yang berfungsi sebagai container
    ancestors = driver.execute_script("""
        const el = arguments[0];
        const xs = [];
        let n = el;
        for (let i=0;i<10 && n;i++){
            xs.push(n);
            n = n.parentElement;
        }
        return xs;
    """, first_card)

    # pilih ancestor yang punya banyak card di dalamnya
    best = None
    best_count = 0
    for node in ancestors:
        try:
            cnt = driver.execute_script(
                "return arguments[0].querySelectorAll('[data-gtm-job-id]').length;", node
            )
            if cnt > best_count:
                best = node
                best_count = cnt
        except Exception:
            continue

    if best:
        return best

    # 4) fallback: gak perlu container (operasi di dokumen)
    return None

def is_scrollable(driver, el):
    return driver.execute_script("""
        const el = arguments[0];
        if(!el) return false;
        const style = getComputedStyle(el);
        const oy = style.overflowY;
        return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > (el.clientHeight + 4);
    """, el)

def get_scrollable_ancestor(driver, el):
    if not el:  # no container → pakai document
        return driver.execute_script("return document.scrollingElement;")
    node = el
    for _ in range(8):
        if is_scrollable(driver, node):
            return node
        try:
            node = node.find_element(By.XPATH, "..")
        except Exception:
            break
    return driver.execute_script("return document.scrollingElement;")

def is_attached(driver, el) -> bool:
    try:
        return bool(driver.execute_script("return arguments[0] && arguments[0].isConnected === true;", el))
    except Exception:
        return False

def get_fresh_container(driver, container_xpath: str | None):
    if not container_xpath:
        return None
    try:
        return WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, container_xpath))
        )
    except Exception:
        return None

def scroll_list_until_no_growth(driver, scope_el, max_loops=80, min_growth=1, pause=(0.35, 0.6)):
    """
    Scroll elemen yang benar-benar scrollable (ancestor) agar virtualized list me-render lebih banyak card.
    scope_el: container (boleh None → dokumen). Tahan stale dengan fallback.
    """
    def safe_scope():
        # jika scope_el sudah tidak ter-attach, pakai None (dokumen)
        if scope_el and is_attached(driver, scope_el):
            return scope_el
        return None

    def count_cards():
        try:
            sc = safe_scope()
            if sc:
                return driver.execute_script(
                    "return arguments[0].querySelectorAll('[data-gtm-job-id],[data-testid=\"opportunity-card\"]').length;",
                    sc
                )
            else:
                return driver.execute_script(
                    "return document.querySelectorAll('[data-gtm-job-id],[data-testid=\"opportunity-card\"]').length;"
                )
        except StaleElementReferenceException:
            # fallback keras: pakai dokumen
            return driver.execute_script(
                "return document.querySelectorAll('[data-gtm-job-id],[data-testid=\"opportunity-card\"]').length;"
            )
        except Exception:
            return 0

    # scrollable ancestor bisa ikut stale juga, jadi re-evaluasi bila perlu
    scrollable = get_scrollable_ancestor(driver, safe_scope())

    def ensure_scrollable():
        nonlocal scrollable
        if not scrollable or not is_attached(driver, scrollable):
            scrollable = get_scrollable_ancestor(driver, safe_scope())

    last = count_cards()
    stagn = 0
    for i in range(max_loops):
        ensure_scrollable()
        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight * 0.92;",
                scrollable
            )
        except StaleElementReferenceException:
            ensure_scrollable()
        time.sleep(random.uniform(*pause))

        try:
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop - 120;", scrollable)
        except StaleElementReferenceException:
            ensure_scrollable()
        time.sleep(random.uniform(0.12, 0.25))

        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight * 0.98;",
                scrollable
            )
        except StaleElementReferenceException:
            ensure_scrollable()
        time.sleep(random.uniform(*pause))

        newc = count_cards()
        print(f"[scroll-list {i+1}/{max_loops}] cards={newc}")
        if newc - last < min_growth:
            stagn += 1
        else:
            stagn = 0
        last = newc
        if stagn >= 3:
            break

def refetch_card_by_index(driver, container, idx):
    """
    Ambil ulang elemen card ke-idx dari scope (container atau dokumen).
    Return None kalau tidak ketemu.
    """
    try:
        if container and is_attached(driver, container):
            el = driver.execute_script(
                "const els = arguments[0].querySelectorAll('[data-gtm-job-id]');"
                "return (arguments[1] < els.length) ? els[arguments[1]] : null;",
                container, idx
            )
        else:
            el = driver.execute_script(
                "const els = document.querySelectorAll('[data-gtm-job-id]');"
                "return (arguments[0] < els.length) ? els[arguments[0]] : null;",
                idx
            )
        return el
    except Exception:
        return None

def extract_jobs_from_container(driver: webdriver.Chrome, container_xpath: str, keyword: str) -> List[Job]:
    """
    Versi robust:
    - auto-detect container bila XPATH gagal
    - scroll ancestor scrollable (bukan window)
    - ambil semua [data-gtm-job-id] dalam scope (container atau dokumen)
    """
    container = find_container_auto(driver, container_xpath if container_xpath else None)

    if container and not is_attached(driver, container):
        container = get_fresh_container(driver, container_xpath)

    # pastikan ada beberapa kartu dulu secara global
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-gtm-job-id]"))
        )
    except TimeoutException:
        return []

    # scroll list (di container/ancestor-nya) sampai tidak bertambah
    scroll_list_until_no_growth(driver, container, max_loops=100, min_growth=1)

    # kumpulkan kartu (scope = container kalau ada; else dokumen)
        # === KUMPULKAN KARTU: tahan stale dengan retry & refresh container ===
    def collect_cards_with_retry(retries=3):
        nonlocal container
        for attempt in range(retries):
            try:
                # kalau container copot, re-locate
                if container and not is_attached(driver, container):
                    container = get_fresh_container(driver, container_xpath)

                if container:
                    return container.find_elements(By.CSS_SELECTOR, "[data-gtm-job-id]")
                else:
                    return driver.find_elements(By.CSS_SELECTOR, "[data-gtm-job-id]")
            except StaleElementReferenceException:
                container = get_fresh_container(driver, container_xpath)
                time.sleep(0.2 + 0.2 * attempt)
            except Exception:
                break
        # hard fallback: pakai dokumen
        try:
            return driver.find_elements(By.CSS_SELECTOR, "[data-gtm-job-id]")
        except Exception:
            return []

    cards = collect_cards_with_retry()
    total = len(cards)
    print(f"[extract] total cards found: {total}")

    jobs: List[Job] = []
    seen = set()

    for idx in range(total):
        # ambil card awal dari list (bisa stale), siapkan retry
        card = cards[idx] if idx < len(cards) else None

        data = None
        for attempt in range(3):
            try:
                if card is None:
                    card = refetch_card_by_index(driver, container, idx)
                    if card is None:
                        break  # tidak ada elemen ke-idx lagi

                data = parse_job_card(card)
                break  # sukses
            except StaleElementReferenceException:
                # re-fetch lalu coba lagi
                card = refetch_card_by_index(driver, container, idx)
                time.sleep(0.1 + 0.1 * attempt)

        if not data:
            continue

        link = data.get("link", "")
        if not data.get("title") or not link or link in seen:
            continue

        seen.add(link)
        norm_locs = normalize_locations(data.get("locations", []))
        loc_str = ", ".join(norm_locs)

        jobs.append(
            Job(
                title=data.get("title", ""),
                company=data.get("company", ""),
                location=loc_str,
                salary=data.get("salary", ""),
                tags=data.get("tags", []),
                link=link,
                posted=data.get("updated_at", ""),
                keyword=keyword,
            )
        )
    return jobs

def extract_salary(card) -> str:
    """
    Ambil teks gaji dari berbagai kemungkinan selector.
    Urutan:
      1) elemen yg biasanya berisi nominal
      2) pesan 'Tidak Ditampilkan'
      3) fallback cari teks mengandung Rp/USD/jt
    """
    css_candidates_nominal = [
        "[data-testid='salary']",
        ".CompactOpportunityCardsc__SalaryWrapper-sc-dkg8my-32",
        "[class*='SalaryWrapper']",
        "[class*='Salary']",  # jaga-jaga kalau mereka rename
    ]
    for sel in css_candidates_nominal:
        try:
            el = card.find_element(By.CSS_SELECTOR, sel)
            txt = (el.text or "").strip()
            if txt and not re.search(r"tidak ditampilkan|not disclosed", txt, re.I):
                return txt
        except Exception:
            pass

    # Not disclosed
    undisclosed_candidates = [
        ".CompactOpportunityCardsc__NotDisclosedMessage-sc-dkg8my-27",
        "[class*='NotDisclosed']",
    ]
    for sel in undisclosed_candidates:
        try:
            el = card.find_element(By.CSS_SELECTOR, sel)
            txt = (el.text or "").strip()
            if txt:
                return txt
        except Exception:
            pass

    # Fallback: cari node yang mengandung pola mata uang / jt
    try:
        el = card.find_element(
            By.XPATH,
            ".//*[contains(normalize-space(.), 'Rp') or contains(normalize-space(.), 'USD') or contains(normalize-space(.), 'jt')]"
        )
        txt = (el.text or "").strip()
        if txt:
            return txt
    except Exception:
        pass

    return ""

def polite_sleep(a=0.75, b=1.35):
    time.sleep(random.uniform(a, b))

def parse_keywords(s: str | None) -> List[str]:
    """Terima string keyword (boleh dipisah koma atau baris), hasilkan list unik (preserve order)."""
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,\n]+", s) if p.strip()]
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def slugify(text: str) -> str:
    """Ubah teks jadi slug aman untuk nama file."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "jobs"

def ensure_parent_dir(path: str):
    """Pastikan folder tujuan ada sebelum menulis file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)

# ===================== WebDriver =====================
LOAD_TIMEOUT = 60

def init_webdriver(headless: bool = True, use_uc: bool = False) -> webdriver.Chrome:
    """
    Inisialisasi Chrome driver (Selenium 4). Bisa pilih undetected-chromedriver (uc) via --use-uc.
    """
    if use_uc:
        if not HAS_UC:
            raise RuntimeError("undetected-chromedriver belum terpasang. pip install undetected-chromedriver")
        opts = uc.ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        # tambahkan ini untuk stabilitas UC
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--start-maximized")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--disable-features=Translate")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1600,4000")
        opts.add_argument("--lang=id-ID,id")
        # UA opsional
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        driver = uc.Chrome(options=opts)
        driver.set_page_load_timeout(LOAD_TIMEOUT)
        return driver

    # Selenium biasa
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1600,4000")
    opts.add_argument("--lang=id-ID,id")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(LOAD_TIMEOUT)
    return driver

def try_accept_cookies(driver: webdriver.Chrome):
    labels = ["Terima", "Setuju", "Accept all", "Accept All", "Saya setuju", "Allow all"]
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for b in buttons:
            txt = (b.text or "").strip()
            if any(lbl.lower() in txt.lower() for lbl in labels):
                try:
                    b.click()
                    polite_sleep(0.5, 0.8)
                    return
                except Exception:
                    pass
    except Exception:
        pass

def wait_for_cards_count(driver: webdriver.Chrome, min_count=3, timeout=30) -> int:
    end = time.time() + timeout
    last = 0
    while time.time() < end:
        try:
            cnt = driver.execute_script(
                "return document.querySelectorAll(\"[data-gtm-job-id], [data-testid='opportunity-card']\").length;"
            )
        except WebDriverException:
            cnt = 0
        if cnt >= min_count:
            return cnt
        if cnt > last:
            last = cnt
            time.sleep(0.6)
        else:
            time.sleep(0.8)
    return last

def scroll_to_load(driver: webdriver.Chrome, max_scrolls: int = 30, min_growth: int = 1):
    last_count = driver.execute_script(
        "return document.querySelectorAll(\"[data-gtm-job-id], [data-testid='opportunity-card']\").length;"
    )
    stagnation = 0
    for i in range(max_scrolls):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        polite_sleep(0.9, 1.5)
        driver.execute_script("window.scrollBy(0, -300);")
        polite_sleep(0.25, 0.5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        polite_sleep(0.7, 1.1)

        new_count = driver.execute_script(
            "return document.querySelectorAll(\"[data-gtm-job-id], [data-testid='opportunity-card']\").length;"
        )
        print(f"[scroll {i+1}/{max_scrolls}] cards={new_count}")
        if new_count - last_count < min_growth:
            stagnation += 1
        else:
            stagnation = 0
        last_count = new_count
        if stagnation >= 3:
            break

# ===================== Extractors =====================
DEFAULT_CONTAINER_XPATH = "/html/body/div[2]/div/div[1]/div[2]/div[2]/div[2]/div[4]/div[2]/div[1]"

def get_text_safe(node, selector_css_list):
    for sel in selector_css_list:
        try:
            el = node.find_element(By.CSS_SELECTOR, sel)
            txt = (el.text or "").strip()
            if txt:
                return txt
        except Exception:
            pass
    return ""

def get_attr_safe(node, selector_css, attr):
    try:
        el = node.find_element(By.CSS_SELECTOR, selector_css)
        return el.get_attribute(attr) or ""
    except Exception:
        return ""

def normalize_locations(locs: List[str]) -> List[str]:
    """
    Bersihkan daftar lokasi:
    - pecah jika ada pemisah koma/·/slash
    - trim spasi dan koma berlebih
    - buang string kosong/placeholder
    - dedupe dengan menjaga urutan
    """
    seen = set()
    cleaned: List[str] = []
    for raw in locs or []:
        # fallback: kalau ada blok teks gabungan "Kecamatan, Kota, Provinsi"
        parts = re.split(r"\s*[·,/]\s*|\s*,\s*", raw)  # dukung koma, titik tengah, slash
        for p in parts:
            t = re.sub(r"\s+", " ", p).strip(" ,\u00A0")  # strip spasi & koma & non-breaking space
            if not t or t == "-" or t.lower() == "all cities/provinces":
                continue
            if t not in seen:
                seen.add(t)
                cleaned.append(t)
    return cleaned

def parse_job_card(card) -> Dict[str, Any]:
    """
    Snapshot isi card via JS agar kebal stale & selector lebih fleksibel.
    """
    driver = getattr(card, "_parent", None) or getattr(card, "parent", None)  # webdriver instance

    js = r"""
    const card = arguments[0];

    const textOf = (el) => el ? (el.innerText || el.textContent || "").trim() : "";
    const q = (sel, root=card) => root.querySelector(sel);
    const qAll = (sel, root=card) => Array.from(root.querySelectorAll(sel));

    // ===== Title & Link =====
    // Cari anchor ke detail job (selektor sangat longgar).
    let anchor = q("a[href*='/opportunities/jobs/']");
    // Fallback: kadang anchor di wrapper terluar
    if (!anchor) {
      const anchors = qAll("a");
      anchor = anchors.find(a => (a.getAttribute("href")||"").includes("/opportunities/jobs/")) || null;
    }

    const titleFromAnchor = textOf(anchor);
    const titleFromAria   = anchor ? (anchor.getAttribute("aria-label") || "") : "";
    const titleFromAttr   = card.getAttribute("data-gtm-job-role") || card.getAttribute("data-gtm-job-title") || "";
    const title = [titleFromAnchor, titleFromAria, titleFromAttr].find(t => t && t.length > 0) || "";

    let href = anchor ? (anchor.getAttribute("href") || "") : "";
    if (!href) {
      // beberapa layout menyimpan url di data-href
      href = card.getAttribute("data-href") || card.getAttribute("data-url") || "";
    }

    // ===== Company =====
    let company = "";
    const compEl = q("[data-cy='company_name_job_card'] a, [data-testid='company-name'] a, a[href*='/companies/']");
    if (compEl) company = textOf(compEl);

    // ===== Locations =====
    let locations = [];
    const locWrap = q("[data-testid='location'], .CardJobLocation__LocationWrapper-sc-v7ofa9-0, [class*='LocationWrapper']");
    if (locWrap) {
      const parts = qAll(".CardJobLocation__LocationSpan-sc-v7ofa9-1, span, a", locWrap).map(textOf).filter(Boolean);
      if (parts.length) {
        locations = parts;
      } else {
        const t = textOf(locWrap);
        if (t) locations = [t];
      }
    }

    // ===== Salary =====
    let salary = "";
    // Cari elemen salary yang jelas
    const sal1 = q("[data-testid='salary'], [class*='SalaryWrapper'], [class*='Salary']");
    if (sal1) {
    salary = textOf(sal1);
    }
    if (!salary) {
    const notD = q("[class*='NotDisclosed']");
    if (notD) salary = textOf(notD);
    }

    // Hapus kasus kalau salary kebawa title
    if (salary && salary.toLowerCase().includes(title.toLowerCase())) {
    salary = salary.replace(title, "").trim();
    }

    // ===== Tags =====
    const tags = qAll(".CompactOpportunityCardsc__TagsWrapper-sc-dkg8my-37 .TagStyle__TagContentWrapper-sc-r1wv7a-1, [data-testid='job-tag']")
      .map(textOf).filter(Boolean);

    // ===== Updated / Meta =====
    const updated = textOf(q(".CompactOpportunityCardsc__UpdatedAtMessage-sc-dkg8my-26, [data-testid='updated-at']"));

    // ===== Logo (opsional) =====
    let logo = "";
    const img = q("img[alt]");
    if (img) logo = img.getAttribute("src") || "";

    const aktif = /aktif merekrut/i.test(textOf(card));

    return {
      job_id: card.getAttribute("data-gtm-job-id") || "",
      job_role: card.getAttribute("data-gtm-job-role") || "",
      job_type: card.getAttribute("data-gtm-job-type") || "",
      job_cat: card.getAttribute("data-gtm-job-category") || "",
      job_sub_cat: card.getAttribute("data-gtm-job-sub-category") || "",
      company_id: card.getAttribute("data-gtm-job-company-id") || "",
      is_hot_job: (card.getAttribute("data-gtm-is-hot-job") || "").toLowerCase() === "true",
      title: title,
      link: href || "",
      company: company,
      locations: locations,
      salary: salary,
      tags: tags,
      aktif_merekrut: aktif,
      updated_at: updated,
      company_logo: logo
    };
    """

    try:
        data = driver.execute_script(js, card) or {}
    except Exception:
        # Fallback super-minimal jika JS error
        data = {
            "job_id": card.get_attribute("data-gtm-job-id") or "",
            "title": card.get_attribute("data-gtm-job-role") or "",
            "link": "",
            "company": "",
            "locations": [],
            "salary": "",
            "tags": [],
            "updated_at": "",
            "company_logo": "",
        }

    # Bersihkan lokasi dengan normalizer Python
    data["locations"] = normalize_locations(data.get("locations", []))
    return data

@dataclass
class Job:
    title: str
    company: str
    location: str
    salary: str
    tags: List[str]
    link: str
    posted: str
    source: str = "glints"
    keyword: str = ""

@dataclass
class EnrichedJob(Job):
    cluster: str = ""
    category: str = ""
    seniority: str = ""
    work_mode: str = ""   # remote/onsite/hybrid/unknown
    languages: List[str] = None
    confidence: float = 0.0

def scrape_current_page(driver: webdriver.Chrome, container_xpath: str, keyword: str) -> List[Job]:
    """Asumsikan halaman glints untuk keyword ini SUDAH TERBUKA di driver.current_window_handle."""
    try_accept_cookies(driver)
    _ = wait_for_cards_count(driver, min_count=2, timeout=20)
    jobs = extract_jobs_from_container(driver, container_xpath, keyword)
    print(f"[result] jobs parsed: {len(jobs)}")
    return jobs

def open_tab_and_scrape(driver: webdriver.Chrome, url: str, container_xpath: str, keyword: str, close_tab_after=True) -> List[Job]:
    """Buka TAB BARU untuk url, scrape, lalu (opsional) tutup tab."""
    # buka tab baru
    driver.execute_script(f"window.open({json.dumps(url)}, '_blank');")
    new_handle = driver.window_handles[-1]
    driver.switch_to.window(new_handle)

    # load selesai (simple wait; glints heavy SPA jadi beri sleep kecil)
    try:
        driver.get(url)  # jaga-jaga kalau open() tidak langsung load
    except Exception:
        pass
    polite_sleep(1.0, 1.6)

    try:
        jobs = scrape_current_page(driver, container_xpath, keyword)
        return jobs
    finally:
        if close_tab_after:
            try:
                driver.close()
            except Exception:
                pass
            # balik ke tab awal jika masih ada
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
     
# ===================== Gemini Grouping =====================
GEMINI_SYSTEM = (
    "You are a job-intelligence assistant. Given a job title, company, location, and optional tags/salary, "
    "return a JSON with fields: cluster, category, seniority, work_mode (remote/onsite/hybrid/unknown), "
    "languages (array of strings), and confidence (0-1). Use concise, consistent cluster names "
    "(e.g., 'Social Media', 'Content', 'Graphic Design', 'Data', 'Sales', 'Customer Support', 'Engineering')."
)
GEMINI_INSTRUCTION = (
    "Return ONLY valid JSON with these keys: cluster, category, seniority, work_mode, languages, confidence."
)

def configure_gemini():
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not found. Buat .env dan set GEMINI_API_KEY=xxx")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    return model

def classify_with_gemini(model, job: Job, retries: int = 3, backoff: float = 1.5) -> Dict[str, Any]:
    prompt = (
        f"{GEMINI_SYSTEM}\n\n"
        f"TITLE: {job.title}\n"
        f"COMPANY: {job.company or '-'}\n"
        f"LOCATION: {job.location or '-'}\n"
        f"SALARY: {job.salary or '-'}\n"
        f"TAGS: {', '.join(job.tags) if job.tags else '-'}\n\n"
        f"{GEMINI_INSTRUCTION}"
    )
    for attempt in range(retries):
        try:
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                text = m.group(0)
            data = json.loads(text)
            data.setdefault("cluster", "Unknown")
            data.setdefault("category", "Unknown")
            data.setdefault("seniority", "Unknown")
            data.setdefault("work_mode", "unknown")
            data.setdefault("languages", [])
            data.setdefault("confidence", 0.5)
            if not isinstance(data.get("languages"), list):
                data["languages"] = [str(data.get("languages"))]
            data["confidence"] = float(data.get("confidence", 0.5))
            return data
        except Exception as e:
            if attempt == retries - 1:
                return {
                    "cluster": "Unknown",
                    "category": "Unknown",
                    "seniority": "Unknown",
                    "work_mode": "unknown",
                    "languages": [],
                    "confidence": 0.0,
                    "_err": str(e),
                }
            time.sleep(backoff * (attempt + 1))

def enrich_jobs_with_gemini(jobs: List[Job]) -> List[EnrichedJob]:
    model = configure_gemini()
    enriched: List[EnrichedJob] = []
    for j in jobs:
        info = classify_with_gemini(model, j)
        enriched.append(
            EnrichedJob(
                **asdict(j),
                cluster=info.get("cluster", "Unknown"),
                category=info.get("category", "Unknown"),
                seniority=info.get("seniority", "Unknown"),
                work_mode=info.get("work_mode", "unknown"),
                languages=info.get("languages", []),
                confidence=info.get("confidence", 0.0),
            )
        )
        polite_sleep(0.2, 0.45)
    return enriched

# ===================== Output =====================
def to_csv(items: List[EnrichedJob], path: str):
    ensure_parent_dir(path)
    fieldnames = list(asdict(items[0]).keys()) if items else [
        "title","company","location","salary","tags","link","posted","source","keyword",
        "cluster","category","seniority","work_mode","languages","confidence"
    ]
    # UTF-8 with BOM agar Excel Windows baca benar; lineterminator "\n" biar tidak ada baris kosong
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n"
        )
        writer.writeheader()
        for it in items:
            row = asdict(it)
            # Bersihkan semua string & join list; juga normalkan gaji & link
            for k, v in list(row.items()):
                if k == "salary":
                    row[k] = clean_salary(v)
                elif k == "link":
                    row[k] = absolutize_link(v)
                elif k in ("tags", "languages"):
                    row[k] = join_list(v)
                elif isinstance(v, str):
                    row[k] = flatten_ws(v)
            writer.writerow(row)

def to_jsonl(items: List[EnrichedJob], path: str):
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            row = asdict(it)
            row["title"]    = flatten_ws(row.get("title", ""))
            row["company"]  = flatten_ws(row.get("company", ""))
            row["location"] = flatten_ws(row.get("location", ""))
            row["salary"] = clean_salary(row.get("salary", ""), row.get("title", ""))
            row["link"]     = absolutize_link(row.get("link", ""))
            if isinstance(row.get("tags"), list):
                row["tags"] = [flatten_ws(t) for t in row["tags"]]
            if isinstance(row.get("languages"), list):
                row["languages"] = [flatten_ws(t) for t in row["languages"]]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def print_summary(items: List[EnrichedJob]):
    from collections import Counter
    clusters = Counter([i.cluster for i in items])
    print("\n=== SUMMARY ===")
    print(f"Total jobs: {len(items)}")
    for k, v in clusters.most_common(10):
        print(f"  - {k}: {v}")

# ===================== CLI =====================
def main():
    parser = argparse.ArgumentParser(description="Scrape Glints (live DOM) + grouping dengan Gemini 2.5 Flash [multi-keyword = new tab per keyword]")
    parser.add_argument("--keyword", help='Satu atau banyak keyword dipisah koma, mis: "admin, social media"')
    parser.add_argument("--keywords", help="Alternatif: daftar keyword (koma/baris). Diabaikan jika --keyword ada.")
    parser.add_argument("--country", default="ID", help="Kode negara (default: ID)")
    parser.add_argument("--max-scrolls", type=int, default=30, help="(Tidak dipakai lagi untuk window scroll global; tetap dipakai di internal scroll list)")
    parser.add_argument("--headless", action="store_true", help="Jalankan headless")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Jalankan dengan browser terlihat")
    parser.set_defaults(headless=True)
    parser.add_argument("--use-uc", action="store_true", help="Gunakan undetected-chromedriver (butuh pip install)")
    parser.set_defaults(use_uc=True)
    parser.add_argument("--container-xpath", default=DEFAULT_CONTAINER_XPATH, help="XPath container list job")
    parser.add_argument("--out", default="jobs", help="Prefix nama file output (boleh folder/prefix)")
    parser.add_argument("--ai", action="store_true", help="Aktifkan pengelompokan dengan Gemini 2.5 Flash (default: mati)")
    parser.add_argument("--cookies", help=("Path ke file cookies (JSON/JSONL/Netscape cookies.txt) atau string header 'name=value; name2=value2'. ""Akan diterapkan ke glints.com sebelum scraping."),)
    parser.add_argument("--keep-tabs", action="store_true", help="Tidak tutup tab setelah selesai scrape (debugging manual).")
    args = parser.parse_args()

    # Resolve keywords
    kw_raw = args.keyword if args.keyword else args.keywords
    keywords = parse_keywords(kw_raw)
    if not keywords:
        parser.error("Harus isi --keyword atau --keywords (bisa dipisah koma atau baris).")

    # === 1 Driver untuk semua keyword ===
    driver = init_webdriver(headless=args.headless, use_uc=args.use_uc)
    try:
        # buka glints root untuk injeksi cookies (sekali di awal)
        try:
            driver.get("https://glints.com/")
        except Exception:
            pass
        if args.cookies:
            cookies = load_cookies_arg(args.cookies)
            if cookies:
                inject_cookies(driver, cookies, base_url="https://glints.com/")

        polite_sleep(0.8, 1.2)

        all_summaries = []
        for kw in keywords:
            url = GLINTS_BASE_URL.format(keyword=kw.replace(" ", "+"), country=args.country)
            print(f"\n=== Keyword: \"{kw}\" → buka tab baru ===")
            if args.keep_tabs:
                jobs = open_tab_and_scrape(
                    driver=driver,
                    url=url,
                    container_xpath=args.container_xpath,
                    keyword=kw,
                    close_tab_after=(not args.keep_tabs),
                )
            else:
                jobs = open_tab_and_scrape(
                    driver=driver,
                    url=url,
                    container_xpath=args.container_xpath,
                    keyword=kw
                )


            if not jobs:
                print(f"[SKIP] Tidak ada job ter-parse untuk: {kw}")
                continue

            if args.ai:
                print("[AI] Grouping dengan Gemini 2.5 Flash…")
                items = enrich_jobs_with_gemini(jobs)
            else:
                items = [EnrichedJob(**asdict(j)) for j in jobs]

            slug = slugify(kw)
            out_prefix = args.out
            csv_path = f"{out_prefix}_{slug}.csv"
            jsonl_path = f"{out_prefix}_{slug}.jsonl"

            to_csv(items, csv_path)
            to_jsonl(items, jsonl_path)
            print(f"[DONE] {csv_path}, {jsonl_path}")
            print_summary(items)

            all_summaries.append((kw, len(items)))

        # Ringkasan simpel (tanpa “batch” wording)
        if len(all_summaries) > 1:
            total = sum(n for _, n in all_summaries)
            print("\n=== RINGKASAN ===")
            for kw, n in all_summaries:
                print(f'  - "{kw}": {n} item')
            print(f"TOTAL: {total} item")

    finally:
        # kalau keep-tabs aktif, biarkan driver terbuka untuk inspeksi; kalau headless, tutup
        if not args.keep_tabs:
            try:
                driver.quit()
            except Exception:
                pass

if __name__ == "__main__":
    main()
