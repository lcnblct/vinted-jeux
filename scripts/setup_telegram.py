#!/usr/bin/env python3
"""
Setup Telegram en 2min — récupère chat_id automatiquement
Usage: python scripts/setup_telegram.py
Env: TELEGRAM_BOT_TOKEN (ou saisi interactif)
"""
import os, sys, time, requests

def get_token():
    tok = os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not tok:
        print("1) Ouvre Telegram → @BotFather → /newbot → nom 'vinted-paris-bot'")
        print("   Copie le token (ex: 1234567890:ABCdef...)")
        tok = input("\nColle TELEGRAM_BOT_TOKEN: ").strip()
    return tok

def main():
    token = get_token()
    if not token or ":" not in token:
        print("Token invalide."); sys.exit(1)
    os.environ["TELEGRAM_BOT_TOKEN"] = token

    print("\n2) Ouvre Telegram → cherche ton bot → envoie n'importe quel message (ex: 'hello')")
    input("   Appuie Entrée quand c'est fait...")

    for i in range(5):
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
            j = r.json()
            if not j.get("ok"):
                print("Erreur API:", j); sys.exit(1)
            results = j.get("result", [])
            if not results:
                print(f"[{i+1}/5] Aucun message reçu, renvoie un message au bot...")
                time.sleep(2)
                continue
            # prend le dernier chat
            last = results[-1]
            chat = last.get("message", {}).get("chat") or last.get("my_chat_member", {}).get("chat") or {}
            chat_id = chat.get("id")
            name = chat.get("first_name") or chat.get("title") or ""
            print(f"\n✅ Trouvé chat_id: {chat_id} ({name})")
            print(f"\nAjoute dans .env:")
            print(f"TELEGRAM_BOT_TOKEN={token}")
            print(f"TELEGRAM_CHAT_ID={chat_id}")
            print(f"\nEt dans GitHub → Settings → Secrets → Actions:")
            print(f"  TELEGRAM_BOT_TOKEN")
            print(f"  TELEGRAM_CHAT_ID")
            # test envoi
            if input("\nEnvoyer message test ? (o/N): ").lower().strip() in ("o","y","oui"):
                text = "🎲 Test Vinted Jeux — Telegram OK ! Tu recevras les annonces sous -10€ en français."
                r2 = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data={"chat_id": chat_id, "text": text})
                print("Test:", r2.json())
            # sauvegarde .env
            env_path = ".env"
            if os.path.exists(env_path):
                content = open(env_path).read()
                if "TELEGRAM_BOT_TOKEN" not in content:
                    open(env_path,"a").write(f"\nTELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
                    print(f"\n→ Ajouté à {env_path}")
            else:
                open(env_path,"w").write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
                print(f"\n→ Créé {env_path}")
            sys.exit(0)
        except Exception as e:
            print("Erreur:", e)
            time.sleep(2)
    print("\n❌ Aucun chat trouvé. Vérifie que tu as bien envoyé un message au bot.")
    print("   Essaie: curl https://api.telegram.org/bot<TOKEN>/getUpdates")

if __name__ == "__main__":
    main()
