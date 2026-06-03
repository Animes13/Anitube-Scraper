# -*- coding: utf-8 -*-

"""
Backup renderizado com Chromium/Chrome + Selenium.

O que faz:
- Abre URLs com Chromium/Chrome.
- Espera a página carregar.
- Salva HTML renderizado.
- Salva screenshot.
- Extrai links internos do mesmo domínio.
- Cria índice JSON.
- Funciona melhor no GitHub Actions/Linux.
- No Termux/Android puro, Selenium local não funciona bem sem navegador remoto.
- Não tenta burlar captcha, fingerprint ou proteção anti-bot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


BASE_URL = "https://anitube.vip"

BACKUP_DIR = Path("backup")
HTML_DIR = BACKUP_DIR / "html"
SHOT_DIR = BACKUP_DIR / "screenshots"
DB_DIR = BACKUP_DIR / "database"
LOG_DIR = Path("logs")

for folder in [HTML_DIR, SHOT_DIR, DB_DIR, LOG_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


@dataclass
class PageBackup:
    url: str
    final_url: str | None
    title: str | None
    status: str
    html_file: str | None
    screenshot_file: str | None
    links_found: int
    error: str | None
    backed_up_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    url = url.strip()
    url, _frag = urldefrag(url)
    return url.rstrip("/")


def same_domain(url: str, base: str = BASE_URL) -> bool:
    try:
        u = urlparse(url)
        b = urlparse(base)

        if not u.netloc:
            return False

        return u.netloc == b.netloc or u.netloc.endswith("." + b.netloc)
    except Exception:
        return False


def safe_filename_from_url(url: str, ext: str) -> str:
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}"

    if parsed.query:
        raw += "_" + parsed.query

    raw = raw.strip("/") or "index"
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)
    raw = raw[:160]

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{raw}_{digest}.{ext}"


def read_seed_urls(path: Path) -> list[str]:
    if not path.exists():
        return [BASE_URL]

    urls: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        urls.append(normalize_url(line))

    return urls or [BASE_URL]


def save_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def path_exists(path: str | None) -> bool:
    if not path:
        return False

    return Path(path).exists()


def find_chrome_binary() -> str | None:
    """
    Procura Chrome/Chromium em caminhos comuns do Linux/GitHub Actions.
    Só retorna caminho existente.
    """

    env_binary = os.environ.get("CHROME_BINARY")

    if path_exists(env_binary):
        return env_binary

    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
    ]

    for candidate in candidates:
        if path_exists(candidate):
            return candidate

    return None


def find_chromedriver() -> str | None:
    """
    Procura ChromeDriver em caminhos comuns.
    Só retorna caminho existente.
    """

    env_driver = os.environ.get("CHROMEDRIVER")

    if path_exists(env_driver):
        return env_driver

    candidates = [
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]

    for candidate in candidates:
        if path_exists(candidate):
            return candidate

    return None


def create_driver(
    headless: bool = True,
    page_load_timeout: int = 45,
    remote_url: str | None = None,
) -> webdriver.Chrome:
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1366,768")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")

    # Identificação clara do crawler.
    options.add_argument(
        "--user-agent=AnitubeBackupBot/1.0 "
        "(authorized backup; contact: owner@anitube.vip)"
    )

    chrome_binary = find_chrome_binary()

    if chrome_binary:
        print(f"[CHROME] Usando navegador: {chrome_binary}")
        options.binary_location = chrome_binary
    else:
        print("[CHROME] Nenhum Chrome/Chromium encontrado manualmente. Selenium tentará resolver.")

    if remote_url:
        print(f"[REMOTE] Usando Selenium remoto: {remote_url}")
        driver = webdriver.Remote(
            command_executor=remote_url,
            options=options,
        )
    else:
        chromedriver = find_chromedriver()

        if chromedriver:
            print(f"[DRIVER] Usando ChromeDriver: {chromedriver}")
            driver = webdriver.Chrome(
                service=Service(chromedriver),
                options=options,
            )
        else:
            print("[DRIVER] ChromeDriver não informado. Selenium Manager tentará resolver.")
            driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(page_load_timeout)
    return driver


def wait_basic_page_ready(driver: webdriver.Chrome, wait_seconds: float) -> None:
    """
    Espera simples, sem tentar resolver desafios/captcha.
    """
    time.sleep(wait_seconds)

    try:
        ready_state = driver.execute_script("return document.readyState")
        print(f"[READY] document.readyState = {ready_state}")
    except Exception:
        pass


def extract_internal_links(html: str, current_url: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href", "")).strip()

        if not href:
            continue

        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        absolute = normalize_url(urljoin(current_url, href))

        if same_domain(absolute, base_url):
            links.add(absolute)

    return sorted(links)


def looks_like_block_or_captcha(html: str, title: str | None) -> bool:
    """
    Detecta bloqueio para registrar no log.
    Não tenta contornar.
    """
    text = " ".join([title or "", html[:5000]]).lower()

    markers = [
        "captcha",
        "verify you are human",
        "checking your browser",
        "access denied",
        "forbidden",
        "cloudflare",
        "challenge",
        "attention required",
    ]

    return any(marker in text for marker in markers)


def backup_one_page(
    driver: webdriver.Chrome,
    url: str,
    base_url: str,
    wait_seconds: float,
    screenshot: bool,
) -> tuple[PageBackup, list[str]]:
    html_file = None
    screenshot_file = None

    try:
        print(f"[OPEN] {url}")

        driver.get(url)
        wait_basic_page_ready(driver, wait_seconds)

        final_url = normalize_url(driver.current_url)
        title = driver.title
        html = driver.page_source or ""

        if looks_like_block_or_captcha(html, title):
            html_name = safe_filename_from_url(final_url, "blocked.html")
            html_path = HTML_DIR / html_name
            html_path.write_text(html, encoding="utf-8")
            html_file = str(html_path)

            if screenshot:
                shot_name = safe_filename_from_url(final_url, "blocked.png")
                shot_path = SHOT_DIR / shot_name
                driver.save_screenshot(str(shot_path))
                screenshot_file = str(shot_path)

            record = PageBackup(
                url=url,
                final_url=final_url,
                title=title,
                status="blocked_or_captcha_detected",
                html_file=html_file,
                screenshot_file=screenshot_file,
                links_found=0,
                error="Página parece conter captcha, challenge ou bloqueio. Não foi tentado contornar.",
                backed_up_at=now_iso(),
            )

            return record, []

        html_name = safe_filename_from_url(final_url, "html")
        html_path = HTML_DIR / html_name
        html_path.write_text(html, encoding="utf-8")
        html_file = str(html_path)

        if screenshot:
            shot_name = safe_filename_from_url(final_url, "png")
            shot_path = SHOT_DIR / shot_name
            driver.save_screenshot(str(shot_path))
            screenshot_file = str(shot_path)

        links = extract_internal_links(html, final_url, base_url)

        record = PageBackup(
            url=url,
            final_url=final_url,
            title=title,
            status="ok",
            html_file=html_file,
            screenshot_file=screenshot_file,
            links_found=len(links),
            error=None,
            backed_up_at=now_iso(),
        )

        return record, links

    except TimeoutException as e:
        return PageBackup(
            url=url,
            final_url=None,
            title=None,
            status="timeout",
            html_file=html_file,
            screenshot_file=screenshot_file,
            links_found=0,
            error=str(e),
            backed_up_at=now_iso(),
        ), []

    except WebDriverException as e:
        return PageBackup(
            url=url,
            final_url=None,
            title=None,
            status="webdriver_error",
            html_file=html_file,
            screenshot_file=screenshot_file,
            links_found=0,
            error=str(e),
            backed_up_at=now_iso(),
        ), []

    except Exception as e:
        return PageBackup(
            url=url,
            final_url=None,
            title=None,
            status="error",
            html_file=html_file,
            screenshot_file=screenshot_file,
            links_found=0,
            error=str(e),
            backed_up_at=now_iso(),
        ), []


def crawl(
    seed_urls: Iterable[str],
    base_url: str,
    max_pages: int,
    max_depth: int,
    delay_min: float,
    delay_max: float,
    wait_seconds: float,
    headless: bool,
    screenshot: bool,
    remote_url: str | None = None,
) -> None:
    queue: list[tuple[str, int]] = [(normalize_url(u), 0) for u in seed_urls]
    seen: set[str] = set()
    queued: set[str] = {normalize_url(u) for u in seed_urls}
    results: list[PageBackup] = []

    driver = create_driver(
        headless=headless,
        remote_url=remote_url,
    )

    try:
        while queue and len(seen) < max_pages:
            url, depth = queue.pop(0)
            url = normalize_url(url)

            if url in seen:
                continue

            if not same_domain(url, base_url):
                continue

            seen.add(url)

            record, links = backup_one_page(
                driver=driver,
                url=url,
                base_url=base_url,
                wait_seconds=wait_seconds,
                screenshot=screenshot,
            )

            results.append(record)

            print(f"[{record.status}] {url} | links: {record.links_found}")

            if depth < max_depth and record.status == "ok":
                for link in links:
                    link = normalize_url(link)

                    if link not in seen and link not in queued:
                        queue.append((link, depth + 1))
                        queued.add(link)

            delay = random.uniform(delay_min, delay_max)
            print(f"[WAIT] {delay:.1f}s")
            time.sleep(delay)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    index = {
        "site": base_url,
        "generated_at": now_iso(),
        "max_pages": max_pages,
        "max_depth": max_depth,
        "total_processed": len(results),
        "total_ok": sum(1 for r in results if r.status == "ok"),
        "total_errors": sum(1 for r in results if r.status != "ok"),
        "pages": [asdict(r) for r in results],
    }

    save_json(DB_DIR / "backup_index.json", index)

    errors = [asdict(r) for r in results if r.status != "ok"]
    save_json(DB_DIR / "errors.json", errors)

    discovered = sorted({r.final_url for r in results if r.final_url})
    save_json(DB_DIR / "discovered_urls.json", discovered)

    print("\nResumo:")
    print("Processadas:", index["total_processed"])
    print("OK:", index["total_ok"])
    print("Erros/bloqueios:", index["total_errors"])
    print("Índice:", DB_DIR / "backup_index.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backup com Chromium/Chrome + Selenium.")

    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--urls-file", default="urls.txt")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--delay-min", type=float, default=3.0)
    parser.add_argument("--delay-max", type=float, default=8.0)
    parser.add_argument("--wait", type=float, default=5.0)
    parser.add_argument("--remote-url", default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--no-screenshot", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.delay_min < 0 or args.delay_max < args.delay_min:
        print("Erro: delay inválido.")
        return 2

    seed_urls = read_seed_urls(Path(args.urls_file))

    crawl(
        seed_urls=seed_urls,
        base_url=args.base_url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        wait_seconds=args.wait,
        headless=not args.no_headless,
        screenshot=not args.no_screenshot,
        remote_url=args.remote_url,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
