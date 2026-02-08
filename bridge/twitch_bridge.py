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
import logging
import os
import re
import socket
import time
from datetime import datetime
from typing import Optional

# Configure logging with timestamps
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('twitch-bridge')

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
        self.messages_sent = 0
        self.last_send_time: Optional[float] = None
        logger.info(f"OSC sender initialized: {host}:{port}")

    def send(self, path: str, *args):
        """Send an OSC message"""
        msg = osc_message(path, *args)
        try:
            self.sock.sendto(msg, (self.host, self.port))
            self.messages_sent += 1
            self.last_send_time = time.time()
            logger.debug(f"OSC sent: {path} {args}")
        except Exception as e:
            logger.error(f"OSC send failed to {self.host}:{self.port}: {e}")

    def close(self):
        logger.info(f"OSC sender closing (sent {self.messages_sent} messages)")
        self.sock.close()

class TwitchIRC:
    """Simple Twitch IRC client with connection health monitoring"""

    IRC_HOST = "irc.chat.twitch.tv"
    IRC_PORT = 6667

    # Connection health settings
    SOCKET_TIMEOUT = 180  # 3 minutes - Twitch sends PING every ~5 minutes
    MAX_SILENCE_DURATION = 360  # 6 minutes without any data = connection dead
    RECONNECT_DELAY_BASE = 5  # Base delay for reconnection backoff
    RECONNECT_DELAY_MAX = 300  # Max 5 minutes between reconnect attempts

    def __init__(self, channel: str, token: str, nickname: Optional[str] = None):
        self.channel = channel.lower().lstrip('#')
        self.token = token
        self.nickname = nickname or f"justinfan{int(time.time()) % 100000}"
        self.sock: Optional[socket.socket] = None
        self.buffer = ""

        # Connection health tracking
        self.connected = False
        self.connect_time: Optional[float] = None
        self.last_data_received: Optional[float] = None
        self.last_ping_received: Optional[float] = None
        self.last_pong_sent: Optional[float] = None
        self.ping_count = 0
        self.message_count = 0
        self.reconnect_attempts = 0

        logger.info(f"TwitchIRC initialized for channel #{self.channel}")

    def connect(self):
        """Connect to Twitch IRC"""
        logger.info(f"Connecting to {self.IRC_HOST}:{self.IRC_PORT}...")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)  # Connection timeout

        try:
            self.sock.connect((self.IRC_HOST, self.IRC_PORT))
        except socket.error as e:
            logger.error(f"Connection failed: {e}")
            raise

        self.sock.settimeout(self.SOCKET_TIMEOUT)
        self.connected = True
        self.connect_time = time.time()
        self.last_data_received = time.time()
        self.buffer = ""

        logger.info("Socket connected, authenticating...")

        # Authenticate
        if self.token and not self.token.startswith("oauth:"):
            self.token = f"oauth:{self.token}"

        if self.token:
            self._send(f"PASS {self.token}")
            logger.debug("Sent PASS (token)")
        else:
            logger.warning("No token provided, using anonymous connection")

        self._send(f"NICK {self.nickname}")
        logger.debug(f"Sent NICK {self.nickname}")

        self._send(f"JOIN #{self.channel}")
        logger.info(f"Joined #{self.channel}")

        self.reconnect_attempts = 0  # Reset on successful connect

    def _send(self, msg: str):
        """Send a raw IRC message"""
        if self.sock:
            try:
                self.sock.send(f"{msg}\r\n".encode('utf-8'))
            except socket.error as e:
                logger.error(f"Send failed: {e}")
                self.connected = False
                raise

    def _recv(self) -> str:
        """Receive data from IRC"""
        if not self.sock:
            return ""
        try:
            data = self.sock.recv(4096).decode('utf-8', errors='ignore')
            if data:
                self.last_data_received = time.time()
                logger.debug(f"Received {len(data)} bytes")
            return data
        except socket.timeout:
            logger.debug("Socket timeout (no data)")
            return ""
        except socket.error as e:
            logger.error(f"Receive error: {e}")
            self.connected = False
            raise ConnectionError(f"Socket error: {e}")

    def is_connection_healthy(self) -> bool:
        """Check if connection appears healthy"""
        if not self.connected or not self.sock:
            return False

        if self.last_data_received is None:
            return False

        silence_duration = time.time() - self.last_data_received
        if silence_duration > self.MAX_SILENCE_DURATION:
            logger.warning(f"Connection unhealthy: no data for {silence_duration:.0f}s")
            return False

        return True

    def get_status(self) -> dict:
        """Get connection status for logging"""
        now = time.time()
        return {
            "connected": self.connected,
            "uptime_seconds": int(now - self.connect_time) if self.connect_time else 0,
            "last_data_ago": int(now - self.last_data_received) if self.last_data_received else None,
            "last_ping_ago": int(now - self.last_ping_received) if self.last_ping_received else None,
            "ping_count": self.ping_count,
            "message_count": self.message_count,
        }

    def read_messages(self):
        """Generator that yields (username, message) tuples"""
        while self.connected:
            # Check connection health
            if not self.is_connection_healthy():
                logger.error("Connection appears dead, raising error for reconnection")
                raise ConnectionError("Connection timeout - no data received")

            try:
                data = self._recv()
            except ConnectionError:
                raise

            if not data:
                # Log periodic status on timeout
                status = self.get_status()
                logger.debug(f"Status: uptime={status['uptime_seconds']}s, "
                            f"last_data={status['last_data_ago']}s ago, "
                            f"pings={status['ping_count']}, msgs={status['message_count']}")
                continue

            self.buffer += data

            while '\r\n' in self.buffer:
                line, self.buffer = self.buffer.split('\r\n', 1)

                # Handle PING - critical for keeping connection alive
                if line.startswith('PING'):
                    pong = line.replace('PING', 'PONG')
                    self._send(pong)
                    self.ping_count += 1
                    self.last_ping_received = time.time()
                    self.last_pong_sent = time.time()
                    logger.info(f"PING/PONG #{self.ping_count} (connection alive)")
                    continue

                # Log other IRC messages for debugging
                if line.startswith(':tmi.twitch.tv') or line.startswith(':'):
                    # Server messages
                    if 'NOTICE' in line:
                        logger.info(f"Server notice: {line}")
                    elif 'USERSTATE' in line or 'ROOMSTATE' in line:
                        logger.debug(f"Room state: {line}")
                    elif '001' in line or '376' in line:  # Welcome/MOTD end
                        logger.info("Received welcome from Twitch IRC")

                # Parse PRIVMSG
                # Format: :username!user@user.tmi.twitch.tv PRIVMSG #channel :message
                match = re.match(r':(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.+)', line)
                if match:
                    username = match.group(1)
                    message = match.group(2).strip()
                    self.message_count += 1
                    logger.debug(f"Chat message #{self.message_count} from {username}")
                    yield (username, message)

    def close(self):
        if self.sock:
            status = self.get_status()
            logger.info(f"Closing IRC connection (uptime={status['uptime_seconds']}s, "
                       f"pings={status['ping_count']}, msgs={status['message_count']})")
            try:
                self._send("QUIT")
            except Exception:
                pass  # Connection might already be dead
            self.sock.close()
            self.sock = None
        self.connected = False

    def reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        self.close()
        self.reconnect_attempts += 1

        delay = min(
            self.RECONNECT_DELAY_BASE * (2 ** (self.reconnect_attempts - 1)),
            self.RECONNECT_DELAY_MAX
        )
        logger.info(f"Reconnection attempt #{self.reconnect_attempts} in {delay}s...")
        time.sleep(delay)

        self.connect()

class TwitchBridge:
    """Bridge between Twitch chat and norns OSC with automatic reconnection"""

    # Command pattern: starts with ! followed by valid norns commands
    COMMAND_PREFIX = "!"
    # Matches: k1, k2:500, e1:5, e2:-3, w:500, d:100, k1+e2:5, k1+k2:300
    VALID_COMMANDS = re.compile(
        r'^([ke][1-3](:-?\d+)?|[wd]:\d+|wait:\d+|delay:\d+|'
        r'([ke][1-3](:-?\d+)?\+)+[ke][1-3](:-?\d+)?)$',
        re.IGNORECASE
    )

    # Status logging interval (seconds)
    STATUS_LOG_INTERVAL = 300  # Log status every 5 minutes

    def __init__(self, channel: str, token: str, norns_ip: str = "norns.local",
                 norns_port: int = 10111, command_prefix: str = "!"):
        self.channel = channel
        self.token = token
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

        # Stats tracking
        self.commands_processed = 0
        self.commands_rate_limited = 0
        self.start_time: Optional[float] = None
        self.last_status_log: Optional[float] = None

        logger.info(f"TwitchBridge initialized: channel=#{channel}, norns={norns_ip}:{norns_port}")

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
                logger.debug(f"Skipping invalid token: {token}")

        if valid_tokens:
            return ' '.join(valid_tokens)
        return None

    def handle_message(self, username: str, message: str):
        """Handle a chat message"""
        # Check user permissions
        if self.blocked_users and username.lower() in self.blocked_users:
            logger.debug(f"Blocked user {username} ignored")
            return
        if self.allowed_users and username.lower() not in self.allowed_users:
            logger.debug(f"User {username} not in allowed list")
            return

        # Parse command
        cmd = self.parse_chat_command(message)
        if not cmd:
            return

        # Rate limiting
        now = time.time()
        if now - self.last_command_time < self.min_command_interval:
            self.commands_rate_limited += 1
            logger.debug(f"Rate limited command from {username}: {cmd}")
            return
        self.last_command_time = now

        # Send to norns
        self.commands_processed += 1
        logger.info(f"CMD #{self.commands_processed} from {username}: {cmd}")
        self.osc.send("/twitch/cmd", cmd)

    def log_status(self, force: bool = False):
        """Log periodic status update"""
        now = time.time()
        if not force and self.last_status_log:
            if now - self.last_status_log < self.STATUS_LOG_INTERVAL:
                return

        self.last_status_log = now
        uptime = int(now - self.start_time) if self.start_time else 0
        irc_status = self.irc.get_status()

        logger.info(f"=== STATUS UPDATE ===")
        logger.info(f"  Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s")
        logger.info(f"  IRC connected: {irc_status['connected']}")
        logger.info(f"  IRC uptime: {irc_status['uptime_seconds']}s")
        logger.info(f"  Last data received: {irc_status['last_data_ago']}s ago")
        logger.info(f"  PING/PONG count: {irc_status['ping_count']}")
        logger.info(f"  Chat messages received: {irc_status['message_count']}")
        logger.info(f"  Commands processed: {self.commands_processed}")
        logger.info(f"  Commands rate-limited: {self.commands_rate_limited}")
        logger.info(f"  OSC messages sent: {self.osc.messages_sent}")
        logger.info(f"========================")

    def run(self):
        """Main loop with automatic reconnection"""
        logger.info("Starting Twitch-norns bridge...")
        self.running = True
        self.start_time = time.time()

        logger.info(f"Command prefix: {self.command_prefix}")
        logger.info("Valid commands: k1 k2 k3 k1:500 e1:5 e2:-3 w:500 etc.")
        logger.info("Press Ctrl+C to stop")

        while self.running:
            try:
                # Connect/reconnect
                if not self.irc.connected:
                    self.irc.connect()
                    logger.info("Connection established, listening for commands...")
                    self.log_status(force=True)

                # Process messages
                for username, message in self.irc.read_messages():
                    if not self.running:
                        break
                    self.handle_message(username, message)
                    self.log_status()  # Periodic status

            except KeyboardInterrupt:
                logger.info("Received interrupt signal, stopping...")
                break

            except ConnectionError as e:
                logger.error(f"Connection error: {e}")
                if self.running:
                    logger.info("Will attempt to reconnect...")
                    try:
                        self.irc.reconnect()
                    except Exception as re:
                        logger.error(f"Reconnection failed: {re}")
                        # Will retry on next loop iteration

            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                if self.running:
                    logger.info("Will attempt to reconnect after error...")
                    try:
                        time.sleep(5)
                        self.irc.reconnect()
                    except Exception as re:
                        logger.error(f"Reconnection failed: {re}")

        self.running = False
        self.log_status(force=True)
        self.irc.close()
        self.osc.close()
        logger.info("Bridge stopped")

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

    # With debug logging
    python twitch_bridge.py --channel mychannel --token abc123 --debug

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
    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--log-file',
        default=os.environ.get('TWITCH_BRIDGE_LOG'),
        help='Log to file (or set TWITCH_BRIDGE_LOG env var)'
    )

    args = parser.parse_args()

    # Configure logging level
    if args.debug:
        logging.getLogger('twitch-bridge').setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    # Configure file logging if requested
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logging.getLogger('twitch-bridge').addHandler(file_handler)
        logger.info(f"Logging to file: {args.log_file}")

    if not args.channel:
        parser.error("Channel name required. Use --channel or set TWITCH_CHANNEL")

    # Token is optional - can use anonymous connection for read-only
    token = args.token or ""
    if not token:
        logger.warning("No OAuth token provided. Using anonymous connection.")

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
