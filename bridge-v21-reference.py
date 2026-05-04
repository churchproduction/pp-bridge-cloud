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

def find_in_playlist(plid, name):
    pl = api("GET", f"/playlist/{plid}")
    items = pl.get("items", []) if pl else []
    for i, it in enumerate(items):
        if it.get("id", {}).get("name") == name:
            return i, it, items
    return None, None, items

def delete_presentation(raw):
    name = sanitize_name(raw)
    print(f"Deleting '{name}'...")
    library = get_library_dir()
    idx, item, items = find_in_playlist(MIN_PLAYLIST_UUID, name)
    if item is not None:
        api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}",
            [normalize_v21_item(it) for j, it in enumerate(items) if j != idx])
        print(f"  removed from Ministries")
    if library:
        pro = os.path.join(library, f"{name}.pro")
        if os.path.exists(pro): os.remove(pro); print("  deleted .pro")
    assets = os.path.join(ASSETS_DIR, name)
    if os.path.isdir(assets): shutil.rmtree(assets); print("  deleted assets")

def normalize_v21_item(it):
    """Ensure an item from GET response has all fields needed for PUT."""
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
    if "destination" not in it:
        it["destination"] = "presentation"
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

def add_existing_to_ministries(raw_name):
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
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    for it in items:
        info = it.get("presentation_info", {}) or {}
        if info.get("presentation_uuid") == pres_uuid:
            print(f"Already in Ministries — skipping")
            return
    items.append(build_v21_playlist_item(pres_uuid, name, len(items)))
    api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}", items)
    print(f"Added '{name}' to Ministries (now {len(items)} items)")

def create_presentation(raw, folder):
    name = sanitize_name(raw)
    folder = os.path.expanduser(folder)
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
    pro = os.path.join(library, f"{name}.pro")
    if os.path.exists(pro):
        print(f"'{name}' exists — replacing"); delete_presentation(name)
    media = sorted([f for f in glob.glob(os.path.join(folder, "*"))
                    if f.lower().endswith(ALL_EXTS)])
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
    print(f"[4/4] Adding to Ministries...")
    time.sleep(1.5)
    pl = api("GET", f"/playlist/{MIN_PLAYLIST_UUID}")
    items = [normalize_v21_item(it) for it in (pl.get("items", []) if pl else [])]
    items.append(build_v21_playlist_item(pres.uuid.string, name, len(items)))
    api("PUT", f"/playlist/{MIN_PLAYLIST_UUID}", items)
    print(f"\nDone — '{name}' ({len(copied)} slides)\n")

# =============================================================================
# REMOTE-CONTROL COMMANDS (added for production GUI)
# All emit JSON to stdout for the agent to forward back to the cloud.
# =============================================================================

def _emit(obj):
    print(json.dumps(obj))

def list_ministries():
    """Emit Ministries playlist items as JSON list."""
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
    """Trigger a slide via playlist path: /playlist/<min>/<item>/<cue>/trigger."""
    cue = int(cue_index)
    api("GET", f"/playlist/{MIN_PLAYLIST_UUID}/{item_uuid}/{cue}/trigger")
    _emit({"ok": True, "item_uuid": item_uuid, "cue": cue})

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
    """Remove a single playlist item by item_uuid. Does NOT delete the .pro."""
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

def reorder_ministries(item_uuids_csv):
    """Reorder Ministries by comma-separated item UUIDs. Must be a perfect permutation."""
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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: bridge.py <command> [args]")
        print("  Content:   create <name> <folder> | delete <name> | add_existing <name>")
        print("  Remote:    list_ministries | get_slides <pres_uuid>")
        print("             trigger_slide <item_uuid> <cue_index>")
        print("             trigger_next | trigger_previous | clear_slide")
        print("             delete_from_min <item_uuid>")
        print("             reorder_min <uuid1,uuid2,...>")
        print("             get_thumbnail <pres_uuid> <cue_index> <output_path>")
        print("             get_thumbnails_bulk <pres_uuid>")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "create":
        if len(sys.argv) < 4: print("Usage: bridge.py create <name> <folder>"); sys.exit(1)
        create_presentation(sys.argv[2], sys.argv[3])
    elif cmd == "delete":
        if len(sys.argv) < 3: print("Usage: bridge.py delete <name>"); sys.exit(1)
        delete_presentation(sys.argv[2])
    elif cmd == "add_existing":
        if len(sys.argv) < 3: print("Usage: bridge.py add_existing <name>"); sys.exit(1)
        add_existing_to_ministries(sys.argv[2])
    elif cmd == "list_ministries":
        list_ministries()
    elif cmd == "get_slides":
        if len(sys.argv) < 3: print("Usage: bridge.py get_slides <pres_uuid>"); sys.exit(1)
        get_slides(sys.argv[2])
    elif cmd == "trigger_slide":
        if len(sys.argv) < 4: print("Usage: bridge.py trigger_slide <item_uuid> <cue_index>"); sys.exit(1)
        trigger_slide(sys.argv[2], sys.argv[3])
    elif cmd == "trigger_next":
        trigger_next()
    elif cmd == "trigger_previous":
        trigger_previous()
    elif cmd == "clear_slide":
        clear_slide()
    elif cmd == "delete_from_min":
        if len(sys.argv) < 3: print("Usage: bridge.py delete_from_min <item_uuid>"); sys.exit(1)
        delete_from_ministries(sys.argv[2])
    elif cmd == "reorder_min":
        if len(sys.argv) < 3: print("Usage: bridge.py reorder_min <uuid1,uuid2,...>"); sys.exit(1)
        reorder_ministries(sys.argv[2])
    elif cmd == "get_thumbnail":
        if len(sys.argv) < 5: print("Usage: bridge.py get_thumbnail <pres_uuid> <cue_index> <output_path>"); sys.exit(1)
        get_thumbnail(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "get_thumbnails_bulk":
        if len(sys.argv) < 3: print("Usage: bridge.py get_thumbnails_bulk <pres_uuid>"); sys.exit(1)
        get_thumbnails_bulk(sys.argv[2])
    else: print(f"Unknown: {cmd}"); sys.exit(1)
