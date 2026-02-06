-- twitch-plays-norns
-- receive commands from twitch chat and control norns
--
-- command format:
--   k1, k2, k3     - button press (short)
--   k1:500         - button hold for 500ms
--   e1:5           - turn encoder clockwise by 5
--   e1:-3          - turn encoder counter-clockwise by 3
--   w:500 or d:500 - wait/delay for 500ms
--   k1+e2:5        - hold k1 while turning e2 by 5 (simultaneous)
--   k1+k2:300      - hold k1 and k2 together for 300ms
--   k1 e2:5 k3     - sequence of commands (space separated)
--
-- osc paths:
--   /twitch/cmd <string>   - execute command string
--   /twitch/raw <n> <z>    - raw key event (1-3, 0/1)
--   /twitch/enc <n> <d>    - raw encoder event (1-3, delta)

local mod = require 'core/mods'

local twitch = {}

-- configuration
twitch.enabled = true
twitch.default_press_duration = 100   -- ms for short press
twitch.command_delay = 50             -- ms between sequence items
twitch.queue = {}
twitch.processing = false

-- logging
local function log(msg)
  print("[twitch-plays] " .. msg)
end

-- forward declarations
local parse_combo

-- parse a single command token
-- returns: {type="key"|"enc"|"wait"|"combo", ...}
local function parse_command(token)
  if not token or token == "" then return nil end

  token = string.lower(token)

  -- check for combo command (contains +): k1+e2:5, k1+k2:300
  if string.find(token, "+") then
    return parse_combo(token)
  end

  -- wait/delay command: w:500, d:500, wait:500, delay:500
  local wait_match = string.match(token, "^[wd]:(%d+)$") or
                     string.match(token, "^wait:(%d+)$") or
                     string.match(token, "^delay:(%d+)$")
  if wait_match then
    return {
      type = "wait",
      duration = tonumber(wait_match)
    }
  end

  -- key command: k1, k2, k3, k1:500
  local key_match = string.match(token, "^k([1-3]):?(-?%d*)$")
  if key_match then
    local n = tonumber(string.match(token, "^k([1-3])"))
    local duration = string.match(token, ":(-?%d+)$")
    return {
      type = "key",
      n = n,
      duration = duration and tonumber(duration) or twitch.default_press_duration
    }
  end

  -- encoder command: e1:5, e2:-3
  local enc_n = string.match(token, "^e([1-3])")
  if enc_n then
    local delta = string.match(token, ":(-?%d+)$")
    if delta then
      return {
        type = "enc",
        n = tonumber(enc_n),
        delta = tonumber(delta)
      }
    else
      -- e1 without value means delta of 1
      return {
        type = "enc",
        n = tonumber(enc_n),
        delta = 1
      }
    end
  end

  return nil
end

-- parse combo command: k1+e2:5, k1+k2:300, k2+e1:3+e3:-2
-- returns: {type="combo", keys={1,2}, encs={{n=1,delta=5}}, duration=ms}
parse_combo = function(token)
  local combo = {
    type = "combo",
    keys = {},
    encs = {},
    duration = twitch.default_press_duration
  }

  -- split by +
  for part in string.gmatch(token, "[^+]+") do
    -- key: k1, k2, k3, k1:500
    local key_n = string.match(part, "^k([1-3])")
    if key_n then
      table.insert(combo.keys, tonumber(key_n))
      -- check for duration on key
      local dur = string.match(part, ":(%d+)$")
      if dur then
        combo.duration = tonumber(dur)
      end
    end

    -- encoder: e1:5, e2:-3
    local enc_n = string.match(part, "^e([1-3])")
    if enc_n then
      local delta = string.match(part, ":(-?%d+)$") or "1"
      table.insert(combo.encs, {
        n = tonumber(enc_n),
        delta = tonumber(delta)
      })
    end
  end

  -- only valid if we have at least one key or encoder
  if #combo.keys > 0 or #combo.encs > 0 then
    return combo
  end
  return nil
end

-- parse a full command string into a list of commands
local function parse_command_string(str)
  local commands = {}

  -- split by spaces
  for token in string.gmatch(str, "%S+") do
    local cmd = parse_command(token)
    if cmd then
      table.insert(commands, cmd)
    end
  end

  return commands
end

-- execute a single command (may be async for combos)
-- returns duration in ms that caller should wait before next command
local function execute_command(cmd)
  if not twitch.enabled then return 0 end

  if cmd.type == "key" then
    log("key " .. cmd.n .. " press (duration: " .. cmd.duration .. "ms)")
    -- press
    _norns.key(cmd.n, 1)
    -- schedule release
    clock.run(function()
      clock.sleep(cmd.duration / 1000)
      _norns.key(cmd.n, 0)
    end)
    return cmd.duration

  elseif cmd.type == "enc" then
    log("enc " .. cmd.n .. " delta: " .. cmd.delta)
    _norns.enc(cmd.n, cmd.delta)
    return 0

  elseif cmd.type == "wait" then
    log("wait " .. cmd.duration .. "ms")
    return cmd.duration

  elseif cmd.type == "combo" then
    local key_str = ""
    for _, k in ipairs(cmd.keys) do
      key_str = key_str .. "k" .. k .. " "
    end
    local enc_str = ""
    for _, e in ipairs(cmd.encs) do
      enc_str = enc_str .. "e" .. e.n .. ":" .. e.delta .. " "
    end
    log("combo [" .. key_str .. "] + [" .. enc_str .. "] (duration: " .. cmd.duration .. "ms)")

    -- press all keys
    for _, k in ipairs(cmd.keys) do
      _norns.key(k, 1)
    end

    -- turn all encoders (spread across the duration)
    if #cmd.encs > 0 then
      clock.run(function()
        for _, e in ipairs(cmd.encs) do
          -- spread encoder turns with small delays
          local steps = math.abs(e.delta)
          local dir = e.delta > 0 and 1 or -1
          local step_delay = (cmd.duration / 1000) / (steps + 1)

          for i = 1, steps do
            clock.sleep(step_delay)
            _norns.enc(e.n, dir)
          end
        end
      end)
    end

    -- schedule key releases
    clock.run(function()
      clock.sleep(cmd.duration / 1000)
      for _, k in ipairs(cmd.keys) do
        _norns.key(k, 0)
      end
    end)

    return cmd.duration
  end

  return 0
end

-- process command queue
local function process_queue()
  if twitch.processing then return end
  if #twitch.queue == 0 then return end

  twitch.processing = true

  clock.run(function()
    while #twitch.queue > 0 do
      local commands = table.remove(twitch.queue, 1)

      for i, cmd in ipairs(commands) do
        local cmd_duration = execute_command(cmd)

        -- wait between commands in a sequence
        if i < #commands then
          local wait_time = twitch.command_delay + cmd_duration
          clock.sleep(wait_time / 1000)
        end
      end

      -- small delay between queued command strings
      if #twitch.queue > 0 then
        clock.sleep(0.1)
      end
    end

    twitch.processing = false
  end)
end

-- queue a command string for execution
function twitch.execute(cmd_string)
  if not twitch.enabled then
    log("disabled, ignoring: " .. cmd_string)
    return
  end

  log("received: " .. cmd_string)

  local commands = parse_command_string(cmd_string)
  if #commands > 0 then
    table.insert(twitch.queue, commands)
    process_queue()
  else
    log("no valid commands parsed")
  end
end

-- direct key simulation
function twitch.key(n, z)
  if not twitch.enabled then return end
  if n < 1 or n > 3 then return end
  log("raw key " .. n .. " " .. z)
  _norns.key(n, z)
end

-- direct encoder simulation
function twitch.enc(n, d)
  if not twitch.enabled then return end
  if n < 1 or n > 3 then return end
  log("raw enc " .. n .. " " .. d)
  _norns.enc(n, d)
end

-- enable/disable
function twitch.set_enabled(state)
  twitch.enabled = state
  log(state and "enabled" or "disabled")
end

-- osc handler
local function osc_handler(path, args, from)
  if path == "/twitch/cmd" then
    if args[1] then
      twitch.execute(tostring(args[1]))
    end
  elseif path == "/twitch/raw" or path == "/twitch/key" then
    if args[1] and args[2] then
      twitch.key(math.floor(args[1]), math.floor(args[2]))
    end
  elseif path == "/twitch/enc" then
    if args[1] and args[2] then
      twitch.enc(math.floor(args[1]), math.floor(args[2]))
    end
  elseif path == "/twitch/enable" then
    twitch.set_enabled(args[1] and args[1] ~= 0)
  elseif path == "/twitch/config" then
    if args[1] == "press_duration" and args[2] then
      twitch.default_press_duration = math.max(10, math.floor(args[2]))
      log("press duration: " .. twitch.default_press_duration .. "ms")
    elseif args[1] == "command_delay" and args[2] then
      twitch.command_delay = math.max(0, math.floor(args[2]))
      log("command delay: " .. twitch.command_delay .. "ms")
    end
  end
end

-- register with mod system
mod.hook.register("system_post_startup", "twitch-plays-init", function()
  log("initialized")
  log("listening on OSC paths: /twitch/cmd, /twitch/key, /twitch/enc")

  -- hook into osc event handler
  local original_osc_event = _norns.osc.event
  _norns.osc.event = function(path, args, from)
    -- check if it's a twitch command
    if string.sub(path, 1, 7) == "/twitch" then
      osc_handler(path, args, from)
    else
      -- pass to original handler
      if original_osc_event then
        original_osc_event(path, args, from)
      end
    end
  end
end)

mod.hook.register("system_pre_shutdown", "twitch-plays-cleanup", function()
  log("shutting down")
  twitch.enabled = false
  twitch.queue = {}
end)

-- expose globally for debugging/scripting
_G.twitch_plays = twitch

return twitch
