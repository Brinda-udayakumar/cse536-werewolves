# Brinda's Werewolves (socket edition)

A 4-player text-based Werewolves variant: 2 Werewolves, 1 Witch, 1 Seer.
The Seer is the strategic twist on the classic game: each night they
privately investigate one living player and learn whether that player
is a werewolf, but that information is never broadcast, so they have
to decide how much to reveal during the day without outing themselves
to the wolves.

## Files

- `werecomm.py` - the communications library (single file, as required).
  Built on **UNIX domain stream sockets** (`socket.AF_UNIX`,
  `SOCK_STREAM`), not named pipes. Each connection gets its own
  background reader thread feeding a queue, so the rest of the code
  can do simple blocking `recv()` calls. Supports unicast (`send`),
  broadcast/multicast (`broadcast`, with optional `exclude`), and a
  `recv_any` that blocks on several players' inboxes at once (used for
  wolf votes, where the server needs the first vote from any wolf
  without starving the others).
- `server.py` - the moderator. Assigns roles, runs the night/day loop,
  resolves wolf kills, witch save/poison, seer visions, day voting,
  and win conditions.
- `client.py` - the player client. Connects, prints broadcasts and
  prompts, lets the player type free text (sent as chat) or
  `vote <target>` to cast a vote.

## Communications library API (`werecomm.py`)

```python
# Moderator side
from werecomm import Server
server = Server(socket_path)
server.start()
names = server.wait_for_players(n, timeout=None)
server.send(player_name, msg_type, payload)          # unicast
server.broadcast(msg_type, payload, exclude=())       # multicast/broadcast
msg_type, payload = server.recv(player_name, timeout=None)
name, msg_type, payload = server.recv_any(player_names, timeout=None)
server.close()

# Player side
from werecomm import Client
client = Client(socket_path, name)
client.connect()
client.send(msg_type, payload)
msg_type, payload = client.recv(timeout=None)
client.close()
```

Messages on the wire are length-prefixed JSON:
`{"type": str, "payload": <json>, "sender": str}`.

## How to run

Each player is a separate Linux user and a separate process, as
required.

**Moderator terminal** (as the `moderator` user):
```bash
cd brinda_werewolves
python3 server.py
```

**Each player terminal** (as `player0`, `player1`, `player2`, `player3`):
```bash
cd brinda_werewolves
python3 client.py            # auto-detects your username via getpass
```

During your turn, type a plain message to chat (broadcast to
everyone), or `vote <target>` to cast a vote/action when prompted
(e.g. `vote player2`, or `vote save:player2` / `vote poison:player1`
for the witch).

## Communications primitive - how it works

`werecomm.py` uses UNIX domain stream sockets instead of named pipes.
The moderator binds one socket at a filesystem path
(`/tmp/werewolves_game.sock`) and `listen()`s on it; each player
`connect()`s to that same path. Unlike named pipes, a stream socket is
a genuine bidirectional, full-duplex byte stream between exactly two
endpoints, so the moderator and each player can both read and write
over the same connection without needing a pair of one-way pipes per
player (the original Werewolves used `NtoS`/`StoN` pipe pairs per
player; here it's one socket per player, period).

Framing is handled manually: every message is prefixed with a 4-byte
big-endian length so the receiver always knows exactly how many bytes
of JSON to read next, even though TCP/UNIX stream sockets give no
built-in message boundaries. Each connection runs a dedicated
background thread whose only job is to pull complete frames off the
socket and push them into a `queue.Queue`; this is what makes the
concurrency element of the assignment real - the moderator is
concurrently and independently listening for input from all 4 players
at once (e.g. during wolf-chat, `recv_any` polls both wolves' queues
simultaneously so either one can vote first), while each player's
listener thread is simultaneously able to receive broadcasts from the
moderator even while the player is still deciding what to type.

## What I learned about this primitive

The game needed both unicast (telling the Seer their vision, telling
the Witch what happened privately) and broadcast (day chat, round
announcements) - sockets handle both fine since `Server` just keeps a
dict of named connections and either picks one (`send`) or loops over
all of them (`broadcast`). The harder part was message framing:
sockets are a raw byte stream with no concept of "one message," so
without the 4-byte length header, concurrent writes/reads could
interleave two JSON payloads into one unparseable blob. Named pipes
have this same framing problem (each `write()` isn't guaranteed to
arrive as one `read()`), but it's easy to overlook with pipes because
small writes usually happen to come through atomically in practice -
sockets made me handle it explicitly from the start instead of
discovering it as a race condition later. The other difference from
named pipes: a socket connection drops cleanly and detectably (a
`recv()` of zero bytes), whereas a FIFO pipe just blocks forever once
the writer is gone unless you're careful, which made disconnect
handling (`__disconnect__` in the inbox) much simpler to get right.
