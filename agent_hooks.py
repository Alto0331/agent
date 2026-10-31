# -*- coding: utf-8 -*-
"""
agent_hooks.py — Point d'ancrage entre les moteurs d'exécution (Terra/Lite/Ollama)
et le serveur (agent_server.py), sans import circulaire.

agent_server.py remplit ces hooks au démarrage ; les moteurs les appellent
avec fallback local si le serveur ne les a pas définis.
"""

import os
import shutil
import subprocess

# Hooks remplis par agent_server.py
fetch_mailbox = None


def run_shell_command(command: str, timeout: int = 30) -> str:
    """Exécute une commande shell Windows et retourne stdout+stderr."""
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=max(3, min(timeout, 120))
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        parts = [f"Code de retour : {r.returncode}"]
        if out:
            parts.append(f"Sortie :\n{out[:3000]}")
        if err:
            parts.append(f"Erreurs :\n{err[:1500]}")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"Commande interrompue : dépassement du délai de {timeout}s."
    except Exception as e:
        return f"Erreur d'exécution : {e}"


def file_operation(action: str, path: str, destination: str = "", content: str = "") -> str:
    """Opérations fichiers directes sans souris : list, read, write, mkdir, delete, move, copy."""
    p = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    a = (action or "").lower().strip()

    if a in ["list", "ls", "dir"]:
        if not os.path.isdir(p):
            return f"Dossier introuvable : {p}"
        try:
            items = os.listdir(p)
            if not items:
                return f"Dossier vide : {p}"
            listing = "\n".join(
                ("[DIR]  " if os.path.isdir(os.path.join(p, i)) else "[FILE] ") + i for i in items[:200]
            )
            return f"Contenu de {p} :\n{listing}"
        except Exception as e:
            return f"Erreur de listage : {e}"

    if a == "read":
        if not os.path.isfile(p):
            return f"Fichier introuvable : {p}"
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:6000]
        except Exception as e:
            return f"Erreur de lecture : {e}"

    if a in ["write", "create"]:
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Fichier écrit : {p} ({len(content)} caractères)."
        except Exception as e:
            return f"Erreur d'écriture : {e}"

    if a in ["mkdir", "makedirs"]:
        try:
            os.makedirs(p, exist_ok=True)
            return f"Dossier créé : {p}"
        except Exception as e:
            return f"Erreur de création : {e}"

    if a in ["delete", "del", "rm"]:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
                return f"Dossier supprimé : {p}"
            if os.path.isfile(p):
                os.remove(p)
                return f"Fichier supprimé : {p}"
            return f"Introuvable : {p}"
        except Exception as e:
            return f"Erreur de suppression : {e}"

    if a in ["move", "mv", "rename"]:
        d = os.path.expandvars(os.path.expanduser(destination.strip().strip('"')))
        try:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.move(p, d)
            return f"Déplacé : {p} -> {d}"
        except Exception as e:
            return f"Erreur de déplacement : {e}"

    if a in ["copy", "cp"]:
        d = os.path.expandvars(os.path.expanduser(destination.strip().strip('"')))
        try:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            if os.path.isdir(p):
                shutil.copytree(p, d, dirs_exist_ok=True)
            else:
                shutil.copy2(p, d)
            return f"Copié : {p} -> {d}"
        except Exception as e:
            return f"Erreur de copie : {e}"

    return f"Action inconnue : {a} (attendu : list, read, write, mkdir, delete, move, copy)"


def extract_attachment_text(path: str) -> str:
    """Extrait le texte d'une pièce jointe (PDF via PyMuPDF, docx via python-docx, txt/csv direct)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import fitz
            with fitz.open(path) as doc:
                return "\n".join(page.get_text() for page in doc)[:8000]
        if ext in [".docx", ".doc"]:
            try:
                from docx import Document
                doc = Document(path)
                return "\n".join(p.text for p in doc.paragraphs)[:8000]
            except ImportError:
                return "(docx : installer python-docx sur la VM pour lire le contenu, ou ouvrir le fichier dans Word)"
        if ext in [".txt", ".csv", ".log", ".json", ".md", ".py", ".html", ".xml"]:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:8000]
        if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]:
            return "(image — ouvre-la avec open_app_or_url pour la consulter visuellement)"
        return f"(type {ext} non lisible en texte — ouvre le fichier avec open_app_or_url)"
    except Exception as e:
        return f"(erreur de lecture : {e})"


def read_agent_mailbox(max_mails: int = 10, unread_only: bool = False) -> str:
    """Lit la boîte mail de l'agent via le hook serveur fetch_mailbox (IMAP, sans navigateur).
    Les pièces jointes sont téléchargées dans attachments/ ; leur contenu texte est extrait (PDF/docx/txt/csv)."""
    if fetch_mailbox is None:
        return "Lecture de boîte mail indisponible sur ce serveur."
    try:
        mails = fetch_mailbox(max_mails=max_mails, unread_only=unread_only)
        if not mails:
            return "Aucun mail trouvé."
        blocks = []
        for m in mails:
            block = f"De : {m['from']}\nDate : {m['date']}\nObjet : {m['subject']}\n---\n{m['body'][:1500]}"
            atts = m.get("attachments") or []
            if atts:
                lines = []
                for a in atts:
                    if a.get("blocked"):
                        lines.append(f"- {a['filename']} [BLOQUÉ : exécutable non sauvegardé]")
                    elif a.get("path"):
                        extracted = extract_attachment_text(a["path"])
                        lines.append(f"- {a['filename']} sauvegardé : {a['path']}\n  Contenu extrait :\n{extracted}")
                block += "\n\nPIÈCES JOINTES :\n" + "\n".join(lines)
            blocks.append(block)
        return "\n\n==========\n\n".join(blocks)
    except Exception as e:
        return f"Erreur de lecture de la boîte mail : {e}"
