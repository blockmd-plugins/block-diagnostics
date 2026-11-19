#!/usr/bin/env python3
"""Block official Performance & Usage plugin (block-diagnostics).

Speaks bidirectional JSON-RPC 2.0 over stdin/stdout and displays live Block CPU,
physical memory (RSS), and the 7-category disk-usage Squarified Treemap.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


class HostRPC:
    def __init__(self) -> None:
        self._next_id = 1

    def send(self, msg: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call_host(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req_id = f"p-{self._next_id}"
        self._next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            line = sys.stdin.readline()
            if not line:
                raise EOFError("stdin closed while waiting for host response")
            line = line.strip()
            if not line:
                continue
            env = json.loads(line)
            if str(env.get("id")) == req_id:
                if "error" in env and env["error"]:
                    raise RuntimeError(env["error"].get("message", "host rpc error"))
                return env.get("result")


def format_bytes_iec(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    val = float(max(0, int(num_bytes)))
    idx = 0
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(val)} B"
    return f"{val:.1f} {units[idx]}"


def format_percent(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    pct = (float(part) / float(total)) * 100.0
    if part > 0 and pct < 0.01:
        return "<0.01%"
    return f"{pct:.1f}%"


class PerformancePlugin:
    def __init__(self, rpc: HostRPC) -> None:
        self.rpc = rpc
        self.locale = "zh"
        self.notebook_root = ""
        self.capabilities: set[str] = set()
        self.has_scanned = False

    def is_en(self) -> bool:
        return self.locale.lower().startswith("en")

    def tr(self, zh: str, en: str) -> str:
        return en if self.is_en() else zh

    def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.locale = str(params.get("locale") or "zh")
        self.notebook_root = str(params.get("notebook_root") or "")
        caps = params.get("ui_capabilities") or []
        self.capabilities = {str(c) for c in caps}
        self.has_scanned = False
        return {"ready": True}

    def fetch_perf_snapshot(self, refresh_disk: bool) -> dict[str, Any] | None:
        if "host_perf" not in self.capabilities:
            return None
        try:
            res = self.rpc.call_host(
                "host.perf.snapshot", {"refresh_disk": bool(refresh_disk)}
            )
            if isinstance(res, dict):
                return res
        except Exception:
            return None
        return None

    def build_fallback_snapshot(self) -> dict[str, Any]:
        notes_bytes = 0
        assets_bytes = 0
        root = self.notebook_root
        if root and os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    try:
                        if os.path.islink(full):
                            continue
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    lower = fname.lower()
                    if lower.endswith((".md", ".markdown", ".mdown")):
                        notes_bytes += size
                    else:
                        assets_bytes += size
        total = notes_bytes + assets_bytes
        return {
            "cpu_percent": 0.0,
            "rss_bytes": 0,
            "total_bytes": total,
            "last_scan": time.strftime("%H:%M:%S"),
            "scan_error": "",
            "categories": [
                {
                    "id": "notes",
                    "label": self.tr("笔记", "Notes"),
                    "detail": self.tr("Markdown 文档", "Markdown documents"),
                    "bytes": notes_bytes,
                },
                {
                    "id": "assets",
                    "label": self.tr("资源图片", "Assets & Images"),
                    "detail": self.tr("图片与资源文件", "Images & assets"),
                    "bytes": assets_bytes,
                },
            ],
        }

    def build_page(self, refresh_disk: bool) -> dict[str, Any]:
        snap = self.fetch_perf_snapshot(refresh_disk=refresh_disk)
        live_supported = snap is not None
        if snap is None:
            snap = self.build_fallback_snapshot()

        total_bytes = int(snap.get("total_bytes") or 0)
        rss_bytes = int(snap.get("rss_bytes") or 0)
        cpu_pct = float(snap.get("cpu_percent") or 0.0)
        last_scan = str(snap.get("last_scan") or "").strip() or "—"
        scan_error = str(snap.get("scan_error") or "").strip()
        raw_cats = snap.get("categories") or []

        metrics = [
            {
                "label": self.tr("磁盘占用", "Storage"),
                "value": format_bytes_iec(total_bytes),
            },
            {
                "label": self.tr("物理内存", "Memory RSS"),
                "value": format_bytes_iec(rss_bytes) if live_supported else "—",
            },
            {
                "label": "CPU",
                "value": f"{cpu_pct:.1f}%" if live_supported else "—",
            },
            {
                "label": self.tr("统计时间", "Scanned"),
                "value": last_scan,
            },
        ]

        storage_items: list[dict[str, Any]] = []
        total_files = 0
        if "treemap" in self.capabilities and raw_cats:
            tiles = []
            for cat in raw_cats:
                cid = str(cat.get("id") or "").strip()
                label = str(cat.get("label") or cid).strip()
                detail = str(cat.get("detail") or "").strip()
                cbytes = max(0, int(cat.get("bytes") or 0))
                file_count = max(0, int(cat.get("file_count") or 0))
                total_files += file_count
                if not cid or not label:
                    continue
                entries = []
                for ent in cat.get("entries") or []:
                    ename = str(ent.get("name") or "").strip()
                    epath = str(ent.get("path") or "").strip()
                    ebytes = max(0, int(ent.get("bytes") or 0))
                    ecount = max(0, int(ent.get("count") or 0))
                    if not ename:
                        continue
                    item_ent: dict[str, Any] = {"name": ename, "bytes": ebytes}
                    if epath:
                        item_ent["path"] = epath
                    if ecount > 1:
                        item_ent["count"] = ecount
                    entries.append(item_ent)
                tile_obj: dict[str, Any] = {
                    "id": cid,
                    "label": label,
                    "detail": detail,
                    "bytes": cbytes,
                    "color": cid,
                }
                if file_count > 0:
                    tile_obj["file_count"] = file_count
                if entries:
                    tile_obj["entries"] = entries
                tiles.append(tile_obj)
            if tiles:
                sub_text = (
                    f"{total_files} files"
                    if self.is_en()
                    else f"{total_files} 个文件"
                ) if total_files > 0 else ""
                summary: dict[str, Any] = {
                    "total_label": self.tr("磁盘总占用", "Total Storage"),
                    "total_value": format_bytes_iec(total_bytes),
                    "metrics": [
                        {
                            "label": self.tr("物理内存", "Memory RSS"),
                            "value": format_bytes_iec(rss_bytes) if live_supported else "—",
                        },
                        {
                            "label": "CPU",
                            "value": f"{cpu_pct:.1f}%" if live_supported else "—",
                        },
                        {
                            "label": self.tr("统计时间", "Scanned"),
                            "value": last_scan,
                        },
                    ],
                }
                if sub_text:
                    summary["sub_text"] = sub_text
                storage_items.append(
                    {
                        "kind": "treemap",
                        "id": "disk-breakdown",
                        "total_bytes": total_bytes,
                        "summary": summary,
                        "tiles": tiles,
                    }
                )
        else:
            rows = []
            for cat in raw_cats:
                label = str(cat.get("label") or cat.get("id") or "").strip()
                cbytes = max(0, int(cat.get("bytes") or 0))
                if not label:
                    continue
                rows.append(
                    {
                        "label": label,
                        "value": f"{format_bytes_iec(cbytes)} ({format_percent(cbytes, total_bytes)})",
                    }
                )
            if rows:
                storage_items.append({"kind": "kv", "rows": rows})

        if scan_error:
            storage_items.append(
                {
                    "kind": "kv",
                    "rows": [
                        {
                            "label": self.tr("读取提示", "Scan Notice"),
                            "value": scan_error,
                            "tooltip": scan_error,
                        }
                    ],
                }
            )

        if "treemap" in self.capabilities and storage_items:
            cards = [
                {
                    "id": "storage",
                    "title": "",
                    "items": storage_items,
                }
            ]
        else:
            cards = [
                {
                    "id": "overview",
                    "title": self.tr("实时占用", "Live Usage"),
                    "items": [{"kind": "metrics", "metrics": metrics}],
                },
                {
                    "id": "storage",
                    "title": self.tr("磁盘空间分布", "Storage Breakdown"),
                    "items": storage_items,
                },
            ]

        page: dict[str, Any] = {
            "title": self.tr("性能与占用", "Performance & Usage"),
            "header_actions": [
                {
                    "id": "refresh",
                    "label": self.tr("刷新", "Refresh"),
                    "variant": "secondary",
                }
            ],
            "cards": cards,
        }
        if live_supported:
            page["refresh_interval_ms"] = 1000
        return page

    def handle_render(self, params: dict[str, Any]) -> dict[str, Any]:
        first_scan = not self.has_scanned
        self.has_scanned = True
        return {"page": self.build_page(refresh_disk=first_scan)}

    def handle_action(self, params: dict[str, Any]) -> dict[str, Any]:
        action_id = str(params.get("action_id") or "")
        if action_id == "refresh":
            self.has_scanned = True
            page = self.build_page(refresh_disk=True)
            status = self.tr("已刷新实时占用与磁盘分布", "Refreshed live usage and storage breakdown")
            page["status"] = status
            return {
                "ok": True,
                "tone": "success",
                "status": status,
                "page": page,
            }
        return {"ok": True, "tone": "info", "page": self.build_page(refresh_disk=False)}


def main() -> None:
    rpc = HostRPC()
    plugin = PerformancePlugin(rpc)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        try:
            if method == "plugin.initialize":
                result = plugin.handle_initialize(params)
            elif method == "plugin.render":
                result = plugin.handle_render(params)
            elif method == "plugin.action":
                result = plugin.handle_action(params)
            elif method == "plugin.shutdown":
                rpc.send({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
                return
            else:
                rpc.send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"unknown method: {method}",
                        },
                    }
                )
                continue
            rpc.send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            rpc.send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )


if __name__ == "__main__":
    main()
