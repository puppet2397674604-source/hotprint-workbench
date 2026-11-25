"""Real headless Google Chrome acceptance verification via Chrome DevTools Protocol (Python stdlib).

Verifies 1440px desktop, 768px tablet, and 390px mobile rendering, zero JS errors,
zero page-level horizontal overflow, interactive category switching, product drawer,
keyboard Escape handling, manual collection dry-run flow, and captures PNG screenshots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

WORKBENCH_DIR = Path(__file__).resolve().parents[1]
if str(WORKBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_DIR))

from backend.config import DEFAULT_DB_PATH
from backend.server import create_server

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCREENSHOT_DIR = WORKBENCH_DIR / "artifacts" / "screenshots"


class SimpleCDPClient:
    """Minimal RFC 6455 WebSocket client over stdlib socket for Chrome DevTools Protocol."""

    def __init__(self, host: str, port: int, path: str):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.settimeout(15)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP handshake failed")
            buf += chunk
        self._recv_buf = buf.split(b"\r\n\r\n", 1)[1]
        self._msg_id = 0
        self.exceptions = []

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_exact(self, n: int) -> bytes:
        while len(self._recv_buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("CDP socket closed")
            self._recv_buf += chunk
        out = self._recv_buf[:n]
        self._recv_buf = self._recv_buf[n:]
        return out

    def _recv_frame(self) -> str:
        chunks = []
        while True:
            hdr = self._read_exact(2)
            fin = (hdr[0] & 0x80) != 0
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise EOFError("CDP closed")
            if opcode == 0x9:
                # Pong
                self._send_raw(0xA, payload)
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def _send_raw(self, opcode: int, data: bytes):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked_payload)

    def send(self, method: str, params: dict | None = None) -> dict:
        self._msg_id += 1
        mid = self._msg_id
        msg = json.dumps({"id": mid, "method": method, "params": params or {}}).encode("utf-8")
        self._send_raw(0x1, msg)
        while True:
            raw = self._recv_frame()
            obj = json.loads(raw)
            if obj.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(obj.get("params"))
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError(f"CDP error for {method}: {obj['error']}")
                return obj.get("result", {})

    def eval_js(self, expression: str):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS eval exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    def set_viewport(self, width: int, height: int, mobile: bool = False):
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 2 if not mobile else 2,
                "mobile": mobile,
            },
        )

    def screenshot(self, path: Path, full_page: bool = False):
        params = {"format": "png", "captureBeyondViewport": full_page}
        if full_page:
            metrics = self.send("Page.getLayoutMetrics")
            content = metrics.get("contentSize", {})
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": content.get("width", 1440),
                "height": min(content.get("height", 2400), 3400),
                "scale": 1,
            }
        res = self.send("Page.captureScreenshot", params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(res["data"]))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cdp: SimpleCDPClient, js_cond: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval_js(js_cond):
            return True
        time.sleep(0.15)
    raise TimeoutError(f"Timed out waiting for condition: {js_cond}")


def main() -> int:
    if not Path(CHROME_BIN).exists():
        print("Google Chrome not found; cannot run real browser verification.", file=sys.stderr)
        return 2

    if not DEFAULT_DB_PATH.exists():
        print(f"Real DB not found at {DEFAULT_DB_PATH}", file=sys.stderr)
        return 2

    db_hash_before = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
    temp_app_db_dir = tempfile.mkdtemp(prefix="hotprint-appdb-")
    temp_app_db = Path(temp_app_db_dir) / "preprocessing.sqlite3"
    temp_key_file = Path(temp_app_db_dir) / "api_key.local"
    temp_ai_cfg_file = Path(temp_app_db_dir) / "ai_config.local"
    temp_sb_cfg_file = Path(temp_app_db_dir) / "supabase_config.local"

    # Start workbench server on port 8772 (fallback to ephemeral if busy)
    port = 8772
    try:
        server, _ = create_server(
            host="127.0.0.1",
            port=port,
            db_path=DEFAULT_DB_PATH,
            app_db_path=temp_app_db,
            workbench_api_key_path=temp_key_file,
            ai_config_path=temp_ai_cfg_file,
            supabase_config_path=temp_sb_cfg_file,
        )
    except OSError:
        port = find_free_port()
        server, _ = create_server(
            host="127.0.0.1",
            port=port,
            db_path=DEFAULT_DB_PATH,
            app_db_path=temp_app_db,
            workbench_api_key_path=temp_key_file,
            ai_config_path=temp_ai_cfg_file,
            supabase_config_path=temp_sb_cfg_file,
        )

    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    # Seed isolated temp_app_db with 1 v2 + 1 legacy v1 AI cache row (zero external API calls, read-only raw DB untouched)
    # so browser E2E verifies v1/v2 coexistence, dry_run v1-upgrade breakdown, and v2 tag rendering in 02 Drawer & 05 Detail Modal!
    import sqlite3
    from backend.ai_service import compute_evidence_hash
    from backend.repository import SnapshotRepository

    _seed_repo = SnapshotRepository(DEFAULT_DB_PATH)
    _seed_date = _seed_repo.get_available_dates()[0]
    _seed_prods = {p["asin"]: p for p in _seed_repo.get_scope_data(observed_date=_seed_date, category="all", dedup_mode="asin")["products"]}
    _v2_asin = "B0HR635LDJ" if "B0HR635LDJ" in _seed_prods else next(iter(_seed_prods))
    _v1_asin = "B0HQC86BJX" if "B0HQC86BJX" in _seed_prods else list(_seed_prods.keys())[1]
    _p_v2 = _seed_prods[_v2_asin]
    _p_v1 = _seed_prods[_v1_asin]
    with sqlite3.connect(temp_app_db) as _conn:
        _conn.execute(
            """INSERT OR REPLACE INTO product_ai_cache
               (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type, confidence,
                filter_reason, print_subject, slogan_text, visual_style, theme_tags_json,
                season_tag, motif, transferability, raw_json, updated_at)
               VALUES (?, ?, 'deepseek-flash', 'v2', 1, 'graphic_print', 0.95,
                       '胸前含圣诞复古手绘字母与图案', '复古圣诞字母图案', 'MERRY', '复古手绘',
                       '["christmas", "retro"]', 'christmas', 'christmas_tree', 'pure_graphic', '{}', '2026-11-22T00:00:00Z')""",
            (_v2_asin, compute_evidence_hash(_p_v2.get("title"), _p_v2.get("main_image_url"))),
        )
        _conn.execute(
            """INSERT OR REPLACE INTO product_ai_cache
               (asin, evidence_hash, model, prompt_version, is_graphic_print, pattern_type, confidence,
                filter_reason, print_subject, slogan_text, visual_style, theme_tags_json,
                season_tag, motif, transferability, raw_json, updated_at)
               VALUES (?, ?, 'deepseek-flash', 'v1', 1, 'graphic_print', 0.91,
                       '旧版 v1 圣诞印花缓存', '圣诞字母卫衣', 'MERRY', '复古',
                       '["christmas"]', '', '', '', '{}', '2026-11-21T00:00:00Z')""",
            (_v1_asin, compute_evidence_hash(_p_v1.get("title"), _p_v1.get("main_image_url"))),
        )
        _conn.commit()

    cdp_port = find_free_port()
    chrome_profile = tempfile.mkdtemp(prefix="hotprint-chrome-")
    chrome_proc = subprocess.Popen(
        [
            CHROME_BIN,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={chrome_profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    report = {"server_port": port, "checks": {}, "screenshots": []}
    cdp = None
    try:
        # Wait for CDP endpoint
        ws_path = None
        for _ in range(40):
            try:
                with urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                    page = next((t for t in targets if t.get("type") == "page"), None)
                    if page and page.get("webSocketDebuggerUrl"):
                        ws_url = page["webSocketDebuggerUrl"]
                        ws_path = "/" + ws_url.split("/", 3)[3]
                        break
            except Exception:
                time.sleep(0.2)
        if not ws_path:
            raise RuntimeError("Failed to connect to headless Chrome CDP")

        cdp = SimpleCDPClient("127.0.0.1", cdp_port, ws_path)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # 1. Desktop 1440x900 — Market Dashboard
        cdp.set_viewport(1440, 900, mobile=False)
        cdp.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/#dashboard"})
        wait_for(cdp, "Boolean(document.querySelector('#chart-sales-hist svg path')) && Boolean(document.querySelector('#chart-keyword-cloud .wc-word'))")
        # Allow eager hot-sample images & floating word cloud to run briefly
        wc_before = cdp.eval_js("document.querySelector('#chart-keyword-cloud .wc-word')?.getAttribute('transform')")
        time.sleep(0.55)
        wc_after = cdp.eval_js("document.querySelector('#chart-keyword-cloud .wc-word')?.getAttribute('transform')")
        # Test pause motion button
        cdp.eval_js("document.getElementById('btn-toggle-wc-motion')?.click()")
        time.sleep(0.15)
        wc_paused_label = cdp.eval_js("document.getElementById('btn-toggle-wc-motion')?.textContent.trim()")
        cdp.eval_js("document.getElementById('btn-toggle-wc-motion')?.click()")
        time.sleep(0.5)

        desktop_metrics = cdp.eval_js("""(() => {
          const doc = document.documentElement;
          const kpis = Array.from(document.querySelectorAll('.kpi-value')).map(e => e.textContent.trim());
          const kpiLabels = Array.from(document.querySelectorAll('.kpi-label')).map(e => e.textContent.trim());
          const navOrder = Array.from(document.querySelectorAll('#nav-sections .nav-item')).map(e => e.getAttribute('data-route'));
          const hotAsins = Array.from(document.querySelectorAll('.hot-grid .product-card')).map(e => e.getAttribute('data-open-asin'));
          const refreshBtn = document.getElementById('btn-refresh');
          const mainText = document.getElementById('main-content')?.textContent || '';
          const salesRect = document.querySelector('#chart-sales-hist')?.getBoundingClientRect();
          const priceRect = document.querySelector('#chart-price-bands')?.getBoundingClientRect();
          const compareRect = document.querySelector('.panel-compare')?.getBoundingClientRect();
          return {
            innerWidth: window.innerWidth,
            scrollWidth: doc.scrollWidth,
            overflow: doc.scrollWidth > window.innerWidth,
            navOrder,
            menuToggleHiddenOnDesktop: getComputedStyle(document.getElementById('menu-toggle')).display === 'none',
            sidebarCloseHiddenOnDesktop: getComputedStyle(document.getElementById('sidebar-close')).display === 'none',
            methodologyBtnRemoved: document.getElementById('btn-methodology') === null,
            disclosureBarRemoved: document.getElementById('disclosure-bar') === null,
            dedupSwitchRemoved: document.getElementById('dedup-switch') === null,
            refreshIsIconOnly: Boolean(refreshBtn && refreshBtn.textContent.trim() === '' && refreshBtn.querySelector('svg') && refreshBtn.getAttribute('title') === '刷新数据'),
            bsrCoverageCardRemoved: !kpiLabels.some(l => l.includes('BSR')),
            verboseCopyRemoved: !mainText.includes('非单印花独立销量') && !mainText.includes('接口报告共 28 页') && !mainText.includes('勿跨类目混排'),
            activePrepScope: document.querySelector('.prep-scope-bar button[data-prep-scope].is-active')?.getAttribute('data-prep-scope') || '',
            kpiCount: kpis.length,
            kpiFirst: kpis[0] || '',
            kpiLabels,
            visualsAboveCompare: Boolean(salesRect && priceRect && compareRect && salesRect.bottom <= compareRect.top + 8 && priceRect.bottom <= compareRect.top + 8),
            salesSplinePaths: document.querySelectorAll('#chart-sales-hist svg path').length,
            salesNodes: document.querySelectorAll('#chart-sales-hist .spline-node').length,
            compareCards: document.querySelectorAll('.panel-compare .cat-compare-card').length,
            priceDonutArcs: document.querySelectorAll('#chart-price-bands .donut-arc').length,
            heroPulsePresent: Boolean(document.querySelector('.dash-hero-pulse')),
            tableCollapsedByDefault: Boolean(document.getElementById('dash-table-collapse-body')?.hidden),
            scatterDotsInitially: document.querySelectorAll('#chart-price-scatter circle').length,
            scatterCategoryPills: document.querySelectorAll('.scatter-cat-pill[data-scatter-cat]').length,
            wordCloudCount: document.querySelectorAll('#chart-keyword-cloud .wc-word').length,
            hotCards: hotAsins.length,
            hotAsins,
            qualityRemovedFromDashboard: document.querySelector('section[aria-label="数据质量概览"]') === null && document.querySelector('.quality-grid') === null,
            brandCards: document.querySelectorAll('.brand-showcase-grid .brand-card').length,
            brandEmblems: document.querySelectorAll('.brand-showcase-grid .brand-emblem').length,
            tableRows: document.querySelectorAll('#product-table-body-wrap tbody tr').length,
            bsrSortDisabled: document.querySelector('#table-sort-field option[value="source_category_bsr"]')?.disabled === true,
          };
        })()""")
        desktop_metrics["wordCloudMoved"] = (wc_before is not None and wc_after is not None and wc_before != wc_after)
        desktop_metrics["wordCloudPauseLabel"] = wc_paused_label
        report["checks"]["desktop_1440_dashboard"] = desktop_metrics
        assert not desktop_metrics["overflow"], f"Desktop horizontal overflow: {desktop_metrics}"
        assert desktop_metrics["navOrder"][:3] == ["collection", "preprocessing", "dashboard"], f"Unexpected nav order: {desktop_metrics['navOrder']}"
        assert desktop_metrics["menuToggleHiddenOnDesktop"], "Menu toggle should be hidden on 1440px desktop"
        assert desktop_metrics["sidebarCloseHiddenOnDesktop"], "Sidebar close button should be hidden on 1440px desktop"
        assert desktop_metrics["methodologyBtnRemoved"], "Methodology button should be removed"
        assert desktop_metrics["disclosureBarRemoved"], "Top disclosure bar should be removed"
        assert desktop_metrics["dedupSwitchRemoved"], "Dedup switch should be removed (always default listing clean)"
        assert desktop_metrics["refreshIsIconOnly"], "Refresh button should be icon-only with tooltip"
        assert desktop_metrics["bsrCoverageCardRemoved"], "Source BSR coverage card should be removed from top KPIs"
        assert desktop_metrics["verboseCopyRemoved"], "Verbose explanatory copy should be removed from main view"
        assert desktop_metrics["activePrepScope"] == "printable", f"Dashboard should default to printable scope: {desktop_metrics}"
        assert desktop_metrics["heroPulsePresent"], "Executive Dark Hero Pulse card should render"
        assert desktop_metrics["tableCollapsedByDefault"], "Product sample table should be collapsed by default"
        assert desktop_metrics["kpiCount"] == 4, f"Expected 4 concise 2x2 KPI cards, got {desktop_metrics['kpiCount']}"
        assert desktop_metrics["visualsAboveCompare"], "Sales & Price visuals must appear ABOVE category comparison"
        assert desktop_metrics["salesSplinePaths"] == 2 and desktop_metrics["salesNodes"] == 5, f"Expected smooth spline area chart: {desktop_metrics}"
        assert desktop_metrics["compareCards"] >= 2, f"Expected category comparison cards: {desktop_metrics}"
        assert desktop_metrics["priceDonutArcs"] == 5, f"Expected 5 price donut ring arcs: {desktop_metrics}"
        assert desktop_metrics["scatterDotsInitially"] > 0, f"Scatter plot should pre-select Top 2 active categories by default: {desktop_metrics}"
        assert desktop_metrics["scatterCategoryPills"] >= 2, "Scatter plot should show selectable category pills"
        assert desktop_metrics["wordCloudCount"] >= 25, f"Expected >=25 rendered word cloud keywords, got {desktop_metrics['wordCloudCount']}"
        assert desktop_metrics["wordCloudMoved"], f"Word cloud should float gently: before={wc_before}, after={wc_after}"
        assert desktop_metrics["wordCloudPauseLabel"] == "恢复浮动", f"Pause toggle label mismatch: {wc_paused_label}"
        assert desktop_metrics["qualityRemovedFromDashboard"], "Data Quality Overview should be removed from Market Dashboard"
        assert desktop_metrics["brandCards"] >= 4 and desktop_metrics["brandEmblems"] == desktop_metrics["brandCards"], "Brand showcase cards and emblems should render"
        assert desktop_metrics["bsrSortDisabled"], "BSR sort should be disabled in All Categories view"

        # Test interactive scatter plot clear & select-all
        cdp.eval_js("document.getElementById('btn-scatter-clear-all').click()")
        wait_for(cdp, "document.querySelectorAll('#chart-price-scatter circle').length === 0")
        cdp.eval_js("document.querySelector('.scatter-cat-pill[data-scatter-cat]').click()")
        wait_for(cdp, "document.querySelectorAll('#chart-price-scatter circle').length > 0")
        scatter_after_pick = cdp.eval_js("document.querySelectorAll('#chart-price-scatter circle').length")
        cdp.eval_js("document.getElementById('btn-scatter-select-all').click()")
        wait_for(cdp, f"document.querySelectorAll('#chart-price-scatter circle').length >= {scatter_after_pick}")
        scatter_after_all = cdp.eval_js("document.querySelectorAll('#chart-price-scatter circle').length")
        report["checks"]["scatter_interactive_selection"] = {
            "dots_after_single_pick": scatter_after_pick,
            "dots_after_select_all": scatter_after_all,
        }
        assert scatter_after_pick > 0 and scatter_after_all >= scatter_after_pick

        shot1 = SCREENSHOT_DIR / "dashboard-desktop-1440.png"
        cdp.screenshot(shot1, full_page=True)
        report["screenshots"].append(str(shot1))

        # Switch prep scope to 'all' to verify full 142 cleaned listing count & folded orphan ASINs
        cdp.eval_js("""document.querySelector('.prep-scope-bar button[data-prep-scope="all"]').click()""")
        wait_for(cdp, "document.querySelector('.kpi-value')?.textContent.trim() === '142'")
        all_scope_asins = cdp.eval_js("Array.from(document.querySelectorAll('.hot-grid .product-card')).map(e => e.getAttribute('data-open-asin'))")
        for folded in ("B0HQCLVVDK", "B0HQC3YX3Q", "B0HQCVJZK3", "B0HQCG8RFQ"):
            assert folded not in all_scope_asins, f"Leaked orphan ASIN {folded} should be folded under B0HQC86BJX!"
        assert "B0HQC86BJX" in all_scope_asins, "Representative parent listing B0HQC86BJX should remain in Top 8"

        # 2. Category Switch -> Women's Novelty Sweatshirts
        cdp.eval_js("""document.querySelector('#category-switch button[data-category="9056929011"]').click()""")
        wait_for(cdp, "document.querySelector('#table-sort-field option[value=\"source_category_bsr\"]')?.disabled === false")
        time.sleep(0.25)
        women_check = cdp.eval_js("""(() => ({
          kpiFirst: document.querySelector('.kpi-value')?.textContent.trim(),
          kpiSubFirst: document.querySelector('.kpi-sub')?.textContent.trim(),
          bsrSortDisabled: document.querySelector('#table-sort-field option[value="source_category_bsr"]')?.disabled,
          scatterDots: document.querySelectorAll('#chart-price-scatter circle').length,
        }))()""")
        report["checks"]["category_switch_women"] = women_check
        assert "100 ASIN" in women_check["kpiSubFirst"], f"Expected 100 raw ASINs in women view: {women_check}"
        assert women_check["bsrSortDisabled"] is False

        # Switch back to all and open B0HQC86BJX (10-ASIN Shiketong exact-description cluster)
        cdp.eval_js("""document.querySelector('#category-switch button[data-category="all"]').click()""")
        wait_for(cdp, "document.querySelector('.kpi-value')?.textContent.includes('142')")

        # 3. Open Product Evidence Drawer for B0HQC86BJX (10-ASIN cluster) & test Escape close
        cdp.eval_js("""document.querySelector('.hot-grid .product-card[data-open-asin="B0HQC86BJX"]').click()""")
        wait_for(cdp, "document.getElementById('detail-drawer').classList.contains('is-open') && document.querySelectorAll('#drawer-body tr[data-sibling-asin]').length === 10")
        time.sleep(0.4)
        shot_drawer = SCREENSHOT_DIR / "drawer-desktop-1440.png"
        cdp.screenshot(shot_drawer, full_page=False)
        report["screenshots"].append(str(shot_drawer))

        # Test Escape key closes drawer
        cdp.eval_js("""document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))""")
        time.sleep(0.2)
        drawer_closed = cdp.eval_js("!document.getElementById('detail-drawer').classList.contains('is-open')")
        report["checks"]["drawer_open_and_escape_close"] = {"opened": True, "closed_by_escape": drawer_closed}
        assert drawer_closed, "Drawer did not close on Escape"

        # 3b. Verify retired Cross-Category Signals tab button is removed from Stage 03
        signals_removed = cdp.eval_js("document.querySelector('[data-dash-tab=\"signals\"]') === null")
        assert signals_removed, "Expected Cross-Category Signals tab button to be removed"
        report["checks"]["stage03_signals_removed"] = True

        # 4. Stage 01 Cloud Data Sync Page + Collapsible Settings + Two-Step Key Clear + Coverage Table
        cdp.eval_js("""document.querySelector('.nav-item[data-route="collection"]').click()""")
        wait_for(cdp, "Boolean(document.getElementById('supabase-sync-panel'))")
        time.sleep(0.25)

        # Ensure Sorftime legacy UI is fully retired from Stage 01
        sorftime_retired_ok = cdp.eval_js(
            "!document.getElementById('collection-plan-form') && !document.getElementById('col-api-key-input')"
        )
        assert sorftime_retired_ok, "Legacy Sorftime UI should be removed from Stage 01"

        # Open settings drawer if collapsed, save & mask Supabase key, then clear via 2-step confirmation
        cdp.eval_js("""(() => {
          const drawer = document.getElementById('supabase-settings-drawer');
          if (drawer && drawer.hidden) {
            document.getElementById('btn-toggle-sb-settings').click();
          }
        })()""")
        wait_for(cdp, "Boolean(document.getElementById('supabase-settings-drawer')) && !document.getElementById('supabase-settings-drawer').hidden")
        cdp.eval_js("""(() => {
          document.getElementById('supabase-key-input').value = 'DUMMY_SUPABASE_KEY_5432';
          document.getElementById('btn-supabase-save').click();
        })()""")
        wait_for(cdp, "(document.getElementById('supabase-sync-panel')?.textContent || '').includes('****5432') && Boolean(document.getElementById('btn-supabase-clear'))")
        sb_saved_ok = cdp.eval_js("!(document.body.textContent || '').includes('DUMMY_SUPABASE_KEY_5432') && Boolean(document.getElementById('btn-supabase-sync'))")
        assert sb_saved_ok, "Supabase key save UI check failed or exposed plaintext key"
        # First click arms 2-step confirmation; second click executes clear
        cdp.eval_js("document.getElementById('btn-supabase-clear').click()")
        wait_for(cdp, "document.getElementById('btn-supabase-clear')?.dataset.confirmArmed === '1'")
        cdp.eval_js("document.getElementById('btn-supabase-clear').click()")
        wait_for(cdp, "!document.getElementById('btn-supabase-clear')")
        report["checks"]["supabase_sync_ui"] = {
            "panel_rendered": True,
            "sorftime_ui_retired": True,
            "saved_masked_without_leak": True,
            "two_step_cleared": True,
        }

        col_check = cdp.eval_js("""(() => {
          return {
            kpiCards: document.querySelectorAll('.col-kpi-grid .kpi-card').length,
            categoryRows: document.querySelectorAll('section[aria-label="核心类目同步覆盖"] tbody tr').length,
            batchesRendered: document.querySelectorAll('section[aria-label="历史同步批次"] tbody tr').length,
            healthStripPresent: Boolean(document.querySelector('section[aria-label="最近快照健康度"]')),
            hpBarPresent: Boolean(document.getElementById('supabase-hp-bar')),
            syncDateInputPresent: Boolean(document.getElementById('supabase-sync-date')),
            badgeCount: document.querySelectorAll('.main-content .badge').length,
            overflow: document.documentElement.scrollWidth > window.innerWidth,
          };
        })()""")
        report["checks"]["collection_streamlined_ui"] = col_check
        assert col_check["kpiCards"] == 3, f"Expected 3 compact KPI cards: {col_check}"
        assert col_check["categoryRows"] == 6, f"Expected 6 target category rows: {col_check}"
        assert col_check["batchesRendered"] >= 1, f"Expected historical batches rendered: {col_check}"
        assert col_check["healthStripPresent"], "Single-line snapshot health summary strip should render"
        assert col_check["hpBarPresent"], "Team cloud state bar should render"
        assert col_check["syncDateInputPresent"], "Explicit sync observation date selector should render"
        assert col_check["badgeCount"] <= 6, f"Expected quiet Stage 01 with <=6 badges, got {col_check['badgeCount']}"
        assert not col_check["overflow"]

        shot_col = SCREENSHOT_DIR / "collection-desktop-1440.png"
        cdp.screenshot(shot_col, full_page=True)
        report["screenshots"].append(str(shot_col))

        # 5. Stage 02/03 Data Preprocessing — Gallery Triage default + Multi-select Batch + Brand Batch + List Toggle
        cdp.eval_js("""document.querySelector('.nav-item[data-route="preprocessing"]').click()""")
        wait_for(cdp, "Boolean(document.getElementById('btn-run-preprocessing')) && document.querySelectorAll('#prep-gallery-grid .triage-card').length > 0")
        time.sleep(0.4)

        prep_initial = cdp.eval_js("""(() => {
          const vals = Array.from(document.querySelectorAll('.prep-kpi-grid .kpi-value')).map(e => Number(e.textContent.replace(/,/g, '').trim()));
          return {
            total: vals[0], ready: vals[1], needsReview: vals[2], excluded: vals[3],
            galleryCards: document.querySelectorAll('#prep-gallery-grid .triage-card').length,
            categoryFilterOptions: document.querySelectorAll('#prep-category-filter option').length,
            rulesDrawerHiddenByDefault: Boolean(document.getElementById('prep-rules-drawer')?.hidden),
            moreFiltersHiddenByDefault: Boolean(document.getElementById('prep-more-filters-drawer')?.hidden),
            sumMatches: vals[0] === vals[1] + vals[2] + vals[3],
            overflow: document.documentElement.scrollWidth > window.innerWidth,
          };
        })()""")
        report["checks"]["preprocessing_initial"] = prep_initial
        assert prep_initial["total"] == 155 and prep_initial["excluded"] == 0 and prep_initial["sumMatches"]
        assert prep_initial["galleryCards"] == prep_initial["needsReview"], f"Expected default queue to be needs_review: {prep_initial}"
        assert prep_initial["rulesDrawerHiddenByDefault"], "Rules diagnostic drawer should be collapsed by default"
        assert prep_initial["moreFiltersHiddenByDefault"], "Advanced filters drawer should be collapsed by default"
        assert prep_initial["categoryFilterOptions"] == 7, f"Expected all + 6 category options: {prep_initial}"
        assert not prep_initial["overflow"]

        # Verify Stage 02 AI Purify dry-run modal & AI suggest_exclude tab exist (0 real API calls)
        assert cdp.eval_js("Boolean(document.getElementById('btn-prep-ai-purify')) && Boolean(document.querySelector('[data-prep-ai=\"suggest_exclude\"]'))"), "Stage 02 AI purify button and tab must render"
        cdp.eval_js("document.getElementById('btn-prep-ai-purify').click()")
        wait_for(cdp, "document.getElementById('review-modal-overlay')?.classList.contains('is-open') && (document.getElementById('review-modal-title')?.textContent || '').includes('成本预检')")
        cdp.eval_js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))")
        wait_for(cdp, "!document.getElementById('review-modal-overlay')?.classList.contains('is-open')")

        # Test Multi-select batch exclude with confirmation modal (select 2 cards -> click 批量确认排除 -> confirm modal -> verify excluded === 2 -> undo)
        cdp.eval_js("""(() => {
          const cbs = Array.from(document.querySelectorAll('#prep-gallery-grid .triage-checkbox')).slice(0, 2);
          cbs.forEach(cb => cb.click());
        })()""")
        wait_for(cdp, "document.getElementById('prep-batch-bar')?.classList.contains('has-selection') && (document.getElementById('prep-selected-count')?.textContent || '').includes('2')")
        cdp.eval_js("""document.getElementById('btn-batch-exclude').click()""")
        wait_for(cdp, "document.getElementById('review-modal-overlay')?.classList.contains('is-open') && Boolean(document.getElementById('modal-confirm-multi-batch-btn'))")
        cdp.eval_js("""document.getElementById('modal-confirm-multi-batch-btn').click()""")
        wait_for(cdp, "document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '2' && Boolean(document.getElementById('btn-toast-undo-batch'))")
        cdp.eval_js("""document.getElementById('btn-toast-undo-batch').click()""")
        wait_for(cdp, "document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '0'")

        # Test List View toggle works cleanly, then switch back to Gallery Triage
        cdp.eval_js("""document.querySelector('[data-prep-view="table"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#prep-table-wrap tbody tr').length > 0")
        cdp.eval_js("""document.querySelector('[data-prep-view="gallery"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#prep-gallery-grid .triage-card').length > 0")

        # Switch to All queue & open More Filters -> filter sales_suspect -> badge renders -> reset
        cdp.eval_js("""document.querySelector('[data-prep-elig="all"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#prep-gallery-grid .triage-card').length >= 15")
        cdp.eval_js("""(() => {
          const drawer = document.getElementById('prep-more-filters-drawer');
          if (drawer && drawer.hidden) document.getElementById('btn-prep-more-filters').click();
        })()""")
        wait_for(cdp, "!document.getElementById('prep-more-filters-drawer')?.hidden")
        cdp.eval_js("""(() => {
          const sel = document.getElementById('prep-issue-filter');
          sel.value = 'sales_suspect';
          sel.dispatchEvent(new Event('change', { bubbles: true }));
        })()""")
        wait_for(cdp, "document.querySelectorAll('#prep-table-wrap .badge-sales-suspect').length > 0")
        prep_anomaly_badges = cdp.eval_js("document.querySelectorAll('#prep-table-wrap .badge-sales-suspect').length")
        assert prep_anomaly_badges > 0, "Expected ⚠ 销量存疑 badges in Stage 02"
        cdp.eval_js("document.getElementById('prep-reset-btn').click()")
        wait_for(cdp, "document.querySelectorAll('#prep-gallery-grid .triage-card').length >= 15")

        # Verify P2 AI Vision v2 tags render in Stage 02 Product Detail Drawer (seeded v2 item)
        cdp.eval_js(f"""(() => {{
          const inp = document.getElementById('prep-search');
          inp.value = '{_v2_asin}';
          inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }})()""")
        wait_for(cdp, f"Boolean(document.querySelector('#prep-table-wrap [data-act-detail=\"{_v2_asin}\"]'))")
        cdp.eval_js(f"""document.querySelector('#prep-table-wrap [data-act-detail="{_v2_asin}"]').click()""")
        wait_for(cdp, "document.getElementById('detail-drawer')?.classList.contains('is-open') && document.querySelectorAll('#detail-drawer [data-ai-v2-tag]').length >= 3")
        prep_drawer_v2 = cdp.eval_js("""(() => ({
          season: document.querySelector('#detail-drawer [data-ai-v2-tag="season"]')?.getAttribute('data-ai-v2-value'),
          motif: document.querySelector('#detail-drawer [data-ai-v2-tag="motif"]')?.getAttribute('data-ai-v2-value'),
          transferability: document.querySelector('#detail-drawer [data-ai-v2-tag="transferability"]')?.getAttribute('data-ai-v2-value'),
        }))()""")
        assert prep_drawer_v2 == {"season": "christmas", "motif": "christmas_tree", "transferability": "pure_graphic"}, f"Stage 02 drawer v2 tags mismatch: {prep_drawer_v2}"
        cdp.eval_js("""document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))""")
        wait_for(cdp, "!document.getElementById('detail-drawer')?.classList.contains('is-open')")
        cdp.eval_js("document.getElementById('prep-reset-btn').click()")
        wait_for(cdp, "document.querySelectorAll('#prep-gallery-grid .triage-card').length >= 10")

        # Click Exclude on first needs_review card -> Confirm in modal
        cdp.eval_js("""document.querySelector('#prep-table-wrap [data-act-exclude]').click()""")
        wait_for(cdp, "document.getElementById('review-modal-overlay').classList.contains('is-open')")
        cdp.eval_js("""document.getElementById('modal-confirm-exclude-btn').click()""")
        wait_for(cdp, "document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '1'")
        time.sleep(0.25)

        # Reload browser page to verify persistence across full page reload!
        cdp.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/#preprocessing"})
        wait_for(cdp, "document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '1'")

        # Click Run Preprocessing again -> verify human exclusion survives re-run!
        cdp.eval_js("""document.getElementById('btn-run-preprocessing').click()""")
        wait_for(cdp, "Boolean(document.getElementById('prep-toast')) && document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '1'")
        time.sleep(0.3)

        shot_prep = SCREENSHOT_DIR / "preprocessing-desktop-1440.png"
        cdp.screenshot(shot_prep, full_page=True)
        report["screenshots"].append(str(shot_prep))

        # Open Preprocessing Detail Drawer on an excluded item (filter excluded tab first)
        cdp.eval_js("""document.querySelector('[data-prep-elig="excluded"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#prep-table-wrap [data-prep-asin]').length === 1")
        cdp.eval_js("""document.querySelector('#prep-table-wrap [data-act-detail]').click()""")
        wait_for(cdp, "document.getElementById('detail-drawer').classList.contains('is-open') && document.querySelectorAll('.timeline-item').length >= 1")
        time.sleep(0.3)
        shot_prep_drw = SCREENSHOT_DIR / "preprocessing-drawer-1440.png"
        cdp.screenshot(shot_prep_drw, full_page=False)
        report["screenshots"].append(str(shot_prep_drw))

        # Restore inside drawer -> verify excluded drops back to 0!
        cdp.eval_js("""document.getElementById('drawer-btn-restore').click()""")
        wait_for(cdp, "document.querySelectorAll('.prep-kpi-grid .kpi-value')[3]?.textContent.trim() === '0'")
        cdp.eval_js("""document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))""")
        time.sleep(0.2)
        report["checks"]["preprocessing_human_review_flow"] = {
          "batch_exclude_and_undo": True,
          "view_toggle_gallery_and_table": True,
          "exclude_persisted_after_reload": True,
          "preserved_after_rerun": True,
          "restored_to_unreviewed": True,
        }

        # Check Preprocessing responsive 768px & 390px overflow
        cdp.set_viewport(768, 1024, mobile=False)
        time.sleep(0.25)
        prep_tab_overflow = cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth")
        assert not prep_tab_overflow, "Preprocessing 768px horizontal overflow"
        shot_prep_tab = SCREENSHOT_DIR / "preprocessing-tablet-768.png"
        cdp.screenshot(shot_prep_tab, full_page=False)
        report["screenshots"].append(str(shot_prep_tab))

        cdp.set_viewport(390, 844, mobile=True)
        time.sleep(0.25)
        prep_mob_overflow = cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth")
        assert not prep_mob_overflow, "Preprocessing 390px horizontal overflow"
        shot_prep_mob = SCREENSHOT_DIR / "preprocessing-mobile-390.png"
        cdp.screenshot(shot_prep_mob, full_page=False)
        report["screenshots"].append(str(shot_prep_mob))

        # Restore desktop viewport for Stage 04 Candidate Scoring checks
        cdp.set_viewport(1440, 900, mobile=False)

        # 5a. Stage 04 Candidate Scoring — Dynamic sliders, rank delta, presets, responsive
        cdp.eval_js("""document.querySelector('.nav-item[data-route="scoring"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#scoring-table-wrap tbody tr[data-score-asin]').length > 10")
        time.sleep(0.35)

        scoring_before = cdp.eval_js("""(() => ({
          sliders: document.querySelectorAll('[data-weight-slider]').length,
          rows: document.querySelectorAll('#scoring-table-wrap tbody tr[data-score-asin]').length,
          contribBars: document.querySelectorAll('#scoring-table-wrap .contrib-track').length,
          confBadges: document.querySelectorAll('#scoring-table-wrap .conf-badge').length,
          anomalyBadges: document.querySelectorAll('#scoring-table-wrap .badge-cred-anomaly').length,
          top10: Array.from(document.querySelectorAll('#scoring-table-wrap tbody tr[data-score-asin]')).slice(0, 10).map(tr => tr.getAttribute('data-score-asin')),
          overflow: document.documentElement.scrollWidth > window.innerWidth,
        }))()""")
        assert scoring_before["sliders"] == 5
        assert scoring_before["rows"] > 10
        assert scoring_before["contribBars"] == scoring_before["rows"]
        assert scoring_before["confBadges"] == scoring_before["rows"]
        assert scoring_before["anomalyBadges"] > 0, f"Expected credibility anomaly badges in Stage 04: {scoring_before}"
        assert not scoring_before["overflow"]

        # Drag sliders to boost freshness & opportunity, verify Top 10 reorders and rank deltas appear!
        cdp.eval_js("""(() => {
          const setVal = (key, val) => {
            const el = document.querySelector(`[data-weight-slider="${key}"]`);
            el.value = String(val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
          };
          setVal('sales', 5);
          setVal('opportunity', 45);
          setVal('freshness', 40);
          setVal('rating', 5);
          setVal('price', 5);
        })()""")
        wait_for(cdp, "document.querySelectorAll('#scoring-table-wrap .rank-delta.is-up').length > 0")
        time.sleep(0.25)
        scoring_after = cdp.eval_js("""(() => ({
          top10: Array.from(document.querySelectorAll('#scoring-table-wrap tbody tr[data-score-asin]')).slice(0, 10).map(tr => tr.getAttribute('data-score-asin')),
          upBadges: document.querySelectorAll('#scoring-table-wrap .rank-delta.is-up').length,
          downBadges: document.querySelectorAll('#scoring-table-wrap .rank-delta.is-down').length,
        }))()""")
        assert scoring_after["top10"] != scoring_before["top10"], "Slider weight adjustment should reorder Top 10"
        assert scoring_after["upBadges"] > 0 and scoring_after["downBadges"] > 0

        # Save a preset via modal -> verify chip appears -> delete chip
        cdp.eval_js("""document.getElementById('btn-scoring-save-preset').click()""")
        wait_for(cdp, "document.getElementById('review-modal-overlay').classList.contains('is-open')")
        cdp.eval_js("""(() => {
          document.getElementById('preset-name-input').value = 'E2E新品验证方案';
          document.getElementById('preset-confirm-save-btn').click();
        })()""")
        wait_for(cdp, "(document.getElementById('scoring-preset-list')?.textContent || '').includes('E2E新品验证方案')")
        cdp.eval_js("""document.querySelector('#scoring-preset-list [data-del-preset]').click()""")
        wait_for(cdp, "!(document.getElementById('scoring-preset-list')?.textContent || '').includes('E2E新品验证方案')")

        report["checks"]["stage04_scoring"] = {
          "sliders": scoring_before["sliders"],
          "rows": scoring_before["rows"],
          "top10_reordered_on_slider_drag": True,
          "rank_delta_badges": {"up": scoring_after["upBadges"], "down": scoring_after["downBadges"]},
          "preset_save_and_delete": True,
        }
        shot_score = SCREENSHOT_DIR / "scoring-desktop-1440.png"
        cdp.screenshot(shot_score, full_page=True)
        report["screenshots"].append(str(shot_score))

        # Check Scoring responsive 768px & 390px zero overflow
        cdp.set_viewport(768, 1024, mobile=False)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Scoring 768px overflow"
        cdp.set_viewport(390, 844, mobile=True)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Scoring 390px overflow"
        cdp.set_viewport(1440, 900, mobile=False)

        # 5a-2. Stage 05 Print Pattern Recognition & Direction Grouping — Full closed-loop E2E + reload persistence
        cdp.eval_js("""document.querySelector('.nav-item[data-route="print_verification"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#dir-cand-grid .dir-cand-card').length >= 10")
        time.sleep(0.35)

        # Test Lightbox zoom preview & Escape close
        cdp.eval_js("""document.querySelector('#dir-cand-grid .dir-zoom-btn').click()""")
        wait_for(cdp, "document.getElementById('dir-lightbox-overlay')?.classList.contains('is-open')")
        cdp.eval_js("""document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))""")
        wait_for(cdp, "!document.getElementById('dir-lightbox-overlay')?.classList.contains('is-open')")

        # Single-click 3 candidate cards (ensuring seeded _v2_asin is included) -> Create new direction from selected
        cdp.eval_js(f"""(() => {{
          const all = Array.from(document.querySelectorAll('#dir-cand-grid .dir-cand-card'));
          const v2Card = all.find(c => c.getAttribute('data-cand-asin') === '{_v2_asin}' || c.textContent.includes('{_v2_asin}'));
          const picks = [];
          if (v2Card) picks.push(v2Card);
          for (const c of all) {{
            if (picks.length >= 3) break;
            if (!picks.includes(c)) picks.push(c);
          }}
          picks.forEach(c => c.click());
        }})()""")
        wait_for(cdp, "document.getElementById('dir-batch-bar')?.classList.contains('has-selection') && (document.getElementById('dir-selected-count')?.textContent || '').includes('3')")
        cdp.eval_js("""document.getElementById('btn-dir-create-from-selected').click()""")
        wait_for(cdp, "document.getElementById('review-modal-overlay').classList.contains('is-open')")
        cdp.eval_js("""(() => {
          document.getElementById('dir-form-name').value = '复古圣诞节日文字梗';
          document.getElementById('dir-form-tags').value = '复古做旧, 文字梗, 节日派对';
          document.getElementById('dir-form-audience').value = '年轻女性';
          document.getElementById('dir-form-submit').click();
        })()""")
        wait_for(cdp, "document.querySelectorAll('#dir-card-list .dir-card').length === 1")

        # Select 1 more unassigned card -> assign to existing direction
        cdp.eval_js("""document.querySelector('#dir-cand-grid .dir-cand-card').click()""")
        wait_for(cdp, "(document.getElementById('dir-selected-count')?.textContent || '').includes('1')")
        cdp.eval_js("""document.getElementById('btn-dir-assign-existing').click()""")
        wait_for(cdp, "(document.querySelector('#dir-card-list .dir-card')?.textContent || '').includes('/ 4')")

        # Rate 5 stars & toggle shortlist -> reload browser -> verify persistence!
        cdp.eval_js("""document.querySelector('#dir-card-list [data-rate-dir$=":5"]').click()""")
        time.sleep(0.25)
        cdp.eval_js("""document.querySelector('#dir-card-list [data-toggle-shortlist]').click()""")
        wait_for(cdp, "document.querySelector('#dir-card-list .dir-card')?.classList.contains('is-shortlisted')")

        # Full page reload -> confirm direction, 4 linked items, 5 stars & shortlisted persist!
        cdp.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/#print_verification"})
        wait_for(cdp, "document.querySelector('#dir-card-list .dir-card')?.classList.contains('is-shortlisted') && (document.querySelector('#dir-card-list .dir-card')?.textContent || '').includes('/ 4')")
        time.sleep(0.35)

        # Verify Stage 05 AI Cluster dry-run modal & Standalone Hits tab
        assert cdp.eval_js("Boolean(document.getElementById('btn-dir-ai-cluster'))"), "Stage 05 AI cluster button must render"
        cdp.eval_js("document.getElementById('btn-dir-ai-cluster').click()")
        wait_for(cdp, "document.getElementById('review-modal-overlay')?.classList.contains('is-open') && (document.getElementById('review-modal-title')?.textContent || '').includes('AI 一键归纳')")
        cdp.eval_js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))")
        wait_for(cdp, "!document.getElementById('review-modal-overlay')?.classList.contains('is-open')")

        # Switch to Standalone Hits tab -> verify Top 20 ungrouped strong singles render, then switch back
        cdp.eval_js("""document.querySelector('[data-dir-right-tab="standalone"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#dir-standalone-list .standalone-card').length > 0")
        standalone_cards_count = cdp.eval_js("document.querySelectorAll('#dir-standalone-list .standalone-card').length")
        cdp.eval_js("""document.querySelector('[data-dir-right-tab="directions"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#dir-card-list .dir-card').length === 1")
        time.sleep(0.25)

        # Open Direction Detail Modal and verify anomaly badge / outlier trim notice + P2 v2 tags render cleanly
        cdp.eval_js("""document.querySelector('#dir-card-list [data-open-dir-detail]').click()""")
        wait_for(cdp, "document.getElementById('dir-detail-overlay')?.classList.contains('is-open')")
        dir_detail_anomaly = cdp.eval_js("""(() => ({
          modalOpen: document.getElementById('dir-detail-overlay')?.classList.contains('is-open'),
          prodCards: document.querySelectorAll('#dir-detail-overlay .dir-detail-prod-card').length,
          anomalyBadges: document.querySelectorAll('#dir-detail-overlay .badge-cred-anomaly, #dir-detail-overlay .badge-cred-trimmed').length,
          v2SeasonTags: document.querySelectorAll('#dir-detail-overlay [data-ai-v2-tag="season"]').length,
          v2MotifTags: document.querySelectorAll('#dir-detail-overlay [data-ai-v2-tag="motif"]').length,
          v2TransferTags: document.querySelectorAll('#dir-detail-overlay [data-ai-v2-tag="transferability"]').length,
          legacyV1Tags: document.querySelectorAll('#dir-detail-overlay [data-ai-v2-tag="legacy_v1"]').length,
        }))()""")
        assert dir_detail_anomaly["v2SeasonTags"] >= 1 and dir_detail_anomaly["v2MotifTags"] >= 1 and dir_detail_anomaly["v2TransferTags"] >= 1, f"Expected v2 tags in Stage 05 detail modal: {dir_detail_anomaly}"
        cdp.eval_js("""document.getElementById('dir-detail-close-btn').click()""")
        wait_for(cdp, "!document.getElementById('dir-detail-overlay')?.classList.contains('is-open')")

        dir_metrics = cdp.eval_js("""(() => ({
          directionsCount: document.querySelectorAll('#dir-card-list .dir-card').length,
          shortlisted: document.querySelector('#dir-card-list .dir-card')?.classList.contains('is-shortlisted'),
          hasTypeBadge: Boolean(document.querySelector('#dir-card-list .badge-cluster, #dir-card-list .badge-standalone')),
          candAnomalyBadges: document.querySelectorAll('#dir-cand-grid .badge-cred-anomaly, #dir-card-list .badge-cred-anomaly, #dir-card-list .badge-cred-trimmed').length,
          unassignedCountText: document.querySelector('[data-dir-assigned="unassigned"]')?.textContent.trim(),
          assignedCountText: document.querySelector('[data-dir-assigned="assigned"]')?.textContent.trim(),
          overflow: document.documentElement.scrollWidth > window.innerWidth,
        }))()""")
        dir_metrics["standaloneCandidatesRendered"] = standalone_cards_count
        dir_metrics["detailModalCheck"] = dir_detail_anomaly
        assert dir_metrics["directionsCount"] == 1 and dir_metrics["shortlisted"] is True and dir_metrics["hasTypeBadge"] is True
        assert dir_metrics["candAnomalyBadges"] > 0 or dir_detail_anomaly["anomalyBadges"] > 0, f"Expected anomaly badges in Stage 05: {dir_metrics}"
        assert standalone_cards_count > 0, "Expected standalone Top 20 candidates in Stage 05"
        assert "(4)" in dir_metrics["assignedCountText"] and not dir_metrics["overflow"]
        report["checks"]["stage05_directions"] = {
          **dir_metrics,
          "lightbox_opened_and_closed": True,
          "created_with_3_then_added_1": True,
          "persisted_after_reload": True,
        }

        shot_dir = SCREENSHOT_DIR / "directions-desktop-1440.png"
        cdp.screenshot(shot_dir, full_page=False)
        report["screenshots"].append(str(shot_dir))

        # Check Stage 05 responsive 768px & 390px zero overflow
        cdp.set_viewport(768, 1024, mobile=False)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Directions 768px overflow"
        cdp.set_viewport(390, 844, mobile=True)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Directions 390px overflow"
        cdp.set_viewport(1440, 900, mobile=False)

        # 5b. Stage 06 Top 10 Directions Output & Handoff — Live sync from Stage 05 + inline note + export + remove -> empty state
        cdp.eval_js("""document.querySelector('.nav-item[data-route="top10_directions"]').click()""")
        wait_for(cdp, "document.querySelectorAll('#top10-card-list .top10-card').length === 1")
        time.sleep(0.35)

        # Inline edit design handoff note & save
        cdp.eval_js("""document.querySelector('#top10-card-list [data-note-edit]').click()""")
        wait_for(cdp, "Boolean(document.querySelector('#top10-card-list textarea'))")
        cdp.eval_js("""(() => {
          const ta = document.querySelector('#top10-card-list textarea');
          ta.value = '保留节日复古撞色条纹，替换为原创可爱猫咪文案二创';
          document.querySelector('#top10-card-list [data-note-save]').click();
        })()""")
        wait_for(cdp, "(document.querySelector('#top10-card-list .top10-note-display')?.textContent || '').includes('原创可爱猫咪文案二创')")

        # Expand supporting products table
        cdp.eval_js("""document.querySelector('#top10-card-list [data-top10-expand]').click()""")
        wait_for(cdp, "document.querySelectorAll('#top10-card-list tbody tr').length === 4")
        time.sleep(0.25)

        top10_metrics = cdp.eval_js("""(() => ({
          countText: document.getElementById('top10-shortlisted-count')?.textContent.trim(),
          cards: document.querySelectorAll('#top10-card-list .top10-card').length,
          hasTypeBadge: Boolean(document.querySelector('#top10-card-list .badge-cluster, #top10-card-list .badge-standalone')),
          hasModeSwitch: Boolean(document.getElementById('btn-top10-mode-present')),
          copyBriefBtns: document.querySelectorAll('#top10-card-list [data-copy-brief]').length,
          holidayCountdownBadges: document.querySelectorAll('#top10-card-list [data-holiday-countdown]').length,
          generateVariantsBtns: document.querySelectorAll('#top10-card-list [data-generate-variants]').length,
          anomalyBadges: document.querySelectorAll('#top10-card-list .badge-cred-anomaly, #top10-card-list .badge-cred-trimmed').length,
          photos: document.querySelectorAll('#top10-card-list .top10-photo-tile').length,
          supportingRows: document.querySelectorAll('#top10-card-list tbody tr').length,
          jsonHref: document.getElementById('btn-export-json')?.getAttribute('href') || '',
          csvHref: document.getElementById('btn-export-csv')?.getAttribute('href') || '',
          overflow: document.documentElement.scrollWidth > window.innerWidth,
        }))()""")
        assert top10_metrics["countText"] == "1" and top10_metrics["cards"] == 1 and top10_metrics["hasTypeBadge"] is True
        assert top10_metrics["hasModeSwitch"] and top10_metrics["copyBriefBtns"] == 1, f"Expected Stage 06 mode switch and copy brief button: {top10_metrics}"
        assert top10_metrics["holidayCountdownBadges"] == 1 and top10_metrics["generateVariantsBtns"] == 1, f"Expected holiday countdown badge and variant generator button: {top10_metrics}"
        assert top10_metrics["anomalyBadges"] > 0, f"Expected anomaly / outlier-trim badges in Stage 06: {top10_metrics}"
        assert top10_metrics["photos"] == 4 and top10_metrics["supportingRows"] == 4
        assert not top10_metrics["overflow"]

        # Verify Presentation Mode toggles cleanly (hides remove-shortlist button) and switches back
        cdp.eval_js("document.getElementById('btn-top10-mode-present').click()")
        wait_for(cdp, "document.getElementById('top10-root')?.classList.contains('is-presentation') && !document.querySelector('#top10-card-list [data-remove-shortlist]')")
        cdp.eval_js("document.getElementById('btn-top10-mode-edit').click()")
        wait_for(cdp, "!document.getElementById('top10-root')?.classList.contains('is-presentation') && Boolean(document.querySelector('#top10-card-list [data-remove-shortlist]'))")

        # Verify on-demand Generate Variants dry-run modal opens cleanly (0 external calls) & closes on Escape
        cdp.eval_js("""document.querySelector('#top10-card-list [data-generate-variants]').click()""")
        wait_for(cdp, "document.getElementById('review-modal-overlay')?.classList.contains('is-open') && (document.getElementById('review-modal-title')?.textContent || '').includes('变体建议')")
        cdp.eval_js("""document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))""")
        wait_for(cdp, "!document.getElementById('review-modal-overlay')?.classList.contains('is-open')")

        # Verify actual HTTP export endpoints from the live URLs on the page
        with urlopen(f"http://127.0.0.1:{port}{top10_metrics['jsonHref']}", timeout=5) as r:
            exp_j = json.loads(r.read().decode("utf-8"))
        assert exp_j["total_directions"] == 1 and "原创可爱猫咪文案二创" in exp_j["directions"][0]["notes"]
        with urlopen(f"http://127.0.0.1:{port}{top10_metrics['csvHref']}", timeout=5) as r:
            exp_c = r.read()
        assert exp_c.startswith(b"\xef\xbb\xbf") and "复古圣诞节日文字梗" in exp_c.decode("utf-8-sig")

        shot_top10 = SCREENSHOT_DIR / "top10-desktop-1440.png"
        cdp.screenshot(shot_top10, full_page=True)
        report["screenshots"].append(str(shot_top10))

        # Check Stage 06 responsive 768px & 390px zero overflow
        cdp.set_viewport(768, 1024, mobile=False)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Top10 768px overflow"
        cdp.set_viewport(390, 844, mobile=True)
        time.sleep(0.2)
        assert not cdp.eval_js("document.documentElement.scrollWidth > window.innerWidth"), "Top10 390px overflow"
        cdp.set_viewport(1440, 900, mobile=False)

        # Click "移出入选" -> verifies real-time 06 empty state ("0 / 10" + jump to 05 button)
        cdp.eval_js("""document.querySelector('#top10-card-list [data-remove-shortlist]').click()""")
        wait_for(cdp, "Boolean(document.getElementById('top10-empty-state')) && document.getElementById('top10-shortlisted-count')?.textContent.trim() === '0'")
        report["checks"]["stage06_top10"] = {
          **top10_metrics,
          "inline_note_saved": True,
          "json_export_verified": True,
          "csv_bom_export_verified": True,
          "remove_shortlist_syncs_to_empty_state": True,
        }

        # 6. Tablet 768x1024 — Market Dashboard
        cdp.eval_js("""document.querySelector('.nav-item[data-route="dashboard"]').click()""")
        wait_for(cdp, "Boolean(document.querySelector('#chart-sales-hist svg path'))")
        cdp.set_viewport(768, 1024, mobile=False)
        time.sleep(0.3)
        tablet_check = cdp.eval_js("""(() => ({
          innerWidth: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          overflow: document.documentElement.scrollWidth > window.innerWidth,
          menuToggleVisible: getComputedStyle(document.getElementById('menu-toggle')).display !== 'none',
        }))()""")
        report["checks"]["tablet_768"] = tablet_check
        assert not tablet_check["overflow"], f"Tablet 768px horizontal overflow: {tablet_check}"
        assert tablet_check["menuToggleVisible"]
        shot_tab = SCREENSHOT_DIR / "dashboard-tablet-768.png"
        cdp.screenshot(shot_tab, full_page=False)
        report["screenshots"].append(str(shot_tab))

        # 7. Mobile 390x844 — Market Dashboard (expand collapsible table to verify inner scroll)
        cdp.set_viewport(390, 844, mobile=True)
        time.sleep(0.2)
        cdp.eval_js("""(() => {
          const body = document.getElementById('dash-table-collapse-body');
          if (body && body.hidden) document.getElementById('btn-toggle-product-table')?.click();
        })()""")
        wait_for(cdp, "!document.getElementById('dash-table-collapse-body')?.hidden")
        time.sleep(0.2)
        mobile_check = cdp.eval_js("""(() => {
          const tableWrap = document.querySelector('#product-table-body-wrap .table-scroll');
          return {
            innerWidth: window.innerWidth,
            scrollWidth: document.documentElement.scrollWidth,
            overflow: document.documentElement.scrollWidth > window.innerWidth,
            tableScrollable: tableWrap ? tableWrap.scrollWidth > tableWrap.clientWidth : false,
          };
        })()""")
        report["checks"]["mobile_390"] = mobile_check
        assert not mobile_check["overflow"], f"Mobile 390px horizontal overflow: {mobile_check}"
        assert mobile_check["tableScrollable"], "Table should scroll horizontally inside its wrapper on 390px"
        shot_mob = SCREENSHOT_DIR / "dashboard-mobile-390.png"
        cdp.screenshot(shot_mob, full_page=False)
        report["screenshots"].append(str(shot_mob))

        report["checks"]["js_runtime_exceptions"] = len(cdp.exceptions)
        assert len(cdp.exceptions) == 0, f"Unexpected JS runtime exceptions: {cdp.exceptions}"

        db_hash_after = hashlib.sha256(DEFAULT_DB_PATH.read_bytes()).hexdigest()
        report["checks"]["original_db_immutable"] = (db_hash_before == db_hash_after)
        assert db_hash_before == db_hash_after, "Original SQLite DB hash changed!"

        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if cdp:
            cdp.close()
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_proc.kill()
        shutil.rmtree(chrome_profile, ignore_errors=True)
        shutil.rmtree(temp_app_db_dir, ignore_errors=True)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
