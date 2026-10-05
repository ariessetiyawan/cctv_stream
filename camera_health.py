"""
camera_health.py — Diagnostik menyeluruh semua kamera.

Cek 4 lapis:
  1. DB         : kamera terdaftar & aktif
  2. RTSP       : bisa connect ke DVR
  3. MediaMTX   : path ready & track terdeteksi
  4. HLS        : manifest bisa di-fetch

Output: tabel status + ringkasan channel bermasalah.

Jalankan:
  python camera_health.py             # cek semua kamera
  python camera_health.py --channel 21  # cek 1 channel
  python camera_health.py --json      # output JSON
  python camera_health.py --watch 30  # refresh tiap 30 detik
"""
import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
from datetime import datetime

import requests

from config import Config
import db as dbmod

# ----------------------------------------------------------------
# Kredensial MediaMTX (baca dari Config jika ada)
# ----------------------------------------------------------------
MTX_API  = getattr(Config, "MEDIAMTX_API",  "http://127.0.0.1:9997")
MTX_HLS  = getattr(Config, "MEDIAMTX_HLS",  "http://127.0.0.1:8888")
MTX_USER = getattr(Config, "MEDIAMTX_USER", "flask_proxy")
MTX_PASS = getattr(Config, "MEDIAMTX_PASS", "Tr@nsdigi53$_proxy")
MTX_AUTH = (MTX_USER, MTX_PASS)

# Timeout (detik)
RTSP_TIMEOUT = 6
MTX_TIMEOUT  = 5
HLS_TIMEOUT  = 8


# ================================================================
# Helper
# ================================================================
def _probe_rtsp(rtsp_url: str) -> dict:
    """Cek RTSP via ffprobe. Return info codec/resolusi atau error."""
    try:
        p = subprocess.run(
            ["ffprobe",
             "-rtsp_transport", "tcp",
             "-timeout", str(RTSP_TIMEOUT * 1_000_000),
             "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1",
             "-i", rtsp_url],
            capture_output=True, text=True,
            timeout=RTSP_TIMEOUT + 4,
        )
        if p.returncode == 0 and p.stdout.strip():
            info = {}
            for line in p.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            return {
                "ok": True,
                "codec":  info.get("codec_name", "?"),
                "width":  int(info.get("width", 0)),
                "height": int(info.get("height", 0)),
            }
        return {"ok": False, "error": p.stderr.strip().splitlines()[-1][:120] if p.stderr else "unknown"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe tidak ditemukan (install FFmpeg)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def _get_mtx_paths() -> dict:
    """Ambil daftar path dari MediaMTX. Return {name: path_info}."""
    try:
        r = requests.get(f"{MTX_API}/v3/paths/list", timeout=MTX_TIMEOUT)
        if r.status_code != 200:
            return {}
        items = r.json().get("items", [])
        return {p["name"]: p for p in items}
    except Exception:
        return {}


def _check_hls_manifest(mtx_path: str) -> dict:
    """Cek manifest HLS via HTTP GET."""
    url = f"{MTX_HLS}/{mtx_path}/index.m3u8"
    try:
        r = requests.get(url, auth=MTX_AUTH, timeout=HLS_TIMEOUT)
        if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
            return {"ok": True, "url": url}
        return {
            "ok": False,
            "status": r.status_code,
            "error": r.text[:120] if r.text else f"HTTP {r.status_code}",
            "url": url,
        }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "MediaMTX tidak dapat dihubungi", "url": url}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "HLS timeout", "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "url": url}


# ================================================================
# Analisis satu kamera
# ================================================================
def analyze_camera(cam: dict, mtx_paths: dict) -> dict:
    """
    Cek 1 kamera lengkap. Return dict status.
    """
    result = {
        "id":       cam.get("id"),
        "name":     cam.get("name"),
        "channel":  cam.get("channel"),
        "dvr":      cam.get("dvr"),
        "mtx_path": cam.get("mtx_path"),
        "is_active": cam.get("is_active"),
        "issues":   [],
        "layer_status": {
            "db":       "ok",
            "rtsp":     "skip",
            "mediamtx": "skip",
            "hls":      "skip",
        },
        "codec":    None,
        "resolution": None,
    }

    # ---------- Lapis 1: DB ----------
    if not cam.get("is_active"):
        result["issues"].append("Kamera dinonaktifkan di database (is_active = 0)")
        result["layer_status"]["db"] = "fail"
        return result  # tidak perlu cek lanjut

    if not cam.get("rtsp_url"):
        result["issues"].append("RTSP URL kosong di database")
        result["layer_status"]["db"] = "fail"
        return result

    if not cam.get("mtx_path"):
        result["issues"].append("mtx_path kosong — belum di-sync ke MediaMTX")
        result["layer_status"]["db"] = "warn"

    # ---------- Lapis 2: RTSP ----------
    rtsp_res = _probe_rtsp(cam["rtsp_url"])
    if not rtsp_res["ok"]:
        result["issues"].append(f"RTSP gagal: {rtsp_res.get('error', '?')}")
        result["layer_status"]["rtsp"] = "fail"
        # kalau RTSP gagal, lapis berikutnya pasti gagal juga — skip
        return result

    result["layer_status"]["rtsp"] = "ok"
    result["codec"]      = rtsp_res["codec"]
    result["resolution"] = f"{rtsp_res['width']}x{rtsp_res['height']}"

    # ---------- Lapis 3: MediaMTX ----------
    mtx_path = cam.get("mtx_path")
    if not mtx_path:
        result["issues"].append("Tidak punya mtx_path → tidak akan muncul di HLS")
        return result

    mtx_info = mtx_paths.get(mtx_path)
    if not mtx_info:
        result["issues"].append(f"Path '{mtx_path}' tidak ada di MediaMTX")
        result["layer_status"]["mediamtx"] = "fail"
        return result

    if not mtx_info.get("ready"):
        result["issues"].append(f"Path '{mtx_path}' belum ready di MediaMTX")
        result["layer_status"]["mediamtx"] = "fail"
        return result

    tracks = mtx_info.get("tracks") or []
    if not tracks:
        result["issues"].append("MediaMTX tidak mendeteksi track video")
        result["layer_status"]["mediamtx"] = "fail"
        return result

    result["layer_status"]["mediamtx"] = "ok"
    result["mtx_tracks"] = tracks

    # ---------- Lapis 4: HLS ----------
    hls_res = _check_hls_manifest(mtx_path)
    if not hls_res["ok"]:
        error = hls_res.get("error", "?")
        result["issues"].append(f"HLS gagal: {error}")

        # Diagnosa tambahan berdasarkan error
        if hls_res.get("status") == 500:
            # HTTP 500 → biasanya masalah codec / muxer
            if any("H265" in t or "HEVC" in t for t in tracks):
                result["issues"].append(
                    "Kemungkinan: H.265 tidak didukung. "
                    "Cek hlsVariant harus 'fmp4' di mediamtx.yml, "
                    "atau upgrade MediaMTX ke v1.15.5+"
                )
            else:
                result["issues"].append(
                    "Muxer error. Cek log MediaMTX — kemungkinan bug DTS "
                    "(upgrade ke v1.15.5+)"
                )
        elif hls_res.get("status") == 401:
            result["issues"].append(
                "401 Unauthorized — cek authInternalUsers di mediamtx.yml "
                "(action: read harus ada)"
            )
        elif hls_res.get("status") == 404:
            result["issues"].append("Manifest 404 — path mungkin belum ter-register")

        result["layer_status"]["hls"] = "fail"
        return result

    result["layer_status"]["hls"] = "ok"
    return result


# ================================================================
# Analisis semua kamera
# ================================================================
def analyze_all(channel_filter=None, workers=6) -> dict:
    """
    Analisa semua kamera. Return dict dengan hasil per kamera & ringkasan.
    """
    started = time.time()
    result = {
        "timestamp":      datetime.now().isoformat(timespec="seconds"),
        "total":          0,
        "ok":             0,
        "failed":         0,
        "cameras":        [],
        "failed_list":    [],
    }

    # Ambil data dari DB
    try:
        cameras = dbmod.fetch_all_cameras()
    except Exception as e:
        return {
            "error": f"Gagal membaca DB: {e}",
            "timestamp": result["timestamp"],
        }

    if channel_filter:
        cameras = [c for c in cameras if c.get("channel") == channel_filter]

    result["total"] = len(cameras)

    # Ambil daftar path MediaMTX sekali saja
    mtx_paths = _get_mtx_paths()

    # Analisa paralel
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(analyze_camera, cam, mtx_paths): cam for cam in cameras}
        for f in concurrent.futures.as_completed(futures):
            try:
                r = f.result()
                result["cameras"].append(r)
                if r["issues"]:
                    result["failed"] += 1
                    result["failed_list"].append(r)
                else:
                    result["ok"] += 1
            except Exception as e:
                cam = futures[f]
                result["failed"] += 1
                result["failed_list"].append({
                    "id": cam.get("id"),
                    "name": cam.get("name"),
                    "issues": [f"Exception: {e}"],
                })

    # Urutkan by channel
    result["cameras"].sort(key=lambda c: c.get("channel") or 0)
    result["failed_list"].sort(key=lambda c: c.get("channel") or 0)

    result["elapsed_sec"] = round(time.time() - started, 2)
    return result


# ================================================================
# Output
# ================================================================
def print_report(result: dict):
    """Print hasil analisa ke terminal dengan format warna."""
    # ANSI colors (fallback aman di Windows)
    RESET  = "\033[0m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"

    print()
    print(f"{CYAN}{'='*72}{RESET}")
    print(f"{CYAN}  CAMERA HEALTH REPORT  —  {result['timestamp']}{RESET}")
    print(f"{CYAN}{'='*72}{RESET}")
    print(f"  Total: {result['total']}  |  "
          f"{GREEN}OK: {result['ok']}{RESET}  |  "
          f"{RED}Failed: {result['failed']}{RESET}  |  "
          f"({result.get('elapsed_sec', '?')}s)")
    print()

    # Tabel kamera OK
    if result["ok"]:
        print(f"{GREEN}✅ KAMERA OK ({result['ok']}){RESET}")
        print(f"  {'CH':>3}  {'Name':<35} {'Codec':<7} {'Resolusi':<12} Path")
        print(f"  {'-'*3}  {'-'*35} {'-'*7} {'-'*12} {'-'*30}")
        for c in result["cameras"]:
            if c["issues"]:
                continue
            name = (c["name"] or "")[:35]
            print(f"  {c['channel']:>3}  {name:<35} "
                  f"{c.get('codec','?'):<7} {c.get('resolution','?'):<12} "
                  f"{c.get('mtx_path') or '-'}")
        print()

    # Tabel kamera gagal
    if result["failed"]:
        print(f"{RED}❌ KAMERA BERMASALAH ({result['failed']}){RESET}")
        for c in result["failed_list"]:
            name = (c["name"] or "")[:50]
            print(f"\n  {RED}CH {c['channel']:>3}  —  {name}{RESET}")
            print(f"    DVR      : {c.get('dvr') or '-'}")
            print(f"    mtx_path : {c.get('mtx_path') or '-'}")
            print(f"    Layer    : ", end="")
            for layer, status in c.get("layer_status", {}).items():
                color = GREEN if status == "ok" else RED if status == "fail" else YELLOW if status == "warn" else GRAY
                print(f"{color}{layer}={status}{RESET} ", end="")
            print()
            print(f"    Issues   :")
            for issue in c["issues"]:
                print(f"      • {issue}")
        print()

    # Rekomendasi
    print(f"{CYAN}{'='*72}{RESET}")
    if result["failed"] == 0:
        print(f"{GREEN}  ✅ Semua kamera berfungsi normal.{RESET}")
    else:
        print(f"{YELLOW}  📋 REKOMENDASI PERBAIKAN:{RESET}")
        issues_text = " ".join(
            issue
            for c in result["failed_list"]
            for issue in c["issues"]
        )
        if "H.265" in issues_text or "hevc" in issues_text.lower():
            print(f"  • Ganti hlsVariant ke 'fmp4' di mediamtx.yml")
        if "DTS" in issues_text or "Muxer" in issues_text:
            print(f"  • Upgrade MediaMTX ke v1.15.5+ (bug DTS regresi)")
        if "401" in issues_text:
            print(f"  • Tambahkan 'action: read' di authInternalUsers")
        if "tidak ada di MediaMTX" in issues_text:
            print(f"  • Jalankan: POST /api/mediamtx/sync")
        if "is_active = 0" in issues_text:
            print(f"  • Aktifkan kamera di DB (is_active = 1)")
        if "RTSP gagal" in issues_text:
            print(f"  • Cek DVR: kamera terpasang, kabel, IP, kredensial")
    print(f"{CYAN}{'='*72}{RESET}\n")


# ================================================================
# Main (CLI)
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="Diagnostik kesehatan kamera CCTV")
    parser.add_argument("--channel", type=int, help="Cek 1 channel saja")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--watch", type=int, metavar="SEC", help="Refresh setiap N detik")
    args = parser.parse_args()

    def run_once():
        result = analyze_all(channel_filter=args.channel)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print_report(result)
        return result

    if args.watch:
        try:
            while True:
                run_once()
                print(f"  (refresh dalam {args.watch}s — Ctrl+C untuk stop)")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nBye.")
    else:
        run_once()


if __name__ == "__main__":
    main()