"""Explicit Desktop captures with manual review or opt-in local pixel diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
from urllib.parse import quote
import uuid
import zipfile
import xml.etree.ElementTree as ET

from powerbi_import import desktop_probe, desktop_window, tableau_desktop_probe
from powerbi_import.desktop_navigation import capture_document_tab
from powerbi_import.html_template import html_open, html_close, data_table, badge
from powerbi_import.report_localization import LocalizedText, text, render, attributes as i18n_attrs
from powerbi_import.local_pixel_comparison import (
    PIXEL_LIMITATION, compare_png_files, unavailable_pixels,
)
from powerbi_import.tableau_desktop_probe import (
    CAPTURE_LIMITATION, document_title_matches, probe_tableau_open,
)


def inventory_tableau_pages(workbook_path: str, *, extraction_dir: str | None = None) -> list[dict]:
    """Dashboards, standalone worksheets and stories; never extract archive paths.

    Optional extraction snapshots use the existing dashboards/worksheets/stories
    JSON lists. XML is size bounded and DTD/entity declarations are forbidden.
    Embedded worksheets are not standalone tabs but remain recorded on dashboards.
    """
    records = []
    used = set()
    if extraction_dir is not None:
        root = Path(extraction_dir)
        groups = {}
        for kind, filename in (("dashboard", "dashboards.json"),
                               ("worksheet", "worksheets.json"), ("story", "stories.json")):
            path = root / filename
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise ValueError(f"Invalid extraction list: {path}")
            groups[kind] = data
        for dashboard in groups["dashboard"]:
            for obj in dashboard.get("objects", []):
                if isinstance(obj, dict) and obj.get("type") in ("worksheet", "sheet", "worksheetReference"):
                    used.add(obj.get("worksheetName", obj.get("name")))
        for kind, data in groups.items():
            for item in data:
                if kind != "worksheet" or item.get("name") not in used:
                    records.append({"kind": kind, "name": item.get("name", ""),
                                    "hidden": item.get("hidden", False),
                                    "inventory_evidence": str(root)})
    else:
        path = Path(workbook_path)
        limit = 64 * 1024 * 1024
        if path.suffix.lower() == ".twbx":
            with zipfile.ZipFile(path) as archive:
                entries = [i for i in archive.infolist() if i.filename.lower().endswith(".twb")]
                if len(entries) != 1:
                    raise ValueError("Packaged workbook has missing or ambiguous TWB")
                if entries[0].file_size > limit:
                    raise ValueError("Workbook XML exceeds size limit")
                raw = archive.read(entries[0])
        else:
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Workbook XML exceeds size limit")
        # Also inspect UTF-16/32 XML declarations without handing them to a parser.
        declarations = raw.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
            raise ValueError("DTD/entity declarations are not permitted in workbook XML")
        root = ET.fromstring(raw)
        # Strip namespaces locally; do not alter or save the source workbook.
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        dashboards = root.findall("./dashboards/dashboard")
        stories = root.findall(".//story")
        story_names = {s.get("name") for s in stories}
        for dashboard in dashboards:
            used.update(z.get("name") for z in dashboard.findall(".//zone") if z.get("name"))
        hidden = {w.get("name") for w in root.findall(".//window")
                  if w.get("hidden", "").lower() in ("true", "1")}
        for kind, elements in (("dashboard", dashboards),
                               ("worksheet", root.findall("./worksheets/worksheet")),
                               ("story", stories)):
            for element in elements:
                name = element.get("name", "")
                if kind == "worksheet" and name in used:
                    continue
                if kind == "dashboard" and name in story_names:
                    continue
                records.append({"kind": kind, "name": name,
                                "hidden": name in hidden or element.get("hidden", "").lower() == "true",
                                "inventory_evidence": str(path)})
    for index, record in enumerate(records):
        if not isinstance(record["name"], str) or not record["name"]:
            raise ValueError("Source page has no valid name")
        record["id"] = f"source-{index:04d}"
    return records


def inventory_powerbi_pages(pbip_path: str) -> list[dict]:
    """Inventory every PBIR page, including hidden/tooltip and unordered pages.

    Only the PBIP's report references are followed, confined to its project.
    """
    pbip = Path(pbip_path).resolve()
    project = pbip.parent
    data = json.loads(pbip.read_text(encoding="utf-8"))
    reports = [a["report"]["path"] for a in data.get("artifacts", [])
               if isinstance(a, dict) and isinstance(a.get("report"), dict)]
    if len(reports) != 1 or not isinstance(reports[0], str):
        raise ValueError("PBIP must reference exactly one report for page comparison")
    report = (project / reports[0]).resolve()
    if not report.is_relative_to(project):
        raise ValueError("Report reference escapes project")
    pages_dir = report / "definition" / "pages"
    if not pages_dir.is_dir():
        raise ValueError("PBIR pages directory is missing")
    metadata_path = pages_dir / "pages.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    order = metadata.get("pageOrder", [])
    records = []
    for path in sorted(pages_dir.glob("*/page.json")):
        if not path.resolve().is_relative_to(project):
            raise ValueError("Page path escapes project")
        try:
            page = json.loads(path.read_text(encoding="utf-8"))
            name = page.get("name")
            display = page.get("displayName")
            if not isinstance(name, str) or not name or not isinstance(display, str) or not display:
                raise ValueError(f"Page lacks name/displayName: {path}")
            visibility = page.get("visibility", page.get("pageVisibility", "visible"))
            # Current PBIR stores page type as a constant expression under objects.
            tooltip = page.get("type") == "Tooltip" or page.get("pageType") == "Tooltip"
            for obj in page.get("objects", {}).get("pageInformation", []):
                value = obj.get("properties", {}).get("type", {})
                tooltip |= value.get("expr", {}).get("Literal", {}).get("Value") in ("'Tooltip'", '"Tooltip"')
            records.append({"id": name, "name": display, "kind": "page",
                            "hidden": str(visibility).casefold() in ("hidden", "1", "true"),
                            "visibility": visibility, "tooltip": bool(tooltip),
                            "ordered": name in order, "page_json": str(path),
                            "inventory_evidence": str(path)})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # Keep the file in inventory, but its directory name is NOT a tab
            # label and may not be used as mapping/selection evidence.
            records.append({"id": "invalid:" + path.parent.name,
                            "name": f"[Invalid page metadata: {path.parent.name}]",
                            "kind": "page", "hidden": None, "tooltip": None,
                            "visibility": "unknown", "ordered": False,
                            "page_json": str(path), "inventory_evidence": str(path),
                            "inventory_error": f"{type(exc).__name__}: {exc}"})
    records.sort(key=lambda x: (order.index(x["id"]) if x["id"] in order else len(order), x["id"]))
    if not records:
        raise ValueError("No PBIR pages found")
    if len({r["id"] for r in records}) != len(records):
        raise ValueError("Duplicate PBIR page names")
    return records


def map_desktop_pages(sources: list[dict], targets: list[dict],
                      lineage: dict | None = None) -> list[dict]:
    """One-to-one mapping supported by exact names or explicit page lineage.

    Worksheet-to-visual/bookmark lineage is not treated as page correspondence.
    Duplicate names and conflicting lineage remain ambiguous, never fuzzy matched.
    """
    links = []
    for group in ("pages", "dashboards", "worksheets", "stories"):
        entries = (lineage or {}).get(group, [])
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("pbi_page"), str):
                continue
            kind = {"pages": "page", "dashboards": "dashboard",
                    "worksheets": "worksheet", "stories": "story"}[group]
            source_name = entry.get(f"tableau_{kind}", entry.get("tableau_page"))
            if isinstance(source_name, str):
                links.append((source_name, entry["pbi_page"], group, kind))
    rows = []
    for target in targets:
        candidates = {}
        for source in sources:
            evidence = []
            if source.get("inventory_error") or target.get("inventory_error"):
                continue
            if source["name"] == target["name"]:
                evidence.append("exact_name")
            for src, dst, group, kind in links:
                if src == source["name"] and dst in (target["name"], target["id"]):
                    if group == "pages" or source["kind"] == kind:
                        evidence.append(f"lineage_map:{group}")
            if evidence:
                candidates[source["id"]] = evidence
        ids = sorted(candidates)
        rows.append({"target_id": target["id"],
                     "source_id": ids[0] if len(ids) == 1 else None,
                     "status": "mapped" if len(ids) == 1 else "ambiguous" if ids else "unmatched_target",
                     "candidate_source_ids": ids, "evidence": candidates})
    claims = {}
    for row in rows:
        if row["source_id"]:
            claims.setdefault(row["source_id"], []).append(row)
    for claimants in claims.values():
        if len(claimants) > 1:
            for row in claimants:
                row.update(source_id=None, status="ambiguous")
    represented = {sid for row in rows for sid in row["candidate_source_ids"]}
    rows.extend({"target_id": None, "source_id": s["id"], "status": "unmatched_source",
                 "candidate_source_ids": [], "evidence": {}}
                for s in sources if s["id"] not in represented)
    return rows


def _launched_window(result: dict, stem: str):
    """Resolve only the probe's launched PID, not a pre-existing document."""
    pid = result.get("pid")
    if not pid or result.get("status") != "captured":
        return None
    windows = [w for w in desktop_window.windows_for_pid(pid)
               if not w.owned and document_title_matches(w.title, stem)
               and w.width >= 400 and w.height >= 300]
    return windows[0] if len(windows) == 1 else None


def _local_capture_pixels(source: dict, target: dict,
                          source_path: Path | None, target_path: Path | None,
                          *, pages: bool = False) -> dict:
    """Accept only verified successes at the destinations requested in this run."""
    for side, capture, expected in (("source", source, source_path),
                                    ("target", target, target_path)):
        if capture.get("status") != "captured" or not capture.get("screenshot"):
            return unavailable_pixels("capture_unavailable",
                                      f"{side}: no current successful capture")
        if capture.get("screenshot_blank") is not False:
            return unavailable_pixels("capture_unverified",
                                      f"{side}: nonblank capture not verified")
        if pages and any(not isinstance(capture.get(key), dict)
                         or capture[key].get("status") != "verified"
                         for key in ("selection", "post_capture_selection")):
            return unavailable_pixels("capture_unverified",
                                      f"{side}: selected tab not verified before and after capture")
        try:
            path = Path(capture["screenshot"])
            _safe_destination(path.absolute())
            if expected is None or path.resolve() != expected.resolve():
                return unavailable_pixels("capture_not_current",
                                          f"{side}: capture is not the requested current-run path")
            if not path.is_file():
                return unavailable_pixels("image_missing", f"{side}: capture file is missing")
        except (OSError, ValueError, TypeError) as exc:
            return unavailable_pixels("capture_not_current",
                                      f"{side}: invalid capture path: {exc}")
    return compare_png_files(source_path, target_path)


def _capture_all_pages(row: dict, shots: Path, page_timeout: float,
                       *, screen_comparison: str = "manual") -> None:
    """Reuse the two initial launches; isolate inventories and individual tabs."""
    inventories = {}
    for field, function, path in (
            ("source", inventory_tableau_pages, row["source"]),
            ("target", inventory_powerbi_pages, row.get("pbip"))):
        try:
            inventories[field] = function(path) if path else []
        except Exception as exc:
            inventories[field] = []
            row["issues"].append(f"{field} inventory failed: {type(exc).__name__}: {exc}")
    row["source_inventory"] = inventories["source"]
    row["target_inventory"] = inventories["target"]
    for pages in inventories.values():
        for page in pages:
            page["capture"] = {"status": "not_requested", "screenshot": None,
                               "note": "Page workflow did not reach this page"}
    lineage = {}
    if row.get("pbip"):
        lineage_path = Path(row["pbip"]).parent / "lineage_map.json"
        if lineage_path.is_file():
            try:
                lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
                if not isinstance(lineage, dict):
                    raise ValueError("Lineage must be an object")
            except Exception as exc:
                row["issues"].append(f"Lineage read failed: {exc}")
                lineage = {}
    row["page_mappings"] = map_desktop_pages(inventories["source"], inventories["target"], lineage)
    requested_paths = {"tableau": {}, "powerbi": {}}
    for field, inventory, stem, subdir in (
            ("tableau", inventories["source"], Path(row["source"]).stem, "source"),
            ("powerbi", inventories["target"], Path(row["pbip"]).stem if row.get("pbip") else "", "output")):
        try:
            window = _launched_window(row[field], stem)
        except Exception as exc:
            row["issues"].append(f"{field} document resolution failed: {type(exc).__name__}: {exc}")
            window = None
        for index, page in enumerate(inventory):
            if page.get("inventory_error"):
                page["capture"] = {"status": "error", "screenshot": None,
                                   "note": page["inventory_error"]}
                continue
            slug = re.sub(r"[^\w.-]", "_", page["name"])[:70] or "page"
            filename = f"{index:04d}-{slug}-{uuid.uuid4().hex[:10]}.png"
            if window is None:
                page["capture"] = {"status": "inaccessible", "screenshot": None,
                                   "note": "No uniquely verified launched document window"}
                continue
            try:
                requested = shots / subdir / filename
                requested_paths[field][page["id"]] = requested
                page["capture"] = capture_document_tab(
                    window.hwnd, row[field]["pid"], page["name"],
                    str(requested), timeout=page_timeout)
            except Exception as exc:
                page["capture"] = {"status": "error", "screenshot": None,
                                   "note": f"{type(exc).__name__}: {exc}"}
            if page["capture"]["status"] != "captured" and (page.get("hidden") or page.get("tooltip")):
                page["capture"]["note"] += " (hidden/tooltip page; no state changes to expose it)"
    if screen_comparison == "local":
        sources = {p["id"]: p for p in inventories["source"]}
        targets = {p["id"]: p for p in inventories["target"]}
        for mapping in row["page_mappings"]:
            source = sources.get(mapping["source_id"])
            target = targets.get(mapping["target_id"])
            if row["pair_status"] != "paired" or mapping["status"] != "mapped" or not source or not target:
                result = unavailable_pixels("page_not_matched",
                                            "No unambiguous workbook/page correspondence")
            else:
                result = _local_capture_pixels(
                    source["capture"], target["capture"],
                    requested_paths["tableau"].get(source["id"]),
                    requested_paths["powerbi"].get(target["id"]), pages=True)
            mapping["pixel_comparison"] = result
    mapped = all(m["status"] == "mapped" for m in row["page_mappings"])
    captured = all(p["capture"]["status"] == "captured"
                   for items in inventories.values() for p in items)
    row["status"] = ("manual_review" if mapped and captured and inventories["source"]
                     and inventories["target"] and not row["issues"] else "failed")


def _cleanup_comparison_processes(row: dict) -> None:
    """Close only processes launched by the probes, after all workbook captures."""
    for field, closer in (
        ("tableau", tableau_desktop_probe.close_probe_process),
        ("powerbi", desktop_probe.close_probe_process),
    ):
        result = row[field]
        pid = result.get("pid")
        if not isinstance(pid, int):
            result["cleanup"] = {
                "status": "not_started", "note": "No launched PID was reported"}
            continue
        try:
            cleanup = closer(pid)
        except Exception as exc:
            cleanup = {"status": "error",
                       "note": f"{type(exc).__name__}: {exc}"}
        result["cleanup"] = cleanup
        if cleanup.get("status") not in ("closed", "already_exited"):
            row["issues"].append(
                f"{field} cleanup {cleanup.get('status', 'error')}: "
                f"{cleanup.get('note', 'No cleanup detail')}")


def _sorted_files(root: Path, pattern: str) -> list[Path]:
    return sorted((p.resolve() for p in root.rglob(pattern) if p.is_file()),
                  key=lambda p: (str(p).casefold(), str(p)))


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _safe_destination(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError(f"Output path contains a link or junction: {component}")


def pair_workbooks(source_dir: str, output_root: str) -> list[dict]:
    """Pair recursively, using exact batch layout and batch quality-summary maps.

    Batch uses relative parent / workbook stem / stem.pbip. Server-download
    sanitization (non word/dot/hyphen -> underscore) is also accepted at that
    exact relative location, never by a fuzzy/global basename search.
    All candidates are retained: conflicts and many-to-one matches are errors.
    """
    source = Path(source_dir).resolve()
    output = Path(output_root).resolve()
    if not source.is_dir() or not output.is_dir():
        raise ValueError("source directory and generated output root must be directories")
    workbooks = sorted((p.resolve() for p in source.rglob("*")
                        if p.is_file() and p.suffix.lower() in (".twb", ".twbx")),
                       key=lambda p: (str(p).casefold(), str(p)))
    pbips = {p for p in _sorted_files(output, "*")
             if p.suffix.lower() == ".pbip" and _inside(p, output)}
    summaries = []
    for summary in _sorted_files(output, "batch_quality_summary.json"):
        if not _inside(summary, output):
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            mapping = data["workbooks"]
            if not isinstance(mapping, dict):
                raise ValueError("workbooks must be an object")
            summaries.append((summary, mapping, None))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summaries.append((summary, {}, f"Invalid quality summary {summary}: {exc}"))
    rows = []
    for workbook in workbooks:
        if not _inside(workbook, source):
            continue
        relative = workbook.relative_to(source)
        candidates = set()
        reasons = []
        names = {workbook.stem, re.sub(r"[^\w\-.]", "_", workbook.stem)}
        for name in names:
            expected = output / relative.parent / name / (name + ".pbip")
            if expected.resolve() in pbips:
                candidates.add(expected.resolve())
        key = relative.with_suffix("").as_posix()
        for summary, mapping, error in summaries:
            if error:
                reasons.append(error)
                continue
            # Summary keys are relative to the batch root containing the summary.
            prefix = summary.parent.relative_to(output)
            try:
                local = Path(key).relative_to(prefix).as_posix()
            except ValueError:
                continue
            entries = [v for k, v in mapping.items()
                       if str(k).replace("\\", "/").casefold() == local.casefold()]
            for entry in entries:
                try:
                    raw = Path(entry["json_path"])
                    quality_paths = {raw.resolve()} if raw.is_absolute() else {
                        raw.resolve(), (summary.parent / raw).resolve(),
                        (output / raw).resolve(),
                        # Existing batch summaries store cwd-relative artifact
                        # paths; the quality file is written beside the summary.
                        (summary.parent / raw.name).resolve()}
                    quality_paths = {p for p in quality_paths
                                     if p.is_file() and _inside(p, output)}
                    if len(quality_paths) != 1:
                        raise ValueError("quality json_path missing or ambiguous")
                    quality = next(iter(quality_paths))
                    detail = json.loads(quality.read_text(encoding="utf-8"))
                    name = detail["report_name"]
                    if not isinstance(name, str) or Path(name).name != name:
                        raise ValueError("invalid report_name")
                    project = (quality.parent / name).resolve()
                    mapped = {p for p in pbips if p.parent == project}
                    if not mapped:
                        raise ValueError("mapped project has no PBIP")
                    candidates.update(mapped)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    reasons.append(f"Invalid quality mapping for {key}: {exc}")
        ordered = sorted(str(p) for p in candidates)
        status = ("error" if reasons else "missing" if not ordered else
                  "paired" if len(ordered) == 1 else "ambiguous")
        rows.append({"source": str(workbook), "relative_source": relative.as_posix(),
                     "pair_status": status,
                     "pbip": ordered[0] if status == "paired" else None,
                     "candidates": ordered, "issues": reasons})
    owners = {}
    for row in rows:
        for candidate in row["candidates"]:
            owners.setdefault(candidate, []).append(row)
    for candidate, claimants in owners.items():
        if len(claimants) > 1:
            for row in claimants:
                row.update(pair_status="ambiguous", pbip=None)
                row["issues"].append(f"PBIP claimed by multiple source workbooks: {candidate}")
    return rows


def _pbi_capture(pbip: str, shot: Path, timeout: float,
                 desktop_path: str | None) -> dict:
    if not desktop_window.IS_WINDOWS:
        return {"status": "unavailable", "screenshot": None,
                "note": "Power BI window capture requires interactive Windows"}
    try:
        if shot.exists() or shot.is_symlink():
            raise ValueError("screenshot destination must not already exist")
        _safe_destination(shot)
        report = desktop_probe.probe_desktop_open(
            pbip, timeout=timeout, close_after=False, wait_for_window=True,
            screenshot_path=str(shot), desktop_path=desktop_path,
            verify_data=True, refresh=True)
        evidence = report.to_dict()
        # Never accept the probe's process-survival verdict or fallback capture.
        captured = (report.status == "opened" and report.window_loaded
                    and document_title_matches(report.window_title, Path(pbip).stem)
                    and report.screenshot is not None
                    and Path(report.screenshot).resolve() == shot.resolve()
                    and shot.is_file() and report.screenshot_blank is False)
        evidence["probe_status"] = report.status
        evidence["status"] = "captured" if captured else (
            report.status if report.status in ("unavailable", "timed_out", "error")
            else "error")
        evidence["screenshot"] = str(shot) if captured else None
        if not captured:
            evidence["note"] = ("No verified document-window nonblank capture. "
                                + report.note)
        return evidence
    except Exception as exc:
        return {"status": "error", "screenshot": None,
                "note": f"{type(exc).__name__}: {exc}"}


def _ui(en: str, fr: str) -> str:
    """Authored interface text only; never pass evidence through translation."""
    return render(text(en, fr), default="fr")


def _pixel_limitation() -> str:
    return _ui(PIXEL_LIMITATION,
        "Diagnostic local non sémantique des pixels, sans certification SSIM ni de parité. "
        "Compare les pixels de fenêtres complètes aux dimensions identiques, sans redimensionnement, "
        "recadrage, alignement, tolérance ni correspondance supposée des zones de dessin. "
        "L’interface des applications, le DPI, les polices, les animations et la sélection peuvent "
        "produire des différences. Les captures initiales établissent uniquement l’association des "
        "classeurs, pas la correspondance des pages sélectionnées. Les canaux RGBA sont comparés "
        "littéralement (alpha 255 pour RGB), sans composition ni correction de profil colorimétrique "
        "ou de gamma. Seuls les PNG RGB/RGBA 8 bits non entrelacés sont pris en charge ; les blocs "
        "de transparence et les PNG animés ne le sont pas. Les fichiers et les lignes décodées "
        "sont chacun limités à 64 Mio. Aucune réussite d’exécution sémantique, de parité des données, "
        "d’actualisation, de fidélité du rendu ni de déploiement n’est déduite. Les mesures ne "
        "modifient jamais l’état des captures.")


def _payload_details(payload: dict, label: str = "Full evidence payload") -> str:
    labels = {
        "Full evidence payload": "Preuves complètes",
        "Full pixel evidence": "Preuves pixel complètes",
        "Totaux techniques complets": "Totaux techniques complets",
        "Full page correspondence evidence": "Preuves complètes de correspondance des pages",
        "Full workbook evidence": "Preuves complètes du classeur",
    }
    en = "Full technical totals" if label == "Totaux techniques complets" else label
    return (f'<details><summary>{_ui(en, labels.get(label, label))}</summary><pre>'
            f'{html.escape(json.dumps(payload, indent=2, ensure_ascii=False))}'
            '</pre></details>')


def _pixel_html(result: dict | None, table_id: str) -> str:
    if result is None:
        return ""
    esc = html.escape
    rows = [[_ui("Status", "État"), esc(result["status"])],
            [_ui("Reason", "Motif"), esc(result.get("reason_code") or "—")]]
    for side in ("source", "target"):
        dimensions = result.get(f"{side}_dimensions")
        value = esc(f'{dimensions["width"]} × {dimensions["height"]}') if dimensions else _ui("Unavailable", "Indisponible")
        rows.append([_ui(f"{side.title()} dimensions", "Dimensions source" if side == "source" else "Dimensions cible"), value])
    labels = {
        "pixel_count": text("Pixel count", "Nombre de pixels"),
        "different_pixel_count": text("Different RGBA pixels", "Pixels RGBA différents"),
        "different_pixel_fraction": text("Different pixel fraction (0–1)", "Proportion de pixels différents (0–1)"),
        "absolute_channel_difference_sum": text("Sum of absolute channel differences", "Somme des différences absolues des canaux"),
        "mean_absolute_channel_error": text("Mean absolute RGBA channel error (0–255)", "Erreur absolue moyenne des canaux RGBA (0–255)"),
        "normalized_mean_absolute_channel_error": text("Normalized mean absolute channel error (0–1)", "Erreur absolue moyenne normalisée des canaux (0–1)"),
    }
    for key, value in (result.get("metrics") or {}).items():
        rows.append([render(labels.get(key, key), "fr"), esc(str(value))])
    return ('<h3>' + _ui("Local pixel comparison (non-semantic)", "Comparaison locale des pixels (non sémantique)") + '</h3>'
            + data_table([text("Metric", "Mesure"), text("Value", "Valeur")], rows, table_id=table_id)
            + f'<p>{esc(result["note"])}</p><p>{_pixel_limitation()}</p>'
            + _payload_details(result, "Full pixel evidence"))


def _report_document(manifest: dict, title: str, body: str, *,
                     collapse_limitations: bool = False) -> str:
    timestamp = manifest.get("created_at", "Not recorded")
    limitation = '<p>' + _ui(CAPTURE_LIMITATION,
        "Une fenêtre réactive portant le titre du document et une capture non vide de la fenêtre complète constituent uniquement des indices diagnostiques, pas une preuve du chargement des données ni de la fidélité visuelle. Les interfaces des applications diffèrent : une revue manuelle est nécessaire. La sonde directe laisse l’application ouverte ; les comparaisons ferment uniquement les PID qu’elles ont lancés, après la capture. Le transfert vers une instance existante n’est pas pris en charge.") + '</p>'
    if collapse_limitations:
        body += f'<details><summary>{_ui("Capture limitations", "Limites des captures")}</summary>{limitation}</details>'
        limitation = ""
    styles = """
/* Local presentation for screenshot-comparison reports only. */
body.desktop-comparison-report,
.desktop-comparison {
    --dc-ink: #292e32;
    --dc-muted: #515b63;
    --dc-line: #dfe3e5;
    --dc-surface: #fff;
    --dc-canvas: #f8f9f9;
    --dc-blue: #365d64;
    --dc-blue-soft: #f0f5f4;
    --dc-hero-bg: #f6f8f7;
    --dc-icon-bg: #edf2f0;
    --dc-focus: #426a73;
    --dc-warn: #713f12;
    --dc-warn-bg: #fff5db;
    color: var(--dc-ink);
    font-size: 1rem;
}
html body.desktop-comparison-report {
    background: var(--dc-canvas); color: var(--dc-ink);
}
body.desktop-comparison-report .report-header {
    background: var(--dc-surface); color: var(--dc-ink);
    border-bottom: 1px solid var(--dc-line);
}
body.desktop-comparison-report .report-header .meta {
    color: var(--dc-muted); opacity: 1;
}
body.desktop-comparison-report .theme-toggle {
    background: var(--dc-surface); color: var(--dc-ink); border-color: var(--dc-line);
}
body.desktop-comparison-report .theme-toggle:hover { background: var(--dc-blue-soft); }
body.desktop-comparison-report .theme-toggle:focus-visible {
    outline: 3px solid var(--dc-focus); outline-offset: 3px;
}
.desktop-comparison thead th {
    background: var(--dc-hero-bg); color: var(--dc-ink);
    border-bottom: 1px solid var(--dc-line);
}
.desktop-comparison thead th.sortable:hover { background: var(--dc-icon-bg); }
body.desktop-comparison-report table { background: var(--dc-surface); }
body.desktop-comparison-report tbody td { border-bottom-color: var(--dc-line); }
body.desktop-comparison-report tbody tr:hover { background: var(--dc-blue-soft); }
body.desktop-comparison-report .table-search input {
    background: var(--dc-surface); color: var(--dc-ink); border-color: var(--dc-line);
}
body.desktop-comparison-report .report-footer {
    color: var(--dc-muted); border-top-color: var(--dc-line);
}
.desktop-comparison * { box-sizing: border-box; }
.desktop-comparison p { color: var(--dc-muted); line-height: 1.6; margin: .45rem 0; }
.desktop-comparison h2, .desktop-comparison h3 { color: var(--dc-ink); line-height: 1.3; }
.desktop-comparison .dc-hero {
    position: relative; overflow: hidden; margin: 0 0 1.25rem; padding: 1.5rem 1.7rem;
    border: 1px solid var(--dc-line); border-radius: 18px;
    background: var(--dc-hero-bg);
    box-shadow: 0 3px 12px rgba(41, 46, 50, .035);
}
.desktop-comparison .dc-hero::after {
    content: ""; position: absolute; width: 13rem; height: 13rem; right: -4rem; top: -7rem;
    border: 1.5rem solid rgba(90, 112, 103, .035); border-radius: 50%; pointer-events: none;
}
.desktop-comparison .dc-eyebrow {
    display: flex; align-items: center; gap: .55rem; color: var(--dc-blue);
    font-size: .75rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase;
}
.desktop-comparison .dc-icon {
    display: inline-grid; width: 2rem; height: 2rem; flex: 0 0 auto;
    place-items: center; border-radius: .65rem; background: var(--dc-icon-bg);
    color: var(--dc-blue); font-size: 1.1rem; line-height: 1;
}
.desktop-comparison .dc-hero h2 { margin: .55rem 0 .35rem; font-size: clamp(1.35rem, 2.5vw, 2rem); }
.desktop-comparison .dc-hero p { max-width: 70rem; margin: .3rem 0; }
.desktop-comparison .dc-stats {
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: .75rem; margin: 1rem 0 1.4rem;
}
.desktop-comparison .dc-stat {
    min-width: 0; padding: .85rem 1rem; border: 1px solid var(--dc-line);
    border-radius: 12px; background: var(--dc-surface); box-shadow: 0 2px 8px rgba(23,43,77,.045);
}
.desktop-comparison .dc-stat strong {
    display: block; color: var(--dc-ink); font-size: 1.55rem; line-height: 1.15;
    font-variant-numeric: tabular-nums;
}
.desktop-comparison .dc-stat span { display: block; margin-top: .3rem; color: var(--dc-muted); font-size: .86rem; }
.desktop-comparison .dc-notice {
    display: flex; gap: .7rem; align-items: flex-start; margin: .9rem 0;
    padding: .8rem 1rem; border: 1px solid #e8c77d; border-radius: 10px;
    background: var(--dc-warn-bg); color: var(--dc-warn);
}
.desktop-comparison .dc-notice p { margin: 0; color: inherit; }
.desktop-comparison #report-navigation,
.desktop-comparison [data-page-navigation] {
    display: flex; flex-wrap: wrap; align-items: center; gap: .65rem;
    margin: 1rem 0; padding: .75rem 1rem; border: 1px solid var(--dc-line);
    border-radius: 12px; background: var(--dc-surface);
}
.desktop-comparison #report-navigation[hidden],
.desktop-comparison [data-page-navigation][hidden],
.desktop-comparison [data-comparison-report][hidden],
.desktop-comparison [data-comparison-page][hidden] { display: none !important; }
.desktop-comparison label { color: var(--dc-ink); font-weight: 700; }
.desktop-comparison select, .desktop-comparison button {
    min-height: 2.65rem; border: 1px solid #8b9bb0; border-radius: 8px;
    background: var(--dc-surface); color: var(--dc-ink); font: inherit;
}
.desktop-comparison select { max-width: 100%; padding: .45rem .7rem; }
.desktop-comparison button { padding: .4rem .85rem; cursor: pointer; font-weight: 700; }
.desktop-comparison button:not(:disabled):hover { background: var(--dc-blue-soft); border-color: var(--dc-blue); }
.desktop-comparison button:disabled { color: #5b6573; cursor: not-allowed; opacity: .72; }
.desktop-comparison :focus-visible {
    outline: 3px solid var(--dc-focus) !important; outline-offset: 3px;
    box-shadow: 0 0 0 2px var(--dc-surface);
}
.desktop-comparison [data-comparison-report] {
    margin: 1rem 0 1.5rem; padding: 1.1rem;
    border: 1px solid var(--dc-line); border-radius: 16px; background: var(--dc-canvas);
}
.desktop-comparison [data-comparison-report] > h2 {
    margin: .15rem 0 .3rem; overflow-wrap: anywhere; font-size: 1.25rem;
}
.desktop-comparison .dc-coverage { font-size: .9rem; }
.desktop-comparison .dc-page {
    margin: 1rem 0; padding: 1rem; border: 1px solid var(--dc-line);
    border-radius: 13px; background: var(--dc-surface);
}
.desktop-comparison .dc-page > h3 { margin: .1rem 0 .7rem; font-size: 1.1rem; }
.desktop-comparison .dc-status {
    display: inline-flex; align-items: center; gap: .45rem; padding: .4rem .7rem;
    border: 1px solid #d7a947; border-radius: 999px; background: #fff5db;
    color: #713f12; font-size: .85rem; font-weight: 800;
}
.desktop-comparison .dc-status-ready { border-color: #86b6a1; background: #e5f5ee; color: #174b37; }
.desktop-comparison .chart-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
.desktop-comparison .chart-card {
    min-width: 0; padding: 1rem; border: 1px solid var(--dc-line); border-radius: 12px;
    background: var(--dc-surface); box-shadow: 0 3px 12px rgba(23,43,77,.05);
}
.desktop-comparison .chart-card h3 {
    display: flex; align-items: center; gap: .55rem; margin: 0 0 .55rem;
    font-size: 1rem; overflow-wrap: anywhere;
}
.desktop-comparison .chart-card h3 .dc-icon { width: 1.75rem; height: 1.75rem; font-size: .95rem; }
.desktop-comparison .chart-card img {
    display: block; width: 100%; height: auto; max-height: 72vh; object-fit: contain;
    border: 1px solid var(--dc-line); border-radius: 8px; background: var(--dc-canvas);
}
.desktop-comparison .chart-card a { display: block; border-radius: 8px; }
.desktop-comparison .dc-empty {
    display: flex; min-height: 9rem; flex-direction: column; align-items: center;
    justify-content: center; gap: .35rem; margin: .5rem 0; padding: 1rem;
    border: 2px dashed #9a5b16; border-radius: 10px; background: #fff8e9;
    color: #633b0c; text-align: center;
}
.desktop-comparison .dc-empty strong { font-size: 1rem; }
.desktop-comparison .dc-empty code { overflow-wrap: anywhere; color: inherit; font-weight: 800; }
.desktop-comparison details {
    margin: .85rem 0; padding: .7rem .9rem; border: 1px solid var(--dc-line);
    border-radius: 9px; background: var(--dc-surface);
}
.desktop-comparison details > summary { color: var(--dc-blue); cursor: pointer; font-weight: 700; }
.desktop-comparison details > summary:focus-visible { border-radius: 3px; }
.desktop-comparison pre { max-height: 28rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.desktop-comparison a { color: var(--dc-blue); text-underline-offset: .15em; }
.desktop-comparison a:hover { color: var(--dc-ink); }
.desktop-comparison .badge { display: inline-flex; align-items: center; gap: .35rem; border-radius: 999px; font-weight: 700; }
.desktop-comparison .badge-blue { background: var(--dc-icon-bg); color: var(--dc-blue); }
.desktop-comparison .badge::before { content: "\\2022"; font-size: 1.2em; line-height: .7; }
.desktop-comparison .dc-back { margin: 0 0 1rem; }
html.dark body.desktop-comparison-report,
html.dark .desktop-comparison {
    --dc-ink: #edf0ee; --dc-muted: #c5ccc8; --dc-line: #48524d;
    --dc-surface: #242a27; --dc-canvas: #1b201e; --dc-blue-soft: #303d37;
    --dc-hero-bg: #282f2b; --dc-icon-bg: #35423b;
    --dc-blue: #b8d8d0; --dc-focus: #b8d8d0;
    --dc-warn: #ffe1a1; --dc-warn-bg: #493719; color: var(--dc-ink);
}
html.dark .desktop-comparison .dc-status { border-color: #99733b; background: #493719; color: #ffe1a1; }
html.dark .desktop-comparison .dc-status-ready { border-color: #4f8a6d; background: #1c4235; color: #b7efd3; }
html.dark .desktop-comparison .dc-empty { border-color: #b58a4b; background: #3b3020; color: #ffe1a1; }
html.dark .desktop-comparison .chart-card img { border-color: var(--dc-line); background: var(--dc-canvas); }
html.dark .desktop-comparison button:disabled { color: #c4cfdd; }
@media (prefers-color-scheme: dark) {
    html:not(.dark) body.desktop-comparison-report,
    html:not(.dark) .desktop-comparison {
        --dc-ink: #edf0ee; --dc-muted: #c5ccc8; --dc-line: #48524d;
        --dc-surface: #242a27; --dc-canvas: #1b201e; --dc-blue-soft: #303d37;
        --dc-hero-bg: #282f2b; --dc-icon-bg: #35423b;
        --dc-blue: #b8d8d0; --dc-focus: #b8d8d0;
        --dc-warn: #ffe1a1; --dc-warn-bg: #493719; color: var(--dc-ink);
    }
    html:not(.dark) .desktop-comparison .dc-status { border-color: #99733b; background: #493719; color: #ffe1a1; }
    html:not(.dark) .desktop-comparison .dc-status-ready { border-color: #4f8a6d; background: #1c4235; color: #b7efd3; }
    html:not(.dark) .desktop-comparison .dc-empty { border-color: #b58a4b; background: #3b3020; color: #ffe1a1; }
    html:not(.dark) .desktop-comparison .chart-card img { border-color: var(--dc-line); background: var(--dc-canvas); }
    html:not(.dark) .desktop-comparison button:disabled { color: #c4cfdd; }
}
@media (max-width: 760px) {
    .desktop-comparison .dc-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .desktop-comparison .chart-row { grid-template-columns: 1fr; }
    .desktop-comparison .dc-hero { padding: 1.1rem; }
    .desktop-comparison [data-comparison-report] { padding: .75rem; }
}
@media (max-width: 420px) {
    .desktop-comparison .dc-stats { grid-template-columns: 1fr 1fr; gap: .5rem; }
    .desktop-comparison .dc-stat { padding: .7rem; }
    .desktop-comparison .dc-stat strong { font-size: 1.3rem; }
}
@media (prefers-reduced-motion: reduce) {
    .desktop-comparison *, .desktop-comparison *::before, .desktop-comparison *::after {
        scroll-behavior: auto !important; transition-duration: .01ms !important;
    }
}
"""
    # Explicit JS MIME type also distinguishes template scripts from user text.
    opening = html_open(title, timestamp=timestamp, language="fr").replace(
                "<body>", '<body class="desktop-comparison-report">').replace(
                "</head>", f'<style id="desktop-comparison-theme">{styles}</style></head>').replace(
                "<script>", '<script type="text/javascript">')
    return (opening + '<main class="desktop-comparison">'
            + limitation + body + '</main>'
            + html_close(timestamp=timestamp).replace(
                "<script>", '<script type="text/javascript">'))


def _capture_asset_url(image: str, run_dir: Path) -> str:
    """Copy recorded bytes to a short presentation URL, never changing evidence.

    Browsers on Windows can reject otherwise valid long local screenshot paths.
    Content addressing keeps copies deterministic and deduplicated. Only callers
    that accepted the recorded capture may use this helper; cached assets never
    establish capture availability or replace a missing original.
    """
    payload = Path(image).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    relative = Path("assets") / f"{digest}.png"
    destination = run_dir / relative
    _safe_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_bytes() != payload:
        destination.write_bytes(payload)
    return html.escape(quote(relative.as_posix(), safe="/."), quote=True)


def _capture_html(capture: dict, run_dir: Path, alt: str, *,
                  collapse_note: bool = False) -> str:
    """Render only an accepted, existing screenshot, without changing evidence."""
    esc = html.escape
    status = capture.get("status", "not_requested")
    image = capture.get("screenshot")
    if status == "captured" and image and Path(image).is_file():
        url = _capture_asset_url(image, run_dir)
        alt_text = alt.fr if isinstance(alt, LocalizedText) else alt
        alt_attrs = i18n_attrs(alt, "alt") if isinstance(alt, LocalizedText) else ""
        content = (f'<a href="{url}"><img width="100%" src="{url}" '
                   f'alt="{esc(alt_text, quote=True)}" {alt_attrs}></a>'
                   f'<p><a href="{url}">'
                   + _ui("Open screenshot (byte-identical local PNG copy)",
                         "Ouvrir la capture (copie PNG locale identique octet par octet)")
                   + '</a></p>')
    else:
        missing = " (screenshot file unavailable)" if status == "captured" else ""
        reason = {
            "captured": text("Image file unavailable", "Fichier image indisponible"),
            "inaccessible": text("Page inaccessible", "Page inaccessible"),
            "unavailable": text("Capture unavailable", "Capture indisponible"),
            "not_requested": text("Capture not requested", "Capture non demandée"),
            "not_run": text("Capture not run", "Capture non exécutée"),
            "timed_out": text("Capture timed out", "Délai dépassé"),
            "error": text("Capture error", "Erreur de capture"),
            "failed": text("Capture failed", "Échec de capture"),
            "unsupported": text("Capture unsupported", "Capture non prise en charge"),
        }.get(status, text("Capture unavailable", "Capture indisponible"))
        content = (f'<div class="dc-empty" role="status" '
                   f'data-capture-status="{esc(status, quote=True)}" '
                   f'data-capture-diagnostic="No capture: {esc(status, quote=True)}{missing}">'
                   '<span class="dc-icon" aria-hidden="true">&#9888;</span>'
                   f'<strong>{_ui("Missing capture", "Capture manquante")}</strong>'
                   f'<span>{render(reason, "fr")}</span></div>'
                   f'<details><summary>{_ui("Capture diagnostic", "Diagnostic de capture")}</summary>'
                   f'<p>No capture: {esc(status)}{missing}</p></details>')
    note = capture.get("note", "")
    if collapse_note:
        return content + (
            f'<details><summary>{_ui("Capture details", "Détails de capture")}</summary>'
            f'<p>{esc(note)}</p></details>' if note else "")
    return content + f'<p>{esc(note)}</p>'


def write_comparison_reports(manifest: dict, run_dir: Path) -> None:
    """Write separate run JSON and HTML; link only current successful captures."""
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    render_comparison_html(manifest, run_dir)


def render_comparison_html(manifest: dict, run_dir: Path) -> None:
    """Regenerate presentation only from recorded evidence, without executing probes.

    Does not write the manifest, original screenshots, source projects, or quality
    data. Writes HTML and byte-identical, content-addressed presentation copies in
    assets/ to avoid long local image paths. Original screenshot existence is
    checked read-only; cached copies never replace missing evidence.
    """
    esc = html.escape
    if manifest.get("all_pages"):
        _write_all_page_html(manifest, run_dir)
        return
    sections = []
    for index, row in enumerate(manifest["workbooks"]):
        panels = []
        for label, field in (("Tableau", "tableau"), ("Power BI", "powerbi")):
            panels.append(f'<div><h3>{label}</h3>'
                          + _capture_html(row[field], run_dir, text(f"{label} capture", f"Capture {label}"))
                          + '</div>')
        sections.append(
            f'<section><h2>{esc(row["relative_source"])}</h2>'
            f'<p>{_ui("Pair:", "Association :")} {esc(row["pair_status"])}; {_ui("Review:", "Revue :")} {esc(row["status"])}</p>'
            f'<p>{esc("; ".join(row["issues"]))}</p>'
            f'<div class="chart-row">{"".join(panels)}</div>'
            + _pixel_html(row.get("pixel_comparison"), f"pixels-{index}")
            + '</section>')
    document = _report_document(
        manifest, text(f'Desktop comparison: {manifest["status"]}', f'Comparaison Desktop : {manifest["status"]}'),
        '<p>' + _ui("No SSIM or parity score is assigned. Missing captures are failures, not equivalent reports. Only the initially opened sheet/page is captured.",
                     "Aucun score SSIM ni de parité n’est attribué. Les captures manquantes sont des échecs, pas des rapports équivalents. Seule la feuille/page ouverte initialement est capturée.") + '</p>'
        + "".join(sections) + _payload_details(manifest))
    (run_dir / "comparison.html").write_text(document, encoding="utf-8")


def _write_all_page_html(manifest: dict, run_dir: Path) -> None:
    esc = html.escape

    def verified(page):
        """Presentation only: never promote an opening or unverified capture."""
        if not page:
            return False
        capture = page["capture"]
        return bool(
            capture.get("status") == "captured"
            and capture.get("screenshot")
            and Path(capture["screenshot"]).is_file()
            and capture.get("screenshot_blank") is False
            and all(isinstance(capture.get(key), dict)
                    and capture[key].get("status") == "verified"
                    for key in ("selection", "post_capture_selection")))

    def document(title, body):
        limitation = (f'<p>{_pixel_limitation()}</p>'
                      if manifest.get("screen_comparison") == "local" else "")
        return _report_document(
            manifest, title,
            body + '<details><summary>' + _ui("Scope and technical limitations", "Périmètre et limites techniques") + '</summary>'
            '<p>' + _ui("All-page diagnostic. Initial launch captures are excluded from page totals and page pixel comparisons. No SSIM, data parity or fidelity score is assigned.",
                        "Diagnostic de toutes les pages. Les captures initiales au lancement sont exclues des totaux de pages et des comparaisons pixel par page. Aucun score SSIM, de parité des données ni de fidélité n’est attribué.") + '</p>'
            + limitation + '</details>', collapse_limitations=True)

    def panel(label, page):
        icon = "&#128202;" if label == "Tableau" else "&#128200;"
        if not page:
            return (f'<div class="chart-card"><h3><span class="dc-icon" aria-hidden="true">{icon}</span>'
                    f'{label}</h3><div class="dc-empty" role="status">'
                    '<span class="dc-icon" aria-hidden="true">&#9888;</span>'
                    f'<strong>{_ui("No verified correspondence", "Aucune correspondance vérifiée")}</strong>'
                    f'<span>{_ui("Page not mapped.", "Page non associée.")}</span></div>'
                    f'<details><summary>{_ui("Mapping diagnostic", "Diagnostic de correspondance")}</summary>'
                    f'<p>{_ui("No verified page mapping.", "Aucune correspondance de page vérifiée.")}</p></details></div>')
        identity_status = ("Page capture verified" if verified(page)
                           else "Page capture missing or unverified")
        identity = (badge(text("Verified capture", "Capture vérifiée"), "blue") if verified(page)
                    else badge(text("Missing or unverified capture", "Capture absente ou non vérifiée"), "yellow"))
        return (f'<div class="chart-card"><h3><span class="dc-icon" aria-hidden="true">{icon}</span>'
                f'{label}: {esc(page["name"])}</h3>'
                + f'<p data-status="{identity_status}">{identity}</p>'
                + _capture_html(page["capture"], run_dir, f'{label} {page["name"]}',
                                collapse_note=True)
                + f'<details><summary>{_ui("Page metadata", "Métadonnées de page")}</summary>'
                f'<p>{_ui("Kind:", "Type :")} {esc(page["kind"])} ; {_ui("Hidden:", "Masquée :")} {page.get("hidden", False)} ; '
                f'{_ui("Tooltip:", "Info-bulle :")} {page.get("tooltip", False)}</p></details>'
                + '</div>')

    total_rows = []
    for side in ("source", "target"):
        totals = manifest["totals"][side]
        label = "Tableau" if side == "source" else "Power BI"
        total_rows.append([label, str(totals["inventory"]),
                           str(totals["captured"]), str(totals["error"])])
    workbooks = manifest["workbooks"]
    initial_count = sum(
        (row.get(side) or {}).get("status") == "captured"
        for row in workbooks for side in ("tableau", "powerbi"))
    source_totals = manifest["totals"]["source"]
    target_totals = manifest["totals"]["target"]
    page_count = source_totals["inventory"] + target_totals["inventory"]
    verified_count = sum(
        verified(page) for row in workbooks
        for inventory_key in ("source_inventory", "target_inventory")
        for page in row.get(inventory_key, []))
    not_verified_count = max(0, page_count - verified_count)
    sections = [
        '<section class="dc-hero" aria-labelledby="comparison-intro">'
        '<div class="dc-eyebrow"><span class="dc-icon" aria-hidden="true">&#128269;</span>'
        + _ui("VISUAL REVIEW · CAPTURE EVIDENCE", "REVUE VISUELLE · PREUVES DE CAPTURE") + '</div>'
        '<h2 id="comparison-intro">' + _ui("Compare screenshots clearly", "Comparez les captures, en toute clarté") + '</h2>'
        '<p>' + _ui("Choose a report and a page: Tableau on the left, Power BI on the right.",
                     "Choisissez un rapport et une page : Tableau à gauche, Power BI à droite.") + '</p>'
        '</section>'
        f'<section class="dc-stats" aria-label="Résumé des captures" {i18n_attrs(text("Capture summary", "Résumé des captures"), "aria-label")}>'
        f'<div class="dc-stat"><strong>{len(workbooks)}</strong><span>{_ui("Reports", "Rapports")}</span></div>'
        f'<div class="dc-stat"><strong>{page_count}</strong><span>{_ui("Pages (both sides)", "Pages (deux côtés)")}</span></div>'
        f'<div class="dc-stat"><strong>{verified_count}</strong><span>{_ui("Verified captures", "Captures vérifiées")}</span></div>'
        f'<div class="dc-stat"><strong>{not_verified_count}</strong><span>{_ui("Without verified capture", "Sans capture vérifiée")}</span></div>'
        '</section>'
        '<div class="dc-notice" role="note"><span class="dc-icon" aria-hidden="true">&#9432;</span>'
        '<p>' + _ui("Captures ≠ validated parity: business review required, without replacing missing pages.",
                     "Captures ≠ parité validée : revue métier nécessaire, sans remplacement des pages manquantes.") + '</p></div>',
        '<p>' + _ui(
            "Unverified opening captures are shown before page evidence for each report. For local PNG links, open this HTML from disk in a browser; a hosted preview may block local resources.",
            "Les captures initiales non vérifiées sont affichées avant les preuves par page de chaque rapport. Pour accéder aux PNG locaux, ouvrez ce HTML depuis le disque dans un navigateur ; un aperçu hébergé peut bloquer les ressources locales.") + '</p>',
        '<details><summary>' + _ui("Detailed coverage totals", "Totaux détaillés de couverture") + '</summary>'
        '<p>' + _ui(f'{len(workbooks)} report(s) recorded; {initial_count} initial capture(s). Recorded status:',
                     f'{len(workbooks)} rapport(s) dans le relevé ; {initial_count} capture(s) initiale(s). État du relevé :')
        + f' <strong>{esc(manifest["status"])}</strong> ' + _ui("(capture status only).", "(état des captures uniquement).") + '</p>'
        '<p>' + _ui(
            f'Page capture status: Tableau {source_totals["captured"]}/{source_totals["inventory"]} with status “captured”; Power BI {target_totals["captured"]}/{target_totals["inventory"]} with status “captured”. Initial captures are excluded from page totals.',
            f'État de capture des pages : Tableau {source_totals["captured"]}/{source_totals["inventory"]} au statut « captured » ; Power BI {target_totals["captured"]}/{target_totals["inventory"]} au statut « captured ». Les captures initiales sont exclues des totaux de pages.') + '</p>'
        + data_table([text("Inventory", "Inventaire"), "Pages", text("Status captured", "Statut captured"), text("Other statuses", "Autres statuts")],
                     total_rows, table_id="page-totals")
        + _payload_details(manifest["totals"], "Totaux techniques complets")
        + '<p>' + _ui(f'{len(workbooks)} workbooks; {initial_count}/{2 * len(workbooks)} initial captures recorded.',
                       f'{len(workbooks)} classeurs ; {initial_count}/{2 * len(workbooks)} captures initiales enregistrées.') + '</p></details>',
        f'<nav id="report-navigation" aria-label="Sélection du rapport" {i18n_attrs(text("Report selection", "Sélection du rapport"), "aria-label")} hidden>'
        f'<label for="report-selector">{_ui("Report:", "Rapport :")} </label>'
        f'<select id="report-selector" aria-label="Choisir un rapport" {i18n_attrs(text("Choose a report", "Choisir un rapport"), "aria-label")}>'
        + ''.join(f'<option value="report-{index}">{esc(row["relative_source"])}</option>'
                  for index, row in enumerate(workbooks))
        + '</select></nav>'
        '<noscript><p>JavaScript est désactivé : tous les rapports et les pages sont affichés. '
        'Les liens de preuve par page restent disponibles.</p><p>'
        'JavaScript is off: all reports and pages are shown. Individual page evidence links remain available.</p>'
        '<span hidden>Individual page evidence links remain available. '
        'JavaScript is off: all reports and pages are shown below.</span></noscript>']
    number = 0
    for report_index, row in enumerate(workbooks):
        sections.append(f'<section id="report-{report_index}" data-comparison-report>'
                        f'<h2>{esc(row["relative_source"])}</h2>')
        sources = {p["id"]: p for p in row.get("source_inventory", [])}
        targets = {p["id"]: p for p in row.get("target_inventory", [])}
        coverage = []
        for label, inventory in (("Tableau", sources), ("Power BI", targets)):
            captured = sum(p["capture"]["status"] == "captured"
                           for p in inventory.values())
            coverage.append(_ui(
                f'{label}: {captured}/{len(inventory)} with status captured; {len(inventory) - captured} other statuses',
                f'{label} : {captured}/{len(inventory)} au statut captured ; {len(inventory) - captured} autres états'))
        # Visible before page evidence, but never used as a per-page substitute.
        sections.append(
            '<details open data-opening-captures><summary>' + _ui("Initial opening captures — page identity unverified", "Captures initiales — identité de page non vérifiée") + '</summary>'
            '<p>' + _ui("These are initial/opening document-window captures, not verified page mappings. They are not included in page totals or page pixel comparisons and do not establish parity.",
                         "Ces captures initiales de fenêtres de document ne sont pas des correspondances de pages vérifiées. Elles sont exclues des totaux de pages et des comparaisons pixel par page, et ne démontrent pas la parité.") + '</p>'
            '<div class="chart-row">'
            + ''.join(
                f'<div class="chart-card"><h3>{label} — {_ui("initial opening capture (page identity unverified)", "capture initiale (identité de page non vérifiée)")}</h3>'
                + _capture_html(row.get(field) or {}, run_dir,
                                text(f'{label} initial opening capture — page identity unverified',
                                     f'{label} — capture initiale, identité de page non vérifiée'),
                                collapse_note=True)
                + '</div>'
                for label, field in (("Tableau", "tableau"), ("Power BI", "powerbi")))
            + '</div></details>')
        page_sections = []
        page_options = []
        for mapping in row.get("page_mappings", []):
            number += 1
            source = sources.get(mapping["source_id"])
            target = targets.get(mapping["target_id"])
            name = target["name"] if target else source["name"] if source else ""
            title = text(f'Capture evidence — {name} | {row["relative_source"]}',
                         f'Preuves de capture — {name} | {row["relative_source"]}')
            page_id = f"page-{number:05d}"
            filename = f"{page_id}.html"
            if (row["pair_status"] == "paired" and mapping["status"] == "mapped"
                    and verified(source) and verified(target)):
                status = "Screenshots ready for visual review"
                visible_status = text("Screenshots ready for visual review", "Captures prêtes à comparer")
                detail = text("Verified captures; business validation still required.", "Captures vérifiées ; validation métier encore nécessaire.")
                status_class = "dc-status dc-status-ready"
            else:
                status = "Comparison incomplete"
                visible_status = text("Comparison incomplete", "Comparaison incomplète")
                detail = text("A mapping or verified capture is missing, without substitution.", "Une correspondance ou une capture vérifiée manque, sans substitution.")
                status_class = "dc-status"
            body = (f'<p><span class="{status_class}" role="status" data-status="{status}">'
                    f'<span aria-hidden="true">{"&#10003;" if status.startswith("Screenshots") else "&#9888;"}</span>'
                    f'{render(visible_status, "fr")}</span> {render(detail, "fr")}</p>'
                    f'<div class="chart-row">{panel("Tableau", source)}{panel("Power BI", target)}</div>')
            if mapping.get("pixel_comparison") is not None:
                body += (f'<details><summary>{_ui("Technical pixel diagnostics (non-semantic)", "Diagnostics techniques des pixels (non sémantiques)")}</summary>'
                         + _pixel_html(mapping["pixel_comparison"], f"page-pixels-{number}")
                         + '</details>')
            body += (f'<details><summary>{_ui("Mapping details", "Détails de correspondance")}</summary>'
                     f'<p>{render(visible_status, "fr")}; '
                     + (_ui('business approval is still required.', 'la validation métier reste nécessaire.')
                        if status_class.endswith("ready")
                        else _ui('Opening captures are not used in its place.', 'Les captures initiales ne remplacent pas les captures manquantes.'))
                     + '</p>'
                     f'<p>{_ui("Mapping:", "Correspondance :")} {esc(mapping["status"])}; '
                     f'{_ui("Candidate source IDs:", "Identifiants source candidats :")} {esc(", ".join(mapping["candidate_source_ids"]))}</p>'
                     + _payload_details(mapping, "Full page correspondence evidence")
                     + '</details>')
            (run_dir / filename).write_text(
                document(title, f'<p><a href="comparison.html#{page_id}">'
                         + _ui('Back to report and page navigation', 'Retour à la navigation des rapports et pages') + '</a></p>' + body),
                encoding="utf-8")
            option_text = text(
                f'{len(page_options) + 1}. {name} — {visible_status.en}',
                f'{len(page_options) + 1}. {name} — {visible_status.fr}')
            page_options.append(f'<option value="{page_id}" {i18n_attrs(option_text)}>'
                                f'{esc(option_text.fr)}</option>')
            page_sections.append(
                f'<section class="dc-page" id="{page_id}" data-comparison-page>'
                f'<h3>{esc(name)}</h3>{body}'
                f'<p><a href="{filename}">{_ui("Open individual page evidence", "Ouvrir les preuves de cette page")}</a></p></section>')
        if page_sections:
            sections.append(
                f'<nav data-page-navigation aria-label="Pages du rapport {report_index + 1}" {i18n_attrs(text(f"Pages of report {report_index + 1}", f"Pages du rapport {report_index + 1}"), "aria-label")} hidden>'
                f'<label for="page-selector-{report_index}">{_ui("Page:", "Page :")} </label>'
                f'<select id="page-selector-{report_index}" data-page-selector aria-label="Choisir une page" {i18n_attrs(text("Choose a page", "Choisir une page"), "aria-label")}>'
                + ''.join(page_options) + '</select> '
                f'<button type="button" data-page-previous aria-label="Page précédente" {i18n_attrs(text("Previous page", "Page précédente"), "aria-label")}>&#8592; {_ui("Previous", "Précédente")}</button> '
                f'<button type="button" data-page-next aria-label="Page suivante" {i18n_attrs(text("Next page", "Page suivante"), "aria-label")}>{_ui("Next", "Suivante")} &#8594;</button> '
                '<span data-page-position role="status" aria-live="polite"></span></nav>'
                + ''.join(page_sections))
        else:
            sections.append('<p>' + _ui("No page correspondences recorded: see diagnostics.", "Aucune correspondance de page : consultez les diagnostics.") + '</p>'
                            '<span hidden>No page correspondences recorded.</span>')
        sections.append(f'<details><summary>{_ui("Technical diagnostics and inventories", "Diagnostics techniques et inventaires")}</summary>')
        sections.append(f'<p class="dc-coverage">{_ui("Capture coverage", "Couverture de capture")} — '
                        + '; '.join(coverage) + '.</p>')
        if row["issues"]:
            sections.append('<p>' + _ui("Issues:", "Problèmes :") + ' ' + esc("; ".join(row["issues"])) + '</p>')
        sections.append(f'<p>{_ui("Recorded status:", "État enregistré :")} <strong>{esc(row["status"])}</strong>; '
                        f'{_ui("Pair:", "Association :")} {esc(row["pair_status"])}.</p>')
        summary = row.get("page_pixel_comparison_summary")
        if summary:
            sections.append(
                '<h3>' + _ui("Local page pixel diagnostics", "Diagnostics locaux des pixels par page") + '</h3>'
                + data_table([text("Status", "État"), text("Compared", "Comparées"), text("Not comparable", "Non comparables"), text("Error", "Erreur")], [[
                    esc(summary["status"]), str(summary["counts"]["compared"]),
                    str(summary["counts"]["not_comparable"]), str(summary["counts"]["error"])]],
                    table_id=f'pixel-summary-{len(sections)}')
                + f'<p>{esc(summary["note"])}</p>')
        # Complete inventory is displayed even when mapping is ambiguous.
        for label, inventory in (("Tableau", sources), ("Power BI", targets)):
            sections.append(f'<h3>{label} — {_ui("Inventory", "Inventaire")}</h3><table><tr><th>Page</th>'
                            f'<th>{_ui("Kind", "Type")}</th><th>{_ui("Hidden/tooltip", "Masquée/info-bulle")}</th><th>Capture</th></tr>')
            for page in inventory.values():
                result = page["capture"]
                status = esc(result["status"])
                if (result["status"] == "captured" and result.get("screenshot")
                        and Path(result["screenshot"]).is_file()):
                    url = _capture_asset_url(result["screenshot"], run_dir)
                    status = f'<a href="{url}">{status}</a>'
                sections.append(f'<tr><td>{esc(page["name"])}</td><td>{esc(page["kind"])}</td>'
                                f'<td>{page.get("hidden", False)}/{page.get("tooltip", False)}</td>'
                                f'<td>{status}: {esc(result.get("note", ""))}</td></tr>')
            sections.append('</table>')
        sections.append('</details>')
        sections.append(_payload_details(row, "Full workbook evidence")
                        + '</section>')
    # Static local-only JS: workbook/page names are escaped markup, never code.
    # Progressive enhancement leaves all evidence readable without JavaScript.
    sections.append('''<script type="text/javascript">
(function () {
    const selector = document.getElementById('report-selector');
    const reports = Array.from(document.querySelectorAll('[data-comparison-report]'));
    if (!selector || !reports.length) return;
    function showReport() {
        reports.forEach(function (report) {
            report.hidden = report.id !== selector.value;
        });
    }
    function showPage(report) {
        const pages = Array.from(report.querySelectorAll('[data-comparison-page]'));
        const select = report.querySelector('[data-page-selector]');
        if (!select) return;
        pages.forEach(function (page) { page.hidden = page.id !== select.value; });
        report.querySelector('[data-page-previous]').disabled = select.selectedIndex <= 0;
        report.querySelector('[data-page-next]').disabled = select.selectedIndex >= pages.length - 1;
        const position = report.querySelector('[data-page-position]');
        position.textContent = window.ReportI18n ? ReportI18n.format(
            'Page {current} of {total}', 'Page {current} sur {total}',
            {current: select.selectedIndex + 1, total: pages.length})
            : 'Page ' + (select.selectedIndex + 1) + ' sur ' + pages.length;
    }
    reports.forEach(function (report) {
        const select = report.querySelector('[data-page-selector]');
        if (!select) return;
        report.querySelector('[data-page-navigation]').hidden = false;
        select.addEventListener('change', function () { showPage(report); });
        [['[data-page-previous]', -1], ['[data-page-next]', 1]].forEach(function (step) {
            report.querySelector(step[0]).addEventListener('click', function () {
                select.selectedIndex = Math.max(0, Math.min(
                    select.options.length - 1, select.selectedIndex + step[1]));
                showPage(report);
            });
        });
        showPage(report);
    });
    function followPageLink() {
        const id = window.location.hash.slice(1);
        const report = reports.find(function (item) {
            return item.id === id || Array.from(
                item.querySelectorAll('[data-comparison-page]')).some(
                    function (page) { return page.id === id; });
        });
        if (!report) return;
        selector.value = report.id;
        const select = report.querySelector('[data-page-selector]');
        if (select && id !== report.id) select.value = id;
        showPage(report);
        showReport();
    }
    selector.addEventListener('change', showReport);
    window.addEventListener('hashchange', followPageLink);
    document.addEventListener('reportlanguagechange', function () {
        reports.forEach(showPage);
    });
    document.getElementById('report-navigation').hidden = false;
    showReport();
    followPageLink();
})();
</script>''')
    (run_dir / "comparison.html").write_text(
        document(text('Visual screenshot comparison', 'Comparaison visuelle des captures'),
                 "".join(sections) + _payload_details(manifest)),
        encoding="utf-8")


def _page_totals(rows: list[dict]) -> dict:
    totals = {}
    for label, key in (("source", "source_inventory"), ("target", "target_inventory")):
        pages = [p for row in rows for p in row.get(key, [])]
        captured = sum(p["capture"]["status"] == "captured" for p in pages)
        statuses = {}
        for page in pages:
            status = page["capture"]["status"]
            statuses[status] = statuses.get(status, 0) + 1
        totals[label] = {"inventory": len(pages), "captured": captured,
                         "error": len(pages) - captured, "statuses": statuses}
    mappings = [m for row in rows for m in row.get("page_mappings", [])]
    totals["mapping"] = {s: sum(m["status"] == s for m in mappings)
                         for s in ("mapped", "ambiguous", "unmatched_source", "unmatched_target")}
    totals["inventory_errors"] = (sum("inventory failed:" in issue
                                      for row in rows for issue in row["issues"])
                                  + sum(bool(p.get("inventory_error")) for row in rows
                                        for key in ("source_inventory", "target_inventory")
                                        for p in row.get(key, [])))
    return totals


def compare_desktop_reports(source_dir: str, output_root: str, *,
                            tableau_desktop_path: str | None = None,
                            powerbi_desktop_path: str | None = None,
                            timeout: float = 90, all_pages: bool = False,
                            page_timeout: float = 30,
                            screen_comparison: str = "manual") -> dict:
    """User-run batch diagnostic; captures both apps for every unambiguous pair.

    Fresh per-project screenshots/runs/<run_id>/{source,output}/<stem>.png,
    plus output_root/desktop_comparisons/<run_id>/{manifest.json,comparison.html}.
    Returns manifest with status 'captured' or 'failed', never a parity score.
    screen_comparison='local' adds read-only decoded PNG diagnostics to each
    initial workbook pair, or each page_mappings entry when all_pages=True.
    Pixel differences/errors do not change capture status or process cleanup.
    Raises ValueError/OSError for invalid folders/options or report write failures.
    """
    if screen_comparison not in ("manual", "local"):
        raise ValueError("screen_comparison must be 'manual' or 'local'")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(page_timeout) or page_timeout <= 0:
        raise ValueError("page_timeout must be a finite positive number")
    rows = pair_workbooks(source_dir, output_root)
    if not rows:
        raise ValueError("source directory contains no .twb/.twbx workbooks")
    output = Path(output_root).absolute()
    _safe_destination(output)
    output = output.resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    run_dir = output / "desktop_comparisons" / run_id
    _safe_destination(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    for row in rows:
        requested_initial = {}
        absent = {"status": "not_requested", "screenshot": None,
                  "note": "Capture not attempted because workbook pair is unresolved"}
        row["tableau"] = dict(absent)
        row["powerbi"] = dict(absent)
        if row["pair_status"] == "paired":
            try:
                project = Path(row["pbip"]).parent
                shots = project / "screenshots" / "runs" / run_id
                _safe_destination(shots)
                # New directory, not canonical paths that may contain stale evidence.
                shots.mkdir(parents=True, exist_ok=False)
                filename = Path(row["source"]).stem + ".png"
            except Exception as exc:
                row["issues"].append(f"Capture setup failed: {type(exc).__name__}: {exc}")
                for field in ("tableau", "powerbi"):
                    row[field] = {"status": "error", "screenshot": None,
                                  "note": row["issues"][-1]}
            else:
                # Each application's directory, launch, and result conversion
                # are independent. Even unexpected probe exceptions must not
                # suppress the other capture or subsequent workbooks.
                for field, subdir in (("tableau", "source"), ("powerbi", "output")):
                    try:
                        destination = shots / subdir
                        _safe_destination(destination)
                        destination.mkdir(exist_ok=False)
                        shot = destination / filename
                        requested_initial[field] = shot
                        if field == "tableau":
                            row[field] = probe_tableau_open(
                                row["source"], screenshot_path=str(shot),
                                desktop_path=tableau_desktop_path,
                                timeout=timeout).to_dict()
                        else:
                            row[field] = _pbi_capture(
                                row["pbip"], shot, timeout, powerbi_desktop_path)
                    except Exception as exc:
                        note = f"{field} capture failed: {type(exc).__name__}: {exc}"
                        row["issues"].append(note)
                        row[field] = {"status": "error", "screenshot": None,
                                      "note": note}
        if all_pages:
            # Even unresolved workbook pairs get their source inventory; initial
            # screenshots are launch evidence only and never count as page captures.
            try:
                page_shots = (Path(row["pbip"]).parent / "screenshots" / "runs" / run_id
                              if row.get("pbip") else run_dir / "unpaired")
                if screen_comparison == "local":
                    _capture_all_pages(row, page_shots, page_timeout,
                                       screen_comparison=screen_comparison)
                else:
                    _capture_all_pages(row, page_shots, page_timeout)
            except Exception as exc:
                row["issues"].append(f"All-page workflow failed: {type(exc).__name__}: {exc}")
            finally:
                _cleanup_comparison_processes(row)
        else:
            _cleanup_comparison_processes(row)
        if screen_comparison == "local":
            if all_pages:
                # Unexpected workflow exceptions must not masquerade as comparisons.
                for mapping in row.get("page_mappings", []):
                    mapping.setdefault("pixel_comparison", unavailable_pixels(
                        "page_workflow_incomplete", "Page capture/comparison workflow did not complete"))
                comparisons = [m["pixel_comparison"] for m in row.get("page_mappings", [])]
                row["page_pixel_comparison_summary"] = {
                    "scope": "page_correspondences_only",
                    "initial_captures_compared": False,
                    "status": ("compared" if comparisons and all(
                        c["status"] == "compared" for c in comparisons) else "not_comparable"
                        if not any(c["status"] == "compared" for c in comparisons) else "partial"),
                    "counts": {status: sum(c["status"] == status for c in comparisons)
                               for status in ("compared", "not_comparable", "error")},
                    "note": ("See page_mappings[].pixel_comparison; no parity verdict"
                             if comparisons else "No page correspondence available to compare"),
                }
            elif row["pair_status"] != "paired":
                row["pixel_comparison"] = unavailable_pixels(
                    "workbook_not_matched", "No unambiguous workbook pair")
            else:
                row["pixel_comparison"] = _local_capture_pixels(
                    row["tableau"], row["powerbi"],
                    requested_initial.get("tableau"), requested_initial.get("powerbi"))
        row["status"] = ("manual_review" if row["pair_status"] == "paired"
                         and row["tableau"]["status"] == "captured"
                         and row["powerbi"]["status"] == "captured"
                         and (not all_pages or row.get("status") == "manual_review")
                         and not row["issues"] else "failed")
    manifest = {
        "schema_version": 3 if all_pages else 2, "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(Path(source_dir).resolve()), "output_root": str(output),
        "run_dir": str(run_dir), "limitation": CAPTURE_LIMITATION,
        "parity_score": None,
        "status": "captured" if all(r["status"] == "manual_review" for r in rows) else "failed",
        "workbooks": rows,
    }
    if all_pages:
        manifest.update(all_pages=True, page_timeout=page_timeout,
                        initial_captures_counted=False, totals=_page_totals(rows))
    if screen_comparison == "local":
        manifest.update(screen_comparison="local",
                        screen_comparison_limitations=PIXEL_LIMITATION)
    write_comparison_reports(manifest, run_dir)
    from powerbi_import.migration_quality import refresh_desktop_comparison_links
    refresh_desktop_comparison_links(str(output))
    return manifest
