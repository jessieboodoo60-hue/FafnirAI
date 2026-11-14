"""Entry point and text interface. The text mode is kept forever as the debug path."""
from __future__ import annotations

import sys

from .agent import Agent
from .common import build_agent, cost_line, describe_tool_call, is_yes
from .config import ConfigError, load_config
from .llm import LLMError
from pathlib import Path

from .memory import MemoryStore

HELP = "Commands: /reset (new conversation), /memory (what I remember), /tools, /quit, /help"


def show_memory(memory: MemoryStore) -> None:
    facts = memory.all()
    print(f"Memory file: {memory.path}")
    if not facts:
        print("  (nothing remembered yet)\n")
        return
    for f in facts:
        print(f"  ({f.id}) [{f.category}] {f.text}")
    print()


def _print(text: str) -> None:
    print(text, end="", flush=True)


def confirm_in_terminal(action: str) -> bool:
    print(f"\n  [confirm] Fafnir wants to: {action}")
    try:
        answer = input("  Allow? (yes/no) ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    ok = is_yes(answer)
    print("  -> approved\n" if ok else "  -> declined\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if "--learn-key" in argv:
        from .voice.ptt import learn_key
        return learn_key()
    if "--kill" in argv:
        from . import killswitch
        killswitch.engage("command line")
        print("Kill switch ON. Release with: python main.py --release")
        return
    if "--release" in argv:
        from . import killswitch
        print("Kill switch released." if killswitch.release("command line") else "The kill switch wasn't on.")
        return
    if any(a in argv for a in ("--vault-list", "--vault-add", "--vault-remove", "--vault-take-out")):
        from .config import load_config
        from .tools.private_vault import PrivateVault
        from .tools.registry import ToolError
        folder = load_config().tools.private_folder
        if not folder:
            print("No private folder is set (private_folder under [tools] in config.toml).")
            return 1
        v = PrivateVault(folder)

        def arg(flag, n=1):
            i = argv.index(flag) + n
            return argv[i] if i < len(argv) and not argv[i].startswith("--") else None
        try:
            if "--vault-list" in argv:
                files = v.list_files()["files"]
                print(f"{len(files)} document(s) in the vault:")
                for f in files:
                    print(f"  {f['name']}  ({f['kb']} KB)")
            elif "--vault-add" in argv:
                if not arg("--vault-add"):
                    print("Usage: python main.py --vault-add <file or folder> [--keep-original] [--replace]")
                    return 1
                r = v.add(arg("--vault-add"), keep_original="--keep-original" in argv, replace="--replace" in argv)
                print(f"Added: {', '.join(r['added'])}. "
                      + (f"{r['originals_deleted']} unencrypted original(s) deleted. " if r["originals_deleted"] else "Originals kept. ")
                      + f"The vault now holds {r['now_in_vault']}.")
            elif "--vault-take-out" in argv:
                if not arg("--vault-take-out") or not arg("--vault-take-out", 2):
                    print("Usage: python main.py --vault-take-out <name> <folder to save it in>")
                    return 1
                r = v.remove(arg("--vault-take-out"), keep_copy_in=arg("--vault-take-out", 2))
                print(f"Took out {r['removed']}; an UNENCRYPTED copy is at {r['unencrypted_copy_at']}. "
                      f"{r['left_in_vault']} left in the vault.")
            else:
                if not arg("--vault-remove"):
                    print("Usage: python main.py --vault-remove <name>")
                    return 1
                if input(f"Permanently delete '{arg('--vault-remove')}' from the vault? Type yes: ").strip().lower() != "yes":
                    print("Nothing was removed.")
                    return 0
                r = v.remove(arg("--vault-remove"))
                print(f"Deleted {r['removed']} from the vault. {r['left_in_vault']} left.")
        except ToolError as e:
            print(str(e))
            return 1
        return 0
    if any(a in argv for a in ("--app-user-add", "--app-user-list", "--app-user-passwd", "--app-user-remove")):
        import getpass
        from .config import ROOT
        from .server.auth import AuthStore
        store = AuthStore(ROOT / "data" / "app_users.db")

        def arg(flag):
            i = argv.index(flag) + 1
            return argv[i] if i < len(argv) and not argv[i].startswith("--") else None
        try:
            if "--app-user-list" in argv:
                for u in store.users():
                    print(f"  {u['username']}  ({u['role']}, {'authenticator code required' if u['mfa'] else 'password only'}, since {u['created']})")
                return 0
            name = arg(next(a for a in ("--app-user-add", "--app-user-passwd", "--app-user-remove") if a in argv))
            if not name:
                print("Usage: python main.py --app-user-add NAME [--viewer] | --app-user-passwd NAME | --app-user-remove NAME | --app-user-list")
                return 1
            if "--app-user-remove" in argv:
                store.remove_user(name)
                print(f"Removed {name} and signed out all of their devices.")
                return 0
            pw = getpass.getpass("New password (at least 10 characters, hidden): ")
            if pw != getpass.getpass("Same password again: "):
                print("The two passwords didn't match. Nothing was changed.")
                return 1
            if "--app-user-passwd" in argv:
                store.set_password(name, pw)
                print(f"Password changed for {name}; every device was signed out.")
                return 0
            secret = store.add_user(name, pw, role="viewer" if "--viewer" in argv else "owner")
            print(f"Account {name} created.")
            if secret:
                print("\nOwner accounts also need a code from an authenticator app (Google Authenticator, Microsoft")
                print("Authenticator, Authy...). In the app choose 'enter a setup key' and type:")
                print(f"\n    Account: FafnirAI ({name})\n    Key:     {' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))}\n    Type:    time-based\n")
                print("Keep this key private; it's only shown now.")
        except ValueError as e:
            print(str(e))
            return 1
        return 0
    if "--health" in argv:
        from .health import main as health_main
        return health_main()
    if "--security-check" in argv:
        from .security_check import main as check_main
        return check_main()
    if "--verify-log" in argv:
        from . import audit
        v = audit.verify()
        print("Action log: " + (f"intact, {v['entries']} hashed entries" if v["ok"] else
                                f"TAMPERED or damaged: first problem at line {v['first_problem_line']} "
                                f"({v['problem_lines']} lines)") +
              (f" ({v['older_unhashed_entries']} older entries from before hashing)" if v.get("older_unhashed_entries") else ""))
        return 0 if v["ok"] else 2
    if "--secrets-import" in argv:
        from . import secretstore
        i = argv.index("--secrets-import")
        src = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        r = secretstore.import_env(src)
        print(f"Moved {len(r['imported'])} secrets into the encrypted vault: {', '.join(r['imported'])}")
        print("Each one read back correctly." if r["verified"] else "WARNING: some did not read back; keep your .env.")
        print("Now delete the .env file (it still holds them in plain text).")
        return 0 if r["verified"] else 1
    if "--secret-set" in argv:
        import getpass
        from . import secretstore
        i = argv.index("--secret-set")
        if i + 1 >= len(argv):
            print("Usage: python main.py --secret-set NAME   (you'll be asked for the value; it isn't shown)")
            return 1
        secretstore.set_value(argv[i + 1], getpass.getpass(f"Value for {argv[i + 1]} (hidden; empty removes it): "))
        print(f"{argv[i + 1]} saved in the encrypted vault. Restart Fafnir to use it.")
        return 0
    if "--secrets-list" in argv:
        from . import secretstore
        print("Secrets in the vault (names only):", ", ".join(secretstore.names()) or "(none)")
        return 0
    if "--conversations-export" in argv:
        from .tools import CONVERSATIONS
        i = argv.index("--conversations-export")
        if i + 1 >= len(argv):
            print("Usage: python main.py --conversations-export <folder>")
            return 1
        print(f"Wrote {CONVERSATIONS.export(argv[i + 1])} day files to {argv[i + 1]} (plain text: delete them when you're done).")
        return 0
    if "--rotate-key" in argv:
        from . import vault
        from .config import ROOT, load_config
        data = ROOT / "data"
        files = [data / "secrets.vault", data / "memory.vault", data / "google_token.json", data / "google_credentials.json"]
        files += sorted((data / "conversations").glob("*.jsonl"))
        pf = load_config().tools.private_folder
        if pf:
            files.append(Path(pf) / "personal-information.vault")
        files = [f for f in files if f.exists() and (vault.is_encrypted(f) or f.suffix == ".jsonl")]
        r = vault.rotate(files)
        print(f"New key in place; {r['files_re_encrypted']} files re-encrypted. A new RECOVERY_KEY.txt was written: "
              "save that key somewhere safe and delete the file. Older backups still need the OLD recovery key.")
        return 0
    if "--memory-export" in argv or "--memory-import" in argv:
        from .config import ROOT
        flag = "--memory-export" if "--memory-export" in argv else "--memory-import"
        i = argv.index(flag)
        if i + 1 >= len(argv):
            print(f"Usage: python main.py {flag} <file.md>")
            return 1
        store = MemoryStore(ROOT / "data" / "memory.md")
        if flag == "--memory-export":
            n = store.export_markdown(argv[i + 1])
            print(f"Wrote {n} facts to {argv[i + 1]} (plain text: delete it when you're done).")
        else:
            r = store.import_markdown(argv[i + 1])
            print(f"Memory now holds {r['facts']} facts" + (f" ({r['skipped_as_secrets']} skipped: looked like secrets)." if r["skipped_as_secrets"] else "."))
        return 0
    if "--restore-backup" in argv:
        from .backup import restore_backup
        i = argv.index("--restore-backup")
        opt = lambda flag: argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None
        if i + 1 >= len(argv):
            print("Usage: python main.py --restore-backup <file> [--to <folder>] [--key <recovery key>]")
            return 1
        r = restore_backup(argv[i + 1], to=opt("--to"), recovery_key=opt("--key"))
        print(f"Restored {r['files']} files to {r['restored_to']}")
        return 0
    if "--backup" in argv:
        from .backup import main as backup_main
        return backup_main()
    if "--train-wake" in argv:
        from .config import ROOT
        from .voice.wake import record_samples
        return record_samples(ROOT / "data" / "wake")
    if "--install-startup" in argv or "--remove-startup" in argv:
        from .ui.install import install, remove
        return install() if "--install-startup" in argv else remove()
    if "--app" in argv:
        from .ui.app import run_app
        return run_app(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Setup problem: {e}")
        return 1
    agent = build_agent(cfg)

    if "--voice" in argv:
        missing = cfg.voice.missing_keys()
        if missing:
            print(f"Setup problem: voice mode needs {', '.join(missing)} in .env.")
            return 1
        from .voice.app import ConsoleVoice
        try:
            app = ConsoleVoice(cfg, agent)
        except ValueError as e:  # bad ptt_key
            print(f"Setup problem: {e}")
            return 1
        except Exception as e:  # audio device problems
            print(f"Setup problem: couldn't open audio: {e}")
            return 1
        return app.run()

    return text_loop(cfg, agent)


def text_loop(cfg, agent: Agent) -> int:
    name = cfg.assistant.name
    print(f"{name} is ready ({cfg.model.name}). {HELP}\n")

    while True:
        try:
            user = input("you> ").strip().lstrip("﻿")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        if user == "/reset":
            agent.reset()
            print("(new conversation; memory reloaded)\n")
            continue
        if user == "/help":
            print(HELP + "\n")
            continue
        if user == "/memory":
            show_memory(agent.memory)
            continue
        if user == "/tools":
            extra = [t["name"] for t in agent.registry.server_tools]
            print("Tools: " + ", ".join(agent.registry.names() + extra) + "\n")
            continue

        print(f"{name.lower()}> ", end="", flush=True)

        def on_tool(tname, args, result, is_error):
            _print(describe_tool_call(tname, args, result, is_error) + f"{name.lower()}> ")

        from .tools import CONVERSATIONS
        CONVERSATIONS.append("user", user, "typed")
        said: list[str] = []

        def on_text(t: str) -> None:
            said.append(t)
            _print(t)

        before = agent.llm.usage.copy()
        try:
            agent.run_turn(user, on_text=on_text, on_tool=on_tool, confirm=confirm_in_terminal)
            CONVERSATIONS.append("assistant", "".join(said))
            print("\n")
            if cfg.model.show_cost:
                print(cost_line(agent.llm.usage, before) + "\n")
        except LLMError as e:
            print(f"\n[{e}]\n")
        except KeyboardInterrupt:
            print("\n[interrupted]\n")

    print(f"{name} signing off.")
    return 0
