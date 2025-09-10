# Glints Scraper + Gemini Clustering

Scraper lowongan **Glints** yang bekerja pada **live DOM** menggunakan Selenium (dengan dukungan **undetected-chromedriver**), lengkap dengan auto-scroll untuk virtualized list, ekstraksi field stabil dari atribut & selector yang lentur, injeksi cookies, dan output **CSV + JSONL**.  
Opsional: **pengelompokan (clustering) lowongan** menggunakan **Gemini 2.5 Flash** untuk menghasilkan bidang: `cluster`, `category`, `seniority`, `work_mode`, `languages`, `confidence`.

> **Default AI: OFF.** Aktifkan dengan flag `--ai` saat menjalankan skrip.

---

## Daftar Isi

- [Fitur](#fitur)
- [Prasyarat](#prasyarat)
- [Instalasi](#instalasi)
- [Konfigurasi Gemini (opsional)](#konfigurasi-gemini-opsional)
- [Penggunaan Cepat](#penggunaan-cepat)
- [Semua Argumen CLI](#semua-argumen-cli)
- [Contoh Injeksi Cookies](#contoh-injeksi-cookies)
- [Output & Skema Kolom](#output--skema-kolom)
- [Tips & Troubleshooting](#tips--troubleshooting)
- [Catatan Etika & Legal](#catatan-etika--legal)
- [Lisensi](#lisensi)

---

## Fitur

- **Live DOM scraping**: menunggu elemen muncul dan menggulir **ancestor** yang benar-benar scrollable agar item baru dirender (bukan sekadar `window.scrollTo`).
- **Deteksi container otomatis**: bisa pakai `--container-xpath` atau biarkan skrip memilih container terbaik.
- **Ekstraksi robust**: `title`, `link`, `company`, `locations`, `salary`, `tags`, `updated_at`, `company_logo`.
- **Normalisasi data**: `clean_salary`, `normalize_locations`, pembersihan whitespace, absolutisasi URL.
- **Stale-proof**: retry & re-fetch elemen kartu berdasarkan index bila terjadi `StaleElementReferenceException`.
- **Injeksi cookies**: mendukung **JSON**, **JSONL**, **Netscape cookies.txt**, dan **header string**.
- **Output**: **CSV** (UTF‑8 BOM; aman untuk Excel Windows) dan **JSONL** per keyword.
- **AI Clustering (opsional)**: **Gemini 2.5 Flash** untuk `cluster`, `category`, `seniority`, `work_mode`, `languages`, `confidence`.  
  → **AI default OFF**, aktifkan dengan `--ai`.

---

## Prasyarat

- **Python** 3.10 atau lebih baru (disarankan).
- **Google Chrome** terpasang.
- Paket Python:
  - `selenium`
  - `webdriver-manager`
  - `undetected-chromedriver` (opsional, tapi disarankan)
  - `python-dotenv`
  - `google-generativeai` (hanya bila memakai AI clustering)
- Koneksi internet dan izin scraping yang sesuai.

---

## Instalasi

```bash
# 1) Clone repo
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

# 2) (Opsional) Buat dan aktifkan virtualenv
python -m venv .venv
# Windows:
. .venv/Scripts/activate
# macOS/Linux:
source .venv/bin/activate

# 3) Install dependencies
pip install -U pip
pip install selenium webdriver-manager undetected-chromedriver python-dotenv google-generativeai
```

> **Windows tip:** kode sudah mematikan destructor UC untuk mencegah `OSError` saat `driver.quit()`.

---

## Konfigurasi Gemini (opsional)

1. Buat file **`.env`** pada root repo.
2. Isi API key Anda:
   ```env
   GEMINI_API_KEY=YOUR_API_KEY_HERE
   ```
3. Jalankan skrip dengan flag `--ai` untuk mengaktifkan clustering.

Tanpa file `.env` ini, jalankan saja **tanpa** flag `--ai` (AI OFF).

---

## Penggunaan Cepat

**Scraping saja (AI OFF — default):**
```bash
python glints_scrape_gemini.py --keyword "admin" --no-headless --use-uc --out jobs_admin
```

**Scraping + Pengelompokan AI (AI ON):**
```bash
python glints_scrape_gemini.py --keyword "social media" --ai --no-headless --use-uc --out jobs_socmed_ai
# atau
python glints_scrape_gemini.py --keyword "social media" --ai --no-headless --use-uc --out jobs_socmed_ai
```

**Multiple keyword (pisah koma / baris):**
```bash
python glints_scrape_gemini.py --keywords "admin, social media, designer" --ai --out hasil/lowongan
```

**Negara lain:**
```bash
python glints_scrape_gemini.py --keyword "designer" --country ID --ai
```

---

## Semua Argumen CLI

| Argumen | Default / Tipe | Deskripsi |
| --- | --- | --- |
| `--keyword` | `str` | Satu keyword pencarian. |
| `--keywords` | `str` | Banyak keyword dipisah koma / baris. Diabaikan jika `--keyword` diisi. |
| `--country` | `ID` | Kode negara. |
| `--max-scrolls` | `30` | Batas loop scroll untuk memicu render item tambahan. |
| `--headless` / `--no-headless` | `headless=True` | Mode headless atau terlihat. |
| `--use-uc` | **ON** | Gunakan **undetected-chromedriver** (disarankan). |
| `--container-xpath` | preset default | XPath container list job (opsional, auto-detect jika gagal). |
| `--out` | `jobs` | Prefix output; file jadi `<out>_<slug>.csv` & `<out>_<slug>.jsonl`. |
| `--ai` | `False` | Aktifkan pengelompokan AI (Gemini 2.5 Flash). **Default: OFF**. |
| `--cookies` | path / header | Injeksi cookies sebelum scraping; dukung JSON / JSONL / Netscape / header string. |

---

## Contoh Injeksi Cookies

**JSON array (`cookies.json`)**
```json
[
  {"name":"_gid","value":"GA1.2.x.y","domain":"glints.com","path":"/","secure":false,"expiry":1750000000},
  {"name":"sessionid","value":"abc123","domain":"glints.com","path":"/","secure":true}
]
```
Jalankan:
```bash
python glints_scrape_gemini.py --keyword "admin" --cookies cookies.json
```

**JSONL**
```
{"name":"_gid","value":"GA1.2.x.y","domain":"glints.com","path":"/"}
{"name":"sessionid","value":"abc123","domain":"glints.com","path":"/","secure":true}
```

**Netscape `cookies.txt`**
```
.glints.com	TRUE	/	TRUE	1750000000	sessionid	abc123
```

**Header string**
```bash
python glints_scrape_gemini.py --keyword "admin" --cookies "sessionid=abc123; _gid=GA1.2.x.y"
```

> Skrip akan menormalkan field cookies (expiry, secure, sameSite, dll) dan refresh halaman setelah injeksi.

---

## Output & Skema Kolom

**CSV** (UTF‑8 dengan BOM) dan **JSONL** per keyword.

Kolom dasar (selalu ada, hasil scraping):
- `title`, `company`, `location`, `salary`, `tags`, `link`, `posted`, `source`, `keyword`

Kolom tambahan (muncul bila AI aktif):
- `cluster`, `category`, `seniority`, `work_mode` (`remote`/`onsite`/`hybrid`/`unknown`), `languages` (array), `confidence` (0–1)

Contoh nama file untuk keyword `"social media"` dengan `--out jobs`:
```
jobs_social-media.csv
jobs_social-media.jsonl
```

---

## Tips & Troubleshooting

- **AI tidak aktif padahal ingin pakai?**  
  Pastikan `.env` berisi `GEMINI_API_KEY` dan jalankan dengan `--ai`.

- **UC/driver error atau halaman kosong:**  
  - Perbarui Chrome & `undetected-chromedriver`: `pip install -U undetected-chromedriver`  
  - Coba **tanpa headless** untuk inspeksi UI.  
  - Skrip fallback ke driver Selenium biasa bila UC gagal saat inisialisasi.

- **`TimeoutException` (kartu tidak muncul):**  
  Tambah `--max-scrolls`, pastikan koneksi stabil, pertimbangkan injeksi cookies (login/consent).

- **`StaleElementReferenceException`:**  
  Sudah ditangani: re-fetch elemen berdasarkan indeks + retry.

- **CSV garis kosong/encoding kacau di Excel:**  
  CSV di-set `lineterminator="\\n"` dan `encoding="utf-8-sig"`. Hindari editor yang mengubah encoding.

- **Lokasi/gaji tidak rapi:**  
  Sudah dinormalisasi, tapi markup situs bisa berubah. Laporkan/ubah selector bila perlu.

---

## Catatan Etika & Legal

Gunakan skrip ini sesuai **Terms of Service** Glints, hukum setempat, dan etika scraping. Hormati `robots.txt`, pertimbangkan rate limit & cache, dan jangan menyalahgunakan data personal.

---

## Lisensi

MIT. Lihat berkas `LICENSE` bila tersedia.  
**Glints** adalah merek dan milik pemegang haknya masing-masing; proyek ini tidak berafiliasi dengan Glints.
