# -*- coding: utf-8 -*-
"""
proxmox_monitor.py — Surveillance et contrôle de l'hyperviseur Proxmox VE.

- API Proxmox via token API (PVEAPIToken=user@realm!tokenid + secret)
- Snapshot complet du cluster en UN appel (/cluster/resources)
- Détection d'anomalies (VM arrêtée, CPU/RAM/disque saturés, nœud down)
- Rapport détaillé avec solutions proposées
- Notifications ntfy en cas de problème
- Actions de contrôle : start/stop/reboot d'une VM (avec validation humaine côté cerveau)

Utilisé par :
- agent_server.py : boucle de surveillance toutes les 15 min (si activée) + outils du cerveau Mistral
- web_app.py / app_client.py : réglages (URL, token, secret, ntfy)
"""

import json
import ssl
import time
import urllib.request
import urllib.error

REPORT_FILE = None  # défini par init (à côté du module)
LAST_REPORT = ""
LAST_ISSUES = []


def init(base_dir):
    global REPORT_FILE
    import os
    REPORT_FILE = os.path.join(base_dir, "proxmox_last_report.txt")


# ==============================================================================
# CLIENT API PROXMOX
# ==============================================================================

def normalize_proxmox_url(url: str) -> str:
    """Normalise l'URL Proxmox : ajoute https:// si schéma absent, :8006 si port absent."""
    base = (url or "").strip().rstrip("/")
    if not base:
        raise ValueError("URL Proxmox non configurée")
    if not base.startswith("http"):
        base = "https://" + base
    # Port manquant -> 8006 (port API par défaut de Proxmox VE)
    import re as _re
    m = _re.match(r"^(https?://)([^/:]+)$", base)
    if m:
        base = m.group(1) + m.group(2) + ":8006"
    return base


def validate_proxmox_credentials(token: str, secret: str):
    """Valide le format du token ID et du secret. Lève ValueError avec un message clair en français."""
    token = (token or "").strip()
    if not token:
        raise ValueError("Token ID vide : il faut l'ID complet, ex. root@pam!nomduToken")
    if "@" not in token or "!" not in token:
        raise ValueError(
            "Token ID incomplet : il faut l'ID complet, ex. root@pam!nomduToken "
            "(utilisateur@realm!nomDuToken, tout collé)"
        )
    if not (secret or "").strip():
        raise ValueError("Secret du token vide : copie l'UUID secret généré à la création du token")


def _api_request(url: str, token: str, secret: str, method: str = "GET", data=None, timeout: int = 20) -> dict:
    """Appel API Proxmox. Retourne le dict 'data' ou lève une exception explicite."""
    validate_proxmox_credentials(token, secret)
    base = normalize_proxmox_url(url)
    path = data.get("path", "") if isinstance(data, dict) else ""
    body = json.dumps(data.get("body", {})).encode() if isinstance(data, dict) and data.get("body") else None
    req = urllib.request.Request(base + "/api2/json" + path, data=body, method=method)
    req.add_header("Authorization", f"PVEAPIToken={token.strip()}={secret.strip()}")
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # certificat auto-signé accepté (réseau local)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("data", {})


def get_cluster_snapshot(url: str, token: str, secret: str) -> dict:
    """Snapshot complet du cluster : tous les nœuds et VMs en un appel."""
    resources = _api_request(url, token, secret, data={"path": "/cluster/resources"})
    nodes, vms, storage = [], [], []
    for r in resources:
        rtype = r.get("type")
        if rtype == "node":
            nodes.append(r)
        elif rtype in ("qemu", "lxc"):
            vms.append(r)
        elif rtype == "storage":
            storage.append(r)
    return {"nodes": nodes, "vms": vms, "storage": storage, "ts": time.time()}


# ==============================================================================
# ANALYSE & RAPPORT
# ==============================================================================

def analyze_snapshot(snapshot: dict) -> list:
    """Retourne la liste des problèmes détectés, chacun avec sévérité et solutions."""
    issues = []

    for n in snapshot.get("nodes", []):
        name = n.get("node", "?")
        if n.get("status") != "online":
            issues.append({
                "severity": "CRITIQUE", "element": f"Nœud {name}",
                "problem": f"Le nœud est {n.get('status', 'inconnu')} (hors ligne)",
                "solutions": [
                    "Vérifier l'alimentation et la carte mère du serveur",
                    "Se connecter à l'interface Proxmox d'un autre nœud pour voir l'état du cluster",
                    "Si le nœud est volontairement en maintenance, l'indiquer dans le contexte de l'agent",
                ],
            })
            continue
        cpu = n.get("cpu", 0) * 100
        if cpu > 85:
            issues.append({
                "severity": "ALERTE", "element": f"Nœud {name}",
                "problem": f"CPU à {cpu:.0f}% (saturé)",
                "solutions": [
                    "Identifier les VMs les plus gourmandes et envisager une migration à chaud vers un autre nœud",
                    "Vérifier qu'aucun processus zombie ne tourne sur le nœud",
                ],
            })
        mem = n.get("mem", 0)
        maxmem = n.get("maxmem", 1)
        if maxmem and mem / maxmem > 0.90:
            issues.append({
                "severity": "ALERTE", "element": f"Nœud {name}",
                "problem": f"RAM à {mem / maxmem * 100:.0f}% (presque pleine)",
                "solutions": [
                    "Migrer une VM gourmande vers un nœud moins chargé (migration à chaud)",
                    "Envisager d'ajouter de la RAM au nœud",
                ],
            })
        rootdisk = n.get("disk", 0)
        rootmax = n.get("maxdisk", 1)
        if rootmax and rootdisk / rootmax > 0.90:
            issues.append({
                "severity": "ALERTE", "element": f"Nœud {name}",
                "problem": f"Disque système à {rootdisk / rootmax * 100:.0f}%",
                "solutions": ["Nettoyer les anciens backups/ISO (pve-storage)", "Vérifier la rétention des snapshots"],
            })

    for v in snapshot.get("vms", []):
        vmid = v.get("vmid", "?")
        name = v.get("name", f"vmid-{vmid}")
        node = v.get("node", "?")
        label = f"VM {vmid} ({name}) sur {node}"
        status = v.get("status", "unknown")
        if status != "running":
            issues.append({
                "severity": "ALERTE", "element": label,
                "problem": f"La VM est à l'état '{status}'",
                "solutions": [
                    f"Si elle doit tourner : la démarrer (action 'start' sur vmid {vmid})",
                    "Vérifier dans les logs Proxmox pourquoi elle s'est arrêtée (crash OOM ? arrêt manuel ?)",
                    "Si l'arrêt est volontaire (maintenance), l'ignorer",
                ],
            })
            continue
        cpu = v.get("cpu", 0) * 100
        if cpu > 90:
            issues.append({
                "severity": "ALERTE", "element": label,
                "problem": f"CPU à {cpu:.0f}% en continu",
                "solutions": [
                    "Se connecter à la VM pour identifier le processus responsable",
                    "Envisager un redémarrage planifié si elle est bloquée",
                ],
            })
        mem = v.get("mem", 0)
        maxmem = v.get("maxmem", 1)
        if maxmem and mem / maxmem > 0.90:
            issues.append({
                "severity": "ALERTE", "element": label,
                "problem": f"RAM à {mem / maxmem * 100:.0f}%",
                "solutions": ["Augmenter la RAM allouée à la VM", "Redémarrer la VM si une fuite mémoire est suspectée"],
            })

    for s in snapshot.get("storage", []):
        sid = s.get("storage", s.get("id", "?"))
        used = s.get("used", 0)
        total = s.get("maxdisk", s.get("total", 1))
        if total and used / total > 0.85 and s.get("active", 1):
            issues.append({
                "severity": "ALERTE", "element": f"Stockage {sid}",
                "problem": f"Espace utilisé à {used / total * 100:.0f}%",
                "solutions": [
                    "Nettoyer les vieux backups (vzdump) selon la politique de rétention",
                    "Supprimer les snapshots obsolètes",
                ],
            })
    return issues


def format_report(snapshot: dict, issues: list) -> str:
    """Rapport détaillé lisible : cartographie + problèmes + solutions."""
    lines = ["🖥️ RAPPORT PROXMOX — " + time.strftime("%d/%m/%Y %H:%M")]
    nodes = snapshot.get("nodes", [])
    vms = snapshot.get("vms", [])
    lines.append(f"\n📊 Cluster : {len(nodes)} nœud(s), {len(vms)} VM(s)/CT(s)")
    for n in nodes:
        cpu = n.get("cpu", 0) * 100
        mem_pct = (n.get("mem", 0) / n.get("maxmem", 1) * 100) if n.get("maxmem") else 0
        lines.append(f"  🖥️ {n.get('node', '?')} : {'🟢' if n.get('status') == 'online' else '🔴'} "
                     f"CPU {cpu:.0f}% | RAM {mem_pct:.0f}% | uptime {int(n.get('uptime', 0) // 3600)}h")
    running = [v for v in vms if v.get("status") == "running"]
    lines.append(f"  ▶️ {len(running)}/{len(vms)} VMs en cours d'exécution")
    if not issues:
        lines.append("\n✅ AUCUN PROBLÈME DÉTECTÉ : tout est nominal.")
    else:
        lines.append(f"\n⚠️ {len(issues)} PROBLÈME(S) DÉTECTÉ(S) :")
        for i, iss in enumerate(issues, 1):
            lines.append(f"\n{i}. [{iss['severity']}] {iss['element']}")
            lines.append(f"   Problème : {iss['problem']}")
            lines.append("   Solutions proposées :")
            for j, sol in enumerate(iss["solutions"], 1):
                lines.append(f"     {j}. {sol}")
    return "\n".join(lines)


def save_report(report: str, issues: list):
    """Sauvegarde le dernier rapport pour l'interface web (page Problèmes)."""
    global LAST_REPORT, LAST_ISSUES
    LAST_REPORT = report
    LAST_ISSUES = issues
    if REPORT_FILE:
        try:
            with open(REPORT_FILE, "w", encoding="utf-8") as f:
                f.write(report)
        except Exception:
            pass


def load_last_report() -> str:
    if LAST_REPORT:
        return LAST_REPORT
    if REPORT_FILE:
        try:
            with open(REPORT_FILE, "r", encoding="utf-8-sig") as f:
                return f.read()
        except Exception:
            pass
    return ""


# ==============================================================================
# NTFY
# ==============================================================================

def send_ntfy(ntfy_url: str, topic: str, title: str, message: str, priority: str = "high", tags: str = "warning") -> bool:
    """Envoie une notification ntfy. URL par défaut https://ntfy.sh."""
    base = (ntfy_url or "https://ntfy.sh").strip().rstrip("/")
    topic = (topic or "").strip()
    if not topic:
        return False
    try:
        req = urllib.request.Request(f"{base}/{topic}", data=message.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Priority", priority)
        req.add_header("Tags", tags)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ==============================================================================
# POINT D'ENTRÉE : VÉRIFICATION COMPLÈTE
# ==============================================================================

def get_dashboard_data(creds: dict) -> dict:
    """Données structurées pour le tableau de bord web (style Datacenter Summary).
    Itère sur tous les hôtes configurés (proxmox_hosts ou rétrocompatibilité clé unique)."""
    hosts = get_proxmox_hosts(creds)
    if not hosts:
        return {"ok": False, "configured": False, "error": "Proxmox non configuré (URL/token/secret) — Réglages → Supervision."}
    nodes, vms, storage, issues = [], [], [], []
    errors = []
    for host in hosts:
        try:
            snapshot = get_cluster_snapshot(host["url"], host["token_id"], host["token_secret"])
        except Exception as e:
            errors.append(f"[{host['name']}] {e}")
            continue
        h_issues = analyze_snapshot(snapshot)
        for i in h_issues:
            i["element"] = f"[{host['name']}] {i['element']}"
        issues.extend(h_issues)
        for n in snapshot.get("nodes", []):
            n["_host"] = host["name"]
            nodes.append(n)
        for v in snapshot.get("vms", []):
            v["_host"] = host["name"]
            vms.append(v)
        for s in snapshot.get("storage", []):
            s["_host"] = host["name"]
            storage.append(s)
    if errors and not nodes:
        return {"ok": False, "configured": True, "error": "Erreur d'accès API : " + " | ".join(errors)}
    snapshot = {"nodes": nodes, "vms": vms, "storage": storage}
    nodes = []
    for n in snapshot.get("nodes", []):
        nodes.append({
            "name": n.get("node", "?"),
            "host": n.get("_host", ""),
            "online": n.get("status") == "online",
            "cpu": round(n.get("cpu", 0) * 100, 1),
            "ram": round((n.get("mem", 0) / n.get("maxmem", 1)) * 100, 1) if n.get("maxmem") else 0,
            "disk": round((n.get("disk", 0) / n.get("maxdisk", 1)) * 100, 1) if n.get("maxdisk") else 0,
            "ram_gb": round(n.get("mem", 0) / 1e9, 1), "ram_max_gb": round(n.get("maxmem", 0) / 1e9, 1),
            "uptime_h": int(n.get("uptime", 0) // 3600),
        })
    vms = []
    for v in snapshot.get("vms", []):
        vms.append({
            "vmid": v.get("vmid"), "name": v.get("name", "?"), "node": v.get("node", "?"),
            "host": v.get("_host", ""),
            "type": "CT" if v.get("type") == "lxc" else "VM",
            "status": v.get("status", "?"),
            "cpu": round(v.get("cpu", 0) * 100, 1),
            "ram": round((v.get("mem", 0) / v.get("maxmem", 1)) * 100, 1) if v.get("maxmem") else 0,
            "ram_gb": round(v.get("mem", 0) / 1e9, 1), "ram_max_gb": round(v.get("maxmem", 0) / 1e9, 1),
        })
    storage = []
    for s in snapshot.get("storage", []):
        total = s.get("maxdisk", s.get("total", 0))
        storage.append({
            "name": s.get("storage", "?"), "active": bool(s.get("active", 0)),
            "used_gb": round(s.get("used", 0) / 1e9, 1), "total_gb": round(total / 1e9, 1),
            "pct": round((s.get("used", 0) / total) * 100, 1) if total else 0,
        })
    return {"ok": True, "configured": True, "nodes": nodes, "vms": vms, "storage": storage,
            "issues": issues, "ts": time.time()}


def get_proxmox_hosts(creds: dict) -> list:
    """Retourne la liste des hôtes Proxmox à surveiller.
    Format : [{"name": str, "url": str, "token_id": str, "token_secret": str}]
    Rétrocompatibilité : si proxmox_hosts est absent/vide, construit une liste
    à un élément depuis les anciennes clés (proxmox_url/proxmox_token/proxmox_secret)."""
    hosts = []
    raw = creds.get("proxmox_hosts") or []
    if isinstance(raw, list):
        for h in raw:
            if isinstance(h, dict) and h.get("url"):
                hosts.append({
                    "name": (h.get("name") or "proxmox").strip() or "proxmox",
                    "url": h.get("url", ""),
                    "token_id": h.get("token_id", ""),
                    "token_secret": h.get("token_secret", ""),
                })
    if not hosts:
        url = creds.get("proxmox_url", "")
        if url:
            hosts.append({
                "name": "proxmox",
                "url": url,
                "token_id": creds.get("proxmox_token", ""),
                "token_secret": creds.get("proxmox_secret", ""),
            })
    return hosts


# ==============================================================================
# GUEST-AGENT : exécuter des commandes DANS les VMs (via l'API Proxmox)
# ==============================================================================

def guest_exec(creds: dict, vmid: int, command: str, pve_host: str = "", timeout: int = 300) -> tuple:
    """Exécute une commande dans une VM via le qemu-guest-agent.
    Retourne (exit_code, sortie). Lève une exception claire si l'agent est absent."""
    import time as _time
    creds = creds or {}
    hosts = get_proxmox_hosts(creds)
    if pve_host:
        hosts = [h for h in hosts if h["name"] == pve_host]
    if not hosts:
        raise ValueError("Aucun hôte Proxmox configuré.")

    h = hosts[0]
    url, token, secret = h["url"], h["token_id"], h["token_secret"]
    # Localise la VM (nœud)
    snapshot = get_cluster_snapshot(url, token, secret)
    node = vm_type = None
    for v in snapshot["vms"]:
        if str(v.get("vmid")) == str(vmid):
            node, vm_type = v.get("node"), v.get("type")
            break
    if not node:
        raise ValueError(f"VM {vmid} introuvable.")
    if vm_type == "lxc":
        raise ValueError(f"VM {vmid} est un conteneur LXC : le guest-agent ne s'applique qu'aux VMs qemu.")

    base = f"/nodes/{node}/qemu/{vmid}/agent"
    # Lance la commande
    resp = _api_request(url, token, secret, method="POST",
                        data={"path": base + "/exec", "body": {"command": "/bin/bash", "arg": ["-c", command]}})
    pid = resp.get("pid")
    if not pid:
        raise ValueError(f"Le guest-agent de la VM {vmid} n'a pas répondu (agent absent ou non installé ?).")
    # Attend la fin (poll du statut)
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        _time.sleep(2)
        try:
            st = _api_request(url, token, secret, data={"path": base + f"/exec-status?pid={pid}"})
        except Exception:
            continue
        if st.get("exited"):
            out = st.get("out", "") or ""
            err = st.get("err", "") or ""
            return int(st.get("exitcode", -1)), (out + ("\n" + err if err else "")).strip()
    raise TimeoutError(f"La commande dans la VM {vmid} n'a pas terminé en {timeout}s.")


def snapshot_vm(creds: dict, vmid: int, snapname: str, pve_host: str = "") -> str:
    """Crée un snapshot de la VM (avec état RAM) avant une opération risquée."""
    hosts = get_proxmox_hosts(creds)
    if pve_host:
        hosts = [h for h in hosts if h["name"] == pve_host]
    h = hosts[0]
    snapshot = get_cluster_snapshot(h["url"], h["token_id"], h["token_secret"])
    node = None
    for v in snapshot["vms"]:
        if str(v.get("vmid")) == str(vmid):
            node = v.get("node")
            break
    if not node:
        raise ValueError(f"VM {vmid} introuvable.")
    _api_request(h["url"], h["token_id"], h["token_secret"], method="POST",
                 data={"path": f"/nodes/{node}/qemu/{vmid}/snapshot/{snapname}", "body": {"vmstate": 1, "description": "Snapshot automatique avant mise à jour"}})
    return snapname


def rollback_vm(creds: dict, vmid: int, snapname: str, pve_host: str = "") -> str:
    """Restaure la VM à son snapshot (rollback après échec de mise à jour)."""
    hosts = get_proxmox_hosts(creds)
    if pve_host:
        hosts = [h for h in hosts if h["name"] == pve_host]
    h = hosts[0]
    snapshot = get_cluster_snapshot(h["url"], h["token_id"], h["token_secret"])
    node = None
    for v in snapshot["vms"]:
        if str(v.get("vmid")) == str(vmid):
            node = v.get("node")
            break
    if not node:
        raise ValueError(f"VM {vmid} introuvable.")
    _api_request(h["url"], h["token_id"], h["token_secret"], method="POST",
                 data={"path": f"/nodes/{node}/qemu/{vmid}/snapshot/{snapname}/rollback", "body": {}})
    return f"Rollback de la VM {vmid} vers {snapname} lancé."


def is_debian_vm(creds: dict, vmid: int, pve_host: str = "") -> bool:
    """True si la VM est un Linux Debian/Ubuntu avec guest-agent fonctionnel."""
    try:
        code, out = guest_exec(creds, vmid, "cat /etc/os-release", pve_host=pve_host, timeout=30)
        return code == 0 and ("debian" in out.lower() or "ubuntu" in out.lower())
    except Exception:
        return False


def update_debian_vm(creds: dict, vmid: int, pve_host: str = "") -> str:
    """Met à jour une VM Debian/Ubuntu (apt update + upgrade non interactif).
    Retourne un résumé de la sortie apt."""
    cmd = ("export DEBIAN_FRONTEND=noninteractive; "
           "apt-get update -qq && apt-get upgrade -y -qq -o Dpkg::Options::='--force-confdef' -o Dpkg::Options::='--force-confold' "
           "&& echo UPDATE_OK || echo UPDATE_FAIL")
    code, out = guest_exec(creds, vmid, cmd, pve_host=pve_host, timeout=1800)
    tail = "\n".join(out.strip().split("\n")[-8:])
    if "UPDATE_OK" in out:
        return f"✅ VM {vmid} mise à jour.\n{tail}"
    return f"❌ VM {vmid} : échec apt (exit {code}).\n{tail}"


def update_all_vms(creds: dict, batch_size: int = 3, blacklist: list = None,
                   progress_fn=None) -> dict:
    """Orchestrateur de mise à jour de toutes les VMs Debian/Ubuntu :
    snapshot -> batch de N -> vérification -> batch suivant ; rollback si échec.
    progress_fn(message) est appelé pour suivre l'avancement (optionnel).
    Retourne un résumé {updated: [], failed: [], rolled_back: [], skipped: []}."""
    import time as _time
    blacklist = [str(b) for b in (blacklist or [])]
    result = {"updated": [], "failed": [], "rolled_back": [], "skipped": []}
    hosts = get_proxmox_hosts(creds)
    if not hosts:
        raise ValueError("Aucun hôte Proxmox configuré.")

    # Collecte les VMs qemu running sur tous les hôtes
    candidates = []
    for h in hosts:
        try:
            snap = get_cluster_snapshot(h["url"], h["token_id"], h["token_secret"])
        except Exception as e:
            if progress_fn:
                progress_fn(f"⚠️ Hôte {h['name']} injoignable : {e}")
            continue
        for v in snap["vms"]:
            if v.get("type") == "qemu" and v.get("status") == "running" and str(v.get("vmid")) not in blacklist:
                candidates.append({"vmid": v["vmid"], "host": h["name"]})
    if progress_fn:
        progress_fn(f"🔍 {len(candidates)} VM(s) candidate(s) à la mise à jour (qemu running, hors blacklist).")

    # Traite par batches
    for i in range(0, len(candidates), max(1, batch_size)):
        batch = candidates[i:i + max(1, batch_size)]
        for vm in batch:
            vmid, host = vm["vmid"], vm["host"]
            tag = f"[{host}] VM {vmid}"
            # Debian/Ubuntu avec guest-agent ?
            if not is_debian_vm(creds, vmid, pve_host=host):
                result["skipped"].append(tag + " (pas Debian ou guest-agent absent)")
                if progress_fn:
                    progress_fn(f"⏭️ {tag} : ignorée (pas Debian/Ubuntu ou guest-agent absent)")
                continue
            # Snapshot de sécurité
            snapname = "pre-update-" + _time.strftime("%Y%m%d-%H%M")
            try:
                snapshot_vm(creds, vmid, snapname, pve_host=host)
                if progress_fn:
                    progress_fn(f"📸 {tag} : snapshot {snapname} créé")
            except Exception as e:
                result["failed"].append(tag + f" (snapshot impossible : {e})")
                if progress_fn:
                    progress_fn(f"❌ {tag} : snapshot impossible ({e}) — VM non touchée")
                continue
            # Mise à jour
            try:
                res = update_debian_vm(creds, vmid, pve_host=host)
            except Exception as e:
                res = f"❌ VM {vmid} : {e}"
            if res.startswith("✅"):
                # Vérification post-update : la VM répond toujours ?
                try:
                    code, _ = guest_exec(creds, vmid, "uptime", pve_host=host, timeout=60)
                    if code == 0:
                        result["updated"].append(tag)
                        if progress_fn:
                            progress_fn(f"✅ {tag} : mise à jour réussie et VM saine")
                        continue
                    raise ValueError(f"vérification uptime exit {code}")
                except Exception as e:
                    # Échec de vérification -> rollback
                    try:
                        rollback_vm(creds, vmid, snapname, pve_host=host)
                        result["rolled_back"].append(tag + f" (vérification KO : {e})")
                        if progress_fn:
                            progress_fn(f"↩️ {tag} : vérification échouée ({e}) → ROLLBACK vers {snapname}")
                    except Exception as re_err:
                        result["failed"].append(tag + f" (rollback impossible : {re_err})")
                        if progress_fn:
                            progress_fn(f"🚨 {tag} : rollback impossible ({re_err})")
            else:
                # apt a échoué -> rollback
                try:
                    rollback_vm(creds, vmid, snapname, pve_host=host)
                    result["rolled_back"].append(tag + " (échec apt → rollback)")
                    if progress_fn:
                        progress_fn(f"↩️ {tag} : échec de mise à jour → ROLLBACK vers {snapname}\n{res[:200]}")
                except Exception as re_err:
                    result["failed"].append(tag + f" (échec apt, rollback impossible : {re_err})")
                    if progress_fn:
                        progress_fn(f"🚨 {tag} : échec apt et rollback impossible ({re_err})")
    return result


def check_and_report(creds: dict, notify_ntfy: bool = True, log_fn=None) -> tuple:
    """
    Vérifie le cluster Proxmox, produit le rapport, le sauvegarde,
    et envoie une notification ntfy si des problèmes sont détectés.
    Retourne (rapport, issues).
    """
    enabled = creds.get("proxmox_enabled", False)
    hosts = get_proxmox_hosts(creds)
    if not (enabled and hosts):
        return "Surveillance Proxmox non configurée (URL/token/secret). Active-la dans les Réglages.", []

    all_issues = []
    report_parts = []
    for host in hosts:
        hname = host["name"]
        try:
            snapshot = get_cluster_snapshot(host["url"], host["token_id"], host["token_secret"])
        except Exception as e:
            err_msg = f"❌ [{hname}] Erreur d'accès à l'API Proxmox : {e}\nVérifie l'URL, le token (user@realm!tokenid) et le secret."
            report_parts.append(err_msg)
            save_report("\n\n".join(report_parts), [])
            if notify_ntfy and creds.get("ntfy_enabled") and creds.get("ntfy_topic"):
                send_ntfy(creds.get("ntfy_url"), creds.get("ntfy_topic"),
                          f"Proxmox {hname} : erreur d'accès", err_msg[:500], priority="urgent", tags="rotating_light")
            continue
        issues = analyze_snapshot(snapshot)
        # Préfixe le nom d'hôte sur chaque problème pour lever toute ambiguïté
        for i in issues:
            i["element"] = f"[{hname}] {i['element']}"
        all_issues.extend(issues)
        report_parts.append(f"=== HÔTE {hname} ===\n" + format_report(snapshot, issues))

    report = "\n\n".join(report_parts)
    save_report(report, all_issues)

    if notify_ntfy and all_issues and creds.get("ntfy_enabled") and creds.get("ntfy_topic"):
        crit = [i for i in all_issues if i["severity"] == "CRITIQUE"]
        title = f"Proxmox : {len(all_issues)} problème(s)" + (" (CRITIQUE)" if crit else "")
        send_ntfy(creds.get("ntfy_url"), creds.get("ntfy_topic"), title, report[:3500],
                  priority="urgent" if crit else "high", tags="rotating_light" if crit else "warning")
    return report, all_issues


# ==============================================================================
# ACTIONS DE CONTRÔLE (utilisées par le cerveau, avec validation humaine)
# ==============================================================================

def vm_action(creds: dict, vmid: int, action: str, node: str = None, pve_host: str = "") -> str:
    """Action sur une VM/CT : start / stop / reboot / shutdown. Actions en liste blanche uniquement.
    pve_host : nom de l'hôte Proxmox cible (multi-hôtes) ; vide = cherche sur tous les hôtes."""
    action = (action or "").strip().lower()
    if action not in ("start", "stop", "reboot", "shutdown"):
        return f"Action '{action}' non autorisée. Autorisées : start, stop, reboot, shutdown."

    hosts = get_proxmox_hosts(creds)
    if not hosts:
        return "Proxmox non configuré (URL/token/secret manquants)."
    # Filtre sur l'hôte demandé s'il est précisé
    if pve_host:
        hosts = [h for h in hosts if h["name"] == pve_host]
        if not hosts:
            return f"Hôte Proxmox '{pve_host}' inconnu. Hôtes configurés : {', '.join(h['name'] for h in get_proxmox_hosts(creds))}"

    # Trouve le nœud ET le type (qemu/lxc) de la VM via le snapshot du/des hôtes
    vm_type = None
    url = token = secret = None
    if not node:
        last_err = None
        for h in hosts:
            try:
                snapshot = get_cluster_snapshot(h["url"], h["token_id"], h["token_secret"])
            except Exception as e:
                last_err = f"[{h['name']}] {e}"
                continue
            for v in snapshot["vms"]:
                if str(v.get("vmid")) == str(vmid):
                    node = v.get("node")
                    vm_type = v.get("type")
                    url, token, secret = h["url"], h["token_id"], h["token_secret"]
                    break
            if node:
                break
        if not node:
            return f"VM {vmid} introuvable" + (f" (dernière erreur : {last_err})" if last_err else " sur le(s) hôte(s) configuré(s).")
    else:
        if not hosts:
            return "Aucun hôte Proxmox configuré."
        url, token, secret = hosts[0]["url"], hosts[0]["token_id"], hosts[0]["token_secret"]

    # Type réel de la cible : lxc (conteneur) ou qemu (machine virtuelle)
    endpoint_type = "/lxc/" if vm_type == "lxc" else "/qemu/"
    endpoint = "/nodes/" + node + endpoint_type + f"{vmid}/status/{action}"
    try:
        _api_request(url, token, secret, method="POST", data={"path": endpoint, "body": {}})
        return f"Action '{action}' envoyée à la VM {vmid} (nœud {node}). Vérifie l'état réel avec proxmox_status après quelques secondes."
    except Exception as e:
        return f"Erreur lors de l'action '{action}' sur VM {vmid} : {e}"
