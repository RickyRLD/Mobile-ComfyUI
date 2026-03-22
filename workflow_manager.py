"""
workflow_manager.py
Zarzadza konfiguracjami workflow: rejestracja, ladowanie, iniekcja wartosci.
Przechowuje konfiguracje w workflows_config.json obok serwera.
"""
import json
import os
import logging

log = logging.getLogger("comfy")

# Plik z konfiguracjami workflow
WORKFLOWS_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "workflows_config.json"
)

def load_configs() -> dict:
    """Laduje wszystkie konfiguracje workflow z pliku JSON."""
    if not os.path.exists(WORKFLOWS_CONFIG_FILE):
        return {}
    try:
        with open(WORKFLOWS_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"workflow_manager: blad ladowania konfiguracji: {e}")
        return {}

def save_configs(configs: dict):
    """Zapisuje konfiguracje do pliku JSON."""
    with open(WORKFLOWS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)
    log.info(f"workflow_manager: zapisano {len(configs)} konfiguracji")

def scan_workflow_nodes(workflow_json: dict) -> list:
    """
    Skanuje plik workflow i zwraca liste nodow z ich metadanymi.
    Kazdy node: {id, title, type, inputs: [{name, type, default}]}
    """
    nodes = []
    for node_id, node_data in workflow_json.items():
        if not isinstance(node_data, dict):
            continue
        meta  = node_data.get("_meta", {})
        title = meta.get("title", "")
        ntype = node_data.get("class_type", "?")
        
        # Zbierz pola inputs (tylko proste wartosci, nie polaczenia)
        raw_inputs = node_data.get("inputs", {})
        inputs = []
        for field_name, field_val in raw_inputs.items():
            # Polaczenia to listy [node_id, slot] – pomijamy
            if isinstance(field_val, list):
                continue
            field_type = "text"
            if isinstance(field_val, bool):
                field_type = "bool"
            elif isinstance(field_val, (int, float)):
                field_type = "number"
            inputs.append({
                "name":    field_name,
                "type":    field_type,
                "default": field_val,
            })
        
        nodes.append({
            "id":     node_id,
            "title":  title,
            "type":   ntype,
            "label":  f"{title} [{ntype}]" if title else f"[{ntype}] #{node_id}",
            "inputs": inputs,
        })
    
    # Sortuj: najpierw nody z tytulami, potem bez
    nodes.sort(key=lambda n: (0 if n["title"] else 1, n["label"]))
    return nodes

def find_node_by_id(workflow: dict, node_id: str) -> dict | None:
    """Zwraca node po ID."""
    return workflow.get(node_id)

def inject_workflow_values(workflow: dict, config: dict, form_values: dict) -> dict:
    """
    Wstrzykuje wartosci z formularza do workflow na podstawie konfiguracji.
    
    config["mappings"] – lista mapowań:
      {
        "role":     "image_1" | "prompt" | "style" | "seed" | "custom",
        "node_id":  "9:209:211",
        "field":    "suffix",
        "label":    "Zdjecie (twarz)",      # tylko dla image/custom
        "form_key": "custom_weight",        # klucz w form_values dla custom
      }
    
    form_values – slownik z danymi formularza:
      {
        "suffix": "...",
        "image_1_filename": "mobile_xxx.jpg",
        "image_2_filename": "mobile_yyy.jpg",
        "style_mode": "auto", "style_main": "Claude", ...
        "custom_weight": "0.8",
        "seed": "auto",
      }
    """
    import random
    
    for mapping in config.get("mappings", []):
        role    = mapping.get("role")
        node_id = mapping.get("node_id")
        field   = mapping.get("field")
        node    = workflow.get(node_id)
        
        if not node or "inputs" not in node:
            log.warning(f"inject: node {node_id} nie istnieje w workflow")
            continue
        
        try:
            if role == "image_1":
                val = form_values.get("image_1_filename", "")
                if val:
                    node["inputs"][field] = val
                    
            elif role == "image_2":
                val = form_values.get("image_2_filename", "")
                if val:
                    node["inputs"][field] = val
                    
            elif role == "prompt":
                # Klucz overrides: "node_id::field" — działa też jako bezpośredni klucz w form_values
                override_key = f'{node_id}::{field}'
                simple_overrides = form_values.get("_simple_overrides", {})

                # Priorytet:
                # 1) simple_override z admina (klucz node_id::field)
                # 2) wartość bezpośrednia pod kluczem node_id::field w form_values (drugi+ prompt)
                # 3) form_key (domyślnie 'suffix' — pierwszy prompt)
                # 4) _suffix_default z konfiguracji
                if override_key in simple_overrides:
                    val = simple_overrides[override_key]
                elif override_key in form_values:
                    val = form_values[override_key]
                else:
                    form_key = mapping.get("form_key", "suffix")
                    val = form_values.get(form_key, "")
                    if not val:
                        val = mapping.get("_suffix_default", "")
                node["inputs"][field] = val
                log.debug(f"inject prompt: node={node_id} field={field} key={override_key} val={str(val)[:60]!r}")

                # Prefix field
                prefix_field = mapping.get("prefix_field")
                if prefix_field:
                    prefix_override_key = f'{node_id}::{prefix_field}'
                    if prefix_override_key in simple_overrides:
                        prefix_val = simple_overrides[prefix_override_key]
                    elif prefix_override_key in form_values:
                        prefix_val = form_values[prefix_override_key]
                    else:
                        prefix_form_key = mapping.get("prefix_form_key", "prefix")
                        prefix_val = form_values.get(prefix_form_key, "")
                        if not prefix_val:
                            prefix_val = mapping.get("_prefix_default", "")
                    node["inputs"][prefix_field] = prefix_val
                    log.debug(f"inject prefix: node={node_id} prefix_field={prefix_field} val={str(prefix_val)[:60]!r}")
                    
            elif role == "style":
                # Styl 3-poziomowy (RandomOrManual3Level lub podobny)
                mode_field    = mapping.get("mode_field", "mode")
                main_field    = mapping.get("main_field", "main_style")
                sub_field     = mapping.get("sub_field", "sub_style")
                subsub_field  = mapping.get("subsub_field", "subsub_style")
                # Sprawdź simple_overrides dla stylu (klucz: node_id::style)
                simple_overrides = form_values.get("_simple_overrides", {})
                style_override_key = f"{node_id}::style"
                if style_override_key in simple_overrides:
                    ov = simple_overrides[style_override_key]
                    if isinstance(ov, dict):
                        main_val   = ov.get("main", "")
                        sub_val    = ov.get("sub", "")
                        subsub_val = ov.get("subsub", "")
                        # Wartości mode: auto, manual_main, manual_sub, manual_all
                        if ov.get("mode"):
                            mode_val = ov.get("mode")
                        elif subsub_val:
                            mode_val = "manual_all"
                        elif sub_val:
                            mode_val = "manual_sub"
                        elif main_val:
                            mode_val = "manual_main"
                        else:
                            mode_val = "auto"
                        node["inputs"][mode_field] = mode_val
                        # Tylko nadpisz jeśli wartość niepusta — puste zostają z pliku JSON
                        if main_val:   node["inputs"][main_field]   = main_val
                        if sub_val:    node["inputs"][sub_field]    = sub_val
                        if subsub_val: node["inputs"][subsub_field] = subsub_val
                    else:
                        node["inputs"][mode_field]   = form_values.get("style_mode", "auto")
                        if form_values.get("style_main"):   node["inputs"][main_field]   = form_values["style_main"]
                        if form_values.get("style_sub"):    node["inputs"][sub_field]    = form_values["style_sub"]
                        if form_values.get("style_subsub"): node["inputs"][subsub_field] = form_values["style_subsub"]
                else:
                    node["inputs"][mode_field]   = form_values.get("style_mode", "auto")
                    if form_values.get("style_main"):   node["inputs"][main_field]   = form_values["style_main"]
                    if form_values.get("style_sub"):    node["inputs"][sub_field]    = form_values["style_sub"]
                    if form_values.get("style_subsub"): node["inputs"][subsub_field] = form_values["style_subsub"]
                
            elif role == "seed":
                seed_val = form_values.get("seed", "auto")
                if seed_val == "auto" or seed_val == "":
                    seed = random.randint(1, 999999999999999)
                else:
                    try:
                        seed = int(seed_val)
                    except ValueError:
                        seed = random.randint(1, 999999999999999)
                node["inputs"][field] = seed
                log.debug(f"inject: seed={seed} -> node {node_id}.{field}")
                
            elif role == "custom":
                form_key = mapping.get("form_key", "")
                val_raw  = form_values.get(form_key, mapping.get("default", ""))
                # Konwersja typu
                original = node["inputs"].get(field)
                if isinstance(original, bool):
                    val = str(val_raw).lower() in ("true", "1", "yes")
                elif isinstance(original, int):
                    try: val = int(val_raw)
                    except: val = original
                elif isinstance(original, float):
                    try: val = float(val_raw)
                    except: val = original
                else:
                    val = str(val_raw)
                node["inputs"][field] = val
                
        except Exception as e:
            log.warning(f"inject: blad roli={role} node={node_id} field={field}: {e}")
    
    return workflow

def get_output_node_ids(config: dict) -> list:
    """Zwraca liste node_id ktore sa wyjsciami (SaveImage itp.)."""
    return config.get("output_node_ids", [])
