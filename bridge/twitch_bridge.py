#!/usr/bin/env python3
"""
Twitch IRC Bridge for twitch-plays-norns
Connects to Twitch chat and forwards commands to norns via OSC

Usage:
    python twitch_bridge.py --channel CHANNEL_NAME --token OAUTH_TOKEN [--norns-ip IP]

Environment variables (alternative to CLI args):
    TWITCH_CHANNEL    - Twitch channel name
    TWITCH_TOKEN      - OAuth token (get from https://twitchapps.com/tmi/)
    NORNS_IP          - norns IP address (default: norns)
    NORNS_PORT        - norns OSC port (default: 10111)

Command prefix: ! (e.g., !k1 e2:5 k3)
"""

import argparse
import os
import re
import socket
import time
from typing import Optional

# Simple OSC implementation (no dependencies)
def osc_string(s: str) -> bytes:
    """Encode a string as OSC string (null-terminated, padded to 4 bytes)"""
    b = s.encode('utf-8') + b'\x00'
    padding = (4 - len(b) % 4) % 4
    return b + b'\x00' * padding

def osc_message(path: str, *args) -> bytes:
    """Create an OSC message with the given path and arguments"""
    msg = osc_string(path)

    # Type tag string
    type_tag = ','
    for arg in args:
        if isinstance(arg, int):
            type_tag += 'i'
        elif isinstance(arg, float):
            type_tag += 'f'
        elif isinstance(arg, str):
            type_tag += 's'
    msg += osc_string(type_tag)

    # Arguments
    for arg in args:
        if isinstance(arg, int):
            msg += arg.to_bytes(4, 'big', signed=True)
        elif isinstance(arg, float):
            import struct
            msg += struct.pack('>f', arg)
        elif isinstance(arg, str):
            msg += osc_string(arg)

    return msg

class OSCSender:
    """Simple UDP-based OSC sender"""
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, path: str, *args):
        """Send an OSC message"""
        msg = osc_message(path, *args)
        try:
            self.sock.sendto(msg, (self.host, self.port))
        except Exception as e:
            print(f"[OSC] Error sending to {self.host}:{self.port}: {e}")

    def close(self):
        self.sock.close()

class TwitchIRC:
    """Simple Twitch IRC client"""

    IRC_HOST = "irc.chat.twitch.tv"
    IRC_PORT = 6667

    def __init__(self, channel: str, token: str, nickname: Optional[str] = None):
        self.channel = channel.lower().lstrip('#')
        self.token = token
        self.nickname = nickname or f"justinfan{int(time.time()) % 100000}"
        self.sock: Optional[socket.socket] = None
        self.buffer = ""

    def connect(self):
        """Connect to Twitch IRC"""
        print(f"[IRC] Connecting to {self.IRC_HOST}:{self.IRC_PORT}...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.IRC_HOST, self.IRC_PORT))
        self.sock.settimeout(300)  # 5 minute timeout for PING/PONG

        # Authenticate
        if self.token and not self.token.startswith("oauth:"):
            self.token = f"oauth:{self.token}"

        if self.token:
            self._send(f"PASS {self.token}")
        self._send(f"NICK {self.nickname}")
        self._send(f"JOIN #{self.channel}")

        print(f"[IRC] Joined #{self.channel}")

    def _send(self, msg: str):
        """Send a raw IRC message"""
        if self.sock:
            self.sock.send(f"{msg}\r\n".encode('utf-8'))

    def _recv(self) -> str:
        """Receive data from IRC"""
        if not self.sock:
            return ""
        try:
            data = self.sock.recv(4096).decode('utf-8', errors='ignore')
            return data
        except socket.timeout:
            return ""
        except Exception as e:
            print(f"[IRC] Receive error: {e}")
            return ""

    def read_messages(self):
        """Generator that yields (username, message) tuples"""
        while True:
            data = self._recv()
            if not data:
                # Check for disconnect
                continue

            self.buffer += data

            while '\r\n' in self.buffer:
                line, self.buffer = self.buffer.split('\r\n', 1)

                # Handle PING
                if line.startswith('PING'):
                    pong = line.replace('PING', 'PONG')
                    self._send(pong)
                    continue

                # Parse PRIVMSG
                # Format: :username!user@user.tmi.twitch.tv PRIVMSG #channel :message
                match = re.match(r':(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.+)', line)
                if match:
                    username = match.group(1)
                    message = match.group(2).strip()
                    yield (username, message)

    def close(self):
        if self.sock:
            self._send("QUIT")
            self.sock.close()
            self.sock = None

class TwitchBridge:
    """Bridge between Twitch chat and norns OSC"""

    # Command pattern: starts with ! followed by valid norns commands
    COMMAND_PREFIX = "!"
    # Matches: k1, k2:500, e1:5, e2:-3, w:500, d:100, k1+e2:5, k1+k2:300
    VALID_COMMANDS = re.compile(
        r'^([ke][1-3](:-?\d+)?|[wd]:\d+|wait:\d+|delay:\d+|'
        r'([ke][1-3](:-?\d+)?\+)+[ke][1-3](:-?\d+)?)$',
        re.IGNORECASE
    )

    def __init__(self, channel: str, token: str, norns_ip: str = "norns.local",
                 norns_port: int = 10111, command_prefix: str = "!"):
        self.irc = TwitchIRC(channel, token)
        self.osc = OSCSender(norns_ip, norns_port)
        self.command_prefix = command_prefix
        self.running = False

        # Rate limiting
        self.last_command_time = 0
        self.min_command_interval = 0.1  # seconds

        # Command filtering
        self.allowed_users: Optional[set] = None  # None = all users allowed
        self.blocked_users: set = set()

    def parse_chat_command(self, message: str) -> Optional[str]:
        """Parse a chat message and return the norns command string if valid"""
        if not message.startswith(self.command_prefix):
            return None

        # Remove prefix
        cmd_str = message[len(self.command_prefix):].strip()
        if not cmd_str:
            return None

        # Validate each token
        tokens = cmd_str.split()
        valid_tokens = []

        for token in tokens:
            token = token.lower()
            if self.VALID_COMMANDS.match(token):
                valid_tokens.append(token)
            else:
                # Skip invalid tokens but continue
                print(f"[Parse] Skipping invalid token: {token}")

        if valid_tokens:
            return ' '.join(valid_tokens)
        return None

    def handle_message(self, username: str, message: str):
        """Handle a chat message"""
        # Check user permissions
        if self.blocked_users and username.lower() in self.blocked_users:
            return
        if self.allowed_users and username.lower() not in self.allowed_users:
            return

        # Parse command
        cmd = self.parse_chat_command(message)
        if not cmd:
            return

        # Rate limiting
        now = time.time()
        if now - self.last_command_time < self.min_command_interval:
            print(f"[Rate] Skipping command from {username}: rate limited")
            return
        self.last_command_time = now

        # Send to norns
        print(f"[CMD] {username}: {cmd}")
        self.osc.send("/twitch/cmd", cmd)

    def run(self):
        """Main loop"""
        print("[Bridge] Starting Twitch-norns bridge...")
        self.running = True

        try:
            self.irc.connect()
            print(f"[Bridge] Listening for commands (prefix: {self.command_prefix})")
            print("[Bridge] Valid commands: k1 k2 k3 k1:500 e1:5 e2:-3 etc.")
            print("[Bridge] Press Ctrl+C to stop")

            for username, message in self.irc.read_messages():
                if not self.running:
                    break
                self.handle_message(username, message)

        except KeyboardInterrupt:
            print("\n[Bridge] Stopping...")
        except Exception as e:
            print(f"[Bridge] Error: {e}")
        finally:
            self.running = False
            self.irc.close()
            self.osc.close()
            print("[Bridge] Stopped")

def main():
    parser = argparse.ArgumentParser(
        description="Twitch IRC bridge for twitch-plays-norns",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Basic usage
    python twitch_bridge.py --channel mychannel --token abc123

    # With custom norns IP
    python twitch_bridge.py --channel mychannel --token abc123 --norns-ip 192.168.1.100

    # Using environment variables
    TWITCH_CHANNEL=mychannel TWITCH_TOKEN=abc123 python twitch_bridge.py

Command format in chat:
    !k1           - Press key 1
    !k2:500       - Hold key 2 for 500ms
    !e1:5         - Turn encoder 1 clockwise by 5
    !e2:-3        - Turn encoder 2 counter-clockwise by 3
    !k1 e1:5 k2   - Sequence of commands
        """
    )

    parser.add_argument(
        '--channel', '-c',
        default=os.environ.get('TWITCH_CHANNEL'),
        help='Twitch channel name (or set TWITCH_CHANNEL env var)'
    )
    parser.add_argument(
        '--token', '-t',
        default=os.environ.get('TWITCH_TOKEN'),
        help='OAuth token (or set TWITCH_TOKEN env var). Get from https://twitchapps.com/tmi/'
    )
    parser.add_argument(
        '--norns-ip',
        default=os.environ.get('NORNS_IP', 'norns'),
        help='norns IP address (default: norns)'
    )
    parser.add_argument(
        '--norns-port',
        type=int,
        default=int(os.environ.get('NORNS_PORT', '10111')),
        help='norns OSC port (default: 10111)'
    )
    parser.add_argument(
        '--prefix',
        default='!',
        help='Command prefix (default: !)'
    )
    parser.add_argument(
        '--rate-limit',
        type=float,
        default=0.1,
        help='Minimum seconds between commands (default: 0.1)'
    )

    args = parser.parse_args()

    if not args.channel:
        parser.error("Channel name required. Use --channel or set TWITCH_CHANNEL")

    # Token is optional - can use anonymous connection for read-only
    token = args.token or ""
    if not token:
        print("[Warning] No OAuth token provided. Using anonymous connection.")

    bridge = TwitchBridge(
        channel=args.channel,
        token=token,
        norns_ip=args.norns_ip,
        norns_port=args.norns_port,
        command_prefix=args.prefix
    )
    bridge.min_command_interval = args.rate_limit
    bridge.run()

if __name__ == '__main__':
    main()
