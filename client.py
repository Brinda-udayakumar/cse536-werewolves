#!/usr/bin/env python3
"""
client.py - Player client for Brinda's Werewolves variant.

Run as player0, player1, player2, or player3 (auto-detected from the
Linux username via getpass.getuser()). Connects to the moderator's
UNIX socket, prints broadcasts and prompts, and lets the player type
chat messages or votes.
"""

import sys
import threading
import getpass

from werecomm import Client

SOCKET_PATH = "/tmp/werewolves_game.sock"


def listener(client, stop_event):
    while not stop_event.is_set():
        msg_type, payload = client.recv(timeout=0.5)
        if msg_type is None:
            continue
        if msg_type == "role":
            print(f"\n~~~~~ YOU ARE A {payload['role'].upper()} ~~~~~\n")
        elif msg_type == "info":
            print(f"\n[MODERATOR] {payload}")
        elif msg_type == "chat":
            print(f"\n{payload['from']}: {payload['text']}")
        elif msg_type == "prompt":
            print(f"\n[PROMPT] {payload['text']}")
            if payload.get("options"):
                print(f"          options: {payload['options']}")
        elif msg_type == "__disconnect__":
            print("\n[connection closed by moderator]")
            stop_event.set()
            return


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else getpass.getuser()
    client = Client(SOCKET_PATH, name)
    print(f"Connecting as {name}...")
    client.connect()
    print(f"Connected as {name}. Waiting for the game to start.")

    stop_event = threading.Event()
    t = threading.Thread(target=listener, args=(client, stop_event), daemon=True)
    t.start()

    try:
        while not stop_event.is_set():
            line = input()
            if not line:
                continue
            if line.startswith("vote "):
                client.send("vote", line[len("vote "):].strip())
            else:
                client.send("chat", line)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
