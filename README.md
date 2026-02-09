# twitch-plays-norns

A norns mod that enables "Twitch Plays" functionality - receive commands from Twitch chat and control norns encoders (e1, e2, e3) and keys (k1, k2, k3).

## Installation

### On norns

#### Via Maiden Console

```
;install https://github.com/Magicking/twitch-plays-norns/archive/refs/tags/v1.0.0.zip
```

#### Via ssh

Copy the mod to your norns dust folder:

```bash
scp -r lib/ we@norns.local:dust/code/twitch-plays-norns/
```

Then enable the mod in SYSTEM > MODS > twitch-plays-norns

### Bridge (runs on your computer)

The bridge connects to Twitch IRC and forwards commands to norns via OSC.

```bash
# Get your OAuth token from [https://twitchtokengenerator.com/](https://twitchtokengenerator.com/)
python bridge/twitch_bridge.py --channel YOUR_CHANNEL --token YOUR_TOKEN --norns-ip norns.local
```

Or use environment variables:
```bash
export TWITCH_CHANNEL=yourchannel
export TWITCH_TOKEN=oauth:abc123
export NORNS_IP=norns.local
python bridge/twitch_bridge.py
```

## Command Format

Chat commands use the `!` prefix:

| Command | Description |
|---------|-------------|
| `!k1` | Press key 1 (short press, 100ms default) |
| `!k2` | Press key 2 (short press) |
| `!k3` | Press key 3 (short press) |
| `!k1:500` | Hold key 1 for 500ms |
| `!e1:5` | Turn encoder 1 clockwise by 5 |
| `!e2:-3` | Turn encoder 2 counter-clockwise by 3 |
| `!e1` | Turn encoder 1 by 1 (shorthand for e1:1) |
| `!w:500` | Wait/delay for 500ms |
| `!d:200` | Delay for 200ms (alias for w:) |

### Sequences

Commands can be chained with spaces:

```
!k1 e1:5 k2           # press k1, turn e1, press k2
!e1:10 e2:-5 k3       # turn e1, turn e2, press k3
!k1 w:1000 k2         # press k1, wait 1 second, press k2
!k2 d:500 e1:3 d:500 k3   # k2 -> wait -> turn e1 -> wait -> k3
```

### Simultaneous Input (Combos)

Use `+` to combine inputs that happen at the same time:

| Command | Description |
|---------|-------------|
| `!k1+e2:5` | Hold k1 while turning e2 by 5 |
| `!k1+k2:300` | Press k1 and k2 together for 300ms |
| `!k2+e1:10+e3:-5` | Hold k2 while turning e1 and e3 |

The duration applies to how long keys are held. Encoder turns are spread across that duration.

```
!k1+e2:8            # hold k1 while turning e2 (8 steps over 100ms)
!k1:500+e1:20       # hold k1 for 500ms while turning e1 (20 steps)
!k2+k3:200          # press k2 and k3 together for 200ms
```

## OSC API

The mod listens for OSC messages on port 10111:

| Path | Args | Description |
|------|------|-------------|
| `/twitch/cmd` | string | Execute command string (e.g., "k1 e2:5") |
| `/twitch/key` | int, int | Raw key event (n=1-3, z=0/1) |
| `/twitch/enc` | int, int | Raw encoder event (n=1-3, delta) |
| `/twitch/enable` | int | Enable (1) or disable (0) the mod |
| `/twitch/config` | string, int | Set config: "press_duration", "command_delay" |

### Testing with oscsend

```bash
# Install oscsend (part of liblo-tools)
oscsend norns.local 10111 /twitch/cmd s "k1 e1:5"
oscsend norns.local 10111 /twitch/key ii 1 1   # k1 press
oscsend norns.local 10111 /twitch/key ii 1 0   # k1 release
oscsend norns.local 10111 /twitch/enc ii 2 5   # e2 +5
```

## Configuration

Default values can be changed via OSC:

```bash
# Set default key press duration to 200ms
oscsend norns.local 10111 /twitch/config si press_duration 200

# Set delay between sequence commands to 100ms
oscsend norns.local 10111 /twitch/config si command_delay 100
```

## Bridge Options

```
--channel, -c     Twitch channel name
--token, -t       OAuth token from https://twitchapps.com/tmi/
--norns-ip        norns IP (default: norns.local)
--norns-port      norns OSC port (default: 10111)
--prefix          Command prefix (default: !)
--rate-limit      Min seconds between commands (default: 0.1)
```

## Debugging

Check norns maiden console for logs:
```
[twitch-plays] initialized
[twitch-plays] received: k1 e2:5
[twitch-plays] key 1 press (duration: 100ms)
[twitch-plays] enc 2 delta: 5
```

Access the mod from a norns script:
```lua
-- Check if enabled
print(twitch_plays.enabled)

-- Disable temporarily
twitch_plays.set_enabled(false)

-- Execute a command directly
twitch_plays.execute("k1 e1:5 k2")
```
