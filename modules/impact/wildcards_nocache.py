"""
No-cache wildcard loading for ComfyUI-Impact-Pack (personal fork).

Isolates the new behavior so that upstream merges only touch small hook
points in wildcards.py / config.py. When the user updates the vendored
impact-pack to a newer upstream release, this file is almost guaranteed
to merge cleanly.

Behavior:
  * wildcard_no_cache = true  -> wildcards are re-read from disk on every
    access (get_wildcard_value). Nothing is written to wildcard_dict /
    loaded_wildcards on the no-cache path.
  * custom_wildcards set + path exists -> only that directory is used;
    the default 'wildcards/' directory is ignored.
  * A single INFO log is emitted on the first resolution after each
    wildcard_load() call so the user can confirm the active mode.
"""

import logging
import os
import threading

from impact import config


_runtime_log_lock = threading.Lock()
_runtime_log_emitted = False


def reset_runtime_log_flag():
    """Called from wildcard_load() so the runtime log fires once per load cycle."""
    global _runtime_log_emitted
    with _runtime_log_lock:
        _runtime_log_emitted = False


def is_enabled():
    """True when wildcards should be re-read on every access."""
    try:
        return bool(config.get_config().get('wildcard_no_cache', False))
    except Exception:
        return False


def is_custom_only():
    """True when only custom_wildcards should be searched."""
    try:
        return bool(config.get_config().get('custom_wildcards_is_set', False))
    except Exception:
        return False


def get_search_paths(default_wildcards_path):
    """
    Return the ordered list of directories to search for wildcard files.

    * custom-only: [custom_wildcards]
    * otherwise:   [default, custom_wildcards?]
    """
    custom_path = None
    try:
        cfg = config.get_config()
        if cfg.get('custom_wildcards_is_set'):
            candidate = cfg.get('custom_wildcards')
            if candidate and os.path.isdir(candidate):
                custom_path = candidate
    except Exception:
        pass

    if is_custom_only() and custom_path:
        return [custom_path]

    paths = [default_wildcards_path]
    if custom_path and custom_path != default_wildcards_path:
        paths.append(custom_path)
    return paths


def find_file(key, default_wildcards_path):
    """
    Locate a wildcard file for `key` across the active search paths.

    Returns (file_path, is_yaml) or (None, False). Mirrors the resolution
    order used by wildcards.find_wildcard_file but honors custom-only mode.
    """
    candidates = [f"{key}.txt", f"{key}.yaml", f"{key}.yml"]
    for base in get_search_paths(default_wildcards_path):
        for rel in candidates:
            file_path = os.path.join(base, rel)
            if os.path.isfile(file_path):
                return (file_path, file_path.endswith(('.yaml', '.yml')))

    # YAML nested key fallback: "colors/warm" -> "colors.yaml"
    if '/' in key:
        parent = key.split('/', 1)[0]
        yaml_candidates = [f"{parent}.yaml", f"{parent}.yml"]
        for base in get_search_paths(default_wildcards_path):
            for rel in yaml_candidates:
                file_path = os.path.join(base, rel)
                if os.path.isfile(file_path):
                    return (file_path, True)

    return (None, False)


def _extract_yaml_key(yaml_data, key):
    """
    Given parsed YAML data and a normalized wildcard key (e.g. "colors/warm"
    or "colors"), return the matching list of strings, or None.
    Supports top-level lists/strings and one level of nesting.
    """
    if not yaml_data:
        return None

    def coerce(v):
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, (str, int, float)):
            return [str(v)]
        if isinstance(v, dict):
            # Flatten one level: concat all leaf values
            out = []
            for inner in v.values():
                if isinstance(inner, list):
                    out.extend(str(x) for x in inner)
                elif isinstance(inner, (str, int, float)):
                    out.append(str(inner))
            return out or None
        return None

    if '/' in key:
        top, sub = key.split('/', 1)
        if top in yaml_data and isinstance(yaml_data[top], dict):
            if sub in yaml_data[top]:
                return coerce(yaml_data[top][sub])
        # also try exact composite key
        if key in yaml_data:
            return coerce(yaml_data[key])
        return None

    if key in yaml_data:
        return coerce(yaml_data[key])
    return None


def load_value(key, default_wildcards_path, load_txt_fn, yaml_module, normalize_fn):
    """
    Read a wildcard value fresh from disk without touching any cache.

    Injected dependencies (from wildcards.py) to avoid import cycles:
      * load_txt_fn: wildcards.load_txt_wildcard
      * yaml_module: yaml
      * normalize_fn: wildcards.wildcard_normalize

    Returns a list of options or None.
    """
    _maybe_emit_runtime_log(default_wildcards_path)

    norm_key = normalize_fn(key)
    file_path, is_yaml = find_file(norm_key, default_wildcards_path)
    if file_path is None:
        return None

    try:
        if is_yaml:
            try:
                with open(file_path, 'r', encoding='ISO-8859-1') as f:
                    yaml_data = yaml_module.load(f, Loader=yaml_module.FullLoader)
            except (yaml_module.reader.ReaderError, UnicodeDecodeError):
                with open(file_path, 'r', encoding='UTF-8', errors='ignore') as f:
                    yaml_data = yaml_module.load(f, Loader=yaml_module.FullLoader)
            return _extract_yaml_key(yaml_data, norm_key)
        return load_txt_fn(file_path)
    except Exception as e:
        logging.warning(f"[Impact Pack] no-cache load failed for '{key}' at {file_path}: {e}")
        return None


def _maybe_emit_runtime_log(default_wildcards_path):
    global _runtime_log_emitted
    if _runtime_log_emitted:
        return
    with _runtime_log_lock:
        if _runtime_log_emitted:
            return
        _runtime_log_emitted = True

    paths = get_search_paths(default_wildcards_path)
    mode_bits = []
    if is_enabled():
        mode_bits.append("no-cache (reload on every access)")
    else:
        mode_bits.append("cached")
    if is_custom_only():
        mode_bits.append(f"custom_wildcards ONLY: {paths[0]} (default 'wildcards/' ignored)")
    else:
        mode_bits.append("sources: " + " + ".join(paths))

    logging.info("[Impact Pack] Wildcard runtime mode -> " + "; ".join(mode_bits))
