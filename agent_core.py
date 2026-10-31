# -*- coding: utf-8 -*-
"""
agent_core.py — Moteur d'agent autonome MODE LITE (Claude Haiku 5.5)
Idéal pour le tri de dossiers, la configuration Cisco Packet Tracer et les tâches rapides à coût minimal.
Compatible à 100% avec agent_server.py et app_client.py.
"""

import os
import sys
import time
import json
import base64
import hashlib
import ctypes
import subprocess
from io import BytesIO

import mss
from PIL import Image, ImageDraw
import pyautogui
import pygetwindow as gw
import pyperclip
from openai import OpenAI

# PyMuPDF (fitz)
try:
    import fitz
except ImportError:
    fitz = None

# Configuration PyAutoGUI
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

# Fichier de stockage local de la clé API
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key.txt")

# --- CONFIGURATION DU MODÈLE LITE (CLAUDE HAIKU 5.5 SUR OPENROUTER) ---
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_LITE = "anthropic/claude-haiku-5.5"

VISION_TARGET_WIDTH = 1280
VISION_TARGET_HEIGHT = 720
JPEG_QUALITY = 78


# ==============================================================================
# 1. GESTION DE LA CLÉ API
# ==============================================================================
def load_api_key() -> str:
    if os.path.exists(API_KEY_FILE):
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def save_api_key(api_key: str) -> bool:
    try:
        clean_key = api_key.strip()
        with open(API_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(clean_key)
        os.environ["OPENROUTER_API_KEY"] = clean_key
        return True
    except Exception as e:
        print(f"[Erreur] Impossible d'enregistrer la clé API : {e}")
        return False


def get_openrouter_client() -> OpenAI:
    api_key = load_api_key()
    if not api_key:
        raise ValueError("Clé API OpenRouter manquante.")
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        timeout=35.0,
        max_retries=1,
        default_headers={
            "HTTP-Referer": "https://github.com/Alto0331/agent",
            "X-Title": "VM Autonomous Agent Lite (Claude Haiku 5.5)",
        },
    )


# ==============================================================================
# 2. FRAPPE UNICODE WIN32 (IMMUNISÉE AZERTY/QWERTY POUR CISCO)
# ==============================================================================
PUL = ctypes.POINTER(ctypes.c_ulong)


class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class Input_I(ctypes.Union):
    _fields_ = [
        ("ki", KeyBdInput),
        ("mi", MouseInput),
        ("hi", HardwareInput),
    ]


class Input(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("ii", Input_I),
    ]


KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


def send_unicode_char(char: str):
    code = ord(char)
    extra = ctypes.c_ulong(0)

    ii_down = Input_I()
    ii_down.ki = KeyBdInput(0, code, KEYEVENTF_UNICODE, 0, ctypes.pointer(extra))
    x_down = Input(ctypes.c_ulong(INPUT_KEYBOARD), ii_down)

    ii_up = Input_I()
    ii_up.ki = KeyBdInput(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
    x_up = Input(ctypes.c_ulong(INPUT_KEYBOARD), ii_up)

    ctypes.windll.user32.SendInput(1, ctypes.pointer(x_down), ctypes.sizeof(x_down))
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x_up), ctypes.sizeof(x_up))


def type_unicode_string(text: str, interval: float = 0.005, app_instance=None):
    for ch in text:
        if app_instance and getattr(app_instance, "stop_requested", False):
            break
        if ch == "\r":
            continue
        elif ch == "\n":
            pyautogui.press("enter")
            time.sleep(0.05)
        else:
            send_unicode_char(ch)
            if interval > 0:
                time.sleep(interval)


# ==============================================================================
# 3. CAPTURE & COORDONNÉES
# ==============================================================================
def get_screen_size():
    return pyautogui.size()


def capture_screen(draw_cursor: bool = True):
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

    real_w, real_h = img.size

    if draw_cursor:
        try:
            mx, my = pyautogui.position()
            draw = ImageDraw.Draw(img)
            r = 10
            draw.ellipse((mx - r, my - r, mx + r, my + r), outline="#ef4444", width=3)
            draw.line((mx - r - 4, my, mx + r + 4, my), fill="#ef4444", width=2)
            draw.line((mx, my - r - 4, mx, my + r + 4), fill="#ef4444", width=2)
        except Exception:
            pass

    small_hash_img = img.resize((64, 36), Image.Resampling.BILINEAR)
    screen_hash = hashlib.md5(small_hash_img.tobytes()).hexdigest()

    if (real_w, real_h) != (VISION_TARGET_WIDTH, VISION_TARGET_HEIGHT):
        img_resized = img.resize((VISION_TARGET_WIDTH, VISION_TARGET_HEIGHT), Image.Resampling.LANCZOS)
    else:
        img_resized = img

    buf = BytesIO()
    img_resized.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64_str, screen_hash, (real_w, real_h)


def resolve_xy(x: float, y: float) -> tuple[int, int]:
    real_w, real_h = get_screen_size()
    scale_x = real_w / float(VISION_TARGET_WIDTH)
    scale_y = real_h / float(VISION_TARGET_HEIGHT)

    final_x = int(round(float(x) * scale_x))
    final_y = int(round(float(y) * scale_y))

    final_x = max(2, min(real_w - 3, final_x))
    final_y = max(2, min(real_h - 3, final_y))
    return final_x, final_y


# ==============================================================================
# 4. PROMPTS ET SCHÉMA D'OUTILS LITE
# ==============================================================================
SYSTEM_PROMPT_LITE = f"""Tu es un agent d'exécution rapide et économique (Mode Lite - Claude Haiku 5.5).
La capture d'écran reçue fait exactement {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT} pixels. Donne tes coordonnées dans ce repère.

🔴 RÈGLES D'ACTION :
- Tu es un automate direct : aucun bavardage, appelle directement les outils nécessaires.
- Si une fenêtre ou une page charge, n'attends pas plus de 3 secondes (`wait` avec `seconds: 3`).
- Dès que l'action demandée est terminée (ex: déplacer un fichier, entrer des commandes Cisco, valider une fenêtre), appelle IMMÉDIATEMENT `finish_task`.
- INTERDICTION DE VÉRIFIER EN BOUCLE : ne refais aucun clic de vérification après avoir terminé.

🔵 SPÉCIFICITÉS :
1. CISCO PACKET TRACER : Clique sur la console CLI pour lui donner le focus, puis utilise EXCLUSIVEMENT `send_cisco_cli_block`.
2. PDF : N'ouvre JAMAIS un PDF dans Docs ou Word. Utilise toujours l'outil `edit_pdf`.
3. TEXTE & MISE EN PAGE : Utilise `type_text` pour injecter des blocs de texte propres sans fautes.
4. PARSEC : Si l'utilisateur demande de se connecter à l'un de ses PC, utilise `parsec_connect(target=...)`. Juste après le double-clic de connexion, appelle OBLIGATOIREMENT `parsec_confirm_connection` UNE SEULE FOIS (clic zone vide + vérification du focus du PC distant). Touches immersives actives : une fois confirmé, pilote le PC distant normalement. Ne te déconnecte pas seul : l'utilisateur le fera avec Ctrl+Alt+² (sauf demande explicite : `parsec_disconnect`).
"""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "click_xy",
            "description": f"Clique aux coordonnées (x, y) sur la grille {VISION_TARGET_WIDTH}x{VISION_TARGET_HEIGHT}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "clicks": {"type": "integer", "default": 1},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click_xy",
            "description": "Double-clique aux coordonnées (x, y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click_xy",
            "description": "Clic droit aux coordonnées (x, y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag_and_drop",
            "description": "Glisser-déposer de (start_x, start_y) jusqu'à (end_x, end_y) (idéal pour ranger des fichiers).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "end_x": {"type": "integer"},
                    "end_y": {"type": "integer"},
                    "duration": {"type": "number", "default": 0.4},
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Écrit un texte complet sans déformation de layout ni bug AZERTY/QWERTY.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "press_enter": {"type": "boolean", "default": False},
                    "clear_first": {"type": "boolean", "default": False},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_cisco_cli_block",
            "description": "Tape un bloc de commandes IOS dans Cisco Packet Tracer CLI sans bug de clavier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {"type": "string"},
                },
                "required": ["commands"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_pdf",
            "description": "Inspecte ou modifie un PDF en conservant la mise en page (champs AcroForm, remplacement, texte, signature).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["inspect", "modify"]},
                    "pdf_path": {"type": "string"},
                    "output_path": {"type": "string"},
                    "form_fields": {"type": "object"},
                    "replacements": {"type": "array", "items": {"type": "object"}},
                    "insert_texts": {"type": "array", "items": {"type": "object"}},
                    "insert_images": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["action", "pdf_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Appuie sur une touche spéciale.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "presses": {"type": "integer", "default": 1},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Exécute un raccourci clavier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Fait défiler la molette.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app_or_url",
            "description": "Lance une application ou ouvre une URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Met au premier plan une fenêtre contenant ce titre.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title_substring": {"type": "string"},
                },
                "required": ["title_substring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_connect",
            "description": "Ouvre Parsec, cherche le PC demandé et lance la connexion distante. Touches immersives actives : une fois connecté, toutes les actions pilotent le PC distant. L'utilisateur se déconnecte avec Ctrl+Alt+².",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Nom du PC distant."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_confirm_connection",
            "description": "OBLIGATOIRE après le double-clic de connexion dans Parsec : clique dans une zone vide pour capter le focus du PC distant et vérifie que l'écran distant réagit. Appelle-le UNE seule fois, jamais en boucle.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parsec_disconnect",
            "description": "Se déconnecte du PC distant Parsec (Ctrl+Alt+²) et rend le contrôle à la VM.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Attente (max 3 secondes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "default": 3.0},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "Demande une indication à l'utilisateur.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Termine la mission immédiatement sans faire de clics superflus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
    },
]


def prepare_task_data(task: str) -> dict:
    t_low = task.lower()
    hints = []
    if any(k in t_low for k in ["cisco", "packet tracer", "vlan", "routeur", "switch", "ospf", "cli"]):
        hints.append("MODE CISCO : Utilise send_cisco_cli_block pour taper les commandes dans l'IOS.")
    if any(k in t_low for k in ["pdf", "formulaire", "contrat", "signer"]):
        hints.append("MODE PDF : N'ouvre pas Docs/Word, utilise l'outil edit_pdf.")
    if any(k in t_low for k in ["tri", "trier", "dossier", "déplacer", "deplacer", "ranger"]):
        hints.append("MODE TRI : Repère l'élément à déplacer et utilise drag_and_drop ou Ctrl+X / Ctrl+V.")
    return {"task": task, "hints": "\n".join(hints) if hints else "Exécution directe."}


# ==============================================================================
# 5. EXÉCUTION DES OUTILS
# ==============================================================================
def _find_brave_exe() -> str | None:
    brave_paths = [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for p in brave_paths:
        if os.path.exists(p):
            return p
    return None


def _find_parsec_exe() -> str | None:
    parsec_paths = [
        os.path.expandvars(r"%ProgramFiles%\Parsec\parsec.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Parsec\parsec.exe"),
        os.path.expandvars(r"%LocalAppData%\Parsec\parsec.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Parsec\parsec.exe"),
    ]
    for p in parsec_paths:
        if os.path.exists(p):
            return p
    return None


def _parsec_connect(target: str, app_instance=None) -> str:
    parsec_exe = _find_parsec_exe()
    if not parsec_exe:
        return "Parsec n'est pas installé sur la VM (parsec.exe introuvable). Demande à l'utilisateur via `ask_human`."

    wins = [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]
    if wins:
        w = wins[0]
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
            time.sleep(0.3)
        except Exception:
            pass
    else:
        subprocess.Popen([parsec_exe])
        time.sleep(2.5)
        wins = [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]
        if wins:
            try:
                wins[0].activate()
            except Exception:
                pass

    if not [w for w in gw.getAllWindows() if w.title and "parsec" in w.title.lower()]:
        return "Impossible d'ouvrir la fenêtre Parsec."

    try:
        time.sleep(1.0)
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)
        type_unicode_string(target, app_instance=app_instance)
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(1.0)
        pyautogui.press("esc")
        time.sleep(0.5)
    except Exception as e:
        return f"Recherche du PC '{target}' dans Parsec échouée : {e}"

    return (f"Recherche du PC '{target}' lancée dans Parsec. Double-clique sur le bon ordinateur dans la liste "
            "pour lancer la connexion, puis appelle `parsec_confirm_connection` pour valider que le contrôle "
            "a bien basculé sur le PC distant avant de continuer la mission.")


def _parsec_confirm_connection(app_instance=None) -> str:
    try:
        sw, sh = get_screen_size()
        center_x, center_y = sw // 2, sh // 2

        for attempt in range(1, 4):
            if app_instance and getattr(app_instance, "stop_requested", False):
                return "Confirmation Parsec interrompue par l'utilisateur."

            before = capture_screen(draw_cursor=False)
            pyautogui.moveTo(center_x, center_y, duration=0.25)
            pyautogui.click(center_x, center_y)
            time.sleep(0.8)
            pyautogui.moveRel(60, 60, duration=0.25)
            time.sleep(0.5)
            after = capture_screen(draw_cursor=False)

            if hashlib.md5(before).hexdigest() != hashlib.md5(after).hexdigest():
                return ("✅ Connexion Parsec confirmée : le contrôle est bien sur le PC distant. "
                        "Continue la mission normalement dedans.")
            time.sleep(1.0)

        return ("⚠️ Impossible de confirmer la bascule vers le PC distant après 3 tentatives (écran identique). "
                "Vérifie que la connexion Parsec est établie ou utilise `ask_human`.")
    except Exception as e:
        return f"Erreur lors de la confirmation Parsec : {e}"


def _parsec_disconnect() -> str:
    try:
        pyautogui.hotkey("ctrl", "alt", "²")
        time.sleep(0.6)
        return "Déconnexion Parsec envoyée (Ctrl+Alt+²)."
    except Exception as e:
        return f"Échec de la déconnexion Parsec : {e}"


def _resolve_pdf_path(raw_path: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(raw_path.strip().strip('"').strip("'")))
    if os.path.exists(expanded):
        return os.path.abspath(expanded)
    filename = os.path.basename(expanded)
    user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    search_dirs = [
        os.path.join(user_profile, "Downloads"),
        os.path.join(user_profile, "Desktop"),
        os.path.join(user_profile, "Documents"),
        os.getcwd(),
    ]
    for d in search_dirs:
        cand = os.path.join(d, filename)
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return expanded


def _handle_edit_pdf(args: dict) -> str:
    if fitz is None:
        return "PyMuPDF (fitz) n'est pas disponible."
    action = str(args.get("action", "inspect")).lower()
    pdf_path = _resolve_pdf_path(args.get("pdf_path", ""))
    if not os.path.exists(pdf_path):
        return f"PDF introuvable : {pdf_path}"

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return f"Erreur ouverture PDF : {e}"

    try:
        if action == "inspect":
            out = [f"📄 PDF : {pdf_path} ({len(doc)} pages)"]
            for idx in range(min(len(doc), 5)):
                page = doc[idx]
                out.append(f"Page {idx} ({int(page.rect.width)}x{int(page.rect.height)}) :")
                for w in (page.widgets() or [])[:25]:
                    out.append(f"  - Champ '{w.field_name}' = '{w.field_value}' [{int(w.rect.x0)}, {int(w.rect.y0)}]")
                for b in (page.get_text("blocks") or [])[:20]:
                    t_clean = " ".join(str(b[4]).splitlines()).strip()
                    if t_clean:
                        out.append(f"  - [{int(b[0])}, {int(b[1])}] : {t_clean[:70]}")
            doc.close()
            return "\n".join(out)

        elif action == "modify":
            for page in doc:
                for w in (page.widgets() or []):
                    if w.field_name in (args.get("form_fields") or {}):
                        w.field_value = str(args["form_fields"][w.field_name])
                        w.update()

            for item in (args.get("replacements") or []):
                s, r = str(item.get("search", "")), str(item.get("replace", ""))
                if not s:
                    continue
                for page in doc:
                    hits = page.search_for(s)
                    if hits:
                        q = []
                        for h in hits:
                            page.add_redact_annot(h, fill=(1, 1, 1))
                            q.append((fitz.Point(h.x0, h.y1 - (h.height * 0.18)), r, max(8.0, round(h.height * 0.78, 1))))
                        page.apply_redactions()
                        for pt, txt, fs in q:
                            page.insert_text(pt, txt, fontsize=fs, fontname="helv", color=(0, 0, 0))

            out_path = _resolve_pdf_path(args.get("output_path") or pdf_path)
            if os.path.abspath(out_path) == os.path.abspath(pdf_path):
                tmp = pdf_path + ".tmp.pdf"
                doc.save(tmp, garbage=3, deflate=True)
                doc.close()
                os.replace(tmp, pdf_path)
            else:
                doc.save(out_path, garbage=3, deflate=True)
                doc.close()
            return f"PDF modifié et sauvegardé : {out_path}"
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        return f"Erreur PDF : {e}"


def execute_action(name: str, args: dict, app_instance=None) -> str:
    if app_instance and getattr(app_instance, "stop_requested", False):
        return "Interrompu par l'utilisateur."

    try:
        if name == "click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            btn = args.get("button", "left")
            clicks = int(args.get("clicks", 1))
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.click(rx, ry, clicks=clicks, interval=0.06, button=btn)
            time.sleep(0.12)
            return f"Clic ({btn} x{clicks}) en ({rx}, {ry})."

        elif name == "double_click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.doubleClick(rx, ry, interval=0.06)
            time.sleep(0.2)
            return f"Double-clic en ({rx}, {ry})."

        elif name == "right_click_xy":
            rx, ry = resolve_xy(args["x"], args["y"])
            pyautogui.moveTo(rx, ry, duration=0.22, tween=pyautogui.easeOutQuad)
            pyautogui.rightClick(rx, ry)
            time.sleep(0.15)
            return f"Clic droit en ({rx}, {ry})."

        elif name == "drag_and_drop":
            sx, sy = resolve_xy(args["start_x"], args["start_y"])
            ex, ey = resolve_xy(args["end_x"], args["end_y"])
            dur = max(0.25, float(args.get("duration", 0.4)))
            pyautogui.moveTo(sx, sy, duration=0.2, tween=pyautogui.easeOutQuad)
            pyautogui.dragTo(ex, ey, duration=dur, button="left", tween=pyautogui.easeInOutQuad)
            time.sleep(0.15)
            return f"Glisser-déposer de ({sx}, {sy}) vers ({ex}, {ey})."

        elif name == "type_text":
            text = str(args.get("text", ""))
            if not text:
                return "Texte vide."
            if args.get("clear_first", False):
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.03)
                pyautogui.press("backspace")
                time.sleep(0.03)

            norm = text.replace("\r\n", "\n").replace("\n", "\r\n")
            pyperclip.copy(norm)
            time.sleep(0.03)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.08)
            if args.get("press_enter", False):
                pyautogui.press("enter")
                time.sleep(0.06)
            return "Texte rédigé avec succès."

        elif name == "edit_pdf":
            return _handle_edit_pdf(args)

        elif name == "send_cisco_cli_block":
            commands_raw = str(args.get("commands", ""))
            lines = [l.strip() for l in commands_raw.splitlines() if l.strip()]
            if not lines:
                return "Aucune commande Cisco."

            old_p = pyautogui.PAUSE
            pyautogui.PAUSE = 0
            try:
                if len(lines) == 1 and lines[0].lower() in ["no", "n"]:
                    type_unicode_string("no", interval=0.01, app_instance=app_instance)
                    pyautogui.press("enter")
                    time.sleep(0.3)
                    pyautogui.press("enter")
                    return "Réponse 'no' envoyée."

                pyautogui.press("enter")
                time.sleep(0.12)
                for l in lines:
                    if app_instance and getattr(app_instance, "stop_requested", False):
                        return "Interrompu."
                    type_unicode_string(l, interval=0.005, app_instance=app_instance)
                    time.sleep(0.02)
                    pyautogui.press("enter")
                    time.sleep(0.15)
            finally:
                pyautogui.PAUSE = old_p
            return f"{len(lines)} commande(s) Cisco tapée(s)."

        elif name == "press_key":
            key = str(args.get("key", "enter")).lower()
            presses = int(args.get("presses", 1))
            pyautogui.press(key, presses=presses, interval=0.04)
            time.sleep(0.1)
            return f"Touche '{key}' ({presses}x)."

        elif name == "hotkey":
            keys = [str(k).lower() for k in args.get("keys", [])]
            if keys:
                pyautogui.hotkey(*keys)
                time.sleep(0.12)
            return f"Raccourci {'+'.join(keys)}."

        elif name == "parsec_connect":
            target = str(args.get("target", "")).strip()
            if not target:
                return "Paramètre 'target' manquant."
            return _parsec_connect(target, app_instance=app_instance)

        elif name == "parsec_disconnect":
            return _parsec_disconnect()

        elif name == "parsec_confirm_connection":
            return _parsec_confirm_connection(app_instance=app_instance)

        elif name == "scroll":
            amt = int(args.get("amount", -500))
            if "x" in args and "y" in args and args["x"] is not None and args["y"] is not None:
                rx, ry = resolve_xy(args["x"], args["y"])
                pyautogui.moveTo(rx, ry, duration=0.18, tween=pyautogui.easeOutQuad)
            pyautogui.scroll(amt)
            time.sleep(0.15)
            return f"Scroll ({amt})."

        elif name == "open_app_or_url":
            t = str(args.get("target", "")).strip()
            brave = _find_brave_exe()
            if t.lower().startswith(("http://", "https://", "www.")):
                u = t if t.lower().startswith("http") else f"https://{t}"
                subprocess.Popen([brave, u]) if brave else os.startfile(u)
                time.sleep(1.0)
                return f"URL ouverte : {u}"
            if t.lower() in ["brave", "brave.exe", "browser"] and brave:
                subprocess.Popen([brave])
                time.sleep(0.9)
                return "Brave lancé."
            try:
                subprocess.Popen(t, shell=True)
            except Exception:
                os.startfile(t)
            time.sleep(0.9)
            return f"Cible lancée : {t}"

        elif name == "focus_window":
            sub = str(args.get("title_substring", "")).lower().strip()
            wins = [w for w in gw.getAllWindows() if w.title and sub in w.title.lower()]
            if not wins:
                return f"Fenêtre '{sub}' introuvable."
            w = wins[0]
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
                time.sleep(0.2)
            except Exception:
                pass
            return f"Fenêtre '{w.title}' au premier plan."

        elif name == "wait":
            secs = min(3.0, max(0.2, float(args.get("seconds", 3.0))))
            time.sleep(secs)
            return f"Attente {secs:.1f}s."

        return f"Outil inconnu : {name}"
    except Exception as e:
        return f"Erreur {name} : {e}"


# ==============================================================================
# 6. BOUCLE PRINCIPALE LITE (CLAUDE HAIKU 5.5)
# ==============================================================================
def run_agent(task: str, app_instance=None, max_steps: int = 35):
    def log_msg(txt: str, tag: str = "system"):
        if app_instance and hasattr(app_instance, "log"):
            app_instance.log(txt, tag)
        else:
            print(txt, end="")

    try:
        client = get_openrouter_client()
    except Exception as e:
        log_msg(f"❌ {e}\n", "error")
        return

    prep_info = "Exécution directe."
    if app_instance and hasattr(app_instance, "shared_state"):
        s = app_instance.shared_state
        if s.get("done") and s.get("data"):
            prep_info = s["data"].get("hints", prep_info)
        else:
            prep_info = prepare_task_data(task).get("hints", prep_info)

    log_msg(f"⚡ Modèle actif : {MODEL_LITE} (Mode Lite économique)\n", "system")

    static_prefix = [
        {"role": "system", "content": SYSTEM_PROMPT_LITE},
        {
            "role": "user",
            "content": f"🎯 MISSION : {task}\n📌 CONSIGNES : {prep_info}\nExécute sans discours et appelle `finish_task` dès que l'action est faite.",
        },
    ]

    history_logs: list[str] = []
    unchanged_screen_count = 0
    last_screen_hash = None

    for step in range(1, max_steps + 1):
        if app_instance and getattr(app_instance, "stop_requested", False):
            log_msg("🛑 Mission interrompue.\n", "error")
            return

        b64_img, current_hash, _ = capture_screen(draw_cursor=True)
        if last_screen_hash and current_hash == last_screen_hash:
            unchanged_screen_count += 1
        else:
            unchanged_screen_count = 0
        last_screen_hash = current_hash

        loop_warning = ""
        if unchanged_screen_count >= 3:
            loop_warning = "\n⚠️ Écran inchangé : si la mission est finie, appelle IMMÉDIATEMENT `finish_task` sans recliquer."

        messages = list(static_prefix)
        if history_logs:
            messages.append({"role": "assistant", "content": "Actions faites :\n" + "\n".join(history_logs[-8:])})

        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Étape {step}/{max_steps}. Prochaine action ?{loop_warning}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}", "detail": "low"}},
                ],
            }
        )

        try:
            response = client.chat.completions.create(
                model=MODEL_LITE,
                messages=messages,
                tools=TOOLS_SCHEMA,
                tool_choice="auto",
                temperature=0.0,
                max_tokens=600,
            )
        except Exception as e:
            log_msg(f"❌ Erreur API Claude : {e}\n", "error")
            time.sleep(1.5)
            continue

        if app_instance and getattr(app_instance, "stop_requested", False):
            return

        msg = response.choices[0].message if response and getattr(response, "choices", None) else None
        if msg is None:
            log_msg("⚠️ Réponse API vide (erreur fournisseur). Nouvelle tentative.\n", "error")
            time.sleep(1.5)
            continue
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            txt = (msg.content or "").strip()
            if txt:
                log_msg(f"🤖 {txt}\n", "agent")
                history_logs.append(f"Étape {step}: {txt[:90]}")
            continue

        for tc in tool_calls:
            if app_instance and getattr(app_instance, "stop_requested", False):
                return
            fn = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}

            if fn == "finish_task":
                log_msg(f"✅ Mission accomplie : {args.get('summary', 'Terminé')}\n", "system")
                return

            if fn == "ask_human":
                q = args.get("question", "Précision nécessaire :")
                log_msg(f"🤖 Question : {q}\n", "agent")
                if app_instance and hasattr(app_instance, "wait_for_human"):
                    app_instance.wait_for_human()
                    if getattr(app_instance, "stop_requested", False):
                        return
                    reply = getattr(app_instance, "human_response", "").strip()
                    log_msg("▶️ Reprise.\n", "system")
                    history_logs.append(f"Q: '{q}' -> R: '{reply}'")
                continue

            log_msg(f"⚡ [{step}] {fn}({json.dumps(args, ensure_ascii=False)[:80]})\n", "agent")
            res = execute_action(fn, args, app_instance=app_instance)
            history_logs.append(f"{fn} => {res}")

    log_msg("⚠️ Limite d'étapes atteinte.\n", "error")
