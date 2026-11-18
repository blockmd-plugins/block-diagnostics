#!/usr/bin/env python3
from datetime import datetime
import html as html_lib
import json
import os
import posixpath
import re
import shutil
import sys
from urllib.parse import unquote

DEFAULT_CONFIG = {
    "scan_scope": "all",
    "check_orphan_assets": True,
    "check_broken_links": True,
    "check_empty_notes": True,
    "large_asset_threshold": "5mb",
    "cleanup_dir": "",
    "report_format": "html",
}

THRESHOLD_BYTES = {
    "1mb": 1024 * 1024,
    "5mb": 5 * 1024 * 1024,
    "10mb": 10 * 1024 * 1024,
}

REPORT_FORMAT_OPTIONS = [
    {"value": "html", "label": "HTML"},
    {"value": "md", "label": "Markdown"},
]

SKIP_DIRS = {"node_modules", "__pycache__"}
MD_LINK_RE = re.compile(r'(!?\[[^\]]*\]\()([^)\n]+)(\))')
HTML_REF_RE = re.compile(r'''(?:src|href)\s*=\s*["']([^"']+)["']''', re.IGNORECASE)
WIKI_LINK_RE = re.compile(r'(!?\[\[)([^\]\n]+)(\]\])')

MAX_LOG_ENTRIES = 50


class HostBridge:
    def __init__(self):
        self._seq = 1000

    def send(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call_host(self, method, params=None):
        self._seq += 1
        req_id = f"p-{self._seq}"
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("stdin closed while awaiting host response")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if str(msg.get("id")) == req_id:
                if "error" in msg and msg["error"]:
                    raise RuntimeError(msg["error"].get("message", "host error"))
                return msg.get("result", {})

    def serve(self, handler):
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            try:
                result = handler(self, method, req.get("params") or {})
                self.send({"jsonrpc": "2.0", "id": req_id, "result": result})
                if method == "plugin.shutdown":
                    break
            except Exception as exc:
                self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}})


class DiagnosticsPlugin:
    def __init__(self):
        self.notebook_root = ""
        self.downloads_dir = os.path.expanduser("~/Downloads")
        self.locale = "zh"

    def initialize(self, params):
        self.notebook_root = params.get("notebook_root") or ""
        self.downloads_dir = params.get("downloads_dir") or os.path.expanduser("~/Downloads")
        self.locale = params.get("locale") or "zh"
        return {"ready": True}

    def discover_scope_options(self, notes_meta):
        options = [{"value": "all", "label": "整个笔记本"}]
        subdirs = set()
        if self.notebook_root and os.path.isdir(self.notebook_root):
            try:
                for name in os.listdir(self.notebook_root):
                    if name.startswith(".") or name in SKIP_DIRS:
                        continue
                    if os.path.isdir(os.path.join(self.notebook_root, name)):
                        subdirs.add(name)
            except OSError:
                pass
        for item in notes_meta:
            rel = (item.get("path") or "").replace("\\", "/").lstrip("/")
            if "/" in rel:
                top = rel.split("/", 1)[0]
                if top and not top.startswith(".") and top not in SKIP_DIRS:
                    subdirs.add(top)
        for d in sorted(subdirs):
            options.append({"value": f"dir:{d}", "label": f"仅目录 · {d}/"})
        return options

    def resolve_cleanup_dir(self, config):
        chosen = config["cleanup_dir"]
        if chosen:
            return os.path.abspath(os.path.expanduser(chosen))
        return os.path.join(self.downloads_dir, "Block-Orphan-Assets")

    def resolve_report_path(self, config):
        fname = "诊断报告.md" if config.get("report_format") == "md" else "诊断报告.html"
        return os.path.join(self.downloads_dir, fname)

    def load_state(self, bridge, override_values=None):
        res = bridge.call_host("host.storage.get", {"key": "diagnostics_state"})
        saved = res.get("value") if res.get("found") and isinstance(res.get("value"), dict) else {}
        raw_cfg = dict(saved.get("config") or {})
        if isinstance(override_values, dict) and override_values:
            raw_cfg.update(override_values)
        entries = normalize_entries(saved.get("entries") or [])
        last_status = saved.get("status") if isinstance(saved.get("status"), str) and saved.get("status") else "已就绪"
        return raw_cfg, entries, last_status

    def save_state(self, bridge, config, entries, status):
        bridge.call_host(
            "host.storage.set",
            {
                "key": "diagnostics_state",
                "value": {
                    "config": config,
                    "entries": entries[:MAX_LOG_ENTRIES],
                    "status": status,
                },
            },
        )

    def collect_workspace_data(self, bridge, read_contents=True):
        notes_res = bridge.call_host("host.notes.list", {})
        notes_meta = notes_res.get("notes") or []
        note_contents = {}
        note_char_counts = {}
        note_byte_counts = {}

        for item in notes_meta:
            path = item.get("path") or ""
            norm_path = normalize_rel_path(path) if path else ""
            size = item.get("size")
            if read_contents and path:
                read_res = bridge.call_host("host.notes.read", {"path": path})
                content = read_res.get("content") or ""
                note_contents[path] = content
                note_char_counts[norm_path] = len(content)
                note_byte_counts[norm_path] = size if isinstance(size, int) and size >= 0 else len(content.encode("utf-8"))
            elif norm_path:
                note_char_counts[norm_path] = 0
                note_byte_counts[norm_path] = size if isinstance(size, int) and size >= 0 else 0

        assets = scan_assets(self.notebook_root)
        return notes_meta, note_contents, note_char_counts, note_byte_counts, assets

    def analyze_workspace(self, notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config):
        scope = config.get("scan_scope") or "all"
        scope_prefix = scope[4:] + "/" if scope.startswith("dir:") else ""

        def in_scope(rel_norm):
            if not scope_prefix:
                return True
            return rel_norm.startswith(scope_prefix)

        note_paths_norm = {normalize_rel_path(item.get("path") or "") for item in notes_meta if item.get("path")}
        referenced_paths = set()
        referenced_basenames = set()
        broken_links = []
        empty_notes = []

        for note_path, content in note_contents.items():
            note_norm = normalize_rel_path(note_path)
            if in_scope(note_norm) and content.strip() == "":
                empty_notes.append(note_norm)

            targets = extract_link_targets(content)
            for raw_target, is_wiki in targets:
                resolved, candidate_name = resolve_relative_target(note_norm, raw_target, is_wiki)
                if resolved is not None:
                    referenced_paths.add(resolved)
                if candidate_name:
                    referenced_basenames.add(candidate_name)

                if in_scope(note_norm) and not target_exists(
                    self.notebook_root,
                    note_norm,
                    raw_target,
                    is_wiki,
                    note_paths_norm,
                    assets,
                ):
                    broken_links.append((note_norm, raw_target))

        scoped_notes = [p for p in note_paths_norm if in_scope(p)]
        scoped_assets = [a for a in assets if in_scope(a["rel_path"])]

        orphan_assets = []
        for asset in scoped_assets:
            rel_norm = asset["rel_path"]
            base_name = posixpath.basename(rel_norm)
            if rel_norm not in referenced_paths and base_name not in referenced_basenames:
                orphan_assets.append(asset)

        threshold_limit = THRESHOLD_BYTES.get(config["large_asset_threshold"], THRESHOLD_BYTES["5mb"])
        large_assets = [a for a in scoped_assets if a["size"] >= threshold_limit]

        total_chars = sum(note_char_counts.get(p, 0) for p in scoped_notes)
        md_bytes = sum(note_byte_counts.get(p, 0) for p in scoped_notes)
        asset_bytes = sum(a["size"] for a in scoped_assets)

        metrics = {
            "note_count": len(scoped_notes),
            "total_chars": total_chars,
            "md_bytes": md_bytes,
            "asset_count": len(scoped_assets),
            "asset_bytes": asset_bytes,
            "total_bytes": md_bytes + asset_bytes,
        }

        findings = {
            "large_assets": large_assets,
            "empty_notes": empty_notes if config["check_empty_notes"] else [],
            "broken_links": broken_links if config["check_broken_links"] else [],
            "orphan_assets": orphan_assets if config["check_orphan_assets"] else [],
            "all_orphan_assets": orphan_assets,
        }
        return metrics, findings

    def build_issues_list(self, findings, now_str):
        issues = []
        for asset in findings["large_assets"]:
            issues.append(
                {
                    "title": f"大体积附件：{asset['rel_path']}（{format_bytes(asset['size'])}）",
                    "time": now_str,
                    "status": "体积超限",
                }
            )
        for note_norm in findings["empty_notes"]:
            issues.append(
                {
                    "title": f"空白笔记：{note_norm}",
                    "time": now_str,
                    "status": "空内容",
                }
            )
        for note_norm, raw_target in findings["broken_links"]:
            issues.append(
                {
                    "title": f"失效链接：{note_norm} -> {raw_target}",
                    "time": now_str,
                    "status": "目标缺失",
                }
            )
        for asset in findings["orphan_assets"]:
            issues.append(
                {
                    "title": f"孤立附件：{asset['rel_path']}（{format_bytes(asset['size'])}）",
                    "time": now_str,
                    "status": "未引用",
                }
            )
        return issues

    def build_preview_item(self, metrics, findings, config):
        report_path = self.resolve_report_path(config)
        orphan_list = findings["orphan_assets"]
        broken_list = findings["broken_links"]
        empty_list = findings["empty_notes"]
        large_list = findings["large_assets"]

        total_issues = len(orphan_list) + len(broken_list) + len(empty_list) + len(large_list)
        lines = []

        if config["check_orphan_assets"]:
            if orphan_list:
                sample = ", ".join(a["rel_path"] for a in orphan_list[:3])
                more = f" 等 {len(orphan_list)} 个" if len(orphan_list) > 3 else ""
                lines.append(f"孤立附件：{len(orphan_list)} 个（{sample}{more}）")
            else:
                lines.append("孤立附件：0 个")
        else:
            lines.append("孤立附件检测：已关闭")

        if config["check_broken_links"]:
            if broken_list:
                sample = ", ".join(f"{n} -> {t}" for n, t in broken_list[:2])
                more = f" 等 {len(broken_list)} 处" if len(broken_list) > 2 else ""
                lines.append(f"失效链接：{len(broken_list)} 处（{sample}{more}）")
            else:
                lines.append("失效链接：0 处")
        else:
            lines.append("失效链接检测：已关闭")

        if config["check_empty_notes"]:
            if empty_list:
                sample = ", ".join(empty_list[:3])
                more = f" 等 {len(empty_list)} 篇" if len(empty_list) > 3 else ""
                lines.append(f"空白笔记：{len(empty_list)} 篇（{sample}{more}）")
            else:
                lines.append("空白笔记：0 篇")
        else:
            lines.append("空白笔记检测：已关闭")

        if large_list:
            sample = ", ".join(f"{a['rel_path']} ({format_bytes(a['size'])})" for a in large_list[:3])
            more = f" 等 {len(large_list)} 个" if len(large_list) > 3 else ""
            lines.append(f"超限大文件：{len(large_list)} 个（{sample}{more}）")
        else:
            lines.append("超限大文件：0 个")

        if metrics["note_count"] == 0 and metrics["asset_count"] == 0:
            lines.append("当前工作区暂无笔记与附件。")

        badge = f"发现 {total_issues} 项待处理" if total_issues > 0 else "工作区健康"
        return {
            "kind": "preview",
            "preview": {
                "variant": "plan",
                "palette": "ocean",
                "title": "实时诊断预览 · 待处理清单",
                "badge": badge,
                "subtitle": f"报告：{os.path.basename(report_path)}",
                "body": "\n".join(lines),
            },
        }

    def run_scan(self, bridge, config):
        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        now_str = datetime.now().strftime("%H:%M:%S")
        issues = self.build_issues_list(findings, now_str)

        if not issues:
            entries = [
                {
                    "title": f"巡检完成：{metrics['note_count']} 篇笔记、{metrics['asset_count']} 个附件均健康",
                    "time": now_str,
                    "status": "通过",
                }
            ]
            status = "巡检通过 · 未发现异常"
        else:
            issue_count = len(issues)
            entries = issues[:MAX_LOG_ENTRIES]
            if issue_count > MAX_LOG_ENTRIES:
                entries[-1] = {
                    "title": f"共发现 {issue_count} 项问题，当前展示前 {MAX_LOG_ENTRIES - 1} 项",
                    "time": now_str,
                    "status": "已截断",
                }
            status = f"巡检完成 · 发现 {issue_count} 项问题"

        return metrics, findings, entries, status

    def archive_orphans(self, bridge, config, old_entries):
        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        orphan_assets = findings["all_orphan_assets"]
        now_str = datetime.now().strftime("%H:%M:%S")

        if not self.notebook_root or not orphan_assets:
            entry = {
                "title": "附件检查：未发现需要移出的孤立附件",
                "time": now_str,
                "status": "健康",
            }
            entries = ([entry] + old_entries)[:MAX_LOG_ENTRIES]
            status = "工作区健康 · 暂无孤立附件需要移出"
            return metrics, findings, entries, status, ""

        nb_real = os.path.realpath(self.notebook_root)
        dest_root = self.resolve_cleanup_dir(config)
        os.makedirs(dest_root, exist_ok=True)

        moved_count = 0
        moved_bytes = 0
        archive_entries = []

        for asset in orphan_assets:
            rel_norm = asset["rel_path"]
            src_abs = os.path.realpath(os.path.join(nb_real, rel_norm))
            if not src_abs.startswith(nb_real + os.sep) or not os.path.isfile(src_abs):
                continue

            sub_rel = rel_norm[len("assets/"):] if rel_norm.startswith("assets/") else rel_norm
            dst_abs = os.path.join(dest_root, sub_rel)
            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
            if os.path.exists(dst_abs):
                stem, ext = os.path.splitext(dst_abs)
                idx = 1
                while os.path.exists(f"{stem}_{idx}{ext}"):
                    idx += 1
                dst_abs = f"{stem}_{idx}{ext}"

            try:
                shutil.move(src_abs, dst_abs)
                moved_count += 1
                moved_bytes += asset["size"]
                archive_entries.append(
                    {
                        "title": f"已移出孤立附件：{rel_norm} -> {dst_abs}",
                        "time": now_str,
                        "status": "已移出",
                    }
                )
            except OSError as exc:
                archive_entries.append(
                    {
                        "title": f"移出失败：{rel_norm}（{exc}）",
                        "time": now_str,
                        "status": "失败",
                    }
                )

        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        entries = (archive_entries + old_entries)[:MAX_LOG_ENTRIES]
        if moved_count > 0:
            status = f"已安全移出 {moved_count} 个孤立附件至 {dest_root}（{format_bytes(moved_bytes)}）"
            return metrics, findings, entries, status, dest_root
        status = "移出未完成，请检查文件权限"
        return metrics, findings, entries, status, ""

    def export_report(self, bridge, config, old_entries):
        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        now_str = datetime.now().strftime("%H:%M:%S")
        out_path = self.resolve_report_path(config)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        if config["report_format"] == "md":
            content = render_markdown_report(metrics, findings, config, self.notebook_root)
        else:
            content = render_html_report(metrics, findings, config, self.notebook_root)

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)

        entry = {
            "title": f"导出诊断报告：{out_path}",
            "time": now_str,
            "status": "已导出",
        }
        entries = ([entry] + old_entries)[:MAX_LOG_ENTRIES]
        status = f"诊断报告已导出至 {out_path}"
        return metrics, findings, entries, status, out_path

    def build_page(self, metrics, findings, config, entries, status, scope_options):
        preview_item = self.build_preview_item(metrics, findings, config)
        return {
            "title": "性能与诊断",
            "status": status,
            "header_actions": [
                {"id": "scan_now", "label": "立即巡检", "variant": "primary"},
            ],
            "cards": [
                {
                    "id": "workspace-metrics",
                    "title": "工作区核心指标",
                    "items": [
                        {
                            "kind": "metrics",
                            "metrics": [
                                {"label": "笔记篇数", "value": f"{metrics['note_count']} 篇"},
                                {"label": "总字符数", "value": f"{metrics['total_chars']:,}"},
                                {"label": "附件文件数", "value": f"{metrics['asset_count']} 个"},
                                {"label": "工作区总体积", "value": format_bytes(metrics["total_bytes"])},
                            ],
                        }
                    ],
                },
                {
                    "id": "health-checks",
                    "title": "巡检范围、规则与清理目标",
                    "items": [
                        {
                            "kind": "select",
                            "id": "scan_scope",
                            "label": "巡检范围",
                            "value": config["scan_scope"],
                            "options": scope_options,
                        },
                        {
                            "kind": "switch",
                            "id": "check_orphan_assets",
                            "label": "检测未被引用的孤立附件",
                            "value": config["check_orphan_assets"],
                        },
                        {
                            "kind": "switch",
                            "id": "check_broken_links",
                            "label": "检测失效相对链接",
                            "value": config["check_broken_links"],
                        },
                        {
                            "kind": "switch",
                            "id": "check_empty_notes",
                            "label": "检测空白笔记",
                            "value": config["check_empty_notes"],
                        },
                        {
                            "kind": "segmented",
                            "id": "large_asset_threshold",
                            "label": "大体积附件提醒阈值",
                            "value": config["large_asset_threshold"],
                            "options": [
                                {"value": "1mb", "label": "1 MB"},
                                {"value": "5mb", "label": "5 MB"},
                                {"value": "10mb", "label": "10 MB"},
                            ],
                        },
                        {
                            "kind": "folder",
                            "id": "cleanup_dir",
                            "label": "孤立附件移出目录",
                            "value": self.resolve_cleanup_dir(config),
                        },
                        {
                            "kind": "segmented",
                            "id": "report_format",
                            "label": "诊断报告格式",
                            "value": config["report_format"],
                            "options": REPORT_FORMAT_OPTIONS,
                        },
                        {
                            "kind": "actions",
                            "buttons": [
                                {"id": "save_config", "label": "保存配置", "variant": "secondary"},
                                {"id": "export_report", "label": "导出诊断报告", "variant": "primary"},
                                {"id": "archive_orphans", "label": "安全移出孤立附件", "variant": "secondary"},
                            ],
                        },
                    ],
                },
                {
                    "id": "preview-card",
                    "title": "实时诊断预览",
                    "items": [
                        preview_item,
                    ],
                },
                {
                    "id": "diagnostics-log",
                    "title": "巡检报告明细",
                    "items": [
                        {
                            "kind": "log_list",
                            "empty_text": "暂无巡检记录，点击右上角「立即巡检」开始检查",
                            "entries": entries,
                        }
                    ],
                },
            ],
        }

    def render(self, bridge, params):
        values = params.get("values") if isinstance(params.get("values"), dict) else None
        raw_cfg, entries, status = self.load_state(bridge, override_values=values)
        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        scope_options = self.discover_scope_options(notes_meta)
        config = normalize_config(raw_cfg, scope_options)
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        return {"page": self.build_page(metrics, findings, config, entries, status, scope_options)}

    def action(self, bridge, params):
        action_id = params.get("action_id") or ""
        values = params.get("values") if isinstance(params.get("values"), dict) else {}
        raw_cfg, entries, status = self.load_state(bridge, override_values=values)
        notes_meta, note_contents, note_char_counts, note_byte_counts, assets = self.collect_workspace_data(
            bridge, read_contents=True
        )
        scope_options = self.discover_scope_options(notes_meta)
        config = normalize_config(raw_cfg, scope_options)

        if action_id == "scan_now":
            metrics, findings, entries, status = self.run_scan(bridge, config)
            self.save_state(bridge, config, entries, status)
            page = self.build_page(metrics, findings, config, entries, status, scope_options)
            return {"values": config, "page": page, "status": status}

        if action_id == "archive_orphans":
            metrics, findings, entries, status, moved_dir = self.archive_orphans(bridge, config, entries)
            self.save_state(bridge, config, entries, status)
            page = self.build_page(metrics, findings, config, entries, status, scope_options)
            res = {"values": config, "page": page, "status": status}
            if moved_dir:
                res["reveal_path"] = moved_dir
            return res

        if action_id == "export_report":
            metrics, findings, entries, status, out_path = self.export_report(bridge, config, entries)
            self.save_state(bridge, config, entries, status)
            page = self.build_page(metrics, findings, config, entries, status, scope_options)
            res = {"values": config, "page": page, "status": status}
            if config["report_format"] == "md":
                res["reveal_path"] = out_path
            else:
                res["open_path"] = out_path
            return res

        if action_id == "save_config":
            status = "已保存巡检配置"
            self.save_state(bridge, config, entries, status)
            metrics, findings = self.analyze_workspace(
                notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
            )
            page = self.build_page(metrics, findings, config, entries, status, scope_options)
            return {"values": config, "page": page, "status": status}

        self.save_state(bridge, config, entries, status)
        metrics, findings = self.analyze_workspace(
            notes_meta, note_contents, note_char_counts, note_byte_counts, assets, config
        )
        page = self.build_page(metrics, findings, config, entries, status, scope_options)
        return {"values": config, "page": page, "status": status}


def normalize_config(raw, scope_options=None):
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    scope_values = {opt["value"] for opt in (scope_options or [{"value": "all"}])}
    if raw.get("scan_scope") in scope_values:
        cfg["scan_scope"] = raw["scan_scope"]

    if isinstance(raw.get("check_orphan_assets"), bool):
        cfg["check_orphan_assets"] = raw["check_orphan_assets"]
    if isinstance(raw.get("check_broken_links"), bool):
        cfg["check_broken_links"] = raw["check_broken_links"]
    elif isinstance(raw.get("check_broken_links_and_empty"), bool):
        cfg["check_broken_links"] = raw["check_broken_links_and_empty"]
    if isinstance(raw.get("check_empty_notes"), bool):
        cfg["check_empty_notes"] = raw["check_empty_notes"]
    elif isinstance(raw.get("check_broken_links_and_empty"), bool):
        cfg["check_empty_notes"] = raw["check_broken_links_and_empty"]

    threshold = raw.get("large_asset_threshold")
    if threshold in THRESHOLD_BYTES:
        cfg["large_asset_threshold"] = threshold

    cleanup_dir = raw.get("cleanup_dir")
    if cleanup_dir is None and raw.get("cleanup_dest") == "custom":
        cleanup_dir = raw.get("cleanup_custom_dir")
    if isinstance(cleanup_dir, str):
        cfg["cleanup_dir"] = cleanup_dir

    if raw.get("report_format") in ("html", "md"):
        cfg["report_format"] = raw["report_format"]

    return cfg


def normalize_entries(raw):
    entries = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        time_str = item.get("time")
        if isinstance(title, str) and title and isinstance(time_str, str) and time_str:
            entry = {"title": title, "time": time_str}
            status = item.get("status")
            if isinstance(status, str) and status:
                entry["status"] = status
            entries.append(entry)
    return entries[:MAX_LOG_ENTRIES]


def format_bytes(size):
    size = max(0, int(size))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def normalize_rel_path(path):
    cleaned = path.replace("\\", "/").lstrip("/")
    return posixpath.normpath(cleaned)


def scan_assets(notebook_root):
    if not notebook_root or not os.path.isdir(notebook_root):
        return []
    result = []
    root_real = os.path.realpath(notebook_root)
    for root, dirnames, files in os.walk(root_real):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".") and d not in SKIP_DIRS]
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in (".md", ".markdown"):
                continue
            full_path = os.path.join(root, name)
            if not os.path.isfile(full_path):
                continue
            rel = os.path.relpath(full_path, root_real)
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            result.append({"rel_path": normalize_rel_path(rel), "size": size})
    return result


def strip_link_decorations(raw):
    s = raw.strip()
    if s.startswith("<") and ">" in s:
        s = s[1:s.index(">")].strip()
    else:
        parts = s.split()
        if parts:
            s = parts[0]
    s = s.split("#", 1)[0].split("?", 1)[0].strip()
    return unquote(s)


def is_external_or_anchor(target):
    if not target or target.startswith("#"):
        return True
    lower = target.lower()
    return (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("mailto:")
        or lower.startswith("tel:")
        or lower.startswith("data:")
        or lower.startswith("javascript:")
    )


def extract_link_targets(content):
    targets = []
    for match in MD_LINK_RE.finditer(content):
        cleaned = strip_link_decorations(match.group(2))
        if not is_external_or_anchor(cleaned):
            targets.append((cleaned, False))
    for match in HTML_REF_RE.finditer(content):
        cleaned = strip_link_decorations(match.group(1))
        if not is_external_or_anchor(cleaned):
            targets.append((cleaned, False))
    for match in WIKI_LINK_RE.finditer(content):
        inner = match.group(2).split("|", 1)[0].split("#", 1)[0].strip()
        if inner and not is_external_or_anchor(inner):
            targets.append((unquote(inner), True))
    return targets


def resolve_relative_target(note_norm, raw_target, is_wiki):
    clean = raw_target.replace("\\", "/")
    if is_wiki:
        norm = posixpath.normpath(clean.lstrip("/"))
        return norm, posixpath.basename(norm)
    if clean.startswith("/"):
        norm = posixpath.normpath(clean.lstrip("/"))
    else:
        base_dir = posixpath.dirname(note_norm)
        norm = posixpath.normpath(posixpath.join(base_dir, clean)) if base_dir and base_dir != "." else posixpath.normpath(clean)
    return norm, None


def target_exists(notebook_root, note_norm, raw_target, is_wiki, note_paths_norm, assets):
    asset_paths = {a["rel_path"] for a in assets}
    asset_basenames = {posixpath.basename(a["rel_path"]) for a in assets}
    resolved, candidate_name = resolve_relative_target(note_norm, raw_target, is_wiki)

    if resolved in note_paths_norm or f"{resolved}.md" in note_paths_norm:
        return True
    if resolved in asset_paths:
        return True
    if is_wiki and candidate_name:
        if candidate_name in asset_basenames:
            return True
        if any(posixpath.basename(p) == candidate_name or posixpath.basename(p) == f"{candidate_name}.md" for p in note_paths_norm):
            return True

    if notebook_root and not resolved.startswith(".."):
        root_real = os.path.realpath(notebook_root)
        candidate_fs = os.path.realpath(os.path.join(root_real, resolved))
        if candidate_fs == root_real or candidate_fs.startswith(root_real + os.sep):
            if os.path.exists(candidate_fs):
                return True
            if not resolved.endswith(".md") and os.path.exists(candidate_fs + ".md"):
                return True
    return False


def render_markdown_report(metrics, findings, config, notebook_root):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scope = config["scan_scope"][4:] if config["scan_scope"].startswith("dir:") else "整个笔记本"
    lines = [
        "# Block 工作区性能与诊断报告",
        "",
        f"- 生成时间：{now}",
        f"- 笔记本路径：{notebook_root or '未打开笔记本'}",
        f"- 巡检范围：{scope}",
        "",
        "## 工作区核心指标",
        "",
        f"- 笔记篇数：{metrics['note_count']} 篇",
        f"- 总字符数：{metrics['total_chars']:,}",
        f"- 附件文件数：{metrics['asset_count']} 个",
        f"- 工作区总体积：{format_bytes(metrics['total_bytes'])}",
        "",
        "## 巡检发现明细",
        "",
    ]

    sections = [
        ("孤立附件", [f"`{a['rel_path']}` ({format_bytes(a['size'])})" for a in findings["orphan_assets"]]),
        ("失效链接", [f"`{n}` -> `{t}`" for n, t in findings["broken_links"]]),
        ("空白笔记", [f"`{n}`" for n in findings["empty_notes"]]),
        ("超限大文件", [f"`{a['rel_path']}` ({format_bytes(a['size'])})" for a in findings["large_assets"]]),
    ]
    for title, items in sections:
        lines.append(f"### {title}（{len(items)}）")
        lines.append("")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- 无异常")
        lines.append("")
    return "\n".join(lines)


def render_html_report(metrics, findings, config, notebook_root):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scope = config["scan_scope"][4:] if config["scan_scope"].startswith("dir:") else "整个笔记本"

    def render_list(items):
        if not items:
            return '<p class="empty">无异常</p>'
        return "<ul>" + "".join(f"<li>{html_lib.escape(i)}</li>" for i in items) + "</ul>"

    orphan_items = [f"{a['rel_path']} ({format_bytes(a['size'])})" for a in findings["orphan_assets"]]
    broken_items = [f"{n} -> {t}" for n, t in findings["broken_links"]]
    empty_items = list(findings["empty_notes"])
    large_items = [f"{a['rel_path']} ({format_bytes(a['size'])})" for a in findings["large_assets"]]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Block 工作区诊断报告</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f8fb; color: #1f2937; margin: 0; padding: 32px 20px; }}
  .wrap {{ max-width: 860px; margin: 0 auto; }}
  .card {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  h2 {{ font-size: 16px; margin: 0 0 12px; color: #0f172a; }}
  .meta {{ color: #64748b; font-size: 13px; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 12px; }}
  .metric {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; }}
  .metric-label {{ font-size: 12px; color: #64748b; }}
  .metric-val {{ font-size: 18px; font-weight: 600; margin-top: 4px; color: #0f172a; }}
  ul {{ margin: 0; padding-left: 20px; line-height: 1.7; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
  .empty {{ color: #16a34a; font-size: 13px; margin: 0; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Block 工作区性能与诊断报告</h1>
    <div class="meta">生成时间：{html_lib.escape(now)} ｜ 范围：{html_lib.escape(scope)} ｜ 路径：{html_lib.escape(notebook_root or '未打开笔记本')}</div>
    <div class="grid">
      <div class="metric"><div class="metric-label">笔记篇数</div><div class="metric-val">{metrics['note_count']} 篇</div></div>
      <div class="metric"><div class="metric-label">总字符数</div><div class="metric-val">{metrics['total_chars']:,}</div></div>
      <div class="metric"><div class="metric-label">附件文件数</div><div class="metric-val">{metrics['asset_count']} 个</div></div>
      <div class="metric"><div class="metric-label">工作区总体积</div><div class="metric-val">{html_lib.escape(format_bytes(metrics['total_bytes']))}</div></div>
    </div>
  </div>
  <div class="card"><h2>孤立附件（{len(orphan_items)}）</h2>{render_list(orphan_items)}</div>
  <div class="card"><h2>失效链接（{len(broken_items)}）</h2>{render_list(broken_items)}</div>
  <div class="card"><h2>空白笔记（{len(empty_items)}）</h2>{render_list(empty_items)}</div>
  <div class="card"><h2>超限大文件（{len(large_items)}）</h2>{render_list(large_items)}</div>
</div>
</body>
</html>
"""


PLUGIN = DiagnosticsPlugin()


def handle_request(bridge, method, params):
    if method == "plugin.initialize":
        return PLUGIN.initialize(params)
    if method == "plugin.render":
        return PLUGIN.render(bridge, params)
    if method == "plugin.action":
        return PLUGIN.action(bridge, params)
    if method == "plugin.shutdown":
        return {"ok": True}
    raise RuntimeError(f"unsupported method: {method}")


if __name__ == "__main__":
    HostBridge().serve(handle_request)
