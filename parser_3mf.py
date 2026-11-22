#!/usr/bin/env python3
"""
Pinha 3D — Processador de Arquivos .3mf
========================================
Extrai dados de fatiamento de arquivos .3mf gerados pelo Creality Print
(e outros slicers compatíveis com o padrão 3MF + extensões Bambu/Creality).

Suporte:
  1. Metadados de fatiamento do Creality Print (print_time, filament_weight por slot CFS)
  2. Fallback geométrico: calcula volume real via tetraedros com sinal (Stl volume theorem)
     e converte para gramas usando a densidade do material.

Uso:
  python3 parser_3mf.py arquivo.3mf [--density 1.24] [--json]

Saída JSON:
  {
    "source_file": "arquivo.3mf",
    "source": "creality_print" | "geometric_estimate",
    "print_time_minutes": 42,
    "total_filament_g": 18.5,
    "filaments": [
      {
        "slot": 0,
        "material": "PLA",
        "color_hex": "#7C3AED",
        "weight_net_g": 15.2,
        "weight_purge_g": 3.3
      }
    ],
    "bounding_box_mm": {"x": 60.0, "y": 45.0, "z": 30.0},
    "warnings": []
  }
"""

import sys
import json
import zipfile
import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# ─── Namespaces conhecidos ────────────────────────────────────────────────────
NS_3MF_CORE    = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_BAMBU       = "http://schemas.bambulab.com/package/2021"
NS_CREALITY    = "http://schemas.creality.com/package/2022"
NS_SLIC3R      = "http://schemas.slic3r.org/3mf/2017/06"
NS_PRUSASLICER = "http://schemas.prusa3d.com/3mf/2021"

# Mapa de nomes de material → densidade (g/cm³)
MATERIAL_DENSITY: dict[str, float] = {
    "PLA":    1.24,
    "PETG":   1.27,
    "ABS":    1.04,
    "ASA":    1.07,
    "TPU":    1.21,
    "NYLON":  1.14,
    "PA":     1.14,
    "PC":     1.20,
    "PLA+":   1.24,
    "SILK":   1.24,
    "MATTE":  1.24,
}


def _ns(tag: str, namespace: str) -> str:
    return f"{{{namespace}}}{tag}"


def _find_text(el: ET.Element, path: str, ns_map: dict) -> Optional[str]:
    found = el.find(path, ns_map)
    if found is not None and found.text:
        return found.text.strip()
    return None


def _attr(el: ET.Element, *attrs: str) -> Optional[str]:
    for a in attrs:
        v = el.get(a)
        if v:
            return v
    return None


# ─── Leitura de metadados Creality Print / Bambu Studio ──────────────────────

def _parse_slicer_config(raw: str) -> dict:
    """Parseia o config.ini ou similar embutido no .3mf."""
    cfg: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith(";") and not line.startswith("#"):
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip()
    return cfg


def _time_to_minutes(time_str: str) -> Optional[int]:
    """Converte strings de tempo ('1h 23m 45s', '5083', '01:23:45') para minutos."""
    s = time_str.strip()
    total_seconds = 0

    # Formato HH:MM:SS
    if s.count(":") == 2:
        try:
            h, m, sec = s.split(":")
            total_seconds = int(h) * 3600 + int(m) * 60 + int(sec)
            return total_seconds // 60
        except ValueError:
            pass

    # Formato numérico puro (segundos)
    if s.isdigit():
        return int(s) // 60

    # Formato legível "1h 23m 45s" ou "83m"
    import re
    hours   = re.search(r"(\d+)\s*h", s)
    minutes = re.search(r"(\d+)\s*m", s)
    seconds = re.search(r"(\d+)\s*s", s)
    if hours or minutes or seconds:
        total_seconds = (
            int(hours.group(1))   * 3600 if hours   else 0
          + int(minutes.group(1)) * 60   if minutes else 0
          + int(seconds.group(1))        if seconds else 0
        )
        return total_seconds // 60

    return None


def _parse_creality_print_model(root: ET.Element) -> Optional[dict]:
    """
    Tenta extrair metadados de fatiamento de um modelo Creality Print / Bambu.
    Retorna dict com chaves: print_time_minutes, filaments[]
    ou None se o arquivo não tiver esses metadados.
    """
    ns_map = {
        "m": NS_3MF_CORE,
        "b": NS_BAMBU,
        "c": NS_CREALITY,
        "s": NS_SLIC3R,
        "p": NS_PRUSASLICER,
    }

    warnings: list[str] = []
    filaments: list[dict] = []
    print_time_minutes: Optional[int] = None

    # --- Metadados no nível raiz do modelo ---
    for meta in root.findall("m:metadata", ns_map):
        name  = (meta.get("name") or "").lower()
        value = (meta.text or "").strip()

        if not value:
            continue

        # Tempo de impressão
        if any(k in name for k in ("print_time", "estimated_time", "printtime")):
            print_time_minutes = _time_to_minutes(value)

        # Filamentos Bambu/Creality (ex: "filament_used_weight_g[0]")
        import re
        wm = re.match(r"filament_(?:used_)?weight(?:_g)?(?:\[(\d+)\])?$", name)
        if wm:
            slot = int(wm.group(1)) if wm.group(1) is not None else 0
            try:
                grams = float(value)
                # garante que a lista tem espaço
                while len(filaments) <= slot:
                    filaments.append({"slot": len(filaments)})
                filaments[slot]["weight_net_g"] = round(grams, 2)
            except ValueError:
                pass

        # Cor do filamento (ex: "filament_colour[0]" ou "extruder_colour[0]")
        cm = re.match(r"(?:filament_colou?r|extruder_colou?r)(?:\[(\d+)\])?$", name)
        if cm:
            slot = int(cm.group(1)) if cm.group(1) is not None else 0
            while len(filaments) <= slot:
                filaments.append({"slot": len(filaments)})
            color = value.strip()
            if not color.startswith("#"):
                color = "#" + color
            filaments[slot]["color_hex"] = color.upper()

        # Material do filamento (ex: "filament_type[0]")
        mm = re.match(r"filament_type(?:\[(\d+)\])?$", name)
        if mm:
            slot = int(mm.group(1)) if mm.group(1) is not None else 0
            while len(filaments) <= slot:
                filaments.append({"slot": len(filaments)})
            filaments[slot]["material"] = value.upper()

        # Purge/wipe weight (ex: "filament_wipe_tower_weight_g[0]")
        pm = re.match(r"filament_(?:wipe_tower|purge)_weight(?:_g)?(?:\[(\d+)\])?$", name)
        if pm:
            slot = int(pm.group(1)) if pm.group(1) is not None else 0
            while len(filaments) <= slot:
                filaments.append({"slot": len(filaments)})
            try:
                filaments[slot]["weight_purge_g"] = round(float(value), 2)
            except ValueError:
                pass

    # Preenche campos faltantes em cada filamento
    for i, f in enumerate(filaments):
        f.setdefault("slot",          i)
        f.setdefault("material",      "PLA")
        f.setdefault("color_hex",     "#FFFFFF")
        f.setdefault("weight_net_g",  0.0)
        f.setdefault("weight_purge_g", 0.0)

    if print_time_minutes is not None or filaments:
        return {
            "print_time_minutes": print_time_minutes,
            "filaments": filaments,
            "warnings": warnings,
        }
    return None


# ─── Fallback geométrico ──────────────────────────────────────────────────────

def _signed_volume_tetrahedron(v0: tuple, v1: tuple, v2: tuple) -> float:
    """Volume com sinal do tetraedro formado por origem + triângulo."""
    x0, y0, z0 = v0
    x1, y1, z1 = v1
    x2, y2, z2 = v2
    return (
        x0 * (y1 * z2 - y2 * z1)
      - y0 * (x1 * z2 - x2 * z1)
      + z0 * (x1 * y2 - x2 * y1)
    ) / 6.0


def _compute_mesh_volume_cm3(root: ET.Element) -> Optional[float]:
    """
    Calcula o volume real da malha em cm³ usando o teorema do volume com sinal.
    Funciona para malhas fechadas (manifold).
    """
    ns_map = {"m": NS_3MF_CORE}

    total_volume_mm3 = 0.0
    meshes_found = 0

    for obj in root.findall(".//m:object", ns_map):
        mesh = obj.find("m:mesh", ns_map)
        if mesh is None:
            continue

        vertices_el = mesh.find("m:vertices", ns_map)
        triangles_el = mesh.find("m:triangles", ns_map)
        if vertices_el is None or triangles_el is None:
            continue

        verts = []
        for v in vertices_el.findall("m:vertex", ns_map):
            verts.append((
                float(v.get("x", 0)),
                float(v.get("y", 0)),
                float(v.get("z", 0)),
            ))

        mesh_volume = 0.0
        for tri in triangles_el.findall("m:triangle", ns_map):
            i0 = int(tri.get("v1", 0))
            i1 = int(tri.get("v2", 0))
            i2 = int(tri.get("v3", 0))
            if i0 < len(verts) and i1 < len(verts) and i2 < len(verts):
                mesh_volume += _signed_volume_tetrahedron(verts[i0], verts[i1], verts[i2])

        total_volume_mm3 += abs(mesh_volume)
        meshes_found += 1

    if meshes_found == 0:
        return None

    return total_volume_mm3 / 1000.0  # mm³ → cm³


def _bounding_box(root: ET.Element) -> Optional[dict]:
    ns_map = {"m": NS_3MF_CORE}
    xs, ys, zs = [], [], []
    for v in root.findall(".//m:vertex", ns_map):
        xs.append(float(v.get("x", 0)))
        ys.append(float(v.get("y", 0)))
        zs.append(float(v.get("z", 0)))
    if not xs:
        return None
    return {
        "x": round(max(xs) - min(xs), 2),
        "y": round(max(ys) - min(ys), 2),
        "z": round(max(zs) - min(zs), 2),
    }


# ─── Leitura do arquivo .3mf ─────────────────────────────────────────────────

def _find_model_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Localiza o arquivo .model principal dentro do .3mf."""
    # Tenta a convenção padrão
    for candidate in ["3D/3dmodel.model", "3d/3dmodel.model", "model/model.model"]:
        if candidate in zf.namelist():
            return candidate

    # Lê _rels/.rels para encontrar o target
    for rels_path in ["_rels/.rels", "_rels\\.rels"]:
        if rels_path in zf.namelist():
            try:
                rels_xml = zf.read(rels_path)
                rels_root = ET.fromstring(rels_xml)
                ns_rels = "http://schemas.openxmlformats.org/package/2006/relationships"
                for rel in rels_root.findall(f"{{{ns_rels}}}Relationship"):
                    rtype = rel.get("Type", "")
                    if "3dmodel" in rtype:
                        target = rel.get("Target", "").lstrip("/")
                        if target in zf.namelist():
                            return target
            except Exception:
                pass

    # Fallback: primeiro .model encontrado
    for name in zf.namelist():
        if name.endswith(".model"):
            return name

    return None


def parse_3mf(filepath: str, default_density: float = 1.24) -> dict:
    """
    Ponto de entrada principal.
    Retorna o dicionário de resultado com todos os dados extraídos.
    """
    result: dict = {
        "source_file":         filepath,
        "source":              "unknown",
        "print_time_minutes":  None,
        "total_filament_g":    None,
        "filaments":           [],
        "bounding_box_mm":     None,
        "warnings":            [],
    }

    path = Path(filepath)
    if not path.exists():
        result["warnings"].append(f"Arquivo não encontrado: {filepath}")
        return result

    if not zipfile.is_zipfile(filepath):
        result["warnings"].append(f"Arquivo não é um .3mf válido (não é ZIP): {filepath}")
        return result

    with zipfile.ZipFile(filepath, "r") as zf:
        model_path = _find_model_path(zf)
        if model_path is None:
            result["warnings"].append("Nenhum arquivo .model encontrado dentro do .3mf")
            return result

        try:
            xml_data = zf.read(model_path).decode("utf-8")
            root = ET.fromstring(xml_data)
        except Exception as e:
            result["warnings"].append(f"Erro ao parsear XML do modelo: {e}")
            return result

        # Bounding box sempre disponível
        result["bounding_box_mm"] = _bounding_box(root)

        # --- Tentativa 1: metadados do slicer ---
        slicer_data = _parse_creality_print_model(root)
        if slicer_data and (slicer_data["print_time_minutes"] or slicer_data["filaments"]):
            result["source"]             = "creality_print"
            result["print_time_minutes"] = slicer_data["print_time_minutes"]
            result["filaments"]          = slicer_data["filaments"]
            result["warnings"].extend(slicer_data["warnings"])

            total = sum(
                f.get("weight_net_g", 0) + f.get("weight_purge_g", 0)
                for f in result["filaments"]
            )
            result["total_filament_g"] = round(total, 2) if total > 0 else None

        # --- Tentativa 2: config.ini ou similar embutido ---
        if result["source"] == "unknown":
            for name in zf.namelist():
                if name.lower().endswith(("config.ini", "config.gcode", "slice_info.config")):
                    try:
                        cfg_raw = zf.read(name).decode("utf-8", errors="replace")
                        cfg = _parse_slicer_config(cfg_raw)

                        # Tempo
                        for tk in ("estimated_printing_time", "print_time", "estimated_time"):
                            if tk in cfg:
                                result["print_time_minutes"] = _time_to_minutes(cfg[tk])
                                break

                        # Filamento (valor único)
                        for wk in ("filament_weight", "filament_used_weight_g", "filament_mass"):
                            if wk in cfg:
                                try:
                                    grams = float(cfg[wk])
                                    result["filaments"] = [{
                                        "slot":           0,
                                        "material":       cfg.get("filament_type", "PLA").upper(),
                                        "color_hex":      "#FFFFFF",
                                        "weight_net_g":   round(grams, 2),
                                        "weight_purge_g": 0.0,
                                    }]
                                    result["total_filament_g"] = round(grams, 2)
                                    result["source"] = "config_ini"
                                except ValueError:
                                    pass
                                break
                    except Exception:
                        pass

        # --- Tentativa 3: estimativa geométrica ---
        if result["source"] == "unknown" or result["total_filament_g"] is None:
            volume_cm3 = _compute_mesh_volume_cm3(root)
            if volume_cm3 is not None and volume_cm3 > 0:
                # Estima infill padrão 15% + perímetros ~40% → ~55% de volume sólido
                INFILL_FACTOR = 0.55
                density = default_density
                estimated_g = volume_cm3 * INFILL_FACTOR * density
                # Adiciona estimativa de purge: 5% do net
                purge_g = estimated_g * 0.05

                if result["source"] == "unknown":
                    result["source"] = "geometric_estimate"
                    result["filaments"] = [{
                        "slot":           0,
                        "material":       "PLA",
                        "color_hex":      "#FFFFFF",
                        "weight_net_g":   round(estimated_g, 2),
                        "weight_purge_g": round(purge_g, 2),
                    }]
                    result["total_filament_g"] = round(estimated_g + purge_g, 2)
                    result["warnings"].append(
                        f"Dados de fatiamento não encontrados. "
                        f"Estimativa geométrica: volume={volume_cm3:.2f}cm³, "
                        f"infill={INFILL_FACTOR*100:.0f}%, densidade={density}g/cm³. "
                        f"Use Creality Print para dados precisos."
                    )

                    # Estima tempo: ~15 mm/s velocidade média → volume / (nozzle_area * speed)
                    # Heurística simples: 1g ≈ 2 minutos para PLA a velocidade moderada
                    result["print_time_minutes"] = max(1, round(result["total_filament_g"] * 2))
                    result["warnings"].append(
                        "Tempo de impressão estimado: ~2min/g (heurística). "
                        "Para o valor real, fatie no Creality Print e re-processe."
                    )

                elif result["total_filament_g"] is None:
                    # Já temos filamentos do slicer mas sem peso — usa geometria como fallback
                    result["warnings"].append(
                        f"Pesos de filamento não encontrados nos metadados. "
                        f"Estimativa geométrica: {round(estimated_g + purge_g, 2)}g total."
                    )
                    result["total_filament_g"] = round(estimated_g + purge_g, 2)

    return result


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pinha 3D — Parser de arquivos .3mf do Creality Print",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file", help="Caminho para o arquivo .3mf")
    parser.add_argument(
        "--density", type=float, default=1.24,
        help="Densidade do material em g/cm³ (padrão: 1.24 para PLA)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Saída em JSON puro (para integração com outros sistemas)"
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="JSON indentado (implica --json)"
    )
    args = parser.parse_args()

    result = parse_3mf(args.file, default_density=args.density)

    if args.json or args.pretty:
        indent = 2 if args.pretty else None
        print(json.dumps(result, ensure_ascii=False, indent=indent))
    else:
        # Saída legível para humanos
        print(f"\n{'='*55}")
        print(f"  Pinha 3D — Resultado do Parser .3mf")
        print(f"{'='*55}")
        print(f"  Arquivo : {result['source_file']}")
        print(f"  Fonte   : {result['source']}")

        if result["bounding_box_mm"]:
            bb = result["bounding_box_mm"]
            print(f"  Tamanho : {bb['x']} × {bb['y']} × {bb['z']} mm")

        print(f"\n  ⏱  Tempo de impressão  : ", end="")
        if result["print_time_minutes"] is not None:
            h, m = divmod(result["print_time_minutes"], 60)
            print(f"{h}h {m:02d}min ({result['print_time_minutes']} min)")
        else:
            print("N/D")

        print(f"  🧵 Filamento total     : ", end="")
        if result["total_filament_g"] is not None:
            print(f"{result['total_filament_g']:.1f} g")
        else:
            print("N/D")

        if result["filaments"]:
            print(f"\n  Filamentos por slot CFS:")
            for f in result["filaments"]:
                print(
                    f"    Slot {f['slot']:>1} │ {f['material']:<8} │ "
                    f"{f['color_hex']} │ "
                    f"net={f['weight_net_g']:.1f}g  "
                    f"purge={f['weight_purge_g']:.1f}g"
                )

        if result["warnings"]:
            print(f"\n  ⚠  Avisos:")
            for w in result["warnings"]:
                print(f"    - {w}")

        print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
