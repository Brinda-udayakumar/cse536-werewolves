#!/usr/bin/env python3
"""
server.py - Moderator for Brinda's Werewolves variant.

Run this as the `moderator` user. Players connect with client.py as
player0..player3. Roles this round: 2 Werewolves, 1 Witch, 1 Seer.

The Seer is this game's strategic twist on classic Werewolves: each
night the Seer privately investigates one living player and is told
whether that player is a werewolf. That intel is never broadcast, so
the Seer has to decide how much to reveal during the day without
outing themself to the wolves.
"""

import random
import time
import sys

from werecomm import Server

SOCKET_PATH = "/tmp/werewolves_game.sock"
PLAYERS = ["player0", "player1", "player2", "player3"]
NUM_WOLVES = 2

import os as _os
WOLF_TALK_TIME = int(_os.environ.get('WW_WOLF_TALK_TIME', 45))
SEER_TIME = int(_os.environ.get('WW_SEER_TIME', 20))
WITCH_TIME = int(_os.environ.get('WW_WITCH_TIME', 20))
DAY_TALK_TIME = int(_os.environ.get('WW_DAY_TALK_TIME', 90))
DAY_VOTE_TIME = int(_os.environ.get('WW_DAY_VOTE_TIME', 30))
CONNECT_TIMEOUT = int(_os.environ.get('WW_CONNECT_TIMEOUT', 120))


def assign_roles(players):
    shuffled = players[:]
    random.shuffle(shuffled)
    roles = {}
    for name in shuffled[:NUM_WOLVES]:
        roles[name] = "wolf"
    roles[shuffled[NUM_WOLVES]] = "witch"
    roles[shuffled[NUM_WOLVES + 1]] = "seer"
    return roles


def alive_players(status):
    return [p for p, alive in status.items() if alive]


def wolves_alive(roles, status):
    return [p for p in alive_players(status) if roles[p] == "wolf"]


def collect_votes(server, voters, valid_targets, duration, prompt_text):
    """Broadcast a prompt to `voters`, collect one vote each within
    `duration` seconds. Returns {voter: target}."""
    for v in voters:
        server.send(v, "prompt", {"text": prompt_text, "options": valid_targets})

    votes = {}
    deadline = time.time() + duration
    remaining = voters[:]
    while remaining and time.time() < deadline:
        name, msg_type, payload = server.recv_any(remaining, timeout=max(0.1, deadline - time.time()))
        if name is None:
            break
        if msg_type == "vote" and payload in valid_targets:
            votes[name] = payload
            remaining.remove(name)
        elif msg_type == "__disconnect__":
            remaining.remove(name)
    return votes


def tally(votes):
    """Return (winner_or_None, counts_dict). None if no majority / tie."""
    if not votes:
        return None, {}
    counts = {}
    for target in votes.values():
        counts[target] = counts.get(target, 0) + 1
    best = max(counts.values())
    leaders = [t for t, c in counts.items() if c == best]
    if len(leaders) == 1 and best > len(votes) / 2:
        return leaders[0], counts
    return None, counts


def main():
    print(f"Moderator starting. Waiting for {len(PLAYERS)} players to connect...")
    server = Server(SOCKET_PATH)
    server.start()

    connected = server.wait_for_players(len(PLAYERS), timeout=CONNECT_TIMEOUT)
    if len(connected) < len(PLAYERS):
        print(f"Only {len(connected)}/{len(PLAYERS)} players connected. Exiting.")
        server.close()
        sys.exit(1)

    print(f"All players connected: {connected}")

    roles = assign_roles(PLAYERS)
    status = {p: True for p in PLAYERS}
    witch_heal_used = False
    witch_kill_used = False

    for p in PLAYERS:
        server.send(p, "role", {"role": roles[p]})
        print(f"{p} -> {roles[p]}")

    server.broadcast("info", f"Game starting. {NUM_WOLVES} wolves, 1 witch, 1 seer among {len(PLAYERS)} players.")

    round_num = 0
    while True:
        round_num += 1
        server.broadcast("info", f"===== ROUND {round_num} - Night falls. Everyone close your eyes. =====")
        time.sleep(1)

        wolves = wolves_alive(roles, status)
        living = alive_players(status)

        # --- Wolves pick a victim -------------------------------------
        victim = None
        if wolves:
            server.broadcast("info", "Wolves are choosing a victim...", exclude=wolves)
            votes = collect_votes(
                server, wolves, living, WOLF_TALK_TIME,
                f"Werewolves, discuss and vote on a victim. Living players: {living}"
            )
            victim, _ = tally(votes)

        # --- Seer investigates ------------------------------------------
        seer = next((p for p in living if roles[p] == "seer"), None)
        if seer:
            targets = [p for p in living if p != seer]
            server.send(seer, "prompt", {"text": "Seer, choose a player to investigate.", "options": targets})
            name, msg_type, payload = server.recv_any([seer], timeout=SEER_TIME)
            if msg_type == "vote" and payload in targets:
                is_wolf = roles[payload] == "wolf"
                server.send(seer, "info", f"Vision: {payload} is {'a WEREWOLF' if is_wolf else 'NOT a werewolf'}.")

        # --- Witch may save or poison ------------------------------------
        witch = next((p for p in living if roles[p] == "witch"), None)
        if witch:
            options = []
            if not witch_heal_used and victim:
                options.append(f"save:{victim}")
            if not witch_kill_used:
                options += [f"poison:{p}" for p in living if p != witch]
            options.append("pass")
            if victim:
                server.send(witch, "info", f"The wolves targeted {victim} tonight.")
            else:
                server.send(witch, "info", "The wolves did not agree on a victim tonight.")
            server.send(witch, "prompt", {"text": "Witch, act or pass.", "options": options})
            name, msg_type, payload = server.recv_any([witch], timeout=WITCH_TIME)
            if msg_type == "vote" and payload and payload != "pass" and ":" in payload:
                action, target = payload.split(":", 1)
                if action == "save" and target == victim and not witch_heal_used:
                    victim = None
                    witch_heal_used = True
                elif action == "poison" and not witch_kill_used:
                    status[target] = False
                    witch_kill_used = True
                    server.broadcast("info", f"{target} was poisoned in the night.")

        if victim and status.get(victim):
            status[victim] = False
            server.broadcast("info", f"Night ends. {victim} was killed by the wolves.")
        else:
            server.broadcast("info", "Night ends. Nobody was killed.")

        # --- Win check after night ---------------------------------------
        living = alive_players(status)
        wolves = wolves_alive(roles, status)
        if not wolves:
            server.broadcast("info", "All wolves are dead. Town wins!")
            break
        if len(wolves) > len(living) - len(wolves):
            server.broadcast("info", "Wolves equal or outnumber the town. Wolves win!")
            break

        # --- Day phase: discussion -----------------------------------------
        server.broadcast("info", f"It is day. Living players: {living}. {DAY_TALK_TIME}s to discuss.")
        deadline = time.time() + DAY_TALK_TIME
        while time.time() < deadline:
            name, msg_type, payload = server.recv_any(living, timeout=max(0.1, deadline - time.time()))
            if name and msg_type == "chat":
                server.broadcast("chat", {"from": name, "text": payload}, exclude=())

        # --- Day vote ------------------------------------------------------
        votes = collect_votes(
            server, living, living, DAY_VOTE_TIME,
            f"Townsfolk, vote on who to hang. Living players: {living}"
        )
        hanged, counts = tally(votes)
        server.broadcast("info", {"votes": counts})
        if hanged:
            status[hanged] = False
            server.broadcast("info", f"{hanged} was hanged by the town. They were a {roles[hanged]}.")
        else:
            server.broadcast("info", "No majority. Nobody was hanged today.")

        living = alive_players(status)
        wolves = wolves_alive(roles, status)
        if not wolves:
            server.broadcast("info", "All wolves are dead. Town wins!")
            break
        if len(wolves) > len(living) - len(wolves):
            server.broadcast("info", "Wolves equal or outnumber the town. Wolves win!")
            break

    server.broadcast("info", "Game over. Thanks for playing.")
    time.sleep(1)
    server.close()


if __name__ == "__main__":
    main()
