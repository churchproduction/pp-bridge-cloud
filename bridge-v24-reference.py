"""ProPresenter Bridge — generator + remote-control commands."""
import os, sys, json, uuid, shutil, glob, time, re, unicodedata
import urllib.request, urllib.parse
import importlib.util

# Per-Mac identity lives in ~/pp-bridge/config.py — required.
# Format:
#   MACHINE_ID = "building-c-side-screens"
#   MACHINE_NAME = "Building C Side Screens"
#   MIN_PLAYLIST_UUID = "..."
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from config import MIN_PLAYLIST_UUID
except ImportError:
    print("FATAL: ~/pp-bridge/config.py is missing or doesn't define MIN_PLAYLIST_UUID.", file=sys.stderr)
    print("Create it with the per-Mac constants. See the deploy notes.", file=sys.stderr)
    sys.exit(2)

HOST, PORT, PASSWORD = "localhost", 1025, "FishHawk"
LIBRARY_DIR = os.path.expanduser("~/Documents/ProPresenter/Libraries")
ASSETS_DIR  = os.path.expanduser("~/Documents/ProPresenter/Media/Assets")
PROTO_ROOT = os.path.expanduser("~/pp-bridge/proto-schema")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".tiff", ".bmp", ".gif")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv")
ALL_EXTS = IMAGE_EXTS + VIDEO_EXTS

def get_library_dir():
    if not os.path.isdir(LIBRARY_DIR): return None
    subs = [d for d in os.listdir(LIBRARY_DIR)
            if os.path.isdir(os.path.join(LIBRARY_DIR, d))]
    return os.path.join(LIBRARY_DIR, subs[0]) if subs else None

def find_pro_in_libraries(raw_name):
    name = sanitize_name(raw_name)
    if not os.path.isdir(LIBRARY_DIR):
        return None, name
    for sub in os.listdir(LIBRARY_DIR):
        sub_dir = os.path.join(LIBRARY_DIR, sub)
        if not os.path.isdir(sub_dir):
            continue
        candidate = os.path.join(sub_dir, f"{name}.pro")
        if os.path.exists(candidate):
            return candidate, name
    return None, name

sys.path.insert(0, PROTO_ROOT)
modules = {}
for f in sorted(glob.glob(os.path.join(PROTO_ROOT, "*_pb2.py"))):
    name = os.path.basename(f)[:-3]
    try:
        spec = importlib.util.spec_from_file_location(name, f)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        modules[name] = m
    except Exception: pass

def find_msg(name):
    for m in modules.values():
        if hasattr(m, name) and hasattr(getattr(m, name), "ParseFromString"):
            return getattr(m, name)
    raise AttributeError(f"Message {name!r} not found")

def find_enum(name):
    def walk(descs):
        for d in descs:
            for et in d.enum_types_by_name.values():
                if name in et.values_by_name: return et.values_by_name[name].number
            r = walk(d.nested_types)
            if r is not None: return r
        return None
    for m in modules.values():
        fd = getattr(m, "DESCRIPTOR", None)
        if not fd: continue
        for et in fd.enum_types_by_name.values():
            if name in et.values_by_name: return et.values_by_name[name].number
        r = walk(fd.message_types_by_name.values())
        if r is not None: return r
    raise AttributeError(f"Enum {name!r} not found")

def api(method, path, body=None):
    url = f"http://{HOST}:{PORT}/v1{path}?password={urllib.parse.quote(PASSWORD)}"
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        t = r.read().decode("utf-8")
        return json.loads(t) if t.strip() else None

def api_raw(path):
    """GET request that returns raw bytes (for thumbnails)."""
    url = f"http://{HOST}:{PORT}/v1{path}?password={urllib.parse.quote(PASSWORD)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()

def sanitize_name(name):
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    return name.strip(". ") or "Untitled"

def uid(): return str(uuid.uuid4()).upper()
def file_url(p): return "file://" + urllib.parse.quote(p, safe="/")

def natural_sort_key(s):
    """Key function for natural sorting — treats embedded numbers as numbers.
    'may10led-wall2' sorts before 'may10led-wall10' (instead of after, which is the
    default lexicographic order). Use as the `key=` arg to sorted() or list.sort()."""
    name = os.path.basename(s).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]

def find_in_playlist(plid, name):
    pl = api("GET", f"/playlist/{plid}")
    items = pl.get("items", []) if pl else []
    for i, it in enumerate(items):
        if it.get("id", {}).get("name") == name:
            return i, it, items
    return None, None, items

def delete_presentation(raw, target_playlist=None):
    """Delete the .pro and asset files for a presentation, AND remove it
    from the given playlist (defaults to MIN_PLAYLIST_UUID for backward compat)."""
    name = sanitize_name(raw)
    plid = target_playlist or MIN_PLAYLIST_UUID
    print(f"Deleting '{name}'...")
    library = get_library_dir()
    idx, item, items = find_in_playlist(plid, name)
    if item is not None:
        api("PUT", f"/playlist/{plid}",
            [normalize_v21_item(it) for j, it in enumerate(items) if j != idx])
        print(f"  removed from playlist")
    if library:
        pro = os.path.join(library, f"{name}.pro")
        if os.path.exists(pro): os.remove(pro); print("  deleted .pro")
    assets = os.path.join(ASSETS_DIR, name)
    if os.path.isdir(assets): shutil.rmtree(assets); print("  deleted assets")

def normalize_v21_item(it):
    """Ensure an item from GET response has all fields needed for PUT.
    Headers and presentations have DIFFERENT required fields:
      - presentation items have presentation_info + target_uuid
      - header items have header_color and target_uuid="" (no presentation_info)
    Mixing these up makes ProPresenter reject the PUT with HTTP 400."""
    item_type = it.get("type", "presentation")
    if "destination" not in it:
        it["destination"] = "presentation"

    if item_type == "header":
        # Header items must NOT have presentation_info. target_uuid is empty.
        if "target_uuid" not in it:
            it["target_uuid"] = ""
        # Strip presentation_info if some earlier round of normalization left one in
        it.pop("presentation_info", None)
        # header_color should be present from GET; if missing, default to ProPresenter's orange
        if "header_color" not in it:
            it["header_color"] = {"red": 1.0, "green": 0.149, "blue": 0.0, "alpha": 1.0}
        return it

    # Presentation item (default)
    info = it.get("presentation_info", {}) or {}
    pres_uuid = info.get("presentation_uuid", "")
    if "target_uuid" not in it:
        it["target_uuid"] = pres_uuid
    if "target_uuid" not in info:
        info["target_uuid"] = pres_uuid
    if "arrangement_uuid" not in info:
        info["arrangement_uuid"] = ""
    if "arrangement_name" not in info:
        info["arrangement_name"] = ""
    it["presentation_info"] = info
    return it

def build_v21_playlist_item(pres_uuid, fallback_name, index):
    """Build a v21-compatible playlist item by looking up the live name from the library."""
    libs = api("GET", "/libraries") or []
    real_name = fallback_name
    for lib in libs:
        items = api("GET", f"/library/{lib['uuid']}")
        for it in (items.get('items', []) if isinstance(items, dict) else items) or []:
            if it.get('uuid') == pres_uuid:
                real_name = it.get('name', fallback_name)
                break
    return {
        "id": {"uuid": uid(), "name": real_name, "index": index},
        "target_uuid": pres_uuid,
        "type": "presentation",
        "is_hidden": False,
        "is_pco": False,
        "destination": "presentation",
        "presentation_info": {
            "presentation_uuid": pres_uuid,
            "target_uuid": pres_uuid,
            "arrangement_name": "",
            "arrangement_uuid": "",
        },
    }

def _add_pres_uuid_to_playlist(plid, pres_uuid, fallback_name):
    """Generic helper — append a presentation to a playlist if not already present.
    Used by both add_existing_to_ministries and add_existing_to_playlist.
    Returns (was_added, total_items_in_playlist)."""
    pl = api("GET", f"/playlist/{plid}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    for it in items:
        info = it.get("presentation_info", {}) or {}
        if info.get("presentation_uuid") == pres_uuid:
            return False, len(items)  # already present
    items.append(build_v21_playlist_item(pres_uuid, fallback_name, len(items)))
    api("PUT", f"/playlist/{plid}", items)
    return True, len(items)

def add_existing_to_ministries(raw_name):
    """LEGACY — kept for backward compat with current agent.py.
    New code should use add_existing_to_playlist."""
    pro_path, name = find_pro_in_libraries(raw_name)
    if not pro_path:
        print(f"Not found in any library: '{name}'")
        sys.exit(2)
    Presentation = find_msg("Presentation")
    pres = Presentation()
    with open(pro_path, "rb") as f:
        pres.ParseFromString(f.read())
    pres_uuid = pres.uuid.string
    print(f"Found '{name}' (uuid={pres_uuid[:8]}...)")
    added, total = _add_pres_uuid_to_playlist(MIN_PLAYLIST_UUID, pres_uuid, name)
    if not added:
        print(f"Already in Ministries — skipping")
    else:
        print(f"Added '{name}' to Ministries (now {total} items)")

def add_existing_to_playlist(raw_name, playlist_uuid):
    """Add a library presentation to the named playlist.
    Used for upload-to-any-playlist routing from the cloud."""
    pro_path, name = find_pro_in_libraries(raw_name)
    if not pro_path:
        print(f"Not found in any library: '{name}'")
        sys.exit(2)
    Presentation = find_msg("Presentation")
    pres = Presentation()
    with open(pro_path, "rb") as f:
        pres.ParseFromString(f.read())
    pres_uuid = pres.uuid.string
    print(f"Found '{name}' (uuid={pres_uuid[:8]}...)")
    added, total = _add_pres_uuid_to_playlist(playlist_uuid, pres_uuid, name)
    if not added:
        print(f"Already in target playlist — skipping")
    else:
        print(f"Added '{name}' to playlist (now {total} items)")

def _build_pro_for_media(name, folder):
    """Build a .pro file from a folder of media. Returns (pres_uuid, copied_count).
    Shared by create_presentation and create_in_playlist."""
    library = get_library_dir()
    if not library:
        print(f"No library folder in {LIBRARY_DIR}"); sys.exit(1)
    print(f"Library: {library}")
    Presentation                   = find_msg("Presentation")
    PLATFORM_MACOS                 = find_enum("PLATFORM_MACOS")
    APPLICATION_PROPRESENTER       = find_enum("APPLICATION_PROPRESENTER")
    ACTION_TYPE_PRESENTATION_SLIDE = find_enum("ACTION_TYPE_PRESENTATION_SLIDE")
    ACTION_TYPE_MEDIA              = find_enum("ACTION_TYPE_MEDIA")
    LAYER_TYPE_FOREGROUND          = find_enum("LAYER_TYPE_FOREGROUND")
    COMPLETION_ACTION_TYPE_LAST    = find_enum("COMPLETION_ACTION_TYPE_LAST")
    ROOT_SHOW                      = find_enum("ROOT_SHOW")

    media = sorted(
        [f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith(ALL_EXTS)],
        key=natural_sort_key)
    if not media: print(f"No media in {folder}"); sys.exit(1)
    print(f"[1/4] {len(media)} file(s)")
    target = os.path.join(ASSETS_DIR, name)
    os.makedirs(target, exist_ok=True)
    copied = []
    for m in media:
        clean = sanitize_name(os.path.basename(m))
        dest = os.path.join(target, clean)
        base, ext = os.path.splitext(clean); n = 1
        while os.path.exists(dest):
            dest = os.path.join(target, f"{base}-{n}{ext}"); n += 1
        shutil.copy2(m, dest); copied.append(dest)
    print(f"[2/4] Copied to {target}")
    print(f"[3/4] Building .pro...")
    pres = Presentation()
    ai = pres.application_info
    ai.platform = PLATFORM_MACOS
    ai.platform_version.major_version = 26
    ai.platform_version.minor_version = 2
    ai.application = APPLICATION_PROPRESENTER
    ai.application_version.major_version = 19
    ai.application_version.patch_version = 1
    ai.application_version.build = "318767361"
    pres.uuid.string = uid(); pres.name = name
    pres.chord_chart.platform = PLATFORM_MACOS
    cue_uuids = [uid() for _ in copied]
    cg = pres.cue_groups.add()
    cg.group.uuid.string = uid(); cg.group.name = ""
    for cu in cue_uuids: cg.cue_identifiers.add().string = cu
    for cue_uuid, p in zip(cue_uuids, copied):
        fname = os.path.basename(p)
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        fmt = ext.upper().replace("JPEG", "JPG")
        cue = pres.cues.add()
        cue.uuid.string = cue_uuid
        cue.completion_action_type = COMPLETION_ACTION_TYPE_LAST
        cue.isEnabled = True
        a1 = cue.actions.add()
        a1.uuid.string = uid(); a1.label.text = fname
        a1.isEnabled = True; a1.type = ACTION_TYPE_PRESENTATION_SLIDE
        bs = a1.slide.presentation.base_slide
        bs.size.width = 1920; bs.size.height = 1080
        bs.uuid.string = uid()
        a1.slide.presentation.chord_chart.platform = PLATFORM_MACOS
        a2 = cue.actions.add()
        a2.uuid.string = uid(); a2.isEnabled = True
        a2.type = ACTION_TYPE_MEDIA
        el = a2.media.element
        el.uuid.string = uid()
        el.url.absolute_string = file_url(p)
        el.url.platform = PLATFORM_MACOS
        el.url.local.root = ROOT_SHOW
        el.url.local.path = f"Media/Assets/{name}/{fname}"
        el.metadata.format = fmt
        el.image.drawing.natural_size.width = 1920
        el.image.drawing.natural_size.height = 1080
        a2.media.layer_type = LAYER_TYPE_FOREGROUND
    out = os.path.join(library, f"{name}.pro")
    with open(out, "wb") as f: f.write(pres.SerializeToString())
    print(f"      Wrote {out}")
    return pres.uuid.string, len(copied)

def create_presentation(raw, folder):
    """LEGACY — creates a presentation and adds it to MIN_PLAYLIST_UUID.
    Kept for backward compat with current agent.py."""
    name = sanitize_name(raw)
    folder = os.path.expanduser(folder)
    library = get_library_dir()
    if not library:
        print(f"No library folder in {LIBRARY_DIR}"); sys.exit(1)
    pro = os.path.join(library, f"{name}.pro")
    if os.path.exists(pro):
        print(f"'{name}' exists — replacing"); delete_presentation(name)
    pres_uuid, count = _build_pro_for_media(name, folder)
    print(f"[4/4] Adding to Ministries...")
    time.sleep(1.5)
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    items.append(build_v21_playlist_item(pres_uuid, name, len(items)))
    api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}", items)
    print(f"\nDone — '{name}' ({count} slides)\n")

def create_in_playlist(raw, folder, playlist_uuid):
    """Build a presentation from a folder of media and add it to the named playlist.
    The .pro file lives in the library (one per name) but the playlist
    membership is what the agent controls. If a .pro with this name exists,
    we replace it AND remove it from the target playlist before adding."""
    name = sanitize_name(raw)
    folder = os.path.expanduser(folder)
    library = get_library_dir()
    if not library:
        print(f"No library folder in {LIBRARY_DIR}"); sys.exit(1)
    pro = os.path.join(library, f"{name}.pro")
    if os.path.exists(pro):
        print(f"'{name}' exists — replacing")
        delete_presentation(name, target_playlist=playlist_uuid)
    pres_uuid, count = _build_pro_for_media(name, folder)
    print(f"[4/4] Adding to playlist {playlist_uuid[:8]}...")
    time.sleep(1.5)
    pl = api("GET", f"/playlist/{playlist_uuid}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    items.append(build_v21_playlist_item(pres_uuid, name, len(items)))
    api("PUT", f"/playlist/{playlist_uuid}", items)
    print(f"\nDone — '{name}' ({count} slides)\n")

# =============================================================================
# REMOTE-CONTROL COMMANDS (added for production GUI)
# All emit JSON to stdout for the agent to forward back to the cloud.
# =============================================================================

def _emit(obj):
    print(json.dumps(obj))

def list_playlists():
    """Emit all top-level playlists on this Mac as JSON.
    Used by the cloud to validate that a config-defined UUID still exists,
    and by the frontend dropdown if we ever go fully dynamic."""
    pls = api("GET", "/playlists")
    out = []
    for p in pls or []:
        ptype = p.get("type", "playlist")
        pid = p.get("id", {}) or {}
        entry = {
            "uuid": pid.get("uuid", ""),
            "name": pid.get("name", ""),
            "index": pid.get("index", 0),
            "type": ptype,
        }
        if ptype == "playlistFolder":
            entry["child_count"] = len(p.get("playlists", []) or [])
        out.append(entry)
    _emit({"ok": True, "playlists": out, "default_uuid": MIN_PLAYLIST_UUID})

def list_ministries():
    """LEGACY: emit Ministries playlist items as JSON list.
    Kept so the current control.html keeps working while the
    multi-playlist UI rolls out. New code should use list_playlist_items."""
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = pl.get("items", []) if pl else []
    out = []
    for it in items:
        info = it.get("presentation_info", {}) or {}
        out.append({
            "item_uuid": it.get("id", {}).get("uuid", ""),
            "name": it.get("id", {}).get("name", ""),
            "type": it.get("type", "presentation"),
            "presentation_uuid": info.get("presentation_uuid", ""),
            "is_hidden": it.get("is_hidden", False),
        })
    _emit({"ok": True, "playlist_uuid": MIN_PLAYLIST_UUID, "items": out})

def list_playlist_items(playlist_uuid):
    """Emit items of any playlist as JSON list (generic version of list_ministries)."""
    pl = api("GET", f"/playlist/{playlist_uuid}")
    items = pl.get("items", []) if pl else []
    out = []
    for it in items:
        info = it.get("presentation_info", {}) or {}
        out.append({
            "item_uuid": it.get("id", {}).get("uuid", ""),
            "name": it.get("id", {}).get("name", ""),
            "type": it.get("type", "presentation"),
            "presentation_uuid": info.get("presentation_uuid", ""),
            "is_hidden": it.get("is_hidden", False),
        })
    _emit({"ok": True, "playlist_uuid": playlist_uuid, "items": out})

def get_slides(pres_uuid):
    """Emit slides of a presentation as JSON, with flat cue indices."""
    p = api("GET", f"/presentation/{pres_uuid}")
    pres = p.get("presentation", {}) if p else {}
    groups = pres.get("groups", [])
    slides = []
    cue = 0
    for g in groups:
        gname = g.get("name", "")
        gcolor = g.get("color", {})
        for s in g.get("slides", []):
            slides.append({
                "cue": cue,
                "group_name": gname,
                "group_color": gcolor,
                "text": s.get("text", ""),
                "label": s.get("label", ""),
                "enabled": s.get("enabled", True),
            })
            cue += 1
    _emit({
        "ok": True,
        "presentation_uuid": pres_uuid,
        "name": pres.get("id", {}).get("name", ""),
        "slides": slides,
    })

def trigger_slide(item_uuid, cue_index):
    """LEGACY: trigger via Ministries playlist.
    New code should use trigger_slide_pl."""
    cue = int(cue_index)
    api("GET", f"/playlist/{MIN_PLAYLIST_UUID}/{item_uuid}/{cue}/trigger")
    _emit({"ok": True, "item_uuid": item_uuid, "cue": cue})

def trigger_slide_pl(playlist_uuid, item_uuid, cue_index):
    """Trigger a slide via any playlist."""
    cue = int(cue_index)
    api("GET", f"/playlist/{playlist_uuid}/{item_uuid}/{cue}/trigger")
    _emit({"ok": True, "playlist_uuid": playlist_uuid, "item_uuid": item_uuid, "cue": cue})

def trigger_next():
    api("GET", "/trigger/next")
    _emit({"ok": True, "action": "next"})

def trigger_previous():
    api("GET", "/trigger/previous")
    _emit({"ok": True, "action": "previous"})

def clear_slide():
    api("GET", "/clear/layer/slide")
    _emit({"ok": True, "action": "clear"})

def delete_from_ministries(item_uuid):
    """LEGACY: remove a single playlist item from Ministries by item_uuid.
    New code should use delete_from_pl."""
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = pl.get("items", []) if pl else []
    new_items, removed_name = [], None
    for it in items:
        if it.get("id", {}).get("uuid") == item_uuid:
            removed_name = it.get("id", {}).get("name", "")
            continue
        new_items.append(normalize_v21_item(it))
    if removed_name is None:
        _emit({"ok": False, "error": "item_uuid not found in Ministries"})
        sys.exit(2)
    api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}", new_items)
    _emit({"ok": True, "removed": removed_name, "remaining": len(new_items)})

def delete_from_pl(playlist_uuid, item_uuid):
    """Remove an item from any playlist by item_uuid. Does NOT delete the .pro."""
    pl = api("GET", f"/playlist/{playlist_uuid}")
    items = pl.get("items", []) if pl else []
    new_items, removed_name = [], None
    for it in items:
        if it.get("id", {}).get("uuid") == item_uuid:
            removed_name = it.get("id", {}).get("name", "")
            continue
        new_items.append(normalize_v21_item(it))
    if removed_name is None:
        _emit({"ok": False, "error": "item_uuid not found in playlist"})
        sys.exit(2)
    api("PUT", f"/playlist/{playlist_uuid}", new_items)
    _emit({"ok": True, "removed": removed_name, "remaining": len(new_items)})

def reorder_ministries(item_uuids_csv):
    """LEGACY: reorder Ministries by comma-separated item UUIDs.
    New code should use reorder_pl."""
    new_order = [u.strip() for u in item_uuids_csv.split(",") if u.strip()]
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = pl.get("items", []) if pl else []
    by_uuid = {it.get("id", {}).get("uuid", ""): it for it in items}
    if set(new_order) != set(by_uuid.keys()):
        _emit({
            "ok": False,
            "error": "uuids must be a permutation of current items",
            "missing": list(set(by_uuid.keys()) - set(new_order)),
            "extra": list(set(new_order) - set(by_uuid.keys())),
        })
        sys.exit(2)
    new_items = [normalize_v21_item(by_uuid[u]) for u in new_order]
    api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}", new_items)
    _emit({"ok": True, "reordered": len(new_items)})

def reorder_pl(playlist_uuid, item_uuids_csv):
    """Reorder any playlist by comma-separated item UUIDs. Must be a perfect permutation."""
    new_order = [u.strip() for u in item_uuids_csv.split(",") if u.strip()]
    pl = api("GET", f"/playlist/{playlist_uuid}")
    items = pl.get("items", []) if pl else []
    by_uuid = {it.get("id", {}).get("uuid", ""): it for it in items}
    if set(new_order) != set(by_uuid.keys()):
        _emit({
            "ok": False,
            "error": "uuids must be a permutation of current items",
            "missing": list(set(by_uuid.keys()) - set(new_order)),
            "extra": list(set(new_order) - set(by_uuid.keys())),
        })
        sys.exit(2)
    new_items = [normalize_v21_item(by_uuid[u]) for u in new_order]
    api("PUT", f"/playlist/{playlist_uuid}", new_items)
    _emit({"ok": True, "reordered": len(new_items)})

def get_thumbnail(pres_uuid, cue_index, output_path):
    """Download thumbnail JPEG for a specific cue to output_path."""
    cue = int(cue_index)
    data = api_raw(f"/presentation/{pres_uuid}/thumbnail/{cue}")
    with open(output_path, "wb") as f:
        f.write(data)
    _emit({"ok": True, "path": output_path, "bytes": len(data)})

def get_thumbnails_bulk(pres_uuid):
    """Fetch every slide thumbnail for a presentation in parallel; emit base64 dict.
    Used by the remote control UI for the slide grid. PP serves thumbnails fast
    (~30ms each) so 8-way parallelism keeps even a 30-slide presentation under 1s."""
    import base64
    from concurrent.futures import ThreadPoolExecutor
    p = api("GET", f"/presentation/{pres_uuid}")
    pres = p.get("presentation", {}) if p else {}
    total = sum(len(g.get("slides", [])) for g in pres.get("groups", []))
    if total == 0:
        _emit({"ok": True, "presentation_uuid": pres_uuid, "count": 0, "thumbnails": {}})
        return
    def fetch_one(cue):
        try:
            data = api_raw(f"/presentation/{pres_uuid}/thumbnail/{cue}")
            return cue, base64.b64encode(data).decode("ascii")
        except Exception:
            return cue, None
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for cue, b64 in ex.map(fetch_one, range(total)):
            out[str(cue)] = b64
    _emit({"ok": True, "presentation_uuid": pres_uuid, "count": total, "thumbnails": out})

def get_active_thumbnail():
    """Return a base64 thumbnail of whatever slide is currently live in PP.
    Used by the floating program-feed panel in /control to show what's on screen.
    Resilient: if nothing is active (no presentation loaded, screen cleared),
    returns ok=true with active=false so the UI can show 'No active slide'.
    """
    import base64
    try:
        # PP v21: /presentation/slide_index returns the active slide info
        # Shape: {"presentation_index": {"index": N, "presentation_id": {"uuid": "..."}}}
        info = api("GET", "/presentation/slide_index")
    except Exception as e:
        _emit({"ok": True, "active": False, "reason": f"no_active_slide ({e})"})
        return

    if not info:
        _emit({"ok": True, "active": False, "reason": "no_response"})
        return

    pidx = info.get("presentation_index") or {}
    cue = pidx.get("index")
    pres_id = (pidx.get("presentation_id") or {}).get("uuid")

    if cue is None or pres_id is None:
        _emit({"ok": True, "active": False, "reason": "nothing_live"})
        return

    # Fetch the thumbnail for that cue
    try:
        data = api_raw(f"/presentation/{pres_id}/thumbnail/{cue}")
        b64 = base64.b64encode(data).decode("ascii")
        # Also return the presentation name for the UI label, if we can get it.
        pres_name = ""
        try:
            p = api("GET", f"/presentation/{pres_id}")
            if p:
                pres_name = (p.get("presentation", {}) or {}).get("name", "") or ""
        except Exception:
            pass
        _emit({
            "ok": True,
            "active": True,
            "presentation_uuid": pres_id,
            "presentation_name": pres_name,
            "cue_index": cue,
            "thumbnail_b64": b64,
        })
    except Exception as e:
        _emit({"ok": True, "active": False, "reason": f"thumbnail_fetch_failed ({e})"})

# =============================================================================
# ADD SLIDES — append media slides to an existing presentation in-place
# =============================================================================

def add_slides_to_pres(pres_uuid, folder):
    """Append media slides (images or videos) from a folder to an existing .pro file.
    Slides are added to the LAST cue group (or a new ungrouped section if there isn't one),
    at the end of the cue list. Returns count of slides appended."""
    library = get_library_dir()
    if not library:
        _emit({"ok": False, "error": "no library folder"}); return

    path, pres = find_pres_by_uuid(pres_uuid)
    if not pres:
        _emit({"ok": False, "error": f"presentation {pres_uuid} not found"}); return

    pres_name = pres.name
    print(f"Found {pres_name} at {path}")

    Presentation                   = find_msg("Presentation")
    PLATFORM_MACOS                 = find_enum("PLATFORM_MACOS")
    ACTION_TYPE_PRESENTATION_SLIDE = find_enum("ACTION_TYPE_PRESENTATION_SLIDE")
    ACTION_TYPE_MEDIA              = find_enum("ACTION_TYPE_MEDIA")
    LAYER_TYPE_FOREGROUND          = find_enum("LAYER_TYPE_FOREGROUND")
    COMPLETION_ACTION_TYPE_LAST    = find_enum("COMPLETION_ACTION_TYPE_LAST")
    ROOT_SHOW                      = find_enum("ROOT_SHOW")

    media = sorted(
        [f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith(ALL_EXTS)],
        key=natural_sort_key)
    if not media:
        _emit({"ok": False, "error": f"no usable media files in {folder}"}); return

    # Copy the new files into the presentation's existing assets dir.
    # If the presentation was originally created via _build_pro_for_media, its assets
    # live under Media/Assets/<name>/. We follow that same convention so PP's URL
    # resolution works correctly.
    target = os.path.join(ASSETS_DIR, pres_name)
    os.makedirs(target, exist_ok=True)
    copied = []
    for m in media:
        clean = sanitize_name(os.path.basename(m))
        dest = os.path.join(target, clean)
        base, ext = os.path.splitext(clean); n = 1
        while os.path.exists(dest):
            dest = os.path.join(target, f"{base}-{n}{ext}"); n += 1
        shutil.copy2(m, dest); copied.append(dest)
    print(f"Copied {len(copied)} file(s) to {target}")

    # Decide which cue_group to append to. Strategy: use the LAST existing group
    # so slides land at the very end of the presentation. If there are no groups
    # at all (edge case — shouldn't really happen), create one.
    if pres.cue_groups:
        target_group = pres.cue_groups[-1]
    else:
        target_group = pres.cue_groups.add()
        target_group.group.uuid.string = uid()
        target_group.group.name = ""

    # Build new cues, mirroring _build_pro_for_media's structure.
    new_cue_uuids = []
    for p in copied:
        fname = os.path.basename(p)
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        fmt = ext.upper().replace("JPEG", "JPG")
        cue_uuid = uid()
        new_cue_uuids.append(cue_uuid)

        cue = pres.cues.add()
        cue.uuid.string = cue_uuid
        cue.completion_action_type = COMPLETION_ACTION_TYPE_LAST
        cue.isEnabled = True

        a1 = cue.actions.add()
        a1.uuid.string = uid(); a1.label.text = fname
        a1.isEnabled = True; a1.type = ACTION_TYPE_PRESENTATION_SLIDE
        bs = a1.slide.presentation.base_slide
        bs.size.width = 1920; bs.size.height = 1080
        bs.uuid.string = uid()
        a1.slide.presentation.chord_chart.platform = PLATFORM_MACOS

        a2 = cue.actions.add()
        a2.uuid.string = uid(); a2.isEnabled = True
        a2.type = ACTION_TYPE_MEDIA
        el = a2.media.element
        el.uuid.string = uid()
        el.url.absolute_string = file_url(p)
        el.url.platform = PLATFORM_MACOS
        el.url.local.root = ROOT_SHOW
        el.url.local.path = f"Media/Assets/{pres_name}/{fname}"
        el.metadata.format = fmt
        el.image.drawing.natural_size.width = 1920
        el.image.drawing.natural_size.height = 1080
        a2.media.layer_type = LAYER_TYPE_FOREGROUND

    # Append the new cue UUIDs to the target group's cue_identifiers.
    for cu in new_cue_uuids:
        target_group.cue_identifiers.add().string = cu

    # Save — rename-then-write trick to force ProPresenter to reload.
    # PP doesn't notice in-place file overwrites (the inode is the same and PP
    # caches the parsed contents indefinitely). But PP DOES rescan when a file
    # at a known path "appears" — so we rename the existing .pro out of the way,
    # write the new contents at the original path (new inode), then delete the
    # old one. This makes PP treat it as a fresh file and reload from disk.
    # Confirmed working 2026-05-11 via the BRIDGE_TEST_* probe.
    bak = path + ".bridgetmp"
    serialized = pres.SerializeToString()
    try:
        if os.path.exists(bak):
            os.remove(bak)
        os.rename(path, bak)
        with open(path, "wb") as f: f.write(serialized)
        os.remove(bak)
    except Exception as e:
        # Fall back to plain in-place write so we never lose the file
        print(f"  rename-rewrite failed ({e}), falling back to in-place write")
        with open(path, "wb") as f: f.write(serialized)
        # Restore from .bak if the rewrite path errored mid-way
        if os.path.exists(bak) and not os.path.exists(path):
            os.rename(bak, path)
    print(f"Saved {path} with {len(new_cue_uuids)} new slide(s)")

    # Nudge ProPresenter to re-read the file from disk. PP caches presentations
    # in memory and only re-reads them on certain triggers. Two-step nudge:
    #   1. Force a libraries listing — this can sometimes prompt a rescan
    #   2. Force a fetch of the specific presentation — this is the reliable one
    # If PP is showing the presentation in its own UI (e.g., currently visible
    # in the library panel), the user may still need to click away and back to
    # see the new slides. That's a PP limitation, not something we can fully
    # automate. But this nudge handles the API-side cache so the Remote sees the
    # new slides on next get_slides.
    try:
        time.sleep(0.3)
        api("GET", "/libraries")
        api("GET", f"/presentation/{pres.uuid.string}")
    except Exception as e:
        print(f"  (reload nudge failed, non-fatal: {e})")

    _emit({
        "ok": True,
        "presentation_name": pres_name,
        "added_count": len(new_cue_uuids),
        "files_added": [os.path.basename(p) for p in copied],
    })



def find_pres_by_uuid(pres_uuid):
    """Find a .pro file in any library by presentation UUID. Returns (path, parsed_pres) or (None, None)."""
    if not os.path.isdir(LIBRARY_DIR): return None, None
    Presentation = find_msg("Presentation")
    target = pres_uuid.upper()
    for sub in os.listdir(LIBRARY_DIR):
        sub_dir = os.path.join(LIBRARY_DIR, sub)
        if not os.path.isdir(sub_dir): continue
        for fname in os.listdir(sub_dir):
            if not fname.endswith(".pro"): continue
            full = os.path.join(sub_dir, fname)
            try:
                pres = Presentation()
                with open(full, "rb") as f: pres.ParseFromString(f.read())
                if pres.uuid.string.upper() == target:
                    return full, pres
            except Exception: continue
    return None, None

def read_pres_for_sync(pres_uuid):
    """Extract the syncable structure from a .pro file: name, group structure,
    and per-slide RTF lyrics. Emits JSON for the cloud to forward to the destination Mac."""
    path, pres = find_pres_by_uuid(pres_uuid)
    if not pres:
        _emit({"ok": False, "error": f"presentation {pres_uuid} not found"})
        return

    # Build a list of cues with their RTF text
    cue_data = {}  # cue_uuid -> {rtf, label}
    for cue in pres.cues:
        if not cue.actions: continue
        a = cue.actions[0]
        if not a.HasField("slide") or not a.slide.HasField("presentation"): continue
        bs = a.slide.presentation.base_slide
        rtf = ""
        if bs.elements:
            el = bs.elements[0].element
            if el.HasField("text"):
                raw = el.text.rtf_data
                # rtf_data comes out of protobuf as bytes; need str for JSON
                if isinstance(raw, bytes):
                    try:
                        rtf = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        rtf = raw.decode("latin-1", errors="replace")
                else:
                    rtf = raw
        cue_data[cue.uuid.string] = {
            "rtf": rtf,
            "label": a.label.text or "",
        }

    # Walk groups to preserve order + group names
    slides_ordered = []
    for cg in pres.cue_groups:
        group_name = cg.group.name or ""
        for cid in cg.cue_identifiers:
            cd = cue_data.get(cid.string)
            if cd is None: continue
            slides_ordered.append({
                "group_name": group_name,
                "rtf": cd["rtf"],
                "label": cd["label"],
            })

    _emit({
        "ok": True,
        "name": pres.name,
        "source_size": {"width": pres.cues[0].actions[0].slide.presentation.base_slide.size.width if pres.cues else 1920,
                        "height": pres.cues[0].actions[0].slide.presentation.base_slide.size.height if pres.cues else 1080},
        "slides": slides_ordered,
    })

def _find_template_pres():
    """Find any .pro file in this Mac's library that has text-bearing slides — use it as
    a styling template. Returns (template_element_bytes, template_size, caps_mode) or
    (None, default, "none").
    caps_mode is "upper" if the template applies CAPITALIZATION_ALL_CAPS, else "none"."""
    Presentation = find_msg("Presentation")
    if not os.path.isdir(LIBRARY_DIR): return None, (1920, 1080), "none"
    # Look up the enum value for ALL_CAPS once
    try:
        ALL_CAPS = find_enum("CAPITALIZATION_ALL_CAPS")
    except Exception:
        ALL_CAPS = None
    for sub in os.listdir(LIBRARY_DIR):
        sub_dir = os.path.join(LIBRARY_DIR, sub)
        if not os.path.isdir(sub_dir): continue
        for fname in sorted(os.listdir(sub_dir)):
            if not fname.endswith(".pro"): continue
            full = os.path.join(sub_dir, fname)
            try:
                pres = Presentation()
                with open(full, "rb") as f: pres.ParseFromString(f.read())
            except Exception: continue
            for cue in pres.cues:
                if not cue.actions: continue
                a = cue.actions[0]
                if not a.HasField("slide") or not a.slide.HasField("presentation"): continue
                bs = a.slide.presentation.base_slide
                if not bs.elements: continue
                el_wrapper = bs.elements[0]
                if not el_wrapper.element.HasField("text"): continue
                # Found a usable template. Detect capitalization mode from text.attributes.capitalization.
                caps_mode = "none"
                try:
                    cap = el_wrapper.element.text.attributes.capitalization
                    if ALL_CAPS is not None and cap == ALL_CAPS:
                        caps_mode = "upper"
                except Exception:
                    pass
                template_bytes = el_wrapper.SerializeToString()
                size = (bs.size.width or 1920, bs.size.height or 1080)
                return template_bytes, size, caps_mode
    return None, (1920, 720), "none"

def _replace_rtf_text(rtf, new_text):
    """Replace the lyric body inside an RTF blob. Preserves all the formatting codes
    at the top (font, color, paragraph style, font size). The actual text starts after
    the LAST \\cfN marker (which is outside the colortbl group)."""
    if not rtf or not new_text:
        return rtf
    if isinstance(rtf, bytes):
        try: rtf = rtf.decode("utf-8")
        except UnicodeDecodeError: rtf = rtf.decode("latin-1", errors="replace")
    # Find ALL \cfN markers — RTF body color marker always comes AFTER the colortbl
    # group, so the LAST match is the one applied to the visible text.
    matches = list(re.finditer(r'\\cf\d+\s?', rtf))
    if not matches:
        return rtf  # unfamiliar RTF shape — bail
    last = matches[-1]
    head = rtf[:last.end()]
    # Find the closing brace at the end of the document
    tail_match = re.search(r'\}\s*$', rtf)
    tail = rtf[tail_match.start():] if tail_match else "}"
    # Escape RTF-significant chars in the new text
    escaped = new_text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    # Newlines in RTF lyric text become \\\n (literal backslash + newline)
    escaped = escaped.replace("\n", "\\\n")
    return head + escaped + tail

def _extract_text_from_rtf(rtf):
    """Pull plain lyric text out of an RTF blob.
    Walks the RTF, tracking brace depth and skipping known header groups
    (fonttbl, colortbl, expandedcolortbl, stylesheet, info, etc.) so their
    contents don't leak into the output."""
    if not rtf: return ""
    if isinstance(rtf, bytes):
        try: rtf = rtf.decode("utf-8")
        except UnicodeDecodeError: rtf = rtf.decode("latin-1", errors="replace")
    out = []
    i = 0
    depth = 0
    skip_until_depth = None  # if set, ignore everything until we exit this depth
    SKIP_KEYWORDS = ("fonttbl", "colortbl", "expandedcolortbl", "stylesheet",
                     "info", "pict", "object", "*", "filetbl", "listtable",
                     "listoverridetable", "rsidtbl", "generator", "themedata",
                     "datastore", "latentstyles")
    n = len(rtf)
    while i < n:
        c = rtf[i]
        if c == "\\":
            if i + 1 >= n:
                i += 1; continue
            nxt = rtf[i+1]
            # Escaped char
            if nxt in ("\\", "{", "}"):
                if skip_until_depth is None:
                    out.append(nxt)
                i += 2; continue
            # \\\n is a line break
            if nxt == "\n":
                if skip_until_depth is None:
                    out.append("\n")
                i += 2; continue
            # \* marks a destination — the whole group should be ignored
            if nxt == "*":
                if skip_until_depth is None:
                    skip_until_depth = depth
                i += 2; continue
            # \' followed by 2 hex digits = special character
            if nxt == "'" and i + 3 < n:
                # Skip the 2 hex digits — we're not bothering with proper decoding
                i += 4; continue
            # Control word: alphas + optional digits
            j = i + 1
            while j < n and rtf[j].isalpha():
                j += 1
            word = rtf[i+1:j]
            # Optional numeric param
            if j < n and (rtf[j] == "-" or rtf[j].isdigit()):
                while j < n and (rtf[j] == "-" or rtf[j].isdigit()):
                    j += 1
            # Optional space delimiter
            if j < n and rtf[j] == " ":
                j += 1
            # Action on certain control words
            if skip_until_depth is None:
                if word == "par" or word == "line":
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
            # If we just entered a group (the previous char was '{') and this control
            # word is a header keyword, mark this whole group for skipping
            if skip_until_depth is None and word in SKIP_KEYWORDS:
                skip_until_depth = depth
            i = j
            continue
        if c == "{":
            depth += 1
            i += 1; continue
        if c == "}":
            depth -= 1
            if skip_until_depth is not None and depth < skip_until_depth:
                skip_until_depth = None
            i += 1; continue
        if c in ("\r", "\n"):
            # Raw newlines in RTF source are not significant
            i += 1; continue
        # Plain literal char
        if skip_until_depth is None:
            out.append(c)
        i += 1
    text = "".join(out)
    # Strip leading punctuation/whitespace artifacts (semicolons from colortbl spillover etc.)
    text = re.sub(r"^[;\s]+", "", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()

def sync_pres_to_playlist(name, playlist_uuid, slides_json):
    """Build a new presentation on THIS Mac using the slides_json from the source Mac,
    styled with this Mac's template (font, bounds, theme size). Then add to the named playlist.

    slides_json format: JSON array of {"group_name": "...", "rtf": "...", "label": "..."}
    Pulled from `read_pres_for_sync` on the source Mac.
    """
    library = get_library_dir()
    if not library:
        _emit({"ok": False, "error": "no library"}); return

    try:
        slides = json.loads(slides_json)
    except Exception as e:
        _emit({"ok": False, "error": f"bad slides_json: {e}"}); return
    if not isinstance(slides, list) or not slides:
        _emit({"ok": False, "error": "slides_json is empty or not a list"}); return

    # Find a unique name on this Mac. If "Holy Forever" exists, use "Holy Forever 2" etc.
    base_name = sanitize_name(name)
    final_name = base_name
    n = 2
    while os.path.exists(os.path.join(library, f"{final_name}.pro")):
        final_name = f"{base_name} {n}"
        n += 1
        if n > 50: break  # safety

    # Pull our local template — gives us this Mac's font / bounds / size / scroller / caps mode
    template_bytes, (tw, th), caps_mode = _find_template_pres()
    if template_bytes is None:
        _emit({"ok": False, "error": "no template presentation on this Mac to style from — need at least one text song in the library"}); return

    Presentation                   = find_msg("Presentation")
    PLATFORM_MACOS                 = find_enum("PLATFORM_MACOS")
    APPLICATION_PROPRESENTER       = find_enum("APPLICATION_PROPRESENTER")
    ACTION_TYPE_PRESENTATION_SLIDE = find_enum("ACTION_TYPE_PRESENTATION_SLIDE")
    COMPLETION_ACTION_TYPE_LAST    = find_enum("COMPLETION_ACTION_TYPE_LAST")

    pres = Presentation()
    ai = pres.application_info
    ai.platform = PLATFORM_MACOS
    ai.platform_version.major_version = 26
    ai.platform_version.minor_version = 2
    ai.application = APPLICATION_PROPRESENTER
    ai.application_version.major_version = 21
    ai.application_version.patch_version = 1
    ai.application_version.build = "318767361"
    pres.uuid.string = uid()
    pres.name = final_name
    pres.chord_chart.platform = PLATFORM_MACOS

    # Build groups, preserving order. Consecutive slides with the same group_name go in the same group.
    # Empty group_name = an unnamed group (PP shows these as ungrouped).
    cue_uuids_by_group = []  # list of (group_name, [cue_uuids])
    for s in slides:
        gn = s.get("group_name", "") or ""
        if cue_uuids_by_group and cue_uuids_by_group[-1][0] == gn:
            cue_uuids_by_group[-1][1].append(uid())
        else:
            cue_uuids_by_group.append((gn, [uid()]))

    # Flatten cue_uuids in order
    flat_cue_uuids = [u for _, group_uuids in cue_uuids_by_group for u in group_uuids]

    # Add cue_groups
    for group_name, cue_uuids in cue_uuids_by_group:
        cg = pres.cue_groups.add()
        cg.group.uuid.string = uid()
        cg.group.name = group_name
        for cu in cue_uuids:
            cg.cue_identifiers.add().string = cu

    # We need a scratch "Element wrapper" message to deserialize the template into.
    # The base_slide.elements field is a repeated submessage — we get the type via the descriptor.
    template_pres = Presentation()
    template_cue = template_pres.cues.add()
    template_action = template_cue.actions.add()
    template_action.type = ACTION_TYPE_PRESENTATION_SLIDE
    ElementWrapperType = template_action.slide.presentation.base_slide.elements.add().__class__

    # Build cues. For each slide, copy the template element wrapper and swap in our RTF text.
    for cue_uuid, slide_data in zip(flat_cue_uuids, slides):
        cue = pres.cues.add()
        cue.uuid.string = cue_uuid
        cue.completion_action_type = COMPLETION_ACTION_TYPE_LAST
        cue.isEnabled = True
        action = cue.actions.add()
        action.uuid.string = uid()
        action.label.text = slide_data.get("label", "") or ""
        action.isEnabled = True
        action.type = ACTION_TYPE_PRESENTATION_SLIDE

        bs = action.slide.presentation.base_slide
        bs.uuid.string = uid()
        bs.size.width = tw
        bs.size.height = th
        action.slide.presentation.chord_chart.platform = PLATFORM_MACOS

        # Clone the template element wrapper and swap in the source's lyrics
        new_wrapper = bs.elements.add()
        new_wrapper.ParseFromString(template_bytes)
        # Fresh UUID for the element so it doesn't collide
        new_wrapper.element.uuid.string = uid()
        # Inject the source's RTF text into the cloned element's text field
        if new_wrapper.element.HasField("text"):
            src_rtf = slide_data.get("rtf", "") or ""
            if src_rtf:
                # The template's rtf_data may also be bytes — coerce to str for editing
                tmpl_rtf = new_wrapper.element.text.rtf_data
                if isinstance(tmpl_rtf, bytes):
                    try:
                        tmpl_rtf = tmpl_rtf.decode("utf-8")
                    except UnicodeDecodeError:
                        tmpl_rtf = tmpl_rtf.decode("latin-1", errors="replace")
                # Use the cloned element's RTF (this Mac's font + styling) and swap in
                # just the lyric text from the source RTF
                src_text = _extract_text_from_rtf(src_rtf)
                if src_text:
                    # Match the destination template's capitalization. ProPresenter's
                    # CAPITALIZATION_ALL_CAPS protobuf flag only renders existing
                    # uppercase characters as caps — anything typed lowercase stays
                    # lowercase. So we manually uppercase here when the template asks for it.
                    if caps_mode == "upper":
                        src_text = src_text.upper()
                    new_rtf = _replace_rtf_text(tmpl_rtf, src_text)
                    # rtf_data field is `bytes` in protobuf — encode back
                    new_wrapper.element.text.rtf_data = new_rtf.encode("utf-8") if isinstance(new_rtf, str) else new_rtf

    # Serialize and save
    out = os.path.join(library, f"{final_name}.pro")
    with open(out, "wb") as f: f.write(pres.SerializeToString())

    # Add to playlist
    time.sleep(1.5)
    pl = api("GET", f"/playlist/{playlist_uuid}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    items.append(build_v21_playlist_item(pres.uuid.string, final_name, len(items)))
    api("PUT", f"/playlist/{playlist_uuid}", items)

    _emit({
        "ok": True,
        "created_name": final_name,
        "renamed": final_name != base_name,
        "presentation_uuid": pres.uuid.string,
        "slides_count": len(slides),
    })


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: bridge.py <command> [args]")
        print("  Content (legacy):  create <name> <folder> | delete <name> | add_existing <name>")
        print("  Content (multi):   create_in_playlist <name> <folder> <playlist_uuid>")
        print("                     add_existing_to_playlist <name> <playlist_uuid>")
        print("  Sync:              read_pres_for_sync <pres_uuid>")
        print("                     sync_pres_to_playlist <name> <playlist_uuid> <slides_json>")
        print("  Read:              list_playlists | list_ministries | list_playlist_items <playlist_uuid>")
        print("                     get_slides <pres_uuid>")
        print("                     get_thumbnail <pres_uuid> <cue_index> <output_path>")
        print("                     get_thumbnails_bulk <pres_uuid>")
        print("                     get_active_thumbnail")
        print("  Trigger:           trigger_slide <item_uuid> <cue_index>            (legacy: Ministries)")
        print("                     trigger_slide_pl <playlist_uuid> <item_uuid> <cue_index>")
        print("                     trigger_next | trigger_previous | clear_slide")
        print("  Mutate:            delete_from_min <item_uuid>                      (legacy: Ministries)")
        print("                     delete_from_pl <playlist_uuid> <item_uuid>")
        print("                     reorder_min <uuid1,uuid2,...>                    (legacy: Ministries)")
        print("                     reorder_pl <playlist_uuid> <uuid1,uuid2,...>")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "create":
        if len(sys.argv) < 4: print("Usage: bridge.py create <name> <folder>"); sys.exit(1)
        create_presentation(sys.argv[2], sys.argv[3])
    elif cmd == "create_in_playlist":
        if len(sys.argv) < 5: print("Usage: bridge.py create_in_playlist <name> <folder> <playlist_uuid>"); sys.exit(1)
        create_in_playlist(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "delete":
        if len(sys.argv) < 3: print("Usage: bridge.py delete <name>"); sys.exit(1)
        delete_presentation(sys.argv[2])
    elif cmd == "add_existing":
        if len(sys.argv) < 3: print("Usage: bridge.py add_existing <name>"); sys.exit(1)
        add_existing_to_ministries(sys.argv[2])
    elif cmd == "add_existing_to_playlist":
        if len(sys.argv) < 4: print("Usage: bridge.py add_existing_to_playlist <name> <playlist_uuid>"); sys.exit(1)
        add_existing_to_playlist(sys.argv[2], sys.argv[3])
    elif cmd == "list_playlists":
        list_playlists()
    elif cmd == "list_ministries":
        list_ministries()
    elif cmd == "list_playlist_items":
        if len(sys.argv) < 3: print("Usage: bridge.py list_playlist_items <playlist_uuid>"); sys.exit(1)
        list_playlist_items(sys.argv[2])
    elif cmd == "get_slides":
        if len(sys.argv) < 3: print("Usage: bridge.py get_slides <pres_uuid>"); sys.exit(1)
        get_slides(sys.argv[2])
    elif cmd == "trigger_slide":
        if len(sys.argv) < 4: print("Usage: bridge.py trigger_slide <item_uuid> <cue_index>"); sys.exit(1)
        trigger_slide(sys.argv[2], sys.argv[3])
    elif cmd == "trigger_slide_pl":
        if len(sys.argv) < 5: print("Usage: bridge.py trigger_slide_pl <playlist_uuid> <item_uuid> <cue_index>"); sys.exit(1)
        trigger_slide_pl(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "trigger_next":
        trigger_next()
    elif cmd == "trigger_previous":
        trigger_previous()
    elif cmd == "clear_slide":
        clear_slide()
    elif cmd == "delete_from_min":
        if len(sys.argv) < 3: print("Usage: bridge.py delete_from_min <item_uuid>"); sys.exit(1)
        delete_from_ministries(sys.argv[2])
    elif cmd == "delete_from_pl":
        if len(sys.argv) < 4: print("Usage: bridge.py delete_from_pl <playlist_uuid> <item_uuid>"); sys.exit(1)
        delete_from_pl(sys.argv[2], sys.argv[3])
    elif cmd == "reorder_min":
        if len(sys.argv) < 3: print("Usage: bridge.py reorder_min <uuid1,uuid2,...>"); sys.exit(1)
        reorder_ministries(sys.argv[2])
    elif cmd == "reorder_pl":
        if len(sys.argv) < 4: print("Usage: bridge.py reorder_pl <playlist_uuid> <uuid1,uuid2,...>"); sys.exit(1)
        reorder_pl(sys.argv[2], sys.argv[3])
    elif cmd == "get_thumbnail":
        if len(sys.argv) < 5: print("Usage: bridge.py get_thumbnail <pres_uuid> <cue_index> <output_path>"); sys.exit(1)
        get_thumbnail(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "get_thumbnails_bulk":
        if len(sys.argv) < 3: print("Usage: bridge.py get_thumbnails_bulk <pres_uuid>"); sys.exit(1)
        get_thumbnails_bulk(sys.argv[2])
    elif cmd == "get_active_thumbnail":
        get_active_thumbnail()
    elif cmd == "read_pres_for_sync":
        if len(sys.argv) < 3: print("Usage: bridge.py read_pres_for_sync <pres_uuid>"); sys.exit(1)
        read_pres_for_sync(sys.argv[2])
    elif cmd == "sync_pres_to_playlist":
        if len(sys.argv) < 5: print("Usage: bridge.py sync_pres_to_playlist <name> <playlist_uuid> <slides_json>"); sys.exit(1)
        sync_pres_to_playlist(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "add_slides_to_pres":
        if len(sys.argv) < 4: print("Usage: bridge.py add_slides_to_pres <pres_uuid> <folder>"); sys.exit(1)
        add_slides_to_pres(sys.argv[2], sys.argv[3])
    else: print(f"Unknown: {cmd}"); sys.exit(1)
